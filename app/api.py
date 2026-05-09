"""FastAPI app: JSON dashboard API + static SPA.

The same process serves both. APScheduler runs the 15-minute Redbark poll in the
FastAPI event loop (started/stopped in the lifespan).

Decimals are serialised as strings (matching Redbark's wire format) using a
PlainSerializer-annotated type so the front-end can use `BigInt`/string parsing
without the JSON.parse precision footgun.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, PlainSerializer
from sqlalchemy import case, desc, func
from sqlmodel import Session, col, select

from app.config import get_settings
from app.db import get_engine, init_db
from app.models import (
    Account,
    BalanceSnapshot,
    Budget,
    Connection,
    SyncRun,
    Transaction,
)
from app.poller import start_scheduler, stop_scheduler

logger = logging.getLogger(__name__)

def _format_decimal(d: Decimal) -> str:
    """Stringify a Decimal as the user wrote it: strip trailing zeros from the
    Numeric column round-trip, but keep at least cents precision so JSON looks
    like money. `Decimal("800.000000")` → `"800.00"`; `Decimal("412.8")` → `"412.80"`;
    `Decimal("0.0001")` → `"0.0001"` (preserves higher precision)."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if "." not in s:
        s += ".00"
    elif len(s.split(".", 1)[1]) == 1:
        s += "0"
    return s


# Serialise Decimal as string in every response.
DecimalStr = Annotated[Decimal, PlainSerializer(_format_decimal, return_type=str)]
OptDecimalStr = Annotated[
    Decimal | None,
    PlainSerializer(
        lambda v: None if v is None else _format_decimal(v),
        return_type=str | None,
    ),
]

STATIC_DIR = Path(__file__).parent / "static"

# ── Lifespan ──────────────────────────────────────────────────────────────────


_LIFESPAN_START_SCHEDULER = True


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    init_db()
    if _LIFESPAN_START_SCHEDULER:
        start_scheduler()
    try:
        yield
    finally:
        if _LIFESPAN_START_SCHEDULER:
            stop_scheduler()


def create_app(*, with_scheduler: bool = True) -> FastAPI:
    global _LIFESPAN_START_SCHEDULER
    _LIFESPAN_START_SCHEDULER = with_scheduler
    fastapi_app = FastAPI(
        title="pyFinance v2",
        version="0.1.0",
        lifespan=_lifespan,
    )
    _register_routes(fastapi_app)
    return fastapi_app


# ── DI ────────────────────────────────────────────────────────────────────────


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


# ── Response models ───────────────────────────────────────────────────────────


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    connection_id: str
    name: str
    masked_number: str | None
    type: str
    institution_name: str | None
    currency: str
    latest_balance: OptDecimalStr = None
    latest_balance_at: datetime | None = None


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    account_id: str
    posted_at: datetime | None
    local_date: date
    amount: DecimalStr
    currency: str
    direction: str
    description: str | None
    merchant_name: str | None
    category: str | None
    mcc: str | None


class TransactionsPage(BaseModel):
    data: list[TransactionOut]
    pagination: dict[str, int | bool]


class CategoryTotal(BaseModel):
    category: str
    total: DecimalStr


class MerchantTotal(BaseModel):
    merchant_name: str
    total: DecimalStr
    count: int


class NetWorthPoint(BaseModel):
    taken_at: datetime
    total: DecimalStr


class NetWorthPerAccountPoint(BaseModel):
    taken_at: datetime
    account_id: str
    balance: OptDecimalStr


class CashflowMonth(BaseModel):
    month: str  # YYYY-MM
    income: DecimalStr
    expenses: DecimalStr  # positive number = total spent
    net: DecimalStr
    savings_rate: float


class BudgetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    monthly_limit: DecimalStr
    currency: str


class BudgetIn(BaseModel):
    category: str
    monthly_limit: Decimal
    currency: str = "AUD"


class CalendarPoint(BaseModel):
    date: date
    total: DecimalStr


class TreemapNode(BaseModel):
    name: str
    value: DecimalStr
    children: list[TreemapNode] = []


class SankeyNode(BaseModel):
    name: str


class SankeyLink(BaseModel):
    source: str
    target: str
    value: DecimalStr


class SankeyGraph(BaseModel):
    nodes: list[SankeyNode]
    links: list[SankeyLink]


