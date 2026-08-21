"""Validate the WP8.1 knowledge handover and write-authority contract."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

WP8_HEAD = "0010_knowledge_foundation"
WP8_PARENT = "0009_model_governance_boundaries"
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
    "platform/alembic/versions/0010_knowledge_foundation.py",
    "platform/tools/validate_wp8.py",
    "platform/tools/wp8_1_runtime_probe.py",
    "platform/scripts/verify-migration-chain.sh",
    "platform/tools/validate_wp3.py",
    "platform/tools/validate_wp4.py",
    "platform/tools/wp3_runtime_probe.py",
)
FORBIDDEN_STAGE_TOKENS = (
    "CREATE FUNCTION core.materialize_claim_bundle",
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
    chain = (platform / "scripts/verify-migration-chain.sh").read_text(encoding="utf-8")
    wp3_validator = (platform / "tools/validate_wp3.py").read_text(encoding="utf-8")
    wp4_validator = (platform / "tools/validate_wp4.py").read_text(encoding="utf-8")
    wp3_probe = (platform / "tools/wp3_runtime_probe.py").read_text(encoding="utf-8")
    makefile = (platform / "Makefile").read_text(encoding="utf-8")
    ci = (repository / ".github/workflows/platform-ci.yml").read_text(encoding="utf-8")
    missing = [path for path in REQUIRED_FILES if not (repository / path).is_file()]
    knowledge_package = repository / "platform/src/uap_platform/knowledge"
    return [
        check("required_files", not missing, missing, []),
        check(
            "unique_wp8_1_head",
            heads == [WP8_HEAD] and revision_ids[:2] == [WP8_HEAD, WP8_PARENT],
            {"heads": heads, "prefix": revision_ids[:2]},
            {"heads": [WP8_HEAD], "prefix": [WP8_HEAD, WP8_PARENT]},
        ),
        check(
            "migration_revision_id",
            f'revision = "{WP8_HEAD}"' in migration
            and f'down_revision = "{WP8_PARENT}"' in migration,
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
            and not knowledge_package.exists(),
            {
                "materialize_present": [
                    token for token in FORBIDDEN_STAGE_TOKENS if token in migration
                ],
                "knowledge_package": knowledge_package.exists(),
            },
            {"materialize_present": [], "knowledge_package": False},
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
            and 'CURRENT_HEAD = "0010_knowledge_foundation"' in wp3_probe
            and "document_version_id" in wp3_probe,
            True,
        ),
        check(
            "migration_chain_head",
            "0010_knowledge_foundation" in chain
            and '= "50"' in chain
            and "knowledge_claim_backfill_required" in chain
            and "entity_candidate_evidence" in chain,
            True,
        ),
        check(
            "wp8_not_wired_to_ci",
            "validate_wp8.py" not in makefile
            and "wp8_1_runtime_probe.py" not in makefile
            and "validate_wp8.py" not in ci
            and "wp8_runtime_probe.py" not in ci
            and "wp8_1_runtime_probe.py" not in ci,
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
