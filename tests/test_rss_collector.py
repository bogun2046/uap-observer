from __future__ import annotations

import tempfile
import unittest
from http.client import IncompleteRead
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from uap_observer.collectors.rss import (
    FeedEntry,
    FeedResponse,
    HttpFeedFetcher,
    RssCollector,
    is_relevant,
    parse_feed,
)
from uap_observer.database import Database
from uap_observer.models import (
    FactStatus,
    NewsCategory,
    Source,
    SourceType,
)
from uap_observer.repositories import Repository
from uap_observer.source_config import load_sources
from uap_observer.url_utils import normalize_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RSS_PAYLOAD = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example</title>
    <item>
      <guid>uap-1</guid>
      <title>NASA discusses UAP research</title>
      <link>https://example.test/uap/?utm_source=rss&amp;b=2&amp;a=1</link>
      <pubDate>Mon, 27 Jul 2026 12:00:00 GMT</pubDate>
      <description><![CDATA[<p>Unidentified anomalous phenomena briefing.</p>]]></description>
    </item>
    <item>
      <guid>space-1</guid>
      <title>NASA announces lunar mission</title>
      <link>https://example.test/moon</link>
      <description>Unrelated mission news.</description>
    </item>
    <item>
      <guid>ml-1</guid>
      <title>UAP model benchmark</title>
      <link>https://example.test/ml</link>
      <description>Universal adversarial perturbation research.</description>
    </item>
  </channel>
