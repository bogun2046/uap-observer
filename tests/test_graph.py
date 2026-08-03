from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from uap_observer.database import Database
from uap_observer.entity_linking import EntityLinkingService
from uap_observer.graph import build_person_graph
from uap_observer.models import FactStatus, News, NewsCategory
from uap_observer.repositories import Repository

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class GraphTests(unittest.TestCase):
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

    def add_analyzed_news(self, suffix: str, persons: list[str], *, relation: bool = False) -> None:
        news_id = self.repository.add_news(
            News(
                title=f"Article {suffix}",
                original_title=f"Article {suffix}",
                source="Test source",
                source_url=f"https://example.test/{suffix}",
                publish_date="2026-07-28",
                category=NewsCategory.OTHER,
                credibility=4,
                fact_status=FactStatus.SOURCE_REPORTED,
            )
        )
        person_relationships = []
        if relation:
            person_relationships = [
                {
                    "source_person": persons[0],
                    "target_person": persons[1],
                    "relationship_type": "supports",
                    "evidence_quote": f"{persons[0]}明确支持{persons[1]}。",
                    "confidence": 0.88,
                }
            ]
        analysis = {
            "named_persons": persons,
            "named_organizations": [],
            "related_events": [],
            "topic_tags": ["信息公开"],
            "person_relationships": person_relationships,
        }
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE news
                SET processing_status = 'completed', analysis_confidence = 0.9,
                    analysis_json = ?
                WHERE id = ?
                """,
                (json.dumps(analysis, ensure_ascii=False), news_id),
            )

    def test_linking_creates_tags_explicit_relations_and_is_idempotent(self) -> None:
        self.add_analyzed_news("explicit", ["Alice", "Bob"], relation=True)
        first = EntityLinkingService(self.repository).run()
        second = EntityLinkingService(self.repository).run()

        self.assertEqual(first.tags_created, 1)
        self.assertEqual(first.person_relationships_created, 1)
        self.assertEqual(second.tags_created, 0)
        self.assertEqual(second.person_relationships_created, 0)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tags").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM person_relationships").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM person_relationship_evidence").fetchone()[0],
                1,
            )

    def test_graph_distinguishes_explicit_and_cooccurrence_edges(self) -> None:
        self.add_analyzed_news("cooccur-1", ["Alice", "Carol"])
        self.add_analyzed_news("cooccur-2", ["Alice", "Carol"])
        self.add_analyzed_news("explicit", ["Alice", "Bob"], relation=True)
        EntityLinkingService(self.repository).run()

        graph = build_person_graph(self.repository)

        self.assertEqual(graph["meta"]["person_count"], 3)
        self.assertEqual(graph["meta"]["explicit_edge_count"], 1)
        self.assertEqual(graph["meta"]["cooccurrence_edge_count"], 1)
        self.assertEqual(graph["tags"][0]["person_ids"], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
