from __future__ import annotations

import http.client
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from uap_observer.collectors.web_pages import (
    AaroCaseCollector,
    AaroCaseParser,
    AaroCollector,
    AaroReleaseParser,
    GenericWebPageParser,
    HttpWebPageFetcher,
    WebPageResponse,
)
from uap_observer.database import Database
from uap_observer.http_fetch import FetchError
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

GENERIC_HTML = """
<html>
  <head><title>Chile SEFAA official records</title></head>
  <body>
    <nav><a href="/menu">UAP menu</a></nav>
    <main>
      <h1>Fenómenos Aéreos Anómalos</h1>
      <p>This official page describes the public process for reviewing
      anomalous aerial phenomena reports and preserving source documents for
      later review by researchers.</p>
      <a href="/reports/ovni-2026.pdf">Informe OVNI 2026</a>
      <a href="/about">About this website</a>
    </main>
  </body>
</html>
""".encode()

SINGLE_PAGE_HTML = b"""
<html>
  <head><title>Official UAP archive</title></head>
  <body>
    <main>
      <h1>Official UAP archive</h1>
      <p>This public archive page records official material about unidentified
      aerial phenomena and explains how the records can be reviewed.</p>
    </main>
  </body>
</html>
"""


class FakeFetcher:
    def __init__(self, response: WebPageResponse) -> None:
        self.response = response

    def fetch(self, url: str, *, etag: str | None, last_modified: str | None) -> WebPageResponse:
        return self.response


class FailingFetcher:
    def fetch(self, url: str, *, etag: str | None, last_modified: str | None) -> WebPageResponse:
        raise RuntimeError("edge HTTP 403")


class ForbiddenThenFallbackFetcher:
    def __init__(self, primary_url: str, body: bytes) -> None:
        self.primary_url = primary_url
        self.body = body
        self.calls: list[str] = []

    def fetch(self, url: str, *, etag: str | None, last_modified: str | None) -> WebPageResponse:
        self.calls.append(url)
        if url == self.primary_url:
            raise FetchError("primary edge returned HTTP 403", status=403)
        return WebPageResponse(status=200, body=self.body)


