"""Validate WP9.1-WP9.5 freeze. Later WP9.x functions must be absent."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

WP91_HEAD = "0014_review_session_authority"
WP91_PARENT = "0013_entity_merge_state_machine"
WP92_HEAD = "0015_review_case_lifecycle"
WP92_PARENT = WP91_HEAD
WP93_HEAD = "0016_review_decisions_and_grants"
WP93_PARENT = WP92_HEAD
WP94_HEAD = "0017_selection_and_promotion"
WP94_PARENT = WP93_HEAD
WP95_HEAD = "0018_authorized_entity_merge"
WP95_PARENT = WP94_HEAD
REQUIRED_FILES = (
    "docs/wp9/implementation-ticket.md",
    "docs/wp9/acceptance-ticket.md",
    "docs/wp9/acceptance-cases.md",
    "docs/wp9/adr/0014-review-session-and-write-authority.md",
    "docs/wp9/adr/0015-review-case-and-decision-lifecycle.md",
    "docs/wp9/adr/0016-publication-grants-and-outbox.md",
    "docs/wp9/adr/0017-analysis-selection-and-candidate-promotion.md",
    "docs/wp9/adr/0018-authorized-entity-merge.md",
    "platform/alembic/versions/0014_review_session_authority.py",
    "platform/alembic/versions/0015_review_case_lifecycle.py",
    "platform/alembic/versions/0016_review_decisions_and_grants.py",
    "platform/alembic/versions/0017_selection_and_promotion.py",
    "platform/alembic/versions/0018_authorized_entity_merge.py",
    "platform/src/uap_platform/review/__init__.py",
    "platform/src/uap_platform/review/errors.py",
    "platform/src/uap_platform/review/session.py",
    "platform/src/uap_platform/review/cases.py",
    "platform/src/uap_platform/review/canonical.py",
    "platform/src/uap_platform/review/decisions.py",
    "platform/src/uap_platform/review/promotion.py",
    "platform/src/uap_platform/review/merge.py",
    "platform/tests/test_wp9_session.py",
    "platform/tests/test_wp9_cases.py",
    "platform/tests/test_wp9_decisions.py",
    "platform/tests/test_wp9_promotion.py",
    "platform/tests/test_wp9_merge.py",
    "platform/tests/test_wp9_foundation.py",
    "platform/tools/validate_wp9.py",
    "platform/tools/wp9_1_runtime_probe.py",
    "platform/tools/wp9_2_runtime_probe.py",
    "platform/tools/wp9_3_runtime_probe.py",
    "platform/tools/wp9_4_runtime_probe.py",
    "platform/tools/wp9_5_runtime_probe.py",
)
FORBIDDEN_STAGE_TOKENS = (
    "CREATE FUNCTION audit.create_manual_claim",
    "CREATE FUNCTION audit._apply_claim_subject_bind",
    "CREATE FUNCTION audit._replace_claim_evidence",
    "CREATE FUNCTION audit._retire_manual_claim_supports",
)
FORBIDDEN_PRIOR_MERGE_TOKENS = (
    "CREATE FUNCTION audit.apply_entity_merge",
    "CREATE FUNCTION audit.apply_entity_merge_reverse",
)
FORBIDDEN_GRANTS = (
    "open_review_case(audit.review_case_type, uuid, smallint, text) TO uap_worker",
    "assign_review_case(uuid, uuid) TO uap_worker",
    "close_review_case(uuid, text) TO uap_worker",
    "record_review_decision(uuid, audit.review_decision, text, jsonb) TO uap_worker",
    "record_review_decision(uuid, audit.review_decision, text, jsonb) TO uap_publisher",
    "select_analysis_result(uuid, text) TO uap_worker",
    "accept_entity_candidate(uuid, text) TO uap_worker",
    "bind_entity_candidate(uuid, uuid, text) TO uap_worker",
    "apply_entity_merge(uuid, uuid, text) TO uap_worker",
    "apply_entity_merge_reverse(uuid, text) TO uap_worker",
    "GRANT EXECUTE ON FUNCTION ops.enqueue_publication_outbox",
    "GRANT EXECUTE ON FUNCTION audit.append_audit_event",
    "GRANT INSERT ON audit.review_cases",
    "GRANT UPDATE ON audit.review_cases",
    "GRANT INSERT ON TABLE audit.review_cases",
    "GRANT INSERT ON TABLE ops.outbox_events",
    "GRANT EXECUTE ON FUNCTION core.merge_entities",
    "GRANT EXECUTE ON FUNCTION core.reverse_entity_merge",
)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    actual: object
    expected: object


def check(name: str, passed: bool, actual: object, expected: object = True) -> Check:
    return Check(name, passed, actual, expected)


def _event_key_lock_precedes_resource(sql: str) -> bool:
    markers = (
        "CREATE FUNCTION audit.select_analysis_result",
        "CREATE FUNCTION audit.accept_entity_candidate",
        "CREATE FUNCTION audit.bind_entity_candidate",
    )
    for marker in markers:
        start = sql.find(marker)
        if start < 0:
            return False
        body = sql[start:]
        lock_at = body.find("pg_advisory_xact_lock(9175, hashtext(v_key))")
        resource_at = body.find("FOR UPDATE")
        if lock_at < 0 or resource_at < 0 or lock_at > resource_at:
            return False
        after_lock = body[lock_at:]
        recheck = after_lock.find("_existing_write_target")
        next_resource = after_lock.find("FOR UPDATE")
        if recheck < 0 or next_resource < 0 or recheck > next_resource:
            return False
    return True


def _event_key_lock_precedes_core(sql: str) -> bool:
    markers = (
        ("CREATE FUNCTION audit.apply_entity_merge(", "core.merge_entities("),
        ("CREATE FUNCTION audit.apply_entity_merge_reverse(", "core.reverse_entity_merge("),
    )
    for marker, core_call in markers:
        start = sql.find(marker)
        if start < 0:
            return False
        body = sql[start:]
        lock_at = body.find("pg_advisory_xact_lock(9175, hashtext(v_key))")
        core_at = body.find(core_call)
        if lock_at < 0 or core_at < 0 or lock_at > core_at:
            return False
        after_lock = body[lock_at:]
        recheck = after_lock.find("_existing_write_target")
        next_core = after_lock.find(core_call)
        if recheck < 0 or next_core < 0 or recheck > next_core:
            return False
    return True


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
    migration_14 = (platform / "alembic/versions/0014_review_session_authority.py").read_text(
        encoding="utf-8"
    )
    migration_15 = (platform / "alembic/versions/0015_review_case_lifecycle.py").read_text(
        encoding="utf-8"
    )
    migration_16 = (platform / "alembic/versions/0016_review_decisions_and_grants.py").read_text(
        encoding="utf-8"
    )
    migration_17 = (platform / "alembic/versions/0017_selection_and_promotion.py").read_text(
        encoding="utf-8"
    )
    migration_18 = (platform / "alembic/versions/0018_authorized_entity_merge.py").read_text(
        encoding="utf-8"
    )
    session_py = (platform / "src/uap_platform/review/session.py").read_text(encoding="utf-8")
    cases_py = (platform / "src/uap_platform/review/cases.py").read_text(encoding="utf-8")
    decisions_py = (platform / "src/uap_platform/review/decisions.py").read_text(encoding="utf-8")
    promotion_py = (platform / "src/uap_platform/review/promotion.py").read_text(encoding="utf-8")
    merge_py = (platform / "src/uap_platform/review/merge.py").read_text(encoding="utf-8")
    canonical_py = (platform / "src/uap_platform/review/canonical.py").read_text(encoding="utf-8")
    errors_py = (platform / "src/uap_platform/review/errors.py").read_text(encoding="utf-8")
    probe1 = (platform / "tools/wp9_1_runtime_probe.py").read_text(encoding="utf-8")
    probe2 = (platform / "tools/wp9_2_runtime_probe.py").read_text(encoding="utf-8")
    probe3 = (platform / "tools/wp9_3_runtime_probe.py").read_text(encoding="utf-8")
    probe4 = (platform / "tools/wp9_4_runtime_probe.py").read_text(encoding="utf-8")
    probe5 = (platform / "tools/wp9_5_runtime_probe.py").read_text(encoding="utf-8")
    tests = (platform / "tests/test_wp9_session.py").read_text(encoding="utf-8")
    case_tests = (platform / "tests/test_wp9_cases.py").read_text(encoding="utf-8")
    decision_tests = (platform / "tests/test_wp9_decisions.py").read_text(encoding="utf-8")
    promotion_tests = (platform / "tests/test_wp9_promotion.py").read_text(encoding="utf-8")
    merge_tests = (platform / "tests/test_wp9_merge.py").read_text(encoding="utf-8")
    makefile = (platform / "Makefile").read_text(encoding="utf-8")
    dockerfile = (platform / "Dockerfile").read_text(encoding="utf-8")
    ci = (repository / ".github/workflows/platform-ci.yml").read_text(encoding="utf-8")
    chain = (platform / "scripts/verify-migration-chain.sh").read_text(encoding="utf-8")
    worker = (platform / "src/uap_platform/knowledge/worker.py").read_text(encoding="utf-8")
    migration_11 = (platform / "alembic/versions/0011_claim_materialization.py").read_text(
        encoding="utf-8"
    )
    migration_12 = (platform / "alembic/versions/0012_entity_materialization.py").read_text(
        encoding="utf-8"
    )
    missing = [path for path in REQUIRED_FILES if not (repository / path).is_file()]
    combined = "\n".join([migration_14, migration_15, migration_16, migration_17, migration_18])
    prior = "\n".join([migration_14, migration_15, migration_16, migration_17])
    forbidden_hits = [token for token in FORBIDDEN_STAGE_TOKENS if token in combined]
    prior_merge_hits = [token for token in FORBIDDEN_PRIOR_MERGE_TOKENS if token in prior]
    grant_hits = [token for token in FORBIDDEN_GRANTS if token in combined]
    resolve_sources = migration_11 + "\n" + migration_12 + "\n" + worker
    return [
        check("required_files", not missing, missing, []),
        check(
            "unique_wp9_5_head",
            heads == [WP95_HEAD] and revision_ids[:2] == [WP95_HEAD, WP95_PARENT],
            {"heads": heads, "prefix": revision_ids[:2]},
            {"heads": [WP95_HEAD], "prefix": [WP95_HEAD, WP95_PARENT]},
        ),
        check(
            "migration_links",
            f'revision = "{WP91_HEAD}"' in migration_14
            and f'down_revision = "{WP91_PARENT}"' in migration_14
            and f'revision = "{WP92_HEAD}"' in migration_15
            and f'down_revision = "{WP92_PARENT}"' in migration_15
            and f'revision = "{WP93_HEAD}"' in migration_16
            and f'down_revision = "{WP93_PARENT}"' in migration_16
            and f'revision = "{WP94_HEAD}"' in migration_17
            and f'down_revision = "{WP94_PARENT}"' in migration_17
            and f'revision = "{WP95_HEAD}"' in migration_18
            and f'down_revision = "{WP95_PARENT}"' in migration_18
            and "CREATE TABLE" not in migration_14
            and "CREATE TABLE" not in migration_15
            and "CREATE TABLE" not in migration_16
            and "CREATE TABLE" not in migration_17
            and "CREATE TABLE" not in migration_18
            and "CREATE FUNCTION audit.record_review_decision" not in migration_15
            and "CREATE FUNCTION audit.select_analysis_result" not in migration_16
            and "CREATE FUNCTION audit.apply_entity_merge" not in migration_17,
            True,
        ),
        check(
            "selection_promotion_contract",
            "CREATE FUNCTION audit.select_analysis_result" in migration_17
            and "CREATE FUNCTION audit.accept_entity_candidate" in migration_17
            and "CREATE FUNCTION audit.bind_entity_candidate" in migration_17
            and "review.selection:" in migration_17
            and "review.candidate.accept:" in migration_17
            and "review.candidate.bind:" in migration_17
            and "review_selection_type_unsupported" in migration_17
            and "review_selection_not_valid" in migration_17
            and "review_candidate_evidence_missing" in migration_17
            and "review_bind_target_not_canonical" in migration_17
            and "FOR UPDATE" in migration_17
            and migration_17.count("_existing_write_target") >= 9
            and migration_17.count("pg_advisory_xact_lock(9175, hashtext(v_key))") >= 3
            and _event_key_lock_precedes_resource(migration_17)
            and "pg_advisory_xact_lock" in migration_17
            and "enqueue_job" not in migration_17
            and "publish_" not in migration_17
            and "core.merge_entities" not in migration_17
            and "GRANT EXECUTE ON FUNCTION audit.select_analysis_result" in migration_17
            and "GRANT EXECUTE ON FUNCTION audit.accept_entity_candidate" in migration_17
            and "GRANT EXECUTE ON FUNCTION audit.bind_entity_candidate" in migration_17
            and "REVOKE INSERT, UPDATE, DELETE ON core.analysis_selections" in migration_17
            and "sqlite-libs>=3.53.4-r0" in dockerfile
            and "libcrypto3>=3.5.8-r0" in dockerfile
            and "libssl3>=3.5.8-r0" in dockerfile,
            True,
        ),
        check(
            "authorized_merge_contract",
            "CREATE FUNCTION audit.apply_entity_merge" in migration_18
            and "CREATE FUNCTION audit.apply_entity_merge_reverse" in migration_18
            and "CREATE FUNCTION audit._require_openable_entity_subject" in migration_18
            and "review.entity.merge:" in migration_18
            and "review.entity.merge_reverse:" in migration_18
            and "require_active_role('senior_reviewer'" in migration_18
            and "core.merge_entities(" in migration_18
            and "core.reverse_entity_merge(" in migration_18
            and "review_subject_not_active" in migration_18
            and "review_subject_not_canonical" in migration_18
            and migration_18.count("_existing_write_target") >= 4
            and migration_18.count("pg_advisory_xact_lock(9175, hashtext(v_key))") >= 2
            and _event_key_lock_precedes_core(migration_18)
            and "GRANT EXECUTE ON FUNCTION audit.apply_entity_merge" in migration_18
            and "GRANT EXECUTE ON FUNCTION audit.apply_entity_merge_reverse" in migration_18
            and "GRANT EXECUTE ON FUNCTION core.merge_entities" not in migration_18
            and "GRANT EXECUTE ON FUNCTION core.reverse_entity_merge" not in migration_18
            and "GRANT EXECUTE ON FUNCTION audit._require_openable_entity_subject"
            not in migration_18
            and "enqueue_job" not in migration_18
            and "publish_" not in migration_18
            and "CREATE FUNCTION audit.create_manual_claim" not in migration_18
            and "sqlite-libs>=3.53.4-r0" in dockerfile
            and "libcrypto3>=3.5.8-r0" in dockerfile
            and "libssl3>=3.5.8-r0" in dockerfile,
            True,
        ),
        check(
            "wp8_resolve_ignores_selections",
            "JOIN core.analysis_selections" not in resolve_sources
            and "FROM core.analysis_selections" not in resolve_sources,
            True,
        ),
        check("no_later_wp9_functions", not forbidden_hits, forbidden_hits, []),
        check("no_merge_wrappers_before_wp9_5", not prior_merge_hits, prior_merge_hits, []),
        check("no_worker_or_review_dml_grants", not grant_hits, grant_hits, []),
        check(
            "python_clients",
            "set_config('uap.principal_id'" in session_py
            and "p_actor_id" not in session_py
            and "audit.open_review_case" in cases_py
            and "record_review_decision" not in cases_py
            and "audit.record_review_decision" in decisions_py
            and "select_analysis_result" not in decisions_py
            and "audit.select_analysis_result" in promotion_py
            and "audit.accept_entity_candidate" in promotion_py
            and "audit.bind_entity_candidate" in promotion_py
            and "p_actor_id" not in promotion_py
            and "apply_entity_merge" not in promotion_py
            and "create_manual_claim" not in promotion_py
            and "audit.apply_entity_merge" in merge_py
            and "audit.apply_entity_merge_reverse" in merge_py
            and "p_actor_id" not in merge_py
            and "create_manual_claim" not in merge_py
            and "core.merge_entities" not in merge_py
            and 'format(value, "f")' in canonical_py
            and "parse_int=Decimal" in canonical_py,
            True,
        ),
        check(
            "g9_cases_named",
            "g9_01" in probe1
            and "g9_06" in probe2
            and "g9_10" in probe3
            and "g9_17" in probe4
            and "g9_18" in probe4
            and "g9_19" in probe4
            and "g9_36" in probe4
            and "extra_concurrent_same_request" in probe4
            and "extra_concurrent_cross_resource" in probe4
            and "EVENT_KEY_LOCK_CLASS = 9175" in probe4
            and "apply_entity_merge" not in probe4
            and "create_manual_claim" not in probe4
            and "select_analysis_result" not in probe3
            and "g9_20" in probe5
            and "g9_21" in probe5
            and "g9_22" in probe5
            and "g9_37" in probe5
            and "extra_concurrent_same_request" in probe5
            and "extra_concurrent_cross_resource" in probe5
            and "EVENT_KEY_LOCK_CLASS = 9175" in probe5
            and "g8_16c" in probe5
            and "create_manual_claim" not in probe5
            and "review_role_denied" in probe5
            and "review_idempotency_payload_conflict" in probe5
            and probe5.count("LIKE 'publish_%%'") == 2
            and "LIKE 'publish_%'" not in probe5.replace("LIKE 'publish_%%'", ""),
            True,
        ),
        check(
            "frozen_error_tokens",
            "review_self_review_denied" in errors_py
            and "review_structured_changes_unsupported" in errors_py
            and "review_grant_not_active" in errors_py
            and "review_selection_type_unsupported" in errors_py
            and "review_selection_not_valid" in errors_py
            and "review_candidate_evidence_missing" in errors_py
            and "review_bind_target_not_canonical" in errors_py
            and "review_subject_not_active" in errors_py
            and "review_subject_not_canonical" in errors_py,
            True,
        ),
        check(
            "quality_and_chain_wired",
            "validate_wp9.py" in makefile
            and "validate_wp9.py" in ci
            and "wp9_1_runtime_probe.py" in ci
            and "wp9_2_runtime_probe.py" in ci
            and "wp9_3_runtime_probe.py" in ci
            and "wp9_4_runtime_probe.py" in ci
            and "wp9_5_runtime_probe.py" in ci
            and f'= "{WP95_HEAD}"' in chain
            and '= "50"' in chain
            and "review_grant_superseded_blocks_downgrade" in chain
            and "apply_entity_merge" in chain,
            True,
        ),
        check(
            "unit_tests_present",
            "require_active_role" in tests
            and "open_review_case" in case_tests
            and "record_review_decision" in decision_tests
            and "select_analysis_result" in promotion_tests
            and "accept_entity_candidate" in promotion_tests
            and "FROZEN_COMPACT_SHA256" in case_tests
            and "apply_entity_merge" in merge_tests
            and "apply_entity_merge_reverse" in merge_tests,
            True,
        ),
    ]


def main() -> None:
    platform = Path(__file__).resolve().parents[1]
    results = evaluate(platform)
    print(json.dumps([asdict(item) for item in results], indent=2))
    failed = [item.name for item in results if not item.passed]
    if failed:
        raise SystemExit("WP9.5 contract failed: " + ", ".join(failed))
    print("WP9.5 contract passed")


if __name__ == "__main__":
    main()
