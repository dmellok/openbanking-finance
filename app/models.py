from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, Index, Numeric, UniqueConstraint
from sqlmodel import Field, SQLModel


class Direction(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"


class TradeType(StrEnum):
    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    SPLIT = "split"
    TRANSFER = "transfer"
    FEE = "fee"
    INTEREST = "interest"
    OTHER = "other"


class ConnectionCategory(StrEnum):
    BANKING = "banking"
    BROKERAGE = "brokerage"


class ConnectionStatus(StrEnum):
    ACTIVE = "active"
    INVALIDATED = "invalidated"
    PENDING_MFA = "pending_mfa"
    REVOKED = "revoked"


class AccountType(StrEnum):
    TRANSACTION = "transaction"
    SAVINGS = "savings"
    CREDIT_CARD = "credit-card"
    LOAN = "loan"
    INVESTMENT = "investment"
    TERM_DEPOSIT = "term-deposit"
    OTHER = "other"


# Money columns: SQLite stores Numeric as TEXT but SQLAlchemy round-trips
# values as Decimal, which is what we need.
_MONEY = Numeric(20, 6)
_QTY = Numeric(28, 10)


class Connection(SQLModel, table=True):
    __tablename__ = "connections"

    id: str = Field(primary_key=True)
    provider: str
    category: str
    institution_id: str | None = None
    institution_name: str
    institution_logo: str | None = None
    status: str
    last_refreshed_at: datetime | None = None
    created_at: datetime | None = None
    raw_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))


class Account(SQLModel, table=True):
    __tablename__ = "accounts"

    id: str = Field(primary_key=True)
    connection_id: str = Field(foreign_key="connections.id", index=True)
    provider: str | None = None
    name: str
    masked_number: str | None = None
    type: str
    institution_name: str | None = None
    currency: str
    last_polled_transactions_at: datetime | None = None
    last_polled_trades_at: datetime | None = None
    raw_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))


class Transaction(SQLModel, table=True):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_tx_account_local_date", "account_id", "local_date"),
        Index("ix_tx_category_local_date", "category", "local_date"),
    )

    id: str = Field(primary_key=True)
    account_id: str = Field(foreign_key="accounts.id", index=True)
    status: str | None = None
    posted_at: datetime | None = None
    local_date: date
    amount: Decimal = Field(sa_column=Column(_MONEY, nullable=False))
    currency: str
    direction: str
    description: str | None = None
    merchant_name: str | None = None
    category: str | None = None
    mcc: str | None = None
    raw_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    received_at: datetime = Field(default_factory=lambda: datetime.now())


class Trade(SQLModel, table=True):
    __tablename__ = "trades"
    __table_args__ = (Index("ix_trade_account_trade_date", "account_id", "trade_date"),)

    id: str = Field(primary_key=True)
    account_id: str = Field(foreign_key="accounts.id", index=True)
    symbol: str | None = None
    name: str | None = None
    type: str
    quantity: Decimal = Field(sa_column=Column(_QTY, nullable=False))
    price: Decimal | None = Field(default=None, sa_column=Column(_MONEY))
    currency: str | None = None
    total_amount: Decimal | None = Field(default=None, sa_column=Column(_MONEY))
    fees: Decimal | None = Field(default=None, sa_column=Column(_MONEY))
    trade_date: date
    settlement_date: date | None = None
    description: str | None = None
    raw_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    received_at: datetime = Field(default_factory=lambda: datetime.now())


class BalanceSnapshot(SQLModel, table=True):
    __tablename__ = "balance_snapshots"
    __table_args__ = (Index("ix_balance_account_taken", "account_id", "taken_at"),)

    id: int | None = Field(default=None, primary_key=True)
    account_id: str = Field(foreign_key="accounts.id", index=True)
    current_balance: Decimal | None = Field(default=None, sa_column=Column(_MONEY))
    available_balance: Decimal | None = Field(default=None, sa_column=Column(_MONEY))
    currency: str | None = None
    taken_at: datetime = Field(default_factory=lambda: datetime.now())


class SyncRun(SQLModel, table=True):
    __tablename__ = "sync_runs"

    id: int | None = Field(default=None, primary_key=True)
    kind: str  # "poll" | "backfill"
    started_at: datetime = Field(default_factory=lambda: datetime.now())
    finished_at: datetime | None = None
    status: str = "running"  # "running" | "ok" | "error"
    detail: str | None = None
    counts: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))


class Budget(SQLModel, table=True):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("category", "currency", name="uq_budget_category_currency"),)

    id: int | None = Field(default=None, primary_key=True)
    category: str
    monthly_limit: Decimal = Field(sa_column=Column(_MONEY, nullable=False))
    currency: str
