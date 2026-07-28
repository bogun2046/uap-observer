from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from uap_observer.database import Database
from uap_observer.entity_linking import EntityLinkingService
from uap_observer.models import FactStatus, News, NewsCategory
from uap_observer.repositories import Repository

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class EntityLinkingTests(unittest.TestCase):
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

    def add_analyzed_news(self) -> int:
        news_id = self.repository.add_news(
            News(
                title="Analyzed article",
                original_title="Analyzed article",
                source="Test source",
                source_url="https://example.test/article",
                category=NewsCategory.OFFICIAL_REPORT,
                credibility=5,
                fact_status=FactStatus.OFFICIAL_RECORD,
            )
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE news
                SET processing_status = 'completed',
                    analysis_confidence = 0.85,
                    analysis_json = ?
                WHERE id = ?
                """,
                (
                    json.dumps(
                        {
                            "named_persons": ["Alex Example"],
                            "named_organizations": ["Test Agency"],
                            "related_events": ["Example Event"],
                        }
                    ),
                    news_id,
                ),
            )
        return news_id

    def test_links_person_organization_and_event_idempotently(self) -> None:
        news_id = self.add_analyzed_news()
        first = EntityLinkingService(self.repository).run()
        second = EntityLinkingService(self.repository).run()

        self.assertEqual(first.records, 1)
        self.assertEqual(first.persons_created, 1)
        self.assertEqual(first.organizations_created, 1)
        self.assertEqual(first.events_created, 1)
        self.assertEqual(first.relationships_created, 3)
        self.assertEqual(second.organizations_created, 0)
        self.assertEqual(second.persons_created, 0)
        self.assertEqual(second.events_created, 0)
        self.assertEqual(second.relationships_created, 0)
        with self.database.connect() as connection:
            relationships = connection.execute(
                "SELECT * FROM relationships WHERE evidence_news_id = ?",
                (news_id,),
            ).fetchall()
            person = connection.execute("SELECT * FROM persons").fetchone()
            event = connection.execute("SELECT * FROM events").fetchone()
        self.assertEqual(len(relationships), 3)
        self.assertEqual(person["organization"], "Test Agency")
        self.assertEqual(event["event_name"], "Example Event")
        self.assertEqual(event["status"], "unverified")

    def test_invalid_analysis_json_is_skipped(self) -> None:
        news_id = self.repository.add_news(
            News(
                title="Broken",
                original_title="Broken",
                source="Test source",
                source_url="https://example.test/broken",
                category=NewsCategory.OTHER,
                credibility=2,
                fact_status=FactStatus.UNVERIFIED,
            )
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE news SET processing_status = 'completed', analysis_json = ? WHERE id = ?",
                ("not-json", news_id),
            )

        result = EntityLinkingService(self.repository).run()

        self.assertEqual(result.records, 1)
        self.assertEqual(result.skipped_invalid, 1)


if __name__ == "__main__":
    unittest.main()
