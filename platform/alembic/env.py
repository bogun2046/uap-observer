"""Single authoritative Alembic environment for the target PostgreSQL platform."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic.runtime.migration import MigrationContext, MigrationInfo
from alembic.script import ScriptDirectory
from sqlalchemy import engine_from_config, pool, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import SQLAlchemyError

from alembic import context
from tools.configure_roles import (
    ALEMBIC_VERSION_ACCESS_QUERY,
    MIGRATOR_MEMBERSHIP_QUERY,
    verify_alembic_version_access,
    verify_migrator_membership,
)
from uap_platform.config import Settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None
LEGACY_OWNER_ENTRY_REVISION = "0021_wp10_publisher_projection"
LEGACY_OWNER_DDL_REVISION = "0022_wp10_claim_search_projection"


def _legacy_owner_upgrade_requested() -> bool:
    """Arm only when the requested upgrade includes the frozen 0022 revision."""

    destination = context.get_revision_argument()
    if not isinstance(destination, str):
        return False
    scripts = ScriptDirectory.from_config(config)
    revision = scripts.get_revision(destination)
    while revision is not None:
        if revision.revision == LEGACY_OWNER_DDL_REVISION:
            return True
        parent = revision.down_revision
        if not isinstance(parent, str):
            return False
        revision = scripts.get_revision(parent)
    return False


def _arm_legacy_wp10_3_owner_boundary(connection: Connection) -> None:
    """Provide owner entry for one frozen DDL preceding 0022's own SET ROLE."""

    connection.exec_driver_sql("SET ROLE uap_owner")


def _on_version_apply(
    ctx: MigrationContext, step: MigrationInfo, heads: set[str], run_args: dict[str, object]
) -> None:
    if (
        context.get_x_argument(as_dictionary=True).get("role") == "migrator"
        and step.is_upgrade
        and heads == {LEGACY_OWNER_ENTRY_REVISION}
        and _legacy_owner_upgrade_requested()
    ):
        if ctx.bind is None:
            raise RuntimeError("legacy owner boundary requires an online connection")
        _arm_legacy_wp10_3_owner_boundary(ctx.bind)


def database_url() -> str:
    """Read the URL from secret-backed runtime settings, never from source control."""

    settings = Settings()  # type: ignore[call-arg]
    url = settings.database_url.get_secret_value()
    if context.get_x_argument(as_dictionary=True).get("role") == "migrator":
        password = os.environ.get("UAP_MIGRATOR_PASSWORD")
        if not password:
            raise RuntimeError("UAP_MIGRATOR_PASSWORD is required for migrator mode")
        return (
            make_url(url)
            .set(username="uap_migrator", password=password)
            .render_as_string(hide_password=False)
        )
    return url


def run_migrations_offline() -> None:
    """Run migrations without creating an Engine."""

    if context.get_x_argument(as_dictionary=True).get("role") == "migrator":
        raise RuntimeError("migrator mode requires online membership verification")
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
        migrator_mode = context.get_x_argument(as_dictionary=True).get("role") == "migrator"
        context.configure(
            connection=connection, target_metadata=target_metadata,
            on_version_apply=_on_version_apply,
        )
        succeeded = False
        try:
            with context.begin_transaction():
                if migrator_mode:
                    row = connection.execute(text(MIGRATOR_MEMBERSHIP_QUERY)).first()
                    verify_migrator_membership(row)
                    version_access = connection.execute(text(ALEMBIC_VERSION_ACCESS_QUERY)).first()
                    verify_alembic_version_access(version_access)
                    if (
                        set(context.get_context().get_current_heads())
                        == {LEGACY_OWNER_ENTRY_REVISION}
                        and _legacy_owner_upgrade_requested()
                    ):
                        _arm_legacy_wp10_3_owner_boundary(connection)
                context.run_migrations()
            succeeded = True
        finally:
            if migrator_mode:
                try:
                    if connection.in_transaction():
                        connection.rollback()
                    connection.exec_driver_sql("RESET ROLE")
                    connection.commit()
                except SQLAlchemyError:
                    connection.invalidate()
                    if succeeded:
                        raise


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
