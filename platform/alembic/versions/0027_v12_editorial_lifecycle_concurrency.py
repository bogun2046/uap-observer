"""V1-2.1B expected-revision wrappers for trash and restore."""

from __future__ import annotations

from alembic import op

revision = "0027_v12_editorial_lifecycle_concurrency"
down_revision = "0026_v12_editorial_reanalysis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION audit.trash_document(
            p_document_id uuid,
            p_expected_revision integer,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $trash_document_expected$
        DECLARE
            v_request uuid;
            v_existing uuid;
            v_version uuid;
            v_revision integer;
            v_sha text;
        BEGIN
            PERFORM audit.require_active_role('editorial_admin'::audit.application_role);
            v_request := audit._review_request_id();
            v_sha := audit._payload_sha256(jsonb_build_object(
                'operation', 'trash', 'document_id', lower(p_document_id::text),
                'reason', nullif(btrim(coalesce(p_reason, '')), '')
            ));
            v_existing := audit._existing_write_target(
                'editorial.trash:' || v_request::text, v_sha
            );
            IF v_existing IS NOT NULL THEN
                SELECT (event.metadata ->> 'revision_no')::integer - 1
                  INTO v_revision
                  FROM audit.audit_events AS event
                 WHERE event.event_key = 'editorial.trash:' || v_request::text;
                IF v_revision IS DISTINCT FROM p_expected_revision THEN
                    RAISE EXCEPTION 'review_idempotency_payload_conflict' USING ERRCODE = '23505';
                END IF;
                RETURN v_existing;
            END IF;
            IF p_document_id IS NULL OR p_expected_revision IS NULL OR p_expected_revision < 0 THEN
                RAISE EXCEPTION 'editorial_lifecycle_request_invalid' USING ERRCODE = '22023';
            END IF;
            PERFORM pg_advisory_xact_lock(9176, hashtext(p_document_id::text));
            v_existing := audit._existing_write_target(
                'editorial.trash:' || v_request::text, v_sha
            );
            IF v_existing IS NOT NULL THEN
                SELECT (event.metadata ->> 'revision_no')::integer - 1
                  INTO v_revision
                  FROM audit.audit_events AS event
                 WHERE event.event_key = 'editorial.trash:' || v_request::text;
                IF v_revision IS DISTINCT FROM p_expected_revision THEN
                    RAISE EXCEPTION 'review_idempotency_payload_conflict' USING ERRCODE = '23505';
                END IF;
                RETURN v_existing;
            END IF;
            SELECT version.id INTO v_version
              FROM core.document_versions AS version
             WHERE version.document_id = p_document_id
             ORDER BY version.version_no DESC, version.id DESC
             LIMIT 1;
            IF v_version IS NULL THEN
                RAISE EXCEPTION 'editorial_document_version_missing' USING ERRCODE = '02000';
            END IF;
            SELECT coalesce(max(revision.revision_no), 0)
              INTO v_revision
              FROM core.editorial_revisions AS revision
             WHERE revision.document_version_id = v_version;
            IF v_revision <> p_expected_revision THEN
                RAISE EXCEPTION 'editorial_revision_conflict' USING ERRCODE = '23505';
            END IF;
            RETURN audit.trash_document(p_document_id, p_reason);
        END
        $trash_document_expected$;

        CREATE FUNCTION audit.restore_document(
            p_document_id uuid,
            p_expected_revision integer,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $restore_document_expected$
        DECLARE
            v_request uuid;
            v_existing uuid;
            v_version uuid;
            v_revision integer;
            v_sha text;
        BEGIN
            PERFORM audit.require_active_role('editorial_admin'::audit.application_role);
            v_request := audit._review_request_id();
            v_sha := audit._payload_sha256(jsonb_build_object(
                'operation', 'restore', 'document_id', lower(p_document_id::text),
                'reason', nullif(btrim(coalesce(p_reason, '')), '')
            ));
            v_existing := audit._existing_write_target(
                'editorial.restore:' || v_request::text, v_sha
            );
            IF v_existing IS NOT NULL THEN
                SELECT (event.metadata ->> 'revision_no')::integer - 1
                  INTO v_revision
                  FROM audit.audit_events AS event
                 WHERE event.event_key = 'editorial.restore:' || v_request::text;
                IF v_revision IS DISTINCT FROM p_expected_revision THEN
                    RAISE EXCEPTION 'review_idempotency_payload_conflict' USING ERRCODE = '23505';
                END IF;
                RETURN v_existing;
            END IF;
            IF p_document_id IS NULL OR p_expected_revision IS NULL OR p_expected_revision < 0 THEN
                RAISE EXCEPTION 'editorial_lifecycle_request_invalid' USING ERRCODE = '22023';
            END IF;
            PERFORM pg_advisory_xact_lock(9176, hashtext(p_document_id::text));
            v_existing := audit._existing_write_target(
                'editorial.restore:' || v_request::text, v_sha
            );
            IF v_existing IS NOT NULL THEN
                SELECT (event.metadata ->> 'revision_no')::integer - 1
                  INTO v_revision
                  FROM audit.audit_events AS event
                 WHERE event.event_key = 'editorial.restore:' || v_request::text;
                IF v_revision IS DISTINCT FROM p_expected_revision THEN
                    RAISE EXCEPTION 'review_idempotency_payload_conflict' USING ERRCODE = '23505';
                END IF;
                RETURN v_existing;
            END IF;
            SELECT version.id INTO v_version
              FROM core.document_versions AS version
             WHERE version.document_id = p_document_id
             ORDER BY version.version_no DESC, version.id DESC
             LIMIT 1;
            IF v_version IS NULL THEN
                RAISE EXCEPTION 'editorial_document_version_missing' USING ERRCODE = '02000';
            END IF;
            SELECT coalesce(max(revision.revision_no), 0)
              INTO v_revision
              FROM core.editorial_revisions AS revision
             WHERE revision.document_version_id = v_version;
            IF v_revision <> p_expected_revision THEN
                RAISE EXCEPTION 'editorial_revision_conflict' USING ERRCODE = '23505';
            END IF;
            RETURN audit.restore_document(p_document_id, p_reason);
        END
        $restore_document_expected$;

        REVOKE ALL ON FUNCTION audit.trash_document(uuid, integer, text),
            audit.restore_document(uuid, integer, text)
            FROM PUBLIC, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        GRANT EXECUTE ON FUNCTION audit.trash_document(uuid, integer, text),
            audit.restore_document(uuid, integer, text) TO uap_api;
        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;
        REVOKE ALL ON FUNCTION audit.trash_document(uuid, integer, text),
            audit.restore_document(uuid, integer, text) FROM PUBLIC, uap_api;
        DROP FUNCTION IF EXISTS audit.trash_document(uuid, integer, text);
        DROP FUNCTION IF EXISTS audit.restore_document(uuid, integer, text);
        RESET ROLE;
        """
    )
