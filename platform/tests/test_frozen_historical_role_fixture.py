from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest

from tools import configure_roles, wp10_3_runtime_probe
from tools import frozen_historical_role_fixture as fixture
from tools.wp10_6_migration_probe import create_database, database_url, libpq_url, run_alembic

FIXTURE_ID = "12345_1"
NAME, MARKER = fixture.identity(FIXTURE_ID, "3")
REVISION = "0022_wp10_claim_search_projection"


def connection_for(
    *,
    revision: str = REVISION,
    name: str = NAME,
    marker: str | None = MARKER,
    inventory: list[tuple[str, str | None]] | None = None,
) -> MagicMock:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    membership = (False, False, True, False)

    def execute(query: object, _parameters: object = None) -> MagicMock:
        nonlocal membership
        result = MagicMock()
        if query == configure_roles.MIGRATOR_MEMBERSHIP_QUERY:
            result.fetchone.return_value = membership
        elif query == "SELECT version_num FROM public.alembic_version":
            result.fetchone.return_value = (revision,)
        elif query == "SELECT to_regclass('public.alembic_version')":
            result.fetchone.return_value = (None,)
        elif isinstance(query, str) and "WHERE datname=current_database()" in query:
            result.fetchone.return_value = (name, marker)
        elif isinstance(query, str) and "WHERE NOT datistemplate" in query:
            result.fetchall.return_value = inventory if inventory is not None else [(name, marker)]
        elif isinstance(query, str) and query.startswith("SELECT NOT EXISTS"):
            result.fetchone.return_value = (True,)
        elif isinstance(query, str) and "WITH INHERIT TRUE" in query:
            membership = (False, True, True, False)
        elif isinstance(query, str) and "WITH INHERIT FALSE" in query:
            membership = (False, False, True, False)
        return result

    connection.execute.side_effect = execute
    return connection


@pytest.mark.parametrize("fail_runtime", [False, True])
def test_0022_registered_fixture_restores_hardening_even_on_failure(
    monkeypatch: pytest.MonkeyPatch, fail_runtime: bool
) -> None:
    connection = connection_for()
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: connection)

    def run() -> None:
        with fixture.configure_frozen_historical_role_fixture(
            "url", FIXTURE_ID, "WP10.3-runtime"
        ) as report:
            assert report == {
                "mode": "frozen-historical",
                "revision": REVISION,
                "rolinherit": False,
                "owner_membership_inherit": True,
                "membership_set": True,
                "membership_admin": False,
                "disposable": True,
                "database": NAME,
            }
            if fail_runtime:
                raise RuntimeError("runtime failure")

    if fail_runtime:
        with pytest.raises(RuntimeError, match="runtime failure"):
            run()
    else:
        run()
    queries = [call.args[0] for call in connection.execute.call_args_list]
    assert (
        queries.count("GRANT uap_owner TO uap_migrator WITH INHERIT TRUE, SET TRUE, ADMIN FALSE")
        == 1
    )
    assert queries[-2] == "ALTER ROLE uap_migrator NOLOGIN NOINHERIT"
    assert connection.execute(configure_roles.MIGRATOR_MEMBERSHIP_QUERY).fetchone() == (
        False,
        False,
        True,
        False,
    )


@pytest.mark.parametrize(
    "revision",
    [
        "0023_wp10_api_read_indexes",
        "0024_wp10_admin_replay",
        "0025_v12_editorial_foundation",
        "0037_v133_deferred_integrity_trigger_security",
        "head",
    ],
)
def test_wrong_revision_cannot_apply_historical_inheritance(revision: str) -> None:
    connection = connection_for(revision=revision)
    with pytest.raises(RuntimeError, match="exact authorized revision"):
        fixture.verify_scope(connection, "url", FIXTURE_ID, "WP10.3-runtime")
    assert all(str(call.args[0]).startswith("SELECT") for call in connection.execute.call_args_list)


@pytest.mark.parametrize(
    "step", ["WP10.4-runtime", "WP10.5-runtime", "current-head", "WP10.3-migration"]
)
def test_other_stages_are_not_authorized(step: str) -> None:
    connection = connection_for()
    with pytest.raises(RuntimeError, match="not authorized"):
        fixture.verify_scope(connection, "url", FIXTURE_ID, step)
    connection.execute.assert_not_called()


@pytest.mark.parametrize("name,marker", [("production", MARKER), (NAME, None), (NAME, "untrusted")])
def test_unregistered_or_non_disposable_database_is_rejected(name: str, marker: str | None) -> None:
    connection = connection_for(name=name, marker=marker)
    with pytest.raises(RuntimeError, match="non-disposable"):
        fixture.verify_scope(connection, "url", FIXTURE_ID, "WP10.3-runtime")


def test_cluster_with_product_database_is_rejected() -> None:
    connection = connection_for(inventory=[(NAME, MARKER), ("product", None)])
    with pytest.raises(RuntimeError, match="isolated frozen-only cluster"):
        fixture.verify_scope(connection, "url", FIXTURE_ID, "WP10.3-runtime")


