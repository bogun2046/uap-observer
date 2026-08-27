"""Guard source-run checkpoints with the current fetch-source lease.

Revision ID: 0007_source_run_lease_guard
Revises: 0006_collectors
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "0007_source_run_lease_guard"
down_revision = "0006_collectors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION ops.require_active_source_job_lease(
            p_job_id uuid,
            p_attempt_id uuid,
            p_lease_token uuid
        ) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, pg_catalog
        AS $require_active_source_job_lease$
        DECLARE
            current_job ops.jobs%ROWTYPE;
            observed_at timestamptz;
        BEGIN
            IF session_user <> 'uap_worker' THEN
                RAISE EXCEPTION 'only worker may checkpoint a source run'
                    USING ERRCODE='42501';
            END IF;

            SELECT * INTO current_job
              FROM ops.jobs
             WHERE id = p_job_id
             FOR UPDATE;
            observed_at := clock_timestamp();
            IF current_job.id IS NULL
               OR current_job.job_type <> 'fetch_source'
               OR current_job.status <> 'running'
               OR current_job.lease_token IS DISTINCT FROM p_lease_token
               OR current_job.lease_expires_at <= observed_at THEN
                RAISE EXCEPTION 'source job lease is missing, expired, or owned by another worker'
                    USING ERRCODE='40001';
            END IF;

            PERFORM 1
              FROM ops.job_attempts
             WHERE id = p_attempt_id
               AND job_id = p_job_id
               AND lease_token = p_lease_token
               AND outcome = 'running'
               AND finished_at IS NULL
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'source job attempt is missing or already finished'
                    USING ERRCODE='40001';
            END IF;
        END
        $require_active_source_job_lease$;

        REVOKE ALL ON FUNCTION ops.require_active_source_job_lease(uuid, uuid, uuid)
            FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION ops.require_active_source_job_lease(uuid, uuid, uuid)
            TO uap_worker;

        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;
        REVOKE EXECUTE ON FUNCTION ops.require_active_source_job_lease(uuid, uuid, uuid)
            FROM uap_worker;
        DROP FUNCTION ops.require_active_source_job_lease(uuid, uuid, uuid);
        RESET ROLE;
        """
    )
