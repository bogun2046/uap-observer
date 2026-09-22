"""Static contract tests for the V1-3.3 0034 migration gate."""

from __future__ import annotations

from pathlib import Path


def migration_text() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/0034_v133_postpublication_withdraw_rebuild.py"
    ).read_text(encoding="utf-8")


def test_0034_is_linear_and_scope_limited() -> None:
    source = migration_text()
    assert 'revision = "0034_v133_postpublication_withdraw_rebuild"' in source
    assert 'down_revision = "0033_v132_publication_payload_revision_compat"' in source
    assert "CREATE TABLE" not in source
    assert "CREATE TYPE" not in source
    assert "publication_quarantine" in source
    assert "publication_rebuild_runs" in source
    assert "publication.granted" in source
    assert "publication.withdrawn" in source


def test_0034_preserves_prepublication_guard_and_adds_published_withdraw() -> None:
    source = migration_text()
    assert "_apply_publication_grant_v131_prepublication_guard" in source
    assert "_apply_publication_grant_v131_unfenced" in source
    assert "publication_already_projected" in source
    assert "lease_expires_at > clock_timestamp()" in source
    assert "_v133_document_grant_was_published" in source
    assert "published_at IS NOT NULL" in source
    assert "manifest_sha256" in source
    assert "editorial_revision_id" in source
    assert "editorial_revision_no" in source


def test_single_document_rebuild_is_manifest_and_publish_evidence_bound() -> None:
    source = migration_text()
    assert "CREATE FUNCTION ops.rebuild_public_projection(" in source
    assert "p_rebuild_id uuid,\n            p_document_id uuid" in source
    assert "only migrator may rebuild publication projection" in source
    assert "publication_rebuild_id_conflict" in source
    assert "scope', 'document'" in source
    assert "publication_rebuild_mismatch" in source
    assert "claim_manifest.document_id = p_document_id" in source
    assert "claim_event.published_at IS NOT NULL" in source
    assert "event_row.published_at" in source
    assert "_wp10_3_document_manifest_payload" in source
    assert "_wp10_3_reconcile_claim" in source
    assert "_wp10_3_refresh_search" in source
    assert "v_document_public_id" in source
    assert "_wp10_3_reconcile_document_entities(v_document_public_id)" in source


def test_0034_has_no_public_or_recovery_role_expansion() -> None:
    source = migration_text()
    assert (
        "GRANT EXECUTE ON FUNCTION ops.rebuild_public_projection(uuid, uuid) TO uap_migrator"
        in source
    )
    assert (
        "GRANT EXECUTE ON FUNCTION ops.rebuild_public_projection(uuid, uuid) TO uap_api"
        not in source
    )
    assert (
        "GRANT EXECUTE ON FUNCTION ops.rebuild_public_projection(uuid, uuid) TO uap_publisher"
        not in source
    )
    assert "CREATE FUNCTION audit.requeue_publication_event" not in source
    assert "CREATE TABLE audit.publication_quarantine" not in source
    assert "DeepSeek" not in source


def test_0034_downgrade_restores_0031_function_name() -> None:
    source = migration_text()
    downgrade = source[source.index("def downgrade") :]
    assert "DROP FUNCTION ops.rebuild_public_projection(uuid, uuid)" in downgrade
    assert "DROP FUNCTION audit._apply_publication_grant(" in downgrade
    assert "RENAME TO _apply_publication_grant" in downgrade
