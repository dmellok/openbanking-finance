from __future__ import annotations

from datetime import UTC, date, datetime
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


def _seed_insights() -> None:
    """Seed transactions across categories/merchants/dates with `posted_at` set,
    plus balance snapshots for a 3-month history."""
    with session_scope() as session:
        session.add(Connection(id="conn-1", provider="fiskil", category="banking",
                               institution_name="Westpac", status="active"))
        session.flush()
        session.add(Account(id="acc-1", connection_id="conn-1", name="Everyday",
                            type="transaction", currency="AUD"))
        session.flush()

        txs = [
            # April: income 5000, expenses across categories
            ("t1", date(2026, 4, 1), "5000.00", "credit", "INCOME", None, 9),
            ("t2", date(2026, 4, 5), "-120.00", "debit", "FOOD_AND_DRINK", "Woolworths", 18),
            ("t3", date(2026, 4, 10), "-80.00", "debit", "FOOD_AND_DRINK", "Coles", 12),
            ("t4", date(2026, 4, 15), "-300.00", "debit", "TRAVEL", "Qantas", 8),
            ("t5", date(2026, 4, 20), "-50.00", "debit", "ENTERTAINMENT", "Netflix", 22),
            ("t6", date(2026, 4, 22), "-200.00", "debit", "FOOD_AND_DRINK", "Woolworths", 13),
            # March
            ("t7", date(2026, 3, 1), "5000.00", "credit", "INCOME", None, 9),
            ("t8", date(2026, 3, 12), "-150.00", "debit", "FOOD_AND_DRINK", "Coles", 11),
            ("t9", date(2026, 3, 20), "-400.00", "debit", "TRAVEL", "Jetstar", 7),
            # An internal transfer (must be excluded everywhere)
            ("t10", date(2026, 4, 18), "1000.00", "credit", "TRANSFER_IN", None, 10),
        ]
        for tid, d, amt, dirn, cat, merchant, hour in txs:
            session.add(Transaction(
                id=tid, account_id="acc-1",
                local_date=d,
                posted_at=datetime(d.year, d.month, d.day, hour, 0, tzinfo=UTC),
                amount=Decimal(amt), currency="AUD",
                direction=dirn, category=cat, merchant_name=merchant,
                description=f"{merchant or cat} {tid}",
            ))

        # Balance snapshots: month-end values for Feb, Mar, Apr; plus a "current" recent one.
        for d, balance in [(date(2026, 2, 28), "10000"), (date(2026, 3, 31), "12000"), (date(2026, 4, 30), "15000")]:
            session.add(BalanceSnapshot(
                account_id="acc-1",
                current_balance=Decimal(balance),
                available_balance=Decimal(balance),
                currency="AUD",
                taken_at=datetime(d.year, d.month, d.day, 23, 59, tzinfo=UTC),
            ))


async def test_calendar_returns_daily_totals_excluding_transfers(api_client: httpx.AsyncClient) -> None:
    _seed_insights()
    resp = await api_client.get("/api/insights/calendar", params={
        "from": "2026-03-01", "to": "2026-04-30",
    })
    assert resp.status_code == 200
    body = resp.json()
    by_date = {row["date"]: Decimal(row["total"]) for row in body}
    assert by_date[date(2026, 4, 5).isoformat()] == Decimal("120")
    assert by_date[date(2026, 4, 22).isoformat()] == Decimal("200")
    # April 18 was a TRANSFER_IN, no debit on that day → not in heatmap.
    assert date(2026, 4, 18).isoformat() not in by_date


async def test_treemap_groups_merchants_under_category(api_client: httpx.AsyncClient) -> None:
    _seed_insights()
    resp = await api_client.get("/api/insights/treemap", params={
        "from": "2026-03-01", "to": "2026-04-30",
    })
    assert resp.status_code == 200
    body = resp.json()
    by_cat = {row["name"]: row for row in body}
    assert "FOOD_AND_DRINK" in by_cat
    assert "TRAVEL" in by_cat
    food = by_cat["FOOD_AND_DRINK"]
    # 120 + 80 + 200 + 150 = 550
    assert Decimal(food["value"]) == Decimal("550")
    food_merchants = {m["name"]: Decimal(m["value"]) for m in food["children"]}
    assert food_merchants == {"Woolworths": Decimal("320"), "Coles": Decimal("230")}


async def test_sankey_routes_income_to_categories_and_savings(api_client: httpx.AsyncClient) -> None:
    _seed_insights()
    resp = await api_client.get("/api/insights/sankey", params={
        "from": "2026-04-01", "to": "2026-04-30",
    })
    assert resp.status_code == 200
    body = resp.json()
    node_names = [n["name"] for n in body["nodes"]]
    assert node_names[0] == "Income"
    assert "Savings" in node_names  # 5000 - 750 = 4250 leftover
    by_target = {link["target"]: Decimal(link["value"]) for link in body["links"]}
    # Income spent on categories.
    assert by_target["FOOD_AND_DRINK"] == Decimal("400")  # 120 + 80 + 200
    assert by_target["TRAVEL"] == Decimal("300")
    assert by_target["ENTERTAINMENT"] == Decimal("50")
    # Leftover → savings (5000 - 750 = 4250).
    assert by_target["Savings"] == Decimal("4250")


