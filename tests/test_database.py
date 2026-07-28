from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from uap_observer.database import CORE_TABLES, Database
from uap_observer.models import (
    EntityType,
    Event,
    FactStatus,
    News,
    NewsCategory,
    Person,
    Relationship,
    Source,
    SourceType,
)
from uap_observer.repositories import Repository

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_directory.name) / "nested" / "test.db"
        self.database = Database(database_path, PROJECT_ROOT / "migrations")

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_initialize_creates_core_tables_and_is_idempotent(self) -> None:
        self.assertEqual(
            self.database.initialize(),
            [
                "001_initial.sql",
                "002_sources.sql",
                "003_article_extraction.sql",
                "004_ai_analysis.sql",
            ],
        )
        self.assertEqual(self.database.initialize(), [])

        with self.database.connect() as connection:
            table_names = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }

        self.assertTrue(set(CORE_TABLES).issubset(table_names))
        self.assertEqual(
            self.database.status().schema_version,
            "004_ai_analysis.sql",
        )
        self.assertEqual(
            self.database.status().row_counts,
            {table: 0 for table in CORE_TABLES},
        )

    def test_repository_saves_models_and_relationship_evidence(self) -> None:
        self.database.initialize()
        repository = Repository(self.database)

        news_id = repository.add_news(
            News(
                title="AARO发布年度报告",
                original_title="AARO Releases Annual Report",
                source="AARO",
                source_url="https://example.test/aaro-report",
                category=NewsCategory.OFFICIAL_REPORT,
                credibility=5,
                fact_status=FactStatus.OFFICIAL_RECORD,
                key_facts=["报告已由AARO公开发布"],
            )
        )
        event_id = repository.add_event(Event(event_name="Test Event", credibility=3))
        person_id = repository.add_person(Person(name="Test Person"))
        relationship_id = repository.add_relationship(
            Relationship(
                source_type=EntityType.EVENT,
                source_id=event_id,
                target_type=EntityType.PERSON,
                target_id=person_id,
                relationship_type="associated_with",
                evidence_news_id=news_id,
                confidence=0.75,
            )
        )

        with self.database.connect() as connection:
            news_row = connection.execute(
                "SELECT * FROM news WHERE id = ?", (news_id,)
            ).fetchone()
            relationship_row = connection.execute(
                "SELECT * FROM relationships WHERE id = ?", (relationship_id,)
            ).fetchone()

        self.assertEqual(json.loads(news_row["key_facts"]), ["报告已由AARO公开发布"])
        self.assertEqual(news_row["fact_status"], "official_record")
        self.assertEqual(relationship_row["evidence_news_id"], news_id)
        self.assertEqual(relationship_row["confidence"], 0.75)

    def test_source_upsert_is_repeatable(self) -> None:
        self.database.initialize()
        repository = Repository(self.database)
        source = Source(
            slug="nasa",
            name="NASA",
            source_type=SourceType.RSS,
            homepage_url="https://www.nasa.gov/",
            feed_url="https://www.nasa.gov/feed/",
            default_category=NewsCategory.OFFICIAL_REPORT,
            default_credibility=5,
            default_fact_status=FactStatus.OFFICIAL_RECORD,
            include_keywords=["UAP"],
        )

        first_id = repository.upsert_source(source)
        source.name = "NASA Recently Published"
        second_id = repository.upsert_source(source)
        stored = repository.get_sources(slug="nasa")

        self.assertEqual(first_id, second_id)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].name, "NASA Recently Published")
        self.assertEqual(stored[0].include_keywords, ["UAP"])

    def test_database_constraints_reject_invalid_credibility(self) -> None:
        self.database.initialize()
        repository = Repository(self.database)

        with self.assertRaises(sqlite3.IntegrityError):
            repository.add_news(
                News(
                    title="Invalid",
                    original_title="Invalid",
                    source="Test",
                    source_url="https://example.test/invalid",
                    category=NewsCategory.OTHER,
                    credibility=6,
                    fact_status=FactStatus.UNVERIFIED,
                )
            )


if __name__ == "__main__":
    unittest.main()
