"""Validate WP9.1-WP9.2 freeze. Later WP9.x functions must be absent."""

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
REQUIRED_FILES = (
    "docs/wp9/implementation-ticket.md",
    "docs/wp9/acceptance-ticket.md",
    "docs/wp9/acceptance-cases.md",
    "docs/wp9/adr/0014-review-session-and-write-authority.md",
    "docs/wp9/adr/0015-review-case-and-decision-lifecycle.md",
    "platform/alembic/versions/0014_review_session_authority.py",
    "platform/alembic/versions/0015_review_case_lifecycle.py",
    "platform/src/uap_platform/review/__init__.py",
    "platform/src/uap_platform/review/errors.py",
    "platform/src/uap_platform/review/session.py",
    "platform/src/uap_platform/review/cases.py",
    "platform/tests/test_wp9_session.py",
    "platform/tests/test_wp9_cases.py",
    "platform/tests/test_wp9_foundation.py",
    "platform/tools/validate_wp9.py",
    "platform/tools/wp9_1_runtime_probe.py",
    "platform/tools/wp9_2_runtime_probe.py",
)
FORBIDDEN_STAGE_TOKENS = (
    "CREATE FUNCTION audit.record_review_decision",
    "CREATE FUNCTION audit.select_analysis_result",
    "CREATE FUNCTION audit.accept_entity_candidate",
    "CREATE FUNCTION audit.bind_entity_candidate",
    "CREATE FUNCTION audit.apply_entity_merge",
    "CREATE FUNCTION audit.apply_entity_merge_reverse",
    "CREATE FUNCTION audit.create_manual_claim",
    "CREATE FUNCTION audit._apply_claim_subject_bind",
)
FORBIDDEN_GRANTS = (
    "open_review_case(audit.review_case_type, uuid, smallint, text) TO uap_worker",
    "assign_review_case(uuid, uuid) TO uap_worker",
    "close_review_case(uuid, text) TO uap_worker",
    "GRANT EXECUTE ON FUNCTION audit.append_audit_event",
    "GRANT INSERT ON audit.review_cases",
    "GRANT UPDATE ON audit.review_cases",
    "GRANT INSERT ON TABLE audit.review_cases",
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
    migration_14 = (platform / "alembic/versions/0014_review_session_authority.py").read_text(
        encoding="utf-8"
    )
    migration_15 = (platform / "alembic/versions/0015_review_case_lifecycle.py").read_text(
        encoding="utf-8"
    )
    session_py = (platform / "src/uap_platform/review/session.py").read_text(encoding="utf-8")
    cases_py = (platform / "src/uap_platform/review/cases.py").read_text(encoding="utf-8")
    errors_py = (platform / "src/uap_platform/review/errors.py").read_text(encoding="utf-8")
    probe1 = (platform / "tools/wp9_1_runtime_probe.py").read_text(encoding="utf-8")
    probe2 = (platform / "tools/wp9_2_runtime_probe.py").read_text(encoding="utf-8")
    tests = (platform / "tests/test_wp9_session.py").read_text(encoding="utf-8")
    case_tests = (platform / "tests/test_wp9_cases.py").read_text(encoding="utf-8")
    makefile = (platform / "Makefile").read_text(encoding="utf-8")
    ci = (repository / ".github/workflows/platform-ci.yml").read_text(encoding="utf-8")
    chain = (platform / "scripts/verify-migration-chain.sh").read_text(encoding="utf-8")
    missing = [path for path in REQUIRED_FILES if not (repository / path).is_file()]
    combined = migration_14 + "\n" + migration_15
    forbidden_hits = [token for token in FORBIDDEN_STAGE_TOKENS if token in combined]
    grant_hits = [token for token in FORBIDDEN_GRANTS if token in combined]
    return [
        check("required_files", not missing, missing, []),
        check(
            "unique_wp9_2_head",
            heads == [WP92_HEAD] and revision_ids[:2] == [WP92_HEAD, WP92_PARENT],
            {"heads": heads, "prefix": revision_ids[:2]},
            {"heads": [WP92_HEAD], "prefix": [WP92_HEAD, WP92_PARENT]},
        ),
        check(
            "migration_links",
            f'revision = "{WP91_HEAD}"' in migration_14
            and f'down_revision = "{WP91_PARENT}"' in migration_14
            and f'revision = "{WP92_HEAD}"' in migration_15
            and f'down_revision = "{WP92_PARENT}"' in migration_15
            and "CREATE TABLE" not in migration_14
            and "CREATE TABLE" not in migration_15,
            True,
        ),
        check(
            "require_active_role_contract",
            "CREATE FUNCTION audit.require_active_role" in migration_14
            and "session_user IS DISTINCT FROM 'uap_api'" in migration_14
            and "GRANT EXECUTE ON FUNCTION audit.require_active_role" in migration_14,
            True,
        ),
        check(
            "case_functions_contract",
            "CREATE FUNCTION audit.open_review_case" in migration_15
            and "CREATE FUNCTION audit.assign_review_case" in migration_15
            and "CREATE FUNCTION audit.close_review_case" in migration_15
            and "review_request_id_missing" in migration_15
            and "knowledge_relation_review_not_in_wp9" in migration_15
            and "review_case_already_open" in migration_15
            and "review_case_not_decidable" in migration_15
            and "review.case.open:" in migration_15
            and "review_idempotency_payload_conflict" in migration_15
            and "GRANT EXECUTE ON FUNCTION audit.open_review_case" in migration_15
            and "GRANT EXECUTE ON FUNCTION audit.assign_review_case" in migration_15
            and "GRANT EXECUTE ON FUNCTION audit.close_review_case" in migration_15,
            True,
        ),
        check("no_later_wp9_functions", not forbidden_hits, forbidden_hits, []),
        check("no_worker_or_review_dml_grants", not grant_hits, grant_hits, []),
        check(
            "python_clients",
            "set_config('uap.principal_id'" in session_py
            and "p_actor_id" not in session_py
            and "audit.open_review_case" in cases_py
            and "record_review_decision" not in cases_py,
            True,
        ),
        check(
            "g9_cases_named",
            "g9_01" in probe1
            and "g9_06" in probe2
            and "g9_07" in probe2
            and "g9_08" in probe2
            and "g9_09" in probe2
            and "g9_31" in probe2
            and "g9_34" in probe2
            and "record_review_decision" not in probe2,
            True,
        ),
        check(
            "frozen_error_tokens",
            "review_request_id_missing" in errors_py
            and "review_case_already_open" in errors_py
            and "knowledge_relation_review_not_in_wp9" in errors_py,
            True,
        ),
        check(
            "quality_and_chain_wired",
            "validate_wp9.py" in makefile
            and "validate_wp9.py" in ci
            and "wp9_1_runtime_probe.py" in ci
            and "wp9_2_runtime_probe.py" in ci
            and f'= "{WP92_HEAD}"' in chain
            and '= "50"' in chain,
            True,
        ),
        check(
            "unit_tests_present",
            "require_active_role" in tests
            and "open_review_case" in case_tests,
            True,
        ),
    ]


def main() -> None:
    platform = Path(__file__).resolve().parents[1]
    results = evaluate(platform)
    print(json.dumps([asdict(item) for item in results], indent=2))
    failed = [item.name for item in results if not item.passed]
    if failed:
        raise SystemExit("WP9.2 contract failed: " + ", ".join(failed))
    print("WP9.2 contract passed")


if __name__ == "__main__":
    main()
