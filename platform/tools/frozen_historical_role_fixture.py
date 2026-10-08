"""Test-only owner inheritance for an explicitly registered frozen WP10.3 fixture.

PostgreSQL roles are cluster-wide. Refuse clusters containing anything except
registered frozen fixtures and an empty maintenance database; current product
databases must use a different PostgreSQL instance. Never call from bootstrap.
"""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from tools import configure_roles
from tools.wp10_stage_revisions import database_url_for_step, libpq_url

ALLOWED_STEPS = {"WP10.3-runtime": ("3", "0022_wp10_claim_search_projection")}
STAGES = frozenset({"legacy", "legacy_probe", "1", "2", "3", "4", "5", "regression"})
MARKER_PREFIX = "uap-frozen-wp10:v1"


def identity(fixture_id: str, stage: str) -> tuple[str, str]:
    if re.fullmatch(r"[0-9]+_[0-9]+", fixture_id) is None or stage not in STAGES:
        raise RuntimeError("invalid frozen fixture identity")
    return f"uap_wp10_{stage}_{fixture_id}", f"{MARKER_PREFIX}:{fixture_id}:{stage}"


def database_identity(connection: psycopg.Connection[Any]) -> tuple[str, str | None]:
    row = connection.execute(
        "SELECT datname,shobj_description(oid,'pg_database') FROM pg_database "
        "WHERE datname=current_database()"
    ).fetchone()
    if row is None:
        raise RuntimeError("missing database identity")
    return str(row[0]), row[1]


def register_disposable_database(url: str, fixture_id: str, stage: str) -> None:
    """Register only a newly created, empty, explicitly named fixture database."""
    expected_name, marker = identity(fixture_id, stage)
    with psycopg.connect(libpq_url(url)) as connection:
        name, _ = database_identity(connection)
        if name != expected_name:
            raise RuntimeError("frozen fixture name mismatch")
        empty = connection.execute(
            "SELECT NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
            "AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S'))"
        ).fetchone()
        if empty != (True,):
            raise RuntimeError("registration requires a fresh empty disposable database")
        connection.execute(
            sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                sql.Identifier(name), sql.Literal(marker)
            )
        )


def verify_scope(
    connection: psycopg.Connection[Any], url: str, fixture_id: str, step_id: str
) -> str:
    allowed = ALLOWED_STEPS.get(step_id)
    if allowed is None:
        raise RuntimeError("historical inheritance is not authorized for this stage")
    stage, revision = allowed
    expected_name, marker = identity(fixture_id, stage)
    if database_identity(connection) != (expected_name, marker):
        raise RuntimeError("unregistered or non-disposable frozen database")
    if connection.execute("SELECT version_num FROM public.alembic_version").fetchone() != (
        revision,
    ):
        raise RuntimeError("historical fixture requires exact authorized revision")
    rows = connection.execute(
        "SELECT datname,shobj_description(oid,'pg_database') FROM pg_database "
        "WHERE NOT datistemplate AND datallowconn"
    ).fetchall()
    registered = {identity(fixture_id, candidate) for candidate in STAGES}
    for name, comment in rows:
        if name == "postgres":
            with psycopg.connect(make_conninfo(libpq_url(url), dbname="postgres")) as maintenance:
                if maintenance.execute(
                    "SELECT to_regclass('public.alembic_version')"
                ).fetchone() != (None,):
                    raise RuntimeError("maintenance database contains product migrations")
        elif (name, comment) not in registered:
            raise RuntimeError("historical fixture requires an isolated frozen-only cluster")
        elif name != expected_name:
            with psycopg.connect(make_conninfo(libpq_url(url), dbname=name)) as other:
                row = other.execute("SELECT version_num FROM public.alembic_version").fetchone()
                if (
                    row is None
                    or re.fullmatch(r"00(?:0[1-9]|1[0-9]|2[0-4])_.+", str(row[0])) is None
                ):
                    raise RuntimeError("frozen cluster contains a non-historical revision")
    configure_roles.verify_migrator_membership(
        connection.execute(configure_roles.MIGRATOR_MEMBERSHIP_QUERY).fetchone()
    )
    return revision


@contextmanager
def configure_frozen_historical_role_fixture(
    url: str, fixture_id: str, step_id: str
) -> Iterator[dict[str, object]]:
    with psycopg.connect(libpq_url(url), autocommit=True) as connection:
        revision = verify_scope(connection, url, fixture_id, step_id)
        try:
            # The historical role remains NOINHERIT; PG16's explicit membership
            # edge supplied inherited owner privileges independently of that bit.
            connection.execute(
                "GRANT uap_owner TO uap_migrator WITH INHERIT TRUE, SET TRUE, ADMIN FALSE"
            )
            if connection.execute(configure_roles.MIGRATOR_MEMBERSHIP_QUERY).fetchone() != (
                False,
                True,
                True,
                False,
            ):
                raise RuntimeError("historical role contract mismatch")
            yield {
                "mode": "frozen-historical",
                "revision": revision,
                "rolinherit": False,
                "owner_membership_inherit": True,
                "membership_set": True,
                "membership_admin": False,
                "disposable": True,
                "database": identity(fixture_id, "3")[0],
            }
        finally:
            connection.execute(
                "GRANT uap_owner TO uap_migrator WITH INHERIT FALSE, SET TRUE, ADMIN FALSE"
            )
            connection.execute("ALTER ROLE uap_migrator NOLOGIN NOINHERIT")
            configure_roles.verify_migrator_membership(
                connection.execute(configure_roles.MIGRATOR_MEMBERSHIP_QUERY).fetchone()
            )


@contextmanager
def historical_step_fixture(step_id: str) -> Iterator[dict[str, object] | None]:
    fixture_id = os.environ.get("UAP_WP10_FROZEN_FIXTURE_ID")
    if fixture_id is None or step_id not in ALLOWED_STEPS:
        yield None
        return
    with configure_frozen_historical_role_fixture(
        database_url_for_step(step_id), fixture_id, step_id
    ) as report:
        yield report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register-stage", choices=sorted(STAGES), required=True)
    parser.add_argument("--fixture-id", required=True)
    args = parser.parse_args()
    register_disposable_database(
        os.environ["UAP_DATABASE_URL"], args.fixture_id, args.register_stage
    )


if __name__ == "__main__":
    main()
