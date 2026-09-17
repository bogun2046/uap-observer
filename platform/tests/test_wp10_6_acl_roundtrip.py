"""Regression checks for the exact 0019 ACL restored by the 0020 downgrade."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.validate_wp10_1 import permission_closure_is_valid

PLATFORM = Path(__file__).resolve().parents[1]
MIGRATION_0019 = PLATFORM / "alembic/versions/0019_manual_claims_and_subject_binding.py"
MIGRATION_0020 = PLATFORM / "alembic/versions/0020_wp10_publication_contract.py"


def _record_review_decision(source: str, occurrence: int) -> str:
    matches = list(
        re.finditer(
            r"CREATE OR REPLACE FUNCTION audit\.record_review_decision\(.*?"
            r"\$record_review_decision\$;",
            source,
            flags=re.DOTALL,
        )
    )
    return matches[occurrence].group(0)


def test_0020_downgrade_restores_exact_0019_review_function() -> None:
    source_0019 = MIGRATION_0019.read_text(encoding="utf-8")
    source_0020 = MIGRATION_0020.read_text(encoding="utf-8")
    assert _record_review_decision(source_0020, 1) == _record_review_decision(source_0019, 0)


def test_0020_downgrade_restores_only_the_four_0019_direct_dml_pairs() -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    downgrade = source[source.index("def downgrade()") :]
    broad_grant = "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA core, ops TO uap_api"
    assert broad_grant not in downgrade
    assert "ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner IN SCHEMA core, ops, audit" not in downgrade
    assert "GRANT INSERT, UPDATE, DELETE ON TABLES TO uap_api" not in downgrade
    assert (
        "GRANT INSERT, UPDATE ON\n"
        "            core.stored_objects, core.documents, core.document_versions, "
        "core.extractions\n"
        "            TO uap_api;"
    ) in downgrade


def test_0020_downgrade_restores_all_nine_0019_execute_pairs() -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    downgrade = source[source.index("def downgrade()") :]
    required = (
        "GRANT EXECUTE ON FUNCTION core.canonical_entity_id(uuid)\n"
        "            TO uap_api, uap_publisher;",
        "GRANT EXECUTE ON FUNCTION ops.claim_job(text, text, text[], integer)\n"
        "            TO uap_publisher;",
        "GRANT EXECUTE ON FUNCTION ops.classify_failure(smallint, text)\n"
        "            TO uap_publisher;",
        "GRANT EXECUTE ON FUNCTION ops.finish_job(\n"
        "            uuid, uuid, uuid, ops.attempt_outcome, smallint, text, text, integer\n"
        "        ) TO uap_publisher;",
        "GRANT EXECUTE ON FUNCTION ops.claim_outbox(text, integer, integer)\n"
        "            TO uap_publisher;",
        "GRANT EXECUTE ON FUNCTION ops.ack_outbox(uuid, uuid) TO uap_publisher;",
        "GRANT EXECUTE ON FUNCTION ops.release_outbox(uuid, uuid, text, text)\n"
        "            TO uap_publisher;",
        "GRANT EXECUTE ON FUNCTION ops.publish_outbox_failure(\n"
        "            uuid, uuid, text, text, integer\n"
        "        ) TO uap_publisher;",
    )
    assert all(statement in downgrade for statement in required)


def _migration_copy(tmp_path: Path, source: str) -> str:
    copied = tmp_path / "0020_wp10_publication_contract.py"
    copied.write_text(source, encoding="utf-8")
    return copied.read_text(encoding="utf-8")


def test_permission_closure_accepts_current_migration(tmp_path: Path) -> None:
    source = _migration_copy(tmp_path, MIGRATION_0020.read_text(encoding="utf-8"))
    assert permission_closure_is_valid(source)


@pytest.mark.parametrize("schema", ["core", "ops", "audit"])
def test_permission_closure_rejects_api_default_dml_by_schema(tmp_path: Path, schema: str) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    forbidden = (
        f"ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner IN SCHEMA {schema}\n"
        "GRANT INSERT, UPDATE, DELETE ON TABLES TO uap_api;"
    )
    assert not permission_closure_is_valid(_migration_copy(tmp_path, source + forbidden))


def test_permission_closure_rejects_original_combined_api_default_dml(
    tmp_path: Path,
) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    forbidden = (
        "ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner IN SCHEMA core, ops, audit\n"
        "GRANT INSERT, UPDATE, DELETE ON TABLES TO uap_api;"
    )
    assert not permission_closure_is_valid(_migration_copy(tmp_path, source + forbidden))


def test_permission_closure_rejects_case_and_whitespace_variant(tmp_path: Path) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    forbidden = (
        "alter   default privileges\nFOR role UAP_OWNER in schema\nAuDiT\n"
        "grant\n update , delete on tables TO Uap_Api ;"
    )
    assert not permission_closure_is_valid(_migration_copy(tmp_path, source + forbidden))


@pytest.mark.parametrize("privileges", ["ALL", "ALL PRIVILEGES"])
def test_permission_closure_rejects_api_default_all_privileges(
    tmp_path: Path, privileges: str
) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    forbidden = (
        "ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner IN SCHEMA core\n"
        f"GRANT {privileges} ON TABLES TO uap_api;"
    )
    assert not permission_closure_is_valid(_migration_copy(tmp_path, source + forbidden))


def test_permission_closure_rejects_api_in_multiple_grantees(tmp_path: Path) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    forbidden = (
        "ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner IN SCHEMA ops\n"
        "GRANT INSERT ON TABLES TO uap_api, uap_worker;"
    )
    assert not permission_closure_is_valid(_migration_copy(tmp_path, source + forbidden))


def test_permission_closure_rejects_omitted_owner(tmp_path: Path) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    forbidden = "ALTER DEFAULT PRIVILEGES IN SCHEMA audit\nGRANT UPDATE ON TABLES TO uap_api;"
    assert not permission_closure_is_valid(_migration_copy(tmp_path, source + forbidden))


def test_permission_closure_rejects_global_api_default_dml(tmp_path: Path) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    forbidden = "ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner\nGRANT DELETE ON TABLES TO uap_api;"
    assert not permission_closure_is_valid(_migration_copy(tmp_path, source + forbidden))


def test_permission_closure_rejects_quoted_identifiers_and_comments(tmp_path: Path) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    forbidden = (
        'ALTER /* default ACL */ DEFAULT PRIVILEGES FOR ROLE "uap_owner"\n'
        'IN SCHEMA "core" -- protected schema\n'
        'GRANT ALL PRIVILEGES ON TABLES TO "uap_worker", /* target */ "uap_api";'
    )
    assert not permission_closure_is_valid(_migration_copy(tmp_path, source + forbidden))


def test_permission_closure_does_not_reject_other_role_default_dml(tmp_path: Path) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    allowed = (
        "ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner IN SCHEMA core\n"
        "GRANT ALL PRIVILEGES ON TABLES TO uap_worker;"
    )
    assert permission_closure_is_valid(_migration_copy(tmp_path, source + allowed))


@pytest.mark.parametrize(
    "scope,privileges,grantees",
    [
        ("IN SCHEMA core", "INSERT", "PUBLIC"),
        ("IN SCHEMA ops", "ALL PRIVILEGES", "uap_worker, PUBLIC"),
        ("IN SCHEMA audit", "UPDATE", "PUBLIC"),
        ("IN SCHEMA core", "ALL", "PUBLIC"),
        ("", "DELETE", "PUBLIC"),
    ],
)
def test_permission_closure_rejects_public_default_dml(
    tmp_path: Path, scope: str, privileges: str, grantees: str
) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    forbidden = (
        f"ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner {scope}\n"
        f"GRANT {privileges} ON TABLES TO {grantees};"
    )
    assert not permission_closure_is_valid(_migration_copy(tmp_path, source + forbidden))


def test_permission_closure_rejects_public_with_case_newlines_and_comments(
    tmp_path: Path,
) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    forbidden = (
        "alter /* default ACL */ default privileges for role uap_owner\n"
        "in schema AuDiT -- protected scope\n"
        "grant\ninsert on tables to uap_worker, /* shared grant */ PuBlIc;"
    )
    assert not permission_closure_is_valid(_migration_copy(tmp_path, source + forbidden))


def test_permission_closure_does_not_treat_quoted_public_role_as_keyword(
    tmp_path: Path,
) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    allowed = (
        "ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner IN SCHEMA core\n"
        'GRANT INSERT ON TABLES TO "PUBLIC";'
    )
    assert permission_closure_is_valid(_migration_copy(tmp_path, source + allowed))


def test_permission_closure_requires_existing_revoke(tmp_path: Path) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    required = "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA audit FROM uap_api;"
    assert required in source
    assert not permission_closure_is_valid(
        _migration_copy(tmp_path, source.replace(required, "", 1))
    )


@pytest.mark.parametrize("comment", ["-- {statement}", "/* {statement} */"])
def test_permission_closure_rejects_commented_required_revoke(tmp_path: Path, comment: str) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    required = "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA audit FROM uap_api;"
    assert required in source
    commented = source.replace(required, comment.format(statement=required), 1)
    assert not permission_closure_is_valid(_migration_copy(tmp_path, commented))


def test_permission_closure_accepts_required_revoke_surrounded_by_comments(
    tmp_path: Path,
) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    required = "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA audit FROM uap_api;"
    replacement = f"-- explanatory comment\n{required}\n/* trailing comment */"
    assert permission_closure_is_valid(
        _migration_copy(tmp_path, source.replace(required, replacement, 1))
    )


def test_permission_closure_accepts_publisher_default_dml(tmp_path: Path) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    assert re.search(
        r"ALTER\s+DEFAULT\s+PRIVILEGES\s+FOR\s+ROLE\s+uap_owner\s+IN\s+SCHEMA\s+public"
        r"\s+GRANT\s+INSERT,\s*UPDATE,\s*DELETE\s+ON\s+TABLES\s+TO\s+uap_publisher;",
        source,
        flags=re.IGNORECASE,
    )
    assert permission_closure_is_valid(_migration_copy(tmp_path, source))


def test_permission_closure_rejects_commented_publisher_default_dml(
    tmp_path: Path,
) -> None:
    source = MIGRATION_0020.read_text(encoding="utf-8")
    required_match = re.search(
        r"ALTER\s+DEFAULT\s+PRIVILEGES\s+FOR\s+ROLE\s+uap_owner\s+IN\s+SCHEMA\s+public"
        r"\s+GRANT\s+INSERT,\s*UPDATE,\s*DELETE\s+ON\s+TABLES\s+TO\s+uap_publisher;",
        source,
        flags=re.IGNORECASE,
    )
    assert required_match is not None
    required = required_match.group(0)
    commented = source.replace(required, f"/* {required} */", 1)
    assert not permission_closure_is_valid(_migration_copy(tmp_path, commented))
