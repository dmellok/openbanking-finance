PYTHON := python3.12
RUNNER := $(shell command -v uv >/dev/null 2>&1 && echo "uv run" || echo "")

.PHONY: install api backfill test lint typecheck check fmt clean

install:
	@if command -v uv >/dev/null 2>&1; then \
		uv venv --python 3.12 && uv sync --extra dev; \
	else \
		$(PYTHON) -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"; \
	fi

api:
	$(RUNNER) uvicorn app.api:app --host 0.0.0.0 --port 8000

backfill:
	$(RUNNER) python -m app.backfill $(ARGS)

test:
	$(RUNNER) pytest -q

lint:
	$(RUNNER) ruff check app tests

fmt:
	$(RUNNER) ruff format app tests
	$(RUNNER) ruff check --fix app tests

typecheck:
	$(RUNNER) mypy app

check: lint typecheck test

clean:
	rm -rf .venv .pytest_cache .ruff_cache .mypy_cache *.db *.db-* __pycache__
	find . -name __pycache__ -type d -exec rm -rf {} +
