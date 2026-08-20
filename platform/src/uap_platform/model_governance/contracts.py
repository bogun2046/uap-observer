"""Versioned contracts for AI task execution and model governance."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

MODEL_PAYLOAD_SCHEMA_VERSION = "model.v1"
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
    idempotency_key: str
    payload_schema_version: str = MODEL_PAYLOAD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in ("provider", "model", "idempotency_key"):
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
            "idempotency_key": self.idempotency_key,
            "payload_schema_version": self.payload_schema_version,
        }


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    structured: Mapping[str, object]
    raw_response: bytes
    provider_response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_minor_units: int | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        if not self.raw_response:
            raise ValueError("provider response cannot be empty")
        for field_name in ("input_tokens", "output_tokens", "cost_minor_units"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative")
        if self.currency is not None and len(self.currency) != 3:
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
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_minor_units: int | None = None,
        currency: str | None = None,
    ) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = " ".join(summary.split())[:240] or "model provider failure"
        self.http_status = http_status
        self.retryable = retryable
        self.raw_response = raw_response
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_minor_units = cost_minor_units
        self.currency = currency


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

    def __post_init__(self) -> None:
        if self.status is ModelRunStatus.SUCCEEDED:
            if self.validation_status is not ValidationStatus.VALID or not self.result:
                raise ValueError("successful model execution requires a valid result")
            if self.error_code is not None:
                raise ValueError("successful model execution cannot contain an error")
        elif not self.error_code or not self.error_summary:
            raise ValueError("failed or invalid model execution requires an error")
