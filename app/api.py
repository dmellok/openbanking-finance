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
