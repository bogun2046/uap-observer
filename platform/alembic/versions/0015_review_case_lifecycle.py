"""Review case open/assign/close. No new tables.

Revision ID: 0015_review_case_lifecycle
Revises: 0014_review_session_authority
Create Date: 2026-08-26
"""

from __future__ import annotations

from alembic import op

revision = "0015_review_case_lifecycle"
down_revision = "0014_review_session_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION audit._review_request_id() RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, pg_catalog
        AS $_review_request_id$
        DECLARE
            v_raw text;
            v_id uuid;
        BEGIN
            v_raw := nullif(btrim(current_setting('uap.request_id', true)), '');
            IF v_raw IS NULL THEN
                RAISE EXCEPTION 'review_request_id_missing' USING ERRCODE = '42501';
            END IF;
            BEGIN
                v_id := v_raw::uuid;
            EXCEPTION
                WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'review_request_id_missing' USING ERRCODE = '42501';
            END;
            RETURN v_id;
        END
        $_review_request_id$;

        CREATE FUNCTION audit._canonical_json_number(p_num numeric) RETURNS text
        LANGUAGE plpgsql
        IMMUTABLE
        STRICT
        SET search_path = pg_catalog
        AS $_canonical_json_number$
        DECLARE
            v_text text;
        BEGIN
            IF p_num = 0 THEN
                RETURN '0';
            END IF;
            IF p_num = trunc(p_num) THEN
                RETURN trunc(p_num)::text;
            END IF;
            v_text := p_num::text;
            IF position('.' IN v_text) > 0 THEN
                v_text := rtrim(v_text, '0');
                v_text := rtrim(v_text, '.');
            END IF;
            RETURN v_text;
        END
        $_canonical_json_number$;

        CREATE FUNCTION audit._canonical_json(p_value jsonb) RETURNS text
        LANGUAGE plpgsql
        IMMUTABLE
        STRICT
        SET search_path = pg_catalog
        AS $_canonical_json$
        DECLARE
            v_type text;
            v_parts text[] := ARRAY[]::text[];
            rec record;
        BEGIN
            v_type := jsonb_typeof(p_value);
            IF v_type IN ('null', 'boolean', 'string') THEN
                RETURN p_value::text;
            ELSIF v_type = 'number' THEN
                RETURN audit._canonical_json_number((p_value #>> '{}')::numeric);
            ELSIF v_type = 'array' THEN
                FOR rec IN
                    SELECT elem.value AS value
                      FROM jsonb_array_elements(p_value)
                           WITH ORDINALITY AS elem(value, ord)
                     ORDER BY elem.ord
                LOOP
                    v_parts := v_parts || audit._canonical_json(rec.value);
                END LOOP;
                RETURN '[' || array_to_string(v_parts, ',') || ']';
            ELSIF v_type = 'object' THEN
                FOR rec IN
                    SELECT each.key, each.value
                      FROM jsonb_each(p_value) AS each
                     ORDER BY each.key COLLATE "C"
                LOOP
                    v_parts := v_parts || (
                        to_json(rec.key)::text
                        || ':'
                        || audit._canonical_json(rec.value)
                    );
                END LOOP;
                RETURN '{' || array_to_string(v_parts, ',') || '}';
            END IF;
            RAISE EXCEPTION 'review_canonical_json_unsupported' USING ERRCODE = '22023';
        END
        $_canonical_json$;

        CREATE FUNCTION audit._payload_sha256(p_payload jsonb) RETURNS text
        LANGUAGE sql
        IMMUTABLE
        STRICT
        SET search_path = pg_catalog
        AS $_payload_sha256$
            SELECT encode(
                sha256(convert_to(audit._canonical_json(p_payload), 'UTF8')),
                'hex'
            )
        $_payload_sha256$;

        CREATE FUNCTION audit._existing_write_target(p_event_key text, p_sha text)
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, pg_catalog
        AS $_existing_write_target$
        DECLARE
            v_target uuid;
            v_sha text;
        BEGIN
            SELECT event.target_id, event.metadata ->> 'payload_sha256'
              INTO v_target, v_sha
              FROM audit.audit_events AS event
             WHERE event.event_key = p_event_key;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            IF v_sha IS NOT DISTINCT FROM p_sha THEN
                RETURN v_target;
            END IF;
            RAISE EXCEPTION 'review_idempotency_payload_conflict' USING ERRCODE = '23505';
        END
        $_existing_write_target$;

        CREATE FUNCTION audit._require_reviewer_principal(p_principal_id uuid)
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, pg_catalog
        AS $_require_reviewer_principal$
        DECLARE
            v_active boolean;
            v_type audit.principal_type;
        BEGIN
            SELECT principal.active, principal.principal_type
              INTO v_active, v_type
              FROM audit.principals AS principal
             WHERE principal.id = p_principal_id;
            IF NOT FOUND OR v_active IS NOT TRUE OR v_type IS DISTINCT FROM 'person'::audit.principal_type THEN
                RAISE EXCEPTION 'review_assignee_invalid' USING ERRCODE = '42501';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                  FROM audit.role_bindings AS binding
                 WHERE binding.principal_id = p_principal_id
                   AND binding.revoked_at IS NULL
                   AND binding.role IN (
                        'reviewer'::audit.application_role,
                        'senior_reviewer'::audit.application_role
                   )
                   AND binding.scope_type = 'global'
                   AND binding.scope_id IS NULL
            ) THEN
                RAISE EXCEPTION 'review_role_denied' USING ERRCODE = '42501';
            END IF;
        END
        $_require_reviewer_principal$;

        CREATE FUNCTION audit.open_review_case(
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

        CREATE FUNCTION audit.assign_review_case(
            p_case_id uuid,
            p_assignee_id uuid
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, pg_catalog
        AS $assign_review_case$
        DECLARE
            v_request uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_status audit.review_status;
            v_closed timestamptz;
        BEGIN
            PERFORM audit.require_active_role('reviewer'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_case_id IS NULL OR p_assignee_id IS NULL THEN
                RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
            END IF;

            v_payload := jsonb_build_object(
                'assignee_id', lower(p_assignee_id::text),
                'case_id', lower(p_case_id::text),
                'op', 'review.case.assign'
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.case.assign:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;
            PERFORM audit._require_reviewer_principal(p_assignee_id);

            SELECT review_case.status, review_case.closed_at
              INTO v_status, v_closed
              FROM audit.review_cases AS review_case
             WHERE review_case.id = p_case_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'review_case_missing' USING ERRCODE = '23503';
            END IF;
            IF v_closed IS NOT NULL
               OR v_status NOT IN (
                    'open'::audit.review_status,
                    'assigned'::audit.review_status
               ) THEN
                RAISE EXCEPTION 'review_case_not_assignable' USING ERRCODE = '22023';
            END IF;

            UPDATE audit.review_cases
               SET status = 'assigned'::audit.review_status,
                   assigned_to = p_assignee_id
             WHERE id = p_case_id;

            PERFORM audit.append_audit_event(
                v_key,
                'review.case.assign',
                'review_case',
                p_case_id,
                v_request,
                jsonb_build_object('payload_sha256', v_sha)
            );
            RETURN p_case_id;
        END
        $assign_review_case$;

        CREATE FUNCTION audit.close_review_case(
            p_case_id uuid,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, pg_catalog
        AS $close_review_case$
        DECLARE
            v_request uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_status audit.review_status;
            v_closed timestamptz;
        BEGIN
            PERFORM audit.require_active_role('reviewer'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_reason IS NULL OR char_length(btrim(p_reason)) < 10 THEN
                RAISE EXCEPTION 'review_reason_too_short' USING ERRCODE = '22023';
            END IF;
            IF p_case_id IS NULL THEN
                RAISE EXCEPTION 'review_case_missing' USING ERRCODE = '23503';
            END IF;

            v_payload := jsonb_build_object(
                'case_id', lower(p_case_id::text),
                'op', 'review.case.close',
                'reason', p_reason
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.case.close:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            SELECT review_case.status, review_case.closed_at
              INTO v_status, v_closed
              FROM audit.review_cases AS review_case
             WHERE review_case.id = p_case_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'review_case_missing' USING ERRCODE = '23503';
            END IF;
            IF v_closed IS NOT NULL THEN
                RAISE EXCEPTION 'review_case_already_closed' USING ERRCODE = '22023';
            END IF;
            IF v_status IN (
                'open'::audit.review_status,
                'assigned'::audit.review_status
            ) THEN
                RAISE EXCEPTION 'review_case_not_decidable' USING ERRCODE = '22023';
            END IF;
            IF v_status NOT IN (
                'approved'::audit.review_status,
                'rejected'::audit.review_status,
                'withdrawn'::audit.review_status,
                'disputed'::audit.review_status
            ) THEN
                RAISE EXCEPTION 'review_case_not_decidable' USING ERRCODE = '22023';
            END IF;

            UPDATE audit.review_cases
               SET status = 'closed'::audit.review_status,
                   closed_at = clock_timestamp()
             WHERE id = p_case_id;

            PERFORM audit.append_audit_event(
                v_key,
                'review.case.close',
                'review_case',
                p_case_id,
                v_request,
                jsonb_build_object('payload_sha256', v_sha)
            );
            RETURN p_case_id;
        END
        $close_review_case$;

        REVOKE ALL ON FUNCTION audit._review_request_id() FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._canonical_json_number(numeric) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._canonical_json(jsonb) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._payload_sha256(jsonb) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._existing_write_target(text, text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._require_reviewer_principal(uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit.open_review_case(
            audit.review_case_type, uuid, smallint, text
        ) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit.assign_review_case(uuid, uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit.close_review_case(uuid, text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION audit.open_review_case(
            audit.review_case_type, uuid, smallint, text
        ) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.assign_review_case(uuid, uuid) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.close_review_case(uuid, text) TO uap_api;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        DROP FUNCTION IF EXISTS audit.close_review_case(uuid, text);
        DROP FUNCTION IF EXISTS audit.assign_review_case(uuid, uuid);
        DROP FUNCTION IF EXISTS audit.open_review_case(
            audit.review_case_type, uuid, smallint, text
        );
        DROP FUNCTION IF EXISTS audit._require_reviewer_principal(uuid);
        DROP FUNCTION IF EXISTS audit._existing_write_target(text, text);
        DROP FUNCTION IF EXISTS audit._payload_sha256(jsonb);
        DROP FUNCTION IF EXISTS audit._canonical_json(jsonb);
        DROP FUNCTION IF EXISTS audit._canonical_json_number(numeric);
        DROP FUNCTION IF EXISTS audit._review_request_id();
        """
    )
