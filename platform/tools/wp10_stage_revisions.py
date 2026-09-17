"""Stage-aware Alembic revision contract for G10-25.

The repository head is 0024. Historical WP10.1-WP10.4 probes must target their
own frozen revision, never the alias ``head``. Each stateful WP10 runtime stage
uses its own database, while the stage's migrator deployment window advances
that database through the required revision. DSN and role passwords stay in the
process environment.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict

PLATFORM_ROOT = Path(__file__).resolve().parents[1]

REVISION_0019 = "0019_manual_claims_binding"
REVISION_0020 = "0020_wp10_publication_contract"
REVISION_0021 = "0021_wp10_publisher_projection"
REVISION_0022 = "0022_wp10_claim_search_projection"
REVISION_0023 = "0023_wp10_api_read_indexes"
REVISION_0024 = "0024_wp10_admin_replay"

STAGE_CHAIN: tuple[str, ...] = (
    REVISION_0019,
    REVISION_0020,
    REVISION_0021,
    REVISION_0022,
    REVISION_0023,
    REVISION_0024,
)

PARENT_OF: dict[str, str] = {
    REVISION_0020: REVISION_0019,
    REVISION_0021: REVISION_0020,
    REVISION_0022: REVISION_0021,
    REVISION_0023: REVISION_0022,
    REVISION_0024: REVISION_0023,
}

ADVANCEABLE = frozenset(PARENT_OF)

WP3_TO_WP9_STEPS: tuple[str, ...] = (
    "WP3",
    "WP4",
    "WP5",
    "WP6",
    "WP7",
    "WP8",
    "WP9.1",
    "WP9.2",
    "WP9.3",
    "WP9.4",
    "WP9.5",
    "WP9.6",
)

LEGACY_DATABASE_ENV = "UAP_WP10_LEGACY_DATABASE_URL"
WP10_1_DATABASE_ENV = "UAP_WP10_1_DATABASE_URL"
WP10_2_DATABASE_ENV = "UAP_WP10_2_DATABASE_URL"
WP10_3_DATABASE_ENV = "UAP_WP10_3_DATABASE_URL"
WP10_4_DATABASE_ENV = "UAP_WP10_4_DATABASE_URL"
WP10_5_DATABASE_ENV = "UAP_WP10_5_DATABASE_URL"
REGRESSION_DATABASE_ENV = "UAP_WP10_2_REGRESSION_DATABASE_URL"

DATABASE_TOPOLOGY_ENVS: tuple[str, ...] = (
    LEGACY_DATABASE_ENV,
    WP10_1_DATABASE_ENV,
    WP10_2_DATABASE_ENV,
    WP10_3_DATABASE_ENV,
    WP10_4_DATABASE_ENV,
    WP10_5_DATABASE_ENV,
    REGRESSION_DATABASE_ENV,
)

# Revision the process-visible database must be at when the named step starts.
STEP_REQUIRED_REVISION: dict[str, str] = {
    **{step_id: REVISION_0019 for step_id in WP3_TO_WP9_STEPS},
    "WP10.1-migration": REVISION_0019,
    "WP10.1-runtime": REVISION_0020,
    "WP10.2-migration": REVISION_0020,
    "WP10.2-runtime": REVISION_0021,
    "WP10.3-migration": REVISION_0021,
    "WP10.3-runtime": REVISION_0022,
    "WP10.3-wp10.2-regression": REVISION_0021,
    "WP10.4-migration": REVISION_0022,
    "WP10.4-runtime": REVISION_0023,
    "WP10.5-migration": REVISION_0023,
    "WP10.5-runtime": REVISION_0024,
}

# Only these steps may open the migrator window on the main runtime database.
MAIN_DB_ADVANCE_BEFORE: dict[str, str] = {
    "WP10.1-runtime": REVISION_0020,
    "WP10.2-runtime": REVISION_0021,
    "WP10.3-runtime": REVISION_0022,
    "WP10.4-runtime": REVISION_0023,
    "WP10.5-runtime": REVISION_0024,
}

HEAD_ALIAS = "head"


class StageRevisionError(RuntimeError):
    """Fail-closed revision or migrator-window error. Message must not contain a DSN."""


def libpq_url(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def database_env_for_step(step_id: str) -> str:
    if step_id in WP3_TO_WP9_STEPS:
        return LEGACY_DATABASE_ENV
    if step_id.startswith("WP10.1"):
        return WP10_1_DATABASE_ENV
    if step_id.startswith("WP10.2"):
        return WP10_2_DATABASE_ENV
    if step_id.startswith("WP10.3"):
        if step_id == "WP10.3-wp10.2-regression":
            return REGRESSION_DATABASE_ENV
        return WP10_3_DATABASE_ENV
    if step_id.startswith("WP10.4"):
        return WP10_4_DATABASE_ENV
    if step_id.startswith("WP10.5"):
        return WP10_5_DATABASE_ENV
    raise StageRevisionError(f"unknown frozen step: {step_id}")


def _database_identity(value: str) -> tuple[str, str, str]:
    try:
        params = conninfo_to_dict(libpq_url(value))
    except Exception as exc:
        raise StageRevisionError("invalid database URL environment") from exc
    host = str(params.get("host") or params.get("hostaddr") or "")
    port = str(params.get("port") or "5432")
    database = str(params.get("dbname") or params.get("database") or "")
    if not host or not database:
        raise StageRevisionError("database URL is missing host or database")
    return host.lower(), port, database


def database_topology() -> dict[str, str]:
    values: dict[str, str] = {}
    for name in DATABASE_TOPOLOGY_ENVS:
        value = os.environ.get(name)
        if not value:
            raise StageRevisionError(f"missing required database topology environment: {name}")
        values[name] = value
    identities = [_database_identity(values[name]) for name in values]
    if len(set(identities)) != len(identities):
        raise StageRevisionError("database topology requires distinct databases")
    return values


def database_url_for_step(step_id: str) -> str:
    environment = database_topology()
    return environment[database_env_for_step(step_id)]


def required_revision(step_id: str) -> str:
    try:
        return STEP_REQUIRED_REVISION[step_id]
    except KeyError as exc:
        raise StageRevisionError(f"unknown frozen step: {step_id}") from exc


def read_alembic_version(database_url: str) -> str:
    with psycopg.connect(libpq_url(database_url)) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM public.alembic_version")
            row = cursor.fetchone()
    if row is None:
        raise StageRevisionError("alembic_version is empty")
    return str(row[0])


def read_migrator_flags(database_url: str) -> tuple[bool, bool]:
    with psycopg.connect(libpq_url(database_url)) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolcanlogin, rolinherit FROM pg_roles WHERE rolname = 'uap_migrator'"
            )
            row = cursor.fetchone()
    if row is None:
        raise StageRevisionError("uap_migrator role is missing")
    return bool(row[0]), bool(row[1])


def _alter_migrator_noinherit(database_url: str) -> None:
    with psycopg.connect(libpq_url(database_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("ALTER ROLE uap_migrator NOINHERIT")


def set_migrator_open(database_url: str) -> None:
    previous = os.environ.get("UAP_DATABASE_URL")
    os.environ["UAP_DATABASE_URL"] = database_url
    try:
        from tools.configure_roles import set_migrator_login

        set_migrator_login(True)
        _alter_migrator_noinherit(database_url)
    finally:
        if previous is None:
            os.environ.pop("UAP_DATABASE_URL", None)
        else:
            os.environ["UAP_DATABASE_URL"] = previous


def set_migrator_closed(database_url: str) -> None:
    previous = os.environ.get("UAP_DATABASE_URL")
    os.environ["UAP_DATABASE_URL"] = database_url
    try:
        from tools.configure_roles import set_migrator_login

        set_migrator_login(False)
        _alter_migrator_noinherit(database_url)
    finally:
        if previous is None:
            os.environ.pop("UAP_DATABASE_URL", None)
        else:
            os.environ["UAP_DATABASE_URL"] = previous
    login, inherit = read_migrator_flags(database_url)
    if login or inherit:
        raise StageRevisionError("uap_migrator was not restored to NOLOGIN/NOINHERIT")


def run_explicit_upgrade(target: str) -> None:
    if target == HEAD_ALIAS or target.lower().endswith("/head"):
        raise StageRevisionError("refusing repository head alias")
    if target not in ADVANCEABLE:
        raise StageRevisionError("upgrade target is not a frozen WP10 stage revision")
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "-x", "role=migrator", "upgrade", target],
        cwd=str(PLATFORM_ROOT),
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        details = "\n".join(
            line
            for line in (result.stdout + result.stderr).splitlines()
            if "postgresql" not in line.lower()
        )
        raise StageRevisionError(f"explicit upgrade to {target} failed\n{details[-800:]}")


def _closed_report(database_url: str) -> dict[str, bool]:
    login, inherit = read_migrator_flags(database_url)
    return {"migrator_login": login, "migrator_inherit": inherit}


def _ensure_closed(database_url: str) -> None:
    login, inherit = read_migrator_flags(database_url)
    if login or inherit:
        set_migrator_closed(database_url)


def _advance(database_url: str, target: str) -> dict[str, Any]:
    parent = PARENT_OF[target]
    before = read_alembic_version(database_url)
    opened = False
    try:
        if before == target:
            _ensure_closed(database_url)
            return {
                "status": "passed",
                "advanced": False,
                "before": before,
                "after": before,
                "target": target,
                "migrator": _closed_report(database_url),
            }
        if before != parent:
            raise StageRevisionError(
                f"revision mismatch: expected {parent} before advancing to {target}"
            )
        set_migrator_open(database_url)
        opened = True
        previous_database_url = os.environ.get("UAP_DATABASE_URL")
        os.environ["UAP_DATABASE_URL"] = database_url
        try:
            run_explicit_upgrade(target)
        finally:
            if previous_database_url is None:
                os.environ.pop("UAP_DATABASE_URL", None)
            else:
                os.environ["UAP_DATABASE_URL"] = previous_database_url
        after = read_alembic_version(database_url)
        if after != target:
            raise StageRevisionError(f"revision mismatch after upgrade: expected {target}")
        return {
            "status": "passed",
            "advanced": True,
            "before": before,
            "after": after,
            "target": target,
            "migrator": {"window": "closed-in-finally"},
        }
    finally:
        if opened:
            set_migrator_closed(database_url)
        else:
            _ensure_closed(database_url)


def _verify(database_url: str, expected: str) -> dict[str, Any]:
    current = read_alembic_version(database_url)
    if current != expected:
        raise StageRevisionError(f"revision mismatch: expected {expected}")
    _ensure_closed(database_url)
    return {
        "status": "passed",
        "advanced": False,
        "before": current,
        "after": current,
        "target": expected,
        "migrator": _closed_report(database_url),
    }


def ensure_for_step(step_id: str) -> dict[str, Any]:
    """Bring the visible database to the frozen revision for ``step_id``.

    Returns a secret-free mapping. Never upgrades through ``head``.
    """

    payload: dict[str, Any] = {"step_id": step_id, "status": "failed"}
    database_url: str | None = None
    try:
        expected = required_revision(step_id)
        database_url = database_url_for_step(step_id)
        payload["target"] = expected
        if step_id in MAIN_DB_ADVANCE_BEFORE:
            result = _advance(database_url, MAIN_DB_ADVANCE_BEFORE[step_id])
        else:
            result = _verify(database_url, expected)
        result["step_id"] = step_id
        return result
    except Exception as exc:
        payload["detail"] = f"{type(exc).__name__}: {exc}"
        if database_url is not None:
            try:
                set_migrator_closed(database_url)
                payload["migrator"] = _closed_report(database_url)
            except Exception as close_exc:
                payload["migrator_close"] = f"{type(close_exc).__name__}"
        return payload


class LiveStageGate:
    """Default G10-25 stage controller used by the orchestrator."""

    def ensure(self, step_id: str) -> dict[str, Any]:
        return ensure_for_step(step_id)
