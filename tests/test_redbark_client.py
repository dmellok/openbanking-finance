from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest
from app.redbark_client import RedbarkClient


def _build_client(handler: Callable[[httpx.Request], httpx.Response]) -> RedbarkClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        transport=transport,
        base_url="https://api.redbark.co",
        headers={"Authorization": "Bearer test-key"},
    )
    return RedbarkClient(api_key="test-key", base_url="https://api.redbark.co", client=http)


async def test_list_connections_returns_data_array() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/connections"
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"data": [{"id": "c1"}, {"id": "c2"}]})

    async with _build_client(handler) as client:
        out = await client.list_connections()
        assert [c["id"] for c in out] == ["c1", "c2"]


async def test_pagination_handles_truncated_header_and_continues(caplog: pytest.LogCaptureFixture) -> None:
    pages = [
        {
            "data": [{"id": f"tx{i}"} for i in range(500)],
            "pagination": {"total": 1100, "limit": 500, "offset": 0, "hasMore": True},
        },
        {
            "data": [{"id": f"tx{i}"} for i in range(500, 1000)],
            "pagination": {"total": 1100, "limit": 500, "offset": 500, "hasMore": True},
        },
        {
            "data": [{"id": f"tx{i}"} for i in range(1000, 1100)],
            "pagination": {"total": 1100, "limit": 500, "offset": 1000, "hasMore": False},
        },
    ]
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        page = pages[call_count]
        call_count += 1
        # First page returns the truncation header — should be logged but not block pagination.
        headers = {"X-Redbark-Truncated": "true"} if call_count == 1 else {}
        return httpx.Response(200, json=page, headers=headers)

    caplog.set_level("WARNING", logger="app.redbark_client")
    async with _build_client(handler) as client:
        items = []
        async for tx in client.list_transactions(
            connection_id="conn1", account_id="acc1"
        ):
            items.append(tx)

    assert len(items) == 1100
    assert call_count == 3
    assert any("X-Redbark-Truncated" in m for m in caplog.messages)


async def test_429_respects_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("app.redbark_client.asyncio.sleep", fake_sleep)

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"message": "slow down"})
        return httpx.Response(200, json={"data": []})

    async with _build_client(handler) as client:
        await client.list_connections()

    assert call_count == 2
    assert 7.0 in sleeps  # honoured the Retry-After header


async def test_503_exponential_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("app.redbark_client.asyncio.sleep", fake_sleep)

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            return httpx.Response(503, json={"message": "provider unavailable"})
        return httpx.Response(200, json={"data": []})

    async with _build_client(handler) as client:
        await client.list_connections()

    # First three calls retry with backoff 1, 2, 4 seconds.
    assert call_count == 4
    assert sleeps[:3] == [1.0, 2.0, 4.0]


async def test_self_throttles_when_remaining_low(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("app.redbark_client.asyncio.sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": []},
            headers={"X-RateLimit-Remaining": "2", "X-RateLimit-Limit": "30"},
        )

    async with _build_client(handler) as client:
        await client.list_connections()

    # 12.0s self-throttle when remaining < 5
    assert 12.0 in sleeps


async def test_heavy_endpoints_capped_at_four_concurrent() -> None:
    in_flight = 0
    high_water = 0
    lock = asyncio.Lock()

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, high_water
        async with lock:
            in_flight += 1
            high_water = max(high_water, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        # Return a single page.
        return httpx.Response(
            200,
            json={
                "data": [{"id": "x"}],
                "pagination": {"total": 1, "limit": 500, "offset": 0, "hasMore": False},
            },
        )

    def handler(request: httpx.Request) -> httpx.Response:  # sync wrapper
        # MockTransport accepts sync handler; we adapt by spawning a task.
        # Simpler: use AsyncMockTransport via a coroutine handler.
        raise RuntimeError("should not be called — use async handler")

    transport = httpx.MockTransport(slow_handler)  # type: ignore[arg-type]
    http = httpx.AsyncClient(
        transport=transport,
        base_url="https://api.redbark.co",
        headers={"Authorization": "Bearer test-key"},
    )
    client = RedbarkClient(api_key="test-key", base_url="https://api.redbark.co", client=http)

    async def one_call(idx: int) -> None:
        items = []
        async for tx in client.list_transactions(
            connection_id="c", account_id=f"a{idx}"
        ):
            items.append(tx)

    await asyncio.gather(*(one_call(i) for i in range(8)))
    await client.aclose()

    assert high_water <= 4, f"expected <=4 concurrent heavy requests, saw {high_water}"


async def test_get_balances_chunks_account_ids_into_groups_of_100() -> None:
    captured_ids: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ids = request.url.params.get("accountIds", "").split(",")
        captured_ids.append(ids)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"accountId": aid, "currentBalance": "1.00", "availableBalance": "1.00", "currency": "AUD"}
                    for aid in ids
                ]
            },
        )

    async with _build_client(handler) as client:
        ids = [f"acc-{i}" for i in range(250)]
        rows = await client.get_balances(ids)

    assert len(rows) == 250
    # 250 ids → chunks of [100, 100, 50]
    assert [len(c) for c in captured_ids] == [100, 100, 50]


async def test_4xx_other_than_429_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    from app.redbark_client import RedbarkAPIError

    async with _build_client(handler) as client:
        with pytest.raises(RedbarkAPIError) as exc_info:
            await client.list_connections()
    assert exc_info.value.status_code == 404


async def test_list_transactions_naive_from_is_serialised_as_utc() -> None:
    # Regression: SQLite roundtrip strips tzinfo from watermarks. Fiskil rejects
    # naive datetimes as non-RFC3339, which Redbark surfaces as a generic 503.
    from datetime import datetime

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["from"] = request.url.params.get("from", "")
        return httpx.Response(
            200,
            json={"data": [], "pagination": {"total": 0, "limit": 500, "offset": 0, "hasMore": False}},
        )

    async with _build_client(handler) as client:
        async for _ in client.list_transactions(
            connection_id="c1",
            account_id="a1",
            from_=datetime(2026, 5, 9, 23, 35, 19, 805700),  # naive
        ):
            pass

    assert captured["from"].endswith("+00:00")
