"""Strict, versioned schemas for model-produced structured results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import ModelTaskType


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceLocator(StrictModel):
    locator_type: Literal["text", "html", "pdf", "video", "audio"]
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    time_start_ms: int | None = Field(default=None, ge=0)
    time_end_ms: int | None = Field(default=None, ge=0)


class TranslationOutput(StrictModel):
    language_code: str = Field(min_length=2, max_length=16)
    text: str = Field(min_length=1, max_length=200_000)


class SummaryOutput(StrictModel):
    summary: str = Field(min_length=1, max_length=20_000)
    bullets: list[str] = Field(min_length=1, max_length=20)


class ClassificationOutput(StrictModel):
    labels: list[str] = Field(min_length=1, max_length=50)
    confidence: float | None = Field(default=None, ge=0, le=1)


class EntityCandidate(StrictModel):
    name: str = Field(min_length=1, max_length=500)
    entity_type: Literal["person", "organization", "location", "event", "object", "concept"]
    evidence: list[EvidenceLocator] = Field(min_length=1, max_length=20)


class EntityExtractionOutput(StrictModel):
    entities: list[EntityCandidate] = Field(max_length=200)


class ClaimCandidate(StrictModel):
    claim: str = Field(min_length=1, max_length=10_000)
    evidence: list[EvidenceLocator] = Field(min_length=1, max_length=20)


class ClaimExtractionOutput(StrictModel):
    claims: list[ClaimCandidate] = Field(max_length=200)


SCHEMA_VERSION = "extract.ai.v1"

_MODELS: dict[ModelTaskType, type[StrictModel]] = {
    ModelTaskType.TRANSLATION: TranslationOutput,
    ModelTaskType.SUMMARY: SummaryOutput,
    ModelTaskType.CLASSIFICATION: ClassificationOutput,
    ModelTaskType.ENTITY_EXTRACTION: EntityExtractionOutput,
    ModelTaskType.CLAIM_EXTRACTION: ClaimExtractionOutput,
}


def schema_for(task_type: ModelTaskType) -> type[StrictModel]:
    return _MODELS[task_type]


def validate_output(
    task_type: ModelTaskType, value: Mapping[str, object]
) -> tuple[dict[str, object] | None, tuple[dict[str, object], ...]]:
    """Validate provider JSON and return JSON-safe data or safe error locations."""

    try:
        parsed = schema_for(task_type).model_validate(value)
    except ValidationError as error:
        errors: tuple[dict[str, object], ...] = tuple(
            {
                "type": str(item.get("type", "validation_error")),
                "location": ".".join(str(part) for part in item.get("loc", ())),
            }
            for item in error.errors()
        )
        return None, errors
    return parsed.model_dump(mode="json"), ()


def schema_json_schema(task_type: ModelTaskType) -> dict[str, object]:
    return schema_for(task_type).model_json_schema()
