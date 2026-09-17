"""Unit tests for stage-aware G10-25 revision orchestration. No PostgreSQL."""

from __future__ import annotations

import os
import subprocess

import pytest

from tools import wp10_stage_revisions as stages
from tools.wp10_1_migration_probe import STAGE_REVISION
from tools.wp10_stage_revisions import (
    DATABASE_TOPOLOGY_ENVS,
    LEGACY_DATABASE_ENV,
    MAIN_DB_ADVANCE_BEFORE,
    REGRESSION_DATABASE_ENV,
    REVISION_0019,
    REVISION_0020,
    REVISION_0021,
    REVISION_0022,
    REVISION_0023,
    REVISION_0024,
    STAGE_CHAIN,
    STEP_REQUIRED_REVISION,
    WP10_1_DATABASE_ENV,
    WP10_2_DATABASE_ENV,
    WP10_3_DATABASE_ENV,
    WP10_4_DATABASE_ENV,
    WP10_5_DATABASE_ENV,
    StageRevisionError,
)


def test_frozen_runtime_advancement_order() -> None:
    assert STAGE_CHAIN == (
        REVISION_0019,
        REVISION_0020,
        REVISION_0021,
        REVISION_0022,
        REVISION_0023,
        REVISION_0024,
    )
    assert MAIN_DB_ADVANCE_BEFORE["WP10.1-runtime"] == REVISION_0020
    assert MAIN_DB_ADVANCE_BEFORE["WP10.2-runtime"] == REVISION_0021
    assert MAIN_DB_ADVANCE_BEFORE["WP10.3-runtime"] == REVISION_0022
    assert MAIN_DB_ADVANCE_BEFORE["WP10.4-runtime"] == REVISION_0023
    assert MAIN_DB_ADVANCE_BEFORE["WP10.5-runtime"] == REVISION_0024
    assert "WP10.1-migration" not in MAIN_DB_ADVANCE_BEFORE
    assert STEP_REQUIRED_REVISION["WP3"] == REVISION_0019
    assert STEP_REQUIRED_REVISION["WP9.6"] == REVISION_0019
    assert STEP_REQUIRED_REVISION["WP10.1-migration"] == REVISION_0019
    assert STAGE_REVISION == REVISION_0020


def test_refuses_repository_head_alias() -> None:
    with pytest.raises(StageRevisionError, match="head alias"):
        stages.run_explicit_upgrade("head")


def test_wp10_1_contract_stays_on_0020_when_repo_head_is_0024() -> None:
    assert STAGE_REVISION != REVISION_0024
    assert STAGE_REVISION == REVISION_0020


class _DbState:
    def __init__(self, current: str) -> None:
        self.current = current
        self.opens = 0
        self.closes = 0
        self.upgrades: list[str] = []


def _patch_db(monkeypatch: pytest.MonkeyPatch, current: str) -> _DbState:
    state = _DbState(current)

    def read_version(_url: str) -> str:
        return state.current

    def open_migrator(_url: str) -> None:
        state.opens += 1

    def close_migrator(_url: str) -> None:
        state.closes += 1

    def upgrade(target: str) -> None:
        state.upgrades.append(target)
        state.current = target

    def flags(_url: str) -> tuple[bool, bool]:
        return False, False

    monkeypatch.setattr(stages, "read_alembic_version", read_version)
    monkeypatch.setattr(stages, "set_migrator_open", open_migrator)
    monkeypatch.setattr(stages, "set_migrator_closed", close_migrator)
    monkeypatch.setattr(stages, "run_explicit_upgrade", upgrade)
    monkeypatch.setattr(stages, "read_migrator_flags", flags)
    monkeypatch.setenv(
        LEGACY_DATABASE_ENV, "postgresql://uap:stage-secret@localhost:5432/wp10-legacy"
    )
    monkeypatch.setenv(WP10_1_DATABASE_ENV, "postgresql://uap:stage-secret@localhost:5432/wp10-1")
    monkeypatch.setenv(WP10_2_DATABASE_ENV, "postgresql://uap:stage-secret@localhost:5432/wp10-2")
    monkeypatch.setenv(WP10_3_DATABASE_ENV, "postgresql://uap:stage-secret@localhost:5432/wp10-3")
    monkeypatch.setenv(WP10_4_DATABASE_ENV, "postgresql://uap:stage-secret@localhost:5432/wp10-4")
    monkeypatch.setenv(WP10_5_DATABASE_ENV, "postgresql://uap:stage-secret@localhost:5432/wp10-5")
    monkeypatch.setenv(
        REGRESSION_DATABASE_ENV,
        "postgresql://uap:stage-secret@localhost:5432/wp10-regression",
    )
    return state