async def test_time_heatmap_buckets_by_dow_and_hour(api_client: httpx.AsyncClient) -> None:
    _seed_insights()
    resp = await api_client.get("/api/insights/time-heatmap", params={
        "from": "2026-03-01", "to": "2026-04-30",
    })
    assert resp.status_code == 200
    body = resp.json()
    # t2 was at 2026-04-05 (Sunday) at 18:00 → dow=0, hour=18
    sunday_18 = next((r for r in body if r["day_of_week"] == 0 and r["hour"] == 18), None)
    assert sunday_18 is not None
    assert Decimal(sunday_18["total"]) == Decimal("120")


async def test_forecast_uses_rolling_window_for_monthly_net(api_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin "today" so the rolling window covers the seeded data.
    import app.api as api_mod
    real_dt = api_mod.datetime

    class _Frozen(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt(2026, 5, 1, tzinfo=UTC)

    monkeypatch.setattr(api_mod, "datetime", _Frozen)

    _seed_insights()
    resp = await api_client.get("/api/forecast", params={"rolling_days": 90, "horizon_months": 6})
    assert resp.status_code == 200
    d = resp.json()
    # Latest snapshot is 2026-04-30 with balance 15000.
    assert Decimal(d["current_net_worth"]) == Decimal("15000")
    # Rolling 90d income (excl. transfer): 5000 + 5000 = 10000
    assert Decimal(d["rolling_income"]) == Decimal("10000")
    # Rolling 90d expenses: 120 + 80 + 300 + 50 + 200 + 150 + 400 = 1300
    assert Decimal(d["rolling_expenses"]) == Decimal("1300")
    assert Decimal(d["rolling_net"]) == Decimal("8700")
    assert d["history"]  # has month-end points
    assert len(d["projection"]) == 6
    assert d["projection"][0]["projected"] is True


async def test_forecast_target_calculator(api_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api as api_mod
    real_dt = api_mod.datetime

    class _Frozen(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt(2026, 5, 1, tzinfo=UTC)

    monkeypatch.setattr(api_mod, "datetime", _Frozen)

    _seed_insights()
    resp = await api_client.get("/api/forecast", params={
        "rolling_days": 90, "target_balance": "30000",
    })
    assert resp.status_code == 200
    d = resp.json()
    assert d["target"] is not None
    assert Decimal(d["target"]["balance"]) == Decimal("30000")
    # delta=15000, monthly_net = 8700 / (90/30.4375) ~= 2941.67 → ~5.1 months
    months = d["target"]["months_to_target"]
    assert months is not None
    assert 4.5 < months < 6.0


async def test_trends_rolling_spend_window(api_client: httpx.AsyncClient) -> None:
    _seed_insights()
    resp = await api_client.get("/api/trends/rolling-spend", params={
        "from": "2026-04-25", "to": "2026-04-30", "window": 30,
    })
    assert resp.status_code == 200
    body = resp.json()
    # Window covers 25/3..25/4 onwards, so the rolling totals should include
    # the April expenses we seeded. Rolling on 2026-04-30 includes everything
    # from 2026-04-01 onwards: 120+80+300+50+200 = 750
    by_date = {row["date"]: Decimal(row["rolling_total"]) for row in body}
    assert by_date["2026-04-30"] == Decimal("750")


async def test_trends_category_by_month(api_client: httpx.AsyncClient) -> None:
    _seed_insights()
    resp = await api_client.get("/api/trends/category-by-month", params={
        "from": "2026-03-01", "to": "2026-04-30",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["months"] == ["2026-03", "2026-04"]
    by_cat = {s["name"]: [Decimal(v) for v in s["data"]] for s in body["series"]}
    # FOOD_AND_DRINK: 150 in March (Coles), 400 in April (120+80+200)
    assert by_cat["FOOD_AND_DRINK"] == [Decimal("150"), Decimal("400")]
    assert by_cat["TRAVEL"] == [Decimal("400"), Decimal("300")]


async def test_trends_day_of_month_averages_across_months(api_client: httpx.AsyncClient) -> None:
    _seed_insights()
    resp = await api_client.get("/api/trends/day-of-month", params={
        "from": "2026-03-01", "to": "2026-04-30",
    })
    assert resp.status_code == 200
    body = resp.json()
    by_day = {row["day"]: row for row in body}
    # Day 12: only March (150), 1 month → avg = 150
    assert Decimal(by_day[12]["avg"]) == Decimal("150")
    assert by_day[12]["months_seen"] == 1
    # Day 20: $50 (Apr) + $400 (Mar) = $450 across 2 months → avg = 225
    assert Decimal(by_day[20]["avg"]) == Decimal("225")
    assert by_day[20]["months_seen"] == 2


async def test_insights_endpoints_empty_db(api_client: httpx.AsyncClient) -> None:
    """Empty-DB sanity: nothing should crash, all endpoints return reasonable empty payloads."""
    for path in [
        "/api/insights/calendar",
        "/api/insights/treemap",
        "/api/insights/time-heatmap",
        "/api/trends/day-of-month",
    ]:
        resp = await api_client.get(path)
        assert resp.status_code == 200
        assert resp.json() == []
    resp = await api_client.get("/api/insights/sankey")
    assert resp.status_code == 200
    assert resp.json() == {"nodes": [], "links": []}
    resp = await api_client.get("/api/trends/category-by-month")
    assert resp.status_code == 200
    assert resp.json() == {"months": [], "series": []}
    resp = await api_client.get("/api/forecast")
    assert resp.status_code == 200
    body = resp.json()
    assert Decimal(body["current_net_worth"]) == Decimal("0")
    assert body["target"] is None
