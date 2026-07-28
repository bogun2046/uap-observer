from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from uap_observer.collectors.web_pages import (
    AaroCaseCollector,
    AaroCaseParser,
    AaroCollector,
    AaroReleaseParser,
    WebPageResponse,
)
from uap_observer.database import Database
from uap_observer.models import FactStatus, NewsCategory, Source, SourceType
from uap_observer.repositories import Repository

PROJECT_ROOT = Path(__file__).resolve().parents[1]

HTML = b"""
<html><body>
<table>
  <tr><th>Year</th><th>Topic</th><th>Report/Brief</th></tr>
  <tr><td>2025</td><td>FY25 UAP Annual Report</td>
      <td><a href="/reports/fy25.pdf">Fiscal Year 2025 Consolidated Annual Report</a></td></tr>
  <tr><td>2024</td><td>Open Hearing</td>
      <td><a href="https://example.test/hearing">Open Hearing</a></td></tr>
</table>
<table>
  <tr><th>Date</th><th>Category</th><th>Paper</th></tr>
  <tr><td>02/27/2026</td><td>PRESS RELEASES</td>
      <td><a href="/press/2026">AARO press release</a></td></tr>
</table>
</body></html>
"""

CASE_HTML = b"""
<table>
  <tr><th>Name</th><th>Description</th><th>Links</th></tr>
  <tr><td>Al Taqaddam Case Resolution</td>
      <td>On October 23, 2017, an infrared sensor recorded an object.
      AARO assesses with high confidence that it was consistent with balloons.</td>
      <td><a href="/cases/al-taqaddam.pdf">Al Taqaddam Case Resolution</a>
      <a href="https://example.test/video">Object Video</a></td></tr>
</table>
"""


class FakeFetcher:
    def __init__(self, response: WebPageResponse) -> None:
        self.response = response

    def fetch(self, url: str, *, etag: str | None, last_modified: str | None) -> WebPageResponse:
        return self.response


class WebPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self.temp_directory.name) / "test.db",
            PROJECT_ROOT / "migrations",
        )
        self.database.initialize()
        self.repository = Repository(self.database)
        self.source = Source(
            slug="aaro-test",
            name="AARO Test",
            source_type=SourceType.WEB_PAGE,
            homepage_url="https://www.aaro.mil/Congressional-Press-Products/",
            default_category=NewsCategory.OFFICIAL_REPORT,
            default_credibility=5,
            default_fact_status=FactStatus.OFFICIAL_RECORD,
        )
        self.source.id = self.repository.upsert_source(self.source)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_parser_extracts_links_dates_and_relative_urls(self) -> None:
        records = AaroReleaseParser().parse(
            HTML,
            base_url=self.source.homepage_url,
        )

        self.assertEqual(len(records), 3)
        self.assertEqual(records[0].publish_date, "2025")
        self.assertEqual(records[0].source_url, "https://www.aaro.mil/reports/fy25.pdf")
        self.assertEqual(records[2].publish_date, "2026-02-27")

    def test_collector_inserts_and_deduplicates_records(self) -> None:
        collector = AaroCollector(
            self.repository,
            FakeFetcher(WebPageResponse(status=200, body=HTML, etag='"v1"')),
        )
        first = collector.collect(self.source, limit=2)
        second = collector.collect(self.source, limit=2)

        self.assertEqual(first.fetched, 2)
        self.assertEqual(first.inserted, 2)
        self.assertEqual(second.duplicates, 2)
        self.assertEqual(self.database.status().row_counts["news"], 2)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT source, source_url, category, fact_status FROM news ORDER BY id LIMIT 1"
            ).fetchone()
        self.assertEqual(row["source"], "AARO Test")
        self.assertEqual(row["category"], "official_report")
        self.assertEqual(row["fact_status"], "official_record")

    def test_collector_handles_not_modified(self) -> None:
        result = AaroCollector(
            self.repository,
            FakeFetcher(WebPageResponse(status=304, body=b"")),
        ).collect(self.source)

        self.assertTrue(result.not_modified)
        self.assertEqual(result.fetched, 0)

    def test_case_parser_extracts_assessment_and_first_resolution_link(self) -> None:
        records = AaroCaseParser().parse(
            CASE_HTML,
            base_url="https://www.aaro.mil/UAP-Cases/UAP-Case-Resolution-Reports/",
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].case_name, "Al Taqaddam Case Resolution")
        self.assertEqual(records[0].date_start, "2017-10-23")
        self.assertIn("balloons", records[0].description)
        self.assertEqual(
            records[0].source_url,
            "https://www.aaro.mil/cases/al-taqaddam.pdf",
        )

    def test_case_collector_creates_news_and_event_once(self) -> None:
        source = Source(
            slug="aaro-case-test",
            name="AARO Cases",
            source_type=SourceType.WEB_PAGE,
            homepage_url="https://www.aaro.mil/UAP-Cases/UAP-Case-Resolution-Reports/",
            default_category=NewsCategory.HISTORICAL_EVENT,
            default_credibility=5,
            default_fact_status=FactStatus.OFFICIAL_RECORD,
        )
        source.id = self.repository.upsert_source(source)
        collector = AaroCaseCollector(
            self.repository,
            FakeFetcher(WebPageResponse(status=200, body=CASE_HTML)),
        )
        first = collector.collect(source)
        second = collector.collect(source)

        self.assertEqual(first.inserted, 1)
        self.assertEqual(first.events_inserted, 1)
        self.assertEqual(second.duplicates, 1)
        self.assertEqual(self.database.status().row_counts["events"], 1)


if __name__ == "__main__":
    unittest.main()
