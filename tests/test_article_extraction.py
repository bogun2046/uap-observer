from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from uap_observer.article_extraction import (
    ArticleExtractionService,
    ExtractedArticle,
    TrafilaturaArticleExtractor,
)
from uap_observer.database import Database
from uap_observer.models import FactStatus, News, NewsCategory
from uap_observer.repositories import Repository


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MappingExtractor:
    def __init__(self, outcomes: dict[str, ExtractedArticle | Exception]) -> None:
        self.outcomes = outcomes

    def extract_url(self, url: str) -> ExtractedArticle:
        outcome = self.outcomes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ArticleExtractionTests(unittest.TestCase):
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

    def add_news(self, suffix: str) -> int:
        return self.repository.add_news(
            News(
                title=f"Article {suffix}",
                original_title=f"Article {suffix}",
                source="Test",
                source_url=f"https://example.test/{suffix}",
                canonical_url=f"https://example.test/{suffix}",
                category=NewsCategory.OTHER,
                credibility=3,
                fact_status=FactStatus.SOURCE_REPORTED,
            )
        )

    def test_service_completes_fails_and_skips_duplicate_content(self) -> None:
        first_id = self.add_news("first")
        duplicate_id = self.add_news("duplicate")
        failed_id = self.add_news("failed")
        shared_content = "Verified public source content. " * 20
        extractor = MappingExtractor(
            {
                "https://example.test/first": ExtractedArticle(
                    content=shared_content,
                    title="Extracted title",
                    author="Test Author",
                    publish_date="2026-07-28",
                    language="en",
                    extractor="fake/1",
                ),
                "https://example.test/duplicate": ExtractedArticle(
                    content=shared_content,
                    extractor="fake/1",
                ),
                "https://example.test/failed": RuntimeError("download failed"),
            }
        )

        result = ArticleExtractionService(self.repository, extractor).run(limit=10)

        self.assertEqual(result.queued, 3)
        self.assertEqual(result.completed, 1)
        self.assertEqual(result.skipped_duplicates, 1)
        self.assertEqual(result.failed, 1)
        with self.database.connect() as connection:
            rows = {
                row["id"]: row
                for row in connection.execute(
                    """
                    SELECT id, extraction_status, extracted_content, extracted_author,
                           content_hash, extraction_attempts, extraction_error
                    FROM news
                    """
                )
            }
        self.assertEqual(rows[first_id]["extraction_status"], "completed")
        self.assertEqual(rows[first_id]["extracted_author"], "Test Author")
        self.assertIsNotNone(rows[first_id]["content_hash"])
        self.assertEqual(rows[duplicate_id]["extraction_status"], "skipped")
        self.assertIn(f"news_id={first_id}", rows[duplicate_id]["extraction_error"])
        self.assertEqual(rows[failed_id]["extraction_status"], "failed")
        self.assertIn("download failed", rows[failed_id]["extraction_error"])
        self.assertEqual(rows[failed_id]["extraction_attempts"], 1)

    def test_failed_tasks_are_retried_only_when_requested(self) -> None:
        news_id = self.add_news("retry")
        failing = MappingExtractor(
            {"https://example.test/retry": RuntimeError("temporary failure")}
        )
        service = ArticleExtractionService(self.repository, failing)
        service.run(limit=10)

        without_retry = service.run(limit=10)
        successful = ArticleExtractionService(
            self.repository,
            MappingExtractor(
                {
                    "https://example.test/retry": ExtractedArticle(
                        content="Recovered article content. " * 20,
                        extractor="fake/2",
                    )
                }
            ),
        ).run(limit=10, retry_failed=True)

        self.assertEqual(without_retry.queued, 0)
        self.assertEqual(successful.completed, 1)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT extraction_status, extraction_attempts FROM news WHERE id = ?",
                (news_id,),
            ).fetchone()
        self.assertEqual(row["extraction_status"], "completed")
        self.assertEqual(row["extraction_attempts"], 2)

    def test_claim_prevents_two_workers_from_processing_same_article(self) -> None:
        news_id = self.add_news("claim")
        self.assertTrue(self.repository.claim_article_task(news_id))
        self.assertFalse(self.repository.claim_article_task(news_id))

    def test_service_recovers_stale_processing_task(self) -> None:
        news_id = self.add_news("stale")
        self.assertTrue(self.repository.claim_article_task(news_id))
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE news
                SET extraction_started_at = '2000-01-01T00:00:00.000Z'
                WHERE id = ?
                """,
                (news_id,),
            )
        result = ArticleExtractionService(
            self.repository,
            MappingExtractor(
                {
                    "https://example.test/stale": ExtractedArticle(
                        content="Recovered stale article. " * 20,
                        extractor="fake/3",
                    )
                }
            ),
        ).run(limit=10)

        self.assertEqual(result.stale_recovered, 1)
        self.assertEqual(result.completed, 1)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT extraction_status, extraction_attempts FROM news WHERE id = ?",
                (news_id,),
            ).fetchone()
        self.assertEqual(row["extraction_status"], "completed")
        self.assertEqual(row["extraction_attempts"], 2)

    @unittest.skipUnless(
        importlib.util.find_spec("trafilatura"),
        "Trafilatura is not installed in this interpreter",
    )
    def test_trafilatura_extracts_static_article_html(self) -> None:
        html = """
        <html lang="en">
          <head>
            <title>Official UAP Research Update</title>
            <meta name="author" content="Research Team">
            <meta property="article:published_time" content="2026-07-28">
          </head>
          <body>
            <nav>Navigation links</nav>
            <article>
              <h1>Official UAP Research Update</h1>
              <p>This public report describes the methodology used to review
              unidentified anomalous phenomena observations.</p>
              <p>The research team distinguishes recorded observations from
              interpretations and does not claim an extraterrestrial origin.</p>
            </article>
            <footer>Footer text</footer>
          </body>
        </html>
        """
        article = TrafilaturaArticleExtractor(minimum_characters=100).extract_html(
            html,
            url="https://example.test/report",
        )
        self.assertIn("methodology", article.content)
        self.assertNotIn("Navigation links", article.content)
        self.assertEqual(article.author, "Research Team")
        self.assertTrue(article.extractor.startswith("trafilatura/"))


if __name__ == "__main__":
    unittest.main()
