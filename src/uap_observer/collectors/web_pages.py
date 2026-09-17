"""Collectors for official HTML release tables that do not expose RSS feeds."""

from __future__ import annotations

import hashlib
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import ClassVar, Protocol
from urllib.parse import unquote, urlparse

from uap_observer.http_fetch import FetchError, HttpFetcher, cooldown_seconds_for_error
from uap_observer.models import Event, EventStatus, News, Source, SourceType
from uap_observer.repositories import Repository
from uap_observer.url_utils import normalize_url

LOGGER = logging.getLogger(__name__)

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
        self._fetcher = HttpFetcher(timeout=timeout)

    def fetch(self, url: str, *, etag: str | None, last_modified: str | None) -> WebPageResponse:
        response = self._fetcher.fetch(
            url,
            accept="text/html,application/xhtml+xml",
            etag=etag,
            last_modified=last_modified,
            accept_partial=True,
        )
        return WebPageResponse(
            status=response.status,
            body=response.body,
            etag=response.etag,
            last_modified=response.last_modified,
        )


def _fetch_primary_or_fallback(
    fetcher: WebPageFetcher,
    source: Source,
) -> tuple[WebPageResponse, str]:
    """Use configured official fallbacks only when the primary returns HTTP 403."""

    primary_error: Exception | None = None
    try:
        response = fetcher.fetch(
            source.homepage_url,
            etag=source.etag,
            last_modified=source.last_modified,
        )
    except FetchError as error:
        if getattr(error, "status", None) != 403 or not source.fallback_urls:
            raise
        primary_error = error
    else:
        if response.status != 403:
            return response, source.homepage_url
        if not source.fallback_urls:
            return response, source.homepage_url
        primary_error = FetchError(
            f"primary web page returned HTTP {response.status}",
            status=response.status,
        )

    for fallback_url in source.fallback_urls:
        if not fallback_url or fallback_url == source.homepage_url:
            continue
        try:
            response = fetcher.fetch(
                fallback_url,
                etag=None,
                last_modified=None,
            )
        except FetchError as error:
            LOGGER.debug("AARO fallback URL failed: %s", fallback_url, exc_info=error)
            continue
        if response.status in {200, 304}:
            return response, fallback_url

    if primary_error is not None:
        raise primary_error
    raise FetchError("primary web page returned HTTP 403", status=403)


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


