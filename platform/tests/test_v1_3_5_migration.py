"""Contract tests for the V1-3.3 forward identifier fix."""

from __future__ import annotations

from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
MIGRATION = PLATFORM / "alembic/versions/0035_v133_rebuild_identifier_fix.py"
MIGRATION_0034 = PLATFORM / "alembic/versions/0034_v133_postpublication_withdraw_rebuild.py"


def migration_text() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_0035_is_linear_and_only_replaces_the_existing_function() -> None:
    source = migration_text()
    assert 'revision = "0035_v133_rebuild_identifier_fix"' in source
    assert 'down_revision = "0034_v133_postpublication_withdraw_rebuild"' in source
    assert "CREATE TABLE" not in source
    assert "ALTER TABLE" not in source
    assert "CREATE TYPE" not in source
    assert "pg_get_functiondef" in source
    assert "CREATE OR REPLACE FUNCTION" not in source
    assert "ops.rebuild_public_projection(uuid, uuid)" in source


def test_0035_inventory_requires_exactly_two_qualified_replacements() -> None:
    source = migration_text()
    assert "v_needle text := 'WHERE rebuild_id = p_rebuild_id'" in source
    assert "2 * length(v_needle)" in source
    assert "FROM audit.publication_rebuild_runs AS prr" in source
    assert "WHERE prr.rebuild_id = p_rebuild_id" in source
    assert source.split("def downgrade", 1)[0].count("WHERE prr.rebuild_id = p_rebuild_id") == 1


def test_0035_preserves_rebuild_contract_and_acl() -> None:
    source = migration_text()
    frozen = MIGRATION_0034.read_text(encoding="utf-8")
    for token in (
        "publication_rebuild_id_conflict",
        "publication_rebuild_mismatch",
    ):
        assert token in frozen
    for token in (
        "v133_rebuild_identifier_inventory_mismatch",
        "v133_rebuild_identifier_fix_not_applied",
        "uap_migrator",
        "uap_owner",
        "REVOKE ALL ON FUNCTION",
        "GRANT EXECUTE ON FUNCTION ops.rebuild_public_projection(uuid, uuid)",
    ):
        assert token in source


def test_0034_is_unchanged_and_still_contains_the_original_runtime_failure() -> None:
    source = MIGRATION_0034.read_text(encoding="utf-8")
    assert "WHERE rebuild_id = p_rebuild_id" in source
    assert "WHERE run.rebuild_id = p_rebuild_id" in source
    assert "CREATE FUNCTION ops.rebuild_public_projection(" in source
    assert "publication.granted" in source
    assert "published_at IS NOT NULL" in source
