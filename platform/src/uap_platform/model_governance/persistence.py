"""PostgreSQL and object-store persistence for model governance."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from psycopg import Connection

from uap_platform.object_registry import (
    ObjectClient,
    RegisteredObject,
    StorageDomain,
    cleanup_unregistered_object,
    read_verified_object,
    store_and_register,
)

from .contracts import (
    ModelExecution,
    ModelRunStatus,
    ModelTaskType,
    PromptVersion,
    json_sha256,
)

LOGGER = logging.getLogger(__name__)


class _PersistenceWriteError(RuntimeError):
    def __init__(self, registered: tuple[RegisteredObject, ...], cause: Exception) -> None:
        super().__init__(str(cause))
        self.registered = registered
        self.cause = cause


class PostgresModelGovernanceStore:
    """Persist model calls and results without overwriting historical outputs."""

    def __init__(self, connection: Connection[object], object_client: ObjectClient) -> None:
        self._connection = connection
        self._object_client = object_client

    def create_prompt_version(
        self,
        prompt: PromptVersion,
        created_by: uuid.UUID,
    ) -> uuid.UUID:
        """Register one immutable Prompt version and return its stable ID."""

        content_sha256 = json_sha256(
            {
                "task_type": prompt.task_type.value,
                "version": prompt.version,
                "system_template": prompt.system_template,
                "user_template": prompt.user_template,
                "output_schema": dict(prompt.output_schema),
            }
        )
        if content_sha256 != prompt.content_sha256:
            raise ValueError("prompt content hash does not match its canonical content")
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ops.prompt_versions (
                    id, task_type, version, system_template, user_template,
                    output_schema, content_sha256, active, created_by
                ) VALUES (%s, %s::ops.model_task_type, %s, %s, %s, %s::jsonb, %s, %s, %s)
                RETURNING id
                """,
                (
                    prompt.id,
                    prompt.task_type.value,
                    prompt.version,
                    prompt.system_template,
                    prompt.user_template,
                    json.dumps(dict(prompt.output_schema), sort_keys=True),
                    prompt.content_sha256,
                    prompt.active,
                    created_by,
                ),
            )
            row = cast(tuple[uuid.UUID] | None, cursor.fetchone())
        if row is None:
            raise RuntimeError("prompt version insert returned no id")
        self._connection.commit()
        return uuid.UUID(str(row[0]))

    def load_prompt(
        self,
        prompt_version_id: uuid.UUID,
        task_type: ModelTaskType,
    ) -> PromptVersion:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, task_type, version, system_template, user_template,
                       output_schema, content_sha256, active
                  FROM ops.prompt_versions
                 WHERE id = %s AND task_type = %s::ops.model_task_type AND active
                """,
                (prompt_version_id, task_type.value),
            )
            row = cast(tuple[Any, ...] | None, cursor.fetchone())
        if row is None:
            raise ValueError("prompt version is missing, inactive, or has the wrong task type")
        return PromptVersion(
            id=uuid.UUID(str(row[0])),
            task_type=ModelTaskType(str(row[1])),
            version=str(row[2]),
            system_template=str(row[3]),
            user_template=str(row[4]),
            output_schema=cast(dict[str, object], row[5]),
            content_sha256=str(row[6]),
            active=bool(row[7]),
        )

    def load_document_input(self, document_version_id: uuid.UUID) -> str:
        """Read the latest successful derived extraction, never a public projection."""

        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT so.bucket_name, so.object_key, so.content_sha256, so.byte_length
                  FROM core.extractions AS e
                  JOIN core.stored_objects AS so
                    ON so.id = e.text_object_id
                   AND so.storage_domain = 'derived'::core.storage_domain
                 WHERE e.document_version_id = %s
                   AND e.outcome = 'succeeded'::core.extraction_outcome
                 ORDER BY e.created_at DESC, e.id DESC
                 LIMIT 1
                """,
                (document_version_id,),
            )
            row = cast(tuple[str, str, str, int] | None, cursor.fetchone())
        if row is None:
            raise LookupError("document version has no successful extracted text")
        data = read_verified_object(self._object_client, row[0], row[1], row[2], int(row[3]))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError("derived extraction is not valid UTF-8") from error
        if not text.strip():
            raise LookupError("document version has empty extracted text")
        return text

    def existing_model_run(self, idempotency_key: str) -> tuple[uuid.UUID, ModelRunStatus] | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, status FROM ops.model_runs WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            row = cast(tuple[Any, ...] | None, cursor.fetchone())
        if row is None:
            return None
        return uuid.UUID(str(row[0])), ModelRunStatus(str(row[1]))

    def persist_and_finish_job(
        self,
        job_id: uuid.UUID,
        job_attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        execution: ModelExecution,
    ) -> uuid.UUID:
        """Write model governance data and close its WP4 task in one transaction."""

        registered: list[RegisteredObject] = []
        try:
            model_run_id, registered = self._persist_uncommitted(execution)
            outcome, http_status, error_code, summary, retry_delay = self._job_result(execution)
            self._finish_job(
                job_id,
                job_attempt_id,
                lease_token,
                outcome,
                http_status,
                error_code,
                summary,
                retry_delay,
            )
            self._connection.commit()
            return model_run_id
        except _PersistenceWriteError as error:
            self._connection.rollback()
            self._cleanup_after_rollback(error.registered)
            raise error.cause from error
        except Exception:
            self._connection.rollback()
            self._cleanup_after_rollback(tuple(registered))
            raise

    def finish_job_only(
        self,
        job_id: uuid.UUID,
        job_attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        *,
        http_status: int,
        error_code: str,
        error_summary: str,
    ) -> None:
        """Close a malformed task without invoking a Provider."""

        try:
            self._finish_job(
                job_id,
                job_attempt_id,
                lease_token,
                "terminal_failure",
                http_status,
                error_code,
                error_summary,
                None,
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def finish_existing_job(
        self,
        job_id: uuid.UUID,
        job_attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
    ) -> None:
        """Close a duplicate task successfully without invoking a Provider."""

        try:
            self._finish_job(
                job_id,
                job_attempt_id,
                lease_token,
                "succeeded",
                None,
                None,
                None,
                None,
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _persist_uncommitted(
        self, execution: ModelExecution
    ) -> tuple[uuid.UUID, list[RegisteredObject]]:
        registered: list[RegisteredObject] = []
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"model-run:{execution.request.idempotency_key}",),
                )
                cursor.execute(
                    """
                    SELECT id
                      FROM ops.model_runs
                     WHERE idempotency_key = %s
                     FOR SHARE
                    """,
                    (execution.request.idempotency_key,),
                )
                existing = cast(tuple[Any, ...] | None, cursor.fetchone())
            if existing is not None:
                return uuid.UUID(str(existing[0])), registered

            request_object = store_and_register(
                self._object_client,
                self._connection,
                StorageDomain.MODEL_IO,
                json.dumps(
                    execution.request.safe_record(), sort_keys=True, separators=(",", ":")
                ).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            registered.append(request_object)

            response_object_id: uuid.UUID | None = None
            response = execution.response
            if response is not None:
                response_object = store_and_register(
                    self._object_client,
                    self._connection,
                    StorageDomain.MODEL_IO,
                    response.raw_response,
                    "application/json; charset=utf-8",
                )
                registered.append(response_object)
                response_object_id = response_object.id

            now = datetime.now(UTC)
            status = execution.status.value
            response_id = response.provider_response_id if response is not None else None
            input_tokens = response.input_tokens if response is not None else None
            output_tokens = response.output_tokens if response is not None else None
            cost_minor_units = response.cost_minor_units if response is not None else None
            currency = response.currency if response is not None else None
            result_payload: Mapping[str, object] = execution.result
            with self._connection.cursor() as cursor:
                cursor.execute(
                """
                INSERT INTO ops.model_runs (
                    id, job_attempt_id, prompt_version_id, document_version_id,
                    task_type, provider, model, input_sha256, idempotency_key,
                    request_object_id, response_object_id, storage_domain,
                    provider_response_id, status, input_tokens, output_tokens,
                    cost_minor_units, currency, error_code, started_at, finished_at
                ) VALUES (
                    %s, %s, %s, %s, %s::ops.model_task_type, %s, %s, %s, %s,
                    %s, %s, 'model_io'::core.storage_domain, %s,
                    %s::ops.model_run_status, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id
                """,
                    (
                        uuid.uuid4(),
                        execution.request.job_attempt_id,
                        execution.request.prompt_version_id,
                        execution.request.document_version_id,
                        execution.request.task_type.value,
                        execution.request.provider,
                        execution.request.model,
                        execution.request.input_sha256,
                        execution.request.idempotency_key,
                        request_object.id,
                        response_object_id,
                        response_id,
                        status,
                        input_tokens,
                        output_tokens,
                        cost_minor_units,
                        currency,
                        execution.error_code,
                        now,
                        now,
                    ),
                )
                row = cast(tuple[uuid.UUID] | None, cursor.fetchone())
                if row is None:
                    raise RuntimeError("model run idempotency conflict")
                model_run_id = uuid.UUID(str(row[0]))

                if execution.status in {ModelRunStatus.SUCCEEDED, ModelRunStatus.INVALID}:
                    if execution.status is ModelRunStatus.INVALID:
                        result_payload = {"validation_errors": list(execution.validation_errors)}
                    cursor.execute(
                        """
                        INSERT INTO core.analysis_results (
                            id, model_run_id, document_version_id, result_type,
                            schema_version, result, result_sha256, validation_status,
                            validation_errors
                        ) VALUES (
                            %s, %s, %s, %s::ops.model_task_type, %s, %s::jsonb, %s,
                            %s::core.validation_status, %s::jsonb
                        )
                        ON CONFLICT (model_run_id, result_type) DO NOTHING
                        """,
                        (
                            uuid.uuid4(),
                            model_run_id,
                            execution.request.document_version_id,
                            execution.request.task_type.value,
                            "ai.v1",
                            json.dumps(dict(result_payload), sort_keys=True),
                            json_sha256(result_payload),
                            execution.validation_status.value,
                            json.dumps(list(execution.validation_errors), sort_keys=True),
                        ),
                    )
            return model_run_id, registered
        except Exception as error:
            raise _PersistenceWriteError(tuple(registered), error) from error

    def _finish_job(
        self,
        job_id: uuid.UUID,
        job_attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        outcome: str,
        http_status: int | None,
        error_code: str | None,
        error_summary: str | None,
        retry_delay: int | None,
    ) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ops.finish_job(
                    %s, %s, %s, %s::ops.attempt_outcome,
                    %s, %s, %s, %s
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
                    retry_delay,
                ),
            )
            if cursor.fetchone() is None:
                raise RuntimeError("finish_job did not return a status")

    def _cleanup_after_rollback(self, registered: tuple[RegisteredObject, ...]) -> None:
        for item in registered:
            if not item.created:
                continue
            try:
                cleanup_unregistered_object(self._connection, self._object_client, item)
                self._connection.commit()
            except Exception as cleanup_error:
                self._connection.rollback()
                LOGGER.warning("model-io object cleanup deferred: %s", cleanup_error)

    @staticmethod
    def _job_result(
        execution: ModelExecution,
    ) -> tuple[str, int | None, str | None, str | None, int | None]:
        if execution.status is ModelRunStatus.SUCCEEDED:
            return "succeeded", None, None, None, None
        if execution.retryable:
            return (
                "retryable_failure",
                execution.http_status,
                execution.error_code,
                execution.error_summary,
                60,
            )
        return (
            "terminal_failure",
            execution.http_status or 422,
            execution.error_code,
            execution.error_summary,
            None,
        )
