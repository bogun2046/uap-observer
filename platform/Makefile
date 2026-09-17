SHELL := /bin/sh
COMPOSE := docker compose --env-file .env.versions --env-file .env -f compose.yaml
V1_COMPOSE_PROJECT_NAME ?= uap-platform-v1-1
COMPOSE_V1 := UAP_COMPOSE_PROJECT_NAME=$(V1_COMPOSE_PROJECT_NAME) docker compose --env-file .env.versions --env-file .env -f compose.yaml -f compose.v1.yaml

.PHONY: dev v1-dev v1-schedule v1-down dev-down dev-reset check lock staging-config wp10-runtime wp10-6-b-migration g10-25-object-store

dev:
	./scripts/bootstrap-env.sh
	$(COMPOSE) up --build --detach --wait
	$(COMPOSE) ps

v1-dev:
	@test -n "$(DEEPSEEK_API_KEY)" || (echo "Export DEEPSEEK_API_KEY securely; do not write it to platform/.env" >&2; exit 2)
	./scripts/bootstrap-env.sh
	$(COMPOSE_V1) up --build --detach --wait
	$(COMPOSE_V1) ps

v1-schedule:
	$(COMPOSE_V1) run --rm --entrypoint uap-platform-v1-schedule v1-scheduler

v1-down:
	$(COMPOSE_V1) down

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
	uv run --frozen python tools/validate_wp8.py
	uv run --frozen python tools/validate_wp9.py
	uv run --frozen python -m tools.validate_wp10
	uv run --frozen python -m tools.validate_wp10_6

wp10-runtime:
	@test -n "$(UAP_WP10_EVIDENCE_DIR)" || (echo "Set UAP_WP10_EVIDENCE_DIR to a directory outside the repository" >&2; exit 2)
	uv run --frozen python -m tools.wp10_runtime_probe --evidence-dir "$(UAP_WP10_EVIDENCE_DIR)"

wp10-6-b-migration:
	@test -n "$(UAP_WP10_6_ADMIN_URL)" || (echo "Set UAP_WP10_6_ADMIN_URL in the environment" >&2; exit 2)
	@test -n "$(UAP_WP10_6_EVIDENCE_OUT)" || (echo "Set UAP_WP10_6_EVIDENCE_OUT outside the repository" >&2; exit 2)
	UAP_WP10_6_ADMIN_URL="$(UAP_WP10_6_ADMIN_URL)" uv run --frozen python -m tools.wp10_6_migration_probe --evidence-out "$(UAP_WP10_6_EVIDENCE_OUT)"

g10-25-object-store:
	./scripts/g10-25-disposable-object-store.sh start

lock:
	uv lock

staging-config:
	@test -n "$(ENV_FILE)" || (echo "Set ENV_FILE=/path/to/staging.env" >&2; exit 2)
	docker compose --env-file .env.versions --env-file "$(ENV_FILE)" -f compose.yaml -f compose.staging.yaml config --quiet
