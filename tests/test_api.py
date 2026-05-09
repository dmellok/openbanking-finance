from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.api import create_app
from app.db import session_scope
from app.models import Account, BalanceSnapshot, Connection, Transaction


@pytest.fixture
async def api_client(temp_db: Path) -> httpx.AsyncClient:
    app = create_app(with_scheduler=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _seed() -> None:
    """Insert a small fixture set for API endpoint tests."""
    with session_scope() as session:
        session.add(
            Connection(
                id="conn-1",
                provider="fiskil",
                category="banking",
                institution_name="Westpac",
                status="active",
            )
        )
        session.flush()
        session.add(
            Account(
                id="acc-1",
                connection_id="conn-1",
                provider="fiskil",
                name="Everyday",
                masked_number="xxxx1234",
                type="transaction",
                institution_name="Westpac",
                currency="AUD",
            )
        )
        session.add(
            Account(
                id="acc-2",
                connection_id="conn-1",
                provider="fiskil",
                name="Savings",
                masked_number="xxxx9999",
                type="savings",
                institution_name="Westpac",
                currency="AUD",
            )
        )
        session.flush()
        # Two snapshots per account so we can exercise "latest" logic.
        for offset_days, total_acc1, total_acc2 in [(2, "1000.00", "5000.00"), (0, "1234.56", "5500.00")]:
            taken_at = datetime.now(UTC) - timedelta(days=offset_days)
            session.add(
                BalanceSnapshot(
                    account_id="acc-1",
                    current_balance=Decimal(total_acc1),
                    available_balance=Decimal(total_acc1),
                    currency="AUD",
                    taken_at=taken_at,
                )
            )
            session.add(
                BalanceSnapshot(
                    account_id="acc-2",
                    current_balance=Decimal(total_acc2),
                    available_balance=Decimal(total_acc2),
                    currency="AUD",
                    taken_at=taken_at,
                )
            )

        # Transactions across two months and several categories.
        txs = [
            ("tx-1", "acc-1", date(2026, 4, 22), "-64.20", "debit", "FOOD_AND_DRINK", "Woolworths"),
            ("tx-2", "acc-1", date(2026, 4, 18), "4250.00", "credit", "INCOME", None),
            ("tx-3", "acc-1", date(2026, 4, 15), "-412.50", "debit", "TRAVEL", "Qantas"),
            ("tx-4", "acc-1", date(2026, 4, 12), "-22.99", "debit", "ENTERTAINMENT", "Netflix"),
            ("tx-5", "acc-2", date(2026, 4, 1), "62.18", "credit", "INCOME", None),
            ("tx-6", "acc-1", date(2026, 3, 22), "-35.00", "debit", "FOOD_AND_DRINK", "Woolworths"),
            ("tx-7", "acc-1", date(2026, 3, 18), "4250.00", "credit", "INCOME", None),
            # An internal transfer that should be excluded from cashflow.
            ("tx-8", "acc-1", date(2026, 4, 21), "500.00", "credit", "TRANSFER_IN", None),
        ]
        for tx_id, account_id, local_date, amount, direction, category, merchant in txs:
            session.add(
                Transaction(
                    id=tx_id,
                    account_id=account_id,
                    posted_at=datetime.combine(local_date, datetime.min.time(), tzinfo=UTC),
                    local_date=local_date,
                    amount=Decimal(amount),
                    currency="AUD",
                    direction=direction,
                    description=f"{merchant or category} txn",
                    merchant_name=merchant,
                    category=category,
                )
            )


async def test_healthz_works_against_empty_db(api_client: httpx.AsyncClient) -> None:
    resp = await api_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "service": "pyfinance-v2"}


async def test_endpoints_return_empty_arrays_when_no_data(api_client: httpx.AsyncClient) -> None:
    for path in [
        "/api/accounts",
        "/api/transactions",
        "/api/spending/by-category",
        "/api/spending/top-merchants",
        "/api/net-worth/series",
        "/api/net-worth/per-account",
        "/api/cashflow/monthly",
        "/api/budgets",
        "/api/sync-runs",
    ]:
        resp = await api_client.get(path)
        assert resp.status_code == 200, f"{path} failed: {resp.text}"
        body = resp.json()
        if path == "/api/transactions":
            assert body == {"data": [], "pagination": {"total": 0, "limit": 100, "offset": 0, "hasMore": False}}
        else:
            assert body == [], f"{path} not empty: {body!r}"