class PartialResponse:
    status = 200

    def __init__(self) -> None:
        self.headers = {"ETag": '"partial"'}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self) -> bytes:
        raise http.client.IncompleteRead(GENERIC_HTML, 100)


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

    def test_fetch_failure_is_persisted_for_source_status(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "edge HTTP 403"):
            AaroCollector(self.repository, fetcher=FailingFetcher()).collect(self.source)
        stored = self.repository.get_sources(slug="aaro-test")[0]
        self.assertEqual(stored.last_error, "edge HTTP 403")
        self.assertIsNone(stored.last_success_at)
        run = self.repository.get_latest_source_runs()[self.source.id]
        self.assertEqual(run["status"], "failed")
        self.assertIn("403", run["error"])

    def test_http_fetcher_uses_non_empty_partial_response(self) -> None:
        with patch(
            "uap_observer.http_fetch.urllib.request.urlopen",
            return_value=PartialResponse(),
        ):
            response = HttpWebPageFetcher().fetch(
                self.source.homepage_url,
                etag=None,
                last_modified=None,
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, GENERIC_HTML)
        self.assertEqual(response.etag, '"partial"')

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
        run = self.repository.get_latest_source_runs()[self.source.id]
        self.assertEqual(run["status"], "success")
        self.assertEqual(run["parsed_count"], 2)
        self.assertEqual(run["inserted_count"], 0)
        with self.database.connect() as connection:
            first_run = connection.execute(
                "SELECT inserted_count FROM source_runs WHERE source_id = ? ORDER BY id LIMIT 1",
                (self.source.id,),
            ).fetchone()
        self.assertEqual(first_run["inserted_count"], 2)

    def test_collector_handles_not_modified(self) -> None:
        result = AaroCollector(
            self.repository,
            FakeFetcher(WebPageResponse(status=304, body=b"")),
        ).collect(self.source)

        self.assertTrue(result.not_modified)
        self.assertEqual(result.fetched, 0)
        run = self.repository.get_latest_source_runs()[self.source.id]
        self.assertEqual(run["status"], "not_modified")
        self.assertEqual(run["http_status"], 304)

    def test_collector_uses_official_fallback_after_403(self) -> None:
        fallback_url = "https://fallback.example/uap"
        self.source.fallback_urls = [fallback_url]
        self.source.include_keywords = ["uap"]
        self.repository.upsert_source(self.source)
        fetcher = ForbiddenThenFallbackFetcher(self.source.homepage_url, SINGLE_PAGE_HTML)

        result = AaroCollector(self.repository, fetcher=fetcher).collect(self.source)

        self.assertEqual(result.inserted, 1)
        self.assertEqual(fetcher.calls, [self.source.homepage_url, fallback_url])
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT source_url FROM news WHERE source_id = ?",
                (self.source.id,),
            ).fetchone()
        self.assertEqual(row["source_url"], fallback_url)
        stored = self.repository.get_sources(slug="aaro-test")[0]
        self.assertIsNone(stored.last_error)
        self.assertIsNotNone(stored.last_success_at)

    def test_generic_parser_handles_link_pages_and_ignores_navigation(self) -> None:
        parser = GenericWebPageParser(include_keywords=["ovni", "fenómenos aéreos anómalos"])

        records = parser.parse(
            GENERIC_HTML,
            base_url="https://sefaa.example/",
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].title, "Informe OVNI 2026")
        self.assertEqual(records[0].source_url, "https://sefaa.example/reports/ovni-2026.pdf")

    def test_generic_parser_can_record_a_single_official_page(self) -> None:
        records = GenericWebPageParser(include_keywords=["uap"]).parse(
            SINGLE_PAGE_HTML,
            base_url="https://archive.example/uap",
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source_url, "https://archive.example/uap")
        self.assertIn("public archive", records[0].description or "")

    def test_generic_parser_ignores_dvids_navigation_and_recovers_video_title(self) -> None:
        body = b"""
        <html><body>
          <a href="/forgotpassword?uap=1">Forgot Password?</a>
          <a href="/search?page=2">VIEW MORE</a>
          <a href="/video/123/uap-pr49-unresolved-report">Visit video Page</a>
        </body></html>
        """

        records = GenericWebPageParser(include_keywords=["uap"]).parse(
            body,
            base_url="https://www.dvidshub.net/search/",
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].title, "UAP PR49 Unresolved Report")
        self.assertEqual(
            records[0].source_url,
            "https://www.dvidshub.net/video/123/uap-pr49-unresolved-report",
        )

    def test_collector_uses_generic_parser_for_non_aaro_pages(self) -> None:
        source = Source(
            slug="international-test",
            name="International Test Source",
            source_type=SourceType.WEB_PAGE,
            homepage_url="https://sefaa.example/",
            default_category=NewsCategory.OFFICIAL_REPORT,
            default_credibility=5,
            default_fact_status=FactStatus.OFFICIAL_RECORD,
            include_keywords=["ovni", "fenómenos aéreos anómalos"],
        )
        source.id = self.repository.upsert_source(source)

        result = AaroCollector(
            self.repository,
            FakeFetcher(WebPageResponse(status=200, body=GENERIC_HTML)),
        ).collect(source)

        self.assertEqual(result.inserted, 1)
        run = self.repository.get_latest_source_runs()[source.id]
        self.assertEqual(run["parsed_count"], 1)
        self.assertEqual(run["inserted_count"], 1)

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

    def test_case_collector_uses_generic_official_fallback_after_403(self) -> None:
        fallback_url = "https://fallback.example/aaro-videos"
        source = Source(
            slug="aaro-case-fallback-test",
            name="AARO Case Fallback",
            source_type=SourceType.WEB_PAGE,
            homepage_url="https://www.aaro.mil/cases",
            fallback_urls=[fallback_url],
            default_category=NewsCategory.HISTORICAL_EVENT,
            default_credibility=5,
            default_fact_status=FactStatus.OFFICIAL_RECORD,
            include_keywords=["ovni"],
        )
        source.id = self.repository.upsert_source(source)
        fetcher = ForbiddenThenFallbackFetcher(source.homepage_url, GENERIC_HTML)

        result = AaroCaseCollector(self.repository, fetcher=fetcher).collect(source)

        self.assertEqual(result.inserted, 1)
        self.assertEqual(result.events_inserted, 1)
        self.assertEqual(fetcher.calls, [source.homepage_url, fallback_url])
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT source_url, original_title FROM news WHERE source_id = ?",
                (source.id,),
            ).fetchone()
        self.assertEqual(row["source_url"], "https://fallback.example/reports/ovni-2026.pdf")
        self.assertEqual(row["original_title"], "Informe OVNI 2026")


if __name__ == "__main__":
    unittest.main()