class TimeHeatPoint(BaseModel):
    day_of_week: int  # 0 = Sunday .. 6 = Saturday (SQLite strftime('%w'))
    hour: int  # 0..23
    total: DecimalStr
    count: int


class ForecastPoint(BaseModel):
    month: str  # YYYY-MM
    balance: DecimalStr
    projected: bool


class ForecastTarget(BaseModel):
    balance: DecimalStr
    months_to_target: float | None
    date_at_target: date | None


class ForecastOut(BaseModel):
    current_net_worth: DecimalStr
    current_net_worth_at: datetime | None
    rolling_window_days: int
    rolling_income: DecimalStr
    rolling_expenses: DecimalStr
    rolling_net: DecimalStr
    monthly_income: DecimalStr
    monthly_expenses: DecimalStr
    monthly_net: DecimalStr
    history: list[ForecastPoint]
    projection: list[ForecastPoint]
    target: ForecastTarget | None


class RollingSpendPoint(BaseModel):
    date: date
    rolling_total: DecimalStr


class CategoryByMonth(BaseModel):
    months: list[str]
    series: list[dict[str, object]]  # [{name: "FOOD", data: [12.0, 34.0, ...]}]


class DayOfMonthPoint(BaseModel):
    day: int  # 1..31
    total: DecimalStr
    months_seen: int
    avg: DecimalStr


class SyncRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    detail: str | None
    counts: dict[str, int] | None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _date_range(
    from_: date | None, to: date | None, *, default_days: int = 30
) -> tuple[date, date]:
    today = datetime.now(UTC).date()
    end = to or today
    start = from_ or (end - timedelta(days=default_days))
    if start > end:
        raise HTTPException(status_code=400, detail="from must be on or before to")
    return start, end


# ── Routes ────────────────────────────────────────────────────────────────────


