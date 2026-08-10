from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

from uap_observer.ai_analysis import (
    ANALYSIS_VERSION,
    AnalysisService,
    AnalyzerResult,
    ArticleAnalysis,
    DeepSeekAnalyzer,
    OpenAIAnalyzer,
    ProviderFailure,
    ProviderHealth,
    ProviderResponseError,
    TitleTranslation,
    TitleTranslationResult,
    is_provider_access_error,
    safe_provider_error,
)
from uap_observer.database import Database
from uap_observer.models import (
    AnalysisRiskFlag,
    FactStatus,
    News,
    NewsCategory,
)
from uap_observer.repositories import Repository

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def valid_analysis(**overrides: object) -> ArticleAnalysis:
    values: dict[str, object] = {
        "chinese_title": "公开报告讨论异常现象观察方法",
        "chinese_summary": (
            "该公开来源介绍了异常现象观察的审查方法，并明确区分已记录信息与解释性判断。"
        ),
        "category": NewsCategory.OFFICIAL_REPORT,
        "fact_status": FactStatus.OFFICIAL_RECORD,
        "key_facts": ["该报告已由来源机构公开发布。"],
        "viewpoints": ["报告作者未对现象来源作出确定结论。"],
        "named_persons": [],
        "named_organizations": ["Test Agency"],
        "related_events": [],
        "confidence": 0.91,
        "risk_flags": [AnalysisRiskFlag.SINGLE_SOURCE_CLAIM],
    }
    values.update(overrides)
    return ArticleAnalysis.model_validate(values)


class MappingAnalyzer:
    model = "fake-structured-model"
    provider = "Test provider"

    def __init__(self, outcomes: dict[int, ArticleAnalysis | Exception]) -> None:
        self.outcomes = outcomes

    def analyze(self, task: object) -> AnalyzerResult:
        news_id = task.news_id
        outcome = self.outcomes[news_id]
        if isinstance(outcome, Exception):
            raise outcome
        return AnalyzerResult(
            analysis=outcome,
            model="fake-structured-model",
            response_id=f"resp_{news_id}",
        )

    def translate_title(self, original_title: str, source: str) -> TitleTranslationResult:
        return TitleTranslationResult(
            title=f"中文：{original_title}",
            model=self.model,
            response_id="title_resp_test",
        )


class RecordingResponses:
    def __init__(self, analysis: ArticleAnalysis) -> None:
        self.analysis = analysis
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_parsed=self.analysis,
            id="resp_test",
            model="gpt-test-resolved",
        )


class FakeProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


class AnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self.temp_directory.name) / "test.db",
            PROJECT_ROOT / "migrations",
        )
        self.database.initialize()
        self.repository = Repository(self.database)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def add_extracted_news(self, suffix: str) -> int:
        news_id = self.repository.add_news(
            News(
                title=f"Article {suffix}",
                original_title=f"Article {suffix}",
                source="Test Agency",
                source_url=f"https://example.test/{suffix}",
                canonical_url=f"https://example.test/{suffix}",
                publish_date="2026-07-28",
                category=NewsCategory.OTHER,
                credibility=4,
                fact_status=FactStatus.SOURCE_REPORTED,
            )
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE news
                SET extraction_status = 'completed',
                    extracted_content = ?
                WHERE id = ?
                """,
                ("Public source article content. " * 30, news_id),
            )
        return news_id

    def test_schema_rejects_unknown_fields_and_invalid_confidence(self) -> None:
        with self.assertRaises(ValidationError):
            valid_analysis(confidence=1.2)
        with self.assertRaises(ValidationError):
            ArticleAnalysis.model_validate(
                {
                    **valid_analysis().model_dump(),
                    "unsupported_conclusion": "aliens",
                }
            )
        with self.assertRaises(ValidationError):
            TitleTranslation(chinese_title="English title only")
        with self.assertRaises(ValidationError):
            valid_analysis(chinese_title="English title only")
        with self.assertRaises(ValidationError):
            valid_analysis(chinese_summary="English summary without localization.")
        with self.assertRaises(ValidationError):
            valid_analysis(key_facts=["English key fact only."])
        with self.assertRaises(ValidationError):
            valid_analysis(viewpoints=["English viewpoint only."])

    def test_service_persists_validated_analysis_and_audit_data(self) -> None:
        news_id = self.add_extracted_news("success")
        result = AnalysisService(
            self.repository,
            MappingAnalyzer({news_id: valid_analysis()}),
        ).run(limit=10)

        self.assertEqual(result.completed, 1)
        self.assertEqual(result.failed, 0)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM news WHERE id = ?",
                (news_id,),
            ).fetchone()
        self.assertEqual(row["processing_status"], "completed")
        self.assertEqual(row["title"], "公开报告讨论异常现象观察方法")
        self.assertEqual(row["category"], "official_report")
        self.assertEqual(row["fact_status"], "official_record")
        self.assertEqual(json.loads(row["key_facts"]), ["该报告已由来源机构公开发布。"])
        self.assertEqual(json.loads(row["risk_flags"]), ["single_source_claim"])
        self.assertEqual(row["analysis_version"], ANALYSIS_VERSION)
        self.assertEqual(row["analysis_response_id"], f"resp_{news_id}")
        self.assertEqual(row["analysis_attempts"], 1)
        self.assertAlmostEqual(row["analysis_confidence"], 0.91)
        self.assertEqual(json.loads(row["analysis_json"])["confidence"], 0.91)

    def test_failed_analysis_requires_explicit_retry(self) -> None:
        news_id = self.add_extracted_news("retry")
        failed = AnalysisService(
            self.repository,
            MappingAnalyzer({news_id: RuntimeError("temporary provider failure")}),
        ).run(limit=10)
        without_retry = AnalysisService(
            self.repository,
            MappingAnalyzer({news_id: valid_analysis()}),
        ).run(limit=10)
        successful_retry = AnalysisService(
            self.repository,
            MappingAnalyzer({news_id: valid_analysis()}),
        ).run(limit=10, retry_failed=True)

        self.assertEqual(failed.failed, 1)
        self.assertEqual(without_retry.queued, 0)
        self.assertEqual(successful_retry.completed, 1)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT processing_status, analysis_attempts, analysis_error
                FROM news WHERE id = ?
                """,
                (news_id,),
            ).fetchone()
        self.assertEqual(row["processing_status"], "completed")
        self.assertEqual(row["analysis_attempts"], 2)
        self.assertIsNone(row["analysis_error"])

    def test_article_analysis_retries_transient_server_failure(self) -> None:
        news_id = self.add_extracted_news("transient-retry")
        delays: list[float] = []

        class EventuallySuccessfulAnalyzer:
            provider = "DeepSeek"
            model = "deepseek-v4-flash"

            def __init__(self) -> None:
                self.calls = 0

            def analyze(self, task: object) -> AnalyzerResult:
                self.calls += 1
                if self.calls < 3:
                    raise FakeProviderError(
                        "temporary upstream payload",
                        status_code=500,
                    )
                return AnalyzerResult(
                    analysis=valid_analysis(),
                    model=self.model,
                    response_id="resp_after_retry",
                )

        analyzer = EventuallySuccessfulAnalyzer()
        result = AnalysisService(
            self.repository,
            analyzer,
            retry_delay_seconds=0.25,
            sleep=delays.append,
        ).run(limit=1, title_translation_limit=0)

        self.assertEqual(result.completed, 1)
        self.assertEqual(result.failed, 0)
        self.assertEqual(analyzer.calls, 3)
        self.assertEqual(delays, [0.25, 0.5])
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT processing_status, analysis_response_id FROM news WHERE id = ?",
                (news_id,),
            ).fetchone()
        self.assertEqual(row["processing_status"], "completed")
        self.assertEqual(row["analysis_response_id"], "resp_after_retry")

    def test_service_recovers_stale_claim(self) -> None:
        news_id = self.add_extracted_news("stale")
        self.assertTrue(self.repository.claim_analysis_task(news_id))
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE news
                SET analysis_started_at = '2000-01-01T00:00:00.000Z'
                WHERE id = ?
                """,
                (news_id,),
            )
        result = AnalysisService(
            self.repository,
            MappingAnalyzer({news_id: valid_analysis()}),
        ).run(limit=10)

        self.assertEqual(result.stale_recovered, 1)
        self.assertEqual(result.completed, 1)

    def test_service_translates_newest_titles_without_waiting_for_extraction(self) -> None:
        older_id = self.repository.add_news(
            News(
                title="Older untranslated title",
                original_title="Older untranslated title",
                source="Test Agency",
                source_url="https://example.test/older",
                canonical_url="https://example.test/older",
                publish_date="2026-07-28",
                category=NewsCategory.OTHER,
                credibility=4,
                fact_status=FactStatus.SOURCE_REPORTED,
            )
        )
        newest_id = self.repository.add_news(
            News(
                title="Newest untranslated title",
                original_title="Newest untranslated title",
                source="Test Agency",
                source_url="https://example.test/newest",
                canonical_url="https://example.test/newest",
                publish_date="2026-07-29",
                category=NewsCategory.OTHER,
                credibility=4,
                fact_status=FactStatus.SOURCE_REPORTED,
            )
        )

        result = AnalysisService(
            self.repository,
            MappingAnalyzer({}),
        ).run(limit=1, title_translation_limit=1)

        self.assertEqual(result.queued, 0)
        self.assertEqual(result.titles_translated, 1)
        with self.database.connect() as connection:
            older = connection.execute(
                "SELECT title, title_translation_status FROM news WHERE id = ?",
                (older_id,),
            ).fetchone()
            newest = connection.execute(
                """
                SELECT title, title_translation_status, title_translation_attempts,
                       title_translation_model, title_translation_response_id,
                       title_translation_last_attempt_at
                FROM news WHERE id = ?
                """,
                (newest_id,),
            ).fetchone()
        self.assertEqual(older["title"], "Older untranslated title")
        self.assertEqual(older["title_translation_status"], "not_started")
        self.assertEqual(newest["title"], "中文：Newest untranslated title")
        self.assertEqual(newest["title_translation_status"], "completed")
        self.assertEqual(newest["title_translation_attempts"], 1)
        self.assertEqual(newest["title_translation_model"], "fake-structured-model")
        self.assertEqual(newest["title_translation_response_id"], "title_resp_test")
        self.assertIsNotNone(newest["title_translation_last_attempt_at"])

    def test_service_prioritizes_youtube_titles_for_translation(self) -> None:
        youtube_id = self.repository.add_news(
            News(
                title="English YouTube title",
                original_title="English YouTube title",
                source="YouTube UAP Channel Watchlist",
                source_url="https://youtube.example/video",
                canonical_url="https://youtube.example/video",
                publish_date="2026-07-01",
                category=NewsCategory.OTHER,
                credibility=2,
                fact_status=FactStatus.SOURCE_REPORTED,
            )
        )
        other_id = self.repository.add_news(
            News(
                title="Newer English article",
                original_title="Newer English article",
                source="Test Agency",
                source_url="https://example.test/newer",
                canonical_url="https://example.test/newer",
                publish_date="2026-08-01",
                category=NewsCategory.OTHER,
                credibility=4,
                fact_status=FactStatus.SOURCE_REPORTED,
            )
        )

        result = AnalysisService(
            self.repository,
            MappingAnalyzer({}),
        ).run(limit=1, title_translation_limit=1)

        self.assertEqual(result.titles_translated, 1)
        with self.database.connect() as connection:
            youtube = connection.execute(
                "SELECT title FROM news WHERE id = ?", (youtube_id,)
            ).fetchone()
            other = connection.execute(
                "SELECT title FROM news WHERE id = ?", (other_id,)
            ).fetchone()
        self.assertEqual(youtube["title"], "中文：English YouTube title")
        self.assertEqual(other["title"], "Newer English article")

    def test_title_translation_failure_is_persisted_without_sensitive_text(self) -> None:
        news_id = self.repository.add_news(
            News(
                title="English title to translate",
                original_title="English title to translate",
                source="YouTube UAP Channel Watchlist",
                source_url="https://youtube.example/failure",
                canonical_url="https://youtube.example/failure",
                category=NewsCategory.OTHER,
                credibility=2,
                fact_status=FactStatus.SOURCE_REPORTED,
            )
        )

        class FailingTranslator:
            provider = "DeepSeek"
            model = "deepseek-v4-flash"

            def __init__(self) -> None:
                self.translation_calls = 0

            def translate_title(self, original_title: str, source: str) -> str:
                self.translation_calls += 1
                raise FakeProviderError(
                    "provider payload included sk-secret-tail and article body",
                    status_code=500,
                    request_id="req_translation_failure",
                )

            def analyze(self, task: object) -> AnalyzerResult:
                raise AssertionError("article analysis should not be queued")

        analyzer = FailingTranslator()
        result = AnalysisService(
            self.repository,
            analyzer,
            sleep=lambda _: None,
        ).run(
            limit=1,
            title_translation_limit=1,
        )

        self.assertEqual(analyzer.translation_calls, 3)
        self.assertEqual(result.titles_translated, 0)
        self.assertEqual(result.titles_failed, 1)
        self.assertFalse(result.provider_access_failed)
        self.assertEqual(
            result.failures,
            (
                ProviderFailure(
                    stage="title_translation",
                    news_id=news_id,
                    attempts=3,
                    error="DeepSeek 请求失败（HTTP 500，FakeProviderError）。",
                    response_id="req_translation_failure",
                ),
            ),
        )
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT title_translation_status, title_translation_attempts,
                       title_translation_error, title_translation_model,
                       title_translation_response_id,
                       title_translation_last_attempt_at
                FROM news WHERE id = ?
                """,
                (news_id,),
            ).fetchone()
        self.assertEqual(row["title_translation_status"], "failed")
        self.assertEqual(row["title_translation_attempts"], 1)
        self.assertEqual(
            row["title_translation_error"],
            "DeepSeek 请求失败（HTTP 500，FakeProviderError）。",
        )
        self.assertEqual(row["title_translation_model"], "deepseek-v4-flash")
        self.assertEqual(row["title_translation_response_id"], "req_translation_failure")
        self.assertIsNotNone(row["title_translation_last_attempt_at"])
        self.assertNotIn("sk-secret-tail", row["title_translation_error"])
        self.assertNotIn("article body", row["title_translation_error"])

    def test_title_translation_retries_then_succeeds(self) -> None:
        news_id = self.repository.add_news(
            News(
                title="English title to retry",
                original_title="English title to retry",
                source="YouTube UAP Channel Watchlist",
                source_url="https://youtube.example/retry-success",
                canonical_url="https://youtube.example/retry-success",
                category=NewsCategory.OTHER,
                credibility=2,
                fact_status=FactStatus.SOURCE_REPORTED,
            )
        )
        delays: list[float] = []

        class EventuallySuccessfulTranslator:
            provider = "DeepSeek"
            model = "deepseek-v4-flash"

            def __init__(self) -> None:
                self.calls = 0

            def translate_title(self, original_title: str, source: str) -> str:
                self.calls += 1
                if self.calls < 3:
                    raise ProviderResponseError(
                        "title_invalid_json",
                        response_id=f"resp_invalid_{self.calls}",
                    )
                return "重试后成功的中文标题"

            def analyze(self, task: object) -> AnalyzerResult:
                raise AssertionError("article analysis should not be queued")

        analyzer = EventuallySuccessfulTranslator()
        result = AnalysisService(
            self.repository,
            analyzer,
            retry_delay_seconds=0.5,
            sleep=delays.append,
        ).run(limit=1, title_translation_limit=1)

        self.assertEqual(result.titles_translated, 1)
        self.assertEqual(result.titles_failed, 0)
        self.assertEqual(result.failures, ())
        self.assertEqual(analyzer.calls, 3)
        self.assertEqual(delays, [0.5, 1.0])
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT title, title_translation_status FROM news WHERE id = ?",
                (news_id,),
            ).fetchone()
        self.assertEqual(row["title"], "重试后成功的中文标题")
        self.assertEqual(row["title_translation_status"], "completed")

    def test_title_authentication_failure_stops_all_later_calls(self) -> None:
        first_id = self.repository.add_news(
            News(
                title="First English YouTube title",
                original_title="First English YouTube title",
                source="YouTube UAP Channel Watchlist",
                source_url="https://youtube.example/first-auth",
                canonical_url="https://youtube.example/first-auth",
                category=NewsCategory.OTHER,
                credibility=2,
                fact_status=FactStatus.SOURCE_REPORTED,
            )
        )
        second_id = self.repository.add_news(
            News(
                title="Second English YouTube title",
                original_title="Second English YouTube title",
                source="YouTube UAP Channel Watchlist",
                source_url="https://youtube.example/second-auth",
                canonical_url="https://youtube.example/second-auth",
                category=NewsCategory.OTHER,
                credibility=2,
                fact_status=FactStatus.SOURCE_REPORTED,
            )
        )
        self.add_extracted_news("must-not-run")

        class AuthenticationFailingAnalyzer:
            provider = "DeepSeek"
            model = "deepseek-v4-flash"

            def __init__(self) -> None:
                self.translation_calls = 0
                self.analysis_calls = 0

            def translate_title(self, original_title: str, source: str) -> str:
                self.translation_calls += 1
                raise FakeProviderError(
                    "Authentication Fails, key suffix and source content",
                    status_code=401,
                )

            def analyze(self, task: object) -> AnalyzerResult:
                self.analysis_calls += 1
                raise AssertionError("article analysis must stop after title auth failure")

        analyzer = AuthenticationFailingAnalyzer()
        result = AnalysisService(self.repository, analyzer).run(
            limit=10,
            title_translation_limit=10,
        )

        self.assertEqual(analyzer.translation_calls, 1)
        self.assertEqual(analyzer.analysis_calls, 0)
        self.assertEqual(result.titles_failed, 1)
        self.assertTrue(result.provider_access_failed)
        self.assertEqual(result.queued, 0)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title_translation_status, title_translation_error
                FROM news WHERE id IN (?, ?) ORDER BY id DESC
                """,
                (first_id, second_id),
            ).fetchall()
        self.assertEqual(rows[0]["title_translation_status"], "failed")
        self.assertEqual(rows[1]["title_translation_status"], "not_started")
        self.assertEqual(
            rows[0]["title_translation_error"],
            "DeepSeek 鉴权失败（HTTP 401）：请检查 API Key。",
        )

    def test_article_authentication_failure_stops_batch_after_one_call(self) -> None:
        first_id = self.add_extracted_news("article-auth-first")
        second_id = self.add_extracted_news("article-auth-second")

        class AuthenticationFailingAnalyzer:
            provider = "DeepSeek"
            model = "deepseek-v4-flash"

            def __init__(self) -> None:
                self.analysis_calls = 0

            def analyze(self, task: object) -> AnalyzerResult:
                self.analysis_calls += 1
                raise FakeProviderError(
                    "Authentication Fails, key suffix and article body",
                    status_code=401,
                )

        analyzer = AuthenticationFailingAnalyzer()
        result = AnalysisService(self.repository, analyzer).run(
            limit=10,
            title_translation_limit=0,
        )

        self.assertEqual(analyzer.analysis_calls, 1)
        self.assertEqual(result.queued, 2)
        self.assertEqual(result.claimed, 1)
        self.assertEqual(result.failed, 1)
        self.assertTrue(result.provider_access_failed)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, processing_status, analysis_error
                FROM news WHERE id IN (?, ?) ORDER BY id ASC
                """,
                (first_id, second_id),
            ).fetchall()
        self.assertEqual(rows[0]["processing_status"], "failed")
        self.assertEqual(rows[1]["processing_status"], "pending")
        self.assertEqual(
            rows[0]["analysis_error"],
            "DeepSeek 鉴权失败（HTTP 401）：请检查 API Key。",
        )

    def test_openai_adapter_uses_non_streaming_structured_response(self) -> None:
        responses = RecordingResponses(valid_analysis())
        analyzer = OpenAIAnalyzer(
            model="gpt-test",
            reasoning_effort="low",
            client=SimpleNamespace(responses=responses),
        )
        task = self.repository.get_analysis_tasks(
            limit=1,
        )
        self.assertEqual(task, [])
        news_id = self.add_extracted_news("adapter")
        analysis_task = self.repository.get_analysis_tasks(limit=1)[0]

        result = analyzer.analyze(analysis_task)

        self.assertEqual(analysis_task.news_id, news_id)
        self.assertEqual(result.response_id, "resp_test")
        self.assertEqual(result.model, "gpt-test-resolved")
        call = responses.calls[0]
        self.assertIs(call["text_format"], ArticleAnalysis)
        self.assertEqual(call["reasoning"], {"effort": "low"})
        self.assertFalse(call["store"])
        self.assertNotIn("stream", call)
        payload = json.loads(call["input"])
        self.assertEqual(payload["source"], "Test Agency")
        self.assertFalse(payload["content_truncated"])

    def test_deepseek_adapter_uses_non_thinking_json_output(self) -> None:
        analysis = valid_analysis().model_dump(mode="json")
        response = SimpleNamespace(
            id="deepseek_resp",
            model="deepseek-v4-flash",
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(analysis)))],
        )
        calls: list[dict[str, object]] = []

        class Completions:
            def create(self, **kwargs: object) -> object:
                calls.append(kwargs)
                return response

        analyzer = DeepSeekAnalyzer(
            model="deepseek-v4-flash",
            api_key="test-key",
            client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        )
        news_id = self.add_extracted_news("deepseek")

        result = analyzer.analyze(self.repository.get_analysis_tasks(limit=1)[0])

        self.assertEqual(result.response_id, "deepseek_resp")
        self.assertEqual(result.model, "deepseek-v4-flash")
        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})
        self.assertEqual(calls[0]["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(calls[0]["max_tokens"], 6000)
        self.assertIn("EXAMPLE JSON OUTPUT", calls[0]["messages"][0]["content"])
        self.assertEqual(json.loads(calls[0]["messages"][1]["content"])["source"], "Test Agency")
        self.assertEqual(news_id, self.repository.get_analysis_tasks(limit=1)[0].news_id)

    def test_deepseek_invalid_json_is_safe_and_keeps_response_id(self) -> None:
        response = SimpleNamespace(
            id="deepseek_invalid_json",
            model="deepseek-v4-flash",
            choices=[SimpleNamespace(message=SimpleNamespace(content="not-json"))],
        )

        class Completions:
            def create(self, **kwargs: object) -> object:
                return response

        analyzer = DeepSeekAnalyzer(
            model="deepseek-v4-flash",
            api_key="test-key",
            client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        )

        with self.assertRaises(ProviderResponseError) as context:
            analyzer.translate_title("Sensitive source title", "Sensitive source")

        self.assertEqual(context.exception.reason, "title_invalid_json")
        self.assertEqual(context.exception.response_id, "deepseek_invalid_json")
        diagnostic = safe_provider_error(context.exception, provider="DeepSeek")
        self.assertEqual(diagnostic, "DeepSeek 响应无效（title_invalid_json）。")
        self.assertNotIn("Sensitive", diagnostic)

    def test_deepseek_reports_safe_finish_reason_for_truncated_output(self) -> None:
        response = SimpleNamespace(
            id="deepseek_truncated",
            model="deepseek-v4-flash",
            choices=[
                SimpleNamespace(
                    finish_reason="length",
                    message=SimpleNamespace(content=""),
                )
            ],
        )

        class Completions:
            def create(self, **kwargs: object) -> object:
                return response

        analyzer = DeepSeekAnalyzer(
            model="deepseek-v4-flash",
            api_key="test-key",
            client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        )

        with self.assertRaises(ProviderResponseError) as context:
            analyzer.translate_title("Sensitive source title", "Sensitive source")

        self.assertEqual(
            context.exception.reason,
            "title_missing_content_finish_length",
        )
        self.assertEqual(context.exception.response_id, "deepseek_truncated")
        diagnostic = safe_provider_error(context.exception, provider="DeepSeek")
        self.assertNotIn("Sensitive", diagnostic)

    def test_deepseek_title_translation_returns_audit_metadata(self) -> None:
        response = SimpleNamespace(
            id="deepseek_title_resp",
            model="deepseek-v4-flash",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({"chinese_title": "量子计算机最可怕的事情"})
                    )
                )
            ],
        )

        class Completions:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def create(self, **kwargs: object) -> object:
                self.calls.append(kwargs)
                return response

        completions = Completions()
        analyzer = DeepSeekAnalyzer(
            model="deepseek-v4-flash",
            api_key="test-key",
            client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        )

        result = analyzer.translate_title("The scariest thing about quantum computers", "YouTube")

        self.assertEqual(result.title, "量子计算机最可怕的事情")
        self.assertEqual(result.model, "deepseek-v4-flash")
        self.assertEqual(result.response_id, "deepseek_title_resp")
        self.assertEqual(
            completions.calls[0]["extra_body"],
            {"thinking": {"type": "disabled"}},
        )
        self.assertEqual(completions.calls[0]["max_tokens"], 600)
        self.assertIn(
            "EXAMPLE JSON OUTPUT",
            completions.calls[0]["messages"][0]["content"],
        )

    def test_deepseek_health_check_lists_models_once(self) -> None:
        class Models:
            def __init__(self) -> None:
                self.calls = 0

            def list(self) -> object:
                self.calls += 1
                return SimpleNamespace(
                    data=[
                        SimpleNamespace(id="deepseek-v4-flash"),
                        SimpleNamespace(id="deepseek-v4-pro"),
                    ]
                )

        models = Models()
        analyzer = DeepSeekAnalyzer(
            model="deepseek-v4-flash",
            api_key="test-key",
            client=SimpleNamespace(models=models),
        )

        result = analyzer.health_check()

        self.assertEqual(
            result,
            ProviderHealth(
                provider="DeepSeek",
                model="deepseek-v4-flash",
                available_models=2,
            ),
        )
        self.assertEqual(models.calls, 1)

    def test_safe_provider_error_never_includes_key_suffix_or_payload(self) -> None:
        error = FakeProviderError(
            "Authentication Fails, Your api key: masked-tail; article body",
            status_code=401,
        )

        diagnostic = safe_provider_error(error, provider="DeepSeek")

        self.assertEqual(diagnostic, "DeepSeek 鉴权失败（HTTP 401）：请检查 API Key。")
        self.assertNotIn("masked-tail", diagnostic)
        self.assertNotIn("article body", diagnostic)

        forbidden = FakeProviderError(
            "Permission denied with provider payload",
            status_code=403,
        )
        self.assertTrue(is_provider_access_error(forbidden))
        self.assertEqual(
            safe_provider_error(forbidden, provider="DeepSeek"),
            "DeepSeek 授权失败（HTTP 403）：请检查账户和模型访问权限。",
        )


if __name__ == "__main__":
    unittest.main()
