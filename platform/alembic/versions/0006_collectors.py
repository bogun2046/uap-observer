"""Add collector payload provenance and source health state.

Revision ID: 0006_collectors
Revises: 0005_durable_jobs
Create Date: 2026-08-17
"""

from __future__ import annotations

from alembic import op

revision = "0006_collectors"
down_revision = "0005_durable_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        SET ROLE uap_owner;

        ALTER TABLE ingest.source_config_versions
            ADD CONSTRAINT uq_source_config_versions_id_source UNIQUE (id, source_id);

        ALTER TABLE ingest.source_runs
            ADD COLUMN source_config_version_id uuid,
            ADD COLUMN payload_schema_version text NOT NULL DEFAULT 'rss.v1',
            ADD COLUMN snapshot_sha256 char(64),
            ADD CONSTRAINT fk_source_run_config_same_source
                FOREIGN KEY (source_config_version_id, source_id)
                REFERENCES ingest.source_config_versions(id, source_id),
            ALTER COLUMN source_config_version_id SET NOT NULL,
            ADD CONSTRAINT ck_source_run_snapshot_sha256
                CHECK (snapshot_sha256 IS NULL OR snapshot_sha256 ~ '^[0-9a-f]{64}$');

        ALTER TABLE ingest.sources
            ADD COLUMN minimum_request_interval_seconds integer NOT NULL DEFAULT 0
                CHECK (minimum_request_interval_seconds >= 0),
            ADD COLUMN cooldown_seconds integer NOT NULL DEFAULT 300
                CHECK (cooldown_seconds >= 0),
            ADD COLUMN failure_threshold integer NOT NULL DEFAULT 3
                CHECK (failure_threshold > 0),
            ADD COLUMN last_requested_at timestamptz,
            ADD COLUMN last_success_at timestamptz,
            ADD COLUMN consecutive_failures integer NOT NULL DEFAULT 0
                CHECK (consecutive_failures >= 0),
            ADD COLUMN cooldown_until timestamptz;

        CREATE INDEX ix_sources_cooldown_until
            ON ingest.sources (cooldown_until)
            WHERE cooldown_until IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        SET ROLE uap_owner;
        DROP INDEX IF EXISTS ingest.ix_sources_cooldown_until;
        ALTER TABLE ingest.sources
            DROP COLUMN cooldown_until,
            DROP COLUMN consecutive_failures,
            DROP COLUMN last_success_at,
            DROP COLUMN last_requested_at,
            DROP COLUMN failure_threshold,
            DROP COLUMN cooldown_seconds,
            DROP COLUMN minimum_request_interval_seconds;
        ALTER TABLE ingest.source_runs
            DROP CONSTRAINT fk_source_run_config_same_source,
            DROP CONSTRAINT ck_source_run_snapshot_sha256,
            DROP COLUMN snapshot_sha256,
            DROP COLUMN payload_schema_version,
            DROP COLUMN source_config_version_id;
        ALTER TABLE ingest.source_config_versions
            DROP CONSTRAINT uq_source_config_versions_id_source;
        """
    )
