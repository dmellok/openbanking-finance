"""Async Redbark REST client.

Implements:
- Bearer auth.
- Retry-with-backoff on 429 (respect Retry-After) and 502/503/504 (exponential).
- Self-throttle when X-RateLimit-Remaining drops below the heavy-endpoint floor.
- asyncio.Semaphore(4) gating the heavy endpoints (/transactions, /holdings, /trades).
- Offset-based pagination via `pagination.hasMore` with X-Redbark-Truncated warning logged.
- Auto-chunking of accountIds=… (max 100) for /balances.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from types import TracebackType
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Heavy endpoints — capped at 4 concurrent in-flight requests per API key.
_HEAVY_PATHS = frozenset({"/v1/transactions", "/v1/holdings", "/v1/trades"})

# When X-RateLimit-Remaining drops below this, sleep before the next call.
_LOW_REMAINING_FLOOR = 5
# Approx duration of a 1-minute window divided into ~5 chunks.
_LOW_REMAINING_SLEEP_S = 12.0

# Status codes we retry with exponential backoff.
_RETRYABLE_5XX = frozenset({502, 503, 504})

# Default per-retry timeout.
_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)


class RedbarkAPIError(Exception):
    """Non-retryable error from the Redbark API (4xx other than 429)."""

    def __init__(self, status_code: int, message: str, *, details: object | None = None) -> None:
        super().__init__(f"Redbark {status_code}: {message}")
        self.status_code = status_code
        self.details = details


class RedbarkClient:
    """Asynchronous Redbark REST client.

    Use as an async context manager so the underlying httpx client is closed:

        async with RedbarkClient(api_key="…", base_url="https://api.redbark.co") as client:
            connections = await client.list_connections()
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.redbark.co",
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
        max_retries: int = 4,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._max_retries = max_retries
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            base_url=self._base_url,
        )
        if not self._owns_client:
            # Caller-supplied clients may not have the auth header set.
            self._client.headers.setdefault("Authorization", f"Bearer {api_key}")
        self._heavy_sem = asyncio.Semaphore(4)

    async def __aenter__(self) -> RedbarkClient:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ── Public methods ────────────────────────────────────────────────────────

    async def list_connections(self) -> list[dict[str, Any]]:
        body = await self._get_json("/v1/connections")
        data = body.get("data", [])
        return list(data) if isinstance(data, list) else []

    async def list_accounts(self, *, page_size: int = 200) -> list[dict[str, Any]]:
        return await self._collect_paginated("/v1/accounts", params=None, page_size=page_size)

    async def get_balances(self, account_ids: Sequence[str]) -> list[dict[str, Any]]:
        ids = list(account_ids)
        out: list[dict[str, Any]] = []
        for chunk in _chunks(ids, 100):
            body = await self._get_json(
                "/v1/balances", params={"accountIds": ",".join(chunk)}
            )
            data = body.get("data", [])
            if isinstance(data, list):
                out.extend(data)
        return out

    async def list_categories(self) -> list[dict[str, Any]]:
        body = await self._get_json("/v1/categories")
        # Docs: response root is {"categories": [{"key", "label"}]}. Fall back to "data".
        cats = body.get("categories", body.get("data", []))
        return list(cats) if isinstance(cats, list) else []

    async def list_holdings(
        self, *, connection_id: str, account_id: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {"connectionId": connection_id}
        if account_id is not None:
            params["accountId"] = account_id
        body = await self._get_json("/v1/holdings", params=params)
        data = body.get("data", [])
        return list(data) if isinstance(data, list) else []

    async def list_transactions(
        self,
        *,
        connection_id: str,
        account_id: str,
        from_: date | datetime | str | None = None,
        to: date | datetime | str | None = None,
        page_size: int = 500,
    ) -> AsyncIterator[dict[str, Any]]:
        params: dict[str, str] = {
            "connectionId": connection_id,
            "accountId": account_id,
        }
        if from_ is not None:
            params["from"] = _format_dt(from_)
        if to is not None:
            params["to"] = _format_dt(to)
        async for item in self._iter_paginated(
            "/v1/transactions", params=params, page_size=page_size
        ):
            yield item

    async def list_trades(
        self,
        *,
        connection_id: str,
        account_id: str | None = None,
        from_: date | datetime | str | None = None,
        to: date | datetime | str | None = None,
        page_size: int = 500,
    ) -> AsyncIterator[dict[str, Any]]:
        params: dict[str, str] = {"connectionId": connection_id}
        if account_id is not None:
            params["accountId"] = account_id
        if from_ is not None:
            params["from"] = _format_dt(from_)
        if to is not None:
            params["to"] = _format_dt(to)
        async for item in self._iter_paginated(
            "/v1/trades", params=params, page_size=page_size
        ):
            yield item

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _collect_paginated(
        self,
        path: str,
        *,
        params: dict[str, str] | None,
        page_size: int,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        async for item in self._iter_paginated(path, params=params, page_size=page_size):
            out.append(item)
        return out

    async def _iter_paginated(
        self,
        path: str,
        *,
        params: dict[str, str] | None,
        page_size: int,
    ) -> AsyncIterator[dict[str, Any]]:
        offset = 0
        while True:
            page_params: dict[str, str] = dict(params or {})
            page_params["limit"] = str(page_size)
            page_params["offset"] = str(offset)
            body = await self._get_json(path, params=page_params)
            data = body.get("data", [])
            if not isinstance(data, list):
                return
            for item in data:
                if isinstance(item, dict):
                    yield item
            pagination = body.get("pagination") or {}
            has_more = bool(pagination.get("hasMore", False))
            if not has_more:
                return
            offset += page_size

    async def _get_json(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        async with self._heavy_gate(path):
            return await self._request_with_retry("GET", path, params=params)

    @asynccontextmanager
    async def _heavy_gate(self, path: str) -> AsyncIterator[None]:
        if path in _HEAVY_PATHS:
            async with self._heavy_sem:
                yield
        else:
            yield

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None,
    ) -> dict[str, Any]:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.request(method, path, params=params)
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as exc:
                if attempt > self._max_retries:
                    raise
                wait = _backoff(attempt)
                logger.warning(
                    "Redbark %s %s: network error %r, retrying in %.1fs (attempt %d/%d)",
                    method, path, exc, wait, attempt, self._max_retries,
                )
                await asyncio.sleep(wait)
                continue

            self._maybe_log_truncation(path, response)

            if response.status_code == 429:
                if attempt > self._max_retries:
                    raise RedbarkAPIError(429, "rate limited; retries exhausted")
                wait = _retry_after_seconds(response) or _backoff(attempt)
                logger.warning(
                    "Redbark %s %s: 429 rate limited, retrying in %.1fs (attempt %d/%d)",
                    method, path, wait, attempt, self._max_retries,
                )
                await asyncio.sleep(wait)
                continue

            if response.status_code in _RETRYABLE_5XX:
                if attempt > self._max_retries:
                    raise RedbarkAPIError(response.status_code, response.text)
                wait = _backoff(attempt)
                logger.warning(
                    "Redbark %s %s: %d, retrying in %.1fs (attempt %d/%d)",
                    method, path, response.status_code, wait, attempt, self._max_retries,
                )
                await asyncio.sleep(wait)
                continue

            if response.status_code >= 400:
                _raise_for_status(response)

            await self._maybe_self_throttle(response)
            data = response.json()
            return data if isinstance(data, dict) else {"data": data}

    @staticmethod
    def _maybe_log_truncation(path: str, response: httpx.Response) -> None:
        if response.headers.get("X-Redbark-Truncated", "").lower() == "true":
            logger.warning(
                "Redbark %s returned X-Redbark-Truncated: true — continuing pagination",
                path,
            )

    @staticmethod
    async def _maybe_self_throttle(response: httpx.Response) -> None:
        remaining_raw = response.headers.get("X-RateLimit-Remaining")
        if remaining_raw is None:
            return
        try:
            remaining = int(remaining_raw)
        except ValueError:
            return
        if remaining < _LOW_REMAINING_FLOOR:
            logger.info(
                "Redbark X-RateLimit-Remaining=%d below floor %d; self-throttling for %.1fs",
                remaining, _LOW_REMAINING_FLOOR, _LOW_REMAINING_SLEEP_S,
            )
            await asyncio.sleep(_LOW_REMAINING_SLEEP_S)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return None


def _backoff(attempt: int) -> float:
    """Exponential backoff: 1, 2, 4, 8, 16… seconds (capped)."""
    return float(min(2 ** (attempt - 1), 30))


def _raise_for_status(response: httpx.Response) -> None:
    try:
        body = response.json()
    except ValueError:
        body = {"message": response.text}
    message = body.get("message") if isinstance(body, dict) else str(body)
    details = body.get("details") if isinstance(body, dict) else None
    raise RedbarkAPIError(response.status_code, str(message or "request failed"), details=details)


def _chunks(seq: Sequence[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(seq), size):
        yield list(seq[i : i + size])


def _format_dt(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        # Fiskil rejects naive datetimes as non-RFC3339. We only ever write UTC
        # watermarks, but SQLite roundtrips strip tzinfo — re-attach on the way out.
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
