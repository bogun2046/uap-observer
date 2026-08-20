"""Harden semantic model idempotency, accounting, and runtime boundaries.

Revision ID: 0009_model_governance_boundaries
Revises: 0008_ai_model_governance
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op

revision = "0009_model_governance_boundaries"
down_revision = "0008_ai_model_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        ALTER TABLE ops.model_runs
            ADD COLUMN semantic_idempotency_key text;
        UPDATE ops.model_runs
           SET semantic_idempotency_key = 'model-legacy:' || idempotency_key
         WHERE semantic_idempotency_key IS NULL;
        ALTER TABLE ops.model_runs
            ALTER COLUMN semantic_idempotency_key SET NOT NULL,
            ADD CONSTRAINT ck_model_runs_semantic_key
                CHECK (btrim(semantic_idempotency_key) <> '');
        CREATE INDEX ix_model_runs_semantic_key
            ON ops.model_runs (semantic_idempotency_key);
        CREATE UNIQUE INDEX uq_model_runs_semantic_success
            ON ops.model_runs (semantic_idempotency_key)
            WHERE status = 'succeeded'::ops.model_run_status;

        CREATE FUNCTION ops.finish_model_job(
            p_job_id uuid,
            p_attempt_id uuid,
            p_lease_token uuid,
            p_outcome ops.attempt_outcome,
            p_http_status smallint DEFAULT NULL,
            p_error_code text DEFAULT NULL,
            p_error_summary text DEFAULT NULL,
            p_retry_delay_seconds integer DEFAULT NULL
        ) RETURNS ops.job_status
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, pg_catalog
        AS $finish_model_job$
        DECLARE
            requested_job_type text;
        BEGIN
            SELECT job_type INTO requested_job_type
              FROM ops.jobs
             WHERE id = p_job_id
             FOR UPDATE;
            IF requested_job_type IS NULL THEN
                RAISE EXCEPTION 'model job does not exist' USING ERRCODE='40001';
            END IF;
            IF requested_job_type <> 'analyze_document' THEN
                RAISE EXCEPTION 'model governance role may only finish analyze_document jobs'
                    USING ERRCODE='42501';
            END IF;
            RETURN ops.finish_job(
                p_job_id, p_attempt_id, p_lease_token, p_outcome,
                p_http_status, p_error_code, p_error_summary, p_retry_delay_seconds
            );
        END
        $finish_model_job$;

        CREATE FUNCTION core.guard_model_governance_object_domain() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = core, pg_catalog
        AS $guard_model_object$
        BEGIN
            IF session_user = 'uap_model_governance'
               AND (
                   NEW.storage_domain IS DISTINCT FROM 'model_io'::core.storage_domain
                   OR (
                       TG_OP = 'UPDATE'
                       AND OLD.storage_domain IS DISTINCT FROM 'model_io'::core.storage_domain
                   )
               ) THEN
                RAISE EXCEPTION 'model governance may only write model_io objects'
                    USING ERRCODE='42501';
            END IF;
            RETURN NEW;
        END
        $guard_model_object$;
        REVOKE ALL ON FUNCTION core.guard_model_governance_object_domain() FROM PUBLIC;
        CREATE TRIGGER guard_model_governance_object_domain
            BEFORE INSERT OR UPDATE ON core.stored_objects
            FOR EACH ROW EXECUTE FUNCTION core.guard_model_governance_object_domain();

        DO $db$
        BEGIN
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO uap_model_governance', current_database());
        END
        $db$;
        GRANT USAGE ON SCHEMA core, ops TO uap_model_governance;
        GRANT SELECT ON core.extractions, core.stored_objects TO uap_model_governance;
        GRANT SELECT ON core.analysis_results TO uap_model_governance;
        GRANT INSERT, UPDATE ON core.stored_objects TO uap_model_governance;
        GRANT SELECT ON ops.prompt_versions, ops.model_runs, ops.jobs, ops.job_attempts
            TO uap_model_governance;
        GRANT INSERT ON ops.prompt_versions, ops.model_runs TO uap_model_governance;
        GRANT INSERT ON core.analysis_results TO uap_model_governance;
        REVOKE ALL ON ops.prompt_versions, ops.model_runs, core.analysis_results
            FROM uap_worker, uap_api;
        REVOKE ALL ON FUNCTION ops.finish_model_job(
            uuid, uuid, uuid, ops.attempt_outcome, smallint, text, text, integer
        ) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION ops.finish_model_job(
            uuid, uuid, uuid, ops.attempt_outcome, smallint, text, text, integer
        ) TO uap_model_governance;

        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;
        DROP TRIGGER guard_model_governance_object_domain ON core.stored_objects;
        DROP FUNCTION core.guard_model_governance_object_domain();
        REVOKE EXECUTE ON FUNCTION ops.finish_model_job(
            uuid, uuid, uuid, ops.attempt_outcome, smallint, text, text, integer
        ) FROM uap_model_governance;
        DROP FUNCTION ops.finish_model_job(
            uuid, uuid, uuid, ops.attempt_outcome, smallint, text, text, integer
        );
        REVOKE ALL ON ops.prompt_versions, ops.model_runs, core.analysis_results
            FROM uap_model_governance;
        REVOKE SELECT ON core.extractions, core.stored_objects FROM uap_model_governance;
        REVOKE INSERT, UPDATE ON core.stored_objects FROM uap_model_governance;
        REVOKE SELECT ON ops.jobs, ops.job_attempts FROM uap_model_governance;
        DO $db$
        BEGIN
            EXECUTE format(
                'REVOKE CONNECT ON DATABASE %I FROM uap_model_governance',
                current_database()
            );
        END
        $db$;
        REVOKE USAGE ON SCHEMA core, ops FROM uap_model_governance;
        GRANT SELECT, INSERT, UPDATE ON ops.prompt_versions, ops.model_runs TO uap_worker;
        GRANT SELECT, INSERT, UPDATE ON core.analysis_results TO uap_worker;
        GRANT SELECT, INSERT, UPDATE ON ops.prompt_versions, ops.model_runs TO uap_api;
        GRANT SELECT, INSERT, UPDATE ON core.analysis_results TO uap_api;
        DROP INDEX ops.uq_model_runs_semantic_success;
        DROP INDEX ops.ix_model_runs_semantic_key;
        ALTER TABLE ops.model_runs
            DROP CONSTRAINT ck_model_runs_semantic_key,
            DROP COLUMN semantic_idempotency_key;
        RESET ROLE;
        """
    )
