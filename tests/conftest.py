from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import get_settings


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Spin up a fresh SQLite DB for the test, reset all engine caches afterwards."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("REDBARK_API_KEY", "test-key")
    get_settings.cache_clear()

    # Import these lazily so the env vars are applied first.
    from app.db import init_db, reset_engine_cache

    reset_engine_cache()
    init_db()
    try:
        yield db_path
    finally:
        reset_engine_cache()
        get_settings.cache_clear()
        # Drop the env vars to keep tests independent.
        for var in ("DATABASE_URL", "REDBARK_API_KEY"):
            os.environ.pop(var, None)


def make_mock_client(handler: Any) -> tuple[Any, httpx.AsyncClient]:
    """Build a RedbarkClient backed by an httpx.MockTransport with `handler`."""
    from app.redbark_client import RedbarkClient

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        transport=transport,
        base_url="https://api.redbark.co",
        headers={"Authorization": "Bearer test-key"},
    )
    client = RedbarkClient(api_key="test-key", base_url="https://api.redbark.co", client=http)
    return client, http
