"""Set runtime database-role passwords without placing secrets in migrations."""

from __future__ import annotations

import os

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


if __name__ == "__main__":
    configure()
