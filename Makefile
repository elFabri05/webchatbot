.PHONY: help install run mock-http test test-live up down logs reset-db

help:  ## Show targets
	@grep -E "^[a-z-]+:.*## " $(MAKEFILE_LIST) | awk -F ":.*## " "{printf \"  %-10s %s\\n\", \$$1, \$$2}"

install:  ## Install dependencies (uv) and seed the mock DB if missing
	uv sync
	uv run python scripts/reset_db.py --skip-app --if-missing

run:  ## Run the app locally (mock MCP over stdio, from .env)
	uv run uvicorn app.main:app --reload

mock-http:  ## Run the mock MCP server over HTTP on :8001
	uv run python -m mock_mcp.server --http --port 8001

test:  ## Run the test suite (no API calls)
	uv run pytest -q

test-live:  ## Run scenario tests against the REAL Claude API (costs money)
	RUN_LIVE_TESTS=1 uv run pytest -q -v tests/test_live.py

up:  ## Build and start app + mock MCP with docker compose
	docker compose up --build

down:  ## Stop the docker compose stack (data volumes are kept)
	docker compose down

logs:  ## Follow docker compose logs
	docker compose logs -f

reset-db:  ## Reset mock tracker data + clear conversations (docker if running, else local)
	@if docker compose ps --status running --services 2>/dev/null | grep -qx mock-mcp; then \
		echo "Resetting data in the running docker compose stack"; \
		docker compose exec mock-mcp python scripts/reset_db.py --skip-app && \
		docker compose exec app python scripts/reset_db.py --skip-mock; \
	else \
		uv run python scripts/reset_db.py; \
	fi
