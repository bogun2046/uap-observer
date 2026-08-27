"""Validate and execute versioned AI jobs through the WP4 state machine."""

from __future__ import annotations

import multiprocessing as mp
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from .contracts import (
    MODEL_MAX_CALLS_PER_SEMANTIC_KEY,
    MODEL_MAX_COST_MINOR_UNITS,
    MODEL_MAX_ERROR_BYTES,
    MODEL_MAX_INPUT_BYTES,
    MODEL_MAX_OUTPUT_BYTES,
    MODEL_PAYLOAD_SCHEMA_VERSION,
    MODEL_PROVIDER_TIMEOUT_SECONDS,
    ModelExecution,
    ModelRequest,
    ModelRunStatus,
    ModelTaskType,
    ProviderError,
    ProviderResponse,
    ValidationStatus,
    semantic_idempotency_key,
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
        semantic_idempotency_key=semantic_idempotency_key(
            document_version_id=_required_uuid(payload, "document_version_id"),
            input_sha256=sha256_bytes(input_text.encode("utf-8")),
            task_type=task_type,
            prompt_version_id=_required_uuid(payload, "prompt_version_id"),
            provider=_required_string(payload, "provider"),
            model=_required_string(payload, "model"),
            payload_schema_version=schema_version,
        ),
        idempotency_key=f"model-attempt:{job_attempt_id}",
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
            if len(request.input_text.encode("utf-8")) > MODEL_MAX_INPUT_BYTES:
                return self._persist_governance_failure(
                    job_id,
                    lease_token,
                    request,
                    code="model_input_too_large",
                    summary="model input exceeds the configured limit",
                    http_status=413,
                    started_at=datetime.now(UTC),
                )
            existing = self._store.acquire_semantic_request(request.semantic_idempotency_key)
            if existing is not None:
                self._store.finish_existing_job(job_id, job_attempt_id, lease_token)
                return existing[0]
            if self._store.model_call_count(request.semantic_idempotency_key) >= (
                MODEL_MAX_CALLS_PER_SEMANTIC_KEY
            ):
                return self._persist_governance_failure(
                    job_id,
                    lease_token,
                    request,
                    code="model_call_budget_exceeded",
                    summary="model call budget exhausted",
                    http_status=422,
                    started_at=datetime.now(UTC),
                )
            if (
                self._store.accumulated_cost_minor_units(request.semantic_idempotency_key)
                >= MODEL_MAX_COST_MINOR_UNITS
            ):
                return self._persist_governance_failure(
                    job_id,
                    lease_token,
                    request,
                    code="model_cost_budget_exceeded",
                    summary="model cost budget exhausted",
                    http_status=422,
                    started_at=datetime.now(UTC),
                )
            provider = self._providers.get(request.provider)
            started_at = datetime.now(UTC)
            response = _invoke_provider(provider, request, prompt)
        except ProviderError as error:
            finished_at = datetime.now(UTC)
            execution = self._provider_failure(request, error, started_at, finished_at)
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
                response=_synthetic_response("provider_error"),
                error_code="provider_error",
                error_summary="model provider failed",
                http_status=503,
                retryable=True,
                started_at=started_at if "started_at" in locals() else datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
            return self._store.persist_and_finish_job(
                job_id, job_attempt_id, lease_token, execution
            )

        finished_at = datetime.now(UTC)
        if len(response.raw_response) > MODEL_MAX_OUTPUT_BYTES:
            execution = ModelExecution(
                request=request,
                status=ModelRunStatus.FAILED,
                validation_status=ValidationStatus.INVALID,
                response=_bounded_response(response),
                error_code="model_output_too_large",
                error_summary="model output exceeds the configured limit",
                http_status=413,
                started_at=started_at,
                finished_at=finished_at,
            )
            return self._store.persist_and_finish_job(
                job_id, job_attempt_id, lease_token, execution
            )
        if (
            self._store.accumulated_cost_minor_units(request.semantic_idempotency_key)
            + response.cost_minor_units
            > MODEL_MAX_COST_MINOR_UNITS
        ):
            execution = ModelExecution(
                request=request,
                status=ModelRunStatus.FAILED,
                validation_status=ValidationStatus.INVALID,
                response=response,
                error_code="model_cost_budget_exceeded",
                error_summary="model cost budget exceeded",
                http_status=422,
                started_at=started_at,
                finished_at=finished_at,
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
                started_at=started_at,
                finished_at=finished_at,
            )
        else:
            execution = ModelExecution(
                request=request,
                status=ModelRunStatus.SUCCEEDED,
                validation_status=ValidationStatus.VALID,
                result=result,
                response=response,
                started_at=started_at,
                finished_at=finished_at,
            )
        return self._store.persist_and_finish_job(
            job_id, job_attempt_id, lease_token, execution
        )

    @staticmethod
    def _provider_failure(
        request: ModelRequest,
        error: ProviderError,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> ModelExecution:
        started_at = started_at or datetime.now(UTC)
        finished_at = finished_at or datetime.now(UTC)
        response = ProviderResponse(
            structured={},
            raw_response=error.raw_response or b"{}",
            provider_response_id=error.provider_response_id,
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
            retryable=_retryable_http_status(error.http_status, error.code),
            started_at=started_at,
            finished_at=finished_at,
        )

    def _persist_governance_failure(
        self,
        job_id: uuid.UUID,
        lease_token: uuid.UUID,
        request: ModelRequest,
        *,
        code: str,
        summary: str,
        http_status: int,
        started_at: datetime,
    ) -> uuid.UUID:
        return self._store.persist_and_finish_job(
            job_id,
            request.job_attempt_id,
            lease_token,
            ModelExecution(
                request=request,
                status=ModelRunStatus.FAILED,
                validation_status=ValidationStatus.INVALID,
                response=_synthetic_response(code),
                error_code=code,
                error_summary=summary,
                http_status=http_status,
                started_at=started_at,
                finished_at=datetime.now(UTC),
            ),
        )


def payload_from_claim(claim: tuple[Any, ...]) -> Mapping[str, object]:
    """Extract the JSON payload from a WP4 claim row."""

    if len(claim) < 5 or not isinstance(claim[3], Mapping):
        raise ValueError("model job claim does not contain a JSON object payload")
    return claim[3]


def _invoke_provider(provider: Any, request: ModelRequest, prompt: Any) -> ProviderResponse:
    """Invoke a provider in a killable process with a hard upper bound."""

    methods = mp.get_all_start_methods()
    context: Any = mp.get_context("fork" if "fork" in methods else "spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_provider_process_entry,
        args=(provider, request, prompt, child),
        daemon=True,
    )
    process.start()
    child.close()
    try:
        if not parent.poll(MODEL_PROVIDER_TIMEOUT_SECONDS):
            raise ProviderError("timeout", "provider call timed out", http_status=504)
        kind, payload = parent.recv()
        if kind == "response":
            return cast(ProviderResponse, payload)
        if kind == "provider_error":
            raise _provider_error_from_payload(cast(dict[str, object], payload))
        raise ProviderError("provider_error", "provider call failed", http_status=503)
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=1)


def _provider_process_entry(provider: Any, request: ModelRequest, prompt: Any, pipe: Any) -> None:
    try:
        pipe.send(("response", provider.complete(request, prompt)))
    except ProviderError as error:
        pipe.send(
            (
                "provider_error",
                {
                    "code": error.code,
                    "http_status": error.http_status,
                    "retryable": error.retryable,
                    "raw_response": (
                        error.raw_response[:MODEL_MAX_ERROR_BYTES]
                        if error.raw_response is not None
                        else None
                    ),
                    "provider_response_id": error.provider_response_id,
                    "input_tokens": error.input_tokens,
                    "output_tokens": error.output_tokens,
                    "cost_minor_units": error.cost_minor_units,
                    "currency": error.currency,
                },
            )
        )
    except BaseException:
        pipe.send(("provider_error", {"code": "provider_error", "http_status": 503}))
    finally:
        pipe.close()


def _provider_error_from_payload(payload: dict[str, object]) -> ProviderError:
    return ProviderError(
        str(payload.get("code", "provider_error")),
        "provider call failed",
        http_status=cast(int | None, payload.get("http_status")),
        retryable=bool(payload.get("retryable", False)),
        raw_response=cast(bytes | None, payload.get("raw_response")),
        provider_response_id=cast(str | None, payload.get("provider_response_id")),
        input_tokens=cast(int | None, payload.get("input_tokens")),
        output_tokens=cast(int | None, payload.get("output_tokens")),
        cost_minor_units=cast(int | None, payload.get("cost_minor_units")),
        currency=cast(str | None, payload.get("currency")),
    )


def _bounded_response(response: ProviderResponse) -> ProviderResponse:
    return ProviderResponse(
        structured={},
        raw_response=b"{}",
        provider_response_id=response.provider_response_id,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cost_minor_units=response.cost_minor_units,
        currency=response.currency,
    )


def _synthetic_response(code: str) -> ProviderResponse:
    return ProviderResponse(
        structured={},
        raw_response=b"{}",
        provider_response_id=f"not-called:{code}",
        input_tokens=0,
        output_tokens=0,
        cost_minor_units=0,
        currency="USD",
    )


def _retryable_http_status(http_status: int | None, error_code: str) -> bool:
    if http_status in (401, 403):
        return False
    return http_status in (408, 429, 504) or (http_status is not None and http_status >= 500) or (
        error_code in {"timeout", "deadline_exceeded", "connection_reset"}
    )


def _safe_summary(value: str) -> str:
    del value
    return "model task input rejected"
