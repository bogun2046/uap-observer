"""Analysis selection and entity candidate promotion. No new tables.

Revision ID: 0017_selection_and_promotion
Revises: 0016_review_decisions_and_grants
Create Date: 2026-08-26
"""

from __future__ import annotations

from alembic import op

revision = "0017_selection_and_promotion"
down_revision = "0016_review_decisions_and_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        REVOKE INSERT, UPDATE, DELETE ON core.analysis_selections
            FROM uap_api, uap_worker;

        CREATE FUNCTION audit.select_analysis_result(
            p_analysis_result_id uuid,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $select_analysis_result$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_document uuid;
            v_type ops.model_task_type;
            v_status core.validation_status;
            v_selection uuid;
        BEGIN
            v_actor := audit.require_active_role('reviewer'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_reason IS NULL OR char_length(btrim(p_reason)) < 10 THEN
                RAISE EXCEPTION 'review_reason_too_short' USING ERRCODE = '22023';
            END IF;
            IF p_analysis_result_id IS NULL THEN
                RAISE EXCEPTION 'review_selection_missing' USING ERRCODE = '23503';
            END IF;

            v_payload := jsonb_build_object(
                'analysis_result_id', lower(p_analysis_result_id::text),
                'op', 'review.selection',
                'reason', p_reason
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.selection:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            SELECT result.document_version_id, result.result_type, result.validation_status
              INTO v_document, v_type, v_status
              FROM core.analysis_results AS result
             WHERE result.id = p_analysis_result_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'review_selection_missing' USING ERRCODE = '23503';
            END IF;

            PERFORM pg_advisory_xact_lock(
                9174,
                hashtext(v_document::text || ':' || v_type::text)
            );

            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            IF v_type NOT IN (
                'claim_extraction'::ops.model_task_type,
                'entity_extraction'::ops.model_task_type
            ) THEN
                RAISE EXCEPTION 'review_selection_type_unsupported'
                    USING ERRCODE = '22023';
            END IF;
            IF v_status IS DISTINCT FROM 'valid'::core.validation_status THEN
                RAISE EXCEPTION 'review_selection_not_valid' USING ERRCODE = '22023';
            END IF;

            UPDATE core.analysis_selections
               SET superseded_at = clock_timestamp()
             WHERE document_version_id = v_document
               AND result_type = v_type
               AND superseded_at IS NULL;

            INSERT INTO core.analysis_selections (
                id, document_version_id, analysis_result_id, result_type,
                selected_by, selection_reason, selected_at
            ) VALUES (
                gen_random_uuid(), v_document, p_analysis_result_id, v_type,
                v_actor, p_reason, clock_timestamp()
            )
            RETURNING id INTO v_selection;

            PERFORM audit.append_audit_event(
                v_key,
                'review.selection',
                'analysis_selection',
                v_selection,
                v_request,
                jsonb_build_object('payload_sha256', v_sha)
            );
            RETURN v_selection;
        END
        $select_analysis_result$;

        CREATE FUNCTION audit.accept_entity_candidate(
            p_candidate_id uuid,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $accept_entity_candidate$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_status core.candidate_status;
            v_name text;
            v_type core.entity_type;
            v_analysis uuid;
            v_origin_type ops.model_task_type;
            v_origin_status core.validation_status;
            v_evidence integer;
            v_entity uuid;
        BEGIN
            v_actor := audit.require_active_role('reviewer'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_reason IS NULL OR char_length(btrim(p_reason)) < 10 THEN
                RAISE EXCEPTION 'review_reason_too_short' USING ERRCODE = '22023';
            END IF;
            IF p_candidate_id IS NULL THEN
                RAISE EXCEPTION 'review_candidate_missing' USING ERRCODE = '23503';
            END IF;

            v_payload := jsonb_build_object(
                'candidate_id', lower(p_candidate_id::text),
                'op', 'review.candidate.accept',
                'reason', p_reason
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.candidate.accept:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            SELECT candidate.status, candidate.proposed_name, candidate.proposed_entity_type,
                   candidate.analysis_result_id
              INTO v_status, v_name, v_type, v_analysis
              FROM core.entity_candidates AS candidate
             WHERE candidate.id = p_candidate_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'review_candidate_missing' USING ERRCODE = '23503';
            END IF;

            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            IF v_status IS DISTINCT FROM 'pending'::core.candidate_status THEN
                RAISE EXCEPTION 'review_candidate_not_pending' USING ERRCODE = '22023';
            END IF;

            SELECT result.result_type, result.validation_status
              INTO v_origin_type, v_origin_status
              FROM core.analysis_results AS result
             WHERE result.id = v_analysis;
            IF v_origin_type IS DISTINCT FROM 'entity_extraction'::ops.model_task_type
               OR v_origin_status IS DISTINCT FROM 'valid'::core.validation_status THEN
                RAISE EXCEPTION 'review_candidate_origin_invalid'
                    USING ERRCODE = '22023';
            END IF;

            SELECT count(*)::integer INTO v_evidence
              FROM core.entity_candidate_evidence AS evidence
             WHERE evidence.entity_candidate_id = p_candidate_id;
            IF v_evidence < 1 THEN
                RAISE EXCEPTION 'review_candidate_evidence_missing'
                    USING ERRCODE = '22023';
            END IF;

            INSERT INTO core.entities (
                id, entity_type, canonical_name, status
            ) VALUES (
                gen_random_uuid(), v_type, v_name, 'active'::core.entity_status
            )
            RETURNING id INTO v_entity;

            UPDATE core.entity_candidates
               SET status = 'resolved'::core.candidate_status,
                   resolved_entity_id = v_entity,
                   resolved_by = v_actor,
                   resolved_at = clock_timestamp()
             WHERE id = p_candidate_id;

            PERFORM audit.append_audit_event(
                v_key,
                'review.candidate.accept',
                'entity',
                v_entity,
                v_request,
                jsonb_build_object('payload_sha256', v_sha)
            );
            RETURN v_entity;
        END
        $accept_entity_candidate$;

        CREATE FUNCTION audit.bind_entity_candidate(
            p_candidate_id uuid,
            p_entity_id uuid,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $bind_entity_candidate$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_status core.candidate_status;
            v_entity_status core.entity_status;
            v_canonical uuid;
        BEGIN
            v_actor := audit.require_active_role('reviewer'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_reason IS NULL OR char_length(btrim(p_reason)) < 10 THEN
                RAISE EXCEPTION 'review_reason_too_short' USING ERRCODE = '22023';
            END IF;
            IF p_candidate_id IS NULL THEN
                RAISE EXCEPTION 'review_candidate_missing' USING ERRCODE = '23503';
            END IF;
            IF p_entity_id IS NULL THEN
                RAISE EXCEPTION 'review_entity_missing' USING ERRCODE = '23503';
            END IF;

            v_payload := jsonb_build_object(
                'candidate_id', lower(p_candidate_id::text),
                'entity_id', lower(p_entity_id::text),
                'op', 'review.candidate.bind',
                'reason', p_reason
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.candidate.bind:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            SELECT candidate.status
              INTO v_status
              FROM core.entity_candidates AS candidate
             WHERE candidate.id = p_candidate_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'review_candidate_missing' USING ERRCODE = '23503';
            END IF;

            SELECT entity.status
              INTO v_entity_status
              FROM core.entities AS entity
             WHERE entity.id = p_entity_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'review_entity_missing' USING ERRCODE = '23503';
            END IF;

            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            IF v_status IS DISTINCT FROM 'pending'::core.candidate_status THEN
                RAISE EXCEPTION 'review_candidate_not_pending' USING ERRCODE = '22023';
            END IF;

            v_canonical := core.canonical_entity_id(p_entity_id);
            IF v_canonical IS DISTINCT FROM p_entity_id THEN
                RAISE EXCEPTION 'review_bind_target_not_canonical'
                    USING ERRCODE = '22023';
            END IF;
            IF v_entity_status IS DISTINCT FROM 'active'::core.entity_status THEN
                RAISE EXCEPTION 'review_bind_target_not_active'
                    USING ERRCODE = '22023';
            END IF;

            UPDATE core.entity_candidates
               SET status = 'resolved'::core.candidate_status,
                   resolved_entity_id = p_entity_id,
                   resolved_by = v_actor,
                   resolved_at = clock_timestamp()
             WHERE id = p_candidate_id;

            PERFORM audit.append_audit_event(
                v_key,
                'review.candidate.bind',
                'entity',
                p_entity_id,
                v_request,
                jsonb_build_object('payload_sha256', v_sha)
            );
            RETURN p_entity_id;
        END
        $bind_entity_candidate$;

        REVOKE ALL ON FUNCTION audit.select_analysis_result(uuid, text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit.accept_entity_candidate(uuid, text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit.bind_entity_candidate(uuid, uuid, text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION audit.select_analysis_result(uuid, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.accept_entity_candidate(uuid, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.bind_entity_candidate(uuid, uuid, text) TO uap_api;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        DROP FUNCTION IF EXISTS audit.bind_entity_candidate(uuid, uuid, text);
        DROP FUNCTION IF EXISTS audit.accept_entity_candidate(uuid, text);
        DROP FUNCTION IF EXISTS audit.select_analysis_result(uuid, text);

        GRANT INSERT, UPDATE ON core.analysis_selections TO uap_api, uap_worker;
        """
    )