class GenericWebPageParser:
    """Parse link lists and single official pages when no RSS exists.

    The original web collector assumed every source page was an AARO-style
    table.  This parser keeps table support first, then handles ordinary
    article/list pages and finally records a meaningful source page itself.
    It deliberately ignores navigation and social links so a homepage does
    not become a collection of unrelated menu records.
    """

    def __init__(self, *, include_keywords: list[str] | None = None) -> None:
        self.include_keywords = include_keywords or []

    def parse(self, body: bytes, *, base_url: str) -> list[AaroRecord]:
        table_records = AaroReleaseParser().parse(body, base_url=base_url)
        if table_records:
            return _unique_records(
                [
                    record
                    for record in table_records
                    if _matches_source_keywords(
                        f"{record.title}\n{record.description or ''}",
                        self.include_keywords,
                    )
                ]
            )

        parser = _DocumentParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        document_text = _clean_text(" ".join(parser.content_parts))
        page_title = parser.title or (parser.headings[0] if parser.headings else "")
        records: list[AaroRecord] = []
        for link in parser.links:
            title = _clean_text(link.text)
            if not _is_candidate_link(title, link.href):
                continue
            if not _matches_source_keywords(
                f"{title}\n{link.href}",
                self.include_keywords,
            ):
                continue
            try:
                source_url = normalize_url(link.href, base_url=base_url)
            except ValueError:
                continue
            if not source_url or source_url == normalize_url(base_url):
                continue
            title = _title_for_link(title, source_url)
            records.append(
                AaroRecord(
                    title=title,
                    source_url=source_url,
                    publish_date=_extract_date([title, document_text]),
                    description=None,
                )
            )
        if records:
            return _unique_records(records)

        if (
            not page_title
            or len(document_text) < 80
            or not _matches_source_keywords(
                f"{page_title}\n{document_text}",
                self.include_keywords,
            )
        ):
            return []
        return [
            AaroRecord(
                title=page_title,
                source_url=normalize_url(base_url),
                publish_date=_extract_date([page_title, document_text]),
                description=document_text[:2000],
            )
        ]


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
        parser: AaroReleaseParser | GenericWebPageParser | None = None,
    ) -> None:
        self.repository = repository
        self.fetcher = fetcher or HttpWebPageFetcher()
        self.parser = parser

    def collect(self, source: Source, *, limit: int | None = None) -> WebCollectionResult:
        if source.source_type is not SourceType.WEB_PAGE:
            raise ValueError(f"Source {source.slug!r} is not a web page source")
        if source.id is None:
            raise ValueError("Persisted web-page source is required")
        run_id = self.repository.start_source_run(source.id)
        try:
            response, collection_url = _fetch_primary_or_fallback(self.fetcher, source)
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
            return WebCollectionResult(not_modified=True)
        if response.status != 200:
            message = f"Web page returned HTTP {response.status}"
            self.repository.record_source_fetch(source.id, error=message)
            self.repository.finish_source_run(
                run_id,
                status="failed",
                http_status=response.status,
                error=message,
            )
            raise RuntimeError(message)

        self.repository.record_source_fetch(
            source.id,
            etag=response.etag,
            last_modified=response.last_modified,
        )
        try:
            parser = self.parser or GenericWebPageParser(
                include_keywords=source.include_keywords
            )
            records = parser.parse(response.body, base_url=collection_url)
            if limit is not None:
                records = records[:limit]
            inserted, duplicates, invalid = _insert_web_records(
                self.repository,
                source,
                records,
            )
            self.repository.finish_source_run(
                run_id,
                status="success" if records else "empty",
                http_status=response.status,
                fetched_count=len(records),
                parsed_count=len(records),
                inserted_count=inserted,
                duplicate_count=duplicates,
                invalid_count=invalid,
            )
            return WebCollectionResult(
                fetched=len(records),
                inserted=inserted,
                duplicates=duplicates,
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
                http_status=response.status,
                error=message,
            )
            raise


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
        if source.id is None:
            raise ValueError("Persisted web-page source is required")
        run_id = self.repository.start_source_run(source.id)
        try:
            response, collection_url = _fetch_primary_or_fallback(self.fetcher, source)
        except Exception as error:
            message = str(error)[:1000]
            self.repository.record_source_fetch(source.id, error=message)
            self.repository.finish_source_run(run_id, status="failed", error=message)
            raise
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
            return WebCollectionResult(not_modified=True)
        if response.status != 200:
            message = f"Web page returned HTTP {response.status}"
            self.repository.record_source_fetch(source.id, error=message)
            self.repository.finish_source_run(
                run_id,
                status="failed",
                http_status=response.status,
                error=message,
            )
            raise RuntimeError(message)

        self.repository.record_source_fetch(
            source.id,
            etag=response.etag,
            last_modified=response.last_modified,
        )
        try:
            if collection_url == source.homepage_url:
                records = self.parser.parse(response.body, base_url=collection_url)
            else:
                fallback_records = GenericWebPageParser(
                    include_keywords=source.include_keywords
                ).parse(response.body, base_url=collection_url)
                records = [
                    AaroCaseRecord(
                        case_name=record.title,
                        source_url=record.source_url,
                        description=record.description
                        or "Official AARO-related record collected from DVIDS.",
                        date_start=record.publish_date,
                    )
                    for record in fallback_records
                ]
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
            self.repository.finish_source_run(
                run_id,
                status="success" if records else "empty",
                http_status=response.status,
                fetched_count=len(records),
                parsed_count=len(records),
                inserted_count=inserted,
                duplicate_count=duplicates,
                invalid_count=invalid,
            )
            return WebCollectionResult(
                fetched=len(records),
                inserted=inserted,
                duplicates=duplicates,
                invalid=invalid,
                events_inserted=events_inserted,
            )
        except Exception as error:
            message = str(error)[:1000]
            self.repository.record_source_fetch(source.id, error=message)
            self.repository.finish_source_run(
                run_id,
                status="failed",
                http_status=response.status,
                error=message,
            )
            raise


