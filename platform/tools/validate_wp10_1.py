"""Validate the WP10.1 publication-authority freeze contract."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

CURRENT_HEAD = "0020_wp10_publication_contract"
PARENT_HEAD = "0019_manual_claims_binding"

REQUIRED_FILES = (
    "docs/wp10/implementation-ticket.md",
    "docs/wp10/acceptance-ticket.md",
    "docs/wp10/acceptance-cases.md",
    "docs/wp10/migration-plan.md",
    "docs/wp10/projection-contract.md",
    "docs/wp10/api-contract.md",
    "docs/wp10/permissions.md",
    "docs/wp10/error-codes.md",
    "docs/wp10/implementation-start.md",
    "docs/wp10/runtime-validation.md",
    "docs/wp10/validation-results-20260828.md",
    "docs/wp10/adr/0020-wp10-scope-and-authority.md",
    "docs/wp10/adr/0021-publication-manifests.md",
    "docs/wp10/adr/0022-outbox-projector.md",
    "docs/wp10/adr/0023-http-consistency-and-auth.md",
    "docs/wp10/adr/0024-relations-remain-closed.md",
    "platform/alembic/versions/0020_wp10_publication_contract.py",
    "platform/tools/validate_wp10_1.py",
    "platform/tools/wp10_1_runtime_probe.py",
    "platform/tools/wp10_1_migration_probe.py",
    "platform/tests/test_wp10_foundation.py",
)

PUBLIC_ENUMS = {
    "document_category": (
        "official_report",
        "government_document",
        "military",
        "scientific_research",
        "historical_event",
        "sighting",
        "disputed_event",
        "other",
    ),
    "fact_status": (
        "official_record",
        "corroborated",
        "source_reported",
        "unverified",
        "disputed",
        "opinion",
    ),
}

MANIFEST_TABLES = (
    "audit.document_publication_manifests",
    "audit.claim_publication_manifests",
    "audit.claim_publication_manifest_evidence",
    "audit.entity_publication_manifests",
    "audit.document_public_identities",
    "audit.claim_public_identities",
    "audit.evidence_public_identities",
    "audit.entity_public_identities",
    "audit.publication_quarantine",
)

FORBIDDEN_LATER_STAGE_TOKENS = (
    "CREATE FUNCTION ops.claim_publication_outbox",
    "CREATE FUNCTION ops.apply_publication_event",
    "CREATE FUNCTION ops.fail_publication_event",
    "CREATE FUNCTION audit.requeue_publication_event",
    "CREATE FUNCTION ops.rebuild_public_projection",
    "CREATE TABLE audit.publication_delivery_attempts",
    "fastapi",
    "uvicorn",
    "CREATE ROUTE",
)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    actual: object
    expected: object


def check(name: str, passed: bool, actual: object, expected: object = True) -> Check:
    return Check(name, passed, actual, expected)


def _ordered(sql: str, first: str, second: str) -> bool:
    left = sql.find(first)
    right = sql.find(second)
    return left >= 0 and right >= 0 and left < right


def _compact_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().upper()


def _strip_sql_comments(sql: str) -> str:
    without_blocks = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\r\n]*", " ", without_blocks)


def _sql_identifiers(value: str) -> set[str] | None:
    identifiers: set[str] = set()
    for item in value.split(","):
        token = item.strip()
        if re.fullmatch(r'"(?:[^"]|"")+"|[A-Z_][A-Z0-9_$]*', token, re.IGNORECASE) is None:
            return None
        if token.startswith('"'):
            token = token[1:-1].replace('""', '"')
        identifiers.add(token.upper())
    return identifiers


def _sql_grantees(value: str) -> tuple[set[str], bool] | None:
    roles: set[str] = set()
    grants_to_public = False
    for item in value.split(","):
        token = item.strip()
        if token.upper() == "PUBLIC":
            grants_to_public = True
            continue
        identifiers = _sql_identifiers(token)
        if identifiers is None:
            return None
        roles.update(identifiers)
    return roles, grants_to_public


def _mentions_protected_grantee(value: str) -> bool:
    return bool(
        re.search(r'"?UAP_API"?', value, re.IGNORECASE)
        or re.search(r'(?<!")\bPUBLIC\b(?!")', value, re.IGNORECASE)
    )


def _has_forbidden_api_default_dml(migration: str) -> bool:
    cleaned = _strip_sql_comments(migration)
    statements = re.finditer(
        r"ALTER\s+DEFAULT\s+PRIVILEGES\b.*?;",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    forbidden_schemas = {"CORE", "OPS", "AUDIT"}
    forbidden_privileges = {"INSERT", "UPDATE", "DELETE"}
    for statement_match in statements:
        statement = _compact_sql(statement_match.group(0))
        grant = re.search(
            r"\bGRANT\s+(?P<privileges>.*?)\s+ON\s+TABLES\s+TO\s+"
            r"(?P<roles>.*?)(?:\s+WITH\s+GRANT\s+OPTION)?\s*;$",
            statement,
            flags=re.IGNORECASE,
        )
        if grant is None:
            if re.search(r"\bTO\s+", statement, re.IGNORECASE) and _mentions_protected_grantee(
                statement
            ):
                return True
            continue
        grantees = _sql_grantees(grant.group("roles"))
        if grantees is None:
            if _mentions_protected_grantee(grant.group("roles")):
                return True
            continue
        roles, grants_to_public = grantees
        if "UAP_API" not in roles and not grants_to_public:
            continue
        privileges = {item.strip().upper() for item in grant.group("privileges").split(",")}
        grants_dml = bool(privileges & forbidden_privileges) or any(
            item in {"ALL", "ALL PRIVILEGES"} for item in privileges
        )
        if not grants_dml:
            continue
        header = statement[: grant.start()]
        schema_match = re.search(r"\bIN\s+SCHEMA\s+(?P<schemas>.+?)\s*$", header)
        if schema_match is None:
            return True
        schemas = _sql_identifiers(schema_match.group("schemas"))
        if schemas is None or schemas & forbidden_schemas:
            return True
    return False


def permission_closure_is_valid(migration: str) -> bool:
    """Check the frozen API/Publisher grants without accepting default API DML."""
    compact = _compact_sql(_strip_sql_comments(migration))
    required = tuple(
        _compact_sql(token)
        for token in (
            "REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA core, ops, audit FROM uap_api",
            "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA core, ops FROM uap_api",
            "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA audit FROM uap_api",
            (
                "REVOKE ALL ON FUNCTION "
                "audit.require_claim_publication_manifest_evidence() FROM PUBLIC"
            ),
            "REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM uap_publisher",
            "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA core, audit FROM uap_publisher",
            "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ops FROM uap_publisher",
            (
                "ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner IN SCHEMA public "
                "GRANT INSERT, UPDATE, DELETE ON TABLES TO uap_publisher"
            ),
        )
    )
    if not all(token in compact for token in required):
        return False

    return not _has_forbidden_api_default_dml(migration)


def evaluate(platform: Path, *, allow_later_head: bool = False) -> list[Check]:
    platform = platform.resolve()
    repository = platform.parent
    migration_path = platform / "alembic/versions/0020_wp10_publication_contract.py"
    migration = migration_path.read_text(encoding="utf-8") if migration_path.is_file() else ""

    config = Config(str(platform / "alembic.ini"))
    config.set_main_option("script_location", str(platform / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    revision = script.get_revision(CURRENT_HEAD)
    revision_ids = {item.revision for item in script.walk_revisions(base="base", head="heads")}

    missing = [path for path in REQUIRED_FILES if not (repository / path).is_file()]
    enum_hits = {
        name: all(repr(value)[1:-1] in migration for value in values)
        for name, values in PUBLIC_ENUMS.items()
    }
    table_hits = {name: f"CREATE TABLE {name}" in migration for name in MANIFEST_TABLES}
    lowered_migration = migration.lower()
    forbidden_hits = [
        token for token in FORBIDDEN_LATER_STAGE_TOKENS if token.lower() in lowered_migration
    ]
    public_tables = (
        "documents",
        "claims",
        "evidence",
        "claim_evidence",
        "entities",
        "document_entities",
        "relations",
        "relation_evidence",
        "search_documents",
    )
    preflight = migration.find("DO $wp10_public_preflight$")
    downgrade_start = migration.find("def downgrade")
    downgrade_sql = migration[downgrade_start:] if downgrade_start >= 0 else ""
    downgrade_sql_compact = " ".join(downgrade_sql.split())
    first_destructive = min(
        (
            position
            for position in (downgrade_sql.find("DROP TABLE"), downgrade_sql.find("ALTER TABLE"))
            if position >= 0
        ),
        default=-1,
    )
    downgrade_guard = downgrade_sql.find("DO $wp10_downgrade_guard$")
    public_downgrade_guard = downgrade_sql.find("DO $wp10_public_downgrade_guard$")
    typed_contract = all(
        token in migration
        for token in (
            "publication-manifest.v2",
            "manifest_sha256",
            "audit._publication_manifest_sha",
            "summary_analysis_result_id",
            "canonical_source_url",
            "claim_publication_manifest_evidence",
        )
    )
    claim_order = _ordered(
        migration,
        "PERFORM audit._replace_claim_evidence",
        "PERFORM audit._apply_publication_grant",
    ) and _ordered(
        migration,
        "INSERT INTO audit.review_decisions",
        "PERFORM audit._apply_claim_subject_bind",
    )
    legacy_contract = all(
        token in migration
        for token in (
            "publication_manifest_required",
            "publication-outbox.v1",
            "terminal_at",
            "terminal_error_code",
            "ck_outbox_published_terminal",
        )
    )
    lock_contract = all(
        token in migration
        for token in (
            "SELECT grant_row.id",
            "FOR UPDATE OF grant_row",
            "Claim publication and document withdrawal/revision serialize",
            "FOR UPDATE;",
        )
    )
    quarantine_resolution = all(
        token in migration
        for token in (
            "CREATE FUNCTION audit._resolve_publication_quarantine",
            "resolves_quarantine_id",
            "resolved_by_v2_revision",
            "PERFORM audit._resolve_publication_quarantine(v_table, v_old)",
            "REVOKE ALL ON FUNCTION audit._resolve_publication_quarantine(text, uuid) FROM PUBLIC",
            "DROP FUNCTION audit._resolve_publication_quarantine(text, uuid)",
        )
    )
    timezone_contract = all(
        token in migration
        for token in (
            "SET timezone = 'UTC'",
            "AT TIME ZONE 'UTC'",
            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"',
        )
    )
    downgrade_wp9_behavior = (
        all(
            token in downgrade_sql
            for token in (
                "bind_subject_entity_id",
                "replace_supporting_span_ids",
                "retire_supporting_evidence",
                "PERFORM audit._apply_claim_subject_bind",
                "PERFORM audit._replace_claim_evidence",
                "PERFORM audit._retire_manual_claim_supports",
            )
        )
        and (
            "IF v_type IS DISTINCT FROM 'claim'::audit.review_case_type THEN RAISE EXCEPTION "
            "'review_structured_changes_unsupported'" in downgrade_sql_compact
        )
        and (
            "v_type IS DISTINCT FROM 'document'::audit.review_case_type"
            not in downgrade_sql_compact
        )
        and _ordered(
            downgrade_sql_compact,
            "INSERT INTO audit.review_decisions",
            "PERFORM audit._apply_publication_grant",
        )
        and _ordered(
            downgrade_sql_compact,
            "PERFORM audit._apply_publication_grant",
            "UPDATE audit.review_cases",
        )
        and _ordered(
            downgrade_sql_compact,
            "UPDATE audit.review_cases",
            "IF p_structured_changes ? 'bind_subject_entity_id'",
        )
    )
    downgrade_api_permissions = all(
        token in downgrade_sql_compact
        for token in (
            "audit.open_review_case(",
            "audit.assign_review_case(uuid, uuid)",
            "audit.close_review_case(uuid, text)",
            "audit.select_analysis_result(uuid, text)",
            "audit.accept_entity_candidate(uuid, text)",
            "audit.bind_entity_candidate(uuid, uuid, text)",
            "audit.apply_entity_merge(uuid, uuid, text)",
            "audit.apply_entity_merge_reverse(uuid, text)",
            "audit.create_manual_claim(",
        )
    )
    implementation_start = (
        (repository / "docs/wp10/implementation-start.md").read_text(encoding="utf-8")
        if (repository / "docs/wp10/implementation-start.md").is_file()
        else ""
    )
    implementation_evidence = all(
        token in implementation_start
        for token in (
            "G10-FROZEN-20260828-01",
            "e9a5587e4a87da22eeef2f3512938db4871db6a8",
            "按以上要求，开工",  # noqa: RUF001
            "不签署 `G10-GATE-10.1`",
            "不得进入 WP10.2",
        )
    )
    runtime_evidence = all(
        (repository / path).is_file() and marker in (repository / path).read_text(encoding="utf-8")
        for path, marker in (
            ("platform/tools/wp10_1_runtime_probe.py", "G10-03"),
            ("platform/tools/wp10_1_runtime_probe.py", "G10-04"),
            ("platform/tools/wp10_1_migration_probe.py", "G10-05"),
        )
    )
    validation_record_path = repository / "docs/wp10/validation-results-20260828.md"
    validation_record = validation_record_path.is_file() and all(
        marker in validation_record_path.read_text(encoding="utf-8")
        for marker in (
            "246 passed",
            "13 passed",
            "G10-05 migration probe passed",
            "WP10.1 runtime validation passed: G10-03 G10-04",
            "publication_document_grant_required",
            "review_structured_changes_unsupported",
            "publication_contract_state_blocks_downgrade",
            "SHA256SUMS",
        )
    )
    permission_closure = permission_closure_is_valid(migration)
    relation_closed = all(
        token not in migration
        for token in (
            "CREATE FUNCTION audit._relation_publication_payload",
            "CREATE FUNCTION ops.apply_relation",
            "relation_publication_manifests",
        )
    )

    results = [
        check(
            "single WP10.1 head",
            heads == [CURRENT_HEAD] or (allow_later_head and CURRENT_HEAD in revision_ids),
            heads,
            [CURRENT_HEAD],
        ),
        check(
            "WP10.1 parent",
            (revision.down_revision if revision else None) == PARENT_HEAD,
            revision.down_revision if revision else None,
            PARENT_HEAD,
        ),
        check(
            "linear chain contains parent", PARENT_HEAD in revision_ids, PARENT_HEAD in revision_ids
        ),
        check("required docs/code files", missing == [], missing, []),
        check(
            "public enum declarations",
            enum_hits == {name: True for name in PUBLIC_ENUMS},
            enum_hits,
            {name: True for name in PUBLIC_ENUMS},
        ),
        check(
            "manifest/quarantine tables",
            table_hits == {name: True for name in MANIFEST_TABLES},
            table_hits,
            {name: True for name in MANIFEST_TABLES},
        ),
        check(
            "public projection preflight covers every table",
            preflight >= 0
            and {name: f"public.{name}" in migration[preflight:] for name in public_tables}
            == {name: True for name in public_tables},
            {name: f"public.{name}" in migration[preflight:] for name in public_tables},
            {name: True for name in public_tables},
        ),
        check(
            "typed manifest schema and hash",
            typed_contract,
            typed_contract,
        ),
        check(
            "decision captures post-change claim state",
            claim_order,
            claim_order,
        ),
        check(
            "legacy quarantine and terminal closure",
            legacy_contract,
            legacy_contract,
        ),
        check("claim/document grant lock contract", lock_contract, lock_contract),
        check("legacy quarantine resolution path", quarantine_resolution, quarantine_resolution),
        check("timezone-independent manifest hash", timezone_contract, timezone_contract),
        check(
            "downgrade restores 0019 structured changes",
            downgrade_wp9_behavior,
            downgrade_wp9_behavior,
        ),
        check(
            "downgrade restores 0019 API grants",
            downgrade_api_permissions,
            downgrade_api_permissions,
        ),
        check("implementation start evidence", implementation_evidence, implementation_evidence),
        check("runtime and migration probe coverage", runtime_evidence, runtime_evidence),
        check("recorded validation results", validation_record, validation_record),
        check(
            "fail-closed downgrade guards precede destructive DDL",
            downgrade_guard >= 0
            and public_downgrade_guard >= 0
            and downgrade_guard < first_destructive
            and public_downgrade_guard < first_destructive,
            downgrade_guard >= 0
            and public_downgrade_guard >= 0
            and downgrade_guard < first_destructive
            and public_downgrade_guard < first_destructive,
        ),
        check(
            "API and publisher permission closure",
            permission_closure,
            permission_closure,
        ),
        check("no later-stage implementation", forbidden_hits == [], forbidden_hits, []),
        check(
            "no relation implementation",
            relation_closed,
            relation_closed,
        ),
    ]
    return results


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("platform", nargs="?", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    checks = evaluate(Path(args.platform), allow_later_head=True)
    print(json.dumps([asdict(item) for item in checks], ensure_ascii=False, indent=2))
    failed = [item.name for item in checks if not item.passed]
    if failed:
        raise SystemExit("WP10.1 validation failed: " + ", ".join(failed))
    print(
        "WP10.1 static contract validation passed; run wp10_1_runtime_probe.py "
        "and wp10_1_migration_probe.py for runtime evidence."
    )


if __name__ == "__main__":
    main()
