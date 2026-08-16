"""Deterministic RSS collection primitives."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from defusedxml import ElementTree

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

from .contracts import (
    CollectionResult,
    FetchClassification,
    FetchResponse,
    NormalizedItem,
    ParsedFeed,
)

_DEFAULT_TRACKING_PARAMETERS = frozenset(
    {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "utm_campaign", "utm_medium", "utm_source"}
)
_WHITESPACE = re.compile(r"\s+")


def normalize_url(
    url: str, tracking_parameters: frozenset[str] = _DEFAULT_TRACKING_PARAMETERS
) -> str:
    """Return a deterministic HTTP(S) locator without tracking noise."""

    candidate = url.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("RSS item URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("RSS item URL must not contain user information")

    hostname = parsed.hostname.lower()
    port = parsed.port
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"
    query = tuple(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in tracking_parameters
        )
    )
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, urlencode(query), ""))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(element: Element | None) -> str | None:
    if element is None:
        return None
    value = _WHITESPACE.sub(" ", " ".join(element.itertext())).strip()
    return value or None


def _first_text(element: Element, *names: str) -> str | None:
    wanted = {name.lower() for name in names}
    for child in element.iter():
        if child is not element and _local_name(child.tag) in wanted:
            value = _text(child)
            if value:
                return value
    return None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _item_elements(root: Element) -> tuple[Element, ...]:
    names = {"item", "entry"}
    return tuple(element for element in root.iter() if _local_name(element.tag) in names)


def _entry_link(element: Element) -> str | None:
    for child in element:
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        value = href or _text(child)
        if value:
            return value
    return None


def _source_item_key(element: Element, canonical_url: str | None) -> str | None:
    guid = _first_text(element, "guid", "id")
    return guid or canonical_url


def parse_rss(
    payload: bytes, tracking_parameters: frozenset[str] = _DEFAULT_TRACKING_PARAMETERS
) -> ParsedFeed:
    """Parse RSS 2.0 or Atom entries into stable normalized items."""

    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise ValueError("RSS payload is not valid XML") from error

    items: list[NormalizedItem] = []
    seen_keys: set[str] = set()
    invalid_count = 0
    duplicate_count = 0
    for element in _item_elements(root):
        title = _first_text(element, "title")
        raw_url = _entry_link(element)
        try:
            canonical_url = normalize_url(raw_url, tracking_parameters) if raw_url else None
        except ValueError:
            canonical_url = None
        key = _source_item_key(element, canonical_url)
        if not title or not key or (raw_url and canonical_url is None):
            invalid_count += 1
            continue
        if key in seen_keys:
            duplicate_count += 1
            continue
        seen_keys.add(key)
        items.append(
            NormalizedItem(
                source_item_key=key,
                canonical_url=canonical_url,
                title=title,
                published_at=_parse_date(_first_text(element, "pubdate", "published", "updated")),
                summary=_first_text(element, "description", "summary", "content"),
                metadata={},
            )
        )
    return ParsedFeed(tuple(items), len(items), invalid_count, duplicate_count)


class RssCollector:
    """Four-stage RSS collector with injected transport and persistence hooks."""

    def __init__(
        self,
        fetch: Callable[[str, Mapping[str, str]], FetchResponse],
        persist: Callable[[tuple[NormalizedItem, ...]], int],
    ) -> None:
        self._fetch = fetch
        self._persist = persist

    def collect(
        self,
        source_url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> CollectionResult:
        """Run fetch, parse, normalize and persist as one source-run result."""

        headers = {
            key: value
            for key, value in (("If-None-Match", etag), ("If-Modified-Since", last_modified))
            if value
        }
        response = self._fetch(source_url, headers)
        classification = response.classify()
        base = CollectionResult(
            classification=classification,
            http_status=response.status_code,
            fetched_count=1,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )
        if classification is not FetchClassification.SUCCESS:
            return base
        try:
            parsed = parse_rss(response.body)
        except ValueError as error:
            return CollectionResult(
                classification=FetchClassification.TERMINAL_FAILURE,
                http_status=base.http_status,
                fetched_count=base.fetched_count,
                etag=base.etag,
                last_modified=base.last_modified,
                error_code="invalid_rss",
                error_summary=str(error),
            )
        persisted_count = self._persist(parsed.items) if parsed.items else 0
        return CollectionResult(
            classification=base.classification,
            http_status=base.http_status,
            fetched_count=base.fetched_count,
            etag=base.etag,
            last_modified=base.last_modified,
            parsed_count=parsed.parsed_count,
            persisted_count=persisted_count,
            duplicate_count=parsed.duplicate_count,
            invalid_count=parsed.invalid_count,
            items=parsed.items,
        )
