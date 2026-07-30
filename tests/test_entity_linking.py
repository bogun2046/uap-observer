from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from uap_observer.database import Database
from uap_observer.entity_linking import EntityLinkingService, canonicalize_organization_name
from uap_observer.models import (
    EntityType,
    FactStatus,
    News,
    NewsCategory,
    Organization,
    Relationship,
)
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

    def test_known_organization_aliases_link_to_one_canonical_entity(self) -> None:
        news_id = self.add_analyzed_news()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE news SET analysis_json = ? WHERE id = ?",
                (
                    json.dumps(
                        {
                            "named_persons": [],
                            "named_organizations": [
                                "AARO",
                                "All-Domain Anomaly Resolution Office (AARO)",
                                "全域异常解决办公室（AARO）",
                            ],
                            "related_events": [],
                        },
                        ensure_ascii=False,
                    ),
                    news_id,
                ),
            )

        result = EntityLinkingService(self.repository).run()

        self.assertEqual(result.organizations_created, 1)
        self.assertEqual(result.relationships_created, 1)
        with self.database.connect() as connection:
            organizations = connection.execute("SELECT name FROM organizations").fetchall()
            relationships = connection.execute(
                "SELECT * FROM relationships WHERE target_type = 'organization'"
            ).fetchall()
        self.assertEqual([row["name"] for row in organizations], ["AARO"])
        self.assertEqual(len(relationships), 1)

    def test_existing_alias_relationships_are_merged_without_duplicates(self) -> None:
        news_id = self.add_analyzed_news()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE news SET analysis_json = ? WHERE id = ?",
                (
                    json.dumps(
                        {
                            "named_persons": [],
                            "named_organizations": [],
                            "related_events": [],
                        }
                    ),
                    news_id,
                ),
            )
        canonical_id = self.repository.add_organization(Organization(name="AARO"))
        alias_id = self.repository.add_organization(
            Organization(name="All-domain Anomaly Resolution Office")
        )
        for organization_id in (canonical_id, alias_id):
            self.repository.add_relationship(
                Relationship(
                    source_type=EntityType.NEWS,
                    source_id=news_id,
                    target_type=EntityType.ORGANIZATION,
                    target_id=organization_id,
                    relationship_type="mentions_organization",
                    evidence_news_id=news_id,
                    confidence=0.85,
                )
            )

        result = EntityLinkingService(self.repository).run()

        self.assertEqual(result.organizations_normalized, 1)
        with self.database.connect() as connection:
            organizations = connection.execute("SELECT id, name FROM organizations").fetchall()
            relationships = connection.execute(
                "SELECT target_id FROM relationships WHERE target_type = 'organization'"
            ).fetchall()
        self.assertEqual([(row["id"], row["name"]) for row in organizations], [(canonical_id, "AARO")])
        self.assertEqual([row["target_id"] for row in relationships], [canonical_id])

    def test_canonicalization_is_conservative_for_unknown_names(self) -> None:
        self.assertEqual(canonicalize_organization_name("  Test   Agency  "), "Test Agency")


if __name__ == "__main__":
    unittest.main()
