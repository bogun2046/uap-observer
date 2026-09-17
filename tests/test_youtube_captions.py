from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from uap_observer.collectors.youtube_captions import YouTubeCaptionCollector
from uap_observer.database import Database
from uap_observer.models import FactStatus, News, NewsCategory, Source, SourceType
from uap_observer.repositories import Repository

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class YouTubeCaptionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self.temp_directory.name) / "test.db",
            PROJECT_ROOT / "migrations",
        )
        self.database.initialize()
        self.repository = Repository(self.database)
        source = Source(
            slug="youtube-uap",
            name="YouTube UAP Channel Watchlist",
            source_type=SourceType.API,
            homepage_url="https://www.youtube.com/",
            default_category=NewsCategory.OTHER,
            default_credibility=2,
            default_fact_status=FactStatus.SOURCE_REPORTED,
        )
        source.id = self.repository.upsert_source(source)
        news_id = self.repository.add_news(
            News(
                title="YouTube video",
                original_title="YouTube video",
                source=source.name,
                source_url="https://www.youtube.com/watch?v=video-1",
                canonical_url="https://www.youtube.com/watch?v=video-1",
                category=NewsCategory.OTHER,
                credibility=2,
                fact_status=FactStatus.SOURCE_REPORTED,
                source_id=source.id,
                feed_entry_id="video-1",
            )
        )
        self.repository.record_youtube_metric(
            news_id=news_id,
            video_id="video-1",
            view_count=100,
            like_count=2,
            comment_count=1,
            priority=True,
        )
        self.news_id = news_id

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_completed_caption_updates_extraction_metadata(self) -> None:
        collector = YouTubeCaptionCollector(self.repository, oauth_token="test-token")

        def fake_request(endpoint: str, params: dict[str, object]) -> bytes:
            if endpoint == "captions":
                return b'{"items":[{"id":"track-1","snippet":{"language":"en"}}]}'
            if endpoint == "captions/download":
                return b"WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello UAP\n"
            raise AssertionError(f"Unexpected endpoint: {endpoint}")

        with patch.object(collector, "_request", side_effect=fake_request):
            result = collector.collect()

        self.assertEqual(result.requested, 1)
        self.assertEqual(result.completed, 1)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT transcript_status, extracted_content, extraction_status,
                       extracted_by, content_hash, content_extracted_at
                FROM news WHERE id = ?
                """,
                (self.news_id,),
            ).fetchone()
        self.assertEqual(row["transcript_status"], "completed")
        self.assertEqual(row["extracted_content"], "Hello UAP")
        self.assertEqual(row["extraction_status"], "completed")
        self.assertEqual(row["extracted_by"], "youtube-captions")
        self.assertIsNotNone(row["content_hash"])
        self.assertIsNotNone(row["content_extracted_at"])


if __name__ == "__main__":
    unittest.main()
