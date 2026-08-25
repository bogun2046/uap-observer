"""Review session GUC and require_active_role. No new tables.

Revision ID: 0014_review_session_authority
Revises: 0013_entity_merge_state_machine
Create Date: 2026-08-26
"""

from __future__ import annotations

from alembic import op

revision = "0014_review_session_authority"
down_revision = "0013_entity_merge_state_machine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION audit.require_active_role(p_role audit.application_role)
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, pg_catalog
        AS $require_active_role$
        DECLARE
            v_raw text;
            v_principal_id uuid;
            v_active boolean;
            v_type audit.principal_type;
            v_has_global boolean;
            v_has_scoped boolean;
        BEGIN
            IF session_user IS DISTINCT FROM 'uap_api' THEN
                RAISE EXCEPTION 'review_session_role_denied' USING ERRCODE = '42501';
            END IF;

            v_raw := nullif(btrim(current_setting('uap.principal_id', true)), '');
            IF v_raw IS NULL THEN
                RAISE EXCEPTION 'review_principal_missing' USING ERRCODE = '42501';
            END IF;
            BEGIN
                v_principal_id := v_raw::uuid;
            EXCEPTION
                WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'review_principal_missing' USING ERRCODE = '42501';
            END;

            SELECT principal.active, principal.principal_type
              INTO v_active, v_type
              FROM audit.principals AS principal
             WHERE principal.id = v_principal_id;

            IF NOT FOUND OR v_active IS NOT TRUE THEN
                RAISE EXCEPTION 'review_principal_missing' USING ERRCODE = '42501';
            END IF;
            IF v_type IS DISTINCT FROM 'person'::audit.principal_type THEN
                RAISE EXCEPTION 'review_service_principal_denied' USING ERRCODE = '42501';
            END IF;

            SELECT
                EXISTS (
                    SELECT 1
                      FROM audit.role_bindings AS binding
                     WHERE binding.principal_id = v_principal_id
                       AND binding.revoked_at IS NULL
                       AND (
                            binding.role = p_role
                            OR (
                                p_role = 'reviewer'::audit.application_role
                                AND binding.role = 'senior_reviewer'::audit.application_role
                            )
                       )
                       AND binding.scope_type = 'global'
                       AND binding.scope_id IS NULL
                ),
                EXISTS (
                    SELECT 1
                      FROM audit.role_bindings AS binding
                     WHERE binding.principal_id = v_principal_id
                       AND binding.revoked_at IS NULL
                       AND (
                            binding.role = p_role
                            OR (
                                p_role = 'reviewer'::audit.application_role
                                AND binding.role = 'senior_reviewer'::audit.application_role
                            )
                       )
                       AND NOT (
                            binding.scope_type = 'global'
                            AND binding.scope_id IS NULL
                       )
                )
              INTO v_has_global, v_has_scoped;

            IF v_has_global THEN
                RETURN v_principal_id;
            END IF;
            IF v_has_scoped THEN
                RAISE EXCEPTION 'review_scope_unsupported' USING ERRCODE = '42501';
            END IF;
            RAISE EXCEPTION 'review_role_denied' USING ERRCODE = '42501';
        END
        $require_active_role$;

        CREATE FUNCTION audit.append_audit_event(
            p_event_key text,
            p_action text,
            p_target_type text,
            p_target_id uuid,
            p_request_id uuid,
            p_metadata jsonb
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, pg_catalog
        AS $append_audit_event$
        DECLARE
            v_actor uuid;
            v_id uuid;
        BEGIN
            v_actor := audit.require_active_role('reviewer'::audit.application_role);
            INSERT INTO audit.audit_events (
                id, event_key, actor_id, action, target_type, target_id,
                request_id, metadata, occurred_at
            ) VALUES (
                gen_random_uuid(),
                p_event_key,
                v_actor,
                p_action,
                p_target_type,
                p_target_id,
                p_request_id,
                coalesce(p_metadata, '{}'::jsonb),
                clock_timestamp()
            )
            RETURNING id INTO v_id;
            RETURN v_id;
        END
        $append_audit_event$;

        REVOKE ALL ON FUNCTION audit.require_active_role(audit.application_role) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION audit.require_active_role(audit.application_role) TO uap_api;
        REVOKE ALL ON FUNCTION audit.append_audit_event(text, text, text, uuid, uuid, jsonb)
            FROM PUBLIC;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        DROP FUNCTION IF EXISTS audit.append_audit_event(text, text, text, uuid, uuid, jsonb);
        DROP FUNCTION IF EXISTS audit.require_active_role(audit.application_role);
        """
    )
