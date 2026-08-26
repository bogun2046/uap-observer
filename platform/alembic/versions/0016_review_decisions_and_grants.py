"""Review decisions, live grant indexes, and private publication outbox.

Revision ID: 0016_review_decisions_and_grants
Revises: 0015_review_case_lifecycle
Create Date: 2026-08-26
"""

from __future__ import annotations

from alembic import op

revision = "0016_review_decisions_and_grants"
down_revision = "0015_review_case_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        DROP INDEX IF EXISTS audit.uq_document_grant_active;
        DROP INDEX IF EXISTS audit.uq_claim_grant_active;
        DROP INDEX IF EXISTS audit.uq_entity_grant_active;

        CREATE UNIQUE INDEX uq_document_grant_live
            ON audit.document_publication_grants(document_version_id)
            WHERE grant_status = 'active';
        CREATE UNIQUE INDEX uq_claim_grant_live
            ON audit.claim_publication_grants(claim_id)
            WHERE grant_status = 'active';
        CREATE UNIQUE INDEX uq_entity_grant_live
            ON audit.entity_publication_grants(entity_id)
            WHERE grant_status = 'active';

        ALTER TABLE audit.document_publication_grants
            ADD CONSTRAINT ck_document_grant_live_withdrawal CHECK (
                (
                    grant_status IN (
                        'active'::audit.grant_status,
                        'superseded'::audit.grant_status
                    )
                    AND withdrawn_at IS NULL
                    AND withdrawn_by_decision_id IS NULL
                )
                OR (
                    grant_status = 'withdrawn'::audit.grant_status
                    AND withdrawn_at IS NOT NULL
                    AND withdrawn_by_decision_id IS NOT NULL
                )
            );
        ALTER TABLE audit.claim_publication_grants
            ADD CONSTRAINT ck_claim_grant_live_withdrawal CHECK (
                (
                    grant_status IN (
                        'active'::audit.grant_status,
                        'superseded'::audit.grant_status
                    )
                    AND withdrawn_at IS NULL
                    AND withdrawn_by_decision_id IS NULL
                )
                OR (
                    grant_status = 'withdrawn'::audit.grant_status
                    AND withdrawn_at IS NOT NULL
                    AND withdrawn_by_decision_id IS NOT NULL
                )
            );
        ALTER TABLE audit.entity_publication_grants
            ADD CONSTRAINT ck_entity_grant_live_withdrawal CHECK (
                (
                    grant_status IN (
                        'active'::audit.grant_status,
                        'superseded'::audit.grant_status
                    )
                    AND withdrawn_at IS NULL
                    AND withdrawn_by_decision_id IS NULL
                )
                OR (
                    grant_status = 'withdrawn'::audit.grant_status
                    AND withdrawn_at IS NOT NULL
                    AND withdrawn_by_decision_id IS NOT NULL
                )
            );

        CREATE FUNCTION ops.enqueue_publication_outbox(
            p_event_type text,
            p_event_key text,
            p_aggregate_type text,
            p_aggregate_id uuid,
            p_payload jsonb
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = ops, pg_catalog
        AS $enqueue_publication_outbox$
        DECLARE
            v_id uuid;
            v_payload jsonb;
        BEGIN
            INSERT INTO ops.outbox_events (
                id, causation_job_id, aggregate_type, aggregate_id,
                event_type, event_key, payload, occurred_at
            ) VALUES (
                gen_random_uuid(), NULL, p_aggregate_type, p_aggregate_id,
                p_event_type, p_event_key, p_payload, clock_timestamp()
            )
            RETURNING id INTO v_id;
            RETURN v_id;
        EXCEPTION
            WHEN unique_violation THEN
                SELECT event.id, event.payload
                  INTO v_id, v_payload
                  FROM ops.outbox_events AS event
                 WHERE event.event_key = p_event_key;
                IF v_payload IS NOT DISTINCT FROM p_payload THEN
                    RETURN v_id;
                END IF;
                RAISE EXCEPTION 'review_idempotency_payload_conflict'
                    USING ERRCODE = '23505';
        END
        $enqueue_publication_outbox$;

        CREATE FUNCTION audit._publication_grant_sha(
            p_grant_table text,
            p_grant_id uuid,
            p_subject_type text,
            p_subject_id uuid,
            p_revision integer
        ) RETURNS text
        LANGUAGE sql
        IMMUTABLE
        STRICT
        SET search_path = audit, pg_catalog
        AS $_publication_grant_sha$
            SELECT audit._payload_sha256(
                jsonb_build_object(
                    'grant_id', lower(p_grant_id::text),
                    'grant_table', p_grant_table,
                    'revision_no', p_revision,
                    'subject_id', lower(p_subject_id::text),
                    'subject_type', p_subject_type
                )
            )
        $_publication_grant_sha$;

        CREATE FUNCTION audit._apply_publication_grant(
            p_case_type audit.review_case_type,
            p_subject_id uuid,
            p_case_id uuid,
            p_decision_id uuid,
            p_decision audit.review_decision
        ) RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, ops, pg_catalog
        AS $_apply_publication_grant$
        DECLARE
            v_table text;
            v_old uuid;
            v_old_rev integer;
            v_new uuid;
            v_rev integer;
            v_sha text;
            v_payload jsonb;
        BEGIN
            IF p_case_type = 'document'::audit.review_case_type THEN
                v_table := 'document_publication_grants';
                SELECT grant_row.id INTO v_old
                  FROM audit.document_publication_grants AS grant_row
                 WHERE grant_row.document_version_id = p_subject_id
                   AND grant_row.grant_status = 'active'::audit.grant_status;
                SELECT coalesce(max(grant_row.revision_no), 0) INTO v_old_rev
                  FROM audit.document_publication_grants AS grant_row
                 WHERE grant_row.document_version_id = p_subject_id;
            ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                v_table := 'claim_publication_grants';
                SELECT grant_row.id INTO v_old
                  FROM audit.claim_publication_grants AS grant_row
                 WHERE grant_row.claim_id = p_subject_id
                   AND grant_row.grant_status = 'active'::audit.grant_status;
                SELECT coalesce(max(grant_row.revision_no), 0) INTO v_old_rev
                  FROM audit.claim_publication_grants AS grant_row
                 WHERE grant_row.claim_id = p_subject_id;
            ELSIF p_case_type = 'entity'::audit.review_case_type THEN
                v_table := 'entity_publication_grants';
                SELECT grant_row.id INTO v_old
                  FROM audit.entity_publication_grants AS grant_row
                 WHERE grant_row.entity_id = p_subject_id
                   AND grant_row.grant_status = 'active'::audit.grant_status;
                SELECT coalesce(max(grant_row.revision_no), 0) INTO v_old_rev
                  FROM audit.entity_publication_grants AS grant_row
                 WHERE grant_row.entity_id = p_subject_id;
            ELSE
                RAISE EXCEPTION 'knowledge_relation_review_not_in_wp9'
                    USING ERRCODE = '22023';
            END IF;

            IF p_decision IN (
                'reject'::audit.review_decision,
                'dispute'::audit.review_decision
            ) THEN
                RETURN;
            END IF;

            IF p_decision = 'withdraw'::audit.review_decision THEN
                IF v_old IS NULL THEN
                    RAISE EXCEPTION 'review_grant_not_active' USING ERRCODE = '22023';
                END IF;
                IF p_case_type = 'document'::audit.review_case_type THEN
                    UPDATE audit.document_publication_grants
                       SET grant_status = 'withdrawn'::audit.grant_status,
                           withdrawn_by_decision_id = p_decision_id,
                           withdrawn_at = clock_timestamp()
                     WHERE id = v_old;
                ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                    UPDATE audit.claim_publication_grants
                       SET grant_status = 'withdrawn'::audit.grant_status,
                           withdrawn_by_decision_id = p_decision_id,
                           withdrawn_at = clock_timestamp()
                     WHERE id = v_old;
                ELSE
                    UPDATE audit.entity_publication_grants
                       SET grant_status = 'withdrawn'::audit.grant_status,
                           withdrawn_by_decision_id = p_decision_id,
                           withdrawn_at = clock_timestamp()
                     WHERE id = v_old;
                END IF;
                v_payload := jsonb_build_object(
                    'decision_id', lower(p_decision_id::text),
                    'grant_id', lower(v_old::text),
                    'payload_sha256', '',
                    'revision_no', v_old_rev,
                    'schema', 'publication-outbox.v1',
                    'subject_id', lower(p_subject_id::text),
                    'subject_type', p_case_type::text
                );
                IF p_case_type = 'document'::audit.review_case_type THEN
                    SELECT grant_row.publication_payload_sha256, grant_row.revision_no
                      INTO v_sha, v_rev
                      FROM audit.document_publication_grants AS grant_row
                     WHERE grant_row.id = v_old;
                ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                    SELECT grant_row.publication_payload_sha256, grant_row.revision_no
                      INTO v_sha, v_rev
                      FROM audit.claim_publication_grants AS grant_row
                     WHERE grant_row.id = v_old;
                ELSE
                    SELECT grant_row.publication_payload_sha256, grant_row.revision_no
                      INTO v_sha, v_rev
                      FROM audit.entity_publication_grants AS grant_row
                     WHERE grant_row.id = v_old;
                END IF;
                v_payload := jsonb_set(v_payload, '{payload_sha256}', to_jsonb(v_sha));
                v_payload := jsonb_set(v_payload, '{revision_no}', to_jsonb(v_rev));
                PERFORM ops.enqueue_publication_outbox(
                    'publication.withdrawn',
                    'publication-withdrawn:' || v_table || ':' || v_old::text
                        || ':' || p_decision_id::text,
                    v_table,
                    v_old,
                    v_payload
                );
                RETURN;
            END IF;

            IF p_decision = 'approve'::audit.review_decision THEN
                IF v_old IS NOT NULL THEN
                    RAISE EXCEPTION 'review_grant_already_active' USING ERRCODE = '22023';
                END IF;
            ELSIF p_decision = 'revise'::audit.review_decision THEN
                IF v_old IS NULL THEN
                    RAISE EXCEPTION 'review_grant_not_active' USING ERRCODE = '22023';
                END IF;
            ELSE
                RAISE EXCEPTION 'review_decision_not_allowed' USING ERRCODE = '22023';
            END IF;

            v_new := gen_random_uuid();
            v_rev := v_old_rev + 1;
            v_sha := audit._publication_grant_sha(
                v_table, v_new, p_case_type::text, p_subject_id, v_rev
            );

            IF p_decision = 'revise'::audit.review_decision THEN
                IF p_case_type = 'document'::audit.review_case_type THEN
                    UPDATE audit.document_publication_grants
                       SET grant_status = 'superseded'::audit.grant_status
                     WHERE id = v_old;
                ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                    UPDATE audit.claim_publication_grants
                       SET grant_status = 'superseded'::audit.grant_status
                     WHERE id = v_old;
                ELSE
                    UPDATE audit.entity_publication_grants
                       SET grant_status = 'superseded'::audit.grant_status
                     WHERE id = v_old;
                END IF;
            END IF;

            IF p_case_type = 'document'::audit.review_case_type THEN
                INSERT INTO audit.document_publication_grants (
                    id, review_case_id, document_version_id, decision_id,
                    revision_no, grant_status, granted_at, publication_payload_sha256
                ) VALUES (
                    v_new, p_case_id, p_subject_id, p_decision_id,
                    v_rev, 'active'::audit.grant_status, clock_timestamp(), v_sha
                );
            ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                INSERT INTO audit.claim_publication_grants (
                    id, review_case_id, claim_id, decision_id,
                    revision_no, grant_status, granted_at, publication_payload_sha256
                ) VALUES (
                    v_new, p_case_id, p_subject_id, p_decision_id,
                    v_rev, 'active'::audit.grant_status, clock_timestamp(), v_sha
                );
            ELSE
                INSERT INTO audit.entity_publication_grants (
                    id, review_case_id, entity_id, decision_id,
                    revision_no, grant_status, granted_at, publication_payload_sha256
                ) VALUES (
                    v_new, p_case_id, p_subject_id, p_decision_id,
                    v_rev, 'active'::audit.grant_status, clock_timestamp(), v_sha
                );
            END IF;

            IF p_decision = 'revise'::audit.review_decision THEN
                PERFORM ops.enqueue_publication_outbox(
                    'publication.superseded',
                    'publication-superseded:' || v_table || ':' || v_old::text
                        || ':' || v_new::text,
                    v_table,
                    v_old,
                    jsonb_build_object(
                        'decision_id', lower(p_decision_id::text),
                        'grant_id', lower(v_new::text),
                        'old_grant_id', lower(v_old::text),
                        'payload_sha256', v_sha,
                        'revision_no', v_rev,
                        'schema', 'publication-outbox.v1',
                        'subject_id', lower(p_subject_id::text),
                        'subject_type', p_case_type::text
                    )
                );
            END IF;

            PERFORM ops.enqueue_publication_outbox(
                'publication.granted',
                'publication-granted:' || v_table || ':' || v_new::text,
                v_table,
                v_new,
                jsonb_build_object(
                    'decision_id', lower(p_decision_id::text),
                    'grant_id', lower(v_new::text),
                    'payload_sha256', v_sha,
                    'revision_no', v_rev,
                    'schema', 'publication-outbox.v1',
                    'subject_id', lower(p_subject_id::text),
                    'subject_type', p_case_type::text
                )
            );
        END
        $_apply_publication_grant$;

        CREATE FUNCTION audit.record_review_decision(
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

        REVOKE ALL ON FUNCTION ops.enqueue_publication_outbox(
            text, text, text, uuid, jsonb
        ) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._publication_grant_sha(
            text, uuid, text, uuid, integer
        ) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit.record_review_decision(
            uuid, audit.review_decision, text, jsonb
        ) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION audit.record_review_decision(
            uuid, audit.review_decision, text, jsonb
        ) TO uap_api;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        DO $refuse_superseded$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM audit.document_publication_grants
                 WHERE grant_status = 'superseded'::audit.grant_status
            ) OR EXISTS (
                SELECT 1 FROM audit.claim_publication_grants
                 WHERE grant_status = 'superseded'::audit.grant_status
            ) OR EXISTS (
                SELECT 1 FROM audit.entity_publication_grants
                 WHERE grant_status = 'superseded'::audit.grant_status
            ) THEN
                RAISE EXCEPTION 'review_grant_superseded_blocks_downgrade'
                    USING ERRCODE = '22023';
            END IF;
        END
        $refuse_superseded$;

        DROP FUNCTION IF EXISTS audit.record_review_decision(
            uuid, audit.review_decision, text, jsonb
        );
        DROP FUNCTION IF EXISTS audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        );
        DROP FUNCTION IF EXISTS audit._publication_grant_sha(
            text, uuid, text, uuid, integer
        );
        DROP FUNCTION IF EXISTS ops.enqueue_publication_outbox(
            text, text, text, uuid, jsonb
        );

        ALTER TABLE audit.document_publication_grants
            DROP CONSTRAINT IF EXISTS ck_document_grant_live_withdrawal;
        ALTER TABLE audit.claim_publication_grants
            DROP CONSTRAINT IF EXISTS ck_claim_grant_live_withdrawal;
        ALTER TABLE audit.entity_publication_grants
            DROP CONSTRAINT IF EXISTS ck_entity_grant_live_withdrawal;

        DROP INDEX IF EXISTS audit.uq_document_grant_live;
        DROP INDEX IF EXISTS audit.uq_claim_grant_live;
        DROP INDEX IF EXISTS audit.uq_entity_grant_live;

        CREATE UNIQUE INDEX uq_document_grant_active
            ON audit.document_publication_grants(document_version_id)
            WHERE withdrawn_at IS NULL;
        CREATE UNIQUE INDEX uq_claim_grant_active
            ON audit.claim_publication_grants(claim_id)
            WHERE withdrawn_at IS NULL;
        CREATE UNIQUE INDEX uq_entity_grant_active
            ON audit.entity_publication_grants(entity_id)
            WHERE withdrawn_at IS NULL;
        """
    )
