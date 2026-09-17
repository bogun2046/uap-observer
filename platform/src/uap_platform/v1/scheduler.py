"""Cron-safe V1 scheduler that only enqueues controlled durable jobs."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg import Connection

from .config import V1Config, load_v1_config


def schedule_window(now: datetime, minutes: int) -> datetime:
    observed = now.astimezone(UTC)
    minute = observed.minute - observed.minute % minutes
    return observed.replace(minute=minute, second=0, microsecond=0)


def enqueue_due(connection: Connection[Any], config: V1Config, now: datetime) -> tuple[str, ...]:
    """Enqueue the single approved V1-1 RSS source for one deterministic time window."""

    source = config.v1_1_source
    window = schedule_window(now, config.schedule_interval_minutes)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT s.id, scv.id, scv.configuration
              FROM ingest.sources AS s
              JOIN ingest.source_config_versions AS scv
                ON scv.source_id = s.id AND scv.effective_to IS NULL
             WHERE s.slug = %s AND s.enabled
            """,
            (source.slug,),
        )
        row = cast(tuple[object, object, dict[str, object]] | None, cursor.fetchone())
        if row is None:
            raise RuntimeError("V1-1 source is not bootstrapped or is disabled")
        source_id, config_version_id, stored = row
        if stored.get("fetch_url") != source.fetch_url or stored.get("v1_1_enabled") is not True:
            raise RuntimeError("stored source configuration does not match approved V1-1 config")
        idempotency_key = f"v1-fetch:{source_id}:{window.isoformat()}"
        payload = {
            "source_id": str(source_id),
            "source_config_version_id": str(config_version_id),
            "source_type": source.source_type,
            "source_url": source.fetch_url,
            "run_key": idempotency_key,
            "payload_schema_version": "v1.fetch.v1",
        }
        cursor.execute(
            """
            SELECT ops.enqueue_job(
                'fetch_source', %s::jsonb, 'v1.fetch.v1', %s,
                0::smallint, %s, 3, 120
            )
            """,
            (json.dumps(payload, sort_keys=True), idempotency_key, window),
        )
        job_id = str(cast(tuple[object], cursor.fetchone())[0])
    connection.commit()
    return (job_id,)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--at", help="ISO-8601 scheduler time; defaults to now")
    args = parser.parse_args()
    database_url = os.environ.get("UAP_V1_SCHEDULER_DATABASE_URL")
    if not database_url:
        raise SystemExit("UAP_V1_SCHEDULER_DATABASE_URL is required")
    observed_at = datetime.fromisoformat(args.at) if args.at else datetime.now(UTC)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    config = load_v1_config(args.config)
    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as connection:
        jobs = enqueue_due(connection, config, observed_at)
    print(
        json.dumps(
            {
                "enqueued_job_ids": jobs,
                "next_window_after": str(
                    timedelta(minutes=config.schedule_interval_minutes)
                ),
            }
        )
    )