def test_registered_database_at_current_head_also_rejects_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_identity = fixture.identity(FIXTURE_ID, "4")
    connection = connection_for(inventory=[(NAME, MARKER), other_identity])
    other = connection_for(revision="0037_v133_deferred_integrity_trigger_security")
    monkeypatch.setattr(psycopg, "connect", lambda *_: other)
    with pytest.raises(RuntimeError, match="non-historical revision"):
        fixture.verify_scope(
            connection, "postgresql://admin@localhost/postgres", FIXTURE_ID, "WP10.3-runtime"
        )


def test_maintenance_database_with_migrations_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = connection_for(inventory=[(NAME, MARKER), ("postgres", None)])
    maintenance = MagicMock()
    maintenance.__enter__.return_value = maintenance
    maintenance.execute.return_value.fetchone.return_value = ("alembic_version",)
    monkeypatch.setattr(psycopg, "connect", lambda *_: maintenance)
    with pytest.raises(RuntimeError, match="maintenance database"):
        fixture.verify_scope(
            connection, "postgresql://admin@localhost/postgres", FIXTURE_ID, "WP10.3-runtime"
        )


@pytest.mark.parametrize("fixture_id,stage", [("", "3"), ("production", "3"), (FIXTURE_ID, "head")])
def test_invalid_fixture_identity_is_rejected(fixture_id: str, stage: str) -> None:
    with pytest.raises(RuntimeError, match="invalid frozen fixture identity"):
        fixture.identity(fixture_id, stage)


def test_registration_requires_empty_named_database(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = connection_for()
    monkeypatch.setattr(psycopg, "connect", lambda *_: connection)
    fixture.register_disposable_database("url", FIXTURE_ID, "3")
    assert not isinstance(connection.execute.call_args.args[0], str)
    connection.execute.side_effect = None
    connection.execute.return_value.fetchone.side_effect = [(NAME, None), (False,)]
    with pytest.raises(RuntimeError, match="fresh empty"):
        fixture.register_disposable_database("url", FIXTURE_ID, "3")


def test_hook_only_enters_explicitly_enabled_wp10_3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UAP_WP10_FROZEN_FIXTURE_ID", raising=False)
    with fixture.historical_step_fixture("WP10.3-runtime") as report:
        assert report is None
    monkeypatch.setenv("UAP_WP10_FROZEN_FIXTURE_ID", FIXTURE_ID)
    for step in ("WP10.4-runtime", "WP10.5-runtime", "WP10.3-migration"):
        with fixture.historical_step_fixture(step) as report:
            assert report is None


def test_cli_registers_only_an_explicit_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UAP_DATABASE_URL", "url")
    monkeypatch.setattr(
        "sys.argv", ["fixture", "--register-stage", "3", "--fixture-id", FIXTURE_ID]
    )
    register = MagicMock()
    monkeypatch.setattr(fixture, "register_disposable_database", register)
    fixture.main()
    register.assert_called_once_with("url", FIXTURE_ID, "3")


def test_workflow_separates_role_clusters_and_evidence() -> None:
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/platform-ci.yml"
    ).read_text()
    historical = workflow.split("- name: Provision isolated G10 database topology", 1)[1].split(
        "- name: Validate WP10.6-B", 1
    )[0]
    assert "--network-alias frozen-postgres" in historical
    assert "data:uid=$frozen_pg_uid,gid=$frozen_pg_gid,mode=0700" in historical
    assert "POSTGRES_DB=postgres" in historical
    assert '--register-stage "$stage" --fixture-id "$fixture_id"' in historical
    runtime = workflow.split("- name: Run WP10 WP3-through-WP10.5 runtime probe", 1)[1].split(
        "- name: Upload WP10 runtime evidence", 1
    )[0]
    assert "@postgres:5432" not in runtime
    assert "UAP_WP10_FROZEN_FIXTURE_ID" in runtime
    head = workflow.split("- name: Run current-head hardened membership runtime", 1)[1].split(
        "- name: Upload current-head hardened runtime evidence", 1
    )[0]
    assert "@postgres:5432" in head
    assert "frozen-postgres" not in head
    assert "UAP_WP10_FROZEN_FIXTURE_ID" not in head
    assert 'chmod 1777 "$HEAD_EVIDENCE_HOST"' in head
    assert "hardened-head-runtime-evidence.json" in head
    assert "name: hardened-head-runtime-evidence" in workflow


def test_real_0022_without_fixture_reproduces_42501(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_url = os.environ.get("UAP_FROZEN_NEGATIVE_ADMIN_URL")
    if not admin_url:
        pytest.skip("requires separate disposable negative-test PostgreSQL instance")
    # This opt-in test runs the unchanged real WP10.3 probe against real 0022.
    name = f"uap_wp10_3_{uuid.uuid4().int % 10**12}_99"
    create_database(admin_url, name)
    url = database_url(admin_url, name)
    result = run_alembic(url, "upgrade", REVISION, role=None)
    assert result.exit_code == 0
    monkeypatch.setenv("UAP_DATABASE_URL", url)
    configure_roles.configure()
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="table claims") as error:
        wp10_3_runtime_probe.run(url)
    assert error.value.sqlstate == "42501"
    with psycopg.connect(libpq_url(url)) as admin:
        assert admin.execute(configure_roles.MIGRATOR_MEMBERSHIP_QUERY).fetchone() == (
            False,
            False,
            True,
            False,
        )
