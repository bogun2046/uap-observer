"""Contract tests for the V1-3.1 submission-audit authority repair."""

from __future__ import annotations

import hashlib
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
MIGRATION_0030 = PLATFORM / "alembic/versions/0030_v131_publication_editorial_revision_binding.py"
MIGRATION_0031 = PLATFORM / "alembic/versions/0031_v131_prepublication_revoke_guard.py"
MIGRATION_0032 = PLATFORM / "alembic/versions/0032_v131_publication_review_submission_audit_auth.py"

FROZEN_0030_SHA256 = "6229aad96909fbb69528eacfac0ce002a327610e4692d4216d9600723ea81e8e"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_0032_is_a_narrow_forward_migration() -> None:
    text = source(MIGRATION_0032)
    assert 'revision = "0032_v131_publication_review_submission_audit_auth"' in text
    assert 'down_revision = "0031_v131_prepublication_revoke_guard"' in text
    for forbidden in (
        "CREATE TABLE",
        "ALTER TABLE",
        "INSERT INTO public.",
        "DELETE FROM public.",
        "UPDATE public.",
        "publication.granted",
        "enqueue_publication_outbox",
    ):
        assert forbidden not in text


def test_frozen_migrations_remain_unchanged() -> None:
    assert hashlib.sha256(MIGRATION_0030.read_bytes()).hexdigest() == FROZEN_0030_SHA256
    assert "0032_v131_publication_review_submission_audit_auth" not in source(MIGRATION_0031)


def test_submission_audit_helper_is_fixed_purpose_and_owner_only() -> None:
    text = source(MIGRATION_0032)
    helper = text[text.index("CREATE FUNCTION audit._append_publication_review_submission_audit") :]
    assert "require_active_role(" in helper
    assert "'editorial_admin'::audit.application_role" in helper
    assert "'review.case.open.publication'" in helper
    assert "'review_case'" in helper
    assert "OWNER TO uap_owner" in helper
    assert "FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher" in helper
    assert "GRANT EXECUTE ON FUNCTION audit._append_publication" not in helper


def test_submission_audit_binds_actor_and_full_provenance() -> None:
    text = source(MIGRATION_0032)
    for marker in (
        "v_actor := audit.require_active_role",
        "p_document_id",
        "p_document_version_id",
        "p_editorial_revision_id",
        "p_editorial_revision_no",
        "p_review_case_id",
        "p_request_id",
        "p_payload_sha256",
        "after_digest",
    ):
        assert marker in text


def test_wrapper_keeps_editorial_submit_and_no_decision_authority() -> None:
    text = source(MIGRATION_0032)
    upgrade = text.split("def downgrade()", 1)[0]
    wrapper = upgrade[upgrade.index("CREATE OR REPLACE FUNCTION audit.open_document") :]
    assert "require_active_role('editorial_admin'::audit.application_role)" in wrapper
    assert "audit._append_publication_review_submission_audit(" in wrapper
    assert "audit.append_audit_event(" not in wrapper
    assert "record_review_decision" not in wrapper
    assert "document_publication_grants" not in wrapper
    assert "document_publication_manifests" not in wrapper
    assert "outbox_events" not in wrapper


def test_wrapper_preserves_idempotency_stale_and_trash_fail_close() -> None:
    text = source(MIGRATION_0032).split("def downgrade()", 1)[0]
    assert "audit._existing_write_target(v_key, v_sha)" in text
    assert "review_idempotency_payload_conflict" not in text  # delegated unchanged
    assert "document.deleted_at IS NULL" in text
    assert "revision.id = (" in text
    assert "editorial_revision_conflict" in text
    assert "INSERT INTO audit.review_cases" in text
    assert "PERFORM audit._append_publication_review_submission_audit" in text


def test_generic_reviewer_audit_contract_is_not_replaced_or_granted() -> None:
    text = source(MIGRATION_0032)
    assert "CREATE OR REPLACE FUNCTION audit.append_audit_event" not in text
    assert "GRANT EXECUTE ON FUNCTION audit.append_audit_event" not in text
    downgrade = text.split("def downgrade()", 1)[1]
    assert "PERFORM audit.append_audit_event(" in downgrade


def test_downgrade_restores_0030_wrapper_and_drops_only_private_helper() -> None:
    downgrade = source(MIGRATION_0032).split("def downgrade()", 1)[1]
    assert "CREATE OR REPLACE FUNCTION audit.open_document_publication_review_case" in downgrade
    assert "PERFORM audit.append_audit_event(" in downgrade
    assert "DROP FUNCTION audit._append_publication_review_submission_audit(" in downgrade
    assert "DROP TABLE" not in downgrade
    assert "DELETE FROM" not in downgrade
    assert "UPDATE " not in downgrade
