"""Ensure the dedicated model-governance login exists before migrations."""

from __future__ import annotations

import psycopg
from psycopg import sql

from uap_platform.config import Settings


def ensure_role() -> None:
    settings = Settings()  # type: ignore[call-arg]
    with psycopg.connect(settings.psycopg_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = 'uap_model_governance'"
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOBYPASSRLS"
                    ).format(sql.Identifier("uap_model_governance"))
                )
            cursor.execute("ALTER ROLE uap_model_governance NOINHERIT")


if __name__ == "__main__":
    ensure_role()
