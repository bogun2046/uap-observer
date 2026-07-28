"""Structured, source-grounded AI analysis for extracted UAP articles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from uap_observer.models import AnalysisRiskFlag, AnalysisTask, FactStatus, NewsCategory
from uap_observer.repositories import Repository

ANALYSIS_VERSION = "uap-analysis-v1"
DEFAULT_MAX_CONTENT_CHARACTERS = 40_000

ANALYSIS_INSTRUCTIONS = """
You organize public-source UAP reporting for a neutral research database.
Analyze only the supplied article and metadata. Do not add outside facts.

Rules:
- Write the title, summary, key facts, and viewpoints in Simplified Chinese.
- Clearly attribute claims to their source and preserve uncertainty.
- A document or statement being official proves that it exists; it does not
  automatically prove every claim contained in it.
- Never infer extraterrestrial origin, government concealment, or military
  conclusions that the article does not establish.
- Use fact_status=official_record only for the existence/content of an official
  record; use source_reported for attributed reporting, unverified for unsupported
  claims, disputed for materially conflicting claims, and opinion for commentary.
- key_facts must be directly supported by the supplied text.
- viewpoints should capture materially different attributed interpretations;
  return an empty list if none are present.
- named entities must appear in the supplied text. Do not resolve identities using
  outside knowledge.
- confidence measures confidence that this extraction accurately represents the
  supplied article, not confidence that extraordinary claims are true.
""".strip()


class ArticleAnalysis(BaseModel):
    """Strict schema persisted as the canonical model output."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    chinese_title: str = Field(min_length=4, max_length=160)
    chinese_summary: str = Field(min_length=20, max_length=1200)
    category: NewsCategory
    fact_status: FactStatus
    key_facts: list[str] = Field(min_length=1, max_length=8)
    viewpoints: list[str] = Field(default_factory=list, max_length=6)
    named_persons: list[str] = Field(default_factory=list, max_length=20)
    named_organizations: list[str] = Field(default_factory=list, max_length=20)
    related_events: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0.0, le=1.0)
    risk_flags: list[AnalysisRiskFlag] = Field(default_factory=list, max_length=5)

    @field_validator(
        "key_facts",
        "viewpoints",
        "named_persons",
        "named_organizations",
        "related_events",
    )
    @classmethod
    def validate_string_lists(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("list items must not be blank")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("list items must be unique")
        return cleaned


@dataclass(frozen=True)
class AnalyzerResult:
    analysis: ArticleAnalysis
    model: str
    response_id: str | None = None


class Analyzer(Protocol):
    def analyze(self, task: AnalysisTask) -> AnalyzerResult:
        """Return a validated structured analysis."""


class OpenAIAnalyzer:
    """Official OpenAI Responses API adapter using non-streaming structured output."""

    def __init__(
        self,
        *,
        model: str,
        reasoning_effort: str = "low",
        client: object | None = None,
        max_content_characters: int = DEFAULT_MAX_CONTENT_CHARACTERS,
    ) -> None:
        if max_content_characters < 1_000:
            raise ValueError("max_content_characters must be at least 1000")
        self.client = client
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_content_characters = max_content_characters

    def analyze(self, task: AnalysisTask) -> AnalyzerResult:
        if self.client is None:
            from openai import OpenAI

            self.client = OpenAI()
        content = task.extracted_content[: self.max_content_characters]
        payload = {
            "original_title": task.original_title,
            "source": task.source,
            "source_url": task.source_url,
            "publish_date": task.publish_date,
            "content_truncated": len(task.extracted_content) > len(content),
            "article_text": content,
        }
        response = self.client.responses.parse(
            model=self.model,
            instructions=ANALYSIS_INSTRUCTIONS,
            input=json.dumps(payload, ensure_ascii=False),
            text_format=ArticleAnalysis,
            reasoning={"effort": self.reasoning_effort},
            store=False,
        )
        analysis = response.output_parsed
        if analysis is None:
            raise RuntimeError("OpenAI response did not contain parsed analysis")
        return AnalyzerResult(
            analysis=analysis,
            model=getattr(response, "model", None) or self.model,
            response_id=getattr(response, "id", None),
        )


@dataclass(frozen=True)
class AnalysisRun:
    stale_recovered: int = 0
    queued: int = 0
    claimed: int = 0
    completed: int = 0
    failed: int = 0


class AnalysisService:
    def __init__(self, repository: Repository, analyzer: Analyzer) -> None:
        self.repository = repository
        self.analyzer = analyzer

    def run(
        self,
        *,
        limit: int = 10,
        retry_failed: bool = False,
        stale_after_minutes: int = 60,
    ) -> AnalysisRun:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        stale_recovered = self.repository.reset_stale_analysis_tasks(
            stale_after_minutes=stale_after_minutes
        )
        tasks = self.repository.get_analysis_tasks(
            limit=limit,
            retry_failed=retry_failed,
        )
        claimed = completed = failed = 0
        for task in tasks:
            if not self.repository.claim_analysis_task(
                task.news_id,
                retry_failed=retry_failed,
            ):
                continue
            claimed += 1
            try:
                result = self.analyzer.analyze(task)
                analysis = result.analysis
                self.repository.complete_analysis(
                    task.news_id,
                    title=analysis.chinese_title,
                    summary=analysis.chinese_summary,
                    category=analysis.category,
                    fact_status=analysis.fact_status,
                    key_facts=analysis.key_facts,
                    viewpoints=analysis.viewpoints,
                    model=result.model,
                    response_id=result.response_id,
                    analysis_version=ANALYSIS_VERSION,
                    confidence=analysis.confidence,
                    risk_flags=analysis.risk_flags,
                    analysis_json=analysis.model_dump_json(),
                )
                completed += 1
            # Each article is an isolated queue job; provider, validation, and
            # persistence failures must not stop the remaining batch.
            except Exception as error:  # noqa: BLE001
                self.repository.fail_analysis(task.news_id, _safe_error(error))
                failed += 1
        return AnalysisRun(
            stale_recovered=stale_recovered,
            queued=len(tasks),
            claimed=claimed,
            completed=completed,
            failed=failed,
        )


def _safe_error(error: Exception) -> str:
    """Keep failure diagnostics bounded without persisting article content."""

    return f"{type(error).__name__}: {error}"[:1000]
