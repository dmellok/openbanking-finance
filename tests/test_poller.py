from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from app.db import session_scope
from app.models import Account, BalanceSnapshot, Connection, SyncRun, Trade, Transaction
from app.poller import run_once
from sqlmodel import select

from tests.conftest import make_mock_client
from tests.fixtures import (
    ACCOUNTS_RESPONSE,
    BALANCES_RESPONSE,
    CONNECTIONS_RESPONSE,
    TRADES_RESPONSE,
    TRANSACTIONS_RESPONSE,
)


def _build_handler(transactions_capture: list[dict[str, str]] | None = None) -> Any:
    """Handler that returns Redbark sample fixtures by path."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        if path == "/v1/connections":
            return httpx.Response(200, json=CONNECTIONS_RESPONSE)
        if path == "/v1/accounts":
            return httpx.Response(200, json=ACCOUNTS_RESPONSE)
        if path == "/v1/balances":
            # Echo only the ids requested.
            ids = set(params.get("accountIds", "").split(","))
            data = [
                row
                for row in BALANCES_RESPONSE["data"]  # type: ignore[union-attr]
                if row["accountId"] in ids
            ]
            return httpx.Response(200, json={"data": data})
        if path == "/v1/transactions":
            if transactions_capture is not None:
                transactions_capture.append(params)
            account_id = params.get("accountId")
            data = [
                tx
                for tx in TRANSACTIONS_RESPONSE["data"]  # type: ignore[union-attr]
                if tx["accountId"] == account_id
            ]
            return httpx.Response(
                200,
                json={
                    "data": data,
                    "pagination": {
                        "total": len(data),
                        "limit": 500,
                        "offset": 0,
                        "hasMore": False,
                    },
                },
            )
        if path == "/v1/trades":
            account_id = params.get("accountId")
            data = [
                tr
                for tr in TRADES_RESPONSE["data"]  # type: ignore[union-attr]
                if tr["accountId"] == account_id
            ]
            return httpx.Response(
                200,
                json={
                    "data": data,
                    "pagination": {
                        "total": len(data),
                        "limit": 500,
                        "offset": 0,
                        "hasMore": False,
                    },
                },
            )
        return httpx.Response(404, json={"message": f"unmocked path {path}"})

    return handler


async def test_run_once_populates_all_entities_and_sets_watermarks(temp_db: Path) -> None:
    handler = _build_handler()
    client, _http = make_mock_client(handler)
    counts = await run_once(client)
    await client.aclose()

    assert counts == {
        "connections": 2,
        "accounts": 4,
        "balances": 4,
        "transactions": 6,
        "trades": 2,
        "failed_accounts": 0,
        "skipped_accounts": 0,
    }

    with session_scope() as session:
        # Connections + accounts upserted.
        connections = session.exec(select(Connection)).all()
        accounts = session.exec(select(Account)).all()
        assert {c.id for c in connections} == {
            "e8f1a2b3-7c4d-5e6f-8a9b-0c1d2e3f4a5b",
            "b7c4a1e2-8d3f-4e9a-9c5b-1f2a3e4d5c6b",
        }
        assert len(accounts) == 4

        # Watermarks bumped on every account.
        for a in accounts:
            assert a.last_polled_transactions_at is not None

        # Trades watermark only set for the brokerage investment account.
        wm_trades = {a.id: a.last_polled_trades_at for a in accounts}
        investment_id = "d4e5f6a7-b8c9-0123-d4e5-f6a7b8c90123"
        assert wm_trades[investment_id] is not None
        assert all(
            wm_trades[aid] is None for aid in wm_trades if aid != investment_id
        ), "trades should only be polled for investment accounts on brokerage connections"

        # Balance snapshots are append-only; null fields stored as NULL.
        snaps = session.exec(select(BalanceSnapshot)).all()
        assert len(snaps) == 4
        null_snap = next(
            s for s in snaps if s.account_id == "d4e5f6a7-b8c9-0123-d4e5-f6a7b8c90123"
        )
        assert null_snap.current_balance is None
        assert null_snap.available_balance is None
        assert null_snap.currency is None

        # Transactions persisted with Decimal amounts.
        txs = session.exec(select(Transaction)).all()
        assert len(txs) == 6
        woolies = next(t for t in txs if t.merchant_name == "Woolworths")
        assert woolies.amount == Decimal("-64.20")
        assert woolies.direction == "debit"
        assert woolies.category == "FOOD_AND_DRINK"

        # Trades persisted only for the investment account.
        trades = session.exec(select(Trade)).all()
        assert len(trades) == 2
        assert all(t.account_id == investment_id for t in trades)

        # SyncRun recorded ok.
        runs = session.exec(select(SyncRun)).all()
        assert len(runs) == 1
        assert runs[0].status == "ok"
        assert runs[0].counts == counts


async def test_second_poll_uses_watermark_minus_24h(temp_db: Path) -> None:
    """After the first cycle sets watermarks, the next cycle should request
    `from = watermark - 24h` for transactions and trades."""
    # First cycle.
    handler1 = _build_handler()
    client1, _http1 = make_mock_client(handler1)
    await run_once(client1)
    await client1.aclose()

    # Capture the second cycle's `from` params on /v1/transactions.
    captured: list[dict[str, str]] = []
    handler2 = _build_handler(transactions_capture=captured)
    client2, _http2 = make_mock_client(handler2)
    await run_once(client2)
    await client2.aclose()

    # Each account should have a `from` ~ 24h ago (give or take a few seconds).
    assert len(captured) >= 1
    now = datetime.now(UTC)
    for params in captured:
        assert "from" in params, f"missing from in {params}"
        # Strip the trailing 'Z' or +00:00 — fromisoformat handles both in py3.12.
        from_dt = datetime.fromisoformat(params["from"])
        if from_dt.tzinfo is None:
            from_dt = from_dt.replace(tzinfo=UTC)
        delta = now - from_dt
        # Should be approximately 24h ago — anywhere between 23h and 25h.
        assert timedelta(hours=23) < delta < timedelta(hours=25), (
            f"expected ~24h overlap, got {delta}"
        )


async def test_one_account_503_does_not_abort_cycle(temp_db: Path) -> None:
    """A 503 on one account's /v1/transactions must NOT kill the whole cycle.
    Other accounts still get polled, the failing account's watermark stays put."""

    bad_account = "a1b2c3d4-e5f6-7890-a1b2-c3d4e5f67890"  # the Everyday Account

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        if path == "/v1/connections":
            return httpx.Response(200, json=CONNECTIONS_RESPONSE)
        if path == "/v1/accounts":
            return httpx.Response(200, json=ACCOUNTS_RESPONSE)
        if path == "/v1/balances":
            return httpx.Response(
                200,
                json={"data": [r for r in BALANCES_RESPONSE["data"] if r["accountId"] in params.get("accountIds", "").split(",")]},  # type: ignore[union-attr]
            )
        if path == "/v1/transactions":
            account_id = params.get("accountId")
            if account_id == bad_account:
                return httpx.Response(503, json={"error": {"message": "provider down"}})
            data = [
                tx for tx in TRANSACTIONS_RESPONSE["data"]  # type: ignore[union-attr]
                if tx["accountId"] == account_id
            ]
            return httpx.Response(
                200,
                json={"data": data, "pagination": {"total": len(data), "limit": 500, "offset": 0, "hasMore": False}},
            )
        if path == "/v1/trades":
            account_id = params.get("accountId")
            data = [t for t in TRADES_RESPONSE["data"] if t["accountId"] == account_id]  # type: ignore[union-attr]
            return httpx.Response(
                200,
                json={"data": data, "pagination": {"total": len(data), "limit": 500, "offset": 0, "hasMore": False}},
            )
        return httpx.Response(404, json={"message": f"unmocked {path}"})

    # Patch backoff to 0 so the 503-retry doesn't actually delay the test.
    import app.redbark_client as client_mod

    real_backoff = client_mod._backoff
    client_mod._backoff = lambda _attempt: 0.0
    try:
        client, _http = make_mock_client(handler)
        counts = await run_once(client)
        await client.aclose()
    finally:
        client_mod._backoff = real_backoff

    assert counts["failed_accounts"] == 1
    # Other accounts (the Savings, Credit Card, Investment) still get their tx pulled.
    assert counts["transactions"] >= 1
    assert counts["trades"] == 2  # investment account succeeded
    with session_scope() as session:
        accounts = {a.id: a for a in session.exec(select(Account)).all()}
        # Bad account watermark stayed null (no successful poll yet).
        assert accounts[bad_account].last_polled_transactions_at is None
        # Investment account got its tx + trades watermarks bumped.
        inv = accounts["d4e5f6a7-b8c9-0123-d4e5-f6a7b8c90123"]
        assert inv.last_polled_transactions_at is not None
        assert inv.last_polled_trades_at is not None
        # SyncRun marked partial with detail mentioning the failing account.
        runs = session.exec(select(SyncRun)).all()
        assert runs[0].status == "partial"
        assert bad_account in (runs[0].detail or "")


