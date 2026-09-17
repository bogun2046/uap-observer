"""Authorized entity merge/reverse wrappers. No new tables.

Revision ID: 0018_authorized_entity_merge
Revises: 0017_selection_and_promotion
Create Date: 2026-08-26
"""

from __future__ import annotations

from alembic import op

revision = "0018_authorized_entity_merge"
down_revision = "0017_selection_and_promotion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION audit._require_openable_entity_subject(p_entity_id uuid)
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $_require_openable_entity_subject$
        DECLARE
            v_status core.entity_status;
            v_canonical uuid;
        BEGIN
            IF p_entity_id IS NULL THEN
                RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
            END IF;
            SELECT entity.status INTO v_status
              FROM core.entities AS entity
             WHERE entity.id = p_entity_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
            END IF;
            IF v_status IS DISTINCT FROM 'active'::core.entity_status THEN
                RAISE EXCEPTION 'review_subject_not_active' USING ERRCODE = '22023';
            END IF;
            v_canonical := core.canonical_entity_id(p_entity_id);
            IF v_canonical IS DISTINCT FROM p_entity_id THEN
                RAISE EXCEPTION 'review_subject_not_canonical' USING ERRCODE = '22023';
            END IF;
        END
        $_require_openable_entity_subject$;

        CREATE OR REPLACE FUNCTION audit.open_review_case(
            p_case_type audit.review_case_type,
            p_subject_id uuid,
            p_priority smallint,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $open_review_case$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_priority smallint;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_case uuid;
        BEGIN
            v_actor := audit.require_active_role('reviewer'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_reason IS NULL OR char_length(btrim(p_reason)) < 10 THEN
                RAISE EXCEPTION 'review_reason_too_short' USING ERRCODE = '22023';
            END IF;
            IF p_subject_id IS NULL THEN
                RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
            END IF;
            v_priority := coalesce(p_priority, 0);
            v_payload := jsonb_build_object(
                'case_type', p_case_type::text,
                'op', 'review.case.open',
                'priority', v_priority,
                'reason', p_reason,
                'subject_id', lower(p_subject_id::text)
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.case.open:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;
            IF p_case_type = 'relation'::audit.review_case_type THEN
                RAISE EXCEPTION 'knowledge_relation_review_not_in_wp9' USING ERRCODE = '22023';
            END IF;

            IF p_case_type = 'document'::audit.review_case_type THEN
                IF NOT EXISTS (
                    SELECT 1 FROM core.document_versions WHERE id = p_subject_id
                ) THEN
                    RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
                END IF;
            ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                IF NOT EXISTS (
                    SELECT 1 FROM core.claims WHERE id = p_subject_id
                ) THEN
                    RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
                END IF;
            ELSIF p_case_type = 'entity'::audit.review_case_type THEN
                PERFORM audit._require_openable_entity_subject(p_subject_id);
            END IF;

            v_case := gen_random_uuid();
            BEGIN
                INSERT INTO audit.review_cases (
                    id, document_version_id, claim_id, entity_id, relation_id,
                    case_type, status, priority, opened_by, opened_at
                ) VALUES (
                    v_case,
                    CASE WHEN p_case_type = 'document'::audit.review_case_type THEN p_subject_id END,
                    CASE WHEN p_case_type = 'claim'::audit.review_case_type THEN p_subject_id END,
                    CASE WHEN p_case_type = 'entity'::audit.review_case_type THEN p_subject_id END,
                    NULL,
                    p_case_type,
                    'open'::audit.review_status,
                    v_priority,
                    v_actor,
                    clock_timestamp()
                );
            EXCEPTION
                WHEN unique_violation THEN
                    RAISE EXCEPTION 'review_case_already_open' USING ERRCODE = '23505';
            END;

            PERFORM audit.append_audit_event(
                v_key,
                'review.case.open',
                'review_case',
                v_case,
                v_request,
                jsonb_build_object('payload_sha256', v_sha)
            );
            RETURN v_case;
        END
        $open_review_case$;

        CREATE FUNCTION audit.apply_entity_merge(
            p_source_entity_id uuid,
            p_target_entity_id uuid,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $apply_entity_merge$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_event uuid;
        BEGIN
            v_actor := audit.require_active_role('senior_reviewer'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_reason IS NULL OR char_length(btrim(p_reason)) < 10 THEN
                RAISE EXCEPTION 'review_reason_too_short' USING ERRCODE = '22023';
            END IF;
            IF p_source_entity_id IS NULL OR p_target_entity_id IS NULL THEN
                RAISE EXCEPTION 'knowledge_merge_missing_entity' USING ERRCODE = '23503';
            END IF;

            v_payload := jsonb_build_object(
                'op', 'review.entity.merge',
                'reason', p_reason,
                'source_entity_id', lower(p_source_entity_id::text),
                'target_entity_id', lower(p_target_entity_id::text)
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.entity.merge:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            PERFORM pg_advisory_xact_lock(9175, hashtext(v_key));
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            v_event := core.merge_entities(
                p_source_entity_id,
                p_target_entity_id,
                v_actor,
                p_reason
            );

            PERFORM audit.append_audit_event(
                v_key,
                'review.entity.merge',
                'entity_merge_event',
                v_event,
                v_request,
                jsonb_build_object('payload_sha256', v_sha)
            );
            RETURN v_event;
        END
        $apply_entity_merge$;

        CREATE FUNCTION audit.apply_entity_merge_reverse(
            p_merge_event_id uuid,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $apply_entity_merge_reverse$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_event uuid;
        BEGIN
            v_actor := audit.require_active_role('senior_reviewer'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_reason IS NULL OR char_length(btrim(p_reason)) < 10 THEN
                RAISE EXCEPTION 'review_reason_too_short' USING ERRCODE = '22023';
            END IF;
            IF p_merge_event_id IS NULL THEN
                RAISE EXCEPTION 'knowledge_merge_missing_event' USING ERRCODE = '23503';
            END IF;

            v_payload := jsonb_build_object(
                'merge_event_id', lower(p_merge_event_id::text),
                'op', 'review.entity.merge_reverse',
                'reason', p_reason
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.entity.merge_reverse:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            PERFORM pg_advisory_xact_lock(9175, hashtext(v_key));
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            v_event := core.reverse_entity_merge(
                p_merge_event_id,
                v_actor,
                p_reason
            );

            PERFORM audit.append_audit_event(
                v_key,
                'review.entity.merge_reverse',
                'entity_merge_event',
                v_event,
                v_request,
                jsonb_build_object('payload_sha256', v_sha)
            );
            RETURN v_event;
        END
        $apply_entity_merge_reverse$;

        REVOKE ALL ON FUNCTION audit._require_openable_entity_subject(uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit.apply_entity_merge(uuid, uuid, text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit.apply_entity_merge_reverse(uuid, text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION audit.apply_entity_merge(uuid, uuid, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.apply_entity_merge_reverse(uuid, text) TO uap_api;
        REVOKE ALL ON FUNCTION core.merge_entities(uuid, uuid, uuid, text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION core.reverse_entity_merge(uuid, uuid, text) FROM PUBLIC;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        DROP FUNCTION IF EXISTS audit.apply_entity_merge_reverse(uuid, text);
        DROP FUNCTION IF EXISTS audit.apply_entity_merge(uuid, uuid, text);

        CREATE OR REPLACE FUNCTION audit.open_review_case(
            p_case_type audit.review_case_type,
            p_subject_id uuid,
            p_priority smallint,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $open_review_case$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_priority smallint;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_case uuid;
        BEGIN
            v_actor := audit.require_active_role('reviewer'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_reason IS NULL OR char_length(btrim(p_reason)) < 10 THEN
                RAISE EXCEPTION 'review_reason_too_short' USING ERRCODE = '22023';
            END IF;
            IF p_subject_id IS NULL THEN
                RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
            END IF;
            v_priority := coalesce(p_priority, 0);
            v_payload := jsonb_build_object(
                'case_type', p_case_type::text,
                'op', 'review.case.open',
                'priority', v_priority,
                'reason', p_reason,
                'subject_id', lower(p_subject_id::text)
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.case.open:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;
            IF p_case_type = 'relation'::audit.review_case_type THEN
                RAISE EXCEPTION 'knowledge_relation_review_not_in_wp9' USING ERRCODE = '22023';
            END IF;

            IF p_case_type = 'document'::audit.review_case_type THEN
                IF NOT EXISTS (
                    SELECT 1 FROM core.document_versions WHERE id = p_subject_id
                ) THEN
                    RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
                END IF;
            ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                IF NOT EXISTS (
                    SELECT 1 FROM core.claims WHERE id = p_subject_id
                ) THEN
                    RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
                END IF;
            ELSIF p_case_type = 'entity'::audit.review_case_type THEN
                IF NOT EXISTS (
                    SELECT 1 FROM core.entities WHERE id = p_subject_id
                ) THEN
                    RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
                END IF;
            END IF;

            v_case := gen_random_uuid();
            BEGIN
                INSERT INTO audit.review_cases (
                    id, document_version_id, claim_id, entity_id, relation_id,
                    case_type, status, priority, opened_by, opened_at
                ) VALUES (
                    v_case,
                    CASE WHEN p_case_type = 'document'::audit.review_case_type THEN p_subject_id END,
                    CASE WHEN p_case_type = 'claim'::audit.review_case_type THEN p_subject_id END,
                    CASE WHEN p_case_type = 'entity'::audit.review_case_type THEN p_subject_id END,
                    NULL,
                    p_case_type,
                    'open'::audit.review_status,
                    v_priority,
                    v_actor,
                    clock_timestamp()
                );
            EXCEPTION
                WHEN unique_violation THEN
                    RAISE EXCEPTION 'review_case_already_open' USING ERRCODE = '23505';
            END;

            PERFORM audit.append_audit_event(
                v_key,
                'review.case.open',
                'review_case',
                v_case,
                v_request,
                jsonb_build_object('payload_sha256', v_sha)
            );
            RETURN v_case;
        END
        $open_review_case$;

        DROP FUNCTION IF EXISTS audit._require_openable_entity_subject(uuid);
        """
    )
