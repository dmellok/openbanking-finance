"""Run the dashboard against demo.db with the poller disabled.

Used for capturing screenshots without touching the real Redbark API.
"""

from __future__ import annotations

import os

import uvicorn

# Must be set before importing app.api (settings are cached on first read).
os.environ.setdefault("DATABASE_URL", "sqlite:///./demo.db")
os.environ.setdefault("REDBARK_API_KEY", "demo")

from app.api import create_app  # noqa: E402

app = create_app(with_scheduler=False)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
