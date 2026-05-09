"""Polling architecture: every POLL_INTERVAL_MINUTES, sync the world from Redbark.

Cycle:
1. GET /v1/connections             → upsert
2. GET /v1/accounts                → upsert (preserves local watermark columns)
3. GET /v1/balances?accountIds=…   → append rows to balance_snapshots (time series)
4. For each account, paginated transactions from `watermark - 24h` → upsert by id
5. For each investment account on a brokerage connection, paginated trades from
   `watermark - 24h` → upsert by id
6. Bump per-account watermarks; record sync_runs row with counts.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlmodel import Session, select

from app.config import get_settings
from app.db import init_db, session_scope
from app.models import (
    Account,
    Connection,
    SyncRun,
    Trade,
    Transaction,
)
from app.normalise import (
    account_from_rest,
    balance_snapshot_from_rest,
    connection_from_rest,
    trade_from_rest,
    transaction_from_rest,
)
from app.redbark_client import RedbarkClient

logger = logging.getLogger(__name__)

# Overlap window: re-fetch the last 24h on every poll so late-posting /
# re-categorised rows refresh. Idempotent merge on provider id handles dupes.
_OVERLAP = timedelta(hours=24)


# ── Upserts ──────────────────────────────────────────────────────────────────


def upsert_connection(session: Session, conn: Connection) -> None:
    existing = session.get(Connection, conn.id)
    if existing is None:
        session.add(conn)
        return
    for field in (
        "provider",
        "category",
        "institution_id",
        "institution_name",
        "institution_logo",
        "status",
        "last_refreshed_at",
        "created_at",
        "raw_json",
    ):
        setattr(existing, field, getattr(conn, field))


def upsert_account(session: Session, account: Account) -> None:
    """Upsert account, preserving local-only `last_polled_*` watermarks on update."""
    existing = session.get(Account, account.id)
    if existing is None:
        session.add(account)
        return
    for field in (
        "connection_id",
        "provider",
        "name",
        "masked_number",
        "type",
        "institution_name",
        "currency",
        "raw_json",
    ):
        setattr(existing, field, getattr(account, field))


def upsert_transaction(session: Session, tx: Transaction) -> None:
    existing = session.get(Transaction, tx.id)
    if existing is None:
        session.add(tx)
        return
    for field in (
        "account_id",
        "status",
        "posted_at",
        "local_date",
        "amount",
        "currency",
        "direction",
        "description",
        "merchant_name",
        "category",
        "mcc",
        "raw_json",
    ):
        setattr(existing, field, getattr(tx, field))


def upsert_trade(session: Session, trade: Trade) -> None:
    existing = session.get(Trade, trade.id)
    if existing is None:
        session.add(trade)
        return
    for field in (
        "account_id",
        "symbol",
        "name",
        "type",
        "quantity",
        "price",
        "currency",
        "total_amount",
        "fees",
        "trade_date",
        "settlement_date",
        "description",
        "raw_json",
    ):
        setattr(existing, field, getattr(trade, field))


# ── Cycle ────────────────────────────────────────────────────────────────────


async def run_once(
    client: RedbarkClient,
    *,
    kind: str = "poll",
    detail: str | None = None,
) -> dict[str, int]:
    """Run a full poll cycle. Returns counts dict and records sync_runs row."""
    started_at = datetime.now(UTC)
    counts = {
        "connections": 0,
        "accounts": 0,
        "balances": 0,
        "transactions": 0,
        "trades": 0,
    }

    with session_scope() as session:
        run = SyncRun(kind=kind, started_at=started_at, status="running", detail=detail)
        session.add(run)
        session.flush()
        run_id = run.id

    try:
        # 1. Connections
        connections_raw = await client.list_connections()
        with session_scope() as session:
            for raw in connections_raw:
                upsert_connection(session, connection_from_rest(raw))
            counts["connections"] = len(connections_raw)

        # 2. Accounts
        accounts_raw = await client.list_accounts()
        with session_scope() as session:
            for raw in accounts_raw:
                upsert_account(session, account_from_rest(raw))
            counts["accounts"] = len(accounts_raw)

        # 3. Balance snapshots (append-only)
        account_ids = [str(row["id"]) for row in accounts_raw]
        if account_ids:
            taken_at = datetime.now(UTC)
            balance_rows = await client.get_balances(account_ids)
            with session_scope() as session:
                for raw in balance_rows:
                    snap = balance_snapshot_from_rest(raw, taken_at=taken_at)
                    session.add(snap)
                counts["balances"] = len(balance_rows)

        # Snapshot the fields we need for the per-account loops as plain tuples,
        # so we don't touch detached SQLAlchemy instances after the session closes.
        with session_scope() as session:
            connection_categories = {
                c.id: c.category for c in session.exec(select(Connection)).all()
            }
            account_rows: list[
                tuple[str, str, str, str, datetime | None, datetime | None]
            ] = [
                (
                    a.id,
                    a.connection_id,
                    a.type,
                    a.currency,
                    a.last_polled_transactions_at,
                    a.last_polled_trades_at,
                )
                for a in session.exec(select(Account)).all()
            ]

        # 4. Transactions per account
        for (
            acc_id,
            conn_id,
            acc_type,
            currency,
            tx_watermark,
            trades_watermark,
        ) in account_rows:
            if conn_id not in connection_categories:
                continue
            from_ = _from_for(tx_watermark, kind=kind)
            tx_count = 0
            async for raw in client.list_transactions(
                connection_id=conn_id,
                account_id=acc_id,
                from_=from_,
            ):
                with session_scope() as session:
                    upsert_transaction(
                        session,
                        transaction_from_rest(raw, currency_fallback=currency),
                    )
                tx_count += 1
            counts["transactions"] += tx_count
            with session_scope() as session:
                _bump_watermark(session, acc_id, "last_polled_transactions_at")

            # 5. Trades — only for investment accounts on brokerage connections
            if connection_categories[conn_id] != "brokerage" or acc_type != "investment":
                continue
            from_trades = _from_for(trades_watermark, kind=kind)
            trade_count = 0
            async for raw in client.list_trades(
                connection_id=conn_id,
                account_id=acc_id,
                from_=from_trades,
            ):
                with session_scope() as session:
                    upsert_trade(session, trade_from_rest(raw))
                trade_count += 1
            counts["trades"] += trade_count
            with session_scope() as session:
                _bump_watermark(session, acc_id, "last_polled_trades_at")

        _finalise_run(run_id, status="ok", counts=counts)
        logger.info("poll cycle ok: %s", counts)
        return counts
    except Exception as exc:
        _finalise_run(run_id, status="error", counts=counts, detail=repr(exc))
        logger.exception("poll cycle failed")
        raise


def _from_for(watermark: datetime | None, *, kind: str) -> datetime | None:
    if watermark is None:
        return None  # API uses 30-day default for transactions when omitted
    if kind == "backfill":
        return None  # backfill handles its own --from
    return watermark - _OVERLAP


def _bump_watermark(session: Session, account_id: str, field: str) -> None:
    account = session.get(Account, account_id)
    if account is None:
        return
    setattr(account, field, datetime.now(UTC))


def _finalise_run(
    run_id: int | None,
    *,
    status: str,
    counts: dict[str, int],
    detail: str | None = None,
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
        if detail is not None:
            run.detail = detail


# ── Watermark reset (for backfill) ───────────────────────────────────────────


def reset_watermarks_to_now(account_ids: Iterable[str]) -> None:
    now = datetime.now(UTC)
    with session_scope() as session:
        for aid in account_ids:
            account = session.get(Account, aid)
            if account is None:
                continue
            account.last_polled_transactions_at = now
            account.last_polled_trades_at = now


# ── APScheduler integration ──────────────────────────────────────────────────


_scheduler: AsyncIOScheduler | None = None


async def _scheduled_job() -> None:
    settings = get_settings()
    async with RedbarkClient(
        api_key=settings.redbark_api_key,
        base_url=settings.redbark_base_url,
    ) as client:
        with suppress(Exception):
            # Errors are already logged + recorded in sync_runs by run_once.
            await run_once(client, kind="poll")


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        _scheduled_job,
        IntervalTrigger(minutes=settings.poll_interval_minutes),
        id="redbark_poll",
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(UTC),  # run once immediately on startup
    )
    scheduler.start()
    _scheduler = scheduler
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    with suppress(Exception):
        _scheduler.shutdown(wait=False)
    _scheduler = None


# ── CLI: `python -m app.poller --once` ───────────────────────────────────────


def _cli() -> None:
    logging.basicConfig(level=get_settings().log_level)
    parser = argparse.ArgumentParser(description="Redbark poller")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = parser.parse_args()

    init_db()

    if args.once:
        settings = get_settings()

        async def _main() -> None:
            async with RedbarkClient(
                api_key=settings.redbark_api_key,
                base_url=settings.redbark_base_url,
            ) as client:
                counts = await run_once(client, kind="poll", detail="cli --once")
                logger.info("done: %s", counts)

        asyncio.run(_main())
        return

    # Default mode: keep scheduler running.
    async def _serve() -> None:
        start_scheduler()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            stop_scheduler()

    asyncio.run(_serve())


if __name__ == "__main__":
    _cli()
