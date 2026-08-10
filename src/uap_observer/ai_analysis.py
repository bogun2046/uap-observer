"""Structured, source-grounded AI analysis for extracted UAP articles."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from uap_observer.models import AnalysisRiskFlag, AnalysisTask, FactStatus, NewsCategory
from uap_observer.repositories import Repository

ANALYSIS_VERSION = "uap-analysis-v2"
DEFAULT_MAX_CONTENT_CHARACTERS = 40_000
SUPPORTED_PERSON_RELATIONSHIP_TYPES = {
    "supports",
    "questions",
    "criticizes",
    "responds_to",
    "quotes",
    "works_with",
    "investigates",
    "participates_with",
    "affiliated_with",
}

_ResultT = TypeVar("_ResultT")
_ModelT = TypeVar("_ModelT", bound=BaseModel)

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
- topic_tags are short, source-supported labels such as "信息公开" or
  "国会听证"; do not invent a taxonomy or use sensational conclusions.
- person_relationships may only include two names that also appear in
  named_persons. Extract a relationship only when the supplied text explicitly
  describes it. Use an empty list for co-occurrence alone. Include a short
  evidence quote and preserve attribution in the quote.
- Allowed relationship types are supports, questions, criticizes, responds_to,
  quotes, works_with, investigates, participates_with, and affiliated_with.
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
    topic_tags: list[str] = Field(default_factory=list, max_length=12)
    person_relationships: list[PersonRelationshipCandidate] = Field(
        default_factory=list,
        max_length=12,
    )
    confidence: float = Field(ge=0.0, le=1.0)
    risk_flags: list[AnalysisRiskFlag] = Field(default_factory=list, max_length=5)

    @field_validator("chinese_title", "chinese_summary")
    @classmethod
    def validate_chinese_prose(cls, value: str) -> str:
        cleaned = value.strip()
        if not re.search(r"[\u4e00-\u9fff]", cleaned):
            raise ValueError("Chinese title and summary must contain Chinese characters")
        return cleaned

    @field_validator(
        "key_facts",
        "viewpoints",
        "named_persons",
        "named_organizations",
        "related_events",
        "topic_tags",
    )
    @classmethod
    def validate_string_lists(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("list items must not be blank")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("list items must be unique")
        return cleaned

    @field_validator("key_facts", "viewpoints")
    @classmethod
    def validate_chinese_analysis_lists(cls, values: list[str]) -> list[str]:
        if any(not re.search(r"[\u4e00-\u9fff]", value) for value in values):
            raise ValueError("key facts and viewpoints must contain Chinese characters")
        return values


class PersonRelationshipCandidate(BaseModel):
    """A source-grounded, reviewable relationship extracted from one article."""

    model_config = ConfigDict(extra="forbid")

    source_person: str = Field(min_length=1, max_length=160)
    target_person: str = Field(min_length=1, max_length=160)
    relationship_type: str = Field(min_length=1, max_length=40)
    evidence_quote: str = Field(min_length=8, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("source_person", "target_person", "relationship_type", "evidence_quote")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("relationship_type")
    @classmethod
    def validate_relationship_type(cls, value: str) -> str:
        if value not in SUPPORTED_PERSON_RELATIONSHIP_TYPES:
            raise ValueError("unsupported person relationship type")
        return value

    @model_validator(mode="after")
    def distinct_people(self) -> PersonRelationshipCandidate:
        if self.source_person.casefold() == self.target_person.casefold():
            raise ValueError("source and target person must differ")
        return self


ArticleAnalysis.model_rebuild()


class TitleTranslation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chinese_title: str = Field(min_length=2, max_length=320)

    @field_validator("chinese_title")
    @classmethod
    def must_contain_simplified_chinese(cls, value: str) -> str:
        if not re.search(r"[\u4e00-\u9fff]", value):
            raise ValueError("translated title must contain Chinese characters")
        return value.strip()


@dataclass(frozen=True)
class AnalyzerResult:
    analysis: ArticleAnalysis
    model: str
    response_id: str | None = None


@dataclass(frozen=True)
class TitleTranslationResult:
    title: str
    model: str
    response_id: str | None = None


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    model: str
    available_models: int


class ProviderConfigurationError(RuntimeError):
    """A safe-to-display provider configuration failure."""


class ProviderResponseError(RuntimeError):
    """A structured provider response failed safe local validation."""

    def __init__(self, reason: str, *, response_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.response_id = response_id


class Analyzer(Protocol):
    model: str

    def analyze(self, task: AnalysisTask) -> AnalyzerResult:
        """Return a validated structured analysis."""


class OpenAIAnalyzer:
    """Official OpenAI Responses API adapter using non-streaming structured output."""

    provider = "OpenAI"

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

    def translate_title(self, original_title: str, source: str) -> TitleTranslationResult:
        if self.client is None:
            from openai import OpenAI

            self.client = OpenAI()
        response = self.client.responses.parse(
            model=self.model,
            instructions=(
                "将新闻标题翻译成简体中文。只翻译，不补充事实；保留专有名词、缩写和不确定语气。"
            ),
            input=json.dumps({"title": original_title, "source": source}, ensure_ascii=False),
            text_format=TitleTranslation,
            reasoning={"effort": self.reasoning_effort},
            store=False,
        )
        result = response.output_parsed
        if result is None:
            raise RuntimeError("OpenAI title translation returned no content")
        return TitleTranslationResult(
            title=result.chinese_title,
            model=getattr(response, "model", None) or self.model,
            response_id=getattr(response, "id", None),
        )


def _parse_deepseek_json_response(
    response: object,
    *,
    schema: type[_ModelT],
    purpose: str,
) -> _ModelT:
    """Validate JSON output without exposing model content in exceptions."""

    response_id = getattr(response, "id", None)
    try:
        message = response.choices[0].message
        raw_content = getattr(message, "content", None)
    except (AttributeError, IndexError, TypeError) as error:
        raise ProviderResponseError(
            f"{purpose}_missing_content",
            response_id=response_id,
        ) from error
    if not isinstance(raw_content, str) or not raw_content.strip():
        raise ProviderResponseError(
            f"{purpose}_missing_content",
            response_id=response_id,
        )
    try:
        payload = json.loads(raw_content)
    except (json.JSONDecodeError, TypeError) as error:
        raise ProviderResponseError(
            f"{purpose}_invalid_json",
            response_id=response_id,
        ) from error
    try:
        return schema.model_validate(payload)
    except ValidationError as error:
        raise ProviderResponseError(
            f"{purpose}_schema_validation",
            response_id=response_id,
        ) from error


class DeepSeekAnalyzer:
    """OpenAI-compatible DeepSeek Chat Completions adapter with JSON Output."""

    provider = "DeepSeek"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        reasoning_effort: str = "low",
        client: object | None = None,
        max_content_characters: int = DEFAULT_MAX_CONTENT_CHARACTERS,
    ) -> None:
        if max_content_characters < 1_000:
            raise ValueError("max_content_characters must be at least 1000")
        self.client = client
        self.api_key = api_key
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_content_characters = max_content_characters

    def _get_client(self) -> object:
        if self.client is None:
            from openai import OpenAI

            if not self.api_key:
                raise ProviderConfigurationError(
                    "DeepSeek 配置错误：DEEPSEEK_API_KEY 未配置。"
                )
            self.client = OpenAI(api_key=self.api_key, base_url="https://api.deepseek.com")
        return self.client

    def health_check(self) -> ProviderHealth:
        """Validate API authentication, connectivity, and configured model availability."""

        response = self._get_client().models.list()
        models = {
            str(getattr(item, "id", ""))
            for item in getattr(response, "data", [])
            if getattr(item, "id", None)
        }
        if self.model not in models:
            raise ProviderConfigurationError(
                f"DeepSeek 模型不可用：{self.model} 未出现在 /models 返回列表中。"
            )
        return ProviderHealth(
            provider=self.provider,
            model=self.model,
            available_models=len(models),
        )

    def analyze(self, task: AnalysisTask) -> AnalyzerResult:
        client = self._get_client()
        content = task.extracted_content[: self.max_content_characters]
        payload = {
            "original_title": task.original_title,
            "source": task.source,
            "source_url": task.source_url,
            "publish_date": task.publish_date,
            "content_truncated": len(task.extracted_content) > len(content),
            "article_text": content,
        }
        instructions = (
            f"{ANALYSIS_INSTRUCTIONS}\n\n"
            "Return only one valid JSON object matching this schema. Use JSON, not Markdown.\n"
            f"{json.dumps(ArticleAnalysis.model_json_schema(), ensure_ascii=False)}"
        )
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            max_tokens=3000,
            stream=False,
        )
        analysis = _parse_deepseek_json_response(
            response,
            schema=ArticleAnalysis,
            purpose="article",
        )
        return AnalyzerResult(
            analysis=analysis,
            model=getattr(response, "model", None) or self.model,
            response_id=getattr(response, "id", None),
        )

    def translate_title(self, original_title: str, source: str) -> TitleTranslationResult:
        client = self._get_client()
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "将新闻标题翻译成简体中文。只翻译，不补充事实；保留专有名词、缩写和不确定语气。返回 JSON：{\"chinese_title\": \"...\"}。",
                },
                {"role": "user", "content": json.dumps({"title": original_title, "source": source}, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            max_tokens=300,
            stream=False,
        )
        translation = _parse_deepseek_json_response(
            response,
            schema=TitleTranslation,
            purpose="title",
        )
        return TitleTranslationResult(
            title=translation.chinese_title,
            model=getattr(response, "model", None) or self.model,
            response_id=getattr(response, "id", None),
        )


@dataclass(frozen=True)
class ProviderFailure:
    stage: str
    news_id: int
    attempts: int
    error: str
    response_id: str | None = None


@dataclass(frozen=True)
class TitleTranslationRun:
    queued: int = 0
    translated: int = 0
    failed: int = 0
    provider_access_failed: bool = False
    fatal_error: str | None = None
    failures: tuple[ProviderFailure, ...] = ()


@dataclass(frozen=True)
class AnalysisRun:
    stale_recovered: int = 0
    queued: int = 0
    claimed: int = 0
    completed: int = 0
    failed: int = 0
    titles_translated: int = 0
    titles_failed: int = 0
    provider_access_failed: bool = False
    fatal_error: str | None = None
    failures: tuple[ProviderFailure, ...] = ()


class AnalysisService:
    def __init__(
        self,
        repository: Repository,
        analyzer: Analyzer,
        *,
        max_provider_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_provider_attempts < 1:
            raise ValueError("max_provider_attempts must be at least 1")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must not be negative")
        self.repository = repository
        self.analyzer = analyzer
        self.max_provider_attempts = max_provider_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.sleep = sleep

    def _call_provider(
        self,
        operation: Callable[[], _ResultT],
    ) -> tuple[_ResultT | None, int, Exception | None]:
        for attempt in range(1, self.max_provider_attempts + 1):
            try:
                return operation(), attempt, None
            except Exception as error:  # noqa: BLE001
                if (
                    attempt >= self.max_provider_attempts
                    or not is_retryable_provider_error(error)
                ):
                    return None, attempt, error
                delay = self.retry_delay_seconds * (2 ** (attempt - 1))
                if delay:
                    self.sleep(delay)
        raise AssertionError("provider retry loop exhausted without a result")

    def run(
        self,
        *,
        limit: int = 10,
        retry_failed: bool = False,
        stale_after_minutes: int = 60,
        title_translation_limit: int | None = None,
    ) -> AnalysisRun:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        stale_recovered = self.repository.reset_stale_analysis_tasks(
            stale_after_minutes=stale_after_minutes
        )
        # Translate short titles before the slower article-analysis queue. This
        # keeps freshly collected YouTube titles publishable in Chinese even
        # when extraction or analysis is slow.
        title_run = self.translate_titles(
            limit=title_translation_limit if title_translation_limit is not None else limit,
            retry_failed=retry_failed,
        )
        if title_run.provider_access_failed:
            return AnalysisRun(
                stale_recovered=stale_recovered,
                titles_translated=title_run.translated,
                titles_failed=title_run.failed,
                provider_access_failed=True,
                fatal_error=title_run.fatal_error,
                failures=title_run.failures,
            )
        tasks = self.repository.get_analysis_tasks(
            limit=limit,
            retry_failed=retry_failed,
        )
        claimed = completed = failed = 0
        provider_access_failed = False
        fatal_error = None
        failure_details = list(title_run.failures)
        for task in tasks:
            if not self.repository.claim_analysis_task(
                task.news_id,
                retry_failed=retry_failed,
            ):
                continue
            claimed += 1
            result, attempts, provider_error = self._call_provider(
                lambda task=task: self.analyzer.analyze(task)
            )
            if provider_error is not None:
                error_text = safe_provider_error(
                    provider_error,
                    provider=self._provider_name,
                )
                self.repository.fail_analysis(task.news_id, error_text)
                failed += 1
                failure_details.append(
                    ProviderFailure(
                        stage="article_analysis",
                        news_id=task.news_id,
                        attempts=attempts,
                        error=error_text,
                        response_id=provider_response_id(provider_error),
                    )
                )
                if is_provider_access_error(provider_error):
                    provider_access_failed = True
                    fatal_error = error_text
                    break
                continue
            if result is None:
                raise AssertionError("provider call succeeded without a result")
            try:
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
            except Exception as error:  # noqa: BLE001
                error_text = safe_provider_error(error, provider=self._provider_name)
                self.repository.fail_analysis(task.news_id, error_text)
                failed += 1
                failure_details.append(
                    ProviderFailure(
                        stage="article_persistence",
                        news_id=task.news_id,
                        attempts=attempts,
                        error=error_text,
                        response_id=provider_response_id(error),
                    )
                )
        return AnalysisRun(
            stale_recovered=stale_recovered,
            queued=len(tasks),
            claimed=claimed,
            completed=completed,
            failed=failed,
            titles_translated=title_run.translated,
            titles_failed=title_run.failed,
            provider_access_failed=provider_access_failed,
            fatal_error=fatal_error,
            failures=tuple(failure_details),
        )

    @property
    def _provider_name(self) -> str:
        return str(getattr(self.analyzer, "provider", "AI provider"))

    @property
    def _model_name(self) -> str:
        return str(getattr(self.analyzer, "model", "unknown"))

    def translate_titles(
        self,
        *,
        limit: int = 100,
        retry_failed: bool = False,
    ) -> TitleTranslationRun:
        translator = getattr(self.analyzer, "translate_title", None)
        if translator is None or limit == 0:
            return TitleTranslationRun()
        rows = self.repository.get_untranslated_titles(
            limit=limit,
            retry_failed=retry_failed,
        )
        translated = failed = 0
        failure_details: list[ProviderFailure] = []
        for row in rows:
            news_id = int(row["id"])
            if not self.repository.claim_title_translation(
                news_id,
                model=self._model_name,
                retry_failed=retry_failed,
            ):
                continue

            def translate_once(
                original_title: str = row["original_title"],
                source: str = row["source"],
            ) -> TitleTranslationResult:
                result = translator(original_title, source)
                if isinstance(result, TitleTranslationResult):
                    translation = result
                else:
                    translation = TitleTranslationResult(
                        title=str(result),
                        model=self._model_name,
                    )
                if not translation.title.strip():
                    raise RuntimeError("Title translation returned a blank title")
                return translation

            translation, attempts, provider_error = self._call_provider(translate_once)
            if provider_error is not None:
                error_text = safe_provider_error(
                    provider_error,
                    provider=self._provider_name,
                )
                response_id = provider_response_id(provider_error)
                self.repository.fail_title_translation(
                    news_id,
                    error=error_text,
                    model=self._model_name,
                    response_id=response_id,
                )
                failed += 1
                failure_details.append(
                    ProviderFailure(
                        stage="title_translation",
                        news_id=news_id,
                        attempts=attempts,
                        error=error_text,
                        response_id=response_id,
                    )
                )
                if is_provider_access_error(provider_error):
                    return TitleTranslationRun(
                        queued=len(rows),
                        translated=translated,
                        failed=failed,
                        provider_access_failed=True,
                        fatal_error=error_text,
                        failures=tuple(failure_details),
                    )
                continue
            if translation is None:
                raise AssertionError("provider call succeeded without a translation")
            try:
                self.repository.complete_title_translation(
                    news_id,
                    title=translation.title.strip(),
                    model=translation.model,
                    response_id=translation.response_id,
                )
                translated += 1
            except Exception as error:  # noqa: BLE001
                error_text = safe_provider_error(error, provider=self._provider_name)
                self.repository.fail_title_translation(
                    news_id,
                    error=error_text,
                    model=self._model_name,
                    response_id=provider_response_id(error),
                )
                failed += 1
                failure_details.append(
                    ProviderFailure(
                        stage="title_persistence",
                        news_id=news_id,
                        attempts=attempts,
                        error=error_text,
                        response_id=provider_response_id(error),
                    )
                )
        return TitleTranslationRun(
            queued=len(rows),
            translated=translated,
            failed=failed,
            failures=tuple(failure_details),
        )


def provider_status_code(error: Exception) -> int | None:
    """Extract an HTTP status without serializing a provider exception."""

    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def is_provider_access_error(error: Exception) -> bool:
    return provider_status_code(error) in {401, 403}


def is_retryable_provider_error(error: Exception) -> bool:
    """Retry transient transport/server failures and invalid model output."""

    if is_provider_access_error(error) or isinstance(error, ProviderConfigurationError):
        return False
    status = provider_status_code(error)
    if status is not None:
        return status in {408, 409, 425, 429} or status >= 500
    if isinstance(error, (ProviderResponseError, TimeoutError, ConnectionError)):
        return True
    return type(error).__name__ in {
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "RateLimitError",
    }


def provider_response_id(error: Exception) -> str | None:
    """Return a request/response identifier when the SDK exposes one."""

    for attribute in ("response_id", "request_id"):
        value = getattr(error, attribute, None)
        if value:
            return str(value)[:200]
    headers = getattr(getattr(error, "response", None), "headers", None)
    if headers:
        value = headers.get("x-request-id")
        if value:
            return str(value)[:200]
    return None


def safe_provider_error(error: Exception, *, provider: str) -> str:
    """Create bounded diagnostics without provider payloads, keys, or article text."""

    status = provider_status_code(error)
    if status == 401:
        return f"{provider} 鉴权失败（HTTP 401）：请检查 API Key。"
    if status == 403:
        return f"{provider} 授权失败（HTTP 403）：请检查账户和模型访问权限。"
    if isinstance(error, ProviderConfigurationError):
        return str(error)[:1000]
    if isinstance(error, ProviderResponseError):
        return f"{provider} 响应无效（{error.reason}）。"
    if status is not None:
        return f"{provider} 请求失败（HTTP {status}，{type(error).__name__}）。"
    return f"{provider} 请求失败（{type(error).__name__}）。"
