"""Audit publication review submissions under editorial authority.

The generic ``audit.append_audit_event`` entry point intentionally remains
reviewer-only.  Publication review submission is an editorial-admin action,
so this migration gives that one SECURITY DEFINER workflow a private,
fixed-purpose audit sink without expanding application-role privileges.
"""

from __future__ import annotations

from alembic import op

revision = "0032_v131_publication_review_submission_audit_auth"
down_revision = "0031_v131_prepublication_revoke_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION audit._append_publication_review_submission_audit(
            p_event_key text,
            p_review_case_id uuid,
            p_request_id uuid,
            p_document_id uuid,
            p_document_version_id uuid,
            p_editorial_revision_id uuid,
            p_editorial_revision_no integer,
            p_payload_sha256 text
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, pg_catalog
        AS $append_publication_review_submission_audit$
        DECLARE
            v_actor uuid;
            v_id uuid;
        BEGIN
            -- Resolve the actor from the authenticated request context.  The
            -- caller cannot supply or impersonate an audit principal.
            v_actor := audit.require_active_role(
                'editorial_admin'::audit.application_role
            );
            IF p_event_key IS NULL
               OR p_review_case_id IS NULL
               OR p_request_id IS NULL
               OR p_document_id IS NULL
               OR p_document_version_id IS NULL
               OR p_editorial_revision_id IS NULL
               OR p_editorial_revision_no IS NULL
               OR p_editorial_revision_no < 1
               OR p_payload_sha256 IS NULL
               OR p_payload_sha256 !~ '^[0-9a-f]{64}$'
            THEN
                RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
            END IF;

            INSERT INTO audit.audit_events (
                id, event_key, actor_id, action, target_type, target_id,
                request_id, after_digest, metadata, occurred_at
            ) VALUES (
                gen_random_uuid(),
                p_event_key,
                v_actor,
                'review.case.open.publication',
                'review_case',
                p_review_case_id,
                p_request_id,
                p_payload_sha256,
                jsonb_build_object(
                    'payload_sha256', p_payload_sha256,
                    'document_id', lower(p_document_id::text),
                    'document_version_id', lower(p_document_version_id::text),
                    'editorial_revision_id', lower(p_editorial_revision_id::text),
                    'editorial_revision_no', p_editorial_revision_no,
                    'review_case_id', lower(p_review_case_id::text)
                ),
                clock_timestamp()
            )
            RETURNING id INTO v_id;
            RETURN v_id;
        END
        $append_publication_review_submission_audit$;

        ALTER FUNCTION audit._append_publication_review_submission_audit(
            text, uuid, uuid, uuid, uuid, uuid, integer, text
        ) OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION audit._append_publication_review_submission_audit(
            text, uuid, uuid, uuid, uuid, uuid, integer, text
        ) FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
               uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;

        CREATE OR REPLACE FUNCTION audit.open_document_publication_review_case(
            p_document_version_id uuid,
            p_editorial_revision_id uuid,
            p_priority smallint,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $open_document_publication_review_case$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_case uuid;
            v_document_id uuid;
            v_revision_no integer;
        BEGIN
            v_actor := audit.require_active_role('editorial_admin'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_reason IS NULL OR char_length(btrim(p_reason)) < 10
               OR p_document_version_id IS NULL OR p_editorial_revision_id IS NULL THEN
                RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
            END IF;
            SELECT document.id, revision.revision_no
              INTO v_document_id, v_revision_no
              FROM core.editorial_revisions AS revision
              JOIN core.document_versions AS version
                ON version.id = revision.document_version_id
              JOIN core.documents AS document
                ON document.id = version.document_id
             WHERE revision.id = p_editorial_revision_id
               AND revision.document_version_id = p_document_version_id
               AND revision.content IS NOT NULL
               AND document.deleted_at IS NULL
               AND revision.id = (
                   SELECT current_revision.id
                     FROM core.editorial_revisions AS current_revision
                    WHERE current_revision.document_version_id = p_document_version_id
                      AND current_revision.content IS NOT NULL
                    ORDER BY current_revision.revision_no DESC, current_revision.id DESC
                    LIMIT 1
               );
            IF NOT FOUND THEN
                RAISE EXCEPTION 'editorial_revision_conflict' USING ERRCODE = '23505';
            END IF;
            v_payload := jsonb_build_object(
                'case_type', 'document',
                'op', 'review.case.open.publication',
                'priority', coalesce(p_priority, 0),
                'reason', p_reason,
                'subject_id', lower(p_document_version_id::text),
                'editorial_revision_id', lower(p_editorial_revision_id::text),
                'editorial_revision_no', v_revision_no
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.case.open.publication:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;
            v_case := gen_random_uuid();
            BEGIN
                INSERT INTO audit.review_cases (
                    id, document_version_id, editorial_revision_id,
                    case_type, status, priority, opened_by, opened_at
                ) VALUES (
                    v_case, p_document_version_id, p_editorial_revision_id,
                    'document'::audit.review_case_type, 'open'::audit.review_status,
                    coalesce(p_priority, 0), v_actor, clock_timestamp()
                );
            EXCEPTION
                WHEN unique_violation THEN
                    RAISE EXCEPTION 'review_case_already_open' USING ERRCODE = '23505';
            END;
            PERFORM audit._append_publication_review_submission_audit(
                v_key, v_case, v_request, v_document_id,
                p_document_version_id, p_editorial_revision_id,
                v_revision_no, v_sha
            );
            RETURN v_case;
        END
        $open_document_publication_review_case$;

        ALTER FUNCTION audit.open_document_publication_review_case(
            uuid, uuid, smallint, text
        ) OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION audit.open_document_publication_review_case(
            uuid, uuid, smallint, text
        ) FROM PUBLIC, uap_worker, uap_scheduler, uap_publisher,
               uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        GRANT EXECUTE ON FUNCTION audit.open_document_publication_review_case(
            uuid, uuid, smallint, text
        ) TO uap_api;

        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE OR REPLACE FUNCTION audit.open_document_publication_review_case(
            p_document_version_id uuid,
            p_editorial_revision_id uuid,
            p_priority smallint,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $open_document_publication_review_case$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_case uuid;
            v_revision_no integer;
        BEGIN
            v_actor := audit.require_active_role('editorial_admin'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_reason IS NULL OR char_length(btrim(p_reason)) < 10
               OR p_document_version_id IS NULL OR p_editorial_revision_id IS NULL THEN
                RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
            END IF;
            SELECT revision.revision_no
              INTO v_revision_no
              FROM core.editorial_revisions AS revision
              JOIN core.document_versions AS version
                ON version.id = revision.document_version_id
              JOIN core.documents AS document
                ON document.id = version.document_id
             WHERE revision.id = p_editorial_revision_id
               AND revision.document_version_id = p_document_version_id
               AND revision.content IS NOT NULL
               AND document.deleted_at IS NULL
               AND revision.id = (
                   SELECT current_revision.id
                     FROM core.editorial_revisions AS current_revision
                    WHERE current_revision.document_version_id = p_document_version_id
                      AND current_revision.content IS NOT NULL
                    ORDER BY current_revision.revision_no DESC, current_revision.id DESC
                    LIMIT 1
               );
            IF NOT FOUND THEN
                RAISE EXCEPTION 'editorial_revision_conflict' USING ERRCODE = '23505';
            END IF;
            v_payload := jsonb_build_object(
                'case_type', 'document',
                'op', 'review.case.open.publication',
                'priority', coalesce(p_priority, 0),
                'reason', p_reason,
                'subject_id', lower(p_document_version_id::text),
                'editorial_revision_id', lower(p_editorial_revision_id::text),
                'editorial_revision_no', v_revision_no
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.case.open.publication:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;
            v_case := gen_random_uuid();
            BEGIN
                INSERT INTO audit.review_cases (
                    id, document_version_id, editorial_revision_id,
                    case_type, status, priority, opened_by, opened_at
                ) VALUES (
                    v_case, p_document_version_id, p_editorial_revision_id,
                    'document'::audit.review_case_type, 'open'::audit.review_status,
                    coalesce(p_priority, 0), v_actor, clock_timestamp()
                );
            EXCEPTION
                WHEN unique_violation THEN
                    RAISE EXCEPTION 'review_case_already_open' USING ERRCODE = '23505';
            END;
            PERFORM audit.append_audit_event(
                v_key, 'review.case.open.publication', 'review_case', v_case,
                v_request, jsonb_build_object(
                    'payload_sha256', v_sha,
                    'editorial_revision_id', lower(p_editorial_revision_id::text),
                    'editorial_revision_no', v_revision_no
                )
            );
            RETURN v_case;
        END
        $open_document_publication_review_case$;

        ALTER FUNCTION audit.open_document_publication_review_case(
            uuid, uuid, smallint, text
        ) OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION audit.open_document_publication_review_case(
            uuid, uuid, smallint, text
        ) FROM PUBLIC, uap_worker, uap_scheduler, uap_publisher,
               uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        GRANT EXECUTE ON FUNCTION audit.open_document_publication_review_case(
            uuid, uuid, smallint, text
        ) TO uap_api;

        DROP FUNCTION audit._append_publication_review_submission_audit(
            text, uuid, uuid, uuid, uuid, uuid, integer, text
        );

        RESET ROLE;
        """
    )
