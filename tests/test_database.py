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
                "005_organizations.sql",
                "006_source_refresh_schedule.sql",
                "007_youtube_metrics.sql",
                "008_youtube_priority.sql",
                "009_youtube_transcripts.sql",
                "010_graph.sql",
                "011_source_runs.sql",
                "012_source_retry_cooldown.sql",
                "013_source_fallback_urls.sql",
                "014_ai_translation_tracking.sql",
                "015_aaro_403_resolution.sql",
                "016_reddit_403_resolution.sql",
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
            "016_reddit_403_resolution.sql",
        )
        self.assertEqual(
            self.database.status().row_counts,
            {table: 0 for table in CORE_TABLES},
        )

    def test_aaro_403_migration_requeues_only_pending_official_records(self) -> None:
        self.database.initialize()
        repository = Repository(self.database)

        def add_record(suffix: str, source: str) -> int:
            return repository.add_news(
                News(
                    title=f"Record {suffix}",
                    original_title=f"Record {suffix}",
                    source=source,
                    source_url=f"https://example.test/{suffix}",
                    canonical_url=f"https://example.test/{suffix}",
                    category=NewsCategory.OFFICIAL_REPORT,
                    credibility=5,
                    fact_status=FactStatus.OFFICIAL_RECORD,
                )
            )

        press_id = add_record("press-403", "AARO Congressional and Press Products")
        imagery_id = add_record("imagery-403", "AARO Official UAP Imagery")
        reddit_id = add_record("reddit-403", "Reddit r/UFOs")
        non_403_id = add_record("press-timeout", "AARO Congressional and Press Products")
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE news
                SET extraction_status = 'failed', extraction_attempts = 12,
                    extraction_error = 'RuntimeError: HTTP 403',
                    processing_status = 'pending'
                WHERE id IN (?, ?, ?)
                """,
                (press_id, imagery_id, reddit_id),
            )
            connection.execute(
                """
                UPDATE news
                SET extraction_status = 'failed', extraction_attempts = 4,
                    extraction_error = 'RuntimeError: temporary timeout',
                    processing_status = 'pending'
                WHERE id = ?
                """,
                (non_403_id,),
            )
            connection.execute(
                "DELETE FROM schema_migrations WHERE name = ?",
                ("015_aaro_403_resolution.sql",),
            )

        self.assertEqual(self.database.initialize(), ["015_aaro_403_resolution.sql"])

        with self.database.connect() as connection:
            rows = {
                row["id"]: row
                for row in connection.execute(
                    """
                    SELECT id, extraction_status, extraction_attempts, extraction_error
                    FROM news WHERE id IN (?, ?, ?, ?)
                    """,
                    (press_id, imagery_id, reddit_id, non_403_id),
                )
            }
        for news_id in (press_id, imagery_id):
            self.assertEqual(rows[news_id]["extraction_status"], "pending")
            self.assertEqual(rows[news_id]["extraction_attempts"], 12)
            self.assertIn("migration 015", rows[news_id]["extraction_error"])
        self.assertEqual(rows[reddit_id]["extraction_status"], "failed")
        self.assertEqual(rows[non_403_id]["extraction_status"], "failed")

    def test_packaged_aaro_migration_matches_repository_copy(self) -> None:
        repository_migration = PROJECT_ROOT / "migrations" / "015_aaro_403_resolution.sql"
        packaged_migration = (
            PROJECT_ROOT
            / "src"
            / "uap_observer"
            / "migrations"
            / "015_aaro_403_resolution.sql"
        )
        self.assertEqual(
            repository_migration.read_text(encoding="utf-8"),
            packaged_migration.read_text(encoding="utf-8"),
        )

    def test_reddit_403_migration_requeues_only_pending_reddit_records(self) -> None:
        self.database.initialize()
        repository = Repository(self.database)

        def add_record(suffix: str, source: str) -> int:
            return repository.add_news(
                News(
                    title=f"Record {suffix}",
                    original_title=f"Record {suffix}",
                    source=source,
                    source_url=f"https://example.test/{suffix}",
                    canonical_url=f"https://example.test/{suffix}",
                    category=NewsCategory.OTHER,
                    credibility=1,
                    fact_status=FactStatus.SOURCE_REPORTED,
                )
            )

        reddit_403_id = add_record("reddit-403", "Reddit r/UFOs")
        reddit_timeout_id = add_record("reddit-timeout", "Reddit r/aliens")
        aaro_403_id = add_record(
            "aaro-403", "AARO Congressional and Press Products"
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE news
                SET extraction_status = 'failed', extraction_attempts = 9,
                    extraction_error = 'RuntimeError: HTTP 403',
                    processing_status = 'pending'
                WHERE id IN (?, ?)
                """,
                (reddit_403_id, aaro_403_id),
            )
            connection.execute(
                """
                UPDATE news
                SET extraction_status = 'failed', extraction_attempts = 4,
                    extraction_error = 'RuntimeError: temporary timeout',
                    processing_status = 'pending'
                WHERE id = ?
                """,
                (reddit_timeout_id,),
            )
            connection.execute(
                "DELETE FROM schema_migrations WHERE name = ?",
                ("016_reddit_403_resolution.sql",),
            )

        self.assertEqual(self.database.initialize(), ["016_reddit_403_resolution.sql"])

        with self.database.connect() as connection:
            rows = {
                row["id"]: row
                for row in connection.execute(
                    """
                    SELECT id, extraction_status, extraction_attempts, extraction_error
                    FROM news WHERE id IN (?, ?, ?)
                    """,
                    (reddit_403_id, reddit_timeout_id, aaro_403_id),
                )
            }
        self.assertEqual(rows[reddit_403_id]["extraction_status"], "pending")
        self.assertEqual(rows[reddit_403_id]["extraction_attempts"], 9)
        self.assertIn("migration 016", rows[reddit_403_id]["extraction_error"])
        self.assertEqual(rows[reddit_timeout_id]["extraction_status"], "failed")
        self.assertEqual(rows[aaro_403_id]["extraction_status"], "failed")

    def test_packaged_reddit_migration_matches_repository_copy(self) -> None:
        repository_migration = PROJECT_ROOT / "migrations" / "016_reddit_403_resolution.sql"
        packaged_migration = (
            PROJECT_ROOT
            / "src"
            / "uap_observer"
            / "migrations"
            / "016_reddit_403_resolution.sql"
        )
        self.assertEqual(
            repository_migration.read_text(encoding="utf-8"),
            packaged_migration.read_text(encoding="utf-8"),
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

    def test_pipeline_counts_report_processing_queue(self) -> None:
        self.database.initialize()
        repository = Repository(self.database)
        repository.add_news(
            News(
                title="Pending",
                original_title="Pending",
                source="Test",
                source_url="https://example.test/pending",
                category=NewsCategory.OTHER,
                credibility=2,
                fact_status=FactStatus.SOURCE_REPORTED,
            )
        )
        self.assertEqual(repository.get_pipeline_counts(), {"pending": 1})

    def test_title_translation_tracking_defaults_are_migrated(self) -> None:
        self.database.initialize()
        repository = Repository(self.database)
        news_id = repository.add_news(
            News(
                title="English title",
                original_title="English title",
                source="Test",
                source_url="https://example.test/title-tracking",
                category=NewsCategory.OTHER,
                credibility=2,
                fact_status=FactStatus.SOURCE_REPORTED,
            )
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

        self.assertEqual(row["title_translation_status"], "not_started")
        self.assertEqual(row["title_translation_attempts"], 0)
        self.assertIsNone(row["title_translation_error"])
        self.assertIsNone(row["title_translation_model"])
        self.assertIsNone(row["title_translation_response_id"])
        self.assertIsNone(row["title_translation_last_attempt_at"])


if __name__ == "__main__":
    unittest.main()
