"""V1-2.2 append-only editorial revision history and restore."""

from __future__ import annotations

from alembic import op

revision = "0028_v12_editorial_revision_restore"
down_revision = "0027_v12_editorial_lifecycle_concurrency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        ALTER TABLE core.editorial_revisions
            DROP CONSTRAINT IF EXISTS editorial_revisions_operation_check,
            DROP CONSTRAINT IF EXISTS editorial_revisions_check;
        ALTER TABLE core.editorial_revisions
            ADD CONSTRAINT editorial_revisions_operation_check CHECK (
                operation IN ('save', 'adopt', 'trash', 'restore', 'restore_revision')
            ),
            ADD CONSTRAINT editorial_revisions_check CHECK (
                (operation IN ('save', 'adopt', 'restore_revision')) = (content IS NOT NULL)
            );

        CREATE FUNCTION audit.restore_editorial_revision(
            p_document_id uuid,
            p_source_revision_id uuid,
            p_expected_revision integer,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $restore_editorial_revision$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_document core.documents%ROWTYPE;
            v_version uuid;
            v_current core.editorial_revisions%ROWTYPE;
            v_source core.editorial_revisions%ROWTYPE;
            v_revision integer;
            v_id uuid;
            v_reason text := nullif(btrim(coalesce(p_reason, '')), '');
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_source_map jsonb;
            v_adopted_from jsonb;
        BEGIN
            IF session_user IS DISTINCT FROM 'uap_api' THEN
                RAISE EXCEPTION 'editorial_session_role_denied' USING ERRCODE = '42501';
            END IF;
            v_actor := audit.require_active_role('editorial_admin'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_document_id IS NULL OR p_source_revision_id IS NULL
               OR p_expected_revision IS NULL OR p_expected_revision < 0 THEN
                RAISE EXCEPTION 'editorial_revision_restore_request_invalid' USING ERRCODE = '22023';
            END IF;
            IF v_reason IS NOT NULL AND char_length(v_reason) > 5000 THEN
                RAISE EXCEPTION 'editorial_revision_restore_reason_too_long' USING ERRCODE = '22023';
            END IF;

            v_payload := jsonb_build_object(
                'operation', 'restore_revision',
                'document_id', lower(p_document_id::text),
                'source_revision_id', lower(p_source_revision_id::text),
                'expected_revision', p_expected_revision,
                'reason', v_reason
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'editorial.restore_revision:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;
            PERFORM pg_advisory_xact_lock(9176, hashtext(p_document_id::text));
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            SELECT document.* INTO v_document
              FROM core.documents AS document
             WHERE document.id = p_document_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'editorial_document_not_found' USING ERRCODE = '02000';
            END IF;
            IF v_document.deleted_at IS NOT NULL THEN
                RAISE EXCEPTION 'editorial_document_trashed' USING ERRCODE = '22023';
            END IF;
            SELECT version.id INTO v_version
              FROM core.document_versions AS version
             WHERE version.document_id = p_document_id
             ORDER BY version.version_no DESC, version.id DESC
             LIMIT 1;
            IF v_version IS NULL THEN
                RAISE EXCEPTION 'editorial_document_version_missing' USING ERRCODE = '02000';
            END IF;

            SELECT revision.* INTO v_source
              FROM core.editorial_revisions AS revision
             WHERE revision.id = p_source_revision_id
               AND revision.document_version_id = v_version
               AND revision.content IS NOT NULL;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'editorial_revision_not_found' USING ERRCODE = '02000';
            END IF;
            SELECT revision.* INTO v_current
              FROM core.editorial_revisions AS revision
             WHERE revision.document_version_id = v_version
             ORDER BY revision.revision_no DESC, revision.id DESC
             LIMIT 1;
            v_revision := coalesce(v_current.revision_no, 0);
            IF v_revision <> p_expected_revision THEN
                RAISE EXCEPTION 'editorial_revision_conflict' USING ERRCODE = '23505';
            END IF;

            v_source_map := jsonb_build_object(
                'source', 'editorial_revision',
                'source_revision_id', v_source.id,
                'source_revision_no', v_source.revision_no,
                'reason', v_reason
            );
            v_adopted_from := jsonb_build_object(
                'operation', 'restore',
                'source_revision_id', v_source.id,
                'source_revision_no', v_source.revision_no,
                'reason', v_reason
            );
            v_id := gen_random_uuid();
            INSERT INTO core.editorial_revisions (
                id, document_version_id, revision_no, parent_revision_id,
                base_revision_no, operation, content, source_map, adopted_from,
                created_by, created_at
            ) VALUES (
                v_id, v_version, v_revision + 1, v_current.id,
                p_expected_revision, 'restore_revision', v_source.content,
                v_source_map, v_adopted_from, v_actor, clock_timestamp()
            );
            PERFORM audit._append_editorial_audit(
                v_key,
                'editorial.revision_restored',
                p_document_id,
                v_request,
                CASE WHEN v_current.content IS NULL THEN NULL
                     ELSE audit._payload_sha256(v_current.content) END,
                audit._payload_sha256(v_source.content),
                jsonb_build_object(
                    'payload_sha256', v_sha,
                    'document_version_id', v_version,
                    'source_revision_id', v_source.id,
                    'source_revision_no', v_source.revision_no,
                    'previous_current_revision_id', v_current.id,
                    'previous_current_revision_no', v_current.revision_no,
                    'new_revision_id', v_id,
                    'new_revision_no', v_revision + 1,
                    'reason', v_reason
                )
            );
            RETURN v_id;
        END
        $restore_editorial_revision$;

        REVOKE ALL ON FUNCTION audit.restore_editorial_revision(uuid, uuid, integer, text)
            FROM PUBLIC, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        GRANT EXECUTE ON FUNCTION audit.restore_editorial_revision(uuid, uuid, integer, text)
            TO uap_api;
        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;
        DO $restore_history_guard$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM core.editorial_revisions
                 WHERE operation = 'restore_revision'
            ) THEN
                RAISE EXCEPTION 'editorial_revision_restore_history_must_be_empty_before_downgrade'
                    USING ERRCODE = '55000';
            END IF;
        END
        $restore_history_guard$;
        REVOKE ALL ON FUNCTION audit.restore_editorial_revision(uuid, uuid, integer, text)
            FROM PUBLIC, uap_api;
        DROP FUNCTION IF EXISTS audit.restore_editorial_revision(uuid, uuid, integer, text);
        ALTER TABLE core.editorial_revisions
            DROP CONSTRAINT IF EXISTS editorial_revisions_operation_check,
            DROP CONSTRAINT IF EXISTS editorial_revisions_check;
        ALTER TABLE core.editorial_revisions
            ADD CONSTRAINT editorial_revisions_operation_check CHECK (
                operation IN ('save', 'adopt', 'trash', 'restore')
            ),
            ADD CONSTRAINT editorial_revisions_check CHECK (
                (operation IN ('save', 'adopt')) = (content IS NOT NULL)
            );
        RESET ROLE;
        """
    )
