"""Close Prompt/model provenance and lifecycle constraints for WP7.

Revision ID: 0008_ai_model_governance
Revises: 0007_source_run_lease_guard
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op

revision = "0008_ai_model_governance"
down_revision = "0007_source_run_lease_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        ALTER TABLE ops.prompt_versions
            ADD CONSTRAINT uq_prompt_versions_id_task UNIQUE (id, task_type);
        CREATE UNIQUE INDEX uq_prompt_versions_active_task
            ON ops.prompt_versions (task_type)
            WHERE active;

        ALTER TABLE ops.model_runs
            DROP CONSTRAINT model_runs_prompt_version_id_fkey,
            ADD CONSTRAINT fk_model_run_prompt_same_task
                FOREIGN KEY (prompt_version_id, task_type)
                REFERENCES ops.prompt_versions (id, task_type),
            ADD CONSTRAINT ck_model_run_lifecycle CHECK (
                (status = 'started' AND finished_at IS NULL AND error_code IS NULL)
                OR (
                    status IN ('succeeded', 'failed', 'invalid')
                    AND finished_at IS NOT NULL
                    AND (
                        (status = 'succeeded' AND error_code IS NULL)
                        OR (status IN ('failed', 'invalid') AND error_code IS NOT NULL)
                    )
                )
            ),
            ADD CONSTRAINT ck_model_run_currency CHECK (
                (cost_minor_units IS NULL AND currency IS NULL)
                OR (cost_minor_units IS NOT NULL AND currency ~ '^[A-Z]{3}$')
            );

        REVOKE INSERT, UPDATE, DELETE ON core.analysis_results FROM uap_api;
        REVOKE UPDATE, DELETE ON ops.prompt_versions FROM uap_worker, uap_api;
        RESET ROLE;
        """
    )

def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;
        GRANT INSERT, UPDATE, DELETE ON core.analysis_results TO uap_api;
        GRANT UPDATE, DELETE ON ops.prompt_versions TO uap_worker, uap_api;
        ALTER TABLE ops.model_runs
            DROP CONSTRAINT ck_model_run_currency,
            DROP CONSTRAINT ck_model_run_lifecycle,
            DROP CONSTRAINT fk_model_run_prompt_same_task,
            ADD CONSTRAINT model_runs_prompt_version_id_fkey
                FOREIGN KEY (prompt_version_id) REFERENCES ops.prompt_versions(id);
        DROP INDEX ops.uq_prompt_versions_active_task;
        ALTER TABLE ops.prompt_versions
            DROP CONSTRAINT uq_prompt_versions_id_task;
        RESET ROLE;
        """
    )
