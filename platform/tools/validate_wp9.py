"""Validate the WP9.1 review-session freeze. Later WP9.x functions must be absent."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

WP91_HEAD = "0014_review_session_authority"
WP91_PARENT = "0013_entity_merge_state_machine"
REQUIRED_FILES = (
    "docs/wp9/implementation-ticket.md",
    "docs/wp9/acceptance-ticket.md",
    "docs/wp9/acceptance-cases.md",
    "docs/wp9/adr/0014-review-session-and-write-authority.md",
    "platform/alembic/versions/0014_review_session_authority.py",
    "platform/src/uap_platform/review/__init__.py",
    "platform/src/uap_platform/review/errors.py",
    "platform/src/uap_platform/review/session.py",
    "platform/tests/test_wp9_session.py",
    "platform/tests/test_wp9_foundation.py",
    "platform/tools/validate_wp9.py",
    "platform/tools/wp9_1_runtime_probe.py",
)
FORBIDDEN_STAGE_TOKENS = (
    "CREATE FUNCTION audit.open_review_case",
    "CREATE FUNCTION audit.assign_review_case",
    "CREATE FUNCTION audit.close_review_case",
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
    "require_active_role(audit.application_role) TO uap_worker",
    "require_active_role(audit.application_role) TO uap_publisher",
    "require_active_role(audit.application_role) TO uap_scheduler",
    "require_active_role(audit.application_role) TO uap_public_reader",
    "require_active_role(audit.application_role) TO uap_model_governance",
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
    migration = (platform / "alembic/versions/0014_review_session_authority.py").read_text(
        encoding="utf-8"
    )
    session_py = (platform / "src/uap_platform/review/session.py").read_text(encoding="utf-8")
    errors_py = (platform / "src/uap_platform/review/errors.py").read_text(encoding="utf-8")
    probe = (platform / "tools/wp9_1_runtime_probe.py").read_text(encoding="utf-8")
    tests = (platform / "tests/test_wp9_session.py").read_text(encoding="utf-8")
    makefile = (platform / "Makefile").read_text(encoding="utf-8")
    ci = (repository / ".github/workflows/platform-ci.yml").read_text(encoding="utf-8")
    chain = (platform / "scripts/verify-migration-chain.sh").read_text(encoding="utf-8")
    missing = [path for path in REQUIRED_FILES if not (repository / path).is_file()]
    forbidden_hits = [token for token in FORBIDDEN_STAGE_TOKENS if token in migration]
    grant_hits = [token for token in FORBIDDEN_GRANTS if token in migration]
    return [
        check("required_files", not missing, missing, []),
        check(
            "unique_wp9_1_head",
            heads == [WP91_HEAD] and revision_ids[:2] == [WP91_HEAD, WP91_PARENT],
            {"heads": heads, "prefix": revision_ids[:2]},
            {"heads": [WP91_HEAD], "prefix": [WP91_HEAD, WP91_PARENT]},
        ),
        check(
            "migration_links_wp8",
            f'revision = "{WP91_HEAD}"' in migration
            and f'down_revision = "{WP91_PARENT}"' in migration
            and "CREATE TABLE" not in migration,
            True,
        ),
        check(
            "require_active_role_contract",
            "CREATE FUNCTION audit.require_active_role" in migration
            and "SECURITY DEFINER" in migration
            and "SET search_path = audit, pg_catalog" in migration
            and "session_user IS DISTINCT FROM 'uap_api'" in migration
            and "review_session_role_denied" in migration
            and "review_principal_missing" in migration
            and "review_service_principal_denied" in migration
            and "review_role_denied" in migration
            and "review_scope_unsupported" in migration
            and "senior_reviewer" in migration
            and "REVOKE ALL ON FUNCTION audit.require_active_role" in migration
            and "TO uap_api" in migration
            and "GRANT EXECUTE ON FUNCTION audit.require_active_role" in migration,
            True,
        ),
        check(
            "append_audit_event_internal",
            "CREATE FUNCTION audit.append_audit_event" in migration
            and "REVOKE ALL ON FUNCTION audit.append_audit_event" in migration
            and "GRANT EXECUTE ON FUNCTION audit.append_audit_event" not in migration,
            True,
        ),
        check("no_later_wp9_functions", not forbidden_hits, forbidden_hits, []),
        check("no_worker_or_review_dml_grants", not grant_hits, grant_hits, []),
        check(
            "python_guc_local_only",
            "set_config('uap.principal_id'" in session_py
            and ", true)" in session_py
            and "p_actor_id" not in session_py
            and "open_review_case" not in session_py,
            True,
        ),
        check(
            "g9_01_05_named",
            "g9_01" in probe
            and "g9_02" in probe
            and "g9_03" in probe
            and "g9_04" in probe
            and "g9_05" in probe
            and "review_self_review_denied" not in probe
            and "open_review_case" not in probe,
            True,
        ),
        check(
            "frozen_error_tokens",
            "review_principal_missing" in errors_py
            and "review_session_role_denied" in errors_py
            and "review_role_denied" in errors_py,
            True,
        ),
        check(
            "quality_and_chain_wired",
            "validate_wp9.py" in makefile
            and "validate_wp9.py" in ci
            and "wp9_1_runtime_probe.py" in ci
            and f'= "{WP91_HEAD}"' in chain
            and '= "50"' in chain,
            True,
        ),
        check("unit_tests_present", "require_active_role" in tests and "set_config" in tests, True),
    ]


def main() -> None:
    platform = Path(__file__).resolve().parents[1]
    results = evaluate(platform)
    print(json.dumps([asdict(item) for item in results], indent=2))
    failed = [item.name for item in results if not item.passed]
    if failed:
        raise SystemExit("WP9.1 contract failed: " + ", ".join(failed))
    print("WP9.1 contract passed")


if __name__ == "__main__":
    main()
