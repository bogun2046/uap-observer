"""PostgreSQL persistence for source runs and normalized RSS items."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any, cast

from psycopg import Connection

from uap_platform.object_registry import (
    ObjectClient,
    RegisteredObject,
    StorageDomain,
    cleanup_unregistered_object,
    sha256_bytes,
    store_and_register,
)

from .contracts import CollectionResult, FetchClassification, NormalizedItem
from .policy import SourceCoolingDown, SourcePolicy

LOGGER = logging.getLogger(__name__)


class PostgresSourceRunStore:
    """Persist source runs with an explicit start/finish transaction boundary."""

    def __init__(self, connection: Connection[object], object_client: ObjectClient) -> None:
        self._connection = connection
        self._object_client = object_client
        self._new_objects: list[RegisteredObject] = []
        self._run_sources: dict[uuid.UUID, uuid.UUID] = {}

    def start_source_run(
        self,
        source_id: uuid.UUID,
        job_id: uuid.UUID,
        run_key: str,
        started_at: datetime,
        source_config_version_id: uuid.UUID,
    ) -> uuid.UUID:
        """Checkpoint the run row before business writes can be rolled back."""

        run_id = uuid.uuid4()
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ingest.source_runs (
                    id, source_id, source_config_version_id, job_id, run_key,
                    outcome, payload_schema_version, started_at
                ) VALUES (
                    %s, %s, %s, %s, %s, 'failed'::ingest.source_run_outcome,
                    'rss.v1', %s
                )
                ON CONFLICT (job_id) DO UPDATE
                   SET run_key = EXCLUDED.run_key,
                       outcome = 'failed'::ingest.source_run_outcome,
                       http_status = NULL,
                       fetched_count = 0,
                       parsed_count = 0,
                       persisted_count = 0,
                       duplicate_count = 0,
                       invalid_count = 0,
                       etag = NULL,
                       last_modified = NULL,
                       error_code = NULL,
                       error_summary = NULL,
                       snapshot_sha256 = NULL,
                       started_at = EXCLUDED.started_at,
                       finished_at = NULL
                 WHERE ingest.source_runs.source_id = EXCLUDED.source_id
                   AND ingest.source_runs.source_config_version_id =
                       EXCLUDED.source_config_version_id
                RETURNING id
                """,
                (run_id, source_id, source_config_version_id, job_id, run_key, started_at),
            )
            row = cast(tuple[uuid.UUID] | None, cursor.fetchone())
        if row is None:
            self._connection.rollback()
            raise RuntimeError("source run provenance does not match the existing job")
        run_id = uuid.UUID(str(row[0]))
        self._connection.commit()
        self._run_sources[run_id] = source_id
        return run_id

    def reserve_source_request(
        self, source_id: uuid.UUID, policy: SourcePolicy, now: datetime
    ) -> float:
        """Atomically reserve a source request or return seconds to wait."""

        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT last_requested_at, cooldown_until,
                       minimum_request_interval_seconds
                  FROM ingest.sources
                 WHERE id = %s
                 FOR UPDATE
                """,
                (source_id,),
            )
            row = cast(
                tuple[datetime | None, datetime | None, int] | None,
                cursor.fetchone(),
            )
            if row is None:
                raise RuntimeError("source does not exist")
            last_requested_at, cooldown_until, configured_interval_seconds = row
            if cooldown_until is not None and cooldown_until > now:
                self._connection.rollback()
                raise SourceCoolingDown(source_id, cooldown_until)
            if last_requested_at is not None:
                interval = max(
                    policy.minimum_request_interval,
                    timedelta(seconds=int(configured_interval_seconds)),
                )
                due_at = last_requested_at + interval
                if due_at > now:
                    self._connection.rollback()
                    return (due_at - now).total_seconds()
            cursor.execute(
                "UPDATE ingest.sources SET last_requested_at = %s, updated_at = %s WHERE id = %s",
                (now, now, source_id),
            )
        self._connection.commit()
        return 0.0

    def persist_items(
        self,
        source_id: uuid.UUID,
        source_run_id: uuid.UUID,
        items: Iterable[NormalizedItem],
        seen_at: datetime,
    ) -> int:
        """Persist raw objects, artifact versions, documents and document versions."""

        created_count = 0
        with self._connection.cursor() as cursor:
            for item in items:
                artifact_id = self._upsert_artifact(cursor, source_id, item, seen_at)
                registered = store_and_register(
                    self._object_client,
                    self._connection,
                    StorageDomain.RAW,
                    item.raw_payload,
                    "application/xml",
                )
                if registered.created:
                    self._new_objects.append(registered)
                artifact_version_id = self._upsert_artifact_version(
                    cursor,
                    artifact_id,
                    source_run_id,
                    registered.id,
                    item,
                    seen_at,
                )
                document_id, document_created = self._upsert_document(
                    cursor, source_id, item, seen_at
                )
                self._upsert_document_version(
                    cursor,
                    document_id,
                    artifact_version_id,
                    item,
                    seen_at,
                )
                created_count += int(document_created)
        return created_count

    def finish_source_run(
        self, run_id: uuid.UUID, result: CollectionResult, finished_at: datetime
    ) -> None:
        self._update_source_run(run_id, result, finished_at)
        self._connection.commit()
        self._new_objects.clear()

    def finish_source_run_and_job(
        self,
        run_id: uuid.UUID,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        result: CollectionResult,
        finished_at: datetime,
    ) -> None:
        """Commit source-run completion and lease completion atomically."""

        try:
            self._update_source_run(run_id, result, finished_at)
            self._finish_job(job_id, attempt_id, lease_token, result)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        self._new_objects.clear()

    def fail_source_run(
        self, run_id: uuid.UUID, result: CollectionResult, finished_at: datetime
    ) -> None:
        """Rollback business writes, then durably record failure on the checkpoint row."""

        self._connection.rollback()
        pending_cleanup = tuple(self._new_objects)
        self._new_objects.clear()
        for registered in pending_cleanup:
            try:
                cleanup_unregistered_object(self._connection, self._object_client, registered)
            except Exception as error:
                # The failed run must still be durably recorded. The scheduled
                # object-storage consistency scan can retry cleanup later.
                LOGGER.warning("object cleanup deferred: %s", error)
        self._update_source_run(run_id, result, finished_at)
        self._connection.commit()

    def fail_source_run_and_job(
        self,
        run_id: uuid.UUID,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        result: CollectionResult,
        finished_at: datetime,
    ) -> None:
        """Rollback business writes, then atomically record run and job outcome."""

        self._connection.rollback()
        pending_cleanup = tuple(self._new_objects)
        self._new_objects.clear()
        for registered in pending_cleanup:
            try:
                cleanup_unregistered_object(self._connection, self._object_client, registered)
            except Exception as error:
                LOGGER.warning("object cleanup deferred: %s", error)
        try:
            self._update_source_run(run_id, result, finished_at)
            self._finish_job(job_id, attempt_id, lease_token, result)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def finish_job(
        self,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        result: CollectionResult,
    ) -> None:
        """Close the WP4 lease using the collector result classification."""

        self._finish_job(job_id, attempt_id, lease_token, result)
        self._connection.commit()

    def _finish_job(
        self,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        result: CollectionResult,
    ) -> None:
        outcome = {
            FetchClassification.SUCCESS: "succeeded",
            FetchClassification.NOT_MODIFIED: "succeeded",
            FetchClassification.EMPTY: "succeeded",
            FetchClassification.TRANSIENT_FAILURE: "retryable_failure",
            FetchClassification.RATE_LIMITED: "retryable_failure",
            FetchClassification.AUTHORIZATION_FAILURE: "terminal_failure",
            FetchClassification.TERMINAL_FAILURE: "terminal_failure",
        }[result.classification]
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ops.finish_job(
                    %s, %s, %s, %s::ops.attempt_outcome,
                    %s, %s, %s, NULL
                )
                """,
                (
                    job_id,
                    attempt_id,
                    lease_token,
                    outcome,
                    result.http_status,
                    result.error_code,
                    result.error_summary,
                ),
            )
            if cursor.fetchone() is None:
                raise RuntimeError("finish_job did not return a status")

    @staticmethod
    def _upsert_artifact(
        cursor: Any, source_id: uuid.UUID, item: NormalizedItem, seen_at: datetime
    ) -> uuid.UUID:
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
            (
                uuid.uuid4(),
                source_id,
                item.canonical_url or f"rss-item:{item.source_item_key}",
                seen_at,
                seen_at,
            ),
        )
        row = cast(tuple[uuid.UUID] | None, cursor.fetchone())
        if row is None:
            raise RuntimeError("artifact upsert did not return an id")
        return uuid.UUID(str(row[0]))

    @staticmethod
    def _upsert_artifact_version(
        cursor: Any,
        artifact_id: uuid.UUID,
        source_run_id: uuid.UUID,
        stored_object_id: uuid.UUID,
        item: NormalizedItem,
        seen_at: datetime,
    ) -> uuid.UUID:
        cursor.execute(
            """
            INSERT INTO ingest.artifact_versions (
                id, artifact_id, source_run_id, stored_object_id,
                storage_domain, retrieved_at, source_published_at, metadata
            ) VALUES (
                %s, %s, %s, %s, 'raw'::core.storage_domain, %s, %s, %s::jsonb
            )
            ON CONFLICT (artifact_id, stored_object_id) DO NOTHING
            RETURNING id
            """,
            (
                uuid.uuid4(),
                artifact_id,
                source_run_id,
                stored_object_id,
                seen_at,
                item.published_at,
                json.dumps(dict(item.metadata), sort_keys=True),
            ),
        )
        row = cast(tuple[uuid.UUID] | None, cursor.fetchone())
        if row is not None:
            return uuid.UUID(str(row[0]))
        cursor.execute(
            """
            SELECT id
              FROM ingest.artifact_versions
             WHERE artifact_id = %s AND stored_object_id = %s
            """,
            (artifact_id, stored_object_id),
        )
        existing = cast(tuple[uuid.UUID] | None, cursor.fetchone())
        if existing is None:
            raise RuntimeError("artifact version upsert did not find the existing row")
        return uuid.UUID(str(existing[0]))

    @staticmethod
    def _upsert_document(
        cursor: Any, source_id: uuid.UUID, item: NormalizedItem, seen_at: datetime
    ) -> tuple[uuid.UUID, bool]:
        if item.canonical_url is not None:
            query = """
            INSERT INTO core.documents (
                id, source_id, source_item_key, canonical_url, document_kind,
                first_seen_at, last_seen_at
            ) VALUES (
                %s, %s, %s, %s, 'article'::core.document_kind, %s, %s
            )
            ON CONFLICT (canonical_url) WHERE canonical_url IS NOT NULL
            DO UPDATE SET last_seen_at = GREATEST(
                core.documents.last_seen_at, EXCLUDED.last_seen_at
            )
            RETURNING id, (xmax = 0) AS inserted
            """
        else:
            query = """
            INSERT INTO core.documents (
                id, source_id, source_item_key, canonical_url, document_kind,
                first_seen_at, last_seen_at
            ) VALUES (
                %s, %s, %s, %s, 'article'::core.document_kind, %s, %s
            )
            ON CONFLICT (source_id, source_item_key)
            WHERE source_item_key IS NOT NULL
            DO UPDATE SET last_seen_at = GREATEST(
                core.documents.last_seen_at, EXCLUDED.last_seen_at
            )
            RETURNING id, (xmax = 0) AS inserted
            """
        cursor.execute(
            query,
            (
                uuid.uuid4(),
                source_id,
                item.source_item_key,
                item.canonical_url,
                seen_at,
                seen_at,
            ),
        )
        row = cast(tuple[uuid.UUID, bool] | None, cursor.fetchone())
        if row is None:
            raise RuntimeError("document upsert did not return an id")
        return uuid.UUID(str(row[0])), bool(row[1])

    @staticmethod
    def _touch_document(cursor: Any, document_id: uuid.UUID, seen_at: datetime) -> None:
        cursor.execute(
            "UPDATE core.documents SET last_seen_at = GREATEST(last_seen_at, %s) WHERE id = %s",
            (seen_at, document_id),
        )

    @staticmethod
    def _upsert_document_version(
        cursor: Any,
        document_id: uuid.UUID,
        artifact_version_id: uuid.UUID,
        item: NormalizedItem,
        seen_at: datetime,
    ) -> None:
        content_hash = sha256_bytes(item.raw_payload)
        cursor.execute(
            """
            SELECT version_no, normalized_content_sha256
              FROM core.document_versions
             WHERE document_id = %s
             ORDER BY version_no DESC
             LIMIT 1
            """,
            (document_id,),
        )
        latest = cast(tuple[int, str] | None, cursor.fetchone())
        if latest is not None and str(latest[1]) == content_hash:
            return
        next_version = 1 if latest is None else int(latest[0]) + 1
        cursor.execute(
            """
            INSERT INTO core.document_versions (
                id, document_id, artifact_version_id, version_no,
                original_title, source_published_at, normalized_content_sha256,
                metadata, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (document_id, normalized_content_sha256) DO NOTHING
            """,
            (
                uuid.uuid4(),
                document_id,
                artifact_version_id,
                next_version,
                item.title,
                item.published_at,
                content_hash,
                json.dumps(dict(item.metadata), sort_keys=True),
                seen_at,
            ),
        )

    def _update_source_run(
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
                       payload_schema_version = %s,
                       snapshot_sha256 = %s,
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
                    result.payload_schema_version,
                    result.snapshot_sha256,
                    finished_at,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("source run update did not affect exactly one row")
            source_id = self._run_sources.get(run_id)
            if source_id is not None:
                successful = result.classification in {
                    FetchClassification.SUCCESS,
                    FetchClassification.NOT_MODIFIED,
                    FetchClassification.EMPTY,
                }
                if successful:
                    cursor.execute(
                        """
                        UPDATE ingest.sources
                           SET last_success_at = %s,
                               consecutive_failures = 0,
                               cooldown_until = NULL,
                               updated_at = %s
                         WHERE id = %s
                        """,
                        (finished_at, finished_at, source_id),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE ingest.sources
                           SET consecutive_failures = consecutive_failures + 1,
                               cooldown_until = CASE
                                   WHEN consecutive_failures + 1 >= failure_threshold
                                   THEN %s + cooldown_seconds * interval '1 second'
                                   ELSE cooldown_until
                               END,
                               updated_at = %s
                         WHERE id = %s
                        """,
                        (finished_at, finished_at, source_id),
                    )
