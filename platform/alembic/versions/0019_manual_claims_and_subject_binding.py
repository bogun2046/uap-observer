"""Manual claims, evidence constraint, and subject binding. No new tables.

Revision ID: 0019_manual_claims_binding
Revises: 0018_authorized_entity_merge
Create Date: 2026-08-27
"""

from __future__ import annotations

from alembic import op

revision = "0019_manual_claims_binding"
down_revision = "0018_authorized_entity_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION core.require_manual_claim_supports() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = core, audit, pg_catalog
        AS $require_manual_claim_supports$
        DECLARE
            targets uuid[];
            target uuid;
        BEGIN
            IF TG_TABLE_NAME = 'claims' THEN
                IF NEW.origin_analysis_result_id IS NOT NULL THEN
                    RETURN NULL;
                END IF;
                targets := ARRAY[NEW.id];
            ELSIF TG_OP = 'INSERT' THEN
                targets := ARRAY[NEW.claim_id];
            ELSIF TG_OP = 'DELETE' THEN
                targets := ARRAY[OLD.claim_id];
            ELSIF NEW.claim_id IS DISTINCT FROM OLD.claim_id THEN
                targets := ARRAY[NEW.claim_id, OLD.claim_id];
            ELSE
                targets := ARRAY[NEW.claim_id];
            END IF;
            FOREACH target IN ARRAY targets LOOP
                IF EXISTS (
                    SELECT 1
                      FROM core.claims AS claim
                     WHERE claim.id = target
                       AND claim.origin_analysis_result_id IS NULL
                ) AND NOT EXISTS (
                    SELECT 1
                      FROM core.claim_evidence
                     WHERE claim_id = target
                       AND support_type = 'supports'::core.support_type
                ) THEN
                    IF EXISTS (
                        SELECT 1
                          FROM audit.review_cases AS review_case
                          JOIN audit.review_decisions AS decision
                            ON decision.review_case_id = review_case.id
                         WHERE review_case.claim_id = target
                           AND review_case.status IN (
                                'rejected'::audit.review_status,
                                'withdrawn'::audit.review_status
                           )
                           AND decision.decision IN (
                                'reject'::audit.review_decision,
                                'withdraw'::audit.review_decision
                           )
                           AND decision.xmin::text::xid8 = pg_current_xact_id()
                    ) THEN
                        CONTINUE;
                    END IF;
                    RAISE EXCEPTION 'manual_claim_requires_supports'
                        USING ERRCODE = '23514';
                END IF;
            END LOOP;
            RETURN NULL;
        END
        $require_manual_claim_supports$;
        CREATE CONSTRAINT TRIGGER claims_require_manual_supports
            AFTER INSERT OR UPDATE ON core.claims
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION core.require_manual_claim_supports();
        CREATE CONSTRAINT TRIGGER claim_evidence_require_manual_supports
            AFTER INSERT OR UPDATE OR DELETE ON core.claim_evidence
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION core.require_manual_claim_supports();
        REVOKE ALL ON FUNCTION core.require_manual_claim_supports() FROM PUBLIC;

        CREATE FUNCTION audit.create_manual_claim(
            p_document_version_id uuid,
            p_claim_text text,
            p_claim_type core.claim_type,
            p_assertion_status core.assertion_status,
            p_attribution text,
            p_span_ids uuid[]
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $create_manual_claim$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_fingerprint text;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_claim uuid;
            v_span uuid;
            v_spans jsonb;
        BEGIN
            v_actor := audit.require_active_role('reviewer'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_document_version_id IS NULL THEN
                RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
            END IF;
            IF p_claim_text IS NULL OR char_length(btrim(p_claim_text)) = 0 THEN
                RAISE EXCEPTION 'review_structured_changes_unsupported'
                    USING ERRCODE = '22023';
            END IF;
            IF p_span_ids IS NULL OR cardinality(p_span_ids) = 0 THEN
                RAISE EXCEPTION 'manual_claim_requires_supports' USING ERRCODE = '23514';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM core.document_versions WHERE id = p_document_version_id
            ) THEN
                RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
            END IF;
            v_fingerprint := core.compute_claim_fingerprint(p_claim_text);
            SELECT coalesce(jsonb_agg(to_jsonb(lower(span_id::text))), '[]'::jsonb)
              INTO v_spans
              FROM unnest(p_span_ids) AS span_id;
            v_payload := jsonb_build_object(
                'assertion_status', p_assertion_status::text,
                'attribution', to_jsonb(p_attribution),
                'claim_text', p_claim_text,
                'claim_type', p_claim_type::text,
                'document_version_id', lower(p_document_version_id::text),
                'fingerprint', v_fingerprint,
                'op', 'review.claim.manual',
                'span_ids', v_spans
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.claim.manual:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;
            PERFORM pg_advisory_xact_lock(9175, hashtext(v_key));
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            FOREACH v_span IN ARRAY p_span_ids LOOP
                IF NOT EXISTS (
                    SELECT 1
                      FROM core.evidence_spans AS span
                     WHERE span.id = v_span
                       AND span.document_version_id = p_document_version_id
                ) THEN
                    RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
                END IF;
            END LOOP;

            v_claim := gen_random_uuid();
            INSERT INTO core.claims (
                id, origin_analysis_result_id, subject_entity_id, ordinal,
                claim_text, claim_fingerprint, claim_type, assertion_status,
                attribution, created_by, document_version_id
            ) VALUES (
                v_claim, NULL, NULL, NULL,
                p_claim_text, v_fingerprint, p_claim_type, p_assertion_status,
                p_attribution, v_actor, p_document_version_id
            );
            FOREACH v_span IN ARRAY p_span_ids LOOP
                INSERT INTO core.claim_evidence (
                    id, claim_id, evidence_span_id, document_version_id, support_type
                ) VALUES (
                    gen_random_uuid(), v_claim, v_span, p_document_version_id,
                    'supports'::core.support_type
                );
            END LOOP;

            BEGIN
                PERFORM audit.append_audit_event(
                    v_key,
                    'review.claim.manual',
                    'claim',
                    v_claim,
                    v_request,
                    jsonb_build_object('payload_sha256', v_sha)
                );
            EXCEPTION
                WHEN unique_violation THEN
                    RAISE EXCEPTION 'review_idempotency_payload_conflict'
                        USING ERRCODE = '23505';
            END;
            RETURN v_claim;
        END
        $create_manual_claim$;

        CREATE FUNCTION audit._require_current_claim_decision(
            p_case_id uuid,
            p_decision_id uuid
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $_require_current_claim_decision$
        DECLARE
            v_case_type audit.review_case_type;
            v_claim uuid;
            v_xmin xid8;
        BEGIN
            SELECT review_case.case_type, review_case.claim_id
              INTO v_case_type, v_claim
              FROM audit.review_cases AS review_case
             WHERE review_case.id = p_case_id
             FOR UPDATE;
            IF NOT FOUND OR v_case_type IS DISTINCT FROM 'claim'::audit.review_case_type
               OR v_claim IS NULL THEN
                RAISE EXCEPTION 'review_case_missing' USING ERRCODE = '23503';
            END IF;
            SELECT decision.xmin::text::xid8 INTO v_xmin
              FROM audit.review_decisions AS decision
             WHERE decision.id = p_decision_id
               AND decision.review_case_id = p_case_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'review_decision_not_allowed' USING ERRCODE = '22023';
            END IF;
            IF v_xmin IS DISTINCT FROM pg_current_xact_id() THEN
                RAISE EXCEPTION 'review_decision_not_in_transaction'
                    USING ERRCODE = '22023';
            END IF;
            RETURN v_claim;
        END
        $_require_current_claim_decision$;

        CREATE FUNCTION audit._apply_claim_subject_bind(
            p_case_id uuid,
            p_decision_id uuid,
            p_entity_id uuid
        ) RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $_apply_claim_subject_bind$
        DECLARE
            v_claim uuid;
            v_decision audit.review_decision;
            v_status core.entity_status;
            v_canonical uuid;
            v_current uuid;
        BEGIN
            v_claim := audit._require_current_claim_decision(p_case_id, p_decision_id);
            SELECT decision.decision INTO v_decision
              FROM audit.review_decisions AS decision
             WHERE decision.id = p_decision_id;
            IF v_decision NOT IN (
                'approve'::audit.review_decision,
                'revise'::audit.review_decision
            ) THEN
                RAISE EXCEPTION 'review_structured_changes_unsupported'
                    USING ERRCODE = '22023';
            END IF;
            IF p_entity_id IS NULL THEN
                RAISE EXCEPTION 'review_entity_missing' USING ERRCODE = '23503';
            END IF;
            SELECT entity.status INTO v_status
              FROM core.entities AS entity
             WHERE entity.id = p_entity_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'review_entity_missing' USING ERRCODE = '23503';
            END IF;
            v_canonical := core.canonical_entity_id(p_entity_id);
            IF v_canonical IS DISTINCT FROM p_entity_id THEN
                RAISE EXCEPTION 'review_subject_not_canonical' USING ERRCODE = '22023';
            END IF;
            IF v_status IS DISTINCT FROM 'active'::core.entity_status THEN
                RAISE EXCEPTION 'review_subject_not_active' USING ERRCODE = '22023';
            END IF;
            SELECT claim.subject_entity_id INTO v_current
              FROM core.claims AS claim
             WHERE claim.id = v_claim
             FOR UPDATE;
            IF v_decision = 'approve'::audit.review_decision
               AND v_current IS NOT NULL THEN
                RAISE EXCEPTION 'review_subject_already_bound' USING ERRCODE = '22023';
            END IF;
            UPDATE core.claims
               SET subject_entity_id = p_entity_id
             WHERE id = v_claim;
        END
        $_apply_claim_subject_bind$;

        CREATE FUNCTION audit._replace_claim_evidence(
            p_case_id uuid,
            p_decision_id uuid,
            p_span_ids uuid[]
        ) RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $_replace_claim_evidence$
        DECLARE
            v_claim uuid;
            v_decision audit.review_decision;
            v_document uuid;
            v_span uuid;
        BEGIN
            v_claim := audit._require_current_claim_decision(p_case_id, p_decision_id);
            SELECT decision.decision INTO v_decision
              FROM audit.review_decisions AS decision
             WHERE decision.id = p_decision_id;
            IF v_decision IS DISTINCT FROM 'revise'::audit.review_decision THEN
                RAISE EXCEPTION 'review_structured_changes_unsupported'
                    USING ERRCODE = '22023';
            END IF;
            IF p_span_ids IS NULL OR cardinality(p_span_ids) = 0 THEN
                RAISE EXCEPTION 'review_structured_changes_unsupported'
                    USING ERRCODE = '22023';
            END IF;
            SELECT claim.document_version_id INTO v_document
              FROM core.claims AS claim
             WHERE claim.id = v_claim
             FOR UPDATE;
            DELETE FROM core.claim_evidence WHERE claim_id = v_claim;
            FOREACH v_span IN ARRAY p_span_ids LOOP
                IF NOT EXISTS (
                    SELECT 1
                      FROM core.evidence_spans AS span
                     WHERE span.id = v_span
                       AND span.document_version_id = v_document
                ) THEN
                    RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
                END IF;
                INSERT INTO core.claim_evidence (
                    id, claim_id, evidence_span_id, document_version_id, support_type
                ) VALUES (
                    gen_random_uuid(), v_claim, v_span, v_document,
                    'supports'::core.support_type
                );
            END LOOP;
        END
        $_replace_claim_evidence$;

        CREATE FUNCTION audit._retire_manual_claim_supports(
            p_case_id uuid,
            p_decision_id uuid
        ) RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $_retire_manual_claim_supports$
        DECLARE
            v_claim uuid;
            v_decision audit.review_decision;
            v_origin uuid;
        BEGIN
            v_claim := audit._require_current_claim_decision(p_case_id, p_decision_id);
            SELECT decision.decision INTO v_decision
              FROM audit.review_decisions AS decision
             WHERE decision.id = p_decision_id;
            IF v_decision NOT IN (
                'reject'::audit.review_decision,
                'withdraw'::audit.review_decision
            ) THEN
                RAISE EXCEPTION 'review_structured_changes_unsupported'
                    USING ERRCODE = '22023';
            END IF;
            SELECT claim.origin_analysis_result_id INTO v_origin
              FROM core.claims AS claim
             WHERE claim.id = v_claim
             FOR UPDATE;
            IF v_origin IS NOT NULL THEN
                RAISE EXCEPTION 'review_ai_evidence_immutable' USING ERRCODE = '22023';
            END IF;
            DELETE FROM core.claim_evidence
             WHERE claim_id = v_claim
               AND support_type = 'supports'::core.support_type;
        END
        $_retire_manual_claim_supports$;

        CREATE OR REPLACE FUNCTION audit.record_review_decision(
            p_case_id uuid,
            p_decision audit.review_decision,
            p_reason text,
            p_structured_changes jsonb
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, ops, pg_catalog
        AS $record_review_decision$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_status audit.review_status;
            v_closed timestamptz;
            v_assigned uuid;
            v_type audit.review_case_type;
            v_subject uuid;
            v_created_by uuid;
            v_seq integer;
            v_decision uuid;
            v_prev uuid;
            v_new_status audit.review_status;
            v_change_key text;
            v_bind uuid;
            v_spans uuid[];
        BEGIN
            IF p_decision = 'withdraw'::audit.review_decision THEN
                v_actor := audit.require_active_role(
                    'senior_reviewer'::audit.application_role
                );
            ELSE
                v_actor := audit.require_active_role(
                    'reviewer'::audit.application_role
                );
            END IF;
            v_request := audit._review_request_id();
            IF p_reason IS NULL OR char_length(btrim(p_reason)) < 10 THEN
                RAISE EXCEPTION 'review_reason_too_short' USING ERRCODE = '22023';
            END IF;
            IF p_case_id IS NULL OR p_decision IS NULL THEN
                RAISE EXCEPTION 'review_case_missing' USING ERRCODE = '23503';
            END IF;

            v_payload := jsonb_build_object(
                'case_id', lower(p_case_id::text),
                'decision', p_decision::text,
                'op', 'review.decision',
                'reason', p_reason,
                'structured_changes', coalesce(p_structured_changes, 'null'::jsonb)
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.decision:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            IF p_structured_changes IS NULL
               OR jsonb_typeof(p_structured_changes) IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'review_structured_changes_unsupported'
                    USING ERRCODE = '22023';
            END IF;

            SELECT review_case.status, review_case.closed_at, review_case.assigned_to,
                   review_case.case_type,
                   coalesce(
                       review_case.document_version_id,
                       review_case.claim_id,
                       review_case.entity_id,
                       review_case.relation_id
                   )
              INTO v_status, v_closed, v_assigned, v_type, v_subject
              FROM audit.review_cases AS review_case
             WHERE review_case.id = p_case_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'review_case_missing' USING ERRCODE = '23503';
            END IF;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;
            IF v_closed IS NOT NULL THEN
                RAISE EXCEPTION 'review_case_already_closed' USING ERRCODE = '22023';
            END IF;
            IF v_type = 'relation'::audit.review_case_type THEN
                RAISE EXCEPTION 'knowledge_relation_review_not_in_wp9'
                    USING ERRCODE = '22023';
            END IF;
            IF v_type = 'claim'::audit.review_case_type THEN
                SELECT claim.created_by INTO v_created_by
                  FROM core.claims AS claim
                 WHERE claim.id = v_subject;
                IF v_created_by IS NOT NULL AND v_created_by = v_actor THEN
                    RAISE EXCEPTION 'review_self_review_denied' USING ERRCODE = '42501';
                END IF;
            END IF;
            IF p_decision <> 'withdraw'::audit.review_decision
               AND v_assigned IS NOT NULL
               AND v_assigned IS DISTINCT FROM v_actor THEN
                RAISE EXCEPTION 'review_assignee_mismatch' USING ERRCODE = '42501';
            END IF;

            IF p_decision IN (
                'approve'::audit.review_decision,
                'reject'::audit.review_decision,
                'dispute'::audit.review_decision
            ) THEN
                IF v_status NOT IN (
                    'open'::audit.review_status,
                    'assigned'::audit.review_status
                ) THEN
                    RAISE EXCEPTION 'review_decision_not_allowed'
                        USING ERRCODE = '22023';
                END IF;
            ELSIF p_decision IN (
                'revise'::audit.review_decision,
                'withdraw'::audit.review_decision
            ) THEN
                IF v_status IS DISTINCT FROM 'approved'::audit.review_status THEN
                    RAISE EXCEPTION 'review_decision_not_allowed'
                        USING ERRCODE = '22023';
                END IF;
            ELSE
                RAISE EXCEPTION 'review_decision_not_allowed' USING ERRCODE = '22023';
            END IF;

            FOR v_change_key IN
                SELECT jsonb_object_keys(p_structured_changes)
            LOOP
                IF v_type IS DISTINCT FROM 'claim'::audit.review_case_type THEN
                    RAISE EXCEPTION 'review_structured_changes_unsupported'
                        USING ERRCODE = '22023';
                END IF;
                IF v_change_key = 'bind_subject_entity_id' THEN
                    IF p_decision NOT IN (
                        'approve'::audit.review_decision,
                        'revise'::audit.review_decision
                    ) THEN
                        RAISE EXCEPTION 'review_structured_changes_unsupported'
                            USING ERRCODE = '22023';
                    END IF;
                ELSIF v_change_key = 'replace_supporting_span_ids' THEN
                    IF p_decision IS DISTINCT FROM 'revise'::audit.review_decision
                       OR jsonb_typeof(
                            p_structured_changes -> 'replace_supporting_span_ids'
                       ) IS DISTINCT FROM 'array'
                       OR jsonb_array_length(
                            p_structured_changes -> 'replace_supporting_span_ids'
                       ) < 1 THEN
                        RAISE EXCEPTION 'review_structured_changes_unsupported'
                            USING ERRCODE = '22023';
                    END IF;
                ELSIF v_change_key = 'retire_supporting_evidence' THEN
                    IF p_decision NOT IN (
                        'reject'::audit.review_decision,
                        'withdraw'::audit.review_decision
                    ) OR (p_structured_changes -> 'retire_supporting_evidence')
                       IS DISTINCT FROM 'true'::jsonb THEN
                        RAISE EXCEPTION 'review_structured_changes_unsupported'
                            USING ERRCODE = '22023';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'review_structured_changes_unsupported'
                        USING ERRCODE = '22023';
                END IF;
            END LOOP;

            SELECT coalesce(max(decision.sequence_no), 0) + 1
              INTO v_seq
              FROM audit.review_decisions AS decision
             WHERE decision.review_case_id = p_case_id;

            IF p_decision = 'revise'::audit.review_decision THEN
                SELECT decision.id INTO v_prev
                  FROM audit.review_decisions AS decision
                 WHERE decision.review_case_id = p_case_id
                   AND decision.decision IN (
                        'approve'::audit.review_decision,
                        'revise'::audit.review_decision
                   )
                 ORDER BY decision.sequence_no DESC
                 LIMIT 1;
            END IF;

            v_decision := gen_random_uuid();
            INSERT INTO audit.review_decisions (
                id, review_case_id, sequence_no, decision, reason,
                structured_changes, decided_by, supersedes_decision_id, decided_at
            ) VALUES (
                v_decision, p_case_id, v_seq, p_decision, p_reason,
                p_structured_changes, v_actor, v_prev, clock_timestamp()
            );

            PERFORM audit._apply_publication_grant(
                v_type, v_subject, p_case_id, v_decision, p_decision
            );

            v_new_status := CASE p_decision
                WHEN 'approve'::audit.review_decision THEN 'approved'::audit.review_status
                WHEN 'revise'::audit.review_decision THEN 'approved'::audit.review_status
                WHEN 'reject'::audit.review_decision THEN 'rejected'::audit.review_status
                WHEN 'dispute'::audit.review_decision THEN 'disputed'::audit.review_status
                WHEN 'withdraw'::audit.review_decision THEN 'withdrawn'::audit.review_status
            END;
            UPDATE audit.review_cases
               SET status = v_new_status
             WHERE id = p_case_id;

            IF p_structured_changes ? 'bind_subject_entity_id' THEN
                v_bind := (p_structured_changes ->> 'bind_subject_entity_id')::uuid;
                PERFORM audit._apply_claim_subject_bind(p_case_id, v_decision, v_bind);
            END IF;
            IF p_structured_changes ? 'replace_supporting_span_ids' THEN
                SELECT array_agg(span_id::uuid)
                  INTO v_spans
                  FROM jsonb_array_elements_text(
                      p_structured_changes -> 'replace_supporting_span_ids'
                  ) AS span_id;
                PERFORM audit._replace_claim_evidence(p_case_id, v_decision, v_spans);
            END IF;
            IF p_structured_changes ? 'retire_supporting_evidence' THEN
                PERFORM audit._retire_manual_claim_supports(p_case_id, v_decision);
            END IF;

            BEGIN
                PERFORM audit.append_audit_event(
                    v_key,
                    'review.decision',
                    'review_decision',
                    v_decision,
                    v_request,
                    jsonb_build_object('payload_sha256', v_sha)
                );
            EXCEPTION
                WHEN unique_violation THEN
                    RAISE EXCEPTION 'review_idempotency_payload_conflict'
                        USING ERRCODE = '23505';
            END;
            RETURN v_decision;
        END
        $record_review_decision$;

        REVOKE ALL ON FUNCTION core.require_manual_claim_supports() FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit.create_manual_claim(
            uuid, text, core.claim_type, core.assertion_status, text, uuid[]
        ) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._require_current_claim_decision(uuid, uuid)
            FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._apply_claim_subject_bind(uuid, uuid, uuid)
            FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._replace_claim_evidence(uuid, uuid, uuid[])
            FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._retire_manual_claim_supports(uuid, uuid)
            FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION audit.create_manual_claim(
            uuid, text, core.claim_type, core.assertion_status, text, uuid[]
        ) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.record_review_decision(
            uuid, audit.review_decision, text, jsonb
        ) TO uap_api;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE OR REPLACE FUNCTION audit.record_review_decision(
            p_case_id uuid,
            p_decision audit.review_decision,
            p_reason text,
            p_structured_changes jsonb
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, ops, pg_catalog
        AS $record_review_decision$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_status audit.review_status;
            v_closed timestamptz;
            v_assigned uuid;
            v_type audit.review_case_type;
            v_subject uuid;
            v_created_by uuid;
            v_seq integer;
            v_decision uuid;
            v_prev uuid;
            v_new_status audit.review_status;
        BEGIN
            IF p_decision = 'withdraw'::audit.review_decision THEN
                v_actor := audit.require_active_role(
                    'senior_reviewer'::audit.application_role
                );
            ELSE
                v_actor := audit.require_active_role(
                    'reviewer'::audit.application_role
                );
            END IF;
            v_request := audit._review_request_id();
            IF p_reason IS NULL OR char_length(btrim(p_reason)) < 10 THEN
                RAISE EXCEPTION 'review_reason_too_short' USING ERRCODE = '22023';
            END IF;
            IF p_case_id IS NULL OR p_decision IS NULL THEN
                RAISE EXCEPTION 'review_case_missing' USING ERRCODE = '23503';
            END IF;

            v_payload := jsonb_build_object(
                'case_id', lower(p_case_id::text),
                'decision', p_decision::text,
                'op', 'review.decision',
                'reason', p_reason,
                'structured_changes', coalesce(p_structured_changes, 'null'::jsonb)
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.decision:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            IF p_structured_changes IS NULL
               OR jsonb_typeof(p_structured_changes) IS DISTINCT FROM 'object'
               OR p_structured_changes <> '{}'::jsonb THEN
                RAISE EXCEPTION 'review_structured_changes_unsupported'
                    USING ERRCODE = '22023';
            END IF;

            SELECT review_case.status, review_case.closed_at, review_case.assigned_to,
                   review_case.case_type,
                   coalesce(
                       review_case.document_version_id,
                       review_case.claim_id,
                       review_case.entity_id,
                       review_case.relation_id
                   )
              INTO v_status, v_closed, v_assigned, v_type, v_subject
              FROM audit.review_cases AS review_case
             WHERE review_case.id = p_case_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'review_case_missing' USING ERRCODE = '23503';
            END IF;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;
            IF v_closed IS NOT NULL THEN
                RAISE EXCEPTION 'review_case_already_closed' USING ERRCODE = '22023';
            END IF;
            IF v_type = 'relation'::audit.review_case_type THEN
                RAISE EXCEPTION 'knowledge_relation_review_not_in_wp9'
                    USING ERRCODE = '22023';
            END IF;
            IF v_type = 'claim'::audit.review_case_type THEN
                SELECT claim.created_by INTO v_created_by
                  FROM core.claims AS claim
                 WHERE claim.id = v_subject;
                IF v_created_by IS NOT NULL AND v_created_by = v_actor THEN
                    RAISE EXCEPTION 'review_self_review_denied' USING ERRCODE = '42501';
                END IF;
            END IF;
            IF p_decision <> 'withdraw'::audit.review_decision
               AND v_assigned IS NOT NULL
               AND v_assigned IS DISTINCT FROM v_actor THEN
                RAISE EXCEPTION 'review_assignee_mismatch' USING ERRCODE = '42501';
            END IF;

            IF p_decision IN (
                'approve'::audit.review_decision,
                'reject'::audit.review_decision,
                'dispute'::audit.review_decision
            ) THEN
                IF v_status NOT IN (
                    'open'::audit.review_status,
                    'assigned'::audit.review_status
                ) THEN
                    RAISE EXCEPTION 'review_decision_not_allowed'
                        USING ERRCODE = '22023';
                END IF;
            ELSIF p_decision IN (
                'revise'::audit.review_decision,
                'withdraw'::audit.review_decision
            ) THEN
                IF v_status IS DISTINCT FROM 'approved'::audit.review_status THEN
                    RAISE EXCEPTION 'review_decision_not_allowed'
                        USING ERRCODE = '22023';
                END IF;
            ELSE
                RAISE EXCEPTION 'review_decision_not_allowed' USING ERRCODE = '22023';
            END IF;

            SELECT coalesce(max(decision.sequence_no), 0) + 1
              INTO v_seq
              FROM audit.review_decisions AS decision
             WHERE decision.review_case_id = p_case_id;

            IF p_decision = 'revise'::audit.review_decision THEN
                SELECT decision.id INTO v_prev
                  FROM audit.review_decisions AS decision
                 WHERE decision.review_case_id = p_case_id
                   AND decision.decision IN (
                        'approve'::audit.review_decision,
                        'revise'::audit.review_decision
                   )
                 ORDER BY decision.sequence_no DESC
                 LIMIT 1;
            END IF;

            v_decision := gen_random_uuid();
            INSERT INTO audit.review_decisions (
                id, review_case_id, sequence_no, decision, reason,
                structured_changes, decided_by, supersedes_decision_id, decided_at
            ) VALUES (
                v_decision, p_case_id, v_seq, p_decision, p_reason,
                p_structured_changes, v_actor, v_prev, clock_timestamp()
            );

            PERFORM audit._apply_publication_grant(
                v_type, v_subject, p_case_id, v_decision, p_decision
            );

            v_new_status := CASE p_decision
                WHEN 'approve'::audit.review_decision THEN 'approved'::audit.review_status
                WHEN 'revise'::audit.review_decision THEN 'approved'::audit.review_status
                WHEN 'reject'::audit.review_decision THEN 'rejected'::audit.review_status
                WHEN 'dispute'::audit.review_decision THEN 'disputed'::audit.review_status
                WHEN 'withdraw'::audit.review_decision THEN 'withdrawn'::audit.review_status
            END;
            UPDATE audit.review_cases
               SET status = v_new_status
             WHERE id = p_case_id;

            BEGIN
                PERFORM audit.append_audit_event(
                    v_key,
                    'review.decision',
                    'review_decision',
                    v_decision,
                    v_request,
                    jsonb_build_object('payload_sha256', v_sha)
                );
            EXCEPTION
                WHEN unique_violation THEN
                    RAISE EXCEPTION 'review_idempotency_payload_conflict'
                        USING ERRCODE = '23505';
            END;
            RETURN v_decision;
        END
        $record_review_decision$;

        DROP FUNCTION IF EXISTS audit._retire_manual_claim_supports(uuid, uuid);
        DROP FUNCTION IF EXISTS audit._replace_claim_evidence(uuid, uuid, uuid[]);
        DROP FUNCTION IF EXISTS audit._apply_claim_subject_bind(uuid, uuid, uuid);
        DROP FUNCTION IF EXISTS audit._require_current_claim_decision(uuid, uuid);
        DROP FUNCTION IF EXISTS audit.create_manual_claim(
            uuid, text, core.claim_type, core.assertion_status, text, uuid[]
        );
        DROP TRIGGER IF EXISTS claim_evidence_require_manual_supports ON core.claim_evidence;
        DROP TRIGGER IF EXISTS claims_require_manual_supports ON core.claims;
        DROP FUNCTION IF EXISTS core.require_manual_claim_supports();
        GRANT EXECUTE ON FUNCTION audit.record_review_decision(
            uuid, audit.review_decision, text, jsonb
        ) TO uap_api;
        """
    )
