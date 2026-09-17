"""WP10.6-A orchestrator and static validator tests. No Docker/PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tools import validate_wp10, validate_wp10_6
from tools import wp10_runtime_probe as runtime_probe
from tools.wp10_runtime_probe import (
    FROZEN_STEPS,
    STEPS_BY_ID,
    WP10_1_G10_03_CASE_IDS,
    WP10_1_G10_04_CASE_EXPECTATIONS,
    WP10_1_LEGACY_API_BOUNDARY,
    WP10_1_LEGACY_CASES,
    WP10_1_MIGRATION_CASE_EXPECTATIONS,
    WP10_1_MIGRATION_COUNT_KEYS,
    WP10_1_MIGRATION_IDENTITIES,
    WP10_1_RUNTIME_COUNT_KEYS,
    WP10_1_RUNTIME_DELTAS,
    WP10_1_RUNTIME_IDENTITIES,
    WP10_1_RUNTIME_POSITIVE_VALUES,
    WP10_1_RUNTIME_RELATED_IDS,
    ProbeConfigurationError,
    argv_contains_secret,
    child_env,
    child_evidence_ok,
    discover_source_boundary,
    execute_steps,
    extra_args,
    matrix_evidence_path,
    os_argv,
    plan_steps,
    probe_evidence_path,
    redact_document,
    redact_text,
    reject_cli_secrets,
    repository_head,
    resolve_evidence_directory,
    run_exec_child,
    stdout_contract_ok,
    step_passed,
)
from tools.wp10_runtime_probe import (
    main as probe_main,
)
from tools.wp10_stage_revisions import (
    DATABASE_TOPOLOGY_ENVS,
    LEGACY_DATABASE_ENV,
    REGRESSION_DATABASE_ENV,
    WP10_1_DATABASE_ENV,
    WP10_2_DATABASE_ENV,
    WP10_3_DATABASE_ENV,
    WP10_4_DATABASE_ENV,
    WP10_5_DATABASE_ENV,
    StageRevisionError,
    database_url_for_step,
)

PLATFORM = Path(__file__).resolve().parents[1]
REPO = PLATFORM.parent


class _PassGate:
    def preflight(self) -> dict[str, object]:
        return {
            "status": "passed",
            "disposable": True,
            "identity": {"endpoint": "g10-25-object-store:8333"},
            "inventory": {"digest": "0", "object_count": 1, "counts": {"raw": 1}},
        }

    def cleanup(self) -> dict[str, object]:
        return {"status": "passed", "residue": 0, "deleted": 0}

    def ensure(self, step_id: str) -> dict[str, object]:
        return {"status": "passed", "step_id": step_id, "advanced": False, "target": "ok"}


@pytest.fixture(autouse=True)
def _g10_25_default_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _PassGate()
    monkeypatch.setattr("tools.wp10_runtime_probe.default_stage_gate", lambda: gate)
    monkeypatch.setattr("tools.wp10_runtime_probe.default_object_store_gate", lambda: gate)
    monkeypatch.setenv(LEGACY_DATABASE_ENV, "postgresql://uap@localhost:5432/wp10-legacy")
    monkeypatch.setenv(WP10_1_DATABASE_ENV, "postgresql://uap@localhost:5432/wp10-1")
    monkeypatch.setenv(WP10_2_DATABASE_ENV, "postgresql://uap@localhost:5432/wp10-2")
    monkeypatch.setenv(WP10_3_DATABASE_ENV, "postgresql://uap@localhost:5432/wp10-3")
    monkeypatch.setenv(WP10_4_DATABASE_ENV, "postgresql://uap@localhost:5432/wp10-4")
    monkeypatch.setenv(WP10_5_DATABASE_ENV, "postgresql://uap@localhost:5432/wp10-5")
    monkeypatch.setenv(REGRESSION_DATABASE_ENV, "postgresql://uap@localhost:5432/wp10-regression")


class FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def wp10_1_evidence(step_id: str) -> dict[str, Any]:
    step = STEPS_BY_ID[step_id]
    requirements = ["G10-05"] if step_id.endswith("migration") else ["G10-02", "G10-03", "G10-04"]
    before = runtime_snapshot()
    cases: list[dict[str, Any]] = []
    if step_id.endswith("migration"):
        for case_id, expected in WP10_1_MIGRATION_CASE_EXPECTATIONS.items():
            case_before: dict[str, Any]
            case_after: dict[str, Any]
            if case_id == "G10-05-empty-roundtrip":
                case_before = {"revision": "0019_manual_claims_binding"}
                case_after = migration_snapshot()
            elif case_id == "G10-05-upgrade-public-preflight":
                case_before = {"public.documents": 1}
                case_after = migration_snapshot(legacy=True, counts={"public.documents": 1})
            elif case_id == "G10-05-legacy-quarantine":
                case_before = migration_snapshot(legacy=True)
                case_after = migration_snapshot(
                    counts={
                        "audit.document_publication_grants": 1,
                        "audit.publication_quarantine": 2,
                        "ops.outbox_events": 1,
                    }
                )
            elif case_id == "G10-05-claim-document-dependency":
                case_before = {"decision_grant_manifest_outbox": [0, 0, 0, 0]}
                case_after = dict(case_before)
            else:
                marker_key = {
                    "G10-05-downgrade-v2": "audit.document_publication_manifests",
                    "G10-05-downgrade-quarantine": "audit.publication_quarantine",
                    "G10-05-downgrade-terminal": "ops.outbox_events",
                    "G10-05-downgrade-public": "public.documents",
                }[case_id]
                case_before = migration_snapshot(counts={marker_key: 1})
                case_after = dict(case_before)
            cases.append(
                wp10_1_case(case_id, "G10-05", expected, dict(expected), case_before, case_after)
            )
    else:
        legacy_expected = {
            "api_exception": "ValueError",
            "database_sqlstate": "22P02",
            "http_status": None,
            "request_id": None,
        }
        for case_id, (database_type, legacy_value) in WP10_1_LEGACY_CASES.items():
            case = wp10_1_case(
                case_id,
                "G10-02",
                legacy_expected,
                {
                    **legacy_expected,
                    "database_error": (
                        f'invalid input value for enum {database_type}: "{legacy_value}"'
                    ),
                },
                before,
                before,
            )
            case["name"] = f"legacy value {legacy_value} rejected"
            case["api_boundary"] = WP10_1_LEGACY_API_BOUNDARY
            cases.append(case)
        for case_id, (api_values, database_only) in WP10_1_RUNTIME_POSITIVE_VALUES.items():
            cases.append(
                wp10_1_case(
                    case_id,
                    "G10-02",
                    {
                        "api_values_accepted_by_database": api_values,
                        "documented_db_only_values": database_only,
                    },
                    {
                        "api_values": api_values,
                        "database_only_values": database_only,
                        "database_values": [*api_values, *database_only],
                    },
                    before,
                    before,
                )
            )
        for case_id in sorted(WP10_1_G10_03_CASE_IDS):
            cases.append(
                wp10_1_case(
                    case_id,
                    "G10-03",
                    {"sqlstate": "42501"},
                    {"sqlstate": "42501", "error": "permission denied"},
                    before,
                    before,
                )
            )
        for case_id, expected in WP10_1_G10_04_CASE_EXPECTATIONS.items():
            actual: dict[str, Any]
            case_after = runtime_snapshot(WP10_1_RUNTIME_DELTAS.get(case_id, {}))
            if case_id == "G10-04-document-approve":
                actual = {
                    "grant_sha256": "c" * 64,
                    "manifest_sha256": "c" * 64,
                    "outbox_sha256": "c" * 64,
                    "outbox_schema": "publication-outbox.v2",
                }
            elif case_id == "G10-04-document-revise":
                actual = {"quarantine_resolutions": 2}
            elif case_id == "G10-04-claim-post-change":
                actual = {
                    "evidence_count": 1,
                    "grant_sha256": "d" * 64,
                    "manifest_sha256": "d" * 64,
                }
            elif case_id == "G10-04-entity-post-change":
                actual = {"grant_sha256": "e" * 64, "manifest_sha256": "e" * 64}
            else:
                actual = dict(expected)
                case_after = before
            cases.append(wp10_1_case(case_id, "G10-04", expected, actual, before, case_after))
    return {
        "schema_version": 1,
        "schema": (
            "wp10.1-migration-evidence.v1"
            if step_id.endswith("migration")
            else "wp10.1-runtime-evidence.v1"
        ),
        "status": "passed",
        "requirements": requirements,
        "source": {
            "commit": runtime_probe.current_source_commit(),
            "probe_sha256": hashlib.sha256(
                (PLATFORM / "tools" / step.script).read_bytes()
            ).hexdigest(),
        },
        "environment": {"python": "3.12.0", "postgresql": "16.1"},
        "started_at": "2026-09-13T00:00:00Z",
        "finished_at": "2026-09-13T00:00:01Z",
        "summary": {
            "total": len(cases),
            "passed": len(cases),
            "failed": 0,
            "not_run": 0,
        },
        "cases": cases,
    }


def wp10_1_case(
    case_id: str,
    requirement: str,
    expected: dict[str, Any],
    actual: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    migration = case_id.startswith("G10-05-")
    role, operation = (
        WP10_1_MIGRATION_IDENTITIES[case_id] if migration else WP10_1_RUNTIME_IDENTITIES[case_id]
    )
    if migration:
        alias = case_id.removeprefix("G10-05-").removeprefix("downgrade-")
        alias = {
            "empty-roundtrip": "roundtrip",
            "upgrade-public-preflight": "preflight",
            "claim-document-dependency": "negative",
            "legacy-quarantine": "legacy",
        }.get(alias, alias)
        related_ids: dict[str, str] = {"database_alias": alias}
        if case_id.startswith("G10-05-downgrade-"):
            related_ids.update(marker=alias, marker_id="00000000-0000-4000-8000-000000000001")
    else:
        related_ids = {
            key: f"00000000-0000-4000-8000-{index:012d}"
            for index, key in enumerate(sorted(WP10_1_RUNTIME_RELATED_IDS.get(case_id, ())), 1)
        }
    return {
        "id": case_id,
        "requirement": requirement,
        "name": f"{case_id} evidence",
        "status": "passed",
        "role": role,
        "operation": operation,
        "expected": expected,
        "actual": actual,
        "before": before,
        "after": after,
        "related_ids": related_ids,
        **(migration_revisions(case_id) if migration else {}),
    }


def stable_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def runtime_snapshot(counts: dict[str, int] | None = None) -> dict[str, Any]:
    values = {key: 0 for key in WP10_1_RUNTIME_COUNT_KEYS}
    values.update(counts or {})
    return {"counts": values, "digest": stable_digest(values)}


def migration_snapshot(
    *, legacy: bool = False, counts: dict[str, int] | None = None
) -> dict[str, Any]:
    values: dict[str, int | None] = {key: 0 for key in WP10_1_MIGRATION_COUNT_KEYS}
    if legacy:
        for key in (
            "audit.document_publication_manifests",
            "audit.claim_publication_manifests",
            "audit.publication_quarantine",
        ):
            values[key] = None
    values.update(counts or {})
    unsigned = {
        "revision": ("0019_manual_claims_binding" if legacy else "0020_wp10_publication_contract"),
        "counts": values,
        "objects": {"tables": 1, "functions": 1, "constraints": 1},
    }
    return {**unsigned, "digest": stable_digest(unsigned)}


def migration_revisions(case_id: str) -> dict[str, str]:
    starts_at_0019 = case_id in {
        "G10-05-legacy-quarantine",
        "G10-05-empty-roundtrip",
        "G10-05-upgrade-public-preflight",
    }
    return {
        "initial_revision": (
            "0019_manual_claims_binding" if starts_at_0019 else "0020_wp10_publication_contract"
        ),
        "target_revision": (
            "0019_manual_claims_binding"
            if case_id.startswith("G10-05-downgrade-")
            else "0020_wp10_publication_contract"
        ),
        "final_revision": (
            "0019_manual_claims_binding"
            if case_id == "G10-05-upgrade-public-preflight"
            else "0020_wp10_publication_contract"
        ),
    }


def write_required_probe_evidence(evidence: Path, step_id: str) -> None:
    step = STEPS_BY_ID[step_id]
    payload: dict[str, Any]
    if step_id in {"WP10.1-migration", "WP10.1-runtime"}:
        payload = wp10_1_evidence(step_id)
    else:
        payload = {"status": "passed"}
    path = probe_evidence_path(evidence, step)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def contract_stdout(step_id: str) -> str:
    if step_id == "WP3":
        return json.dumps(
            {
                "append_only_sqlstate": "25006",
                "business_tables": 12,
                "denied_role_sqlstates": {"uap_worker": "42501"},
                "head": "0024_wp10_admin_replay",
                "migrator_login_rejected": True,
                "object_sha256": "ab",
            },
            indent=2,
            sort_keys=True,
        )
    if step_id == "WP5":
        return str(
            {
                "source_run_outcome": "completed",
                "job_status": "succeeded",
                "retry_job_status": "succeeded",
                "atomic_retry": "reclaimed and completed",
                "provenance_relabel": "rejected",
                "stale_checkpoint_sqlstate": "40001",
                "cross_source_config_sqlstate": "23503",
            }
        )
    if step_id == "WP6":
        return json.dumps({"documents": 3, "successful_extractions": 3}, sort_keys=True)
    if step_id == "WP7":
        return json.dumps(
            {
                "model_runs": 4,
                "valid_result": True,
                "invalid_result": True,
                "semantic_duplicate_without_provider_call": True,
                "auth_failures_terminal": True,
                "rate_limit_retry": True,
                "upstream_failure_closed": True,
            }
        )
    return "runtime probe passed"


def _ok_runner(argv: list[str], *, cwd: Path, env: dict[str, str]) -> FakeCompleted:
    step = STEPS_BY_ID[argv[argv.index("--exec-child") + 1]]
    return FakeCompleted(0, stdout=contract_stdout(step.step_id))


def test_frozen_step_order() -> None:
    assert [step.step_id for step in FROZEN_STEPS] == list(validate_wp10.EXPECTED_STEP_IDS)
    assert [step.step_id for step in FROZEN_STEPS[:6]] == ["WP3", "WP4", "WP5", "WP6", "WP7", "WP8"]
    assert [step.step_id for step in FROZEN_STEPS[6:12]] == [
        "WP9.1",
        "WP9.2",
        "WP9.3",
        "WP9.4",
        "WP9.5",
        "WP9.6",
    ]
    groups = [step.group for step in FROZEN_STEPS]
    assert groups.index("WP10.1") < groups.index("WP10.2") < groups.index("WP10.3")
    assert groups.index("WP10.3") < groups.index("WP10.4") < groups.index("WP10.5")


def test_plan_mode_is_not_passed(capsys: pytest.CaptureFixture[str]) -> None:
    assert probe_main(["--plan"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "plan"
    assert payload["status"] != "passed"
    assert [item["step_id"] for item in payload["steps"]] == list(validate_wp10.EXPECTED_STEP_IDS)
    assert all(item["status"] == "planned" for item in payload["steps"])
    assert plan_steps()[0]["status"] == "planned"


def test_module_entry_plan_with_src_only_pythonpath(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(PLATFORM / "src")}
    argv = [sys.executable, "-m", "tools.wp10_runtime_probe", "--plan"]
    result = subprocess.run(  # noqa: S603
        argv,
        cwd=str(PLATFORM),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "plan"

    child_argv = os_argv(FROZEN_STEPS[0], tmp_path)
    assert child_argv[:3] == [sys.executable, "-m", "tools.wp10_runtime_probe"]
    assert "tools/wp10_runtime_probe.py" not in " ".join(child_argv)


def test_database_topology_routes_each_frozen_step(monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = "postgresql://uap@localhost:5432/wp10-legacy"
    wp10_1 = "postgresql://uap@localhost:5432/wp10-1"
    wp10_2 = "postgresql://uap@localhost:5432/wp10-2"
    wp10_3 = "postgresql://uap@localhost:5432/wp10-3"
    wp10_4 = "postgresql://uap@localhost:5432/wp10-4"
    wp10_5 = "postgresql://uap@localhost:5432/wp10-5"
    regression = "postgresql://uap@localhost:5432/wp10-regression"
    monkeypatch.setenv(LEGACY_DATABASE_ENV, legacy)
    monkeypatch.setenv(WP10_1_DATABASE_ENV, wp10_1)
    monkeypatch.setenv(WP10_2_DATABASE_ENV, wp10_2)
    monkeypatch.setenv(WP10_3_DATABASE_ENV, wp10_3)
    monkeypatch.setenv(WP10_4_DATABASE_ENV, wp10_4)
    monkeypatch.setenv(WP10_5_DATABASE_ENV, wp10_5)
    monkeypatch.setenv(REGRESSION_DATABASE_ENV, regression)

    assert database_url_for_step("WP3") == legacy
    assert database_url_for_step("WP10.1-migration") == wp10_1
    assert database_url_for_step("WP10.2-runtime") == wp10_2
    assert database_url_for_step("WP10.3-runtime") == wp10_3
    assert database_url_for_step("WP10.4-runtime") == wp10_4
    assert database_url_for_step("WP10.5-runtime") == wp10_5
    assert database_url_for_step("WP10.3-wp10.2-regression") == regression
    child = child_env(STEPS_BY_ID["WP10.3-wp10.2-regression"])
    assert child["UAP_DATABASE_URL"] == regression
    args = extra_args(STEPS_BY_ID["WP10.3-wp10.2-regression"], Path("wp10-evidence"))
    assert args[args.index("--database-url") + 1] == regression


def test_database_topology_rejects_missing_or_duplicate_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for environment in DATABASE_TOPOLOGY_ENVS:
        monkeypatch.setenv(environment, f"postgresql://uap@localhost:5432/{environment.lower()}")
    monkeypatch.delenv(REGRESSION_DATABASE_ENV, raising=False)
    with pytest.raises(StageRevisionError, match="topology"):
        database_url_for_step("WP3")

    monkeypatch.setenv(
        REGRESSION_DATABASE_ENV, "postgresql://uap@localhost:5432/uap_wp10_legacy_database_url"
    )
    with pytest.raises(StageRevisionError, match="distinct"):
        database_url_for_step("WP3")


def test_ci_isolates_legacy_probe_and_closes_migrators() -> None:
    workflow = (REPO / ".github/workflows/platform-ci.yml").read_text(encoding="utf-8")
    integration = validate_wp10.workflow_job(workflow, "integration")
    legacy_index = integration.index(
        "Run legacy WP3 through WP9.6 probes on isolated legacy probe database"
    )
    rotate_index = integration.index("Rotate disposable object store before full G10-25")
    runtime_index = integration.index("Run WP10 WP3-through-WP10.5 runtime probe")

    assert integration.count("g10-25-disposable-object-store.sh start") == 2
    assert "g10-25-disposable-object-store.sh stop" in integration
    assert 'legacy_probe_db="uap_wp10_legacy_probe_' in integration
    assert '"$UAP_WP10_LEGACY_PROBE_DB"' in integration
    store_start = "g10-25-disposable-object-store.sh start"
    first_start = integration.index(store_start)
    second_start = integration.rindex(store_start)
    assert first_start < legacy_index < rotate_index
    assert rotate_index < second_start < runtime_index
    assert "close_migrator()" in integration
    assert 'run_tool "$1" python tools/configure_roles.py disable-migrator' in integration
    assert integration.count('close_migrator "$') == 8


def _container_platform_layout(tmp_path: Path) -> Path:
    container_platform = tmp_path / "workspace"
    tools = container_platform / "tools"
    tools.mkdir(parents=True)
    for name in (
        "__init__.py",
        "wp10_runtime_probe.py",
        "wp10_object_store_guard.py",
        "wp10_stage_revisions.py",
    ):
        shutil.copy2(PLATFORM / "tools" / name, tools / name)
    for script in {step.script for step in FROZEN_STEPS}:
        (tools / script).symlink_to(PLATFORM / "tools" / script)
    (container_platform / "src").symlink_to(PLATFORM / "src", target_is_directory=True)
    return container_platform


def _container_module_run(platform: Path, evidence: Path) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("UAP_")}
    env["PYTHONPATH"] = str(platform / "src")
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "tools.wp10_runtime_probe",
            "--evidence-dir",
            str(evidence),
        ],
        cwd=str(platform),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_container_source_boundary_subprocess_is_fail_closed(tmp_path: Path) -> None:
    platform = _container_platform_layout(tmp_path)
    assert not (platform / ".git").exists()
    assert discover_source_boundary(platform) == platform.resolve()

    external = tmp_path / "evidence"
    result = _container_module_run(platform, external)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "must be outside" not in result.stderr
    summary = json.loads((external / "wp10-runtime-evidence.json").read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert summary["failed_step"] in {"object-store-preflight", "discovery"}

    inside = platform / "evidence"
    result = _container_module_run(platform, inside)
    assert result.returncode == 2
    assert "outside the source boundary" in result.stderr
    assert not (inside / "wp10-runtime-evidence.json").exists()

    escape = platform / "evidence-link"
    escape.symlink_to(external, target_is_directory=True)
    result = _container_module_run(platform, escape)
    assert result.returncode == 2
    assert "outside the source boundary" in result.stderr

    ingress = tmp_path / "source-link"
    ingress.symlink_to(platform, target_is_directory=True)
    result = _container_module_run(platform, ingress / "linked-evidence")
    assert result.returncode == 2
    assert "outside the source boundary" in result.stderr


def test_evidence_boundary_discovers_real_git_root_and_normalizes_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    platform = repository / "platform"
    platform.mkdir(parents=True)
    (repository / ".git").mkdir()
    boundary = discover_source_boundary(platform)
    assert boundary == repository.resolve()

    outside = tmp_path / "outside"
    monkeypatch.chdir(tmp_path)
    assert resolve_evidence_directory(Path("outside"), source_boundary=boundary) == outside
    with pytest.raises(ProbeConfigurationError):
        resolve_evidence_directory(platform / "evidence", source_boundary=boundary)
    with pytest.raises(ProbeConfigurationError):
        resolve_evidence_directory(Path("repo/evidence"), source_boundary=boundary)

    worktree_repository = tmp_path / "worktree-repository"
    worktree_platform = worktree_repository / "platform"
    worktree_platform.mkdir(parents=True)
    (worktree_repository / ".git").write_text("gitdir: /tmp/uap-wp10-gitdir\n", encoding="utf-8")
    assert discover_source_boundary(worktree_platform) == worktree_repository.resolve()


def test_exec_child_uses_sibling_imports_and_restores_process_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tools.wp10_runtime_probe as runtime

    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    script = tools_dir / "wp7_runtime_probe.py"
    script.write_text("# test script is intercepted by runpy\n", encoding="utf-8")
    sibling_name = "wp10_r8_sibling_probe"
    (tools_dir / f"{sibling_name}.py").write_text("VALUE = 'sibling'\n", encoding="utf-8")

    original_argv = sys.argv
    original_argv_values = list(sys.argv)
    original_sys_path = sys.path
    original_sys_path_values = list(sys.path)
    seen: dict[str, str] = {}

    def fake_run_path(path: str, *, run_name: str) -> None:
        seen["path"] = path
        seen["run_name"] = run_name
        seen["value"] = __import__(sibling_name).VALUE
        assert sys.path[0] == str(tools_dir)
        assert sys.argv[0] == str(script)
        raise RuntimeError("probe failure")

    monkeypatch.setattr(runtime, "TOOLS_DIR", tools_dir)
    monkeypatch.setattr("tools.wp10_runtime_probe.runpy.run_path", fake_run_path)
    with pytest.raises(RuntimeError, match="probe failure"):
        run_exec_child("WP7", tmp_path / "evidence")

    assert seen == {"path": str(script), "run_name": "__main__", "value": "sibling"}
    assert sys.argv is original_argv
    assert sys.argv == original_argv_values
    assert sys.path is original_sys_path
    assert sys.path == original_sys_path_values


def test_exec_child_subprocess_preserves_legacy_sibling_import(tmp_path: Path) -> None:
    platform = tmp_path / "platform"
    tools = platform / "tools"
    tools.mkdir(parents=True)
    for name in (
        "__init__.py",
        "wp10_runtime_probe.py",
        "wp10_object_store_guard.py",
        "wp10_stage_revisions.py",
        "wp6_runtime_probe.py",
        "wp7_runtime_probe.py",
    ):
        shutil.copy2(PLATFORM / "tools" / name, tools / name)
    (platform / "src").symlink_to(PLATFORM / "src", target_is_directory=True)

    evidence = tmp_path / "evidence"
    env = {key: value for key, value in os.environ.items() if not key.startswith("UAP_")}
    env["PYTHONPATH"] = str(platform / "src")
    env.update(
        {
            LEGACY_DATABASE_ENV: "postgresql://uap@localhost:5432/wp10-legacy",
            WP10_1_DATABASE_ENV: "postgresql://uap@localhost:5432/wp10-1",
            WP10_2_DATABASE_ENV: "postgresql://uap@localhost:5432/wp10-2",
            WP10_3_DATABASE_ENV: "postgresql://uap@localhost:5432/wp10-3",
            WP10_4_DATABASE_ENV: "postgresql://uap@localhost:5432/wp10-4",
            WP10_5_DATABASE_ENV: "postgresql://uap@localhost:5432/wp10-5",
            REGRESSION_DATABASE_ENV: "postgresql://uap@localhost:5432/wp10-regression",
        }
    )
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "tools.wp10_runtime_probe",
            "--exec-child",
            "WP7",
            "--evidence-dir",
            str(evidence),
        ],
        cwd=str(platform),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "No module named 'wp6_runtime_probe'" not in combined
    assert "ModuleNotFoundError" not in combined
    assert "ValidationError" in combined


def test_execute_stops_after_nonzero(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], *, cwd: Path, env: dict[str, str]) -> FakeCompleted:
        calls.append(argv)
        step = STEPS_BY_ID[argv[argv.index("--exec-child") + 1]]
        if len(calls) < 3:
            return FakeCompleted(0, stdout=contract_stdout(step.step_id))
        return FakeCompleted(7, stdout=contract_stdout(step.step_id))

    payload = execute_steps(tmp_path / "evidence", runner=runner)  # type: ignore[arg-type]
    assert payload["status"] == "failed"
    assert payload["failed_step"] == FROZEN_STEPS[2].step_id
    assert len(calls) == 3
    assert len(payload["steps"]) == len(FROZEN_STEPS)
    assert payload["steps"][2]["exit_code"] == 7
    assert [item["status"] for item in payload["steps"][:2]] == ["passed", "passed"]
    assert payload["steps"][2]["status"] == "failed"
    assert all(item["status"] == "not_run" for item in payload["steps"][3:])
    assert payload["step_counts"]["not_run"] == len(FROZEN_STEPS) - 3
    assert payload["step_counts"]["failed"] == 1
    assert payload["step_counts"]["passed"] == 2


def test_nonzero_exit_propagates(tmp_path: Path) -> None:
    def runner(argv: list[str], *, cwd: Path, env: dict[str, str]) -> FakeCompleted:
        return FakeCompleted(11, stdout="child failed")

    payload = execute_steps(tmp_path / "out", runner=runner)  # type: ignore[arg-type]
    assert payload["status"] == "failed"
    assert payload["steps"][0]["exit_code"] == 11
    assert payload["failed_step"] == "WP3"


def test_exception_writes_failed_evidence(tmp_path: Path) -> None:
    def runner(argv: list[str], *, cwd: Path, env: dict[str, str]) -> FakeCompleted:
        raise RuntimeError("spawn exploded")

    payload = execute_steps(tmp_path / "out", runner=runner)  # type: ignore[arg-type]
    assert payload["status"] == "failed"
    assert payload["steps"][0]["status"] == "failed"
    assert "RuntimeError" in payload["steps"][0]["detail"]
    assert payload["steps"][0]["exit_code"] == 1
    assert payload["steps"][0]["evidence_path"]


def test_main_writes_failed_summary_on_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "outside"
    evidence.mkdir()

    def boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("orchestrator crashed")

    monkeypatch.setattr("tools.wp10_runtime_probe.execute_steps", boom)
    assert probe_main(["--evidence-dir", str(evidence)]) == 1
    summary = json.loads((evidence / "wp10-runtime-evidence.json").read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert summary["failed_step"] == "orchestrator"


def test_evidence_redacts_password_and_dsn() -> None:
    hidden = "super-secret-probe-password"
    payload = {
        "command": ["python", "tools/x.py", "--admin-url", f"postgresql://uap:{hidden}@db/uap"],
        "detail": f"could not login with {hidden}",
    }
    redacted = redact_document(payload, frozenset({hidden}))
    dumped = json.dumps(redacted)
    assert hidden not in dumped
    assert "<redacted>" in dumped
    assert hidden not in redact_text(f"postgresql://uap:{hidden}@db/uap", frozenset({hidden}))


def test_rejects_cli_passwords() -> None:
    with pytest.raises(ProbeConfigurationError):
        reject_cli_secrets(["wp10_runtime_probe.py", "--password", "x"])
    with pytest.raises(ProbeConfigurationError):
        reject_cli_secrets(["wp10_runtime_probe.py", "--admin-url", "postgresql://uap:x@db/uap"])
    with pytest.raises(ProbeConfigurationError):
        reject_cli_secrets(["wp10_runtime_probe.py", "postgresql://uap:secret@localhost/db"])


def test_os_argv_has_no_dsn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hidden = "argv-must-not-leak-password"
    monkeypatch.setenv("UAP_DATABASE_URL", f"postgresql://uap:{hidden}@localhost/db")
    monkeypatch.setenv("UAP_PUBLISHER_PASSWORD", hidden)
    monkeypatch.setenv("UAP_PUBLIC_READER_PASSWORD", hidden)
    monkeypatch.setenv("UAP_WP10_ADMIN_URL", f"postgresql://uap:{hidden}@localhost/db")
    for step in FROZEN_STEPS:
        argv = os_argv(step, tmp_path)
        assert argv[:3] == [sys.executable, "-m", "tools.wp10_runtime_probe"]
        assert "wp10_runtime_probe.py" not in " ".join(argv)
        assert "--admin-url" not in argv
        assert "--database-url" not in argv
        assert "--publisher-url" not in argv
        assert "--reader-url" not in argv
        assert hidden not in " ".join(argv)
        assert not argv_contains_secret(argv, frozenset({hidden}))
        assert "--exec-child" in argv


def test_wp3_real_success_json_without_passed_token() -> None:
    stdout = contract_stdout("WP3")
    assert "passed" not in stdout.lower()
    ok, reason = stdout_contract_ok(STEPS_BY_ID["WP3"], stdout)
    assert ok is True
    assert reason == "wp3-json"


def test_wp5_wp6_wp7_real_success_output_without_passed_token() -> None:
    for step_id, expected in (
        ("WP5", "wp5-mapping"),
        ("WP6", "wp6-json"),
        ("WP7", "wp7-json"),
    ):
        stdout = contract_stdout(step_id)
        assert "passed" not in stdout.lower()
        ok, reason = stdout_contract_ok(STEPS_BY_ID[step_id], stdout)
        assert ok is True, step_id
        assert reason == expected


def test_wp3_success_json_is_accepted_by_orchestrator(tmp_path: Path) -> None:
    def runner(argv: list[str], *, cwd: Path, env: dict[str, str]) -> FakeCompleted:
        step = STEPS_BY_ID[argv[argv.index("--exec-child") + 1]]
        if step.step_id == "WP3":
            return FakeCompleted(0, stdout=contract_stdout("WP3"))
        return FakeCompleted(1, stdout="stop")

    payload = execute_steps(tmp_path / "ev", runner=runner)  # type: ignore[arg-type]
    assert payload["steps"][0]["status"] == "passed"
    assert payload["failed_step"] == "WP4"


def test_wp3_json_missing_required_keys_fails(tmp_path: Path) -> None:
    def runner(argv: list[str], *, cwd: Path, env: dict[str, str]) -> FakeCompleted:
        return FakeCompleted(0, stdout='{"head": "0024_wp10_admin_replay"}')

    payload = execute_steps(tmp_path / "ev", runner=runner)  # type: ignore[arg-type]
    assert payload["status"] == "failed"
    assert payload["failed_step"] == "WP3"
    assert payload["steps"][0]["detail"] == "stdout-contract:wp3-json"


def test_exit_zero_without_probe_json_is_failed(tmp_path: Path) -> None:
    payload = execute_steps(tmp_path / "ev", runner=_ok_runner)  # type: ignore[arg-type]
    assert payload["status"] == "failed"
    required = [step for step in FROZEN_STEPS if step.requires_probe_json]
    assert payload["failed_step"] == required[0].step_id
    failed = next(item for item in payload["steps"] if item["status"] == "failed")
    assert failed["detail"] == "missing-probe-evidence"
    assert payload["steps"][-1]["status"] == "not_run"


def test_preexisting_passed_probe_json_is_not_accepted(tmp_path: Path) -> None:
    evidence = tmp_path / "ev"
    evidence.mkdir()
    required = [step for step in FROZEN_STEPS if step.requires_probe_json]
    for step in required:
        path = probe_evidence_path(evidence, step)
        path.write_text('{"status": "passed"}\n', encoding="utf-8")
        os.utime(path, None)
    payload = execute_steps(evidence, runner=_ok_runner)  # type: ignore[arg-type]
    assert payload["status"] == "failed"
    assert payload["failed_step"] == required[0].step_id
    failed = next(item for item in payload["steps"] if item["status"] == "failed")
    assert failed["detail"] == "missing-probe-evidence"
    assert not probe_evidence_path(evidence, required[0]).exists()
    assert payload["steps"][-1]["status"] == "not_run"


def test_probe_json_must_be_passed(tmp_path: Path) -> None:
    evidence = tmp_path / "ev"

    def runner(argv: list[str], *, cwd: Path, env: dict[str, str]) -> FakeCompleted:
        idx = argv.index("--exec-child")
        step = STEPS_BY_ID[argv[idx + 1]]
        if step.requires_probe_json:
            path = probe_evidence_path(evidence, step)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"status": "failed"}\n', encoding="utf-8")
        return FakeCompleted(0, stdout=contract_stdout(step.step_id))

    payload = execute_steps(evidence, runner=runner)  # type: ignore[arg-type]
    assert payload["status"] == "failed"
    failed = next(item for item in payload["steps"] if item["status"] == "failed")
    assert "probe-evidence-status" in str(failed.get("detail"))


@pytest.mark.parametrize("step_id", ["WP10.1-migration", "WP10.1-runtime"])
def test_wp10_1_probe_evidence_accepts_complete_payload(tmp_path: Path, step_id: str) -> None:
    evidence = tmp_path / "evidence"
    write_required_probe_evidence(evidence, step_id)
    assert child_evidence_ok(STEPS_BY_ID[step_id], evidence) == (True, "ok")


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda payload: payload.update(schema="wrong.v1"), "wp10.1-probe-evidence-schema"),
        (
            lambda payload: payload["summary"].update(passed=0),
            "wp10.1-probe-evidence-summary",
        ),
        (
            lambda payload: payload["cases"][0].pop("before"),
            "wp10.1-probe-evidence-case",
        ),
        (
            lambda payload: payload["source"].update(probe_sha256="0" * 64),
            "wp10.1-probe-evidence-shape",
        ),
        (
            lambda payload: payload.update(environment={"python": "3.12"}),
            "wp10.1-probe-evidence-shape",
        ),
    ],
)
def test_wp10_1_probe_evidence_rejects_spoofed_payload(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    reason: str,
) -> None:
    step = STEPS_BY_ID["WP10.1-runtime"]
    payload = wp10_1_evidence(step.step_id)
    mutation(payload)
    path = probe_evidence_path(tmp_path, step)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert child_evidence_ok(step, tmp_path) == (False, reason)


def test_wp10_1_probe_evidence_rejects_missing_requirement(tmp_path: Path) -> None:
    step = STEPS_BY_ID["WP10.1-runtime"]
    payload = wp10_1_evidence(step.step_id)
    payload["cases"] = payload["cases"][:-1]
    payload["summary"].update(total=65, passed=65)
    path = probe_evidence_path(tmp_path, step)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert child_evidence_ok(step, tmp_path) == (False, "wp10.1-probe-evidence-scenarios")


@pytest.mark.parametrize("step_id", ["WP10.1-migration", "WP10.1-runtime"])
@pytest.mark.parametrize("commit", ["f" * 40, "unavailable", "candidate"])
def test_wp10_1_probe_evidence_rejects_unbound_commit(
    tmp_path: Path, step_id: str, commit: str
) -> None:
    step = STEPS_BY_ID[step_id]
    payload = wp10_1_evidence(step_id)
    payload["source"]["commit"] = commit
    path = probe_evidence_path(tmp_path, step)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert child_evidence_ok(step, tmp_path) == (False, "wp10.1-probe-evidence-shape")
    assert step_passed(exit_code=0, stdout="", step=step, evidence_dir=tmp_path) == (
        False,
        "wp10.1-probe-evidence-shape",
    )


def test_wp10_1_probe_evidence_fails_closed_when_git_identity_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = STEPS_BY_ID["WP10.1-migration"]
    payload = wp10_1_evidence(step.step_id)
    path = probe_evidence_path(tmp_path, step)
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(runtime_probe, "current_source_commit", lambda: None)
    assert child_evidence_ok(step, tmp_path) == (False, "wp10.1-probe-evidence-shape")


def test_repository_head_resolves_read_only_loose_reference(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    reference = git_dir / "refs/heads/candidate"
    reference.parent.mkdir(parents=True)
    git_dir.joinpath("HEAD").write_text("ref: refs/heads/candidate\n", encoding="utf-8")
    reference.write_text("a" * 40 + "\n", encoding="utf-8")
    assert repository_head(tmp_path) == "a" * 40


def test_current_source_commit_uses_metadata_when_runtime_omits_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    git_dir.joinpath("HEAD").write_text("b" * 40 + "\n", encoding="utf-8")
    monkeypatch.setattr("tools.wp10_runtime_probe.shutil.which", lambda _name: None)
    monkeypatch.setattr(runtime_probe, "SOURCE_BOUNDARY", tmp_path)
    assert runtime_probe.current_source_commit() == "b" * 40


@pytest.mark.parametrize("head", ["unavailable", "ref: ../../outside", "1" * 39])
def test_repository_head_rejects_unreliable_metadata(tmp_path: Path, head: str) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    git_dir.joinpath("HEAD").write_text(head + "\n", encoding="utf-8")
    assert repository_head(tmp_path) is None


@pytest.mark.parametrize("step_id", ["WP10.1-migration", "WP10.1-runtime"])
def test_wp10_1_probe_evidence_requires_exact_unique_scenario_ids(
    tmp_path: Path, step_id: str
) -> None:
    step = STEPS_BY_ID[step_id]
    payload = wp10_1_evidence(step_id)
    payload["cases"][-1] = json.loads(json.dumps(payload["cases"][0]))
    path = probe_evidence_path(tmp_path, step)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert child_evidence_ok(step, tmp_path) == (False, "wp10.1-probe-evidence-scenarios")


@pytest.mark.parametrize("step_id", ["WP10.1-migration", "WP10.1-runtime"])
def test_wp10_1_probe_evidence_rejects_wrong_requirement(tmp_path: Path, step_id: str) -> None:
    step = STEPS_BY_ID[step_id]
    payload = wp10_1_evidence(step_id)
    payload["cases"][0]["requirement"] = "G10-99"
    path = probe_evidence_path(tmp_path, step)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert child_evidence_ok(step, tmp_path) == (False, "wp10.1-probe-evidence-case")


@pytest.mark.parametrize("step_id", ["WP10.1-migration", "WP10.1-runtime"])
def test_wp10_1_probe_evidence_rejects_failed_case_hidden_by_summary(
    tmp_path: Path, step_id: str
) -> None:
    step = STEPS_BY_ID[step_id]
    payload = wp10_1_evidence(step_id)
    payload["cases"][0]["status"] = "not_run"
    path = probe_evidence_path(tmp_path, step)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert child_evidence_ok(step, tmp_path) == (False, "wp10.1-probe-evidence-case")


@pytest.mark.parametrize(
    ("step_id", "case_id", "key", "value"),
    [
        (
            "WP10.1-migration",
            "G10-05-downgrade-v2",
            "sqlstate",
            "00000",
        ),
        ("WP10.1-runtime", "G10-02-positive-01", "database_values", []),
        ("WP10.1-runtime", "G10-03-api-dml-01", "sqlstate", "00000"),
        (
            "WP10.1-runtime",
            "G10-04-document-approve",
            "manifest_sha256",
            "0" * 64,
        ),
    ],
)
def test_wp10_1_probe_evidence_rejects_tampered_key_result(
    tmp_path: Path,
    step_id: str,
    case_id: str,
    key: str,
    value: object,
) -> None:
    step = STEPS_BY_ID[step_id]
    payload = wp10_1_evidence(step_id)
    case = next(item for item in payload["cases"] if item["id"] == case_id)
    case["actual"][key] = value
    path = probe_evidence_path(tmp_path, step)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert child_evidence_ok(step, tmp_path) == (False, "wp10.1-probe-evidence-result")


def test_wp10_1_probe_evidence_rejects_empty_key_result(tmp_path: Path) -> None:
    step = STEPS_BY_ID["WP10.1-runtime"]
    payload = wp10_1_evidence(step.step_id)
    case = next(item for item in payload["cases"] if item["id"] == "G10-03-uap_api-merge")
    case["actual"]["error"] = ""
    path = probe_evidence_path(tmp_path, step)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert child_evidence_ok(step, tmp_path) == (False, "wp10.1-probe-evidence-result")


def write_wp10_1_runtime_evidence(tmp_path: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    step = STEPS_BY_ID["WP10.1-runtime"]
    probe_evidence_path(tmp_path, step).write_text(json.dumps(payload), encoding="utf-8")
    return step_passed(exit_code=0, stdout="", step=step, evidence_dir=tmp_path)


def legacy_case(payload: dict[str, Any], case_id: str) -> dict[str, Any]:
    return next(item for item in payload["cases"] if item["id"] == case_id)


def test_wp10_1_legacy_enum_fresh_contract_accepts_all_ten_bound_inputs(tmp_path: Path) -> None:
    payload = wp10_1_evidence("WP10.1-runtime")
    assert write_wp10_1_runtime_evidence(tmp_path, payload) == (True, "probe-json")


@pytest.mark.parametrize("case_id", tuple(WP10_1_LEGACY_CASES))
def test_wp10_1_legacy_enum_rejects_non_frozen_input_for_each_case(
    tmp_path: Path, case_id: str
) -> None:
    payload = wp10_1_evidence("WP10.1-runtime")
    case = legacy_case(payload, case_id)
    database_type, _legacy_value = WP10_1_LEGACY_CASES[case_id]
    wrong_value = "not_a_frozen_legacy_value"
    case["name"] = f"legacy value {wrong_value} rejected"
    case["actual"]["database_error"] = (
        f'invalid input value for enum {database_type}: "{wrong_value}"'
    )
    assert write_wp10_1_runtime_evidence(tmp_path, payload) == (
        False,
        "wp10.1-probe-evidence-result",
    )


@pytest.mark.parametrize(
    ("source_id", "target_id"),
    [
        ("G10-02-legacy-02", "G10-02-legacy-03"),
        ("G10-02-legacy-03", "G10-02-legacy-02"),
        ("G10-02-legacy-04", "G10-02-legacy-05"),
        ("G10-02-legacy-04", "G10-02-legacy-06"),
        ("G10-02-legacy-05", "G10-02-legacy-04"),
        ("G10-02-legacy-05", "G10-02-legacy-06"),
        ("G10-02-legacy-06", "G10-02-legacy-04"),
        ("G10-02-legacy-06", "G10-02-legacy-05"),
        ("G10-02-legacy-07", "G10-02-legacy-08"),
        ("G10-02-legacy-08", "G10-02-legacy-07"),
    ],
)
def test_wp10_1_legacy_enum_rejects_same_type_case_impersonation(
    tmp_path: Path, source_id: str, target_id: str
) -> None:
    payload = wp10_1_evidence("WP10.1-runtime")
    source = json.loads(json.dumps(legacy_case(payload, source_id)))
    target_index = next(
        index for index, item in enumerate(payload["cases"]) if item["id"] == target_id
    )
    source["id"] = target_id
    payload["cases"][target_index] = source
    assert write_wp10_1_runtime_evidence(tmp_path, payload) == (
        False,
        "wp10.1-probe-evidence-result",
    )


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("missing-database-error", None),
        ("database-error-wrong-type", {"message": "invalid"}),
        ("database-error-prefix", "prefix "),
        ("database-error-suffix", " suffix"),
        ("wrong-name", "legacy value unrelated rejected"),
        ("missing-api-boundary", None),
        ("wrong-api-boundary", "unrelated boundary"),
    ],
)
def test_wp10_1_legacy_enum_rejects_ambiguous_or_malformed_input_evidence(
    tmp_path: Path, mutation: str, value: object
) -> None:
    payload = wp10_1_evidence("WP10.1-runtime")
    case = legacy_case(payload, "G10-02-legacy-01")
    if mutation == "missing-database-error":
        case["actual"].pop("database_error")
    elif mutation == "database-error-wrong-type":
        case["actual"]["database_error"] = value
    elif mutation == "database-error-prefix":
        case["actual"]["database_error"] = f"{value}{case['actual']['database_error']}"
    elif mutation == "database-error-suffix":
        case["actual"]["database_error"] = f"{case['actual']['database_error']}{value}"
    elif mutation == "wrong-name":
        case["name"] = value
    elif mutation == "missing-api-boundary":
        case.pop("api_boundary")
    else:
        case["api_boundary"] = value
    assert write_wp10_1_runtime_evidence(tmp_path, payload) == (
        False,
        "wp10.1-probe-evidence-result",
    )


@pytest.mark.parametrize("step_id", ["WP10.1-migration", "WP10.1-runtime"])
@pytest.mark.parametrize("field", ["role", "operation"])
def test_wp10_1_probe_evidence_binds_scenario_actor_and_operation(
    tmp_path: Path, step_id: str, field: str
) -> None:
    step = STEPS_BY_ID[step_id]
    payload = wp10_1_evidence(step_id)
    payload["cases"][0][field] = "SELECT 1" if field == "operation" else "gateadmin"
    probe_evidence_path(tmp_path, step).write_text(json.dumps(payload), encoding="utf-8")
    assert step_passed(exit_code=0, stdout="", step=step, evidence_dir=tmp_path) == (
        False,
        "wp10.1-probe-evidence-result",
    )


@pytest.mark.parametrize("step_id", ["WP10.1-migration", "WP10.1-runtime"])
@pytest.mark.parametrize("field", ["role", "operation"])
def test_wp10_1_probe_evidence_requires_scenario_actor_and_operation(
    tmp_path: Path, step_id: str, field: str
) -> None:
    step = STEPS_BY_ID[step_id]
    payload = wp10_1_evidence(step_id)
    payload["cases"][0].pop(field)
    probe_evidence_path(tmp_path, step).write_text(json.dumps(payload), encoding="utf-8")
    assert child_evidence_ok(step, tmp_path) == (False, "wp10.1-probe-evidence-case")


@pytest.mark.parametrize("case_id", sorted(WP10_1_RUNTIME_IDENTITIES))
def test_wp10_1_runtime_contract_rejects_incomplete_or_inconsistent_snapshot(
    tmp_path: Path, case_id: str
) -> None:
    step = STEPS_BY_ID["WP10.1-runtime"]
    payload = wp10_1_evidence(step.step_id)
    case = next(item for item in payload["cases"] if item["id"] == case_id)
    case["before"]["counts"].pop("audit.audit_events")
    probe_evidence_path(tmp_path, step).write_text(json.dumps(payload), encoding="utf-8")
    assert child_evidence_ok(step, tmp_path) == (False, "wp10.1-probe-evidence-result")

    payload = wp10_1_evidence(step.step_id)
    case = next(item for item in payload["cases"] if item["id"] == case_id)
    case["after"]["digest"] = "0" * 64
    probe_evidence_path(tmp_path, step).write_text(json.dumps(payload), encoding="utf-8")
    assert child_evidence_ok(step, tmp_path) == (False, "wp10.1-probe-evidence-result")


@pytest.mark.parametrize("case_id", sorted(WP10_1_RUNTIME_DELTAS))
def test_wp10_1_runtime_atomic_publish_requires_every_exact_delta(
    tmp_path: Path, case_id: str
) -> None:
    step = STEPS_BY_ID["WP10.1-runtime"]
    payload = wp10_1_evidence(step.step_id)
    case = next(item for item in payload["cases"] if item["id"] == case_id)
    required_key = next(iter(WP10_1_RUNTIME_DELTAS[case_id]))
    case["after"]["counts"][required_key] = case["before"]["counts"][required_key]
    case["after"]["digest"] = stable_digest(case["after"]["counts"])
    probe_evidence_path(tmp_path, step).write_text(json.dumps(payload), encoding="utf-8")
    assert child_evidence_ok(step, tmp_path) == (False, "wp10.1-probe-evidence-result")

    payload = wp10_1_evidence(step.step_id)
    case = next(item for item in payload["cases"] if item["id"] == case_id)
    unchanged_key = next(
        key for key in WP10_1_RUNTIME_COUNT_KEYS if key not in WP10_1_RUNTIME_DELTAS[case_id]
    )
    case["after"]["counts"][unchanged_key] += 1
    case["after"]["digest"] = stable_digest(case["after"]["counts"])
    probe_evidence_path(tmp_path, step).write_text(json.dumps(payload), encoding="utf-8")
    assert child_evidence_ok(step, tmp_path) == (False, "wp10.1-probe-evidence-result")


@pytest.mark.parametrize("case_id", sorted(WP10_1_RUNTIME_RELATED_IDS))
def test_wp10_1_runtime_contract_binds_required_related_ids(tmp_path: Path, case_id: str) -> None:
    step = STEPS_BY_ID["WP10.1-runtime"]
    payload = wp10_1_evidence(step.step_id)
    case = next(item for item in payload["cases"] if item["id"] == case_id)
    case["related_ids"].pop(next(iter(case["related_ids"])))
    probe_evidence_path(tmp_path, step).write_text(json.dumps(payload), encoding="utf-8")
    assert child_evidence_ok(step, tmp_path) == (False, "wp10.1-probe-evidence-result")


@pytest.mark.parametrize(
    "case_id",
    [
        "G10-05-downgrade-v2",
        "G10-05-downgrade-quarantine",
        "G10-05-downgrade-terminal",
        "G10-05-downgrade-public",
    ],
)
def test_wp10_1_migration_downgrade_requires_complete_snapshot_digest_and_marker(
    tmp_path: Path, case_id: str
) -> None:
    step = STEPS_BY_ID["WP10.1-migration"]
    payload = wp10_1_evidence(step.step_id)
    case = next(item for item in payload["cases"] if item["id"] == case_id)
    case["before"].pop("objects")
    case["after"] = dict(case["before"])
    probe_evidence_path(tmp_path, step).write_text(json.dumps(payload), encoding="utf-8")
    assert child_evidence_ok(step, tmp_path) == (False, "wp10.1-probe-evidence-result")

    payload = wp10_1_evidence(step.step_id)
    case = next(item for item in payload["cases"] if item["id"] == case_id)
    marker_key = {
        "G10-05-downgrade-v2": "audit.document_publication_manifests",
        "G10-05-downgrade-quarantine": "audit.publication_quarantine",
        "G10-05-downgrade-terminal": "ops.outbox_events",
        "G10-05-downgrade-public": "public.documents",
    }[case_id]
    case["before"]["counts"][marker_key] = 0
    unsigned = {key: case["before"][key] for key in ("revision", "counts", "objects")}
    case["before"]["digest"] = stable_digest(unsigned)
    case["after"] = json.loads(json.dumps(case["before"]))
    probe_evidence_path(tmp_path, step).write_text(json.dumps(payload), encoding="utf-8")
    assert child_evidence_ok(step, tmp_path) == (False, "wp10.1-probe-evidence-result")


def test_wp10_1_malformed_snapshot_fails_step_and_marks_following_steps_not_run(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"

    def runner(argv: list[str], *, cwd: Path, env: dict[str, str]) -> FakeCompleted:
        del cwd, env
        step = STEPS_BY_ID[argv[argv.index("--exec-child") + 1]]
        if step.requires_probe_json:
            write_required_probe_evidence(evidence, step.step_id)
        if step.step_id == "WP10.1-migration":
            payload = wp10_1_evidence(step.step_id)
            payload["cases"][0]["before"] = {"counts": "malformed"}
            probe_evidence_path(evidence, step).write_text(json.dumps(payload), encoding="utf-8")
        return FakeCompleted(0, stdout=contract_stdout(step.step_id))

    payload = execute_steps(evidence, runner=runner)  # type: ignore[arg-type]
    assert payload["failed_step"] == "WP10.1-migration"
    failed_index = next(
        index for index, item in enumerate(payload["steps"]) if item["status"] == "failed"
    )
    assert all(item["status"] == "not_run" for item in payload["steps"][failed_index + 1 :])


def test_wp10_1_steps_require_dedicated_evidence_output(tmp_path: Path) -> None:
    for step_id in ("WP10.1-migration", "WP10.1-runtime"):
        step = STEPS_BY_ID[step_id]
        arguments = extra_args(step, tmp_path)
        assert arguments[-2:] == ["--evidence-out", str(probe_evidence_path(tmp_path, step))]


def test_empty_stdout_fails_wp3_contract(tmp_path: Path) -> None:
    def runner(argv: list[str], *, cwd: Path, env: dict[str, str]) -> FakeCompleted:
        return FakeCompleted(0, stdout="")

    payload = execute_steps(tmp_path / "ev", runner=runner)  # type: ignore[arg-type]
    assert payload["status"] == "failed"
    assert payload["failed_step"] == "WP3"
    assert payload["steps"][0]["detail"] == "stdout-contract:wp3-json"


def test_git_paths_file_allows_unavailable_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = tmp_path / "paths.txt"
    listing.write_text(
        "\n".join(sorted(validate_wp10.ALLOWED_WP106A_PATHS)) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UAP_WP10_GIT_PATHS_FILE", str(listing))
    status, extra = validate_wp10.git_changed_paths(tmp_path)
    assert status == "allowed"
    assert extra == []


def test_git_paths_file_missing_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UAP_WP10_GIT_PATHS_FILE", str(tmp_path / "missing.txt"))
    status, _extra = validate_wp10.git_changed_paths(tmp_path)
    assert status == "git-error"
    checks = validate_wp10.evaluate(PLATFORM)
    item = next(c for c in checks if c.name == "WP10.6 changes stay in the authorized surface")
    assert item.passed is False


def test_git_changed_paths_command_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UAP_WP10_GIT_PATHS_FILE", raising=False)
    monkeypatch.setattr("tools.validate_wp10.shutil.which", lambda _command: "/usr/bin/git")
    monkeypatch.setattr(
        "tools.validate_wp10.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout="", stderr="git failed"
        ),
    )

    result = validate_wp10.git_changed_paths(REPO)
    assert result == ("git-error", ["git failed"])
    monkeypatch.setattr(validate_wp10, "git_changed_paths", lambda _repository: result)
    monkeypatch.setattr(validate_wp10_6, "git_changed_paths", lambda _repository: result)

    wp10_checks = validate_wp10.evaluate(PLATFORM)
    wp10_item = next(
        check
        for check in wp10_checks
        if check.name == "WP10.6 changes stay in the authorized surface"
    )
    assert wp10_item.passed is False
    assert wp10_item.actual == {"status": "git-error", "extra": ["git failed"]}

    wp10_6_checks = validate_wp10_6.evaluate(PLATFORM)
    wp10_6_item = next(
        check
        for check in wp10_6_checks
        if check.name == "WP10.6 changes stay in the authorized surface"
    )
    assert wp10_6_item.passed is False
    assert wp10_6_item.actual == {"status": "git-error", "extra": ["git failed"]}


def test_validator_frozen_tree_passes() -> None:
    failed = [
        item.name for item in validate_wp10.evaluate(PLATFORM, git_paths=[]) if not item.passed
    ]
    assert failed == []


def test_validator_detects_second_head() -> None:
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], version_names=["0024_x.py", "0024b.py"])
    linear = next(item for item in checks if item.name == "0001-0024 strictly linear")
    assert linear.passed is False


def test_validator_detects_0025_and_wp11() -> None:
    names = ["0024_wp10_admin_replay.py", "0025_wp11_branch.py"]
    assert validate_wp10.forbidden_versions(names) == ["0025_wp11_branch.py"]
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], version_names=names)
    extra = next(item for item in checks if item.name == "no 0025 or WP11 migration")
    assert extra.passed is False


def test_validator_detects_missing_or_reordered_steps() -> None:
    reordered = (FROZEN_STEPS[-1], *FROZEN_STEPS[:-1])
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], frozen_steps=reordered)
    item = next(c for c in checks if c.name == "frozen WP3→WP10.5 step order")
    assert item.passed is False
    missing = FROZEN_STEPS[:-1]
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], frozen_steps=missing)
    item = next(c for c in checks if c.name == "frozen WP3→WP10.5 step order")
    assert item.passed is False


def test_evaluate_detects_makefile_unwired() -> None:
    makefile = (PLATFORM / "Makefile").read_text(encoding="utf-8")
    broken = makefile.replace("python -m tools.validate_wp10\n", "")
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], makefile=broken)
    item = next(c for c in checks if c.name == "Makefile check wires validate_wp10.py")
    assert item.passed is False


def test_evaluate_detects_workflow_missing_docs_path() -> None:
    workflow = (REPO / ".github/workflows/platform-ci.yml").read_text(encoding="utf-8")
    broken = workflow.replace('"docs/wp10/**"', '"docs/wp9/**"')
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], workflow=broken)
    item = next(c for c in checks if c.name == "workflow paths include docs/wp10/**")
    assert item.passed is False


def test_evaluate_detects_continue_on_error() -> None:
    workflow = (REPO / ".github/workflows/platform-ci.yml").read_text(encoding="utf-8")
    injected = workflow.replace(
        "timeout-minutes: 60", "timeout-minutes: 60\n    continue-on-error: true", 1
    )
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], workflow=injected)
    item = next(c for c in checks if c.name == "workflow has no continue-on-error")
    assert item.passed is False


def test_evaluate_detects_quality_and_integration_gap() -> None:
    workflow = (REPO / ".github/workflows/platform-ci.yml").read_text(encoding="utf-8")
    no_quality = workflow.replace("python -m tools.validate_wp10", "python -m tools.validate_wp9")
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], workflow=no_quality)
    item = next(c for c in checks if c.name == "quality job runs validate_wp10.py")
    assert item.passed is False
    no_probe = workflow.replace(
        "python -m tools.wp10_runtime_probe", "python -m tools.wp9_runtime_probe"
    )
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], workflow=no_probe)
    item = next(c for c in checks if c.name == "integration job runs wp10_runtime_probe.py")
    assert item.passed is False


def test_evaluate_rejects_unclosed_migrator_or_shared_legacy_probe() -> None:
    workflow = (REPO / ".github/workflows/platform-ci.yml").read_text(encoding="utf-8")
    broken = workflow.replace('          close_migrator "$legacy_db"\n', "", 1)
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], workflow=broken)
    expected_check = "isolated WP10 database topology and disposable CI store"
    item = next(c for c in checks if c.name == expected_check)
    assert item.passed is False

    broken = workflow.replace('"$UAP_WP10_LEGACY_PROBE_DB"', '"$UAP_WP10_LEGACY_DB"', 1)
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], workflow=broken)
    item = next(c for c in checks if c.name == expected_check)
    assert item.passed is False


def test_evaluate_rejects_direct_runtime_script_entries() -> None:
    makefile = (PLATFORM / "Makefile").read_text(encoding="utf-8")
    direct_makefile = makefile.replace(
        "python -m tools.wp10_runtime_probe", "python tools/wp10_runtime_probe.py"
    )
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], makefile=direct_makefile)
    item = next(c for c in checks if c.name == "Makefile WP10 runtime entry")
    assert item.passed is False

    workflow = (REPO / ".github/workflows/platform-ci.yml").read_text(encoding="utf-8")
    direct_workflow = workflow.replace(
        "python -m tools.wp10_runtime_probe", "python tools/wp10_runtime_probe.py"
    )
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], workflow=direct_workflow)
    item = next(c for c in checks if c.name == "integration job runs wp10_runtime_probe.py")
    assert item.passed is False

    probe = (PLATFORM / "tools/wp10_runtime_probe.py").read_text(encoding="utf-8")
    direct_probe = probe.replace(
        '        "-m",\n        "tools.wp10_runtime_probe",',
        '        str(TOOLS_DIR / "wp10_runtime_probe.py"),',
    )
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], probe_source=direct_probe)
    item = next(c for c in checks if c.name == "OS argv uses WP10 runtime module entry")
    assert item.passed is False


def test_validator_rejects_parent_or_root_evidence_boundary() -> None:
    probe = (PLATFORM / "tools/wp10_runtime_probe.py").read_text(encoding="utf-8")
    parent_fallback = probe.replace(
        "SOURCE_BOUNDARY = discover_source_boundary(PLATFORM_DIR)",
        "SOURCE_BOUNDARY = PLATFORM_DIR.parent",
    )
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], probe_source=parent_fallback)
    item = next(c for c in checks if c.name == "evidence directory source boundary is fail-closed")
    assert item.passed is False

    root_fallback = probe.replace(
        "SOURCE_BOUNDARY = discover_source_boundary(PLATFORM_DIR)",
        'SOURCE_BOUNDARY = Path("/")',
    )
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], probe_source=root_fallback)
    item = next(c for c in checks if c.name == "evidence directory source boundary is fail-closed")
    assert item.passed is False


def test_validator_rejects_child_without_sibling_import_context() -> None:
    probe = (PLATFORM / "tools/wp10_runtime_probe.py").read_text(encoding="utf-8")
    broken = probe.replace(
        "sys.path.insert(0, str(TOOLS_DIR))",
        "sys.path.insert(0, str(PLATFORM_DIR))",
    )
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], probe_source=broken)
    item = next(c for c in checks if c.name == "exec child restores import context")
    assert item.passed is False


def test_evaluate_detects_unpinned_upload_artifact() -> None:
    workflow = (REPO / ".github/workflows/platform-ci.yml").read_text(encoding="utf-8")
    broken = workflow.replace(
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        "actions/upload-artifact@v4",
    )
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], workflow=broken)
    item = next(c for c in checks if c.name == "CI persists WP10 evidence")
    assert item.passed is False


def test_evaluate_detects_missing_host_git_paths() -> None:
    workflow = (REPO / ".github/workflows/platform-ci.yml").read_text(encoding="utf-8")
    broken = workflow.replace("UAP_WP10_GIT_PATHS_FILE=/tmp/wp10-git-paths.txt", "")
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], workflow=broken)
    item = next(c for c in checks if c.name == "quality job runs validate_wp10.py")
    assert item.passed is False


def test_evaluate_detects_weakened_gate() -> None:
    workflow = (REPO / ".github/workflows/platform-ci.yml").read_text(encoding="utf-8")
    weakened = workflow.replace(
        "needs: [quality, security, integration]",
        "needs: [quality, integration]",
    )
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], workflow=weakened)
    item = next(c for c in checks if c.name == "gate depends on quality/security/integration")
    assert item.passed is False


def test_evaluate_detects_forbidden_git_path() -> None:
    checks = validate_wp10.evaluate(PLATFORM, git_paths=["platform/src/uap_platform/secret.py"])
    item = next(c for c in checks if c.name == "WP10.6 changes stay in the authorized surface")
    assert item.passed is False


@pytest.mark.parametrize("path", sorted(validate_wp10.ALLOWED_WP106D_PATHS))
def test_wp10_6_d_paths_are_individually_allowed(path: str) -> None:
    assert validate_wp10.classify_git_paths([path]) == ("allowed", [])
    checks = validate_wp10_6.evaluate(PLATFORM, paths=[path])
    item = next(c for c in checks if c.name == "WP10.6 changes stay in the authorized surface")
    assert item.passed is True


def test_wp10_6_d_paths_and_prior_stage_paths_are_allowed() -> None:
    d_paths = sorted(validate_wp10.ALLOWED_WP106D_PATHS)
    prior_paths = [
        next(iter(validate_wp10.ALLOWED_WP106A_PATHS)),
        next(iter(validate_wp10.ALLOWED_WP106B_PATHS)),
        next(iter(validate_wp10.ALLOWED_WP106C_PATHS)),
    ]
    paths = [*d_paths, *prior_paths]
    assert validate_wp10.classify_git_paths(paths) == ("allowed", [])
    checks = validate_wp10_6.evaluate(PLATFORM, paths=paths)
    item = next(c for c in checks if c.name == "WP10.6 changes stay in the authorized surface")
    assert item.passed is True


@pytest.mark.parametrize(
    "path",
    [
        "docs/wp10/acceptance-cases.md",
        "docs/wp10/permissions.md",
        "docs/wp10/unreviewed-status.md",
        "docs/wp11/README.md",
        "platform/alembic/versions/0024_wp10_admin_replay.py",
        "platform/src/uap_platform/public_api/server.py",
    ],
)
def test_wp10_6_d_path_scope_rejects_unapproved_paths(path: str) -> None:
    allowed = sorted(validate_wp10.ALLOWED_WP106D_PATHS)
    assert validate_wp10.classify_git_paths([*allowed, path]) == ("forbidden", [path])
    checks = validate_wp10_6.evaluate(PLATFORM, paths=[*allowed, path])
    item = next(c for c in checks if c.name == "WP10.6 changes stay in the authorized surface")
    assert item.passed is False
    assert item.actual == {"status": "forbidden", "extra": [path]}


@pytest.mark.parametrize("path", ["/absolute/path.py", "../outside.py"])
def test_wp10_6_d_path_scope_rejects_invalid_paths(path: str) -> None:
    assert validate_wp10.classify_git_paths([path]) == ("forbidden", [path])
    checks = validate_wp10_6.evaluate(PLATFORM, paths=[path])
    item = next(c for c in checks if c.name == "WP10.6 changes stay in the authorized surface")
    assert item.passed is False


@pytest.mark.parametrize("path", sorted(validate_wp10.ALLOWED_WP106_FINAL_GATE_PATHS))
def test_final_gate_remediation_paths_are_individually_allowed(path: str) -> None:
    assert validate_wp10.classify_git_paths([path]) == ("allowed", [])
    checks = validate_wp10_6.evaluate(PLATFORM, paths=[path])
    item = next(c for c in checks if c.name == "WP10.6 changes stay in the authorized surface")
    assert item.passed is True


def test_current_closeout_candidate_paths_are_allowed() -> None:
    paths = sorted(
        {
            "platform/scripts/verify-migration-chain.sh",
            "platform/tests/test_wp10_4_public_api.py",
            "platform/tests/test_wp10_5_admin_api.py",
            "platform/tests/test_wp10_6_chain.py",
            "platform/tests/test_wp10_6_performance.py",
            "platform/tools/validate_wp10.py",
            "platform/tools/validate_wp10_4.py",
            "platform/tools/validate_wp10_5.py",
            "platform/tools/validate_wp10_6.py",
        }
    )
    assert validate_wp10.classify_git_paths(paths) == ("allowed", [])
    checks = validate_wp10_6.evaluate(PLATFORM, paths=paths)
    item = next(c for c in checks if c.name == "WP10.6 changes stay in the authorized surface")
    assert item.passed is True


@pytest.mark.parametrize(
    "path",
    [
        "platform/scripts/verify-migrator-failure-close.sh",
        "platform/tools/validate_wp10_2.py",
        "platform/tools/validate_wp10_3.py",
    ],
)
def test_final_gate_scope_rejects_adjacent_paths(path: str) -> None:
    allowed = sorted(validate_wp10.ALLOWED_WP106_FINAL_GATE_PATHS)
    assert validate_wp10.classify_git_paths([*allowed, path]) == ("forbidden", [path])
    checks = validate_wp10_6.evaluate(PLATFORM, paths=[*allowed, path])
    item = next(c for c in checks if c.name == "WP10.6 changes stay in the authorized surface")
    assert item.passed is False
    assert item.actual == {"status": "forbidden", "extra": [path]}


@pytest.mark.parametrize("path", sorted(validate_wp10.ALLOWED_WP106F3_PATHS))
def test_f3_image_paths_are_individually_allowed(path: str) -> None:
    assert validate_wp10.classify_git_paths([path]) == ("allowed", [])
    checks = validate_wp10_6.evaluate(PLATFORM, paths=[path])
    item = next(c for c in checks if c.name == "WP10.6 changes stay in the authorized surface")
    assert item.passed is True


def test_f3_paths_combine_with_prior_authorized_stages() -> None:
    paths = sorted(
        validate_wp10.ALLOWED_WP106A_PATHS
        | validate_wp10.ALLOWED_WP106B_PATHS
        | validate_wp10.ALLOWED_WP106C_PATHS
        | validate_wp10.ALLOWED_WP106D_PATHS
        | validate_wp10.ALLOWED_WP106_FINAL_GATE_PATHS
        | validate_wp10.ALLOWED_WP106F3_PATHS
    )
    assert validate_wp10.classify_git_paths(paths) == ("allowed", [])
    checks = validate_wp10_6.evaluate(PLATFORM, paths=paths)
    item = next(c for c in checks if c.name == "WP10.6 changes stay in the authorized surface")
    assert item.passed is True


@pytest.mark.parametrize("path", sorted(validate_wp10.ALLOWED_WP106F4_PATHS))
def test_f4_format_paths_are_individually_allowed(path: str) -> None:
    assert validate_wp10.classify_git_paths([path]) == ("allowed", [])
    checks = validate_wp10_6.evaluate(PLATFORM, paths=[path])
    item = next(c for c in checks if c.name == "WP10.6 changes stay in the authorized surface")
    assert item.passed is True


def test_f4_paths_combine_with_all_prior_authorized_stages() -> None:
    paths = sorted(
        validate_wp10.ALLOWED_WP106A_PATHS
        | validate_wp10.ALLOWED_WP106B_PATHS
        | validate_wp10.ALLOWED_WP106C_PATHS
        | validate_wp10.ALLOWED_WP106D_PATHS
        | validate_wp10.ALLOWED_WP106_FINAL_GATE_PATHS
        | validate_wp10.ALLOWED_WP106F3_PATHS
        | validate_wp10.ALLOWED_WP106F4_PATHS
    )
    assert validate_wp10.classify_git_paths(paths) == ("allowed", [])
    checks = validate_wp10_6.evaluate(PLATFORM, paths=paths)
    item = next(c for c in checks if c.name == "WP10.6 changes stay in the authorized surface")
    assert item.passed is True


@pytest.mark.parametrize(
    "path",
    [
        "platform/alembic/script.py.mako",
        "platform/alembic/versions/0008_ai_model_governance_extra.py",
        "platform/src/uap_platform/collectors/contracts_extra.py",
        "platform/tests/test_wp9_decisions_extra.py",
        "platform/tools/wp9_6_runtime_probe_extra.py",
        "platform/tools/../tools/wp9_6_runtime_probe.py",
        "/platform/tools/wp9_6_runtime_probe.py",
        "../platform/tools/wp9_6_runtime_probe.py",
    ],
)
def test_f4_scope_rejects_adjacent_approximate_and_invalid_paths(path: str) -> None:
    allowed = sorted(validate_wp10.ALLOWED_WP106F4_PATHS)
    assert validate_wp10.classify_git_paths([*allowed, path]) == ("forbidden", [path])
    checks = validate_wp10_6.evaluate(PLATFORM, paths=[*allowed, path])
    item = next(c for c in checks if c.name == "WP10.6 changes stay in the authorized surface")
    assert item.passed is False
    assert item.actual == {"status": "forbidden", "extra": [path]}


def test_f4_scope_matches_reviewed_format_target_manifest() -> None:
    assert len(validate_wp10.ALLOWED_WP106F4_PATHS) == 42
    assert validate_wp10.ALLOWED_WP106F4_PATHS == frozenset(
        {
            "platform/alembic/env.py",
            "platform/alembic/versions/0008_ai_model_governance.py",
            "platform/src/uap_platform/collectors/contracts.py",
            "platform/src/uap_platform/collectors/workflow.py",
            "platform/src/uap_platform/documents/contracts.py",
            "platform/src/uap_platform/model_governance/persistence.py",
            "platform/src/uap_platform/model_governance/workflow.py",
            "platform/src/uap_platform/object_registry.py",
            "platform/src/uap_platform/review/canonical.py",
            "platform/tests/test_config.py",
            "platform/tests/test_configure_roles.py",
            "platform/tests/test_document_extraction.py",
            "platform/tests/test_document_persistence.py",
            "platform/tests/test_document_workflow.py",
            "platform/tests/test_model_governance.py",
            "platform/tests/test_object_registry.py",
            "platform/tests/test_source_run_store.py",
            "platform/tests/test_subtitle_extraction.py",
            "platform/tests/test_wp10_foundation.py",
            "platform/tests/test_wp8_claim_bundle.py",
            "platform/tests/test_wp8_claim_handler.py",
            "platform/tests/test_wp8_relation_reject.py",
            "platform/tests/test_wp9_cases.py",
            "platform/tests/test_wp9_claims.py",
            "platform/tests/test_wp9_decisions.py",
            "platform/tools/build_wp3_evidence.py",
            "platform/tools/build_wp4_evidence.py",
            "platform/tools/ensure_model_governance_role.py",
            "platform/tools/object_backup.py",
            "platform/tools/validate_wp6.py",
            "platform/tools/validate_wp7.py",
            "platform/tools/wp4_runtime_probe.py",
            "platform/tools/wp5_runtime_probe.py",
            "platform/tools/wp6_runtime_probe.py",
            "platform/tools/wp7_runtime_probe.py",
            "platform/tools/wp8_1_runtime_probe.py",
            "platform/tools/wp8_3_runtime_probe.py",
            "platform/tools/wp8_5_runtime_probe.py",
            "platform/tools/wp9_2_runtime_probe.py",
            "platform/tools/wp9_3_runtime_probe.py",
            "platform/tools/wp9_4_runtime_probe.py",
            "platform/tools/wp9_6_runtime_probe.py",
        }
    )


def test_f3_image_security_versions_are_immutably_pinned() -> None:
    versions = (PLATFORM / ".env.versions").read_text(encoding="utf-8")
    app = (PLATFORM / "Dockerfile").read_text(encoding="utf-8")
    postgres = (PLATFORM / "postgres/Dockerfile").read_text(encoding="utf-8")
    object_store = (PLATFORM / "object-store/Dockerfile").read_text(encoding="utf-8")

    assert "golang:1.26-alpine3.23@sha256:" in versions
    assert "UAP_SEAWEEDFS_COMMIT=e919bec9d194b61aa07cef75f46b207dd687a019" in versions
    assert "'libuuid>=2.41.6-r1'" in app
    assert "'libuuid>=2.42.3-r1'" in postgres
    assert "ARG UAP_GO_IMAGE=golang:1.26-alpine3.23@sha256:" in object_store
    assert "ARG UAP_SEAWEEDFS_COMMIT=e919bec9d194b61aa07cef75f46b207dd687a019" in object_store
    assert "ARG UAP_GRPC_VERSION=v1.85.0-dev.0.20260825072537-93e31b48545e" in object_store
    assert 'go get "google.golang.org/grpc@${UAP_GRPC_VERSION}"' in object_store


@pytest.mark.parametrize(
    "path",
    [
        "Dockerfile",
        "platform/Dockerfile.dev",
        "platform/postgres/compose.yaml",
        "platform/object-store/entrypoint.sh",
        "platform/tools/validate_platform_extra.py",
        "platform/tests/test_platform_policy_extra.py",
    ],
)
def test_f3_scope_rejects_adjacent_image_paths(path: str) -> None:
    allowed = sorted(validate_wp10.ALLOWED_WP106F3_PATHS)
    assert validate_wp10.classify_git_paths([*allowed, path]) == ("forbidden", [path])
    checks = validate_wp10_6.evaluate(PLATFORM, paths=[*allowed, path])
    item = next(c for c in checks if c.name == "WP10.6 changes stay in the authorized surface")
    assert item.passed is False
    assert item.actual == {"status": "forbidden", "extra": [path]}


def test_both_validators_fail_closed_when_git_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        validate_wp10,
        "git_changed_paths",
        lambda _repository: ("git-unavailable", []),
    )
    wp10_item = next(
        c
        for c in validate_wp10.evaluate(PLATFORM)
        if c.name == "WP10.6 changes stay in the authorized surface"
    )
    assert wp10_item.passed is False

    monkeypatch.setattr(
        validate_wp10_6,
        "git_changed_paths",
        lambda _repository: ("git-unavailable", []),
    )
    wp10_6_item = next(
        c
        for c in validate_wp10_6.evaluate(PLATFORM)
        if c.name == "WP10.6 changes stay in the authorized surface"
    )
    assert wp10_6_item.passed is False
    assert wp10_6_item.actual == {"status": "git-unavailable", "extra": []}


def test_evaluate_detects_probe_without_exec_child() -> None:
    probe = (PLATFORM / "tools/wp10_runtime_probe.py").read_text(encoding="utf-8")
    broken = probe.replace("--exec-child", "--no-exec")
    checks = validate_wp10.evaluate(PLATFORM, git_paths=[], probe_source=broken)
    item = next(c for c in checks if c.name == "orchestrator fail-closed")
    assert item.passed is False


def test_git_changed_paths_unavailable_without_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UAP_WP10_GIT_PATHS_FILE", raising=False)
    status, extra = validate_wp10.git_changed_paths(tmp_path)
    assert status == "git-unavailable"
    assert extra == []


def test_validator_cli_does_not_fail_import() -> None:
    env = {**os.environ, "PYTHONPATH": str(PLATFORM)}
    env.pop("UAP_WP10_GIT_PATHS_FILE", None)
    invocations = (
        [sys.executable, str(PLATFORM / "tools/validate_wp10.py"), str(PLATFORM)],
        [sys.executable, "-m", "tools.validate_wp10", str(PLATFORM)],
    )
    for argv in invocations:
        result = subprocess.run(  # noqa: S603
            argv,
            cwd=str(PLATFORM),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        combined = result.stdout + result.stderr
        assert "ModuleNotFoundError" not in combined
        assert "No module named 'tools'" not in combined
        assert "WP10 validation" in combined or "WP10 static" in combined


def test_makefile_wiring_helpers() -> None:
    makefile = (PLATFORM / "Makefile").read_text(encoding="utf-8")
    root = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "python -m tools.validate_wp10" in makefile
    assert "wp10-runtime:" in makefile
    assert "python -m tools.wp10_runtime_probe" in makefile
    assert "wp10_runtime_probe.py" not in makefile
    assert "wp10-runtime:" in root
    body = validate_wp10.makefile_check_body(makefile)
    assert "validate_wp10" in body
    assert "wp10_runtime_probe.py" not in body


def test_workflow_persists_evidence() -> None:
    workflow = (REPO / ".github/workflows/platform-ci.yml").read_text(encoding="utf-8")
    integration = validate_wp10.workflow_job(workflow, "integration")
    quality = validate_wp10.workflow_job(workflow, "quality")
    assert "upload-artifact" in integration
    assert "wp10-runtime-evidence" in integration
    assert "--volume" in integration
    assert "ea165f8d65b6e75b540449e92b4886f43607fa02" in integration
    assert "UAP_WP10_GIT_PATHS_FILE" in quality
    assert "git cat-file -e" in quality
    assert "fetch-depth: 0" in quality


def test_execute_does_not_skip_remaining_as_passed(tmp_path: Path) -> None:
    def runner(argv: list[str], *, cwd: Path, env: dict[str, str]) -> FakeCompleted:
        return FakeCompleted(1, stdout="failed")

    payload = execute_steps(tmp_path, runner=runner)  # type: ignore[arg-type]
    assert all(item["status"] != "skipped" for item in payload["steps"])
    assert all(item["status"] != "passed" for item in payload["steps"])
    assert payload["steps"][0]["status"] == "failed"
    assert all(item["status"] == "not_run" for item in payload["steps"][1:])
    assert len(payload["steps"]) == len(FROZEN_STEPS)


def test_plan_does_not_invoke_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> FakeCompleted:
        raise AssertionError("subprocess must not run in plan mode")

    monkeypatch.setattr("tools.wp10_runtime_probe.run_subprocess", boom)
    assert probe_main(["--list"]) == 0


def test_allowed_wp106a_paths_are_the_frozen_33() -> None:
    security_27 = {
        "Makefile",
        "platform/Makefile",
        ".github/workflows/platform-ci.yml",
        "platform/tools/wp10_runtime_probe.py",
        "platform/tools/validate_wp10.py",
        "platform/tests/test_wp10_6_chain.py",
        "platform/src/uap_platform/publishing/service.py",
        "platform/src/uap_platform/public_api/config.py",
        "platform/src/uap_platform/public_api/service.py",
        "platform/src/uap_platform/public_api/handler.py",
        "platform/src/uap_platform/admin_api/config.py",
        "platform/src/uap_platform/admin_api/service.py",
        "platform/src/uap_platform/admin_api/handler.py",
        "platform/src/uap_platform/admin_api/errors.py",
        ".gitleaks.toml",
        "platform/tests/test_wp10_4_public_api.py",
        "platform/tests/test_wp10_5_admin_api.py",
        "platform/tests/test_wp10_5_oidc.py",
        "platform/tools/wp10_1_migration_probe.py",
        "platform/tools/wp10_1_runtime_probe.py",
        "platform/tools/wp10_2_migration_probe.py",
        "platform/tools/wp10_2_runtime_probe.py",
        "platform/tools/wp10_3_migration_probe.py",
        "platform/tools/wp10_3_runtime_probe.py",
        "platform/tools/wp10_4_migration_probe.py",
        "platform/tools/wp10_4_runtime_probe.py",
        "platform/tools/wp10_5_runtime_probe.py",
    }
    extra_g10_25 = {
        "platform/tools/wp10_stage_revisions.py",
        "platform/tools/wp10_object_store_guard.py",
        "platform/tests/test_wp10_stage_revisions.py",
        "platform/tests/test_wp10_object_store_guard.py",
        "platform/scripts/g10-25-disposable-object-store.sh",
        "platform/compose.g10-25-object-store.yaml",
    }
    expected = security_27 | extra_g10_25
    assert len(security_27) == 27
    assert len(extra_g10_25) == 6
    assert validate_wp10.ALLOWED_WP106A_PATHS == frozenset(expected)
    assert len(validate_wp10.ALLOWED_WP106A_PATHS) == 33
    status, extra = validate_wp10.classify_git_paths(sorted(expected))
    assert status == "allowed"
    assert extra == []
    status, extra = validate_wp10.classify_git_paths(
        [*sorted(expected), "docs/wp10/acceptance-cases.md"]
    )
    assert status == "forbidden"
    assert extra == ["docs/wp10/acceptance-cases.md"]


def test_plan_mode_does_not_claim_live_passed(capsys: pytest.CaptureFixture[str]) -> None:
    assert probe_main(["--plan"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "plan"
    assert payload["live"] == "NOT RUN / NOT PROVEN"
    assert payload["object_store_contract"]["status"] == "plan"
    assert payload["object_store_contract"]["live"] == "NOT RUN / NOT PROVEN"
    assert payload["object_store_contract"]["status"] != "passed"


def test_wp10_1_migration_probe_does_not_upgrade_head() -> None:
    source = (PLATFORM / "tools/wp10_1_migration_probe.py").read_text(encoding="utf-8")
    assert 'upgrade", "head"' not in source
    assert "upgrade', 'head'" not in source
    assert "upgrade head" not in source
    assert "STAGE_REVISION" in source
    assert "REVISION_0020" in source
    assert "conninfo_to_dict" not in source
    assert "make_conninfo(libpq_url(database_url), user=role, password=password)" in source
    assert validate_wp10.migration_probes_using_head(PLATFORM) == []


def test_legacy_migration_chain_pins_wp9_boundary_without_weakening_checks() -> None:
    source = (PLATFORM / "scripts/verify-migration-chain.sh").read_text(encoding="utf-8")
    assert "legacy_head=0019_manual_claims_binding" in source
    assert source.count('alembic_step -x role=migrator upgrade "$legacy_head"') == 10
    assert "upgrade head" not in source
    for marker in (
        "review_grant_superseded_blocks_downgrade",
        "knowledge_claim_backfill_required",
        "session_replication_role = replica",
        "ALTER ROLE uap_migrator NOLOGIN",
        "SELECT count(*) FROM audit.principals",
        "WP10 revisions 0020-0024 are covered by their stage probes and G10-26",
    ):
        assert marker in source


def test_stage_mismatch_fails_closed_and_halts_later_steps(tmp_path: Path) -> None:
    class BlockingStage:
        def ensure(self, step_id: str) -> dict[str, object]:
            if step_id == "WP10.1-runtime":
                return {"status": "failed", "detail": "revision mismatch: expected 0020"}
            return {"status": "passed", "step_id": step_id, "advanced": False}

    def runner(argv: list[str], *, cwd: Path, env: dict[str, str]) -> FakeCompleted:
        step = STEPS_BY_ID[argv[argv.index("--exec-child") + 1]]
        if step.requires_probe_json:
            write_required_probe_evidence(tmp_path / "ev", step.step_id)
        return FakeCompleted(0, stdout=contract_stdout(step.step_id))

    payload = execute_steps(
        tmp_path / "ev",
        runner=runner,  # type: ignore[arg-type]
        stage_gate=BlockingStage(),
    )
    assert payload["status"] == "failed"
    assert payload["failed_step"] == "WP10.1-runtime"
    statuses = {item["step_id"]: item["status"] for item in payload["steps"]}
    assert statuses["WP10.1-migration"] == "passed"
    assert statuses["WP10.1-runtime"] == "failed"
    assert statuses["WP10.2-migration"] == "not_run"
    assert statuses["WP10.5-runtime"] == "not_run"


def test_object_store_preflight_rejects_before_any_step(tmp_path: Path) -> None:
    class RejectingStore:
        def preflight(self) -> dict[str, object]:
            return {"status": "failed", "detail": "refusing shared object-store endpoint"}

        def cleanup(self) -> dict[str, object]:
            return {"status": "not_run", "residue": None, "deleted": 0}

    calls: list[str] = []

    def runner(argv: list[str], *, cwd: Path, env: dict[str, str]) -> FakeCompleted:
        calls.append(argv[argv.index("--exec-child") + 1])
        return FakeCompleted(0, stdout=contract_stdout("WP3"))

    payload = execute_steps(
        tmp_path / "ev",
        runner=runner,  # type: ignore[arg-type]
        object_store_gate=RejectingStore(),
    )
    assert calls == []
    assert payload["failed_step"] == "object-store-preflight"
    assert payload["object_store"]["cleanup"]["deleted"] == 0
    assert all(item["status"] == "not_run" for item in payload["steps"])


def test_runtime_db_is_advanced_in_frozen_order(tmp_path: Path) -> None:
    seen: list[str] = []

    class RecordingStage:
        def ensure(self, step_id: str) -> dict[str, object]:
            seen.append(step_id)
            return {"status": "passed", "step_id": step_id, "advanced": step_id.endswith("runtime")}

    def runner(argv: list[str], *, cwd: Path, env: dict[str, str]) -> FakeCompleted:
        step = STEPS_BY_ID[argv[argv.index("--exec-child") + 1]]
        if step.requires_probe_json:
            write_required_probe_evidence(tmp_path / "ev", step.step_id)
            if step.step_id == "WP10.5-runtime":
                matrix_evidence_path(tmp_path / "ev").write_text(
                    '{"status": "passed"}\n', encoding="utf-8"
                )
        return FakeCompleted(0, stdout=contract_stdout(step.step_id))

    payload = execute_steps(
        tmp_path / "ev",
        runner=runner,  # type: ignore[arg-type]
        stage_gate=RecordingStage(),
    )
    assert payload["status"] == "passed"
    assert seen[0] == "WP3"
    assert seen[seen.index("WP9.6") + 1] == "WP10.1-migration"
    assert seen[seen.index("WP10.1-migration") + 1] == "WP10.1-runtime"
    assert seen[seen.index("WP10.1-runtime") + 1] == "WP10.2-migration"
    assert seen[-1] == "WP10.5-runtime"
    assert payload["step_counts"]["passed"] == len(FROZEN_STEPS)
    assert payload["step_counts"]["not_run"] == 0


def test_publication_service_and_loop_cover_claim_apply_fail() -> None:
    from datetime import UTC, datetime
    from threading import Event
    from typing import Any, cast
    from unittest.mock import patch
    from uuid import UUID

    from uap_platform.publishing import (
        PublicationApplyResult,
        PublicationClaim,
        PublicationFailureResult,
        PublicationService,
    )
    from uap_platform.publishing.loop import PublisherLoop, build_loop
    from uap_platform.publishing.loop import main as publisher_main
    from uap_platform.publishing.service import stable_failure_code, stable_failure_summary

    event_id = UUID("00000000-0000-4000-8000-000000000021")
    lease = UUID("00000000-0000-4000-8000-000000000022")
    aggregate = UUID("00000000-0000-4000-8000-000000000023")
    published_at = datetime(2026, 8, 31, tzinfo=UTC)
    claim_row = (
        event_id,
        None,
        "document",
        aggregate,
        "publication.document.granted",
        "document:1",
        {"grant_id": str(event_id)},
        1,
        lease,
        published_at,
    )
    apply_row = (event_id, published_at, "digest", 1, False)
    fail_row = (event_id, 2, "retry_wait", published_at, None, False)

    class ScriptedCursor:
        def __init__(self, owner: ScriptedConnection) -> None:
            self._owner = owner
            self._row = None
            self._rows = []

        def execute(self, sql: str, params: object = None) -> None:
            result = self._owner.next_result()
            if isinstance(result, list):
                self._rows = result
                self._row = result[0] if result else None
            else:
                self._row = result
                self._rows = [] if result is None else [result]

        def fetchall(self) -> list[object]:
            return list(self._rows)

        def fetchone(self) -> object:
            return self._row

        def __enter__(self) -> ScriptedCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class ScriptedConnection:
        def __init__(self, results: list[object]) -> None:
            self._results = list(results)
            self.commits = 0
            self.rollbacks = 0
            self.closed = False

        def next_result(self) -> object:
            return self._results.pop(0)

        def cursor(self) -> ScriptedCursor:
            return ScriptedCursor(self)

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            self.rollbacks += 1

        def close(self) -> None:
            self.closed = True

    connection = ScriptedConnection([[claim_row], apply_row, fail_row])
    service = PublicationService(cast(Any, connection), dispatcher_id="publisher-test")
    assert service.connection is cast(Any, connection)
    claims = service.claim()
    assert len(claims) == 1
    claim = claims[0]
    applied = service.apply(claim)
    assert applied.event_id == event_id
    assert applied.projection_digest == "digest"
    failed = service.fail(claim, error_code="publication_lease_lost", retry_delay_seconds=5)
    assert failed.outcome == "retry_wait"
    assert stable_failure_summary("publication_grant_missing").startswith("publication grant")
    assert stable_failure_summary("unknown") == "publication database dependency failed"

    class Diagnostic:
        def __init__(self, primary: str | None) -> None:
            self.message_primary = primary

    class MappedError(Exception):
        def __init__(self, primary: str | None, sqlstate: str | None) -> None:
            self.diag = Diagnostic(primary)
            self.sqlstate = sqlstate

    assert stable_failure_code(MappedError("publication_dependency_not_ready", None)) == (
        "publication_dependency_not_ready"
    )
    assert stable_failure_code(MappedError("publication_retry_exhausted", None)) == (
        "publication_retry_exhausted"
    )
    assert stable_failure_code(MappedError("other", "40001")) == "publication_database_unavailable"
    assert stable_failure_code(MappedError(None, None)) == "publication_database_unavailable"

    empty = ScriptedConnection([[]])
    empty_service = PublicationService(cast(Any, empty), dispatcher_id="empty")
    assert empty_service.claim() == []
    with pytest.raises(RuntimeError, match="invalid shape"):
        PublicationService._claim(("too", "short"))
    with pytest.raises(RuntimeError, match="no result"):
        PublicationService(cast(Any, ScriptedConnection([None])), dispatcher_id="x").apply(claim)
    with pytest.raises(RuntimeError, match="no result"):
        PublicationService(cast(Any, ScriptedConnection([None])), dispatcher_id="x").fail(
            claim, error_code="publication_lease_lost"
        )

    class ApplyingService:
        connection = ScriptedConnection([])

        def __init__(self) -> None:
            self.connection = ScriptedConnection([])
            self._claim = PublicationClaim(
                event_id=event_id,
                causation_job_id=None,
                aggregate_type="document",
                aggregate_id=aggregate,
                event_type="publication.document.granted",
                event_key="document:1",
                payload={"ok": True},
                attempt_no=1,
                lease_token=lease,
                lease_expires_at=published_at,
            )

        def claim(self) -> list[PublicationClaim]:
            return [self._claim]

        def apply(self, _claim: PublicationClaim) -> PublicationApplyResult:
            return PublicationApplyResult(
                event_id=event_id,
                published_at=published_at,
                projection_digest="ok",
                attempt_no=1,
                replayed=False,
            )

        def fail(self, *_args: object, **_kwargs: object) -> PublicationFailureResult:
            raise AssertionError("apply path must not fail")

    success_service = ApplyingService()
    success_loop = PublisherLoop(cast(Any, success_service))
    assert success_service.connection.commits == 0
    assert success_loop.run_once() == 1
    assert success_service.connection.commits == 2

    class RetryableApply(ApplyingService):
        def apply(self, _claim: PublicationClaim) -> PublicationApplyResult:
            raise MappedError("publication_dependency_not_ready", None)

        def fail(self, *_args: object, **kwargs: object) -> PublicationFailureResult:
            assert kwargs["terminal"] is False
            return PublicationFailureResult(
                event_id=event_id,
                attempt_no=2,
                outcome="retry_wait",
                available_at=published_at,
                terminal_at=None,
                replayed=False,
            )

    retry_service = RetryableApply()
    retry_loop = PublisherLoop(cast(Any, retry_service))
    assert retry_loop.run_once() == 1
    assert retry_service.connection.rollbacks == 1
    assert retry_service.connection.commits == 2

    class TerminalApply(ApplyingService):
        def apply(self, _claim: PublicationClaim) -> PublicationApplyResult:
            raise MappedError("publication_manifest_invalid", None)

        def fail(self, *_args: object, **kwargs: object) -> PublicationFailureResult:
            assert kwargs["terminal"] is True
            return PublicationFailureResult(
                event_id=event_id,
                attempt_no=2,
                outcome="terminal",
                available_at=None,
                terminal_at=published_at,
                replayed=False,
            )

    terminal_loop = PublisherLoop(cast(Any, TerminalApply()))
    assert terminal_loop.run_once() == 1

    class LeaseLostFail(ApplyingService):
        def apply(self, _claim: PublicationClaim) -> PublicationApplyResult:
            raise MappedError("boom", "08006")

        def fail(self, *_args: object, **_kwargs: object) -> PublicationFailureResult:
            raise MappedError("publication_lease_lost", None)

    lost_service = LeaseLostFail()
    lost = PublisherLoop(cast(Any, lost_service))
    assert lost.run_once() == 1
    assert lost_service.connection.rollbacks == 2

    class HardFail(ApplyingService):
        def apply(self, _claim: PublicationClaim) -> PublicationApplyResult:
            raise MappedError("boom", None)

        def fail(self, *_args: object, **_kwargs: object) -> PublicationFailureResult:
            raise MappedError("publication_database_unavailable", "08000")

    with pytest.raises(MappedError):
        PublisherLoop(cast(Any, HardFail())).run_once()

    class IdleService(ApplyingService):
        def claim(self) -> list[PublicationClaim]:
            return []

    idle = PublisherLoop(cast(Any, IdleService()), idle_seconds=0)

    class StopSoon(Event):
        def wait(self, timeout: float | None = None) -> bool:
            del timeout
            self.set()
            return True

    stop = StopSoon()
    idle.run_forever(stop)
    assert stop.is_set()

    with patch("uap_platform.publishing.loop.psycopg.connect", return_value=connection):
        built = build_loop("postgresql://uap_publisher@db/uap", dispatcher_id="d", lease_seconds=30)
    assert isinstance(built, PublisherLoop)

    with patch.dict("os.environ", {}, clear=False):
        from os import environ

        environ.pop("UAP_DATABASE_URL", None)
        with pytest.raises(SystemExit):
            publisher_main()

    idle_service = IdleService()
    fake_loop = PublisherLoop(cast(Any, idle_service))
    with (
        patch.dict("os.environ", {"UAP_DATABASE_URL": "postgresql+psycopg://uap_publisher@db/uap"}),
        patch("uap_platform.publishing.loop.build_loop", return_value=fake_loop),
        patch("sys.argv", ["publisher", "--once"]),
    ):
        publisher_main()
    assert idle_service.connection.closed is True

    forever_loop = PublisherLoop(cast(Any, IdleService()), idle_seconds=0)
    forever_stop = Event()
    forever_stop.set()
    with (
        patch.dict("os.environ", {"UAP_DATABASE_URL": "postgresql://uap_publisher@db/uap"}),
        patch("uap_platform.publishing.loop.build_loop", return_value=forever_loop),
        patch("uap_platform.publishing.loop.Event", return_value=forever_stop),
        patch("sys.argv", ["publisher"]),
    ):
        publisher_main()


def test_public_and_admin_servers_dispatch_and_shutdown() -> None:
    from io import BytesIO
    from typing import Any, cast
    from unittest.mock import MagicMock, patch

    from uap_platform.admin_api.oidc import TokenError
    from uap_platform.admin_api.server import main as admin_main
    from uap_platform.admin_api.server import make_handler as admin_make_handler
    from uap_platform.public_api.handler import HttpResponse as PublicResponse
    from uap_platform.public_api.server import main as public_main
    from uap_platform.public_api.server import make_handler as public_make_handler

    class FakePublicApp:
        def handle(self, method: str, target: str, headers: object) -> PublicResponse:
            del target, headers
            return PublicResponse(
                status=200 if method == "GET" else 404,
                headers={"Content-Type": "application/json"},
                body=b'{"status":"ok"}',
            )

    handler_cls = public_make_handler(cast(Any, FakePublicApp()))
    handler: Any = object.__new__(handler_cls)
    handler.path = "/healthz"
    handler.headers = {"X-Request-ID": "00000000-0000-4000-8000-000000000099"}
    handler.wfile = BytesIO()
    sent: list[tuple[str, object]] = []
    handler.send_response = lambda code: sent.append(("status", code))
    handler.send_header = lambda name, value: sent.append((name, value))
    handler.end_headers = lambda: sent.append(("end", None))
    handler.do_GET()
    handler.do_POST()
    handler.do_PUT()
    handler.do_PATCH()
    handler.do_DELETE()
    handler.do_OPTIONS()
    handler.do_HEAD()
    handler.send_error(500, "no", "no")
    handler.log_message("%s", "ignored")
    assert ("status", 200) in sent
    assert handler.wfile.getvalue()

    class FakeAdminApp:
        def handle(
            self, method: str, target: str, headers: object, body: bytes | None
        ) -> PublicResponse:
            del method, target, headers
            payload = b"{}" if body == b"" or body is None else body
            return PublicResponse(
                status=200,
                headers={"Content-Type": "application/json"},
                body=payload or b'{"ok":true}',
            )

    admin_cls = admin_make_handler(cast(Any, FakeAdminApp()))
    admin: Any = object.__new__(admin_cls)
    admin.path = "/healthz"
    admin.headers = {"Content-Length": "2", "Authorization": "Bearer x"}
    admin.rfile = BytesIO(b"{}")
    admin.wfile = BytesIO()
    admin.send_response = lambda code: None
    admin.send_header = lambda *_a: None
    admin.end_headers = lambda: None
    admin.do_POST()
    admin.headers = {"Content-Length": "nope"}
    admin.do_GET()
    admin.headers = {"Content-Length": "-1"}
    admin.do_PUT()
    admin.headers = {"Content-Length": "0"}
    admin.do_PATCH()
    admin.do_DELETE()
    admin.do_OPTIONS()
    admin.do_HEAD()
    admin.send_error(400)
    admin.log_message("%s", "ignored")

    public_settings = MagicMock()
    public_settings.psycopg_database_url = "postgresql://uap_public_reader@db/uap"
    public_settings.pool_min_size = 1
    public_settings.pool_max_size = 2
    public_settings.cursor_key = b"k" * 32
    public_settings.cursor_max_length = 2048
    public_settings.host = "127.0.0.1"
    public_settings.port = 8081
    public_settings.safe_summary.return_value = {"port": 8081}
    public_pool = MagicMock()

    class BoomServer:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            return None

    with (
        patch(
            "uap_platform.public_api.server.load_public_api_settings", return_value=public_settings
        ),
        patch("uap_platform.public_api.server.PublicReaderPool", return_value=public_pool),
        patch("uap_platform.public_api.server.ThreadingHTTPServer", BoomServer),
        pytest.raises(KeyboardInterrupt),
    ):
        public_main()
    public_pool.close.assert_called_once()

    admin_settings = MagicMock()
    admin_settings.oidc_issuer = "https://issuer.test"
    admin_settings.oidc_audience = "uap-admin"
    admin_settings.jwks_document = {"keys": [{"kty": "RSA"}]}
    admin_settings.psycopg_database_url = "postgresql://uap_api@db/uap"
    admin_settings.pool_min_size = 1
    admin_settings.pool_max_size = 2
    admin_settings.cursor_key = b"k" * 32
    admin_settings.cursor_max_length = 2048
    admin_settings.host = "127.0.0.1"
    admin_settings.port = 8082
    admin_settings.safe_summary.return_value = {"port": 8082}
    admin_pool = MagicMock()
    oidc = MagicMock()
    with (
        patch("uap_platform.admin_api.server.load_admin_api_settings", return_value=admin_settings),
        patch("uap_platform.admin_api.server.OidcValidator.from_settings", return_value=oidc),
        patch("uap_platform.admin_api.server.AdminApiPool", return_value=admin_pool),
        patch("uap_platform.admin_api.server.ThreadingHTTPServer", BoomServer),
        pytest.raises(KeyboardInterrupt),
    ):
        admin_main()
    admin_pool.close.assert_called_once()

    with (
        patch("uap_platform.admin_api.server.load_admin_api_settings", return_value=admin_settings),
        patch(
            "uap_platform.admin_api.server.OidcValidator.from_settings",
            side_effect=TokenError("api_token_invalid"),
        ),
        pytest.raises(SystemExit, match="OIDC"),
    ):
        admin_main()
