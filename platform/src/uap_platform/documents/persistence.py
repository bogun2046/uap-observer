"""PostgreSQL persistence for append-only document extraction results."""

from __future__ import annotations

import json
import logging
import uuid
from typing import cast

from psycopg import Connection

from uap_platform.object_registry import (
    ObjectClient,
    RegisteredObject,
    StorageDomain,
    cleanup_unregistered_object,
    read_verified_object,
    store_and_register,
)

from .contracts import ExtractionInput, ExtractionOutcome, ExtractionResult, Extractor

LOGGER = logging.getLogger(__name__)


class _PersistenceWriteError(RuntimeError):
    """Carry a newly created object through a failed database write."""

    def __init__(self, registered: RegisteredObject | None, cause: Exception) -> None:
        super().__init__(str(cause))
        self.registered = registered
        self.cause = cause


class PostgresExtractionStore:
    """Read the G5 raw object and append one G6 extraction row atomically."""

    def __init__(self, connection: Connection[object], object_client: ObjectClient) -> None:
        self._connection = connection
        self._object_client = object_client

    def load_raw(self, request: ExtractionInput) -> bytes:
        """Load only the raw object belonging to the requested document version."""

        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT so.bucket_name, so.object_key, so.content_sha256,
                       so.byte_length, so.media_type
                  FROM core.document_versions AS dv
                  JOIN ingest.artifact_versions AS av
                    ON av.id = dv.artifact_version_id
                   AND av.storage_domain = 'raw'::core.storage_domain
                  JOIN core.stored_objects AS so
                    ON so.id = av.stored_object_id
                   AND so.storage_domain = 'raw'::core.storage_domain
                 WHERE dv.id = %s AND so.id = %s
                """,
                (request.document_version_id, request.source_object_id),
            )
            row = cast(tuple[str, str, str, int, str] | None, cursor.fetchone())
        if row is None:
            raise LookupError("document version is not linked to the requested raw object")
        bucket_name, object_name, content_sha256, byte_length, stored_media_type = row
        requested_media_type = request.media_type.split(";", 1)[0].strip().casefold()
        if requested_media_type != stored_media_type.split(";", 1)[0].strip().casefold():
            raise LookupError(
                "document raw object media type does not match the extraction request"
            )
        return read_verified_object(
            self._object_client,
            bucket_name,
            object_name,
            content_sha256,
            int(byte_length),
        )

    def persist(self, job_attempt_id: uuid.UUID, result: ExtractionResult) -> uuid.UUID:
        """Append a result and commit its derived object in one transaction."""

        registered: RegisteredObject | None = None
        try:
            extraction_id, registered = self._persist_uncommitted(job_attempt_id, result)
            self._connection.commit()
            return extraction_id
        except _PersistenceWriteError as error:
            self._connection.rollback()
            self._cleanup_after_rollback(error.registered)
            raise error.cause from error
        except Exception:
            self._connection.rollback()
            self._cleanup_after_rollback(registered)
            raise

    def persist_and_finish_job(
        self,
        job_id: uuid.UUID,
        job_attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        result: ExtractionResult,
    ) -> uuid.UUID:
        """Commit extraction and WP4 job completion as one transaction."""

        registered: RegisteredObject | None = None
        try:
            extraction_id, registered = self._persist_uncommitted(job_attempt_id, result)
            outcome, http_status, error_code, error_summary = self._job_result(result)
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
                        job_attempt_id,
                        lease_token,
                        outcome,
                        http_status,
                        error_code,
                        error_summary,
                    ),
                )
                if cursor.fetchone() is None:
                    raise RuntimeError("finish_job did not return a status")
            self._connection.commit()
            return extraction_id
        except _PersistenceWriteError as error:
            self._connection.rollback()
            self._cleanup_after_rollback(error.registered)
            raise error.cause from error
        except Exception:
            self._connection.rollback()
            self._cleanup_after_rollback(registered)
            raise

    def _persist_uncommitted(
        self, job_attempt_id: uuid.UUID, result: ExtractionResult
    ) -> tuple[uuid.UUID, RegisteredObject | None]:
        registered: RegisteredObject | None = None
        text_object_id: uuid.UUID | None = None
        if result.outcome is ExtractionOutcome.SUCCEEDED:
            registered = store_and_register(
                self._object_client,
                self._connection,
                StorageDomain.DERIVED,
                result.text.encode("utf-8"),
                "text/plain; charset=utf-8",
                expected_sha256=result.output_sha256,
            )
            text_object_id = registered.id

        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO core.extractions (
                        id, document_version_id, job_attempt_id,
                        extractor_name, extractor_version, outcome,
                        text_object_id, storage_domain, output_sha256,
                        title, author, language_code, source_date,
                        location_map, error_code
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s::core.extraction_outcome,
                        %s, 'derived'::core.storage_domain, %s,
                        %s, %s, %s, %s, %s::jsonb, %s
                    )
                    ON CONFLICT (
                        document_version_id, extractor_name,
                        extractor_version, output_sha256
                    ) DO NOTHING
                    RETURNING id
                    """,
                    (
                        uuid.uuid4(),
                        result.request.document_version_id,
                        job_attempt_id,
                        result.request.extractor_name,
                        result.request.extractor_version,
                        result.outcome.value,
                        text_object_id,
                        result.output_sha256,
                        result.title,
                        result.author,
                        result.language_code,
                        result.source_date,
                        json.dumps(list(result.location_map), sort_keys=True),
                        result.error_code,
                    ),
                )
                row = cast(tuple[uuid.UUID] | None, cursor.fetchone())
                if row is None:
                    cursor.execute(
                        """
                        SELECT id
                          FROM core.extractions
                         WHERE document_version_id = %s
                           AND extractor_name = %s
                           AND extractor_version = %s
                           AND output_sha256 = %s
                        """,
                        (
                            result.request.document_version_id,
                            result.request.extractor_name,
                            result.request.extractor_version,
                            result.output_sha256,
                        ),
                    )
                    row = cast(tuple[uuid.UUID] | None, cursor.fetchone())
                if row is None:
                    raise RuntimeError("extraction upsert did not return an id")
        except Exception as error:
            raise _PersistenceWriteError(registered, error) from error
        return uuid.UUID(str(row[0])), registered

    def _cleanup_after_rollback(self, registered: RegisteredObject | None) -> None:
        if registered is None or not registered.created:
            return
        try:
            cleanup_unregistered_object(self._connection, self._object_client, registered)
            self._connection.commit()
        except Exception as cleanup_error:
            self._connection.rollback()
            LOGGER.warning("derived object cleanup deferred: %s", cleanup_error)

    @staticmethod
    def _job_result(
        result: ExtractionResult,
    ) -> tuple[str, int | None, str | None, str | None]:
        if result.outcome is ExtractionOutcome.SUCCEEDED:
            return "succeeded", None, None, None
        if result.error_code == "storage_read_failed":
            return "retryable_failure", 503, result.error_code, result.error_summary
        return "terminal_failure", 422, result.error_code, result.error_summary

    def run(
        self,
        job_attempt_id: uuid.UUID,
        request: ExtractionInput,
        extractor: Extractor,
    ) -> tuple[uuid.UUID, ExtractionResult]:
        """Execute an extractor against its linked raw object and persist the result."""

        payload = self.load_raw(request)
        result = extractor.extract(request, payload)
        return self.persist(job_attempt_id, result), result

    def run_and_finish_job(
        self,
        job_id: uuid.UUID,
        job_attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        request: ExtractionInput,
        extractor: Extractor,
    ) -> tuple[uuid.UUID, ExtractionResult]:
        """Run extraction and close the durable job in one transaction."""

        try:
            payload = self.load_raw(request)
            result = extractor.extract(request, payload)
        except LookupError as error:
            self._connection.rollback()
            result = ExtractionResult(
                request=request,
                outcome=ExtractionOutcome.FAILED,
                error_code="input_not_found",
                error_summary=_safe_summary(str(error)),
            )
        except RuntimeError as error:
            self._connection.rollback()
            result = ExtractionResult(
                request=request,
                outcome=ExtractionOutcome.FAILED,
                error_code="storage_read_failed",
                error_summary=_safe_summary(str(error)),
            )
        except Exception as error:
            self._connection.rollback()
            result = ExtractionResult(
                request=request,
                outcome=ExtractionOutcome.FAILED,
                error_code="extractor_error",
                error_summary=_safe_summary(str(error)),
            )
        extraction_id = self.persist_and_finish_job(
            job_id,
            job_attempt_id,
            lease_token,
            result,
        )
        return extraction_id, result


def _safe_summary(value: str) -> str:
    """Keep extraction errors auditable without allowing multiline/raw payload logs."""

    return " ".join(value.split())[:240] or "extraction failed"
