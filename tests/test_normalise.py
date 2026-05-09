from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.normalise import (
    account_from_rest,
    balance_snapshot_from_rest,
    connection_from_rest,
    trade_from_rest,
    transaction_from_rest,
)

from tests.fixtures import (
    ACCOUNTS_RESPONSE,
    BALANCES_RESPONSE,
    CONNECTIONS_RESPONSE,
    TRADES_RESPONSE,
    TRANSACTIONS_RESPONSE,
)


def test_connection_from_rest_parses_iso_datetimes() -> None:
    raw = CONNECTIONS_RESPONSE["data"][0]  # type: ignore[index]
    conn = connection_from_rest(raw)
    assert conn.id == "e8f1a2b3-7c4d-5e6f-8a9b-0c1d2e3f4a5b"
    assert conn.category == "brokerage"
    assert conn.status == "active"
    assert conn.last_refreshed_at == datetime(2026, 4, 23, 3, 0, tzinfo=UTC)


def test_account_from_rest_uppercases_currency() -> None:
    raw = ACCOUNTS_RESPONSE["data"][0]  # type: ignore[index]
    account = account_from_rest(raw)
    assert account.currency == "AUD"
    assert account.masked_number == "xxxx4567"
    assert account.type == "transaction"


def test_balance_snapshot_handles_null_fields_and_lowercase_currency() -> None:
    taken_at = datetime(2026, 5, 9, tzinfo=UTC)
    rows = [
        balance_snapshot_from_rest(item, taken_at=taken_at)
        for item in BALANCES_RESPONSE["data"]  # type: ignore[union-attr]
    ]
    # Mixed-case "aud" upper-cased.
    assert rows[2].currency == "AUD"
    assert rows[2].current_balance == Decimal("-842.15")
    # Null fields preserved as None.
    assert rows[3].current_balance is None
    assert rows[3].available_balance is None
    assert rows[3].currency is None


def test_transaction_from_rest_decimal_and_dates() -> None:
    raw = TRANSACTIONS_RESPONSE["data"][0]  # type: ignore[index]
    tx = transaction_from_rest(raw, currency_fallback="AUD")
    assert tx.amount == Decimal("-64.20")
    assert isinstance(tx.amount, Decimal)
    assert tx.local_date == date(2026, 4, 22)
    assert tx.posted_at == datetime(2026, 4, 21, 23, 14, tzinfo=UTC)
    assert tx.direction == "debit"
    assert tx.category == "FOOD_AND_DRINK"
    assert tx.mcc == "5411"
    assert tx.currency == "AUD"


def test_transaction_from_rest_handles_null_datetime() -> None:
    raw = TRANSACTIONS_RESPONSE["data"][1]  # type: ignore[index]
    tx = transaction_from_rest(raw, currency_fallback="AUD")
    assert tx.posted_at is None
    assert tx.local_date == date(2026, 4, 21)


def test_trade_from_rest_decimals_and_currency() -> None:
    raw = TRADES_RESPONSE["data"][0]  # type: ignore[index]
    trade = trade_from_rest(raw)
    assert trade.symbol == "VOO"
    assert trade.type == "buy"
    assert trade.quantity == Decimal("10")
    assert trade.price == Decimal("465.2")
    assert trade.total_amount == Decimal("4652")
    assert trade.fees == Decimal("1")
    assert trade.trade_date == date(2026, 4, 15)
    assert trade.settlement_date == date(2026, 4, 17)
    # currency arrived lowercase — must be normalised
    assert trade.currency == "USD"
