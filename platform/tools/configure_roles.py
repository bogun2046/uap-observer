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
    "uap_public_reader": "UAP_PUBLIC_READER_PASSWORD",
    "uap_audit_reader": "UAP_AUDIT_READER_PASSWORD",
    "uap_backup": "UAP_BACKUP_PASSWORD",
}


def required_passwords(environ: dict[str, str]) -> dict[str, str]:
    missing = [variable for variable in ROLE_PASSWORDS.values() if not environ.get(variable)]
    if missing:
        raise RuntimeError(f"missing role password variables: {', '.join(sorted(missing))}")
    return {role: environ[variable] for role, variable in ROLE_PASSWORDS.items()}


def configure() -> None:
    settings = Settings()  # type: ignore[call-arg]
    passwords = required_passwords(dict(os.environ))
    with psycopg.connect(settings.psycopg_database_url) as connection:
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
