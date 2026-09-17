"""RSS/Atom fetching and incremental persistence."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol

from uap_observer.http_fetch import HttpFetcher, cooldown_seconds_for_error
from uap_observer.models import News, Source
from uap_observer.repositories import Repository
from uap_observer.url_utils import normalize_url

TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class FeedResponse:
    status: int
    body: bytes
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class FeedEntry:
    entry_id: str | None
    title: str
    link: str
    published_at: str | None
    description: str | None


@dataclass(frozen=True)
class CollectionResult:
    source_slug: str
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    filtered: int = 0
    invalid: int = 0
    not_modified: bool = False


class FeedFetcher(Protocol):
    def fetch(
        self,
        url: str,
        *,
        etag: str | None,
        last_modified: str | None,
    ) -> FeedResponse:
        ...


class HttpFeedFetcher:
    def __init__(
        self,
        timeout: float = 20.0,
        max_retries: int = 2,
        allow_curl_fallback: bool = True,
    ) -> None:
        self._fetcher = HttpFetcher(
            timeout=timeout,
            max_retries=max_retries,
            allow_curl_fallback=allow_curl_fallback,
        )

    def fetch(
        self,
        url: str,
        *,
        etag: str | None,
        last_modified: str | None,
    ) -> FeedResponse:
        response = self._fetcher.fetch(
            url,
            accept="application/rss+xml, application/atom+xml",
            etag=etag,
            last_modified=last_modified,
            accept_partial=False,
        )
        return FeedResponse(
            status=response.status,
            body=response.body,
            etag=response.etag,
            last_modified=response.last_modified,
        )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element: ET.Element, names: set[str]) -> str | None:
    for child in element:
        if _local_name(child.tag) in names and child.text:
            return child.text.strip()
    return None


def _entry_link(element: ET.Element) -> str | None:
    for child in element:
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if href and child.attrib.get("rel", "alternate") in {"alternate", ""}:
            return href.strip()
        if child.text:
            return child.text.strip()
    return None


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = html.unescape(TAG_RE.sub(" ", value))
    cleaned = " ".join(cleaned.split())
    return cleaned or None


def _normalize_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_feed(payload: bytes) -> list[FeedEntry]:
    root = ET.fromstring(payload)
    elements = [element for element in root.iter() if _local_name(element.tag) in {"item", "entry"}]
    entries: list[FeedEntry] = []
    for element in elements:
        title = _clean_text(_child_text(element, {"title"}))
        link = _entry_link(element)
        if not title or not link:
            continue
        entries.append(
            FeedEntry(
                entry_id=_child_text(element, {"guid", "id"}),
                title=title,
                link=link,
                published_at=_normalize_date(
                    _child_text(element, {"pubdate", "published", "updated", "date"})
                ),
                description=_clean_text(
                    _child_text(element, {"description", "summary", "content", "encoded"})
                ),
            )
        )
    return entries


def is_relevant(entry: FeedEntry, source: Source) -> bool:
    searchable = f"{entry.title}\n{entry.description or ''}".casefold()

    def matches(keyword: str) -> bool:
        folded = keyword.casefold().strip()
        if not folded:
            return False
        if folded.isalnum() and len(folded) <= 4:
            return re.search(rf"(?<!\w){re.escape(folded)}(?!\w)", searchable) is not None
        return folded in searchable

    if any(matches(keyword) for keyword in source.exclude_keywords):
        return False
    return not source.include_keywords or any(matches(keyword) for keyword in source.include_keywords)


class RssCollector:
    def __init__(self, repository: Repository, fetcher: FeedFetcher | None = None) -> None:
        self.repository = repository
        self.fetcher = fetcher or HttpFeedFetcher()

    def collect(self, source: Source, *, limit: int | None = None) -> CollectionResult:
        if source.id is None or not source.feed_url:
            raise ValueError("Persisted RSS source with feed_url is required")
        run_id = self.repository.start_source_run(source.id)
        try:
            response = self.fetcher.fetch(
                source.feed_url,
                etag=source.etag,
                last_modified=source.last_modified,
            )
            if response.status == 304:
                self.repository.record_source_fetch(
                    source.id,
                    etag=response.etag,
                    last_modified=response.last_modified,
                )
                self.repository.finish_source_run(
                    run_id,
                    status="not_modified",
                    http_status=304,
                )
                return CollectionResult(source_slug=source.slug, not_modified=True)

            entries = parse_feed(response.body)
            if limit is not None:
                entries = entries[:limit]
            inserted = duplicates = filtered = invalid = 0
            for entry in entries:
                if not is_relevant(entry, source):
                    filtered += 1
                    continue
                try:
                    canonical_url = normalize_url(entry.link, base_url=source.feed_url)
                except ValueError:
                    invalid += 1
                    continue
                entry_id = entry.entry_id or canonical_url
                if self.repository.news_exists(
                    canonical_url=canonical_url,
                    source_id=source.id,
                    feed_entry_id=entry_id,
                ):
                    duplicates += 1
                    continue
                self.repository.add_news(
                    News(
                        title=entry.title,
                        original_title=entry.title,
                        source=source.name,
                        source_url=entry.link,
                        canonical_url=canonical_url,
                        publish_date=entry.published_at,
                        country=source.country,
                        category=source.default_category,
                        credibility=source.default_credibility,
                        fact_status=source.default_fact_status,
                        raw_content=entry.description,
                        source_id=source.id,
                        feed_entry_id=entry_id,
                    )
                )
                inserted += 1

            self.repository.record_source_fetch(
                source.id,
                etag=response.etag,
                last_modified=response.last_modified,
            )
            self.repository.finish_source_run(
                run_id,
                status="success" if entries else "empty",
                http_status=response.status,
                fetched_count=len(entries),
                parsed_count=len(entries),
                inserted_count=inserted,
                duplicate_count=duplicates,
                filtered_count=filtered,
                invalid_count=invalid,
            )
            return CollectionResult(
                source_slug=source.slug,
                fetched=len(entries),
                inserted=inserted,
                duplicates=duplicates,
                filtered=filtered,
                invalid=invalid,
            )
        except Exception as error:
            message = str(error)[:1000]
            self.repository.record_source_fetch(
                source.id,
                error=message,
                cooldown_seconds=cooldown_seconds_for_error(error),
            )
            self.repository.finish_source_run(
                run_id,
                status="failed",
                http_status=getattr(error, "status", None),
                error=message,
            )
            raise
