from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest
from alembic.script import ScriptDirectory
from psycopg.conninfo import conninfo_to_dict

from tools import configure_roles, wp10_2_runtime_probe, wp10_4_runtime_probe, wp10_5_runtime_probe
from tools import frozen_historical_role_fixture as historical
from tools import hardened_head_runtime_probe as probe


def test_migrator_connection_uses_distinct_role_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UAP_MIGRATOR_PASSWORD", "migrator-only-password")
    monkeypatch.setenv("UAP_PUBLISHER_PASSWORD", "different-publisher-password")
    observed: list[dict[str, str | int | None]] = []
    connection = MagicMock()

    def connect(dsn: str) -> MagicMock:
        observed.append(conninfo_to_dict(dsn))
        return connection

    monkeypatch.setattr(psycopg, "connect", connect)
    assert probe.connect_migrator("postgresql+psycopg://admin:admin-password@db/test") is connection
    assert observed == [{
        "user": "uap_migrator", "password": "migrator-only-password",
        "dbname": "test", "host": "db",
    }]


def test_migrator_connection_fails_closed_without_own_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UAP_MIGRATOR_PASSWORD", raising=False)
    monkeypatch.setenv("UAP_PUBLISHER_PASSWORD", "publisher-must-not-be-used")

    def forbidden_connect(*args: object, **kwargs: object) -> None:
        pytest.fail("missing migrator credential attempted database connection")

    monkeypatch.setattr(psycopg, "connect", forbidden_connect)
    with pytest.raises(RuntimeError, match="UAP_MIGRATOR_PASSWORD"):
        probe.connect_migrator("postgresql+psycopg://admin:admin-password@db/test")


def test_public_api_receives_explicit_distinct_role_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UAP_PUBLISHER_PASSWORD", "publisher-only-password")
    monkeypatch.setenv("UAP_PUBLIC_READER_PASSWORD", "different-reader-password")
    observed: list[object] = []

    def run(admin: str, publisher: str, reader: str) -> None:
        observed.extend([admin, conninfo_to_dict(publisher), conninfo_to_dict(reader)])

    monkeypatch.setattr(wp10_4_runtime_probe, "run", run)
    url = "postgresql+psycopg://admin:admin-password@db/test"
    probe.run_public_api(url)
    assert observed == [url,
        {"user": "uap_publisher", "password": "publisher-only-password",
         "dbname": "test", "host": "db"},
        {"user": "uap_public_reader", "password": "different-reader-password",
         "dbname": "test", "host": "db"},
    ]


