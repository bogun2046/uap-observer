from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from tools.validate_wp3 import evaluate as evaluate_wp3
from tools.validate_wp4 import evaluate as evaluate_wp4
from tools.validate_wp8 import evaluate as evaluate_wp8
from uap_platform.knowledge.job_types import (
    CLAIMABLE_JOB_TYPES,
    ENTITY_CLAIMABLE_JOB_TYPES,
    FORBIDDEN_CLAIMABLE_JOB_TYPES,
    PRE_CLAIM_HANDLER_JOB_TYPES,
    PRE_ENTITY_HANDLER_JOB_TYPES,
    claimable_job_types,
)


def claim_fingerprint(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    collapsed = re.sub(r"\s+", " ", normalized)
    trimmed = collapsed.strip(" ")
    return hashlib.sha256(trimmed.encode("utf-8")).hexdigest()


def platform_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_wp3_and_wp4_accept_later_head() -> None:
    wp3 = evaluate_wp3(platform_root())
    wp4 = evaluate_wp4(platform_root())
    assert all(check.passed for check in wp3)
    assert all(check.passed for check in wp4)


def test_wp8_static_contract() -> None:
    checks = evaluate_wp8(platform_root())
    failed = [check.name for check in checks if not check.passed]
    assert failed == []


def test_claim_fingerprint_algorithm() -> None:
    digest = claim_fingerprint("  Hello   World  ")
    assert digest == hashlib.sha256(b"Hello World").hexdigest()
    assert claim_fingerprint("A") != claim_fingerprint("a")


def test_wp8_1_migration_does_not_implement_materialize() -> None:
    source = (platform_root() / "alembic/versions/0010_knowledge_foundation.py").read_text(
        encoding="utf-8"
    )
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in source
    assert "materialize_claim_bundle" not in source
    assert "materialize_entity_bundle" not in source
    assert "merge_entities" not in source


def test_wp8_3_claim_materialization_present() -> None:
    source = (platform_root() / "alembic/versions/0011_claim_materialization.py").read_text(
        encoding="utf-8"
    )
    assert "CREATE FUNCTION core.materialize_claim_bundle" in source
    assert "materialize_entity_bundle" not in source
    assert "merge_entities" not in source
    knowledge = platform_root() / "src/uap_platform/knowledge"
    package = "\n".join(path.read_text(encoding="utf-8") for path in knowledge.glob("*.py"))
    assert "ResolveClaimsHandler" in package
    assert "parse_knowledge_payload" in package
    assert "_finish_unmapped_failure" in package
    assert "ResolveClaimsWorker" in package
    assert "from_settings" in package
    assert "read_verified_object" in package
    assert "ORDER BY" not in package
    assert "canonical_locator_digest" not in package
    assert 'MISMATCH = "mismatch"' not in package
    assert "def resolve_entities" not in package
    assert "WHERE claim_id = claim_id" not in source
    assert "v_claim_id" in source
    assert "trunc((code_value" in source.split("def downgrade", 1)[0]
    assert "truncate((code_value" not in source.split("def downgrade", 1)[0]
    assert "core._jsonb_keys_exact" in source
    assert "object_keys(key)" in source
    downgrade = source.split("def downgrade", 1)[1]
    assert "CREATE OR REPLACE FUNCTION ops.validate_knowledge_attempt_metrics" in downgrade
    assert "knowledge_locator_hash_conflict" not in downgrade
    assert "fixture_extraction_text" not in package
    assert "current_setting" not in package


def _validator_bodies(source: str) -> list[str]:
    marker = "$validate_knowledge_attempt_metrics$"
    parts = source.split(marker)
    return [parts[index] for index in range(1, len(parts), 2)]


def test_0011_downgrade_restores_frozen_0010_metrics_validator() -> None:
    source_10 = (platform_root() / "alembic/versions/0010_knowledge_foundation.py").read_text(
        encoding="utf-8"
    )
    source_11 = (platform_root() / "alembic/versions/0011_claim_materialization.py").read_text(
        encoding="utf-8"
    )
    bodies_10 = _validator_bodies(source_10)
    bodies_11 = _validator_bodies(source_11)
    assert len(bodies_10) == 1
    assert len(bodies_11) == 2
    upgrade_body, downgrade_body = bodies_11
    assert "trunc(" in upgrade_body
    assert "truncate(" not in upgrade_body
    assert "knowledge_locator_hash_conflict" in upgrade_body
    assert "truncate(" in downgrade_body
    assert re.sub(r"\s+", " ", downgrade_body).strip() == re.sub(r"\s+", " ", bodies_10[0]).strip()


def test_g8_16a_claimable_job_types_activation() -> None:
    """G8-16A: resolve_claims absent until handler activation; entities/relations never."""

    before = claimable_job_types(claims_handler_active=False)
    assert "resolve_claims" not in before
    assert before == PRE_CLAIM_HANDLER_JOB_TYPES
    for forbidden in FORBIDDEN_CLAIMABLE_JOB_TYPES:
        assert forbidden not in before

    after = claimable_job_types(claims_handler_active=True)
    assert "resolve_claims" in after
    assert after == CLAIMABLE_JOB_TYPES
    for forbidden in FORBIDDEN_CLAIMABLE_JOB_TYPES:
        assert forbidden not in after


def test_wp8_4_entity_materialization_present() -> None:
    source = (platform_root() / "alembic/versions/0012_entity_materialization.py").read_text(
        encoding="utf-8"
    )
    source_11 = (platform_root() / "alembic/versions/0011_claim_materialization.py").read_text(
        encoding="utf-8"
    )
    assert "CREATE FUNCTION core.materialize_entity_bundle" in source
    assert "materialize_entity_bundle" not in source_11
    assert "CREATE FUNCTION core._jsonb_keys_exact" not in source
    assert "core._jsonb_keys_exact" in source
    assert "INSERT INTO core.entities" not in source
    assert "UPDATE core.claims" not in source
    assert "merge_entities" not in source
    knowledge = platform_root() / "src/uap_platform/knowledge"
    package = "\n".join(path.read_text(encoding="utf-8") for path in knowledge.glob("*.py"))
    assert "ResolveEntitiesHandler" in package
    assert "ResolveEntitiesWorker" in package
    assert "entities_handler_active" in package
    assert "def resolve_entities" not in package
    assert "fixture_extraction_text" not in package
    probe4 = (platform_root() / "tools/wp8_4_runtime_probe.py").read_text(encoding="utf-8")
    assert "if payload is None:" in probe4
    assert "admin, worker, world, tag, name, payload" in probe4


def test_g8_16b_claimable_job_types_activation() -> None:
    """G8-16B: resolve_entities absent until handler activation; relations never."""

    before = claimable_job_types(claims_handler_active=True, entities_handler_active=False)
    assert "resolve_entities" not in before
    assert before == PRE_ENTITY_HANDLER_JOB_TYPES
    assert before == CLAIMABLE_JOB_TYPES
    assert "resolve_relations" not in before

    after = claimable_job_types(claims_handler_active=True, entities_handler_active=True)
    assert "resolve_entities" in after
    assert after == ENTITY_CLAIMABLE_JOB_TYPES
    assert "resolve_relations" not in after
    inactive_claims = claimable_job_types(claims_handler_active=False)
    assert "resolve_entities" not in inactive_claims
    assert inactive_claims == PRE_CLAIM_HANDLER_JOB_TYPES


def test_wp8_5_merge_state_machine_present() -> None:
    source = (platform_root() / "alembic/versions/0013_entity_merge_state_machine.py").read_text(
        encoding="utf-8"
    )
    source_12 = (platform_root() / "alembic/versions/0012_entity_materialization.py").read_text(
        encoding="utf-8"
    )
    assert "CREATE FUNCTION core.merge_entities" in source
    assert "CREATE FUNCTION core.reverse_entity_merge" in source
    assert "CREATE FUNCTION core.canonical_entity_id" in source
    assert "uq_open_merge_source" in source
    assert "pg_advisory_xact_lock(824, 1)" in source
    assert "GRANT EXECUTE ON FUNCTION core.merge_entities" not in source
    assert "GRANT EXECUTE ON FUNCTION core.reverse_entity_merge" not in source
    assert "GRANT EXECUTE ON FUNCTION core.canonical_entity_id" in source
    assert "UPDATE core.entity_candidates" not in source
    assert "UPDATE core.claims" not in source
    assert "UPDATE core.relations" not in source
    assert "CREATE FUNCTION core.merge_entities" not in source_12
    knowledge = platform_root() / "src/uap_platform/knowledge"
    package = "\n".join(path.read_text(encoding="utf-8") for path in knowledge.glob("*.py"))
    assert "merge_entities" not in package
    assert "reverse_entity_merge" not in package
    probe5 = (platform_root() / "tools/wp8_5_runtime_probe.py").read_text(encoding="utf-8")
    assert "def g8_17" in probe5
    assert "def g8_18" in probe5
    assert "def g8_19" in probe5
    assert "if payload is None:" not in probe5
    assert "name[:8]" not in probe5
    assert 'CURRENT_HEAD = "0013_entity_merge_state_machine"' in probe5
