"""Versioned contracts for AI task execution and model governance."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

MODEL_PAYLOAD_SCHEMA_VERSION = "model.v1"
MODEL_MAX_INPUT_BYTES = 1_000_000
MODEL_MAX_OUTPUT_BYTES = 2_000_000
MODEL_MAX_ERROR_BYTES = 64_000
MODEL_MAX_CALLS_PER_SEMANTIC_KEY = 3
MODEL_MAX_COST_MINOR_UNITS = 10_000
MODEL_DEFAULT_CURRENCY = "USD"
MODEL_PROVIDER_TIMEOUT_SECONDS = 30.0
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ModelTaskType(StrEnum):
    TRANSLATION = "translation"
    SUMMARY = "summary"
    CLASSIFICATION = "classification"
    ENTITY_EXTRACTION = "entity_extraction"
    CLAIM_EXTRACTION = "claim_extraction"


class ModelRunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INVALID = "invalid"


class ValidationStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Mapping[str, object] | Sequence[object]) -> bytes:
    """Serialize structured model data deterministically for hashing/storage."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def json_sha256(value: Mapping[str, object] | Sequence[object]) -> str:
    return sha256_bytes(canonical_json(value))


def semantic_idempotency_key(
    *,
    document_version_id: uuid.UUID,
    input_sha256: str,
    task_type: ModelTaskType,
    prompt_version_id: uuid.UUID,
    provider: str,
    model: str,
    payload_schema_version: str,
) -> str:
    """Build the stable identity of one semantic model request."""

    return "model-semantic:" + json_sha256(
        {
            "document_version_id": str(document_version_id),
            "input_sha256": input_sha256,
            "task_type": task_type.value,
            "prompt_version_id": str(prompt_version_id),
            "provider": provider,
            "model": model,
            "payload_schema_version": payload_schema_version,
        }
    )


@dataclass(frozen=True, slots=True)
class PromptVersion:
    id: uuid.UUID
    task_type: ModelTaskType
    version: str
    system_template: str
    user_template: str
    output_schema: Mapping[str, object]
    content_sha256: str
    active: bool

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("prompt version is required")
        if not _HEX_SHA256.fullmatch(self.content_sha256):
            raise ValueError("prompt content hash must be a lowercase SHA-256 value")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    document_version_id: uuid.UUID
    job_attempt_id: uuid.UUID
    task_type: ModelTaskType
    prompt_version_id: uuid.UUID
    provider: str
    model: str
    input_text: str
    input_sha256: str
    semantic_idempotency_key: str
    idempotency_key: str
    payload_schema_version: str = MODEL_PAYLOAD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "provider",
            "model",
            "semantic_idempotency_key",
            "idempotency_key",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} is required")
        if not self.input_text.strip():
            raise ValueError("model input text is required")
        if not _HEX_SHA256.fullmatch(self.input_sha256):
            raise ValueError("input_sha256 must be a lowercase SHA-256 value")
        if self.payload_schema_version != MODEL_PAYLOAD_SCHEMA_VERSION:
            raise ValueError("unsupported model payload schema version")

    def safe_record(self) -> dict[str, object]:
        """Return metadata safe for model-io storage; never include source text."""

        return {
            "document_version_id": str(self.document_version_id),
            "job_attempt_id": str(self.job_attempt_id),
            "task_type": self.task_type.value,
            "prompt_version_id": str(self.prompt_version_id),
            "provider": self.provider,
            "model": self.model,
            "input_sha256": self.input_sha256,
            "semantic_idempotency_key": self.semantic_idempotency_key,
            "idempotency_key": self.idempotency_key,
            "payload_schema_version": self.payload_schema_version,
        }


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    structured: Mapping[str, object]
    raw_response: bytes
    provider_response_id: str
    input_tokens: int
    output_tokens: int
    cost_minor_units: int
    currency: str

    def __post_init__(self) -> None:
        if not self.raw_response:
            raise ValueError("provider response cannot be empty")
        if not self.provider_response_id.strip():
            raise ValueError("provider response id is required")
        for field_name in ("input_tokens", "output_tokens", "cost_minor_units"):
            value = getattr(self, field_name)
            if value < 0:
                raise ValueError(f"{field_name} cannot be negative")
        if len(self.currency) != 3 or not self.currency.isalpha() or not self.currency.isupper():
            raise ValueError("currency must be an ISO-like three-letter code")


class ProviderError(RuntimeError):
    """A sanitized Provider failure that can be mapped to WP4 retry semantics."""

    def __init__(
        self,
        code: str,
        summary: str,
        *,
        http_status: int | None = None,
        retryable: bool = False,
        raw_response: bytes | None = None,
        provider_response_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_minor_units: int | None = None,
        currency: str | None = None,
    ) -> None:
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        self.raw_response = (
            raw_response[:MODEL_MAX_ERROR_BYTES] if raw_response is not None else None
        )
        self.provider_response_id = provider_response_id or f"error:{code}:{http_status or 0}"
        self.input_tokens = 0 if input_tokens is None else input_tokens
        self.output_tokens = 0 if output_tokens is None else output_tokens
        self.cost_minor_units = 0 if cost_minor_units is None else cost_minor_units
        self.currency = currency or MODEL_DEFAULT_CURRENCY
        if http_status in (401, 403):
            self.summary = "model provider authentication failed"
        elif http_status == 429:
            self.summary = "model provider rate limited"
        elif http_status in (408, 504) or code in {"timeout", "deadline_exceeded"}:
            self.summary = "model provider timed out"
        elif http_status is not None and http_status >= 500:
            self.summary = "model provider upstream failure"
        else:
            self.summary = "model provider request failed"
        super().__init__(self.summary)


class ModelProvider(Protocol):
    name: str

    def complete(self, request: ModelRequest, prompt: PromptVersion) -> ProviderResponse: ...


@dataclass(frozen=True, slots=True)
class ModelExecution:
    request: ModelRequest
    status: ModelRunStatus
    validation_status: ValidationStatus
    result: Mapping[str, object] = field(default_factory=dict)
    validation_errors: Sequence[Mapping[str, object]] = field(default_factory=tuple)
    response: ProviderResponse | None = None
    error_code: str | None = None
    error_summary: str | None = None
    http_status: int | None = None
    retryable: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.status is ModelRunStatus.SUCCEEDED:
            if self.validation_status is not ValidationStatus.VALID or not self.result:
                raise ValueError("successful model execution requires a valid result")
            if self.error_code is not None:
                raise ValueError("successful model execution cannot contain an error")
        elif not self.error_code or not self.error_summary:
            raise ValueError("failed or invalid model execution requires an error")
