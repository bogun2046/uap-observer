from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from uap_platform.collectors import (
    CollectionResult,
    FetchClassification,
    FetchResponse,
    NormalizedItem,
    RssCollector,
    normalize_url,
    parse_rss,
)

RSS_FIXTURE = b"""
<rss version="2.0">
  <channel>
    <title>Example</title>
    <item>
      <guid>story-1</guid>
      <title> First story </title>
      <link>HTTPS://Example.test/news?id=7&amp;utm_source=feed</link>
      <pubDate>Sat, 15 Aug 2026 12:00:00 GMT</pubDate>
      <description>Summary one</description>
    </item>
    <item>
      <guid>story-1</guid>
      <title>Duplicate story</title>
      <link>https://example.test/news?id=7</link>
    </item>
    <item>
      <guid>story-2</guid>
      <title>Second story</title>
      <link>https://example.test/other?b=2&amp;a=1</link>
    </item>
    <item>
      <title>Invalid missing locator</title>
    </item>
  </channel>
</rss>
"""


def test_normalize_url_removes_tracking_and_sorts_query() -> None:
    assert normalize_url("HTTPS://Example.test:443/a?utm_medium=rss&b=2&a=1#fragment") == (
        "https://example.test/a?a=1&b=2"
    )


def test_parse_rss_is_deterministic_and_counts_duplicates() -> None:
    first = parse_rss(RSS_FIXTURE)
    second = parse_rss(RSS_FIXTURE)

    assert first == second
    assert first.parsed_count == 2
    assert first.duplicate_count == 1
    assert first.invalid_count == 1
    assert first.items[0].source_item_key == "story-1"
    assert first.items[0].canonical_url == "https://example.test/news?id=7"
    assert first.items[0].published_at == datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def test_fixed_snapshot_hash_is_stable() -> None:
    payload = (Path(__file__).parent / "fixtures/rss/g5-fixed-feed.xml").read_bytes()

    first = parse_rss(payload)
    second = parse_rss(payload)

    assert first == second
    assert first.snapshot_sha256 == (
        "b3a998a48fecd9c18bfb75d294a60465aad12a55490b1c72e6629ebcf9dd73c8"
    )


def test_parse_rss_deduplicates_different_ids_with_same_canonical_url() -> None:
    payload = b"""
    <rss><channel>
      <item><guid>first-id</guid><title>First</title>
        <link>https://example.test/story?utm_source=one</link></item>
      <item><guid>second-id</guid><title>Second</title>
        <link>https://example.test/story?utm_source=two</link></item>
    </channel></rss>
    """

    parsed = parse_rss(payload)

    assert parsed.parsed_count == 1
    assert parsed.duplicate_count == 1


def test_parse_atom_prefers_alternate_link_over_self() -> None:
    payload = b"""
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>atom-1</id><title>Atom story</title>
        <link rel="self" href="https://api.example.test/items/1" />
        <link rel="alternate" href="https://example.test/story/1" />
      </entry>
    </feed>
    """

    parsed = parse_rss(payload)

    assert parsed.items[0].canonical_url == "https://example.test/story/1"


def test_collector_sends_conditional_headers_and_persists_items() -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    persisted: list[tuple[object, ...]] = []

    def fetch(url: str, headers: Mapping[str, str]) -> FetchResponse:
        calls.append((url, dict(headers)))
        return FetchResponse(200, RSS_FIXTURE, {"ETag": "new-etag", "Last-Modified": "today"})

    def persist(items: tuple[NormalizedItem, ...]) -> int:
        persisted.append(items)
        return len(items)

    result = RssCollector(fetch, persist).collect(
        "https://example.test/feed", etag="old-etag", last_modified="yesterday"
    )

    assert result.classification is FetchClassification.SUCCESS
    assert result.persisted_count == 2
    assert result.etag == "new-etag"
    assert result.last_modified == "today"
    assert calls == [
        (
            "https://example.test/feed",
            {"If-None-Match": "old-etag", "If-Modified-Since": "yesterday"},
        )
    ]
    assert len(persisted) == 1


def test_collector_classifies_not_modified_without_persisting() -> None:
    persisted = False

    def persist(_items: tuple[object, ...]) -> int:
        nonlocal persisted
        persisted = True
        return 0

    result = RssCollector(
        lambda _url, _headers: FetchResponse(304, headers={"etag": "same"}), persist
    ).collect("https://example.test/feed")

    assert result.classification is FetchClassification.NOT_MODIFIED
    assert result.persisted_count == 0
    assert persisted is False


def test_collector_maps_connection_timeout_to_transient_failure() -> None:
    result = RssCollector(
        lambda _url, _headers: (_ for _ in ()).throw(TimeoutError("socket timed out")),
        lambda _items: 0,
    ).collect("https://example.test/feed")

    assert result.classification is FetchClassification.TRANSIENT_FAILURE
    assert result.http_status == 599
    assert result.error_code == "timeout"


def test_fetch_classification_covers_frozen_http_cases() -> None:
    assert FetchResponse(200, b" ").classify() is FetchClassification.EMPTY
    assert FetchResponse(403).classify() is FetchClassification.AUTHORIZATION_FAILURE
    assert FetchResponse(429).classify() is FetchClassification.RATE_LIMITED
    assert FetchResponse(408).classify() is FetchClassification.TRANSIENT_FAILURE
    assert FetchResponse(503).classify() is FetchClassification.TRANSIENT_FAILURE
    assert FetchResponse(404).classify() is FetchClassification.TERMINAL_FAILURE


def test_invalid_xml_becomes_terminal_collection_result() -> None:
    result = RssCollector(
        lambda _url, _headers: FetchResponse(200, b"<rss>"), lambda _items: 0
    ).collect("https://example.test/feed")

    assert isinstance(result, CollectionResult)
    assert result.classification is FetchClassification.TERMINAL_FAILURE
    assert result.error_code == "invalid_rss"