def test_current_head_requires_exact_formal_successor(monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = MagicMock()
    monkeypatch.setattr(ScriptDirectory, "from_config", lambda _: scripts)
    scripts.get_current_head.return_value = probe.REQUIRED_HEAD
    assert probe.current_head() == "0038_v133_full_rebuild_compaction"
    scripts.get_current_head.return_value = "0024_wp10_admin_replay"
    with pytest.raises(RuntimeError, match="unexpected current Alembic head"):
        probe.current_head()


@pytest.mark.parametrize("failed_stage", [None, "bootstrap", "upgrade"])
def test_fresh_fixture_runs_formal_migrations_and_closes_login(
    monkeypatch: pytest.MonkeyPatch, failed_stage: str | None
) -> None:
    monkeypatch.setenv("UAP_DATABASE_URL", "original-url")
    calls: list[object] = []
    monkeypatch.setattr(probe, "create_database", lambda *args: calls.append(("create", args)))
    monkeypatch.setattr(probe, "database_url", lambda *_: "fresh-url")
    monkeypatch.setattr(probe, "current_head", lambda: probe.REQUIRED_HEAD)
    monkeypatch.setattr(configure_roles, "configure", lambda: calls.append("reconcile"))
    monkeypatch.setattr(
        probe, "set_migrator_login", lambda _, state: calls.append(("login", state))
    )

    def migrate(_url: str, _action: str, target: str, **kwargs: object) -> MagicMock:
        calls.append(("migration", target, kwargs))
        fail = failed_stage == ("bootstrap" if target.startswith("0001") else "upgrade")
        return MagicMock(exit_code=int(fail))

    monkeypatch.setattr(probe, "run_alembic", migrate)
    if failed_stage:
        with pytest.raises(RuntimeError, match="failed"):
            probe.fresh_database("admin", "membership_test")
    else:
        assert probe.fresh_database("admin", "membership_test") == "fresh-url"
        assert calls == [
            ("create", ("admin", "membership_test")),
            ("migration", "0001_roles_and_schemas", {"role": None}),
            "reconcile",
            ("login", True),
            ("migration", probe.REQUIRED_HEAD, {}),
            ("login", False),
        ]
    if failed_stage != "bootstrap":
        assert calls[-1] == ("login", False)
    import os

    assert os.environ["UAP_DATABASE_URL"] == "original-url"


def test_projection_privilege_inventory_is_read_only_and_complete() -> None:
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = (False,)
    observed = probe.privilege_snapshot(connection)
    assert len(observed) == 45
    assert not any(observed.values())
    assert all(
        call.args[0].startswith("SELECT has_table_privilege")
        for call in connection.execute.call_args_list
    )


@pytest.mark.parametrize("failure", [None, "publication", "public_api", "admin_api"])
def test_head_runtime_emits_separate_evidence_and_closes_login_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str | None
) -> None:
    def forbidden_fixture(*args: object, **kwargs: object) -> None:
        pytest.fail("current-head path attempted historical inheritance")

    monkeypatch.setattr(historical, "configure_frozen_historical_role_fixture", forbidden_fixture)
    monkeypatch.setattr(probe, "current_head", lambda: probe.REQUIRED_HEAD)
    fixtures: list[str] = []
    login: list[bool] = []

    def fresh(_admin: str, name: str) -> str:
        fixtures.append(name)
        return name

    monkeypatch.setattr(probe, "fresh_database", fresh)
    monkeypatch.setattr(
        probe, "verify_database", lambda _: {"privileges": {"claims.SELECT": False}}
    )
    monkeypatch.setattr(probe, "set_migrator_login", lambda _, value: login.append(value))
    connection = MagicMock()
    connection.info.dsn = "role-url"
    connection.execute.return_value.fetchone.return_value = (False,)
    connection.__enter__.return_value = connection
    monkeypatch.setattr(psycopg, "connect", lambda *_: connection)
    monkeypatch.setattr(probe, "connect_role", lambda *_: connection)
    monkeypatch.setattr(configure_roles, "verify_migrator_membership", lambda _: None)

    def operation(kind: str) -> dict[str, str]:
        if failure == kind:
            raise RuntimeError("credential-bearing text must never be serialized")
        return {"status": "PASS"}

    monkeypatch.setattr(probe, "publication_and_rebuild", lambda _: operation("publication"))
    monkeypatch.setattr(wp10_2_runtime_probe, "run", lambda _: operation("publisher"))
    monkeypatch.setattr(probe, "run_public_api", lambda _: operation("public_api"))
    monkeypatch.setattr(wp10_5_runtime_probe, "run", lambda _: operation("admin_api"))
    path = tmp_path / "hardened-head-runtime-evidence.json"
    if failure:
        with pytest.raises(RuntimeError, match="credential-bearing"):
            probe.run("admin", path)
    else:
        assert probe.run("admin", path)["status"] == "passed"
    evidence = json.loads(path.read_text())
    assert evidence["schema"] == "hardened-head-runtime-evidence.v1"
    assert evidence["head"] == probe.REQUIRED_HEAD
    assert evidence["role_fixture"] == {
        "mode": "current-hardened",
        "revision": probe.REQUIRED_HEAD,
        "rolinherit": False,
        "membership_inherit": False,
        "membership_set": True,
        "membership_admin": False,
    }
    assert evidence["historical_stage_contract_modified"] is False
    assert "step_counts" not in evidence
    assert evidence["final_migrator_nologin"] is True
    assert "credential-bearing" not in path.read_text()
    assert login[-1] is False
    assert all(name.startswith("membership_head_") for name in fixtures)


def test_entrypoint_has_no_trigger_patch_or_inheritance_restore() -> None:
    text = Path(probe.__file__).read_text()
    for forbidden in (
        "CREATE OR REPLACE",
        "_sql(upgrade",
        "WITH INHERIT TRUE",
        "SET SESSION AUTHORIZATION",
        "FROZEN_STEPS =",
        "STEP_REQUIRED_REVISION =",
        "MAIN_DB_ADVANCE_BEFORE =",
        "frozen_historical_role_fixture",
    ):
        assert forbidden not in text


def test_head_runtime_rejects_inherited_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.side_effect = [
        (probe.REQUIRED_HEAD,),
        (False, True, True, False),
    ]
    monkeypatch.setattr(psycopg, "connect", lambda *_: connection)
    with pytest.raises(RuntimeError, match="not hardened"):
        probe.verify_database("url")


def test_cli_reports_independent_head_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UAP_DATABASE_URL", "admin-url")
    monkeypatch.setattr("sys.argv", ["probe", "--evidence-out", "head.json"])
    calls: list[tuple[str, Path]] = []

    def run(url: str, path: Path) -> dict[str, str]:
        calls.append((url, path))
        return {"status": "passed", "head": probe.REQUIRED_HEAD}

    monkeypatch.setattr(probe, "run", run)
    probe.main()
    assert calls == [("admin-url", Path("head.json"))]
