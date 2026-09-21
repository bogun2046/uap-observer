"""Static contract tests for the V1-3.1 editorial binding migration."""

from __future__ import annotations

from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/0030_v131_publication_editorial_revision_binding.py"
)


def source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_revision_chain_and_legacy_nullable_strategy() -> None:
    text = source()
    assert 'revision = "0030_v131_publication_editorial_revision_binding"' in text
    assert 'down_revision = "0029_v123_editorial_claim_entity_provenance"' in text
    assert "ADD COLUMN editorial_revision_id uuid" in text
    assert "ADD COLUMN editorial_revision_no integer" in text
    assert "No historical provenance is guessed or" in text
    assert "UPDATE audit.document_publication_grants SET editorial_revision_id" not in text


def test_binding_is_separate_from_publication_sequence() -> None:
    text = source()
    assert "document_publication_grants.revision_no" in text or "grant_row.revision_no" in text
    assert "publication sequence" in text.lower()
    assert "editorial_revision_id" in text
    assert "editorial_revision_no" in text
    assert "max(grant_row.revision_no)" in text
    assert "v_editorial_revision_id" in text


def test_new_workflow_requires_current_owned_editorial_revision() -> None:
    text = source()
    assert "revision.document_version_id = p_document_version_id" in text
    assert "document.deleted_at IS NULL" in text
    assert "revision.content IS NOT NULL" in text
    assert "editorial_revision_conflict" in text
    assert "review_case.editorial_revision_id" in text


def test_publication_chain_carries_editorial_identity_and_hash() -> None:
    text = source()
    for marker in (
        "document_publication_grants",
        "document_publication_manifests",
        "publication.granted",
        "publication.superseded",
        "publication.withdrawn",
    ):
        assert marker in text
    assert "'editorial_revision_id', lower(v_editorial_revision_id::text)" in text
    assert "'editorial_revision_no', v_editorial_revision_no" in text
    assert "v_sha := audit._publication_manifest_sha(v_manifest)" in text
    assert "payload_sha256', v_sha" in text


def test_editorial_admin_submission_does_not_expand_decision_authority() -> None:
    text = source()
    wrapper = text[text.index("CREATE FUNCTION audit.open_document_publication_review_case") :]
    assert "require_active_role('editorial_admin'::audit.application_role)" in wrapper
    assert "GRANT EXECUTE ON FUNCTION audit.open_document_publication_review_case" in wrapper
    assert "record_review_decision" not in wrapper
    assert "INSERT INTO audit.document_publication_grants" not in wrapper
    assert "editorial_admin" in text


def test_legacy_function_is_preserved_and_null_rows_delegate() -> None:
    text = source()
    assert "RENAME TO _apply_publication_grant_legacy" in text
    assert "_apply_publication_grant_legacy(" in text
    assert "IF v_editorial_revision_id IS NULL" in text
    assert "RENAME TO _apply_publication_grant" in text[text.index("def downgrade") :]


def test_migration_does_not_start_publication_or_model_execution() -> None:
    text = source().lower()
    assert "apply_publication_event" not in text
    assert "claim_publication_outbox" not in text
    assert "deepseek" not in text
    assert "create table public." not in text
    assert "insert into public." not in text
