from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlmodel import select

from app.backfill import _run
from app.db import session_scope
from app.models import Account, Trade, Transaction
from tests.conftest import make_mock_client
from tests.fixtures import (
    ACCOUNTS_RESPONSE,
    CONNECTIONS_RESPONSE,
    TRADES_RESPONSE,
    TRANSACTIONS_RESPONSE,
)


def _build_handler(captured_from: list[str] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        if path == "/v1/connections":
            return httpx.Response(200, json=CONNECTIONS_RESPONSE)
        if path == "/v1/accounts":
            return httpx.Response(200, json=ACCOUNTS_RESPONSE)
        if path == "/v1/transactions":
            if captured_from is not None and "from" in params:
                captured_from.append(params["from"])
            account_id = params.get("accountId")
            data = [
                tx for tx in TRANSACTIONS_RESPONSE["data"]  # type: ignore[union-attr]
                if tx["accountId"] == account_id
            ]
            return httpx.Response(
                200,
                json={
                    "data": data,
                    "pagination": {
                        "total": len(data), "limit": 500, "offset": 0, "hasMore": False,
                    },
                },
            )
        if path == "/v1/trades":
            account_id = params.get("accountId")
            data = [
                t for t in TRADES_RESPONSE["data"]  # type: ignore[union-attr]
                if t["accountId"] == account_id
            ]
            return httpx.Response(
                200,
                json={
                    "data": data,
                    "pagination": {
                        "total": len(data), "limit": 500, "offset": 0, "hasMore": False,
                    },
                },
            )
        return httpx.Response(404, json={"message": f"unmocked {path}"})

    return handler


async def test_backfill_resets_watermarks_to_now(temp_db: Path, monkeypatch: Any) -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return _build_handler(captured)(request)

    client, _http = make_mock_client(handler)

    # Patch RedbarkClient context manager used inside _run with our mock.
    import app.backfill as backfill_mod

    class _Ctx:
        async def __aenter__(self) -> Any:
            return client

        async def __aexit__(self, *_: Any) -> None:
            await client.aclose()

    monkeypatch.setattr(backfill_mod, "RedbarkClient", lambda **kw: _Ctx())

    before = datetime.now(UTC)
    await _run(
        from_str="2024-01-01",
        account_id=None,
        connection_id=None,
        include_trades=True,
    )
    after = datetime.now(UTC)

    # All `from` query params on /v1/transactions should be the requested 2024-01-01,
    # NOT a per-account watermark — backfill bypasses watermarks.
    assert captured, "expected at least one /v1/transactions call"
    for f in captured:
        # Allow either YYYY-MM-DD or ISO datetime — must contain 2024-01-01.
        assert f.startswith("2024-01-01")

    # Watermarks should be set to roughly now() for every account that was backfilled.
    with session_scope() as session:
        accounts = session.exec(select(Account)).all()
        for a in accounts:
            assert a.last_polled_transactions_at is not None
            assert before <= a.last_polled_transactions_at.replace(tzinfo=UTC) <= after + timedelta(seconds=2)


async def test_backfill_skips_trades_for_non_investment_accounts(temp_db: Path, monkeypatch: Any) -> None:
    trade_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/trades":
            trade_calls.append(request.url.params.get("accountId", ""))
        return _build_handler()(request)

    client, _http = make_mock_client(handler)

    import app.backfill as backfill_mod

    class _Ctx:
        async def __aenter__(self) -> Any:
            return client

        async def __aexit__(self, *_: Any) -> None:
            await client.aclose()

    monkeypatch.setattr(backfill_mod, "RedbarkClient", lambda **kw: _Ctx())

    await _run(
        from_str="2024-01-01",
        account_id=None,
        connection_id=None,
        include_trades=True,
    )

    # /v1/trades should only ever be called for the brokerage investment account.
    investment_id = "d4e5f6a7-b8c9-0123-d4e5-f6a7b8c90123"
    assert trade_calls and all(aid == investment_id for aid in trade_calls), (
        f"trades pulled for non-investment accounts: {trade_calls}"
    )

    # And the trade was actually persisted.
    with session_scope() as session:
        trades = session.exec(select(Trade)).all()
        assert len(trades) >= 1


async def test_backfill_no_trades_flag(temp_db: Path, monkeypatch: Any) -> None:
    trade_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/trades":
            trade_calls.append(request.url.params.get("accountId", ""))
        return _build_handler()(request)

    client, _http = make_mock_client(handler)

    import app.backfill as backfill_mod

    class _Ctx:
        async def __aenter__(self) -> Any:
            return client

        async def __aexit__(self, *_: Any) -> None:
            await client.aclose()

    monkeypatch.setattr(backfill_mod, "RedbarkClient", lambda **kw: _Ctx())

    await _run(
        from_str="2024-01-01",
        account_id=None,
        connection_id=None,
        include_trades=False,
    )

    assert trade_calls == [], "no /v1/trades calls expected with --no-trades"
    with session_scope() as session:
        assert not session.exec(select(Trade)).all()
        # Transactions should still be backfilled.
        assert session.exec(select(Transaction)).all()
