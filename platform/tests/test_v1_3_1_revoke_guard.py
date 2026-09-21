"""V1-3.1 pre-publication revoke boundary contract tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

from uap_platform.admin_api.errors import AdminError

PLATFORM = Path(__file__).resolve().parents[1]
MIGRATION_0030 = PLATFORM / "alembic/versions/0030_v131_publication_editorial_revision_binding.py"
MIGRATION_0031 = PLATFORM / "alembic/versions/0031_v131_prepublication_revoke_guard.py"
PUBLISHER_MIGRATION = PLATFORM / "alembic/versions/0021_wp10_publisher_projection.py"
RUNTIME_PROBE = PLATFORM / "tools/v1_3_1_revoke_guard_probe.py"

FROZEN_0030_SHA256 = "6229aad96909fbb69528eacfac0ce002a327610e4692d4216d9600723ea81e8e"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_0030_remains_byte_identical_to_migration_gate() -> None:
    assert hashlib.sha256(MIGRATION_0030.read_bytes()).hexdigest() == FROZEN_0030_SHA256


def test_0031_is_a_narrow_forward_migration() -> None:
    text = source(MIGRATION_0031)
    assert 'revision = "0031_v131_prepublication_revoke_guard"' in text
    assert 'down_revision = "0030_v131_publication_editorial_revision_binding"' in text
    assert "CREATE TABLE" not in text
    assert "ALTER TABLE" not in text
    assert "INSERT INTO public." not in text
    assert "DELETE FROM public." not in text
    assert "UPDATE public." not in text
    assert "publication.withdrawn" not in text


def test_guard_uses_canonical_ack_live_lease_and_public_projection_signals() -> None:
    text = source(MIGRATION_0031)
    assert "event.published_at" in text
    assert "event.lease_token" in text
    assert "event.lease_expires_at" in text
    assert "event.event_type = 'publication.granted'" in text
    assert "FOR UPDATE" in text
    assert "'publication-document:' || p_subject_id::text" in text
    assert "document.document_grant_id = v_grant_id" in text
    assert "publication_already_projected" in text


def test_guard_stays_inside_database_authority_boundary_and_preserves_acl() -> None:
    text = source(MIGRATION_0031)
    assert "SECURITY DEFINER" in text
    assert "OWNER TO uap_owner" in text
    assert text.count("FROM PUBLIC") >= 3
    assert "GRANT EXECUTE" not in text
    assert "editorial_admin" not in text
    assert "senior_reviewer" not in text
    assert "uap_publisher" not in text


def test_downgrade_restores_exact_0030_function_entry_point() -> None:
    downgrade = source(MIGRATION_0031).split("def downgrade()", 1)[1]
    assert "DROP FUNCTION audit._apply_publication_grant(" in downgrade
    assert "RENAME TO _apply_publication_grant" in downgrade
    assert "publication_already_projected" not in downgrade
    assert "DELETE FROM" not in downgrade
    assert "UPDATE " not in downgrade


def test_existing_apply_path_suppresses_a_revoked_queued_grant() -> None:
    text = source(PUBLISHER_MIGRATION)
    assert "event_row.event_type = 'publication.granted'" in text
    assert "v_grant_status <> 'active'::audit.grant_status" in text
    assert "valid stale event" in text
    assert "v_present := false" in text


def test_post_projection_revoke_has_stable_http_conflict() -> None:
    error = AdminError("publication_already_projected")
    assert error.code == "publication_already_projected"
    assert error.status == 409
    assert error.detail


def test_runtime_probe_covers_revoke_contract_and_race_boundaries() -> None:
    text = source(RUNTIME_PROBE)
    assert "audit.record_review_decision" in text
    assert "review_idempotency_payload_conflict" in text
    assert "publication_already_projected" in text
    assert "PublicationService" in text
    assert "service.apply(pre_claim)" in text
    assert "revoked queued grant became public" in text
    assert "post-publication rejection mutated state" in text
    assert "live lease did not fence revoke" in text
    assert 'bind_role(admin, senior, "senior_reviewer")' in text
    assert 'bind_role(admin, editorial_admin, "editorial_admin")' in text
    assert "public.search_documents WHERE document_id=%s" in text
