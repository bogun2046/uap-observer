"""Static tests for the G10-26 live matrix contract."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools import validate_wp10_6
from tools.wp10_6_migration_probe import SCENARIO_IDS, scenario_variants

PLATFORM = Path(__file__).resolve().parents[1]
REPOSITORY = PLATFORM.parent
EXPECTED_MIGRATION_HASHES = {
    "platform/alembic/versions/0008_ai_model_governance.py": (
        "541249354c8e35771225012632e7345a6ce69052d82a60fe2997bca112b45295"
    ),
    "platform/alembic/versions/0020_wp10_publication_contract.py": (
        "efcac169fbcbf16bba439a5b36c76626ebc19bcb727cce78bd10d8c824b45f4d"
    ),
}


def _git_changed_paths_fail_closed() -> list[str] | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        diff = subprocess.run(  # noqa: S603
            [
                git,
                "-c",
                f"safe.directory={REPOSITORY}",
                "diff",
                "--name-only",
                validate_wp10_6.START_SHA,
            ],
            cwd=REPOSITORY,
            check=False,
            capture_output=True,
            text=True,
        )
        untracked = subprocess.run(  # noqa: S603
            [
                git,
                "-c",
                f"safe.directory={REPOSITORY}",
                "ls-files",
                "--others",
                "--exclude-standard",
            ],
            cwd=REPOSITORY,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if diff.returncode != 0 or untracked.returncode != 0:
        return None
    return sorted(
        {
            line.strip()
            for line in diff.stdout.splitlines() + untracked.stdout.splitlines()
            if line.strip()
        }
    )


def _migration_scope_is_valid(changed: list[str] | None, hashes: dict[str, str]) -> bool:
    if changed is None:
        return False
    migrations = {path for path in changed if path.startswith("platform/alembic/versions/")}
    return migrations == set(EXPECTED_MIGRATION_HASHES) and hashes == EXPECTED_MIGRATION_HASHES


def _current_migration_hashes() -> dict[str, str]:
    return {
        path: hashlib.sha256((REPOSITORY / path).read_bytes()).hexdigest()
        for path in EXPECTED_MIGRATION_HASHES
    }


def test_g10_26_has_exactly_fourteen_top_level_scenarios() -> None:
    assert len(SCENARIO_IDS) == 14
    assert len(set(SCENARIO_IDS)) == 14
    assert all(scenario_variants(scenario_id) for scenario_id in SCENARIO_IDS)


def test_g10_26_matrix_is_fail_closed_and_digest_backed() -> None:
    source = (PLATFORM / "tools/wp10_6_migration_probe.py").read_text(encoding="utf-8")
    for marker in (
        '"not_run"',
        '"failed"',
        '"before"',
        '"after"',
        '"temporary_databases_remaining"',
        "publication_contract_state_blocks_downgrade",
        'EXPECTED_SQLSTATE = "22023"',
        "run_acl_roundtrip",
        '"A_native_0019"',
        '"B_first_0020"',
        '"C_downgraded_0019"',
        '"D_second_0020"',
        '"E_native_0024"',
        '"C_minus_A"',
        '"D_minus_B"',
        "real_role_permissions",
    ):
        assert marker in source


def test_g10_26_validator_accepts_current_contract_without_live_claim() -> None:
    checks = validate_wp10_6.evaluate(PLATFORM, paths=[])
    assert all(check.passed for check in checks), json.dumps(
        [{"name": check.name, "actual": check.actual} for check in checks if not check.passed],
        ensure_ascii=False,
    )


def test_g10_26_and_f4_touch_only_the_two_authorized_migrations() -> None:
    changed = _git_changed_paths_fail_closed()
    assert changed == validate_wp10_6.changed_paths()
    assert _migration_scope_is_valid(changed, _current_migration_hashes())


def test_migration_scope_rejects_an_additional_migration() -> None:
    changed = [*EXPECTED_MIGRATION_HASHES, "platform/alembic/versions/0009_extra.py"]
    assert not _migration_scope_is_valid(changed, EXPECTED_MIGRATION_HASHES)


def test_migration_scope_rejects_each_missing_migration() -> None:
    for missing in EXPECTED_MIGRATION_HASHES:
        changed = [path for path in EXPECTED_MIGRATION_HASHES if path != missing]
        assert not _migration_scope_is_valid(changed, EXPECTED_MIGRATION_HASHES)


def test_migration_scope_rejects_each_hash_mismatch() -> None:
    for path in EXPECTED_MIGRATION_HASHES:
        hashes = {**EXPECTED_MIGRATION_HASHES, path: "0" * 64}
        assert not _migration_scope_is_valid(list(EXPECTED_MIGRATION_HASHES), hashes)


def test_migration_scope_rejects_unavailable_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _command: None)
    assert _git_changed_paths_fail_closed() is None
    assert not _migration_scope_is_valid(None, EXPECTED_MIGRATION_HASHES)


def test_migration_scope_rejects_failed_git_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _command: "/usr/bin/git")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout="", stderr="git failed"
        ),
    )
    assert _git_changed_paths_fail_closed() is None
    assert not _migration_scope_is_valid(None, EXPECTED_MIGRATION_HASHES)
