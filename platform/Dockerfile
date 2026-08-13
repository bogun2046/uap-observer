ARG PYTHON_IMAGE=python:3.12.13-alpine3.23
FROM ${PYTHON_IMAGE} AS base

ARG UV_VERSION=0.12.3
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:${PATH}

RUN addgroup -S -g 10001 uap \
    && adduser -S -D -H -u 10001 -G uap uap \
    && python -m pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./

FROM base AS development
RUN uv sync --frozen --all-groups --no-install-project
COPY src ./src
COPY tests ./tests
COPY tools ./tools
COPY scripts/migrate-platform.sh ./scripts/
COPY alembic.ini ./
COPY alembic ./alembic
RUN uv sync --frozen --all-groups \
    && chown -R uap:uap /app
USER uap
CMD ["uap-platform-dev"]

FROM base AS runtime
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY tools/configure_roles.py tools/object_backup.py tools/wp3_runtime_probe.py ./tools/
COPY scripts/migrate-platform.sh ./scripts/
COPY alembic.ini ./
COPY alembic ./alembic
RUN uv sync --frozen --no-dev \
    && chown -R uap:uap /app
USER uap
CMD ["uap-platform-dev"]
