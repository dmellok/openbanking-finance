"""One-off historical backfill CLI.

Usage:
    uv run python -m app.backfill --from 2024-01-01
    uv run python -m app.backfill --from 2024-01-01 --account-id <uuid>
    uv run python -m app.backfill --from 2024-01-01 --connection-id <uuid> --no-trades

Pulls per-account transactions (and trades for investment accounts on brokerage
connections) from the requested date forward, bypassing the per-account
watermark. Once finished, sets `last_polled_*_at` to now() so the regular
poller does not re-pull historical data.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

import typer
from sqlmodel import select

from app.config import get_settings
from app.db import init_db, session_scope
from app.models import Account, Connection, SyncRun
from app.normalise import (
    account_from_rest,
    connection_from_rest,
    trade_from_rest,
    transaction_from_rest,
)
from app.poller import (
    upsert_account,
    upsert_connection,
    upsert_trade,
    upsert_transaction,
)
from app.redbark_client import RedbarkClient

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help="Redbark historical backfill")


def _default_from() -> str:
    return (date.today() - timedelta(days=2 * 365)).isoformat()


@app.command()
def main(
    from_: Annotated[
        str,
        typer.Option("--from", help="Earliest date (YYYY-MM-DD); default = 2 years ago"),
    ] = _default_from(),
    account_id: Annotated[
        str | None,
        typer.Option("--account-id", help="Limit to a single account UUID"),
    ] = None,
    connection_id: Annotated[
        str | None,
        typer.Option("--connection-id", help="Limit to accounts on this connection UUID"),
    ] = None,
    no_trades: Annotated[
        bool,
        typer.Option("--no-trades", help="Skip /v1/trades for investment accounts"),
    ] = False,
) -> None:
    """Run a historical backfill from --from to now."""
    logging.basicConfig(level=get_settings().log_level)
    init_db()
    asyncio.run(
        _run(
            from_str=from_,
            account_id=account_id,
            connection_id=connection_id,
            include_trades=not no_trades,
        )
    )


async def _run(
    *,
    from_str: str,
    account_id: str | None,
    connection_id: str | None,
    include_trades: bool,
) -> None:
    from_dt = _parse_from(from_str)
    settings = get_settings()
    started_at = datetime.now(UTC)

    counts = {"connections": 0, "accounts": 0, "transactions": 0, "trades": 0}
    detail = (
        f"from={from_str} account_id={account_id or '*'} "
        f"connection_id={connection_id or '*'} include_trades={include_trades}"
    )

    with session_scope() as session:
        run = SyncRun(kind="backfill", started_at=started_at, status="running", detail=detail)
        session.add(run)
        session.flush()
        run_id = run.id

    async with RedbarkClient(
        api_key=settings.redbark_api_key,
        base_url=settings.redbark_base_url,
    ) as client:
        try:
            # 1. Refresh connections + accounts so we have the latest set.
            connections_raw = await client.list_connections()
            with session_scope() as session:
                for raw in connections_raw:
                    upsert_connection(session, connection_from_rest(raw))
            counts["connections"] = len(connections_raw)

            accounts_raw = await client.list_accounts()
            with session_scope() as session:
                for raw in accounts_raw:
                    upsert_account(session, account_from_rest(raw))
            counts["accounts"] = len(accounts_raw)

            # 2. Filter accounts.
            with session_scope() as session:
                connection_categories = {
                    c.id: c.category for c in session.exec(select(Connection)).all()
                }
                account_rows: list[tuple[str, str, str, str]] = [
                    (a.id, a.connection_id, a.type, a.currency)
                    for a in session.exec(select(Account)).all()
                    if (account_id is None or a.id == account_id)
                    and (connection_id is None or a.connection_id == connection_id)
                ]

            if not account_rows:
                logger.warning(
                    "backfill: no accounts matched the filter (account_id=%s, connection_id=%s)",
                    account_id, connection_id,
                )

            # 3. Per-account pull.
            backfilled_account_ids: list[str] = []
            for acc_id, conn_id, acc_type, currency in account_rows:
                if conn_id not in connection_categories:
                    continue
                logger.info("backfill: account %s from %s", acc_id, from_dt.isoformat())
                async for raw in client.list_transactions(
                    connection_id=conn_id,
                    account_id=acc_id,
                    from_=from_dt,
                ):
                    with session_scope() as session:
                        upsert_transaction(
                            session,
                            transaction_from_rest(raw, currency_fallback=currency),
                        )
                    counts["transactions"] += 1

                if (
                    include_trades
                    and connection_categories[conn_id] == "brokerage"
                    and acc_type == "investment"
                ):
                    async for raw in client.list_trades(
                        connection_id=conn_id,
                        account_id=acc_id,
                        from_=from_dt,
                    ):
                        with session_scope() as session:
                            upsert_trade(session, trade_from_rest(raw))
                        counts["trades"] += 1

                backfilled_account_ids.append(acc_id)

            # 4. Reset watermarks to "now" so the poller does not re-fetch history.
            now = datetime.now(UTC)
            with session_scope() as session:
                for aid in backfilled_account_ids:
                    account = session.get(Account, aid)
                    if account is None:
                        continue
                    account.last_polled_transactions_at = now
                    if include_trades:
                        # Always set trades watermark even for non-investment accounts —
                        # it's a no-op for the poller in that case.
                        account.last_polled_trades_at = now

            _finalise(run_id, status="ok", counts=counts)
            logger.info("backfill done: %s", counts)
        except Exception as exc:
            _finalise(run_id, status="error", counts=counts, detail_extra=repr(exc))
            logger.exception("backfill failed")
            raise


def _parse_from(value: str) -> datetime:
    # Accept YYYY-MM-DD or full ISO 8601.
    if "T" in value:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        dt = datetime.fromisoformat(value + "T00:00:00+00:00")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _finalise(
    run_id: int | None,
    *,
    status: str,
    counts: dict[str, int],
    detail_extra: str | None = None,
) -> None:
    if run_id is None:
        return
    with session_scope() as session:
        run = session.get(SyncRun, run_id)
        if run is None:
            return
        run.finished_at = datetime.now(UTC)
        run.status = status
        run.counts = counts
        if detail_extra is not None:
            run.detail = (run.detail or "") + " | " + detail_extra


if __name__ == "__main__":
    app()