def test_wp3_to_wp9_stay_on_0019(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _patch_db(monkeypatch, REVISION_0019)
    result = stages.ensure_for_step("WP3")
    assert result["status"] == "passed"
    assert result["advanced"] is False
    assert result["after"] == REVISION_0019
    assert state.upgrades == []
    assert state.opens == 0


def test_database_environment_mapping_is_frozen() -> None:
    assert DATABASE_TOPOLOGY_ENVS == (
        LEGACY_DATABASE_ENV,
        WP10_1_DATABASE_ENV,
        WP10_2_DATABASE_ENV,
        WP10_3_DATABASE_ENV,
        WP10_4_DATABASE_ENV,
        WP10_5_DATABASE_ENV,
        REGRESSION_DATABASE_ENV,
    )
    assert stages.database_env_for_step("WP3") == LEGACY_DATABASE_ENV
    assert stages.database_env_for_step("WP9.6") == LEGACY_DATABASE_ENV
    assert stages.database_env_for_step("WP10.1-migration") == WP10_1_DATABASE_ENV
    assert stages.database_env_for_step("WP10.2-runtime") == WP10_2_DATABASE_ENV
    assert stages.database_env_for_step("WP10.3-runtime") == WP10_3_DATABASE_ENV
    assert stages.database_env_for_step("WP10.4-runtime") == WP10_4_DATABASE_ENV
    assert stages.database_env_for_step("WP10.5-runtime") == WP10_5_DATABASE_ENV
    assert stages.database_env_for_step("WP10.3-wp10.2-regression") == REGRESSION_DATABASE_ENV


def test_database_topology_rejects_duplicate_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    topology = {
        LEGACY_DATABASE_ENV: "wp10-legacy",
        WP10_1_DATABASE_ENV: "wp10-same",
        WP10_2_DATABASE_ENV: "wp10-2",
        WP10_3_DATABASE_ENV: "wp10-3",
        WP10_4_DATABASE_ENV: "wp10-4",
        WP10_5_DATABASE_ENV: "wp10-5",
        REGRESSION_DATABASE_ENV: "wp10-other",
    }
    for environment, database in topology.items():
        monkeypatch.setenv(environment, f"postgresql://uap@localhost:5432/{database}")
    monkeypatch.setenv(LEGACY_DATABASE_ENV, "postgresql://uap@localhost:5432/wp10-same")
    with pytest.raises(StageRevisionError, match="distinct"):
        stages.database_topology()


def test_main_runtime_advances_0019_to_0024(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _patch_db(monkeypatch, REVISION_0019)
    expected = [
        ("WP10.1-runtime", REVISION_0020),
        ("WP10.2-runtime", REVISION_0021),
        ("WP10.3-runtime", REVISION_0022),
        ("WP10.4-runtime", REVISION_0023),
        ("WP10.5-runtime", REVISION_0024),
    ]
    for step_id, revision in expected:
        result = stages.ensure_for_step(step_id)
        assert result["status"] == "passed", result
        assert result["after"] == revision
        assert result["advanced"] is True
    assert state.upgrades == [item[1] for item in expected]
    assert state.closes == len(expected)


def test_revision_mismatch_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _patch_db(monkeypatch, REVISION_0024)
    result = stages.ensure_for_step("WP10.1-runtime")
    assert result["status"] == "failed"
    assert "mismatch" in str(result["detail"])
    assert state.upgrades == []
    assert state.opens == 0


def test_wp10_2_and_later_migration_probes_do_not_advance_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _patch_db(monkeypatch, REVISION_0020)
    result = stages.ensure_for_step("WP10.2-migration")
    assert result["status"] == "passed"
    assert result["advanced"] is False
    assert state.upgrades == []


def test_migrator_closed_on_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _patch_db(monkeypatch, REVISION_0019)
    ok = stages.ensure_for_step("WP10.1-runtime")
    assert ok["status"] == "passed"
    assert state.closes >= 1

    def boom(_target: str) -> None:
        raise StageRevisionError("explicit upgrade failed")

    monkeypatch.setattr(stages, "run_explicit_upgrade", boom)
    state.current = REVISION_0020
    failed = stages.ensure_for_step("WP10.2-runtime")
    assert failed["status"] == "failed"
    assert state.closes >= 2


def test_stage_upgrade_subprocess_inherits_selected_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _patch_db(monkeypatch, REVISION_0019)
    captured: dict[str, str] = {}

    def upgrade(_target: str) -> None:
        captured["database_url"] = os.environ["UAP_DATABASE_URL"]
        state.upgrades.append(_target)
        state.current = _target

    monkeypatch.setattr(stages, "run_explicit_upgrade", upgrade)
    result = stages.ensure_for_step("WP10.1-runtime")

    assert result["status"] == "passed"
    assert captured["database_url"].endswith("/wp10-1")


def test_explicit_upgrade_argv_has_no_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("tools.wp10_stage_revisions.subprocess.run", fake_run)
    monkeypatch.setenv("UAP_DATABASE_URL", "postgresql://uap:stage-secret@localhost/db")
    stages.run_explicit_upgrade(REVISION_0020)
    argv = captured["argv"]
    assert isinstance(argv, list)
    joined = " ".join(str(item) for item in argv)
    assert "stage-secret" not in joined
    assert "postgresql://" not in joined
    assert argv[-1] == REVISION_0020
    assert "head" not in argv
