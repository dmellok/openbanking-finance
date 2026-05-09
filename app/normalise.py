"""Normalisation layer: Redbark REST JSON (camelCase, decimal strings) → internal rows.

The Redbark API returns:
- Currency in mixed case (lowercase or uppercase per endpoint) — we always upper-case.
- Money as decimal strings (`"1234.56"`, `"-45.50"`); we parse to Decimal. Never use float.
- Dates as either `YYYY-MM-DD` (transactions.date, trades.tradeDate) or ISO 8601 (datetime, lastRefreshedAt).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.models import (
    Account,
    BalanceSnapshot,
    Connection,
    Trade,
    Transaction,
)


def _norm_currency(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    return value.strip().upper() or None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_required_decimal(value: Any, field: str) -> Decimal:
    parsed = _to_decimal(value)
    if parsed is None:
        raise ValueError(f"required decimal field '{field}' is missing")
    return parsed


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    s = value.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def _parse_iso_date(value: Any) -> date | None:
    """Parse `YYYY-MM-DD` or the date portion of an ISO 8601 datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        return None
    s = value.strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return date.fromisoformat(s[:10])
    parsed = _parse_iso_datetime(s)
    return parsed.date() if parsed is not None else None


def _required_date(value: Any, field: str) -> date:
    parsed = _parse_iso_date(value)
    if parsed is None:
        raise ValueError(f"required date field '{field}' is missing or unparseable")
    return parsed


def connection_from_rest(payload: dict[str, Any]) -> Connection:
    return Connection(
        id=str(payload["id"]),
        provider=str(payload["provider"]),
        category=str(payload["category"]),
        institution_id=payload.get("institutionId"),
        institution_name=str(payload["institutionName"]),
        institution_logo=payload.get("institutionLogo"),
        status=str(payload["status"]),
        last_refreshed_at=_parse_iso_datetime(payload.get("lastRefreshedAt")),
        created_at=_parse_iso_datetime(payload.get("createdAt")),
        raw_json=payload,
    )


def account_from_rest(payload: dict[str, Any]) -> Account:
    currency = _norm_currency(payload.get("currency")) or "AUD"
    return Account(
        id=str(payload["id"]),
        connection_id=str(payload["connectionId"]),
        provider=payload.get("provider"),
        name=str(payload["name"]),
        masked_number=payload.get("accountNumber"),
        type=str(payload["type"]),
        institution_name=payload.get("institutionName"),
        currency=currency,
        raw_json=payload,
    )


def balance_snapshot_from_rest(
    payload: dict[str, Any],
    *,
    taken_at: datetime,
) -> BalanceSnapshot:
    return BalanceSnapshot(
        account_id=str(payload["accountId"]),
        current_balance=_to_decimal(payload.get("currentBalance")),
        available_balance=_to_decimal(payload.get("availableBalance")),
        currency=_norm_currency(payload.get("currency")),
        taken_at=taken_at,
    )


def transaction_from_rest(payload: dict[str, Any], *, currency_fallback: str) -> Transaction:
    direction = str(payload["direction"]).lower()
    return Transaction(
        id=str(payload["id"]),
        account_id=str(payload["accountId"]),
        status=payload.get("status"),
        posted_at=_parse_iso_datetime(payload.get("datetime")),
        local_date=_required_date(payload.get("date"), "date"),
        amount=_to_required_decimal(payload.get("amount"), "amount"),
        currency=_norm_currency(payload.get("currency")) or currency_fallback,
        direction=direction,
        description=payload.get("description"),
        merchant_name=payload.get("merchantName"),
        category=payload.get("category"),
        mcc=payload.get("merchantCategoryCode"),
        raw_json=payload,
    )


def trade_from_rest(payload: dict[str, Any]) -> Trade:
    return Trade(
        id=str(payload["id"]),
        account_id=str(payload["accountId"]),
        symbol=payload.get("symbol"),
        name=payload.get("name"),
        type=str(payload["type"]).lower(),
        quantity=_to_required_decimal(payload.get("quantity"), "quantity"),
        price=_to_decimal(payload.get("price")),
        currency=_norm_currency(payload.get("currency")),
        total_amount=_to_decimal(payload.get("totalAmount")),
        fees=_to_decimal(payload.get("fees")),
        trade_date=_required_date(payload.get("tradeDate"), "tradeDate"),
        settlement_date=_parse_iso_date(payload.get("settlementDate")),
        description=payload.get("description"),
        raw_json=payload,
    )
