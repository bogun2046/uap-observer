SHELL := /bin/sh
COMPOSE := docker compose --env-file .env.versions --env-file .env -f compose.yaml

.PHONY: dev dev-down dev-reset check lock staging-config

dev:
	./scripts/bootstrap-env.sh
	$(COMPOSE) up --build --detach --wait
	$(COMPOSE) ps

dev-down:
	$(COMPOSE) down

dev-reset:
	@test "$(CONFIRM)" = "delete-wp2-dev-data" || (echo "Set CONFIRM=delete-wp2-dev-data" >&2; exit 2)
	$(COMPOSE) down --volumes

check:
	uv lock --check
	uv run --frozen ruff check src tests tools alembic
	uv run --frozen mypy src tests tools
	uv run --frozen pytest --cov --cov-report=term-missing
	uv run --frozen python tools/validate_platform.py
	uv run --frozen python tools/validate_wp3.py
	uv run --frozen python tools/validate_wp4.py
	uv run --frozen python tools/validate_wp5.py
	uv run --frozen python tools/validate_wp6.py
	uv run --frozen python tools/validate_wp7.py

lock:
	uv lock

staging-config:
	@test -n "$(ENV_FILE)" || (echo "Set ENV_FILE=/path/to/staging.env" >&2; exit 2)
	docker compose --env-file .env.versions --env-file "$(ENV_FILE)" -f compose.yaml -f compose.staging.yaml config --quiet
