"""Single authoritative Alembic environment for the target PostgreSQL platform."""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

from alembic import context
from uap_platform.config import Settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def database_url() -> str:
    """Read the URL from secret-backed runtime settings, never from source control."""

    settings = Settings()  # type: ignore[call-arg]
    url = settings.database_url.get_secret_value()
    if context.get_x_argument(as_dictionary=True).get("role") == "migrator":
        password = os.environ.get("UAP_MIGRATOR_PASSWORD")
        if not password:
            raise RuntimeError("UAP_MIGRATOR_PASSWORD is required for migrator mode")
        return make_url(url).set(username="uap_migrator", password=password).render_as_string(
            hide_password=False
        )
    return url


def run_migrations_offline() -> None:
    """Run migrations without creating an Engine."""

    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured PostgreSQL database."""

    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