</rss>
"""


class FakeFetcher:
    def __init__(self, response: FeedResponse) -> None:
        self.response = response
        self.calls: list[tuple[str | None, str | None]] = []

    def fetch(
        self,
        url: str,
        *,
        etag: str | None,
        last_modified: str | None,
    ) -> FeedResponse:
        self.calls.append((etag, last_modified))
        return self.response


class FakeHttpResponse:
    status = 200
    headers: dict[str, str] = {}

    def __init__(self, body: bytes | None = None, error: Exception | None = None) -> None:
        self.body = body
        self.error = error

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        if self.error:
            raise self.error
        return self.body or b""


class RssCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self.temp_directory.name) / "test.db",
            PROJECT_ROOT / "migrations",
        )
        self.database.initialize()
        self.repository = Repository(self.database)
        self.source = Source(
            slug="test-rss",
            name="Test RSS",
            source_type=SourceType.RSS,
            homepage_url="https://example.test/",
            feed_url="https://example.test/feed.xml",
            country="USA",
            language="en",
            default_category=NewsCategory.OFFICIAL_REPORT,
            default_credibility=5,
            default_fact_status=FactStatus.OFFICIAL_RECORD,
            include_keywords=["UAP", "unidentified anomalous phenomena"],
            exclude_keywords=["universal adversarial perturbation"],
        )
        self.source.id = self.repository.upsert_source(self.source)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_parse_and_collect_incrementally(self) -> None:
        fetcher = FakeFetcher(
            FeedResponse(
                status=200,
                body=RSS_PAYLOAD,
                etag='"v1"',
                last_modified="Mon, 27 Jul 2026 12:00:00 GMT",
            )
        )
        collector = RssCollector(self.repository, fetcher)

        first = collector.collect(self.source)
        refreshed_source = self.repository.get_sources(slug="test-rss")[0]
        second = collector.collect(refreshed_source)

        self.assertEqual(first.fetched, 3)
        self.assertEqual(first.inserted, 1)
        self.assertEqual(first.filtered, 2)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.duplicates, 1)
        self.assertEqual(fetcher.calls[1], ('"v1"', "Mon, 27 Jul 2026 12:00:00 GMT"))
        self.assertEqual(self.database.status().row_counts["news"], 1)

        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM news").fetchone()
        self.assertEqual(row["canonical_url"], "https://example.test/uap?a=1&b=2")
        self.assertEqual(row["feed_entry_id"], "uap-1")
        self.assertEqual(row["processing_status"], "pending")

    def test_304_response_records_no_new_items(self) -> None:
        collector = RssCollector(
            self.repository,
            FakeFetcher(FeedResponse(status=304, body=b"", etag='"v1"')),
        )

        result = collector.collect(self.source)

        self.assertTrue(result.not_modified)
        self.assertEqual(self.database.status().row_counts["news"], 0)

    def test_short_keyword_matches_whole_token_only(self) -> None:
        startup = FeedEntry(None, "NASA startup program", "https://example.test", None, None)
        uap = FeedEntry(None, "NASA UAP program", "https://example.test", None, None)

        self.assertFalse(is_relevant(startup, self.source))
        self.assertTrue(is_relevant(uap, self.source))

    def test_url_normalization_removes_tracking_and_fragment(self) -> None:
        normalized = normalize_url(
            "/story/?utm_medium=rss&z=2&a=1#section",
            base_url="HTTPS://Example.TEST/feed.xml",
        )
        self.assertEqual(normalized, "https://example.test/story?a=1&z=2")

    def test_version_controlled_source_registry_is_valid(self) -> None:
        sources = load_sources(PROJECT_ROOT / "config" / "sources.json")
        self.assertEqual(
            [source.slug for source in sources],
            [
                "x-uap",
                "nasa-recent",
                "nara-uap",
                "geipan-official",
                "the-debrief",
                "metabunk-ufo",
                "reddit-ufos",
                "reddit-aliens",
                "reddit-high-strangeness",
                "aaro-press-products",
                "aaro-case-resolutions",
                "aaro-official-imagery",
                "aaro-efoia",
            ],
        )
        self.assertEqual(sources[1].default_credibility, 5)
        by_slug = {source.slug: source for source in sources}
        self.assertEqual(by_slug["nara-uap"].default_credibility, 5)
        self.assertEqual(by_slug["geipan-official"].language, "fr")
        self.assertEqual(by_slug["the-debrief"].feed_url, "https://thedebrief.org/feed/")
        self.assertEqual(by_slug["metabunk-ufo"].default_category, NewsCategory.DISPUTED_EVENT)
        self.assertEqual(by_slug["reddit-ufos"].default_credibility, 1)
        self.assertEqual(by_slug["reddit-aliens"].default_credibility, 1)
        self.assertNotIn("alien", by_slug["reddit-aliens"].include_keywords)
        self.assertEqual(by_slug["reddit-high-strangeness"].default_credibility, 1)
        self.assertEqual(by_slug["aaro-press-products"].source_type, SourceType.WEB_PAGE)
        self.assertTrue(by_slug["aaro-official-imagery"].enabled)
        self.assertEqual(
            by_slug["aaro-case-resolutions"].default_category,
            NewsCategory.HISTORICAL_EVENT,
        )

    def test_parser_supports_atom(self) -> None:
        atom = b"""<feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>atom-1</id>
            <title>UAP update</title>
            <link href="https://example.test/atom" />
            <updated>2026-07-27T12:00:00Z</updated>
            <summary>Official update</summary>
          </entry>
        </feed>"""
        entries = parse_feed(atom)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].entry_id, "atom-1")
        self.assertEqual(entries[0].published_at, "2026-07-27T12:00:00Z")

    def test_http_fetcher_retries_incomplete_chunked_response(self) -> None:
        responses = [
            FakeHttpResponse(error=IncompleteRead(b"partial", 10)),
            FakeHttpResponse(body=RSS_PAYLOAD),
        ]
        with patch("urllib.request.urlopen", side_effect=responses) as urlopen:
            response = HttpFeedFetcher(max_retries=1).fetch(
                "https://example.test/feed.xml",
                etag=None,
                last_modified=None,
            )

        self.assertEqual(response.body, RSS_PAYLOAD)
        self.assertEqual(urlopen.call_count, 2)

    def test_http_fetcher_uses_curl_fallback_after_retries(self) -> None:
        incomplete = FakeHttpResponse(error=IncompleteRead(b"partial", 10))
        curl_result = CompletedProcess(
            args=["curl"],
            returncode=0,
            stdout=RSS_PAYLOAD,
            stderr=(
                b"HTTP/2 200\r\netag: \"curl-v1\"\r\n"
                b"last-modified: Mon, 27 Jul 2026 12:00:00 GMT\r\n\r\n"
                b"\nUAP_HTTP_STATUS:200\n"
            ),
        )
        with patch("urllib.request.urlopen", return_value=incomplete):
            with patch("shutil.which", return_value="/usr/bin/curl"):
                with patch("subprocess.run", return_value=curl_result) as run:
                    response = HttpFeedFetcher(max_retries=0).fetch(
                        "https://example.test/feed.xml",
                        etag=None,
                        last_modified=None,
                    )

        self.assertEqual(response.body, RSS_PAYLOAD)
        self.assertEqual(response.etag, '"curl-v1"')
        self.assertEqual(run.call_count, 1)

    def test_http_fetcher_parses_curl_status_written_to_stdout(self) -> None:
        incomplete = FakeHttpResponse(error=IncompleteRead(b"partial", 10))
        curl_result = CompletedProcess(
            args=["curl"],
            returncode=0,
            stdout=RSS_PAYLOAD + b"\nUAP_HTTP_STATUS:200\n",
            stderr=b"HTTP/2 200\r\netag: \"stdout-v1\"\r\n\r\n",
        )
        with patch("urllib.request.urlopen", return_value=incomplete):
            with patch("shutil.which", return_value="/usr/bin/curl"):
                with patch("subprocess.run", return_value=curl_result):
                    response = HttpFeedFetcher(max_retries=0).fetch(
                        "https://example.test/feed.xml",
                        etag=None,
                        last_modified=None,
                    )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, RSS_PAYLOAD)
        self.assertEqual(response.etag, '"stdout-v1"')


if __name__ == "__main__":
    unittest.main()
