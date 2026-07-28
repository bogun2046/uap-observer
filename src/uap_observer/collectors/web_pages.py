"""Collectors for official HTML release tables that do not expose RSS feeds."""

from __future__ import annotations

import hashlib
import html
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Protocol

from uap_observer.models import Event, EventStatus, News, Source, SourceType
from uap_observer.repositories import Repository
from uap_observer.url_utils import normalize_url

USER_AGENT = "UAPObserver/0.1 (+public-source research; contact via repository)"
DATE_PATTERN = re.compile(r"\b(?:\d{1,2}/\d{1,2}/\d{4}|\d{4})\b")
MONTH_DATE_PATTERN = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},\s+\d{4}\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WebPageResponse:
    status: int
    body: bytes
    etag: str | None = None
    last_modified: str | None = None


class WebPageFetcher(Protocol):
    def fetch(self, url: str, *, etag: str | None, last_modified: str | None) -> WebPageResponse:
        """Fetch an HTML page with optional conditional validators."""


class HttpWebPageFetcher:
    def __init__(self, *, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def fetch(self, url: str, *, etag: str | None, last_modified: str | None) -> WebPageResponse:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return WebPageResponse(
                    status=response.status,
                    body=response.read(),
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                )
        except urllib.error.HTTPError as error:
            if error.code == 304:
                return WebPageResponse(
                    status=304,
                    body=b"",
                    etag=error.headers.get("ETag"),
                    last_modified=error.headers.get("Last-Modified"),
                )
            raise


@dataclass(frozen=True)
class AaroRecord:
    title: str
    source_url: str
    publish_date: str | None
    description: str | None


@dataclass(frozen=True)
class AaroCaseRecord:
    case_name: str
    source_url: str
    description: str
    date_start: str | None


@dataclass(frozen=True)
class WebCollectionResult:
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    invalid: int = 0
    events_inserted: int = 0
    not_modified: bool = False


class AaroReleaseParser:
    """Parse linked rows from AARO's Congressional/Press Products tables."""

    def parse(self, body: bytes, *, base_url: str) -> list[AaroRecord]:
        parser = _TableParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        records: list[AaroRecord] = []
        for cells in parser.rows:
            links = [cell for cell in cells if cell.href]
            if not links:
                continue
            link = links[-1]
            title = _clean_text(link.text)
            if not title or title.lower() in {"report/brief", "paper"}:
                continue
            source_url = normalize_url(link.href or "", base_url=base_url)
            if not source_url:
                continue
            cell_text = [_clean_text(cell.text) for cell in cells]
            date_value = _extract_date(cell_text)
            description = " | ".join(value for value in cell_text if value and value != title)
            records.append(
                AaroRecord(
                    title=title,
                    source_url=source_url,
                    publish_date=date_value,
                    description=description or None,
                )
            )
        return _unique_records(records)


class AaroCaseParser:
    """Parse AARO case-resolution rows and retain the official assessment text."""

    def parse(self, body: bytes, *, base_url: str) -> list[AaroCaseRecord]:
        parser = _TableParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        records: list[AaroCaseRecord] = []
        for cells in parser.rows:
            if len(cells) < 2:
                continue
            links = [cell for cell in cells if cell.href]
            if not links:
                continue
            case_name = _clean_text(cells[0].text)
            description = _clean_text(cells[1].text)
            if not case_name or case_name.lower() in {"name", "description"} or not description:
                continue
            records.append(
                AaroCaseRecord(
                    case_name=case_name,
                    source_url=normalize_url(links[0].href or "", base_url=base_url),
                    description=description,
                    date_start=_extract_date([description]),
                )
            )
        return _unique_case_records(records)


class AaroCollector:
    def __init__(
        self,
        repository: Repository,
        fetcher: WebPageFetcher | None = None,
        parser: AaroReleaseParser | None = None,
    ) -> None:
        self.repository = repository
        self.fetcher = fetcher or HttpWebPageFetcher()
        self.parser = parser or AaroReleaseParser()

    def collect(self, source: Source, *, limit: int | None = None) -> WebCollectionResult:
        if source.source_type is not SourceType.WEB_PAGE:
            raise ValueError(f"Source {source.slug!r} is not a web page source")
        response = self.fetcher.fetch(
            source.homepage_url,
            etag=source.etag,
            last_modified=source.last_modified,
        )
        self.repository.record_source_fetch(
            source.id or 0,
            etag=response.etag,
            last_modified=response.last_modified,
        )
        if response.status == 304:
            return WebCollectionResult(not_modified=True)
        if response.status != 200:
            raise RuntimeError(f"Web page returned HTTP {response.status}")

        records = self.parser.parse(response.body, base_url=source.homepage_url)
        if limit is not None:
            records = records[:limit]
        inserted = duplicates = invalid = 0
        for record in records:
            if not record.title or not record.source_url:
                invalid += 1
                continue
            if self.repository.news_exists(
                canonical_url=record.source_url,
                source_id=source.id,
                feed_entry_id=_record_id(record.source_url),
            ):
                duplicates += 1
                continue
            self.repository.add_news(
                News(
                    title=record.title,
                    original_title=record.title,
                    source=source.name,
                    source_url=record.source_url,
                    canonical_url=record.source_url,
                    publish_date=record.publish_date,
                    country=source.country,
                    category=source.default_category,
                    credibility=source.default_credibility,
                    fact_status=source.default_fact_status,
                    summary=record.description,
                    source_id=source.id,
                    feed_entry_id=_record_id(record.source_url),
                )
            )
            inserted += 1
        return WebCollectionResult(
            fetched=len(records),
            inserted=inserted,
            duplicates=duplicates,
            invalid=invalid,
        )


class AaroCaseCollector:
    def __init__(
        self,
        repository: Repository,
        fetcher: WebPageFetcher | None = None,
        parser: AaroCaseParser | None = None,
    ) -> None:
        self.repository = repository
        self.fetcher = fetcher or HttpWebPageFetcher()
        self.parser = parser or AaroCaseParser()

    def collect(self, source: Source, *, limit: int | None = None) -> WebCollectionResult:
        if source.source_type is not SourceType.WEB_PAGE:
            raise ValueError(f"Source {source.slug!r} is not a web page source")
        response = self.fetcher.fetch(
            source.homepage_url,
            etag=source.etag,
            last_modified=source.last_modified,
        )
        self.repository.record_source_fetch(
            source.id or 0,
            etag=response.etag,
            last_modified=response.last_modified,
        )
        if response.status == 304:
            return WebCollectionResult(not_modified=True)
        if response.status != 200:
            raise RuntimeError(f"Web page returned HTTP {response.status}")

        records = self.parser.parse(response.body, base_url=source.homepage_url)
        if limit is not None:
            records = records[:limit]
        inserted = duplicates = invalid = events_inserted = 0
        for record in records:
            if not record.case_name or not record.source_url:
                invalid += 1
                continue
            record_id = _record_id(record.source_url)
            if self.repository.news_exists(
                canonical_url=record.source_url,
                source_id=source.id,
                feed_entry_id=record_id,
            ):
                duplicates += 1
                continue
            self.repository.add_news(
                News(
                    title=record.case_name,
                    original_title=record.case_name,
                    source=source.name,
                    source_url=record.source_url,
                    canonical_url=record.source_url,
                    publish_date=record.date_start,
                    country=source.country,
                    category=source.default_category,
                    credibility=source.default_credibility,
                    fact_status=source.default_fact_status,
                    summary=record.description,
                    source_id=source.id,
                    feed_entry_id=record_id,
                )
            )
            if not self.repository.event_exists(
                event_name=record.case_name,
                date_start=record.date_start,
            ):
                self.repository.add_event(
                    Event(
                        event_name=record.case_name,
                        date_start=record.date_start,
                        country=source.country,
                        description=record.description,
                        status=EventStatus.OFFICIAL_RECORD,
                        credibility=source.default_credibility,
                    )
                )
                events_inserted += 1
            inserted += 1
        return WebCollectionResult(
            fetched=len(records),
            inserted=inserted,
            duplicates=duplicates,
            invalid=invalid,
            events_inserted=events_inserted,
        )


@dataclass
class _Cell:
    text: str
    href: str | None = None


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_Cell]] = []
        self._row: list[_Cell] | None = None
        self._cell_text: list[str] | None = None
        self._cell_href: str | None = None
        self._cell_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell_text = []
            self._cell_href = None
            self._cell_tag = tag
        elif tag == "a" and self._cell_text is not None:
            self._cell_href = self._cell_href or dict(attrs).get("href")

    def handle_data(self, data: str) -> None:
        if self._cell_text is not None:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell_tag == tag and self._row is not None:
            self._row.append(_Cell(" ".join(self._cell_text or []), self._cell_href))
            self._cell_text = None
            self._cell_href = None
            self._cell_tag = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _clean_text(value: str) -> str:
    return " ".join(html.unescape(value).split())


def _extract_date(values: list[str]) -> str | None:
    for value in values:
        month_match = MONTH_DATE_PATTERN.search(value)
        if month_match:
            parsed = datetime.strptime(month_match.group(0).title(), "%B %d, %Y").replace(
                tzinfo=timezone.utc
            )
            return parsed.date().isoformat()
        match = DATE_PATTERN.search(value)
        if not match:
            continue
        raw = match.group(0)
        if "/" in raw:
            month, day, year = raw.split("/")
            return f"{year}-{int(month):02d}-{int(day):02d}"
        return raw
    return None


def _record_id(source_url: str) -> str:
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:32]


def _unique_records(records: list[AaroRecord]) -> list[AaroRecord]:
    seen: set[str] = set()
    unique: list[AaroRecord] = []
    for record in records:
        if record.source_url in seen:
            continue
        seen.add(record.source_url)
        unique.append(record)
    return unique


def _unique_case_records(records: list[AaroCaseRecord]) -> list[AaroCaseRecord]:
    seen: set[str] = set()
    unique: list[AaroCaseRecord] = []
    for record in records:
        if record.source_url in seen:
            continue
        seen.add(record.source_url)
        unique.append(record)
    return unique
