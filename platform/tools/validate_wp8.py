"""Validate the WP8.1-WP8.3 knowledge handover, mapping, and claim materialization contract."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

WP8_HEAD = "0011_claim_materialization"
WP8_PARENT = "0010_knowledge_foundation"
WP8_1_HEAD = "0010_knowledge_foundation"
WP8_1_PARENT = "0009_model_governance_boundaries"
KNOWLEDGE_V2_KEYS = (
    "payload_schema_version",
    "analysis_result_id",
    "analysis_result_sha256",
    "analysis_schema_version",
    "document_version_id",
    "result_type",
    "model_run_id",
    "input_sha256",
    "extraction_anchor_status",
    "extraction_id",
)
REQUIRED_FILES = (
    "docs/wp8/implementation-ticket.md",
    "docs/wp8/acceptance-ticket.md",
    "docs/wp8/acceptance-cases.md",
    "docs/wp8/adr/0008-knowledge-job-handover.md",
    "docs/wp8/adr/0009-claim-document-evidence-constraints.md",
    "docs/wp8/adr/0011-knowledge-write-authority.md",
    "docs/wp8/adr/0010-evidence-locator-mapping.md",
    "platform/alembic/versions/0010_knowledge_foundation.py",
    "platform/alembic/versions/0011_claim_materialization.py",
    "platform/src/uap_platform/knowledge/__init__.py",
    "platform/src/uap_platform/knowledge/bundle.py",
    "platform/src/uap_platform/knowledge/handler.py",
    "platform/src/uap_platform/knowledge/job_types.py",
    "platform/src/uap_platform/knowledge/metrics.py",
    "platform/src/uap_platform/knowledge/payload.py",
    "platform/src/uap_platform/knowledge/worker.py",
    "platform/tools/wp8_3_runtime_probe.py",
    "platform/src/uap_platform/knowledge/anchors.py",
    "platform/src/uap_platform/knowledge/contracts.py",
    "platform/src/uap_platform/knowledge/locators.py",
    "platform/src/uap_platform/knowledge/mapping.py",
    "platform/src/uap_platform/knowledge/reasons.py",
    "platform/tests/test_wp8_locator_mapping.py",
    "platform/tools/validate_wp8.py",
    "platform/tools/wp8_1_runtime_probe.py",
    "platform/scripts/verify-migration-chain.sh",
    "platform/tools/validate_wp3.py",
    "platform/tools/validate_wp4.py",
    "platform/tools/wp3_runtime_probe.py",
)
FORBIDDEN_STAGE_TOKENS = (
    "CREATE FUNCTION core.materialize_entity_bundle",
    "CREATE FUNCTION core.merge_entities",
    "CREATE FUNCTION core.reverse_entity_merge",
    "CREATE FUNCTION core.canonical_entity_id",
)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    actual: object
    expected: object


def check(name: str, passed: bool, actual: object, expected: object = True) -> Check:
    return Check(name, passed, actual, expected)


def evaluate(platform: Path) -> list[Check]:
    platform = platform.resolve()
    repository = platform.parent
    config = Config(str(platform / "alembic.ini"))
    config.set_main_option("script_location", str(platform / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    revision_ids = [
        revision.revision for revision in script.walk_revisions(base="base", head="heads")
    ]
    migration_path = platform / "alembic/versions/0010_knowledge_foundation.py"
    migration = migration_path.read_text(encoding="utf-8") if migration_path.is_file() else ""
    migration_11_path = platform / "alembic/versions/0011_claim_materialization.py"
    migration_11 = (
        migration_11_path.read_text(encoding="utf-8") if migration_11_path.is_file() else ""
    )
    foundation_tests = (
        (platform / "tests/test_wp8_foundation.py").read_text(encoding="utf-8")
        if (platform / "tests/test_wp8_foundation.py").is_file()
        else ""
    )
    handler_tests = (
        (platform / "tests/test_wp8_claim_handler.py").read_text(encoding="utf-8")
        if (platform / "tests/test_wp8_claim_handler.py").is_file()
        else ""
    )
    probe3 = (
        (platform / "tools/wp8_3_runtime_probe.py").read_text(encoding="utf-8")
        if (platform / "tools/wp8_3_runtime_probe.py").is_file()
        else ""
    )
    chain = (platform / "scripts/verify-migration-chain.sh").read_text(encoding="utf-8")
    wp3_validator = (platform / "tools/validate_wp3.py").read_text(encoding="utf-8")
    wp4_validator = (platform / "tools/validate_wp4.py").read_text(encoding="utf-8")
    wp3_probe = (platform / "tools/wp3_runtime_probe.py").read_text(encoding="utf-8")
    makefile = (platform / "Makefile").read_text(encoding="utf-8")
    ci = (repository / ".github/workflows/platform-ci.yml").read_text(encoding="utf-8")
    missing = [path for path in REQUIRED_FILES if not (repository / path).is_file()]
    knowledge_package = repository / "platform/src/uap_platform/knowledge"
    knowledge_sources = ""
    if knowledge_package.is_dir():
        knowledge_sources = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(knowledge_package.glob("*.py"))
        )
    locator_tests = (
        (platform / "tests/test_wp8_locator_mapping.py").read_text(encoding="utf-8")
        if (platform / "tests/test_wp8_locator_mapping.py").is_file()
        else ""
    )
    reasons = (
        (platform / "src/uap_platform/knowledge/reasons.py").read_text(encoding="utf-8")
        if (platform / "src/uap_platform/knowledge/reasons.py").is_file()
        else ""
    )
    return [
        check("required_files", not missing, missing, []),
        check(
            "unique_wp8_3_head",
            heads == [WP8_HEAD] and revision_ids[:2] == [WP8_HEAD, WP8_PARENT],
            {"heads": heads, "prefix": revision_ids[:2]},
            {"heads": [WP8_HEAD], "prefix": [WP8_HEAD, WP8_PARENT]},
        ),
        check(
            "migration_revision_id",
            f'revision = "{WP8_1_HEAD}"' in migration
            and f'down_revision = "{WP8_1_PARENT}"' in migration
            and f'revision = "{WP8_HEAD}"' in migration_11
            and f'down_revision = "{WP8_PARENT}"' in migration_11,
            True,
        ),
        check(
            "handover_functions",
            all(
                token in migration
                for token in (
                    "CREATE FUNCTION ops.enqueue_followup_job",
                    "CREATE FUNCTION core.tg_enqueue_knowledge_followup",
                    "AFTER INSERT ON core.analysis_results",
                    "CREATE FUNCTION ops.reconcile_knowledge_jobs",
                    "CREATE FUNCTION ops.require_active_resolution_job_lease",
                    "CREATE FUNCTION ops.finish_knowledge_job",
                    "CREATE FUNCTION core.compute_evidence_locator_sha256",
                    "CREATE FUNCTION core.compute_claim_fingerprint",
                    "CREATE TABLE core.entity_candidate_evidence",
                    "knowledge_claim_backfill_required",
                    "knowledge_candidate_backfill_required",
                    "knowledge_idempotency_payload_conflict",
                    "knowledge_entity_evidence_downgrade_blocked",
                    "v_idempotency_key",
                )
            ),
            True,
        ),
        check(
            "knowledge_v2_payload",
            all(key in migration for key in KNOWLEDGE_V2_KEYS)
            and "'knowledge.v2'" in migration
            and "ON CONFLICT (idempotency_key) DO NOTHING" in migration
            and "ON CONFLICT (idempotency_key) DO UPDATE" not in migration,
            True,
        ),
        check(
            "hash_and_fingerprint",
            "encode(sha256(convert_to(p_envelope::text, 'UTF8')), 'hex')" in migration
            and "normalize(p_text, NFKC)" in migration
            and "[[:space:]]+" in migration
            and "current_setting('server_encoding')" in migration,
            True,
        ),
        check(
            "write_authority",
            "GRANT SELECT ON core.analysis_results TO uap_worker" in migration
            and "REVOKE INSERT, UPDATE, DELETE ON" in migration
            and "GRANT EXECUTE ON FUNCTION ops.reconcile_knowledge_jobs" in migration
            and "TO uap_scheduler" in migration
            and "GRANT EXECUTE ON FUNCTION ops.finish_knowledge_job" in migration
            and "GRANT EXECUTE ON FUNCTION ops.require_active_resolution_job_lease" in migration
            and "GRANT EXECUTE ON FUNCTION ops.enqueue_followup_job" not in migration
            and "GRANT EXECUTE ON FUNCTION core.compute_evidence_locator_sha256" not in migration
            and "GRANT EXECUTE ON FUNCTION core.compute_claim_fingerprint" not in migration
            and "REVOKE SELECT ON core.analysis_results FROM uap_worker" in migration,
            True,
        ),
        check(
            "stage_boundary",
            all(token not in migration for token in FORBIDDEN_STAGE_TOKENS)
            and all(token not in migration_11 for token in FORBIDDEN_STAGE_TOKENS)
            and all(token not in knowledge_sources for token in FORBIDDEN_STAGE_TOKENS)
            and "CREATE FUNCTION core.materialize_claim_bundle" in migration_11
            and "GRANT EXECUTE ON FUNCTION core.materialize_claim_bundle" in migration_11
            and "TO uap_worker" in migration_11
            and "materialize_entity_bundle" not in knowledge_sources
            and "def resolve_entities" not in knowledge_sources
            and knowledge_package.is_dir(),
            {
                "materialize_claim": "CREATE FUNCTION core.materialize_claim_bundle"
                in migration_11,
                "forbidden": [
                    token
                    for token in FORBIDDEN_STAGE_TOKENS
                    if token in migration or token in migration_11 or token in knowledge_sources
                ],
                "knowledge_package": knowledge_package.is_dir(),
            },
            {"materialize_claim": True, "forbidden": [], "knowledge_package": True},
        ),
        check(
            "historical_validators_use_suffix",
            "WP3_SUFFIX" in wp3_validator
            and "historical suffix 0004 -> 0003 -> 0002 -> 0001" in wp3_validator
            and "0009_model_governance_boundaries"
            not in wp3_validator.split("single_head", 1)[-1].split("linear_revision_chain", 1)[0]
            and "WP4_SUBCHAIN" in wp4_validator
            and "contains_consecutive" in wp4_validator
            and "consecutive subchain 0009 -> 0008 -> 0007 -> 0006 -> 0005" in wp4_validator,
            True,
        ),
        check(
            "wp3_original_tables_still_asserted",
            "frozen_49_tables" in wp3_validator
            and "len(actual_tables) == 49" in wp3_validator
            and "WP3_ORIGINAL_TABLE_COUNT = 49" in wp3_probe
            and "EXPECTED_TABLE_COUNT = 50" in wp3_probe
            and 'CURRENT_HEAD = "0011_claim_materialization"' in wp3_probe
            and "document_version_id" in wp3_probe,
            True,
        ),
        check(
            "migration_chain_head",
            "0011_claim_materialization" in chain
            and '= "50"' in chain
            and "knowledge_claim_backfill_required" in chain
            and "entity_candidate_evidence" in chain,
            True,
        ),
        check(
            "deferred_evidence_checks_both_sides_of_update",
            "COALESCE(NEW.claim_id, OLD.claim_id)" not in migration
            and "COALESCE(NEW.entity_candidate_id, OLD.entity_candidate_id)" not in migration
            and "NEW.claim_id IS DISTINCT FROM OLD.claim_id" in migration
            and "NEW.entity_candidate_id IS DISTINCT FROM OLD.entity_candidate_id" in migration
            and "ARRAY[NEW.claim_id, OLD.claim_id]" in migration
            and "OLD.entity_candidate_id" in migration
            and "TG_OP = 'DELETE'" in migration,
            True,
        ),
        check(
            "wp8_not_wired_to_ci",
            "validate_wp8.py" not in makefile
            and "wp8_1_runtime_probe.py" not in makefile
            and "wp8_3_runtime_probe.py" not in makefile
            and "validate_wp8.py" not in ci
            and "wp8_runtime_probe.py" not in ci
            and "wp8_1_runtime_probe.py" not in ci
            and "wp8_3_runtime_probe.py" not in ci,
            True,
        ),
        check(
            "wp8_2_locator_mapping",
            knowledge_package.is_dir()
            and "evidence-locator.v2" in knowledge_sources
            and "matched" in knowledge_sources
            and "missing" in knowledge_sources
            and "ambiguous" in knowledge_sources
            and "payload_anchor" in knowledge_sources
            and 'MISMATCH = "mismatch"' not in knowledge_sources
            and "canonical_locator_digest" not in knowledge_sources
            and "canonical_envelope_text" not in knowledge_sources
            and "locator_cross_axis_mismatch" in reasons
            and "locator_excerpt_too_large" in reasons
            and "MAX_EVIDENCE_UTF8_BYTES" in reasons
            and "8192" in reasons
            and "ORDER BY" not in knowledge_sources
            and "LIMIT 1" not in knowledge_sources,
            True,
        ),
        check(
            "wp8_2_gate_tests",
            "test_g8_07_matched_missing_ambiguous" in locator_tests
            and "test_g8_07_mapper_uses_frozen_payload" in locator_tests
            and "test_g8_08_five_locator_types" in locator_tests
            and "test_g8_08_python_does_not_emit_span_hash" in locator_tests
            and "test_g8_09_pdf_and_media_cross_axis" in locator_tests
            and "test_g8_10_result_classes" in locator_tests
            and "test_g8_10_empty_illegal_payload_fail_closed" in locator_tests
            and "test_g8_10_excerpt_utf8_limit" in locator_tests
            and "locator_duplicate" in locator_tests,
            True,
        ),
        check(
            "wp8_3_claim_materialization",
            "CREATE FUNCTION core.materialize_claim_bundle" in migration_11
            and "core.compute_claim_fingerprint" in migration_11
            and "knowledge-bundle.v2" in migration_11
            and "knowledge_locator_hash_conflict" in migration_11.split("def downgrade", 1)[0]
            and "knowledge_locator_hash_conflict"
            not in migration_11.split("def downgrade", 1)[-1]
            and "trunc((code_value" in migration_11.split("def downgrade", 1)[0]
            and "truncate((code_value" not in migration_11.split("def downgrade", 1)[0]
            and "WHERE claim_id = claim_id" not in migration_11
            and "v_claim_id" in migration_11
            and "core._jsonb_keys_exact" in migration_11
            and "object_keys(key)" in migration_11
            and "CREATE OR REPLACE FUNCTION ops.validate_knowledge_attempt_metrics"
            in migration_11.split("def downgrade", 1)[-1]
            and "ResolveClaimsHandler" in knowledge_sources
            and "ResolveClaimsWorker" in knowledge_sources
            and "from_settings" in knowledge_sources
            and "claim_job_types" in knowledge_sources
            and "if not requested" in knowledge_sources
            and "read_verified_object" in knowledge_sources
            and "parse_knowledge_payload" in knowledge_sources
            and "_finish_unmapped_failure" in knowledge_sources
            and "claimable_job_types" in knowledge_sources
            and "PRE_CLAIM_HANDLER_JOB_TYPES" in knowledge_sources
            and "fixture_extraction_text" not in knowledge_sources
            and "current_setting" not in knowledge_sources
            and "materialize_entity_bundle" not in migration_11
            and "def resolve_entities" not in knowledge_sources,
            True,
        ),
        check(
            "wp8_3_runtime_and_fail_closed_tests",
            "test_g8_16a_claimable_job_types_activation" in foundation_tests
            and "test_production_worker_activates_resolve_claims" in handler_tests
            and "inactive.claim_job_types == ()" in handler_tests
            and "test_0011_downgrade_restores_frozen_0010_metrics_validator" in foundation_tests
            and "test_parse_knowledge_payload_missing_key" in handler_tests
            and "test_handler_missing_key_finishes_attempt" in handler_tests
            and "test_handler_slice_comes_from_verified_object" in handler_tests
            and "def g8_11" in probe3
            and "def g8_12" in probe3
            and "def g8_13" in probe3
            and "def g8_16a" in probe3
            and "def g8_live_definitions" in probe3
            and "ResolveClaimsWorker" in probe3
            and "from_settings" in probe3
            and "put_verified" in probe3
            and "read_verified_object" in probe3
            and "ProbeObjectClient" not in probe3
            and "uap_worker" in probe3
            and "fixture_extraction_text" not in probe3
            and "name[:8]" not in probe3
            and "inactive.claim_job_types == ()" in probe3
            and "SAVEPOINT g8_16a_preclaim_control" in probe3
            and "g8-11 evidence slice" in probe3
            and "g8-13 evidence slice" in probe3
            and "object_content" in probe3
            and "object_length" in probe3
            and "object_hash" in probe3
            and "locator_ordinal" in probe3
            and "locator_axes" in probe3
            and "g8-13 race at most one claim" in probe3
            and "g8-13 expired first 40001" in probe3
            and "g8-13 at most one succeeded" in probe3
            and "g8-13 all attempts closed" in probe3
            and "ThreadPoolExecutor" in probe3
            and "read_verified_object" in probe3,
            True,
        ),
    ]


def main() -> None:
    checks = evaluate(Path(__file__).resolve().parents[1])
    print(json.dumps([asdict(item) for item in checks], indent=2, sort_keys=True))
    failed = [item.name for item in checks if not item.passed]
    if failed:
        raise SystemExit(f"WP8 contract checks failed: {', '.join(failed)}")
    print(f"WP8 contract checks passed: {len(checks)}/{len(checks)}")


if __name__ == "__main__":
    main()