def _insert_web_records(
    repository: Repository,
    source: Source,
    records: list[AaroRecord],
) -> tuple[int, int, int]:
    inserted = duplicates = invalid = 0
    for record in records:
        if not record.title or not record.source_url:
            invalid += 1
            continue
        if repository.news_exists(
            canonical_url=record.source_url,
            source_id=source.id,
            feed_entry_id=_record_id(record.source_url),
        ):
            duplicates += 1
            continue
        repository.add_news(
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
    return inserted, duplicates, invalid


@dataclass
class _Link:
    text: str
    href: str


class _DocumentParser(HTMLParser):
    """Collect visible document text, headings, title, and ordinary links."""

    _ignored_tags: ClassVar[set[str]] = {
        "script",
        "style",
        "nav",
        "footer",
        "header",
        "svg",
        "noscript",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.headings: list[str] = []
        self.content_parts: list[str] = []
        self.links: list[_Link] = []
        self._ignored_depth = 0
        self._title_depth = 0
        self._heading_depth = 0
        self._current_link_href: str | None = None
        self._current_link_text: list[str] = []
        self._current_heading: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._ignored_tags:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._title_depth += 1
        elif tag in {"h1", "h2", "h3"}:
            self._heading_depth += 1
            self._current_heading = []
        elif tag == "a":
            self._current_link_href = dict(attrs).get("href")
            self._current_link_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._ignored_tags:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        elif tag in {"h1", "h2", "h3"} and self._heading_depth:
            heading = _clean_text(" ".join(self._current_heading))
            if heading:
                self.headings.append(heading)
            self._heading_depth -= 1
            self._current_heading = []
        elif tag == "a" and self._current_link_href is not None:
            self.links.append(
                _Link(
                    text=" ".join(self._current_link_text),
                    href=self._current_link_href,
                )
            )
            self._current_link_href = None
            self._current_link_text = []

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        text = " ".join(data.split())
        if not text:
            return
        self.content_parts.append(text)
        if self._title_depth:
            self.title_parts.append(text)
        if self._heading_depth:
            self._current_heading.append(text)
        if self._current_link_href is not None:
            self._current_link_text.append(text)

    @property
    def title(self) -> str:
        return _clean_text(" ".join(self.title_parts))


_GENERIC_LINK_LABELS = {
    "home",
    "menu",
    "more",
    "read more",
    "learn more",
    "click here",
    "view",
    "details",
    "download",
    "next",
    "previous",
    "forgot password?",
    "view more",
}

_GENERIC_DETAIL_LINK_LABELS = {
    "visit page",
    "visit video page",
    "watch video",
}


def _title_for_link(title: str, source_url: str) -> str:
    """Recover a useful title when a site labels every document link generically."""

    if title.casefold() not in _GENERIC_DETAIL_LINK_LABELS:
        return title
    path = unquote(urlparse(source_url).path).rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    if not slug:
        return title
    recovered = re.sub(r"[-_]+", " ", slug).title()
    recovered = re.sub(r"\bUap\b", "UAP", recovered)
    return re.sub(r"\bPr(\d+)\b", r"PR\1", recovered)


def _is_candidate_link(title: str, href: str) -> bool:
    if len(title) < 8 or title.casefold() in _GENERIC_LINK_LABELS:
        return False
    lowered_href = href.strip().casefold()
    return bool(lowered_href) and not lowered_href.startswith(
        ("#", "mailto:", "javascript:", "tel:")
    )


def _matches_source_keywords(value: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    searchable = value.casefold()
    for keyword in keywords:
        folded = keyword.casefold().strip()
        if not folded:
            continue
        if folded.isalnum() and len(folded) <= 4:
            if re.search(rf"(?<!\w){re.escape(folded)}(?!\w)", searchable):
                return True
        elif folded in searchable:
            return True
    return False


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
