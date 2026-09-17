"""Strict, file-backed V1 source configuration."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: uuid.UUID
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,80}$")
    name: str = Field(min_length=1, max_length=200)
    source_type: Literal["rss", "web", "api"]
    homepage_url: str
    fetch_url: str
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    language_code: str | None = Field(default=None, min_length=2, max_length=16)
    enabled: bool
    v1_1_enabled: bool
    minimum_request_interval_seconds: int = Field(ge=0, le=86_400)
    configuration: Mapping[str, object]

    @field_validator("homepage_url", "fetch_url")
    @classmethod
    def validate_https_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("source URLs must be absolute HTTPS URLs without user information")
        return value

    @model_validator(mode="after")
    def validate_rollout(self) -> SourceSpec:
        if self.v1_1_enabled and (not self.enabled or self.source_type != "rss"):
            raise ValueError("V1-1 sources must be enabled RSS/Atom sources")
        return self


class V1Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["v1.sources.v1"]
    schedule_interval_minutes: int = Field(ge=5, le=1_440)
    deepseek_monthly_budget_cny: Decimal = Field(gt=0)
    deepseek_budget_warning_ratio_provisional: Decimal = Field(gt=0, lt=1)
    sources: tuple[SourceSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_and_v1_1_scope(self) -> V1Config:
        if len({item.id for item in self.sources}) != len(self.sources):
            raise ValueError("source ids must be unique")
        if len({item.slug for item in self.sources}) != len(self.sources):
            raise ValueError("source slugs must be unique")
        if sum(item.v1_1_enabled for item in self.sources) != 1:
            raise ValueError("V1-1 must enable exactly one source")
        if self.deepseek_monthly_budget_cny != Decimal("20.00"):
            raise ValueError("the approved V1 monthly budget is CNY 20.00")
        if self.deepseek_budget_warning_ratio_provisional != Decimal("0.80"):
            raise ValueError("the provisional V1 budget warning ratio is 0.80")
        return self

    @property
    def v1_1_source(self) -> SourceSpec:
        return next(item for item in self.sources if item.v1_1_enabled)


def load_v1_config(path: Path | None = None) -> V1Config:
    """Load the controlled source file; no source URL is embedded in collector code."""

    if path is None:
        raw = files("uap_platform.v1").joinpath("default_sources.json").read_text("utf-8")
    else:
        raw = path.read_text("utf-8")
    return V1Config.model_validate(json.loads(raw))