def _register_routes(app: FastAPI) -> None:
    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"ok": True, "service": "pyfinance-v2"}

    @app.get("/api/accounts", response_model=list[AccountOut])
    def list_accounts(session: SessionDep) -> list[AccountOut]:
        # Latest balance per account: subquery for max(taken_at) per account_id.
        latest = (
            select(
                BalanceSnapshot.account_id,
                func.max(BalanceSnapshot.taken_at).label("latest_at"),
            )
            .group_by(BalanceSnapshot.account_id)
            .subquery()
        )
        rows = session.exec(
            select(Account, BalanceSnapshot)
            .join(
                latest,
                col(Account.id) == latest.c.account_id,
                isouter=True,
            )
            .join(
                BalanceSnapshot,
                (col(BalanceSnapshot.account_id) == latest.c.account_id)
                & (col(BalanceSnapshot.taken_at) == latest.c.latest_at),
                isouter=True,
            )
        ).all()
        out: list[AccountOut] = []
        for account, snap in rows:
            payload = AccountOut.model_validate(account)
            if snap is not None:
                payload.latest_balance = snap.current_balance
                payload.latest_balance_at = snap.taken_at
            out.append(payload)
        out.sort(key=lambda a: (a.institution_name or "", a.name))
        return out

    @app.get("/api/transactions", response_model=TransactionsPage)
    def list_transactions(
        session: SessionDep,
        from_: Annotated[date | None, Query(alias="from")] = None,
        to: date | None = None,
        account_id: str | None = None,
        category: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> TransactionsPage:
        start, end = _date_range(from_, to, default_days=30)
        base = select(Transaction).where(
            col(Transaction.local_date) >= start,
            col(Transaction.local_date) <= end,
        )
        if account_id:
            base = base.where(Transaction.account_id == account_id)
        if category:
            base = base.where(Transaction.category == category)

        total = session.exec(select(func.count()).select_from(base.subquery())).one()
        rows = session.exec(
            base.order_by(
                desc(col(Transaction.local_date)),
                desc(col(Transaction.posted_at)),
            )
            .offset(offset)
            .limit(limit)
        ).all()
        return TransactionsPage(
            data=[TransactionOut.model_validate(r) for r in rows],
            pagination={
                "total": int(total),
                "limit": limit,
                "offset": offset,
                "hasMore": offset + len(rows) < int(total),
            },
        )

    @app.get("/api/spending/by-category", response_model=list[CategoryTotal])
    def spending_by_category(
        session: SessionDep,
        from_: Annotated[date | None, Query(alias="from")] = None,
        to: date | None = None,
        account_ids: Annotated[list[str] | None, Query()] = None,
    ) -> list[CategoryTotal]:
        start, end = _date_range(from_, to)
        # Spending = absolute value of debit transactions.
        query = (
            select(
                func.coalesce(col(Transaction.category), "UNCATEGORISED").label("category"),
                func.sum(func.abs(col(Transaction.amount))).label("total"),
            )
            .where(
                col(Transaction.local_date) >= start,
                col(Transaction.local_date) <= end,
                col(Transaction.direction) == "debit",
            )
            .group_by(col(Transaction.category))
            .order_by(desc("total"))
        )
        if account_ids:
            query = query.where(col(Transaction.account_id).in_(account_ids))
        rows = session.exec(query).all()
        return [CategoryTotal(category=r[0], total=Decimal(str(r[1] or 0))) for r in rows]

    @app.get("/api/spending/top-merchants", response_model=list[MerchantTotal])
    def top_merchants(
        session: SessionDep,
        from_: Annotated[date | None, Query(alias="from")] = None,
        to: date | None = None,
        account_ids: Annotated[list[str] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
    ) -> list[MerchantTotal]:
        start, end = _date_range(from_, to)
        query = (
            select(
                func.coalesce(col(Transaction.merchant_name), "Unknown").label("merchant_name"),
                func.sum(func.abs(col(Transaction.amount))).label("total"),
                func.count().label("count"),
            )
            .where(
                col(Transaction.local_date) >= start,
                col(Transaction.local_date) <= end,
                col(Transaction.direction) == "debit",
                col(Transaction.merchant_name).is_not(None),
            )
            .group_by(col(Transaction.merchant_name))
            .order_by(desc("total"))
            .limit(limit)
        )
        if account_ids:
            query = query.where(col(Transaction.account_id).in_(account_ids))
        rows = session.exec(query).all()
        return [
            MerchantTotal(
                merchant_name=r[0], total=Decimal(str(r[1] or 0)), count=int(r[2])
            )
            for r in rows
        ]

    @app.get("/api/net-worth/series", response_model=list[NetWorthPoint])
    def net_worth_series(
        session: SessionDep,
        from_: Annotated[date | None, Query(alias="from")] = None,
        to: date | None = None,
        account_ids: Annotated[list[str] | None, Query()] = None,
    ) -> list[NetWorthPoint]:
        start, end = _date_range(from_, to, default_days=90)
        # Naive cross-currency sum: this dashboard is single-user; FX conversion
        # would need an external feed. Sum everything in raw units.
        query = (
            select(
                col(BalanceSnapshot.taken_at),
                func.sum(col(BalanceSnapshot.current_balance)).label("total"),
            )
            .where(
                func.date(col(BalanceSnapshot.taken_at)) >= start,
                func.date(col(BalanceSnapshot.taken_at)) <= end,
            )
            .group_by(col(BalanceSnapshot.taken_at))
            .order_by(col(BalanceSnapshot.taken_at))
        )
        if account_ids:
            query = query.where(col(BalanceSnapshot.account_id).in_(account_ids))
        rows = session.exec(query).all()
        return [NetWorthPoint(taken_at=r[0], total=Decimal(str(r[1] or 0))) for r in rows]

    @app.get("/api/net-worth/per-account", response_model=list[NetWorthPerAccountPoint])
    def net_worth_per_account(
        session: SessionDep,
        from_: Annotated[date | None, Query(alias="from")] = None,
        to: date | None = None,
        account_ids: Annotated[list[str] | None, Query()] = None,
    ) -> list[NetWorthPerAccountPoint]:
        start, end = _date_range(from_, to, default_days=90)
        query = (
            select(
                col(BalanceSnapshot.taken_at),
                col(BalanceSnapshot.account_id),
                col(BalanceSnapshot.current_balance),
            )
            .where(
                func.date(col(BalanceSnapshot.taken_at)) >= start,
                func.date(col(BalanceSnapshot.taken_at)) <= end,
            )
            .order_by(col(BalanceSnapshot.taken_at))
        )
        if account_ids:
            query = query.where(col(BalanceSnapshot.account_id).in_(account_ids))
        rows = session.exec(query).all()
        return [
            NetWorthPerAccountPoint(taken_at=r[0], account_id=r[1], balance=r[2])
            for r in rows
        ]

    @app.get("/api/cashflow/monthly", response_model=list[CashflowMonth])
    def cashflow_monthly(
        session: SessionDep,
        from_: Annotated[date | None, Query(alias="from")] = None,
        to: date | None = None,
        account_ids: Annotated[list[str] | None, Query()] = None,
    ) -> list[CashflowMonth]:
        start, end = _date_range(from_, to, default_days=365)
        # SQLite-friendly month bucketing; respects local_date column.
        month_expr = func.strftime("%Y-%m", col(Transaction.local_date))
        income_expr = func.sum(
            case(
                (col(Transaction.direction) == "credit", col(Transaction.amount)),
                else_=0,
            )
        )
        expenses_expr = func.sum(
            case(
                (
                    col(Transaction.direction) == "debit",
                    func.abs(col(Transaction.amount)),
                ),
                else_=0,
            )
        )
        # Exclude TRANSFER_* categories so we don't double-count internal moves.
        query = (
            select(
                month_expr.label("month"),
                income_expr.label("income"),
                expenses_expr.label("expenses"),
            )
            .where(
                col(Transaction.local_date) >= start,
                col(Transaction.local_date) <= end,
                col(Transaction.category).not_in(["TRANSFER_IN", "TRANSFER_OUT"]),
            )
            .group_by("month")
            .order_by("month")
        )
        if account_ids:
            query = query.where(col(Transaction.account_id).in_(account_ids))
        rows = session.exec(query).all()
        out: list[CashflowMonth] = []
        for r in rows:
            income = Decimal(str(r[1] or 0))
            expenses = Decimal(str(r[2] or 0))
            net = income - expenses
            savings_rate = float(net / income) if income > 0 else 0.0
            out.append(
                CashflowMonth(
                    month=str(r[0]),
                    income=income,
                    expenses=expenses,
                    net=net,
                    savings_rate=savings_rate,
                )
            )
        return out

    @app.get("/api/budgets", response_model=list[BudgetOut])
    def list_budgets(session: SessionDep) -> list[BudgetOut]:
        rows = session.exec(select(Budget).order_by(Budget.category)).all()
        return [BudgetOut.model_validate(r) for r in rows]

    @app.post("/api/budgets", response_model=BudgetOut)
    def upsert_budget(payload: BudgetIn, session: SessionDep) -> BudgetOut:
        currency = payload.currency.strip().upper() or "AUD"
        existing = session.exec(
            select(Budget).where(
                Budget.category == payload.category,
                Budget.currency == currency,
            )
        ).first()
        if existing is None:
            row = Budget(
                category=payload.category,
                monthly_limit=payload.monthly_limit,
                currency=currency,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return BudgetOut.model_validate(row)
        existing.monthly_limit = payload.monthly_limit
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return BudgetOut.model_validate(existing)

    # ── Insights ──────────────────────────────────────────────────────────────

    @app.get("/api/insights/calendar", response_model=list[CalendarPoint])
    def insights_calendar(
        session: SessionDep,
        from_: Annotated[date | None, Query(alias="from")] = None,
        to: date | None = None,
        account_ids: Annotated[list[str] | None, Query()] = None,
    ) -> list[CalendarPoint]:
        """Daily spending totals for a GitHub-style heatmap. Excludes transfers."""
        start, end = _date_range(from_, to, default_days=365)
        query = (
            select(
                col(Transaction.local_date),
                func.sum(func.abs(col(Transaction.amount))).label("total"),
            )
            .where(
                col(Transaction.local_date) >= start,
                col(Transaction.local_date) <= end,
                col(Transaction.direction) == "debit",
                col(Transaction.category).not_in(["TRANSFER_IN", "TRANSFER_OUT"]),
            )
            .group_by(col(Transaction.local_date))
            .order_by(col(Transaction.local_date))
        )
        if account_ids:
            query = query.where(col(Transaction.account_id).in_(account_ids))
        rows = session.exec(query).all()
        return [CalendarPoint(date=r[0], total=Decimal(str(r[1] or 0))) for r in rows]

    @app.get("/api/insights/treemap", response_model=list[TreemapNode])
    def insights_treemap(
        session: SessionDep,
        from_: Annotated[date | None, Query(alias="from")] = None,
        to: date | None = None,
        account_ids: Annotated[list[str] | None, Query()] = None,
    ) -> list[TreemapNode]:
        """Category → merchant treemap rows (debits only, transfers excluded)."""
        start, end = _date_range(from_, to)
        query = (
            select(
                func.coalesce(col(Transaction.category), "UNCATEGORISED").label("category"),
                func.coalesce(col(Transaction.merchant_name), "Unknown").label("merchant"),
                func.sum(func.abs(col(Transaction.amount))).label("total"),
            )
            .where(
                col(Transaction.local_date) >= start,
                col(Transaction.local_date) <= end,
                col(Transaction.direction) == "debit",
                col(Transaction.category).not_in(["TRANSFER_IN", "TRANSFER_OUT"]),
            )
            .group_by(col(Transaction.category), col(Transaction.merchant_name))
            .order_by(desc("total"))
        )
        if account_ids:
            query = query.where(col(Transaction.account_id).in_(account_ids))
        rows = session.exec(query).all()
        # Roll up into hierarchy.
        by_cat: dict[str, list[TreemapNode]] = {}
        cat_totals: dict[str, Decimal] = {}
        for category, merchant, total in rows:
            amount = Decimal(str(total or 0))
            by_cat.setdefault(category, []).append(TreemapNode(name=merchant, value=amount))
            cat_totals[category] = cat_totals.get(category, Decimal(0)) + amount
        return [
            TreemapNode(name=cat, value=cat_totals[cat], children=by_cat[cat])
            for cat in sorted(cat_totals, key=cat_totals.get, reverse=True)  # type: ignore[arg-type]
        ]

    @app.get("/api/insights/sankey", response_model=SankeyGraph)
    def insights_sankey(
        session: SessionDep,
        from_: Annotated[date | None, Query(alias="from")] = None,
        to: date | None = None,
        account_ids: Annotated[list[str] | None, Query()] = None,
    ) -> SankeyGraph:
        """Income → categories → 'Savings' Sankey (transfers excluded).

        Income node aggregates all credit transactions; each spending category
        is a sink; any leftover net flows to 'Savings'.
        """
        start, end = _date_range(from_, to, default_days=90)
        # Income.
        income_q = (
            select(func.sum(col(Transaction.amount)))
            .where(
                col(Transaction.local_date) >= start,
                col(Transaction.local_date) <= end,
                col(Transaction.direction) == "credit",
                col(Transaction.category).not_in(["TRANSFER_IN", "TRANSFER_OUT"]),
            )
        )
        if account_ids:
            income_q = income_q.where(col(Transaction.account_id).in_(account_ids))
        income_total = Decimal(str(session.exec(income_q).one() or 0))

        # Spending by category.
        spend_q = (
            select(
                func.coalesce(col(Transaction.category), "UNCATEGORISED").label("category"),
                func.sum(func.abs(col(Transaction.amount))).label("total"),
            )
            .where(
                col(Transaction.local_date) >= start,
                col(Transaction.local_date) <= end,
                col(Transaction.direction) == "debit",
                col(Transaction.category).not_in(["TRANSFER_IN", "TRANSFER_OUT"]),
            )
            .group_by(col(Transaction.category))
            .order_by(desc("total"))
        )
        if account_ids:
            spend_q = spend_q.where(col(Transaction.account_id).in_(account_ids))
        spend_rows = session.exec(spend_q).all()

        nodes: list[SankeyNode] = []
        links: list[SankeyLink] = []
        if income_total <= 0 and not spend_rows:
            return SankeyGraph(nodes=nodes, links=links)

        nodes.append(SankeyNode(name="Income"))
        spent_total = Decimal(0)
        for category, total in spend_rows:
            amount = Decimal(str(total or 0))
            if amount <= 0:
                continue
            nodes.append(SankeyNode(name=category))
            links.append(SankeyLink(source="Income", target=category, value=amount))
            spent_total += amount
        savings = income_total - spent_total
        if savings > 0:
            nodes.append(SankeyNode(name="Savings"))
            links.append(SankeyLink(source="Income", target="Savings", value=savings))
        return SankeyGraph(nodes=nodes, links=links)

    @app.get("/api/insights/time-heatmap", response_model=list[TimeHeatPoint])
    def insights_time_heatmap(
        session: SessionDep,
        from_: Annotated[date | None, Query(alias="from")] = None,
        to: date | None = None,
        account_ids: Annotated[list[str] | None, Query()] = None,
    ) -> list[TimeHeatPoint]:
        """Day-of-week x hour-of-day spending grid.
        Uses `posted_at` (a UTC datetime) — rows without one are skipped."""
        start, end = _date_range(from_, to, default_days=180)
        dow = func.strftime("%w", col(Transaction.posted_at))
        hour = func.strftime("%H", col(Transaction.posted_at))
        query = (
            select(
                dow.label("dow"),
                hour.label("hour"),
                func.sum(func.abs(col(Transaction.amount))).label("total"),
                func.count().label("count"),
            )
            .where(
                col(Transaction.local_date) >= start,
                col(Transaction.local_date) <= end,
                col(Transaction.direction) == "debit",
                col(Transaction.posted_at).is_not(None),
                col(Transaction.category).not_in(["TRANSFER_IN", "TRANSFER_OUT"]),
            )
            .group_by("dow", "hour")
        )
        if account_ids:
            query = query.where(col(Transaction.account_id).in_(account_ids))
        rows = session.exec(query).all()
        out: list[TimeHeatPoint] = []
        for dow_val, hour_val, total, count in rows:
            if dow_val is None or hour_val is None:
                continue
            out.append(
                TimeHeatPoint(
                    day_of_week=int(dow_val),
                    hour=int(hour_val),
                    total=Decimal(str(total or 0)),
                    count=int(count),
                )
            )
        return out

    # ── Trends ────────────────────────────────────────────────────────────────

    @app.get("/api/trends/rolling-spend", response_model=list[RollingSpendPoint])
    def trends_rolling_spend(
        session: SessionDep,
        from_: Annotated[date | None, Query(alias="from")] = None,
        to: date | None = None,
        account_ids: Annotated[list[str] | None, Query()] = None,
        window: Annotated[int, Query(ge=2, le=180)] = 30,
    ) -> list[RollingSpendPoint]:
        """Rolling-window total of daily spending. Excludes transfers."""
        start, end = _date_range(from_, to, default_days=180)
        query = (
            select(
                col(Transaction.local_date),
                func.sum(func.abs(col(Transaction.amount))).label("total"),
            )
            .where(
                col(Transaction.local_date) >= start - timedelta(days=window),
                col(Transaction.local_date) <= end,
                col(Transaction.direction) == "debit",
                col(Transaction.category).not_in(["TRANSFER_IN", "TRANSFER_OUT"]),
            )
            .group_by(col(Transaction.local_date))
            .order_by(col(Transaction.local_date))
        )
        if account_ids:
            query = query.where(col(Transaction.account_id).in_(account_ids))
        rows = session.exec(query).all()
        daily = {r[0]: Decimal(str(r[1] or 0)) for r in rows}

        out: list[RollingSpendPoint] = []
        cursor = start
        while cursor <= end:
            total = Decimal(0)
            for offset in range(window):
                d = cursor - timedelta(days=offset)
                total += daily.get(d, Decimal(0))
            out.append(RollingSpendPoint(date=cursor, rolling_total=total))
            cursor += timedelta(days=1)
        return out

    @app.get("/api/trends/category-by-month", response_model=CategoryByMonth)
    def trends_category_by_month(
        session: SessionDep,
        from_: Annotated[date | None, Query(alias="from")] = None,
        to: date | None = None,
        account_ids: Annotated[list[str] | None, Query()] = None,
    ) -> CategoryByMonth:
        """Stacked-bar source: total spend per (month, category). Transfers excluded."""
        start, end = _date_range(from_, to, default_days=365)
        month_expr = func.strftime("%Y-%m", col(Transaction.local_date))
        query = (
            select(
                month_expr.label("month"),
                func.coalesce(col(Transaction.category), "UNCATEGORISED").label("category"),
                func.sum(func.abs(col(Transaction.amount))).label("total"),
            )
            .where(
                col(Transaction.local_date) >= start,
                col(Transaction.local_date) <= end,
                col(Transaction.direction) == "debit",
                col(Transaction.category).not_in(["TRANSFER_IN", "TRANSFER_OUT"]),
            )
            .group_by("month", col(Transaction.category))
            .order_by("month", desc("total"))
        )
        if account_ids:
            query = query.where(col(Transaction.account_id).in_(account_ids))
        rows = session.exec(query).all()
        if not rows:
            return CategoryByMonth(months=[], series=[])
        months: list[str] = []
        seen_months: set[str] = set()
        cat_totals: dict[str, Decimal] = {}
        cell: dict[tuple[str, str], Decimal] = {}
        for month, category, total in rows:
            month = str(month)
            if month not in seen_months:
                months.append(month)
                seen_months.add(month)
            amount = Decimal(str(total or 0))
            cell[(month, category)] = amount
            cat_totals[category] = cat_totals.get(category, Decimal(0)) + amount
        # Order categories by overall size so the stacked bar is most readable.
        categories = sorted(cat_totals, key=cat_totals.get, reverse=True)  # type: ignore[arg-type]
        series = [
            {
                "name": cat,
                "data": [_format_decimal(cell.get((m, cat), Decimal(0))) for m in months],
            }
            for cat in categories
        ]
        return CategoryByMonth(months=months, series=series)

    @app.get("/api/trends/day-of-month", response_model=list[DayOfMonthPoint])
    def trends_day_of_month(
        session: SessionDep,
        from_: Annotated[date | None, Query(alias="from")] = None,
        to: date | None = None,
        account_ids: Annotated[list[str] | None, Query()] = None,
    ) -> list[DayOfMonthPoint]:
        """Average daily spend by day-of-month (1..31). Transfers excluded.

        Useful for spotting payday spikes, end-of-month creep, or rent-day patterns.
        """
        start, end = _date_range(from_, to, default_days=365)
        day_expr = func.strftime("%d", col(Transaction.local_date))
        month_expr = func.strftime("%Y-%m", col(Transaction.local_date))
        query = (
            select(
                day_expr.label("day"),
                func.sum(func.abs(col(Transaction.amount))).label("total"),
                func.count(func.distinct(month_expr)).label("months_seen"),
            )
            .where(
                col(Transaction.local_date) >= start,
                col(Transaction.local_date) <= end,
                col(Transaction.direction) == "debit",
                col(Transaction.category).not_in(["TRANSFER_IN", "TRANSFER_OUT"]),
            )
            .group_by("day")
            .order_by("day")
        )
        if account_ids:
            query = query.where(col(Transaction.account_id).in_(account_ids))
        rows = session.exec(query).all()
        out: list[DayOfMonthPoint] = []
        for day_str, total, months_seen in rows:
            if day_str is None:
                continue
            total_dec = Decimal(str(total or 0))
            months = int(months_seen or 1)
            out.append(
                DayOfMonthPoint(
                    day=int(day_str),
                    total=total_dec,
                    months_seen=months,
                    avg=total_dec / months if months > 0 else total_dec,
                )
            )
        return out

    # ── Forecast ──────────────────────────────────────────────────────────────

    @app.get("/api/forecast", response_model=ForecastOut)
    def forecast(
        session: SessionDep,
        target_balance: Decimal | None = None,
        rolling_days: Annotated[int, Query(ge=14, le=365)] = 90,
        horizon_months: Annotated[int, Query(ge=1, le=120)] = 24,
        account_ids: Annotated[list[str] | None, Query()] = None,
    ) -> ForecastOut:
        """Net-worth forecast based on rolling-window average net cashflow."""
        today = datetime.now(UTC).date()
        rolling_start = today - timedelta(days=rolling_days)

        # Latest balance snapshot per account → sum.
        latest_subq = (
            select(
                col(BalanceSnapshot.account_id),
                func.max(col(BalanceSnapshot.taken_at)).label("latest_at"),
            )
            .group_by(col(BalanceSnapshot.account_id))
            .subquery()
        )
        latest_q = (
            select(
                func.sum(col(BalanceSnapshot.current_balance)),
                func.max(col(BalanceSnapshot.taken_at)),
            )
            .join(
                latest_subq,
                (col(BalanceSnapshot.account_id) == latest_subq.c.account_id)
                & (col(BalanceSnapshot.taken_at) == latest_subq.c.latest_at),
            )
        )
        if account_ids:
            latest_q = latest_q.where(col(BalanceSnapshot.account_id).in_(account_ids))
        current_total_raw, current_at = session.exec(latest_q).one()
        current_net_worth = Decimal(str(current_total_raw or 0))

        # Rolling window cashflow (transfers excluded).
        income_q = select(func.sum(col(Transaction.amount))).where(
            col(Transaction.local_date) >= rolling_start,
            col(Transaction.local_date) <= today,
            col(Transaction.direction) == "credit",
            col(Transaction.category).not_in(["TRANSFER_IN", "TRANSFER_OUT"]),
        )
        expense_q = select(func.sum(func.abs(col(Transaction.amount)))).where(
            col(Transaction.local_date) >= rolling_start,
            col(Transaction.local_date) <= today,
            col(Transaction.direction) == "debit",
            col(Transaction.category).not_in(["TRANSFER_IN", "TRANSFER_OUT"]),
        )
        if account_ids:
            income_q = income_q.where(col(Transaction.account_id).in_(account_ids))
            expense_q = expense_q.where(col(Transaction.account_id).in_(account_ids))
        income_total = Decimal(str(session.exec(income_q).one() or 0))
        expense_total = Decimal(str(session.exec(expense_q).one() or 0))
        net_total = income_total - expense_total

        months_in_window = Decimal(str(rolling_days)) / Decimal("30.4375")
        if months_in_window > 0:
            monthly_income = income_total / months_in_window
            monthly_expenses = expense_total / months_in_window
            monthly_net = net_total / months_in_window
        else:
            monthly_income = monthly_expenses = monthly_net = Decimal(0)

        # Historical month-end net worth (last balance snapshot per month).
        month_expr = func.strftime("%Y-%m", col(BalanceSnapshot.taken_at))
        hist_subq = (
            select(
                col(BalanceSnapshot.account_id).label("acc"),
                month_expr.label("ym"),
                func.max(col(BalanceSnapshot.taken_at)).label("month_end"),
            )
            .group_by(col(BalanceSnapshot.account_id), "ym")
            .subquery()
        )
        hist_q = (
            select(hist_subq.c.ym, func.sum(col(BalanceSnapshot.current_balance)))
            .join(
                hist_subq,
                (col(BalanceSnapshot.account_id) == hist_subq.c.acc)
                & (col(BalanceSnapshot.taken_at) == hist_subq.c.month_end),
            )
            .group_by(hist_subq.c.ym)
            .order_by(hist_subq.c.ym)
        )
        if account_ids:
            hist_q = hist_q.where(col(BalanceSnapshot.account_id).in_(account_ids))
        hist_rows = session.exec(hist_q).all()
        history = [
            ForecastPoint(month=str(r[0]), balance=Decimal(str(r[1] or 0)), projected=False)
            for r in hist_rows
        ]

        # Projection: extrapolate from current point at monthly_net for horizon_months.
        projection: list[ForecastPoint] = []
        anchor_balance = current_net_worth
        anchor_year, anchor_month = today.year, today.month
        for i in range(1, horizon_months + 1):
            m = anchor_month + i
            y = anchor_year + (m - 1) // 12
            m = ((m - 1) % 12) + 1
            projection.append(
                ForecastPoint(
                    month=f"{y:04d}-{m:02d}",
                    balance=anchor_balance + monthly_net * i,
                    projected=True,
                )
            )

        # Target / FI calculator.
        target: ForecastTarget | None = None
        if target_balance is not None:
            months_to: float | None = None
            date_at: date | None = None
            delta = Decimal(str(target_balance)) - current_net_worth
            if delta <= 0:
                months_to = 0.0
                date_at = today
            elif monthly_net > 0:
                months_to = float(delta / monthly_net)
                m = today.month + int(months_to)
                y = today.year + (m - 1) // 12
                m = ((m - 1) % 12) + 1
                date_at = date(y, m, min(today.day, 28))
            target = ForecastTarget(
                balance=Decimal(str(target_balance)),
                months_to_target=months_to,
                date_at_target=date_at,
            )

        return ForecastOut(
            current_net_worth=current_net_worth,
            current_net_worth_at=current_at,
            rolling_window_days=rolling_days,
            rolling_income=income_total,
            rolling_expenses=expense_total,
            rolling_net=net_total,
            monthly_income=monthly_income,
            monthly_expenses=monthly_expenses,
            monthly_net=monthly_net,
            history=history,
            projection=projection,
            target=target,
        )

    @app.get("/api/sync-runs", response_model=list[SyncRunOut])
    def list_sync_runs(
        session: SessionDep,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
    ) -> list[SyncRunOut]:
        rows = session.exec(
            select(SyncRun).order_by(desc(col(SyncRun.started_at))).limit(limit)
        ).all()
        return [SyncRunOut.model_validate(r) for r in rows]

    # Static dashboard.
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def root() -> FileResponse:
        index = STATIC_DIR / "index.html"
        if not index.exists():
            raise HTTPException(404, "dashboard not built")
        return FileResponse(index)


# Default app for `uvicorn app.api:app`.
_ = Connection  # ensure model is imported for metadata
app = create_app(with_scheduler=True)
