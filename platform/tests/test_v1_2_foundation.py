"""Static contract tests for the V1-2.1A database foundation migration."""

from __future__ import annotations

from pathlib import Path


def migration_text() -> str:
    return (
        Path(__file__).parents[1]
        .joinpath("alembic/versions/0025_v12_editorial_foundation.py")
        .read_text(encoding="utf-8")
    )


def test_editorial_foundation_is_after_wp10_admin_replay() -> None:
    migration = migration_text()
    assert 'revision = "0025_v12_editorial_foundation"' in migration
    assert 'down_revision = "0024_wp10_admin_replay"' in migration
    assert "ADD VALUE IF NOT EXISTS 'editorial_admin'" in migration


def test_editorial_revision_is_append_only_and_references_existing_facts() -> None:
    migration = migration_text()
    assert "CREATE TABLE core.editorial_revisions" in migration
    assert "REFERENCES core.document_versions(id)" in migration
    assert "parent_revision_id, document_version_id" in migration
    assert "CREATE TRIGGER editorial_revisions_append_only" in migration
    assert "audit.reject_mutation()" in migration
    assert "analysis_result" in migration or "adopted_from" in migration
    assert "DROP TABLE IF EXISTS core.editorial_revisions" in migration


def test_editorial_content_contract_has_frozen_fields_and_safe_limits() -> None:
    migration = migration_text()
    for field in ("title", "summary", "bullets", "category", "labels", "claims", "entities"):
        assert field in migration
    for error_code in (
        "editorial_content_field_unknown",
        "editorial_title_invalid",
        "editorial_summary_invalid",
        "editorial_claim_invalid",
        "editorial_entity_invalid",
    ):
        assert error_code in migration
    assert "char_length(NEW.content ->> 'title') NOT BETWEEN 1 AND 500" in migration
    assert "char_length(NEW.content ->> 'summary') > 20000" in migration
    assert "jsonb_array_length(NEW.content -> 'bullets') > 20" in migration


def test_optimistic_concurrency_and_idempotency_are_database_bound() -> None:
    migration = migration_text()
    assert "p_expected_revision" in migration
    assert "editorial_revision_conflict" in migration
    assert "pg_advisory_xact_lock(9176" in migration
    assert "audit._existing_write_target" in migration
    assert "v_key := 'editorial.' || p_operation || ':'" in migration


def test_lifecycle_is_soft_delete_and_fail_closed_for_trashed_documents() -> None:
    migration = migration_text()
    assert "deleted_at timestamptz" in migration
    assert "deleted_by uuid REFERENCES audit.principals(id)" in migration
    assert "delete_reason text" in migration
    assert "audit.trash_document" in migration
    assert "audit.restore_document" in migration
    assert "editorial_document_trashed" in migration
    assert "editorial_document_already_trashed" in migration
    assert "editorial_document_not_trashed" in migration
    assert "operation IN ('save', 'adopt', 'trash', 'restore')" in migration


def test_editorial_functions_are_narrowly_granted() -> None:
    migration = migration_text()
    for function in (
        "audit.save_editorial_revision",
        "audit.adopt_editorial_suggestion",
        "audit.trash_document",
        "audit.restore_document",
    ):
        assert f"GRANT EXECUTE ON FUNCTION {function}" in migration
        assert f"REVOKE ALL ON FUNCTION {function}" in migration
    for principal in (
        "uap_worker",
        "uap_scheduler",
        "uap_publisher",
        "uap_model_governance",
        "uap_public_reader",
    ):
        assert principal in migration
    assert "public_authorized" not in migration
    assert "publication" not in migration.lower()


def test_downgrade_removes_contract_objects_without_rewriting_history() -> None:
    downgrade = migration_text().split("def downgrade()", 1)[1]
    assert "DROP FUNCTION IF EXISTS audit.save_editorial_revision" in downgrade
    assert "DROP FUNCTION IF EXISTS audit.adopt_editorial_suggestion" in downgrade
    assert "DROP TABLE IF EXISTS core.editorial_revisions" in downgrade
    assert "DROP COLUMN IF EXISTS deleted_at" in downgrade
    assert "cannot safely remove an enum label" in downgrade
