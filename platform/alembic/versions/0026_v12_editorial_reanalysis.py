"""V1-2.1B controlled editorial reanalysis enqueue boundary."""

from __future__ import annotations

from alembic import op

revision = "0026_v12_editorial_reanalysis"
down_revision = "0025_v12_editorial_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION audit.request_editorial_reanalysis(
            p_document_id uuid,
            p_document_version_id uuid,
            p_task_type text,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = audit, core, ops, pg_catalog
        AS $request_editorial_reanalysis$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_version uuid;
            v_deleted_at timestamptz;
            v_prompt uuid;
            v_calls integer;
            v_monthly bigint;
            v_key text;
            v_payload jsonb;
            v_job uuid;
            v_existing uuid;
        BEGIN
            IF session_user IS DISTINCT FROM 'uap_api' THEN
                RAISE EXCEPTION 'editorial_session_role_denied' USING ERRCODE = '42501';
            END IF;
            v_actor := audit.require_active_role('editorial_admin'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_document_id IS NULL OR p_document_version_id IS NULL
               OR p_task_type NOT IN (
                    'classification', 'summary', 'claim_extraction', 'entity_extraction'
               )
               OR char_length(btrim(coalesce(p_reason, ''))) NOT BETWEEN 1 AND 2000 THEN
                RAISE EXCEPTION 'editorial_reanalysis_invalid' USING ERRCODE = '22023';
            END IF;

            SELECT version.id, document.deleted_at
              INTO v_version, v_deleted_at
              FROM core.document_versions AS version
              JOIN core.documents AS document ON document.id = version.document_id
              JOIN core.extractions AS extraction
                ON extraction.document_version_id = version.id
               AND extraction.outcome = 'succeeded'::core.extraction_outcome
             WHERE document.id = p_document_id
               AND version.id = p_document_version_id
             LIMIT 1;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'editorial_document_version_mismatch' USING ERRCODE = '22023';
            END IF;
            IF v_deleted_at IS NOT NULL THEN
                RAISE EXCEPTION 'editorial_document_trashed' USING ERRCODE = '22023';
            END IF;

            SELECT prompt.id
              INTO v_prompt
              FROM ops.prompt_versions AS prompt
             WHERE prompt.task_type = p_task_type::ops.model_task_type
               AND prompt.active
             ORDER BY prompt.created_at DESC, prompt.id DESC
             LIMIT 1;
            IF v_prompt IS NULL THEN
                RAISE EXCEPTION 'active_prompt_missing' USING ERRCODE = '02000';
            END IF;

            SELECT count(*)
              INTO v_calls
              FROM ops.model_runs
             WHERE document_version_id = p_document_version_id;
            IF v_calls >= 12 THEN
                RAISE EXCEPTION 'article_model_call_budget_exhausted' USING ERRCODE = '22023';
            END IF;
            SELECT coalesce(sum(cost_minor_units), 0) * 10000
              INTO v_monthly
              FROM ops.model_runs
             WHERE currency = 'CNY'
               AND started_at >= (
                    date_trunc('month', clock_timestamp() AT TIME ZONE 'Asia/Shanghai')
                    AT TIME ZONE 'Asia/Shanghai'
               );
            IF v_monthly >= 20000000 THEN
                RAISE EXCEPTION 'monthly_model_budget_exhausted' USING ERRCODE = '22023';
            END IF;

            v_key := 'v1-admin-model:' || p_document_version_id::text || ':'
                || p_task_type || ':' || v_request::text;
            v_payload := jsonb_build_object(
                'document_id', p_document_id,
                'document_version_id', p_document_version_id,
                'prompt_version_id', v_prompt,
                'task_type', p_task_type,
                'provider', 'deepseek',
                'model', 'deepseek-flash',
                'payload_schema_version', 'model.v1'
            );
            v_existing := audit._existing_write_target(
                'v1-reanalysis:' || v_request::text,
                audit._payload_sha256(v_payload)
            );
            IF v_existing IS NOT NULL THEN
                SELECT (event.metadata ->> 'job_id')::uuid
                  INTO v_job
                  FROM audit.audit_events AS event
                 WHERE event.event_key = 'v1-reanalysis:' || v_request::text;
                RETURN coalesce(v_job, v_existing);
            END IF;
            SELECT job.id INTO v_existing
              FROM ops.jobs AS job
             WHERE job.idempotency_key = v_key;
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;
            PERFORM pg_advisory_xact_lock(9177, hashtext(v_key));
            SELECT job.id INTO v_existing
              FROM ops.jobs AS job
             WHERE job.idempotency_key = v_key;
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            INSERT INTO ops.jobs (
                id, job_type, payload, payload_schema_version, idempotency_key,
                priority, available_at, max_attempts, timeout_seconds
            ) VALUES (
                md5(random()::text || clock_timestamp()::text)::uuid,
                'analyze_document', v_payload, 'model.v1', v_key,
                10::smallint, now(), 3, 60
            )
            RETURNING id INTO v_job;

            INSERT INTO audit.audit_events (
                id, event_key, actor_id, action, target_type, target_id,
                request_id, after_digest, metadata, occurred_at
            ) VALUES (
                gen_random_uuid(), 'v1-reanalysis:' || v_request::text, v_actor,
                'reanalysis.request', 'document', p_document_id, v_request,
                audit._payload_sha256(v_payload),
                jsonb_build_object(
                    'payload_sha256', audit._payload_sha256(v_payload),
                    'task_type', p_task_type,
                    'reason', btrim(p_reason),
                    'job_id', v_job,
                    'document_version_id', p_document_version_id,
                    'prompt_version_id', v_prompt
                ),
                clock_timestamp()
            );
            RETURN v_job;
        END
        $request_editorial_reanalysis$;

        REVOKE ALL ON FUNCTION audit.request_editorial_reanalysis(uuid, uuid, text, text)
            FROM PUBLIC, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        GRANT EXECUTE ON FUNCTION audit.request_editorial_reanalysis(uuid, uuid, text, text)
            TO uap_api;
        GRANT SELECT ON ops.model_runs, ops.prompt_versions TO uap_api;
        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;
        REVOKE ALL ON FUNCTION audit.request_editorial_reanalysis(uuid, uuid, text, text)
            FROM PUBLIC, uap_api;
        DROP FUNCTION IF EXISTS audit.request_editorial_reanalysis(uuid, uuid, text, text);
        REVOKE SELECT ON ops.model_runs, ops.prompt_versions FROM uap_api;
        RESET ROLE;
        """
    )
