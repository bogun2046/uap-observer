"""PostgreSQL persistence for source runs and normalized RSS items."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import cast

from psycopg import Connection

from uap_platform.object_registry import uuid7

from .contracts import CollectionResult, FetchClassification, NormalizedItem


class PostgresSourceRunStore:
    """Persist one collector run using the caller-owned PostgreSQL transaction."""

    def __init__(self, connection: Connection[object]) -> None:
        self._connection = connection

    def start_source_run(
        self, source_id: uuid.UUID, job_id: uuid.UUID, run_key: str, started_at: datetime
    ) -> uuid.UUID:
        run_id = uuid7()
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ingest.source_runs (
                    id, source_id, job_id, run_key, outcome, started_at
                ) VALUES (%s, %s, %s, %s, 'failed'::ingest.source_run_outcome, %s)
                """,
                (run_id, source_id, job_id, run_key, started_at),
            )
        return run_id

    def persist_items(
        self,
        source_id: uuid.UUID,
        source_run_id: uuid.UUID,
        items: Iterable[NormalizedItem],
        seen_at: datetime,
    ) -> int:
        """Upsert source artifacts and documents, returning newly created documents."""

        created_count = 0
        with self._connection.cursor() as cursor:
            for item in items:
                locator = item.canonical_url or f"rss-item:{item.source_item_key}"
                cursor.execute(
                    """
                    INSERT INTO ingest.artifacts (
                        id, source_id, canonical_locator, artifact_kind,
                        first_seen_at, last_seen_at
                    ) VALUES (
                        %s, %s, %s, 'rss_item'::ingest.artifact_kind, %s, %s
                    )
                    ON CONFLICT (source_id, canonical_locator) DO UPDATE
                    SET last_seen_at = GREATEST(
                        ingest.artifacts.last_seen_at, EXCLUDED.last_seen_at
                    )
                    RETURNING id
                    """,
                    (uuid7(), source_id, locator, seen_at, seen_at),
                )
                artifact_row = cast(tuple[uuid.UUID] | None, cursor.fetchone())
                if artifact_row is None:
                    raise RuntimeError("artifact upsert did not return an id")

                cursor.execute(
                    """
                    INSERT INTO core.documents (
                        id, source_id, source_item_key, canonical_url, document_kind,
                        first_seen_at, last_seen_at
                    ) VALUES (
                        %s, %s, %s, %s, 'article'::core.document_kind, %s, %s
                    )
                    ON CONFLICT (source_id, source_item_key)
                    WHERE source_item_key IS NOT NULL DO NOTHING
                    RETURNING id
                    """,
                    (
                        uuid7(),
                        source_id,
                        item.source_item_key,
                        item.canonical_url,
                        seen_at,
                        seen_at,
                    ),
                )
                document_row = cast(tuple[uuid.UUID] | None, cursor.fetchone())
                if document_row is not None:
                    created_count += 1
                else:
                    cursor.execute(
                        """
                        UPDATE core.documents
                           SET last_seen_at = GREATEST(last_seen_at, %s)
                         WHERE source_id = %s AND source_item_key = %s
                        RETURNING id
                        """,
                        (seen_at, source_id, item.source_item_key),
                    )
                    if cursor.fetchone() is None:
                        raise RuntimeError("document upsert did not find the existing row")
        return created_count

    def finish_source_run(
        self, run_id: uuid.UUID, result: CollectionResult, finished_at: datetime
    ) -> None:
        outcome = {
            FetchClassification.SUCCESS: "succeeded",
            FetchClassification.NOT_MODIFIED: "not_modified",
            FetchClassification.EMPTY: "empty",
        }.get(result.classification, "failed")
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ingest.source_runs
                   SET outcome = %s::ingest.source_run_outcome,
                       http_status = %s,
                       fetched_count = %s,
                       parsed_count = %s,
                       persisted_count = %s,
                       duplicate_count = %s,
                       invalid_count = %s,
                       etag = %s,
                       last_modified = %s,
                       error_code = %s,
                       error_summary = %s,
                       finished_at = %s
                 WHERE id = %s
                """,
                (
                    outcome,
                    result.http_status,
                    result.fetched_count,
                    result.parsed_count,
                    result.persisted_count,
                    result.duplicate_count,
                    result.invalid_count,
                    result.etag,
                    result.last_modified,
                    result.error_code,
                    result.error_summary,
                    finished_at,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("source run update did not affect exactly one row")
