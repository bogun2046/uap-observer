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
    reverse_fn = source[
        source.find("CREATE FUNCTION core.reverse_entity_merge") : source.find(
            "$reverse_entity_merge$;"
        )
    ]
    assert "event_kind = 'reverse'" in reverse_fn
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
    assert "def _activity_wait_event" in probe5
    assert "def _ungranted_advisory_locks" in probe5
    wait_helper = probe5[
        probe5.find("def _activity_wait_event") : probe5.find("def g8_live_definitions")
    ]
    assert "FROM pg_stat_activity" in wait_helper
    assert "FROM pg_locks" in wait_helper
    assert "fetchone()" in wait_helper
    assert "scalar(" not in wait_helper
    g8_18 = probe5[probe5.find("def g8_18") : probe5.find("def g8_19")]
    assert "_activity_wait_event(" in g8_18
    assert "_ungranted_advisory_locks(" in g8_18
    assert "set_config('application_name'" in g8_18
    assert "SET application_name = %s" not in g8_18
    assert "g8-18 waiter not SET syntax error" in g8_18
    assert "SELECT coalesce(wait_event_type" not in g8_18
    assert "if payload is None:" not in probe5
    assert "name[:8]" not in probe5
    assert 'CURRENT_HEAD = "0019_manual_claims_binding"' in probe5


def test_wp8_6_relation_reject_present() -> None:
    worker = (platform_root() / "src/uap_platform/knowledge/worker.py").read_text(encoding="utf-8")
    assert "def finish_misclaimed_relation_job" in worker
    helper = worker[
        worker.find("def finish_misclaimed_relation_job") : worker.find(
            "class KnowledgeJobDispatcher"
        )
    ]
    assert "SELECT ops.finish_job" in helper
    assert "SELECT ops.finish_knowledge_job" not in helper
    assert "materialize_claim" not in helper
    assert "materialize_entity" not in helper
    assert 'types.append("resolve_relations")' not in worker
    probe6 = (platform_root() / "tools/wp8_6_runtime_probe.py").read_text(encoding="utf-8")
    assert "def g8_16c" in probe6
    assert "knowledge_relation_task_not_in_wp8" in probe6
    assert "name[:8]" not in probe6
    assert 'CURRENT_HEAD = "0019_manual_claims_binding"' in probe6
    orchestrator = (platform_root() / "tools/wp8_runtime_probe.py").read_text(encoding="utf-8")
    assert "wp3_runtime_probe.py" in orchestrator
    assert "wp8_6_runtime_probe.py" in orchestrator
    probe1 = (platform_root() / "tools/wp8_1_runtime_probe.py").read_text(encoding="utf-8")
    assert 'CURRENT_HEAD = "0019_manual_claims_binding"' in probe1


def test_wp8_3_drain_closes_prior_queued_resolve_claims() -> None:
    probe3 = (platform_root() / "tools/wp8_3_runtime_probe.py").read_text(encoding="utf-8")
    close = probe3[
        probe3.find("def _close_claimed_resolve_job") : probe3.find("def _claim_target_job")
    ]
    drain = probe3[
        probe3.find("def _drain_resolve_claims") : probe3.find("def _require_handler_fail_closed")
    ]
    claim_loop = probe3[
        probe3.find("def _claim_target_job") : probe3.find("def _drain_resolve_claims")
    ]
    g16 = probe3[probe3.find("def g8_16a") : probe3.find("def g8_permissions")]
    assert "ResolveClaimsHandler" in close
    assert "active.handle(" in close
    assert "There is no pre-handler succeeded bypass." in close
    assert "Handler failures never fall back to succeeded." in close
    assert "def _proven_complete_materialization" not in probe3
    assert "def _finish_existing_materialization" not in probe3
    assert "'succeeded'::ops.attempt_outcome" not in close
    assert "SELECT ops.finish_knowledge_job" not in close
    handler_except = close.split("except PsycopgError", 1)[1]
    assert "_finish_existing_materialization" not in handler_except
    assert "_finish_probe_failure(" not in handler_except
    assert "_close_claimed_resolve_job(" in drain
    assert "_finish_probe_failure(" not in drain
    assert "_close_claimed_resolve_job(" in claim_loop
    assert "_close_claimed_resolve_job(" in g16
    assert "wp8-3-startup-drain" in probe3
    assert "DELETE FROM ops.jobs" not in probe3
    assert "TRUNCATE" not in probe3
    probe1 = (platform_root() / "tools/wp8_1_runtime_probe.py").read_text(encoding="utf-8")
    assert "put_verified(" in probe1
    assert "expected_sha256=output_sha256" in probe1
    assert "physical.object_key" in probe1
    assert "def finish_owned_resolution_job(" in probe1
    assert "g8-06 claim leftover closed" in probe1
    assert 'f"derived/{tag}/{extractor_name}"' not in probe1


def test_wp8_4_drain_closes_prior_queued_resolve_entities() -> None:
    probe4 = (platform_root() / "tools/wp8_4_runtime_probe.py").read_text(encoding="utf-8")
    close = probe4[
        probe4.find("def _close_claimed_resolve_job") : probe4.find("def _claim_target_job")
    ]
    drain = probe4[
        probe4.find("def _drain_resolve_entities") : probe4.find("def _require_handler_fail_closed")
    ]
    g16 = probe4[probe4.find("def g8_16b") : probe4.find("def g8_permissions")]
    assert "ResolveEntitiesHandler" in close
    assert "active.handle(" in close
    assert "There is no pre-handler succeeded bypass." in close
    assert "Handler failures never fall back to succeeded." in close
    assert "def _proven_complete_materialization" not in probe4
    assert "def _finish_existing_materialization" not in probe4
    assert "'succeeded'::ops.attempt_outcome" not in close
    assert "_close_claimed_resolve_job(" in drain
    assert "_finish_probe_failure(" not in drain
    assert "_close_claimed_resolve_job(" in g16
    handler_except = close.split("except PsycopgError", 1)[1]
    assert "_finish_existing_materialization" not in handler_except
    assert "wp8-4-startup-drain" in probe4
    assert "DELETE FROM ops.jobs" not in probe4
    assert "TRUNCATE" not in probe4
