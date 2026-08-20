"""Validate and execute versioned AI jobs through the WP4 state machine."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from .contracts import (
    MODEL_PAYLOAD_SCHEMA_VERSION,
    ModelExecution,
    ModelRequest,
    ModelRunStatus,
    ModelTaskType,
    ProviderError,
    ProviderResponse,
    ValidationStatus,
    sha256_bytes,
)
from .persistence import PostgresModelGovernanceStore
from .providers import ProviderRegistry
from .schemas import validate_output

_ALLOWED_FIELDS = frozenset(
    {
        "document_version_id",
        "prompt_version_id",
        "task_type",
        "provider",
        "model",
        "payload_schema_version",
    }
)


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"model payload field {key} is required")
    return value.strip()


def _required_uuid(payload: Mapping[str, object], key: str) -> uuid.UUID:
    value = _required_string(payload, key)
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise ValueError(f"model payload field {key} is not a UUID") from error


def build_model_request(
    payload: Mapping[str, object],
    *,
    job_id: uuid.UUID,
    job_attempt_id: uuid.UUID,
    input_text: str,
) -> ModelRequest:
    unknown = set(payload) - _ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"model payload contains unknown fields: {sorted(unknown)}")
    schema_version = _required_string(payload, "payload_schema_version")
    if schema_version != MODEL_PAYLOAD_SCHEMA_VERSION:
        raise ValueError("unsupported model payload schema version")
    try:
        task_type = ModelTaskType(_required_string(payload, "task_type"))
    except ValueError as error:
        raise ValueError("unsupported model task type") from error
    return ModelRequest(
        document_version_id=_required_uuid(payload, "document_version_id"),
        job_attempt_id=job_attempt_id,
        task_type=task_type,
        prompt_version_id=_required_uuid(payload, "prompt_version_id"),
        provider=_required_string(payload, "provider"),
        model=_required_string(payload, "model"),
        input_text=input_text,
        input_sha256=sha256_bytes(input_text.encode("utf-8")),
        idempotency_key=f"model-job:{job_id}",
        payload_schema_version=schema_version,
    )


class ModelJobHandler:
    """Run one claimed model task without exposing raw Provider data in logs."""

    def __init__(
        self,
        store: PostgresModelGovernanceStore,
        providers: ProviderRegistry,
    ) -> None:
        self._store = store
        self._providers = providers

    def handle(
        self,
        job_id: uuid.UUID,
        job_attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        payload: Mapping[str, object],
    ) -> uuid.UUID:
        try:
            document_version_id = _required_uuid(payload, "document_version_id")
            input_text = self._store.load_document_input(document_version_id)
            request = build_model_request(
                payload,
                job_id=job_id,
                job_attempt_id=job_attempt_id,
                input_text=input_text,
            )
            prompt = self._store.load_prompt(request.prompt_version_id, request.task_type)
            existing = self._store.existing_model_run(request.idempotency_key)
            if existing is not None:
                self._store.finish_existing_job(job_id, job_attempt_id, lease_token)
                return existing[0]
            provider = self._providers.get(request.provider)
            response = provider.complete(request, prompt)
        except ProviderError as error:
            execution = self._provider_failure(request, error)
            return self._store.persist_and_finish_job(
                job_id, job_attempt_id, lease_token, execution
            )
        except (LookupError, ValueError) as error:
            self._store.finish_job_only(
                job_id,
                job_attempt_id,
                lease_token,
                http_status=422,
                error_code="invalid_model_input",
                error_summary=_safe_summary(str(error)),
            )
            raise
        except Exception as error:
            if "request" not in locals():
                self._store.finish_job_only(
                    job_id,
                    job_attempt_id,
                    lease_token,
                    http_status=500,
                    error_code="model_dispatch_failed",
                    error_summary="model dispatch failed",
                )
                raise RuntimeError("model dispatch failed") from error
            execution = ModelExecution(
                request=request,
                status=ModelRunStatus.FAILED,
                validation_status=ValidationStatus.INVALID,
                error_code="provider_error",
                error_summary="model provider failed",
                http_status=503,
                retryable=True,
            )
            return self._store.persist_and_finish_job(
                job_id, job_attempt_id, lease_token, execution
            )

        result, validation_errors = validate_output(request.task_type, response.structured)
        if result is None:
            execution = ModelExecution(
                request=request,
                status=ModelRunStatus.INVALID,
                validation_status=ValidationStatus.INVALID,
                validation_errors=validation_errors,
                response=response,
                error_code="schema_validation_failed",
                error_summary=f"provider output failed {len(validation_errors)} schema checks",
                http_status=422,
            )
        else:
            execution = ModelExecution(
                request=request,
                status=ModelRunStatus.SUCCEEDED,
                validation_status=ValidationStatus.VALID,
                result=result,
                response=response,
            )
        return self._store.persist_and_finish_job(
            job_id, job_attempt_id, lease_token, execution
        )

    @staticmethod
    def _provider_failure(request: ModelRequest, error: ProviderError) -> ModelExecution:
        response: ProviderResponse | None = None
        if error.raw_response:
            response = ProviderResponse(
                structured={},
                raw_response=error.raw_response,
                input_tokens=error.input_tokens,
                output_tokens=error.output_tokens,
                cost_minor_units=error.cost_minor_units,
                currency=error.currency,
            )
        return ModelExecution(
            request=request,
            status=ModelRunStatus.FAILED,
            validation_status=ValidationStatus.INVALID,
            response=response,
            error_code=error.code,
            error_summary=error.summary,
            http_status=error.http_status,
            retryable=error.retryable,
        )


def payload_from_claim(claim: tuple[Any, ...]) -> Mapping[str, object]:
    """Extract the JSON payload from a WP4 claim row."""

    if len(claim) < 5 or not isinstance(claim[3], Mapping):
        raise ValueError("model job claim does not contain a JSON object payload")
    return claim[3]


def _safe_summary(value: str) -> str:
    return " ".join(value.split())[:240] or "model task failed"
