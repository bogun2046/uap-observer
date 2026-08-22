"""Parse frozen knowledge.v2 job payload before mapping or materialize."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from .contracts import AnchorStatus, ExtractionAnchor
from .reasons import KNOWLEDGE_PAYLOAD_MISMATCH, KNOWLEDGE_SCHEMA_UNSUPPORTED

REQUIRED_KEYS = (
    "payload_schema_version",
    "analysis_result_id",
    "analysis_result_sha256",
    "analysis_schema_version",
    "document_version_id",
    "result_type",
    "model_run_id",
    "input_sha256",
    "extraction_anchor_status",
    "extraction_id",
)


class KnowledgePayloadError(ValueError):
    """Fail-closed payload problem with a frozen reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class FrozenKnowledgePayload:
    analysis_result_id: uuid.UUID
    analysis_result_sha256: str
    analysis_schema_version: str
    document_version_id: uuid.UUID
    result_type: str
    model_run_id: uuid.UUID
    input_sha256: str
    anchor: ExtractionAnchor


def _require_str(payload: Mapping[str, object], key: str) -> str:
    if key not in payload:
        raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
    value = payload[key]
    if not isinstance(value, str) or value == "":
        raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
    return value


def _require_uuid(payload: Mapping[str, object], key: str) -> uuid.UUID:
    raw = _require_str(payload, key)
    try:
        return uuid.UUID(raw)
    except ValueError as error:
        raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH) from error


def parse_knowledge_payload(payload: Mapping[str, object] | object) -> FrozenKnowledgePayload:
    """Reject missing keys, illegal UUIDs, and non-knowledge.v2 schemas with frozen codes."""

    if not isinstance(payload, Mapping):
        raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
    for key in REQUIRED_KEYS:
        if key not in payload:
            raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
    schema = payload.get("payload_schema_version")
    if schema != "knowledge.v2":
        raise KnowledgePayloadError(KNOWLEDGE_SCHEMA_UNSUPPORTED)
    analysis_schema = payload.get("analysis_schema_version")
    if analysis_schema != "ai.v1":
        raise KnowledgePayloadError(KNOWLEDGE_SCHEMA_UNSUPPORTED)
    result_type = _require_str(payload, "result_type")
    if result_type != "claim_extraction":
        raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
    status_raw = payload.get("extraction_anchor_status")
    try:
        status = AnchorStatus(str(status_raw))
    except ValueError as error:
        raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH) from error
    extraction_raw = payload.get("extraction_id")
    extraction_id: uuid.UUID | None
    if extraction_raw is None:
        extraction_id = None
    elif isinstance(extraction_raw, str) and extraction_raw != "":
        try:
            extraction_id = uuid.UUID(extraction_raw)
        except ValueError as error:
            raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH) from error
    else:
        raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
    if (status is AnchorStatus.MATCHED) != (extraction_id is not None):
        raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
    input_sha256 = _require_str(payload, "input_sha256")
    analysis_sha = _require_str(payload, "analysis_result_sha256")
    if len(input_sha256) != 64 or len(analysis_sha) != 64:
        raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
    return FrozenKnowledgePayload(
        analysis_result_id=_require_uuid(payload, "analysis_result_id"),
        analysis_result_sha256=analysis_sha,
        analysis_schema_version="ai.v1",
        document_version_id=_require_uuid(payload, "document_version_id"),
        result_type=result_type,
        model_run_id=_require_uuid(payload, "model_run_id"),
        input_sha256=input_sha256,
        anchor=ExtractionAnchor(status=status, extraction_id=extraction_id),
    )
