"""Static tests for the G10-26 live matrix contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
    migration_names = {path.name for path in (PLATFORM / "alembic/versions").glob("*.py")}
    assert "0025_v12_editorial_foundation.py" in migration_names
    assert "0036_v133_full_rebuild_publication_evidence_guard.py" in migration_names
    checks = validate_wp10_6.evaluate(PLATFORM, paths=[])
    assert all(check.passed for check in checks), json.dumps(
        [{"name": check.name, "actual": check.actual} for check in checks if not check.passed],
        ensure_ascii=False,
    )


def test_g10_26_and_f4_migrations_match_the_authorized_hash_scope() -> None:
    authorized_paths = sorted(EXPECTED_MIGRATION_HASHES)
    assert _migration_scope_is_valid(authorized_paths, _current_migration_hashes())


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


def test_migration_scope_rejects_unavailable_path_list() -> None:
    assert not _migration_scope_is_valid(None, EXPECTED_MIGRATION_HASHES)
