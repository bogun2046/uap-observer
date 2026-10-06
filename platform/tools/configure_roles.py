"""Set runtime database-role passwords without placing secrets in migrations."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

import psycopg
from psycopg import sql

from uap_platform.config import Settings

ROLE_PASSWORDS = {
    "uap_migrator": "UAP_MIGRATOR_PASSWORD",
    "uap_api": "UAP_API_PASSWORD",
    "uap_worker": "UAP_WORKER_PASSWORD",
    "uap_scheduler": "UAP_SCHEDULER_PASSWORD",
    "uap_publisher": "UAP_PUBLISHER_PASSWORD",
    "uap_model_governance": "UAP_MODEL_GOVERNANCE_PASSWORD",
    "uap_public_reader": "UAP_PUBLIC_READER_PASSWORD",
    "uap_audit_reader": "UAP_AUDIT_READER_PASSWORD",
    "uap_backup": "UAP_BACKUP_PASSWORD",
}


MIGRATOR_MEMBERSHIP_QUERY = """
SELECT member_role.rolinherit, membership.inherit_option,
       membership.set_option, membership.admin_option
FROM pg_auth_members AS membership
JOIN pg_roles AS granted_role ON granted_role.oid = membership.roleid
JOIN pg_roles AS member_role ON member_role.oid = membership.member
WHERE granted_role.rolname = 'uap_owner'
  AND member_role.rolname = 'uap_migrator'
"""


def verify_migrator_membership(row: Sequence[object] | None) -> None:
    """Require explicit owner elevation, never inherited owner privileges."""

    if row is None or tuple(row) != (False, False, True, False):
        raise RuntimeError("uap_migrator owner membership is not hardened")


def configure_migrator_membership(connection: psycopg.Connection[tuple[object, ...]]) -> None:
    """Reconcile PostgreSQL 16 membership options using administrator privileges."""

    with connection.cursor() as cursor:
        cursor.execute("ALTER ROLE uap_migrator NOINHERIT")
        cursor.execute(
            "GRANT uap_owner TO uap_migrator WITH INHERIT FALSE, SET TRUE, ADMIN FALSE"
        )
        cursor.execute(MIGRATOR_MEMBERSHIP_QUERY)
        verify_migrator_membership(cursor.fetchone())


ALEMBIC_VERSION_ACCESS_QUERY = """
SELECT (SELECT pg_get_userbyid(relowner) FROM pg_class
        WHERE oid = to_regclass('public.alembic_version')),
       has_table_privilege('uap_migrator', to_regclass('public.alembic_version'), 'SELECT'),
       has_table_privilege('uap_migrator', to_regclass('public.alembic_version'), 'INSERT'),
       has_table_privilege('uap_migrator', to_regclass('public.alembic_version'), 'UPDATE'),
       has_table_privilege('uap_migrator', to_regclass('public.alembic_version'), 'DELETE'),
       has_table_privilege('uap_migrator', to_regclass('public.alembic_version'), 'TRUNCATE'),
       has_table_privilege('uap_migrator', to_regclass('public.alembic_version'), 'REFERENCES'),
       has_table_privilege('uap_migrator', to_regclass('public.alembic_version'), 'TRIGGER'),
       has_schema_privilege('uap_migrator', 'public', 'USAGE')
"""


def verify_alembic_version_access(row: Sequence[object] | None) -> None:
    """Require only revision bookkeeping privileges without implicit owner access."""

    expected = ("uap_owner", True, True, True, True, False, False, False, True)
    if row is None or tuple(row) != expected:
        raise RuntimeError(
            "uap_migrator alembic version-table privilege contract is not hardened"
        )


def configure_alembic_version_access(connection: psycopg.Connection[tuple[object, ...]]) -> None:
    """Reconcile minimal version-table DML; never take over an unexpected owner."""

    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.alembic_version')")
        row = cursor.fetchone()
        if not row or row[0] is None:
            raise RuntimeError("public.alembic_version is missing")
        cursor.execute(
            "SELECT pg_get_userbyid(relowner) FROM pg_class "
            "WHERE oid = 'public.alembic_version'::regclass"
        )
        owner = cursor.fetchone()
        if owner is None or owner[0] != "uap_owner":
            raise RuntimeError("public.alembic_version owner must be uap_owner")
        cursor.execute("GRANT USAGE ON SCHEMA public TO uap_migrator")
        cursor.execute("REVOKE ALL PRIVILEGES ON TABLE public.alembic_version FROM uap_migrator")
        cursor.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.alembic_version TO uap_migrator"
        )
        cursor.execute(ALEMBIC_VERSION_ACCESS_QUERY)
        verify_alembic_version_access(cursor.fetchone())


def required_passwords(environ: dict[str, str]) -> dict[str, str]:
    missing = [variable for variable in ROLE_PASSWORDS.values() if not environ.get(variable)]
    if missing:
        raise RuntimeError(f"missing role password variables: {', '.join(sorted(missing))}")
    return {role: environ[variable] for role, variable in ROLE_PASSWORDS.items()}


def configure() -> None:
    settings = Settings()  # type: ignore[call-arg]
    passwords = required_passwords(dict(os.environ))
    with psycopg.connect(settings.psycopg_database_url) as connection:
        configure_migrator_membership(connection)
        configure_alembic_version_access(connection)
        with connection.cursor() as cursor:
            for role, password in passwords.items():
                cursor.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
    print(f"Configured {len(passwords)} database role credentials.")


def database_bootstrapped() -> bool:
    """Report whether this database has completed the administrator bootstrap revision."""

    settings = Settings()  # type: ignore[call-arg]
    with psycopg.connect(settings.psycopg_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('public.alembic_version')")
            row = cursor.fetchone()
            if not row or row[0] is None:
                return False
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                      FROM public.alembic_version
                     WHERE version_num IN (
                         '0001_roles_and_schemas',
                         '0002_authoritative_schema',
                         '0003_permissions_and_guards',
                         '0004_g3_semantic_repairs'
                     )
                )
                """
            )
            revision_row = cursor.fetchone()
    return bool(revision_row and revision_row[0])


def set_migrator_login(enabled: bool) -> None:
    """Temporarily open or close the privileged migration login."""

    settings = Settings()  # type: ignore[call-arg]
    state = sql.SQL("LOGIN") if enabled else sql.SQL("NOLOGIN")
    with psycopg.connect(settings.psycopg_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='uap_migrator')")
            row = cursor.fetchone()
            if not bool(row and row[0]):
                if enabled:
                    raise RuntimeError("uap_migrator does not exist")
                print("Migrator role absent; login already disabled.")
                return
            cursor.execute(sql.SQL("ALTER ROLE uap_migrator {}").format(state))
    print(f"Migrator login {'enabled' if enabled else 'disabled'}.")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        nargs="?",
        default="configure",
        choices=("configure", "database-bootstrapped", "enable-migrator", "disable-migrator"),
    )
    operation = parser.parse_args(argv).operation
    if operation == "configure":
        configure()
    elif operation == "database-bootstrapped":
        if not database_bootstrapped():
            raise SystemExit(3)
        print("Database bootstrap revision exists.")
    else:
        set_migrator_login(operation == "enable-migrator")


if __name__ == "__main__":
    main()
