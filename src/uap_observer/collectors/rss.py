"""RSS/Atom fetching and incremental persistence."""

from __future__ import annotations

import html
import http.client
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol

from uap_observer.models import News, Source
from uap_observer.repositories import Repository
from uap_observer.url_utils import normalize_url


USER_AGENT = "UAPObserver/0.1 (+https://github.com/; public-source research)"
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
        self.timeout = timeout
        self.max_retries = max_retries
        self.allow_curl_fallback = allow_curl_fallback

    def fetch(
        self,
        url: str,
        *,
        etag: str | None,
        last_modified: str | None,
    ) -> FeedResponse:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return FeedResponse(
                        status=response.status,
                        body=response.read(),
                        etag=response.headers.get("ETag"),
                        last_modified=response.headers.get("Last-Modified"),
                    )
            except urllib.error.HTTPError as error:
                if error.code == 304:
                    return FeedResponse(
                        status=304,
                        body=b"",
                        etag=error.headers.get("ETag"),
                        last_modified=error.headers.get("Last-Modified"),
                    )
                if error.code not in {429, 500, 502, 503, 504} or attempt == self.max_retries:
                    raise
            except (
                http.client.IncompleteRead,
                http.client.RemoteDisconnected,
                TimeoutError,
                urllib.error.URLError,
            ) as error:
                last_error = error
                if attempt == self.max_retries:
                    break
            time.sleep(0.5 * (2**attempt))
        if self.allow_curl_fallback and shutil.which("curl"):
            return self._fetch_with_curl(
                url,
                etag=etag,
                last_modified=last_modified,
            )
        if last_error:
            raise last_error
        raise RuntimeError("Feed retry loop exited unexpectedly")

    def _fetch_with_curl(
        self,
        url: str,
        *,
        etag: str | None,
        last_modified: str | None,
    ) -> FeedResponse:
        command = [
            "curl",
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            str(max(1, int(self.timeout))),
            "--header",
            f"User-Agent: {USER_AGENT}",
            "--header",
            "Accept: application/rss+xml, application/atom+xml",
            "--header",
            "Accept-Encoding: identity",
            "--dump-header",
            "/dev/stderr",
            "--write-out",
            "\nUAP_HTTP_STATUS:%{http_code}\n",
        ]
        if etag:
            command.extend(("--header", f"If-None-Match: {etag}"))
        if last_modified:
            command.extend(("--header", f"If-Modified-Since: {last_modified}"))
        command.append(url)
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=self.timeout + 5,
        )
        stderr = completed.stderr.decode("utf-8", errors="replace")
        stdout = completed.stdout
        stdout_text = stdout.decode("utf-8", errors="replace")
        status_source = f"{stderr}\n{stdout_text}"
        status_matches = re.findall(r"UAP_HTTP_STATUS:(\d{3})", status_source)
        if completed.returncode != 0 or not status_matches:
            detail = stderr.strip() or f"curl exited with {completed.returncode}"
            raise RuntimeError(f"curl feed fallback failed: {detail}")
        status = int(status_matches[-1])
        if status == 304:
            return FeedResponse(status=304, body=b"", etag=etag, last_modified=last_modified)
        if status >= 400:
            raise RuntimeError(f"curl feed fallback returned HTTP {status}")

        def last_header(name: str) -> str | None:
            matches = re.findall(rf"(?im)^{re.escape(name)}:\s*(.+?)\r?$", stderr)
            return matches[-1].strip() if matches else None

        return FeedResponse(
            status=status,
            body=_remove_curl_status_marker(stdout),
            etag=last_header("etag"),
            last_modified=last_header("last-modified"),
        )


def _remove_curl_status_marker(body: bytes) -> bytes:
    marker = b"\nUAP_HTTP_STATUS:"
    marker_start = body.rfind(marker)
    if marker_start == -1:
        return body
    return body[:marker_start]


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
            return CollectionResult(
                source_slug=source.slug,
                fetched=len(entries),
                inserted=inserted,
                duplicates=duplicates,
                filtered=filtered,
                invalid=invalid,
            )
        except Exception as error:
            self.repository.record_source_fetch(source.id, error=str(error)[:1000])
            raise
