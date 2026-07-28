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
    OpenAIAnalyzer,
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


if __name__ == "__main__":
    unittest.main()
