"""Exercise WP10.5 0024 replay-function upgrade/downgrade/upgrade."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path
from typing import Any

import psycopg

from tools.wp10_2_migration_probe import (
    create_database,
    database_url,
    drop_database,
    libpq_url,
    run_alembic,
    scalar,
)

REVISION = "0024_wp10_admin_replay"
PARENT = "0023_wp10_api_read_indexes"
EVIDENCE: list[dict[str, Any]] = []


def function_present(connection: psycopg.Connection[object]) -> bool:
    return bool(
        scalar(
            connection,
            """
            SELECT EXISTS (
                SELECT 1
                  FROM pg_proc
                  JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
                 WHERE pg_namespace.nspname = 'audit'
                   AND pg_proc.proname = 'requeue_publication_event'
            )
            """,
        )
    )


def execute_grant(connection: psycopg.Connection[object], role: str) -> bool:
    value = scalar(
        connection,
        """
        SELECT has_function_privilege(
            %s,
            'audit.requeue_publication_event(uuid, text)',
            'EXECUTE'
        )
        """,
        role,
    )
    return bool(value)


def run(admin_url: str) -> None:
    name = f"uap_wp10_5_replay_{uuid.uuid4().hex[:10]}"
    create_database(admin_url, name)
    isolated = database_url(admin_url, name)
    try:
        run_alembic(isolated, "upgrade", PARENT)
        with psycopg.connect(libpq_url(isolated), autocommit=True) as connection:
            if function_present(connection):
                raise RuntimeError("replay function existed before 0024")
            parent_version = scalar(connection, "SELECT version_num FROM alembic_version")

        run_alembic(isolated, "upgrade", REVISION)
        with psycopg.connect(libpq_url(isolated), autocommit=True) as connection:
            if not function_present(connection):
                raise RuntimeError("0024 did not create audit.requeue_publication_event")
            if not execute_grant(connection, "uap_api"):
                raise RuntimeError("uap_api lacks EXECUTE on requeue_publication_event")
            forbidden = [
                role
                for role in (
                    "uap_publisher",
                    "uap_worker",
                    "uap_public_reader",
                    "uap_scheduler",
                )
                if execute_grant(connection, role)
            ]
            if forbidden:
                raise RuntimeError("replay execute leaked to " + ",".join(forbidden))
            version = str(scalar(connection, "SHOW server_version"))
            upgraded = scalar(connection, "SELECT version_num FROM alembic_version")

        run_alembic(isolated, "downgrade", PARENT)
        with psycopg.connect(libpq_url(isolated), autocommit=True) as connection:
            if function_present(connection):
                raise RuntimeError("0024 downgrade left replay function")
            if scalar(connection, "SELECT version_num FROM alembic_version") != parent_version:
                raise RuntimeError("0024 downgrade did not restore 0023")

        run_alembic(isolated, "upgrade", REVISION)
        with psycopg.connect(libpq_url(isolated), autocommit=True) as connection:
            if not function_present(connection):
                raise RuntimeError("0024 re-upgrade did not restore replay function")
            restored = scalar(connection, "SELECT version_num FROM alembic_version")

        EVIDENCE.append(
            {
                "case": "0023_0024_0023_0024_roundtrip",
                "status": "passed",
                "postgresql_version": version,
                "parent_version": parent_version,
                "upgraded_version": upgraded,
                "restored_version": restored,
                "uap_api_execute": True,
                "forbidden_execute": [],
            }
        )
    finally:
        drop_database(admin_url, name)


def _admin_url(explicit: str | None) -> str:
    url = explicit or os.environ.get("UAP_WP10_5_ADMIN_URL")
    if not url:
        raise SystemExit("admin URL missing")
    return url


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-url")
    parser.add_argument("--evidence-out", type=Path)
    args = parser.parse_args()
    status = "passed"
    try:
        run(_admin_url(args.admin_url))
    except Exception:
        status = "failed"
        raise
    finally:
        if args.evidence_out:
            args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
            args.evidence_out.write_text(
                json.dumps(
                    {
                        "schema": "wp10.5-migration-evidence.v1",
                        "status": status,
                        "checks": EVIDENCE,
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )
    print("WP10.5 migration probe passed")


if __name__ == "__main__":
    main()
