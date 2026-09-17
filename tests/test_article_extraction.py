from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def add_news(self, suffix: str, *, raw_content: str | None = None) -> int:
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
                raw_content=raw_content,
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

    def test_failed_tasks_stop_at_default_attempt_budget_unless_forced(self) -> None:
        news_id = self.add_news("exhausted-retry")
        failing = ArticleExtractionService(
            self.repository,
            MappingExtractor(
                {"https://example.test/exhausted-retry": RuntimeError("still blocked")}
            ),
        )

        failing.run(limit=10)
        failing.run(limit=10, retry_failed=True)
        failing.run(limit=10, retry_failed=True)
        exhausted = failing.run(limit=10, retry_failed=True)

        self.assertEqual(exhausted.queued, 0)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT extraction_status, extraction_attempts FROM news WHERE id = ?",
                (news_id,),
            ).fetchone()
        self.assertEqual(row["extraction_status"], "failed")
        self.assertEqual(row["extraction_attempts"], 3)

        forced = ArticleExtractionService(
            self.repository,
            MappingExtractor(
                {
                    "https://example.test/exhausted-retry": ExtractedArticle(
                        content="Recovered after an explicit operator retry. " * 20,
                        extractor="fake/forced-retry",
                    )
                }
            ),
        ).run(limit=10, retry_failed=True, max_failed_attempts=None)

        self.assertEqual(forced.completed, 1)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT extraction_status, extraction_attempts FROM news WHERE id = ?",
                (news_id,),
            ).fetchone()
        self.assertEqual(row["extraction_status"], "completed")
        self.assertEqual(row["extraction_attempts"], 4)

    def test_pending_tasks_are_prioritized_ahead_of_failed_retries(self) -> None:
        failed_id = self.add_news("older-failed")
        ArticleExtractionService(
            self.repository,
            MappingExtractor(
                {"https://example.test/older-failed": RuntimeError("temporary failure")}
            ),
        ).run(limit=1)
        pending_id = self.add_news("newer-pending")
        service = ArticleExtractionService(
            self.repository,
            MappingExtractor(
                {
                    "https://example.test/older-failed": ExtractedArticle(
                        content="Recovered older article. " * 20,
                        extractor="fake/retry",
                    ),
                    "https://example.test/newer-pending": ExtractedArticle(
                        content="New pending article content. " * 20,
                        extractor="fake/pending",
                    ),
                }
            ),
        )

        result = service.run(limit=1, retry_failed=True)

        self.assertEqual(result.queued, 1)
        self.assertEqual(result.completed, 1)
        with self.database.connect() as connection:
            rows = {
                row["id"]: row["extraction_status"]
                for row in connection.execute(
                    "SELECT id, extraction_status FROM news WHERE id IN (?, ?)",
                    (failed_id, pending_id),
                )
            }
        self.assertEqual(rows[failed_id], "failed")
        self.assertEqual(rows[pending_id], "completed")

    def test_failed_extraction_uses_rss_description_fallback(self) -> None:
        news_id = self.add_news(
            "rss-fallback",
            raw_content=(
                "A Reddit post describes an unidentified aerial observation and includes "
                "enough feed text to provide a useful source-reported summary."
            ),
        )
        service = ArticleExtractionService(
            self.repository,
            MappingExtractor({"https://example.test/rss-fallback": RuntimeError("HTTP 403")}),
        )

        result = service.run(limit=10)

        self.assertEqual(result.completed, 1)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT extraction_status, extracted_by, extracted_content FROM news WHERE id = ?",
                (news_id,),
            ).fetchone()
        self.assertEqual(row["extraction_status"], "completed")
        self.assertEqual(row["extracted_by"], "rss-description-fallback")
        self.assertIn("unidentified aerial observation", row["extracted_content"])

    def test_reddit_http_403_without_feed_text_becomes_metadata_only_skip(self) -> None:
        url = "https://www.reddit.com/r/UFOs/comments/example/source_record/"
        news_id = self.repository.add_news(
            News(
                title="Reddit source record",
                original_title="Reddit source record",
                source="Reddit r/UFOs",
                source_url=url,
                canonical_url=url,
                category=NewsCategory.OTHER,
                credibility=1,
                fact_status=FactStatus.SOURCE_REPORTED,
                raw_content="submitted by /u/example [link] [comments]",
            )
        )

        result = ArticleExtractionService(
            self.repository,
            MappingExtractor({url: RuntimeError("Unable to download article (403)")}),
        ).run(limit=10)

        self.assertEqual(result.completed, 0)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.skipped_unavailable, 1)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT extraction_status, processing_status, extracted_by,
                       extracted_content, extraction_error
                FROM news WHERE id = ?
                """,
                (news_id,),
            ).fetchone()
        self.assertEqual(row["extraction_status"], "skipped")
        self.assertEqual(row["processing_status"], "skipped")
        self.assertEqual(row["extracted_by"], "reddit-metadata-only")
        self.assertIsNone(row["extracted_content"])
        self.assertIn("HTTP 403", row["extraction_error"])
        self.assertNotIn(url, row["extraction_error"])

    def test_reddit_non_403_failure_remains_retryable(self) -> None:
        url = "https://www.reddit.com/r/aliens/comments/example/source_record/"
        news_id = self.repository.add_news(
            News(
                title="Reddit source record",
                original_title="Reddit source record",
                source="Reddit r/aliens",
                source_url=url,
                canonical_url=url,
                category=NewsCategory.OTHER,
                credibility=1,
                fact_status=FactStatus.SOURCE_REPORTED,
                raw_content="submitted by /u/example [link] [comments]",
            )
        )

        result = ArticleExtractionService(
            self.repository,
            MappingExtractor({url: RuntimeError("temporary network timeout")}),
        ).run(limit=10)

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.skipped_unavailable, 0)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT extraction_status, processing_status FROM news WHERE id = ?",
                (news_id,),
            ).fetchone()
        self.assertEqual(row["extraction_status"], "failed")
        self.assertEqual(row["processing_status"], "pending")

    def test_aaro_imagery_uses_official_description_when_pdf_is_blocked(self) -> None:
        description = (
            "In October 2017, an infrared sensor onboard a force protection aerostat "
            "near Al Taqaddum Air Base captured video of an unidentified object. "
            "AARO analyzed the available official imagery and published its assessment."
        )
        url = "https://www.aaro.mil/example/imagery-report.pdf"
        news_id = self.repository.add_news(
            News(
                title="Al Taqaddum Object",
                original_title=description,
                source="AARO Official UAP Imagery",
                source_url=url,
                canonical_url=url,
                category=NewsCategory.OFFICIAL_REPORT,
                credibility=5,
                fact_status=FactStatus.OFFICIAL_RECORD,
                summary="+ Expand row details | Al Taqaddum Object",
            )
        )

        result = ArticleExtractionService(
            self.repository,
            MappingExtractor({url: RuntimeError("Unable to download PDF (403)")}),
        ).run(limit=10)

        self.assertEqual(result.completed, 1)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.skipped_unavailable, 0)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT extraction_status, processing_status, extracted_by,
                       extracted_content
                FROM news WHERE id = ?
                """,
                (news_id,),
            ).fetchone()
        self.assertEqual(row["extraction_status"], "completed")
        self.assertEqual(row["processing_status"], "pending")
        self.assertEqual(row["extracted_by"], "official-page-description-fallback")
        self.assertEqual(row["extracted_content"], description)

    def test_aaro_press_http_403_becomes_explicit_metadata_only_skip(self) -> None:
        url = "https://www.aaro.mil/example/annual-report.pdf"
        news_id = self.repository.add_news(
            News(
                title="FY25 UAP Annual Report",
                original_title="Fiscal Year 2025 Consolidated Annual Report on UAP",
                source="AARO Congressional and Press Products",
                source_url=url,
                canonical_url=url,
                category=NewsCategory.OFFICIAL_REPORT,
                credibility=5,
                fact_status=FactStatus.OFFICIAL_RECORD,
                summary="2025 | FY25 UAP Annual Report",
            )
        )

        result = ArticleExtractionService(
            self.repository,
            MappingExtractor({url: RuntimeError("Unable to download PDF (403): blocked")}),
        ).run(limit=10)

        self.assertEqual(result.completed, 0)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.skipped_unavailable, 1)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT extraction_status, processing_status, extracted_by,
                       extracted_content, extraction_error
                FROM news WHERE id = ?
                """,
                (news_id,),
            ).fetchone()
        self.assertEqual(row["extraction_status"], "skipped")
        self.assertEqual(row["processing_status"], "skipped")
        self.assertEqual(row["extracted_by"], "official-metadata-only")
        self.assertIsNone(row["extracted_content"])
        self.assertIn("HTTP 403", row["extraction_error"])
        self.assertNotIn(url, row["extraction_error"])

    def test_aaro_press_non_403_failure_remains_retryable(self) -> None:
        url = "https://www.aaro.mil/example/temporarily-unavailable.pdf"
        news_id = self.repository.add_news(
            News(
                title="AARO briefing",
                original_title="AARO briefing",
                source="AARO Congressional and Press Products",
                source_url=url,
                canonical_url=url,
                category=NewsCategory.OFFICIAL_REPORT,
                credibility=5,
                fact_status=FactStatus.OFFICIAL_RECORD,
                summary="2026 | Briefing",
            )
        )

        result = ArticleExtractionService(
            self.repository,
            MappingExtractor({url: RuntimeError("temporary network timeout")}),
        ).run(limit=10)

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.skipped_unavailable, 0)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT extraction_status, processing_status FROM news WHERE id = ?",
                (news_id,),
            ).fetchone()
        self.assertEqual(row["extraction_status"], "failed")
        self.assertEqual(row["processing_status"], "pending")

    def test_duplicate_rss_fallback_is_skipped_and_batch_continues(self) -> None:
        existing_id = self.add_news("existing")
        shared_content = (
            "A syndicated Reddit description repeats previously extracted public "
            "source material while preserving enough text for the RSS fallback."
        )
        shared_hash = hashlib.sha256(shared_content.encode("utf-8")).hexdigest()
        self.assertTrue(self.repository.claim_article_task(existing_id))
        self.repository.complete_article_extraction(
            existing_id,
            content=shared_content,
            content_hash=shared_hash,
            title="Existing article",
            author=None,
            publish_date=None,
            language="en",
            extracted_by="fake/seed",
        )
        duplicate_id = self.add_news("duplicate-rss", raw_content=shared_content)
        later_id = self.add_news("later")
        service = ArticleExtractionService(
            self.repository,
            MappingExtractor(
                {
                    "https://example.test/duplicate-rss": RuntimeError("HTTP 403"),
                    "https://example.test/later": ExtractedArticle(
                        content="A later unique article completes after the duplicate. " * 8,
                        extractor="fake/later",
                    ),
                }
            ),
        )

        result = service.run(limit=10)

        self.assertEqual(result.queued, 2)
        self.assertEqual(result.claimed, 2)
        self.assertEqual(result.completed, 1)
        self.assertEqual(result.skipped_duplicates, 1)
        self.assertEqual(result.failed, 0)
        with self.database.connect() as connection:
            rows = {
                row["id"]: row
                for row in connection.execute(
                    """
                    SELECT id, extraction_status, extracted_by, extraction_error
                    FROM news
                    WHERE id IN (?, ?)
                    """,
                    (duplicate_id, later_id),
                )
            }
        self.assertEqual(rows[duplicate_id]["extraction_status"], "skipped")
        self.assertEqual(rows[duplicate_id]["extracted_by"], "rss-description-fallback")
        self.assertIn(f"news_id={existing_id}", rows[duplicate_id]["extraction_error"])
        self.assertEqual(rows[later_id]["extraction_status"], "completed")
        self.assertEqual(rows[later_id]["extracted_by"], "fake/later")

    def test_youtube_description_is_used_without_fetching_dynamic_video_page(self) -> None:
        description = "A public video description provides source context. " * 8
        news_id = self.repository.add_news(
            News(
                title="YouTube UAP report",
                original_title="YouTube UAP report",
                source="YouTube UAP Channel Watchlist",
                source_url="https://www.youtube.com/watch?v=video-1",
                canonical_url="https://www.youtube.com/watch?v=video-1",
                category=NewsCategory.OTHER,
                credibility=2,
                fact_status=FactStatus.SOURCE_REPORTED,
                raw_content=json.dumps({"description": description}),
            )
        )

        result = ArticleExtractionService(
            self.repository,
            MappingExtractor({}),
        ).run(limit=10)

        self.assertEqual(result.completed, 1)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT extraction_status, extracted_by, extracted_content FROM news WHERE id = ?",
                (news_id,),
            ).fetchone()
        self.assertEqual(row["extraction_status"], "completed")
        self.assertEqual(row["extracted_by"], "youtube-description-fallback")
        self.assertEqual(row["extracted_content"], " ".join(description.split()))

    def test_youtube_without_description_reports_caption_requirement(self) -> None:
        news_id = self.repository.add_news(
            News(
                title="YouTube video without description",
                original_title="YouTube video without description",
                source="YouTube UAP Channel Watchlist",
                source_url="https://www.youtube.com/watch?v=video-2",
                canonical_url="https://www.youtube.com/watch?v=video-2",
                category=NewsCategory.OTHER,
                credibility=2,
                fact_status=FactStatus.SOURCE_REPORTED,
                raw_content=json.dumps({"description": ""}),
            )
        )

        result = ArticleExtractionService(self.repository, MappingExtractor({})).run(limit=10)

        self.assertEqual(result.failed, 1)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT extraction_status, extraction_error FROM news WHERE id = ?",
                (news_id,),
            ).fetchone()
        self.assertEqual(row["extraction_status"], "failed")
        self.assertIn("captions are required", row["extraction_error"])

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

    @unittest.skipUnless(
        importlib.util.find_spec("pypdf"),
        "pypdf is not installed in this interpreter",
    )
    def test_pdf_extraction_supports_official_documents(self) -> None:
        from pypdf import PdfWriter

        output = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        writer.write(output)
        # The blank-PDF branch is covered by the short-content guard; use a
        # mocked reader to keep this unit test independent of PDF font layout.
        with patch("pypdf.PdfReader") as reader_class:
            reader_class.return_value.pages = [
                type("Page", (), {"extract_text": lambda self: "Official report content. " * 20})()
            ]
            reader_class.return_value.metadata = None
            article = TrafilaturaArticleExtractor().extract_pdf(output.getvalue(), url="https://example.test/report.pdf")
        self.assertIn("Official report content", article.content)
        self.assertEqual(article.extractor, "pypdf")

    def test_html_fallback_extracts_official_main_content(self) -> None:
        html = """
        <html><head><title>Official release</title></head>
        <body><nav>Navigation</nav><main><p>Official release content. </p>
        <p>This text is long enough to be analyzed safely. </p></main></body></html>
        """
        with patch.object(TrafilaturaArticleExtractor, "_module") as module:
            module.return_value.extract.return_value = None
            article = TrafilaturaArticleExtractor(minimum_characters=40).extract_html(
                html, url="https://example.test/release"
            )
        self.assertIn("Official release content", article.content)
        self.assertEqual(article.extractor, "html-fallback")

    def test_defense_release_uses_matching_official_pdf_after_http_failure(self) -> None:
        url = (
            "https://www.defense.gov/News/Releases/Release/Article/3964824/"
            "department-of-defense-releases-the-annual-report-on-unidentified-anomalous-phen/"
        )
        expected = type(
            "Extracted",
            (),
            {"content": "Official PDF content. " * 20, "extractor": "pypdf"},
        )()
        with patch.object(TrafilaturaArticleExtractor, "_module") as module:
            module.return_value.fetch_url.side_effect = RuntimeError("HTTP 403")
            with patch.object(
                TrafilaturaArticleExtractor,
                "extract_pdf_url",
                return_value=expected,
            ) as extract_pdf_url:
                article = TrafilaturaArticleExtractor().extract_url(url)
        extract_pdf_url.assert_called_once_with(
            "https://media.defense.gov/2024/Nov/14/2003583603/-1/-1/0/FY24-CONSOLIDATED-ANNUAL-REPORT-ON-UAP-508.PDF"
        )
        self.assertEqual(article.extractor, "pypdf")

    def test_defense_release_uses_official_fact_fallback_when_pdf_is_blocked(self) -> None:
        url = (
            "https://www.defense.gov/News/Releases/Release/Article/3964824/"
            "department-of-defense-releases-the-annual-report-on-unidentified-anomalous-phen"
        )
        with patch.object(TrafilaturaArticleExtractor, "_module") as module:
            module.return_value.fetch_url.return_value = None
            with patch.object(
                TrafilaturaArticleExtractor,
                "_fallback_download",
                side_effect=RuntimeError("HTTP 403"),
            ), patch.object(
                TrafilaturaArticleExtractor,
                "extract_pdf_url",
                side_effect=RuntimeError("HTTP 403"),
            ):
                article = TrafilaturaArticleExtractor().extract_url(url)
        self.assertEqual(article.extractor, "official-source-fallback")
        self.assertIn("757 reports", article.content)


if __name__ == "__main__":
    unittest.main()