async def test_accounts_returns_latest_balance(api_client: httpx.AsyncClient) -> None:
    _seed()
    resp = await api_client.get("/api/accounts")
    assert resp.status_code == 200
    body = resp.json()
    assert {a["id"] for a in body} == {"acc-1", "acc-2"}
    by_id = {a["id"]: a for a in body}
    # latest_balance is the second snapshot (offset_days=0, the "now" snapshot).
    assert by_id["acc-1"]["latest_balance"] == "1234.56"
    assert by_id["acc-2"]["latest_balance"] == "5500.00"
    assert by_id["acc-1"]["currency"] == "AUD"


async def test_transactions_filter_and_paginate(api_client: httpx.AsyncClient) -> None:
    _seed()
    resp = await api_client.get(
        "/api/transactions",
        params={"from": "2026-04-01", "to": "2026-04-30", "account_id": "acc-1", "limit": 2, "offset": 0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pagination"]["total"] == 5  # tx-1..4 + tx-8 in April for acc-1
    assert len(body["data"]) == 2
    assert body["pagination"]["hasMore"] is True
    # Decimals serialised as strings.
    for tx in body["data"]:
        assert isinstance(tx["amount"], str)


async def test_spending_by_category(api_client: httpx.AsyncClient) -> None:
    _seed()
    resp = await api_client.get(
        "/api/spending/by-category",
        params={"from": "2026-04-01", "to": "2026-04-30"},
    )
    assert resp.status_code == 200
    body = resp.json()
    by_cat = {row["category"]: Decimal(row["total"]) for row in body}
    assert by_cat["TRAVEL"] == Decimal("412.50")
    assert by_cat["FOOD_AND_DRINK"] == Decimal("64.20")
    assert by_cat["ENTERTAINMENT"] == Decimal("22.99")
    # Income should not appear.
    assert "INCOME" not in by_cat


async def test_top_merchants(api_client: httpx.AsyncClient) -> None:
    _seed()
    resp = await api_client.get(
        "/api/spending/top-merchants",
        params={"from": "2026-03-01", "to": "2026-04-30", "limit": 5},
    )
    assert resp.status_code == 200
    body = resp.json()
    by_merchant = {row["merchant_name"]: row for row in body}
    # Woolworths appears twice, summing -64.20 and -35.00 → 99.20
    assert Decimal(by_merchant["Woolworths"]["total"]) == Decimal("99.20")
    assert by_merchant["Woolworths"]["count"] == 2


async def test_cashflow_monthly_excludes_transfers(api_client: httpx.AsyncClient) -> None:
    _seed()
    resp = await api_client.get(
        "/api/cashflow/monthly",
        params={"from": "2026-03-01", "to": "2026-04-30"},
    )
    assert resp.status_code == 200
    body = resp.json()
    by_month = {row["month"]: row for row in body}
    assert "2026-03" in by_month
    assert "2026-04" in by_month
    apr = by_month["2026-04"]
    # April income = 4250.00 + 62.18 (TRANSFER_IN of 500 excluded)
    assert Decimal(apr["income"]) == Decimal("4312.18")
    # April expenses = 64.20 + 412.50 + 22.99 = 499.69
    assert Decimal(apr["expenses"]) == Decimal("499.69")
    assert Decimal(apr["net"]) == Decimal("3812.49")
    # Savings rate is a float in [0, 1].
    assert 0.0 < apr["savings_rate"] < 1.0


async def test_net_worth_series_sums_per_taken_at(api_client: httpx.AsyncClient) -> None:
    _seed()
    resp = await api_client.get("/api/net-worth/series")
    assert resp.status_code == 200
    body = resp.json()
    # Two snapshots per account at two different taken_at => 2 points.
    assert len(body) == 2
    # Last point: 1234.56 + 5500.00 = 6734.56
    assert Decimal(body[-1]["total"]) == Decimal("6734.56")


async def test_budgets_create_and_list(api_client: httpx.AsyncClient) -> None:
    _seed()
    resp = await api_client.post(
        "/api/budgets",
        json={"category": "FOOD_AND_DRINK", "monthly_limit": "800.00", "currency": "AUD"},
    )
    assert resp.status_code == 200
    created = resp.json()
    assert created["category"] == "FOOD_AND_DRINK"
    assert created["monthly_limit"] == "800.00"

    # Upsert: same category+currency should update, not duplicate.
    resp = await api_client.post(
        "/api/budgets",
        json={"category": "FOOD_AND_DRINK", "monthly_limit": "1000.00", "currency": "AUD"},
    )
    assert resp.status_code == 200

    resp = await api_client.get("/api/budgets")
    body = resp.json()
    assert len(body) == 1
    assert body[0]["monthly_limit"] == "1000.00"