async def test_inactive_connection_accounts_are_skipped(temp_db: Path) -> None:
    """Accounts on connections with status != 'active' must be skipped entirely
    (no /v1/transactions or /v1/trades calls at all)."""
    invalidated_response = {
        "data": [
            {**c, "status": "invalidated"} if c["category"] == "banking" else c
            for c in CONNECTIONS_RESPONSE["data"]  # type: ignore[union-attr]
        ]
    }
    tx_calls: list[str] = []
    trade_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        if path == "/v1/connections":
            return httpx.Response(200, json=invalidated_response)
        if path == "/v1/accounts":
            return httpx.Response(200, json=ACCOUNTS_RESPONSE)
        if path == "/v1/balances":
            return httpx.Response(200, json={"data": []})
        if path == "/v1/transactions":
            tx_calls.append(params.get("accountId", ""))
            return httpx.Response(200, json={"data": [], "pagination": {"total": 0, "limit": 500, "offset": 0, "hasMore": False}})
        if path == "/v1/trades":
            trade_calls.append(params.get("accountId", ""))
            return httpx.Response(200, json={"data": [], "pagination": {"total": 0, "limit": 500, "offset": 0, "hasMore": False}})
        return httpx.Response(404, json={"message": f"unmocked {path}"})

    client, _http = make_mock_client(handler)
    counts = await run_once(client)
    await client.aclose()

    # The 3 banking accounts are on the invalidated connection — they must be skipped.
    banking_account_ids = {
        "a1b2c3d4-e5f6-7890-a1b2-c3d4e5f67890",
        "c3d4e5f6-a7b8-9012-c3d4-e5f6a7b89012",
        "b2c3d4e5-f6a7-8901-b2c3-d4e5f6a78901",
    }
    assert not any(aid in banking_account_ids for aid in tx_calls)
    # The investment account on the active brokerage connection still gets polled.
    assert "d4e5f6a7-b8c9-0123-d4e5-f6a7b8c90123" in tx_calls
    assert "d4e5f6a7-b8c9-0123-d4e5-f6a7b8c90123" in trade_calls
    assert counts["skipped_accounts"] == 3
    assert counts["failed_accounts"] == 0


async def test_run_once_records_error_on_failure(temp_db: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    client, _http = make_mock_client(handler)
    try:
        await run_once(client)
    except Exception:
        pass
    finally:
        await client.aclose()

    with session_scope() as session:
        runs = session.exec(select(SyncRun)).all()
        assert len(runs) == 1
        assert runs[0].status == "error"
        assert runs[0].detail is not None
