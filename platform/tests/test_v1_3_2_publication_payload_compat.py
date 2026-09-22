"""Static contract checks for the V1-3.2 publication payload fix."""

from __future__ import annotations

from pathlib import Path

MIGRATION = Path(__file__).parents[1] / "alembic" / "versions" / (
    "0033_v132_publication_payload_revision_compat.py"
)


def test_0033_is_forward_only_and_does_not_add_schema_objects() -> None:
    text = MIGRATION.read_text()

    assert 'revision = "0033_v132_publication_payload_revision_compat"' in text
    assert 'down_revision = "0032_v131_publication_review_submission_audit_auth"' in text
    assert "CREATE TABLE" not in text
    assert "ALTER TABLE" not in text
    assert "INSERT INTO ops.outbox_events" not in text
    assert "UPDATE audit.document_publication_grants" not in text
    assert "UPDATE audit.document_publication_manifests" not in text


def test_new_document_payload_allow_list_is_strict_but_includes_identity() -> None:
    text = MIGRATION.read_text()

    assert "editorial_revision_id" in text
    assert "editorial_revision_no" in text
    assert "payload - ARRAY[" in text
    assert "= '{}'::jsonb" in text
    assert "unknown" not in text.lower() or "unknown-field" in text.lower()


def test_identity_validator_binds_event_grant_and_manifest() -> None:
    text = MIGRATION.read_text()

    assert "_validate_publication_editorial_identity" in text
    assert "v_grant_revision_id" in text
    assert "v_manifest_revision_id" in text
    assert "publication_payload_hash_mismatch" in text
    assert "core.editorial_revisions" in text
    assert "_wp10_3_document_manifest_payload" in text


def test_legacy_payloads_remain_accepted_without_revision_identity() -> None:
    text = MIGRATION.read_text()

    assert "legacy pre-0030 event" in text
    assert "_claim_publication_v132_documents" in text
    assert "IF NOT (p_payload ? 'editorial_revision_id')" in text


def test_acl_is_narrow_to_publisher() -> None:
    text = MIGRATION.read_text()

    assert "GRANT EXECUTE ON FUNCTION ops._validate_publication_editorial_identity" in text
    assert "TO uap_publisher" in text
    assert "uap_public_reader" in text
    assert "uap_model_governance" in text
