"""Create database roles and the five authoritative schemas.

Revision ID: 0001_roles_and_schemas
Revises:
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "0001_roles_and_schemas"
down_revision = None
branch_labels = None
depends_on = None

NOLOGIN_ROLES = ("uap_owner",)
LOGIN_ROLES = (
    "uap_migrator",
    "uap_api",
    "uap_worker",
    "uap_scheduler",
    "uap_publisher",
    "uap_public_reader",
    "uap_audit_reader",
    "uap_backup",
)


def upgrade() -> None:
    for role in NOLOGIN_ROLES:
        op.execute(
            f"""
            DO $role$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                        NOREPLICATION NOBYPASSRLS;
                END IF;
            END
            $role$;
            """
        )
    for role in LOGIN_ROLES:
        op.execute(
            f"""
            DO $role$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                        NOREPLICATION NOBYPASSRLS;
                END IF;
            END
            $role$;
            """
        )

    op.execute("GRANT uap_owner TO uap_migrator")
    op.execute("ALTER ROLE uap_migrator NOINHERIT")
    op.execute("ALTER ROLE uap_public_reader SET search_path = public, pg_catalog")
    op.execute("ALTER ROLE uap_backup SET default_transaction_read_only = on")
    op.execute(
        "DO $db$ BEGIN EXECUTE format('ALTER DATABASE %I OWNER TO uap_owner', "
        "current_database()); END $db$;"
    )
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute("ALTER SCHEMA public OWNER TO uap_owner")
    op.execute("ALTER TABLE public.alembic_version OWNER TO uap_owner")
    for schema in ("ingest", "core", "ops", "audit"):
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema} AUTHORIZATION uap_owner")
    op.execute("REVOKE ALL ON SCHEMA ingest, core, ops, audit FROM PUBLIC")


def downgrade() -> None:
    for schema in ("audit", "ops", "core", "ingest"):
        op.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    op.execute("GRANT CREATE ON SCHEMA public TO PUBLIC")
    op.execute("ALTER SCHEMA public OWNER TO CURRENT_USER")
    for role in reversed(NOLOGIN_ROLES + LOGIN_ROLES):
        op.execute(f"DROP ROLE IF EXISTS {role}")
