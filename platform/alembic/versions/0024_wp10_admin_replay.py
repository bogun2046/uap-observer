"""WP10.5 data_operator publication replay wrapper. No new tables.

Revision ID: 0024_wp10_admin_replay
Revises: 0023_wp10_api_read_indexes
Create Date: 2026-08-31
"""

from __future__ import annotations

from alembic import op

revision = "0024_wp10_admin_replay"
down_revision = "0023_wp10_api_read_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION audit.requeue_publication_event(p_event_id uuid, p_reason text)
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, ops, pg_catalog
        AS $requeue_publication_event$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_event ops.outbox_events%ROWTYPE;
        BEGIN
            IF session_user IS DISTINCT FROM 'uap_api' THEN
                RAISE EXCEPTION 'review_session_role_denied' USING ERRCODE = '42501';
            END IF;
            v_actor := audit.require_active_role('data_operator'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_event_id IS NULL THEN
                RAISE EXCEPTION 'api_resource_not_found' USING ERRCODE = '02000';
            END IF;
            IF p_reason IS NULL OR char_length(btrim(p_reason)) < 10 THEN
                RAISE EXCEPTION 'publication_replay_reason_too_short' USING ERRCODE = '22023';
            END IF;
            v_payload := jsonb_build_object(
                'event_id', lower(p_event_id::text),
                'op', 'publication.replay',
                'reason', p_reason
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'publication.replay:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            SELECT event.*
              INTO v_event
              FROM ops.outbox_events AS event
             WHERE event.id = p_event_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'api_resource_not_found' USING ERRCODE = '02000';
            END IF;
            IF v_event.event_type NOT LIKE 'publication.%' THEN
                RAISE EXCEPTION 'api_resource_not_found' USING ERRCODE = '02000';
            END IF;
            IF v_event.terminal_at IS NULL THEN
                RAISE EXCEPTION 'publication_event_not_terminal' USING ERRCODE = '23505';
            END IF;

            UPDATE ops.outbox_events AS outbox
               SET terminal_at = NULL,
                   terminal_error_code = NULL,
                   last_error_code = NULL,
                   last_error_summary = NULL,
                   lease_owner = NULL,
                   lease_token = NULL,
                   lease_expires_at = NULL,
                   available_at = clock_timestamp()
             WHERE outbox.id = p_event_id;

            INSERT INTO audit.audit_events (
                id, event_key, actor_id, action, target_type, target_id,
                request_id, metadata, occurred_at
            ) VALUES (
                gen_random_uuid(),
                v_key,
                v_actor,
                'publication.replay',
                'outbox_event',
                p_event_id,
                v_request,
                jsonb_build_object('payload_sha256', v_sha),
                clock_timestamp()
            );

            RETURN p_event_id;
        END
        $requeue_publication_event$;

        REVOKE ALL ON FUNCTION audit.requeue_publication_event(uuid, text)
            FROM PUBLIC, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        GRANT EXECUTE ON FUNCTION audit.requeue_publication_event(uuid, text) TO uap_api;
        GRANT SELECT ON core.analysis_results TO uap_api;
        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;
        REVOKE SELECT ON core.analysis_results FROM uap_api;
        REVOKE EXECUTE ON FUNCTION audit.requeue_publication_event(uuid, text) FROM uap_api;
        DROP FUNCTION audit.requeue_publication_event(uuid, text);
        RESET ROLE;
        """
    )
