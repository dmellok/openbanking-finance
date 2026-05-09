# pyFinance v2

Personal finance dashboard built on top of [Redbark](https://docs.redbark.co/) — an Australian open-banking + brokerage sync service. FastAPI serves both a REST JSON API and a static dashboard; APScheduler polls Redbark every 15 minutes; SQLite (via SQLModel) is the local store. There are no webhooks — Redbark does not store financial data long-term, so the local database is the source of truth.

## Stack

- **FastAPI** + **uvicorn** — JSON API + static dashboard files
- **SQLite** (WAL) via **SQLModel**
- **APScheduler** — in-process poller (15-minute interval, in the FastAPI event loop)
- **httpx** — async Redbark REST client (rate-limit aware, 4-concurrent semaphore for heavy endpoints)
- **Apache ECharts** (CDN) — bespoke vanilla HTML/JS dashboard, no build step
- **pydantic-settings** + **python-dotenv** — config

Pinned to **Python 3.12**. `uv` is used if available; otherwise pip + venv.

## Setup

```bash
make install
cp .env.example .env
# edit .env and set REDBARK_API_KEY=...
```

Get an API key from your Redbark account (Developer or Professional plan required for API access).

## Run

```bash
# API + scheduler (combined process)
make api

# One-off historical backfill (default: 2 years back)
make backfill ARGS="--from 2024-01-01"

# Backfill a single account, skipping trades
make backfill ARGS="--from 2024-01-01 --account-id <uuid> --no-trades"
```

The dashboard is at <http://localhost:8000/>. JSON endpoints live under `/api/`.

## Suggested workflow on first install

1. `make install`
2. Set `REDBARK_API_KEY` in `.env`
3. Run `make api` once so the DB is created and the first poll populates connections + accounts. Stop after one poll cycle (15 min) or use `python -m app.poller --once`.
4. Run `make backfill ARGS="--from 2024-01-01"` to pull two years of history. After backfill, the per-account watermarks are advanced to "now" so the regular poller will not re-fetch history.
5. Restart `make api` and let the scheduler take over.

## Editing budgets

```bash
curl -X POST http://localhost:8000/api/budgets \
    -H "Content-Type: application/json" \
    -d '{"category": "FOOD_AND_DRINK", "monthly_limit": "800.00", "currency": "AUD"}'
```

Or use the Cash flow tab — budget editing is wired into the dashboard.

## Development

```bash
make check       # lint + typecheck + tests
make fmt         # ruff format + autofix
make test        # pytest only
make typecheck   # mypy --strict
make lint        # ruff check
```

## Architecture notes

- **Watermarks**: each account has `last_polled_transactions_at` and `last_polled_trades_at`. Each poll requests `from = watermark - 24h` so late-posting / re-categorised rows refresh. Idempotent merge on provider ID prevents duplicates.
- **Rate limits**: heavy endpoints (`/transactions`, `/trades`, `/holdings`) are gated by an `asyncio.Semaphore(4)`. The client self-throttles when `X-RateLimit-Remaining` drops below 5 and respects `Retry-After` on 429s.
- **Truncation**: `X-Redbark-Truncated: true` is logged but does not stop pagination — the loop keeps requesting the next offset.
- **Money**: Decimal end-to-end. JSON serialisation emits Decimals as strings.

## Limitations

- Single-user, single-machine. No auth on the dashboard.
- SQLite — fine for one user, not suitable for multi-tenant.
