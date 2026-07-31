from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from uap_observer.ai_analysis import ANALYSIS_VERSION
from uap_observer.database import Database
from uap_observer.models import (
    AnalysisRiskFlag,
    EntityType,
    Event,
    FactStatus,
    News,
    NewsCategory,
    Person,
    Relationship,
)
from uap_observer.publishing import MarkdownPublisher
from uap_observer.repositories import Repository

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PublishingTests(unittest.TestCase):
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

    def add_published_news(self) -> int:
        news_id = self.repository.add_news(
            News(
                title="待翻译标题",
                original_title="Public UAP Research Update",
                source="Test Agency",
                source_url="https://example.test/article",
                publish_date="2026-07-28T10:00:00Z",
                category=NewsCategory.OTHER,
                credibility=5,
                fact_status=FactStatus.SOURCE_REPORTED,
            )
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE news SET extraction_status = 'completed' WHERE id = ?",
                (news_id,),
            )
        analysis = {
            "chinese_title": "公开报告讨论异常现象观察方法",
            "chinese_summary": "该公开来源介绍了观察方法，并区分记录与解释。",
            "category": "official_report",
            "fact_status": "official_record",
            "key_facts": ["报告已经由来源机构公开发布。"],
            "viewpoints": ["报告没有确认异常现象的具体来源。"],
            "named_persons": [],
            "named_organizations": ["Test Agency"],
            "related_events": [],
            "confidence": 0.9,
            "risk_flags": ["single_source_claim"],
        }
        self.assertTrue(self.repository.claim_analysis_task(news_id))
        self.repository.complete_analysis(
            news_id,
            title=analysis["chinese_title"],
            summary=analysis["chinese_summary"],
            category=NewsCategory.OFFICIAL_REPORT,
            fact_status=FactStatus.OFFICIAL_RECORD,
            key_facts=analysis["key_facts"],
            viewpoints=analysis["viewpoints"],
            model="fake-model",
            response_id="resp_test",
            analysis_version=ANALYSIS_VERSION,
            confidence=0.9,
            risk_flags=[AnalysisRiskFlag.SINGLE_SOURCE_CLAIM],
            analysis_json=json.dumps(analysis, ensure_ascii=False),
        )
        return news_id

    def test_publisher_generates_pages_without_article_body(self) -> None:
        news_id = self.add_published_news()
        event_id = self.repository.add_event(
            Event(
                event_name="Test Historical Event",
                date_start="2004-11-02",
                country="USA",
                description="公开资料记录的历史事件条目。",
                credibility=3,
            )
        )
        self.assertGreater(event_id, 0)
        person_id = self.repository.add_person(Person(name="Test Person", organization="Test Agency"))
        self.repository.add_relationship(
            Relationship(
                source_type=EntityType.NEWS,
                source_id=news_id,
                target_type=EntityType.PERSON,
                target_id=person_id,
                relationship_type="mentions_person",
                evidence_news_id=news_id,
                confidence=0.9,
            )
        )
        output = Path(self.temp_directory.name) / "generated"

        result = MarkdownPublisher(self.repository, output).publish(today="2026-07-28")

        self.assertEqual(result.news_pages, 1)
        self.assertEqual(result.event_count, 1)
        homepage = (output / "index.md").read_text(encoding="utf-8")
        news_index = (output / "news" / "index.md").read_text(encoding="utf-8")
        detail = (output / "news" / f"{news_id}.md").read_text(encoding="utf-8")
        legacy_detail = output / "news" / f"{news_id}-public-uap-research-update.md"
        timeline = (output / "timeline.md").read_text(encoding="utf-8")
        events = (output / "events" / "index.md").read_text(encoding="utf-8")
        persons = (output / "persons" / "index.md").read_text(encoding="utf-8")
        relationships = (output / "relationships.md").read_text(encoding="utf-8")
        search_index = json.loads((output / "search.json").read_text(encoding="utf-8"))
        search_page = (output / "search.md").read_text(encoding="utf-8")

        self.assertIn("开放观测档案", homepage)
        self.assertIn("进入事件地图", homepage)
        self.assertIn("最近新增的图片或视频证据", homepage)
        self.assertIn(f"news/{news_id}.html", homepage)
        self.assertIn("当前无可公开的媒体附件", homepage)
        self.assertIn(f"]({news_id}.html", news_index)
        self.assertIn("官方报告", news_index)
        self.assertIn("../search.html", news_index)
        self.assertTrue((output / "_config.yml").exists())
        self.assertTrue((output / "_layouts" / "default.html").exists())
        self.assertTrue((output / "assets" / "site.css").exists())
        self.assertTrue((output / "assets" / "site.js").exists())
        self.assertNotIn(f"news/news/{news_id}", news_index)
        self.assertTrue(legacy_detail.exists())
        self.assertIn(f"../news/{news_id}.html", legacy_detail.read_text(encoding="utf-8"))
        self.assertIn("## 原始来源", detail)
        self.assertIn("正文已成功提取。", detail)
        self.assertIn("[打开原文](https://example.test/article)", detail)
        self.assertNotIn("内部正文不应出现在页面", detail)
        self.assertIn("2004", timeline)
        self.assertIn("Test Historical Event", events)
        self.assertIn("Test Person", detail)
        self.assertIn("Test Person", persons)
        self.assertIn("mentions_person", relationships)
        self.assertEqual(search_index[0]["url"], f"news/{news_id}.html")
        self.assertNotIn("内部正文不应出现在页面", json.dumps(search_index, ensure_ascii=False))
        self.assertIn("uap-search", search_page)

    def test_empty_publisher_writes_safe_empty_pages(self) -> None:
        output = Path(self.temp_directory.name) / "empty"
        result = MarkdownPublisher(self.repository, output).publish(today="2026-07-28")

        self.assertEqual(result.news_pages, 0)
        self.assertIn(
            "暂无已发布的来源记录",
            (output / "index.md").read_text(encoding="utf-8"),
        )
        self.assertIn("暂无已录入", (output / "events" / "index.md").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((output / "search.json").read_text(encoding="utf-8")), [])

    def test_publisher_includes_source_filtered_news_before_ai(self) -> None:
        news_id = self.repository.add_news(
            News(
                title="Queued UAP report",
                original_title="Queued UAP report",
                source="Test source",
                source_url="https://example.test/queued",
                category=NewsCategory.OTHER,
                credibility=3,
                fact_status=FactStatus.SOURCE_REPORTED,
            )
        )
        output = Path(self.temp_directory.name) / "queued"

        result = MarkdownPublisher(self.repository, output).publish(today="2026-07-28")

        self.assertEqual(result.news_pages, 1)
        self.assertIn("Queued UAP report", (output / "news" / "index.md").read_text(encoding="utf-8"))
        self.assertIn("Queued UAP report", (output / "search.json").read_text(encoding="utf-8"))
        detail = (output / "news" / f"{news_id}.md").read_text(encoding="utf-8")
        self.assertIn("原文正文尚未提取", detail)


if __name__ == "__main__":
    unittest.main()
