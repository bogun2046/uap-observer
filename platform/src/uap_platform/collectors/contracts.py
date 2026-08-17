"""Stable, persistence-independent contracts for source collectors."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

RSS_PAYLOAD_SCHEMA_VERSION = "rss.v1"


class FetchClassification(StrEnum):
    """How a fetch result should affect the source run and durable job."""

    SUCCESS = "success"
    NOT_MODIFIED = "not_modified"
    EMPTY = "empty"
    AUTHORIZATION_FAILURE = "authorization_failure"
    RATE_LIMITED = "rate_limited"
    TRANSIENT_FAILURE = "transient_failure"
    TERMINAL_FAILURE = "terminal_failure"


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """Transport output kept separate from parsing and persistence."""

    status_code: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)
    retrieved_at: datetime | None = None
    error_code: str | None = None
    error_summary: str | None = None
    payload_schema_version: str = RSS_PAYLOAD_SCHEMA_VERSION

    def header(self, name: str) -> str | None:
        """Read an HTTP header without relying on the transport's casing."""

        wanted = name.casefold()
        for key, value in self.headers.items():
            if key.casefold() == wanted:
                return value
        return None

    def classify(self) -> FetchClassification:
        """Map HTTP status and body presence to the frozen source-run semantics."""

        if self.status_code == 304:
            return FetchClassification.NOT_MODIFIED
        if self.status_code == 403:
            return FetchClassification.AUTHORIZATION_FAILURE
        if self.status_code == 429:
            return FetchClassification.RATE_LIMITED
        if self.status_code == 408 or self.status_code >= 500:
            return FetchClassification.TRANSIENT_FAILURE
        if 200 <= self.status_code < 300:
            return (
                FetchClassification.EMPTY
                if not self.body.strip()
                else FetchClassification.SUCCESS
            )
        return FetchClassification.TERMINAL_FAILURE


@dataclass(frozen=True, slots=True)
class NormalizedItem:
    """A source item after parsing and canonicalization."""

    source_item_key: str
    canonical_url: str | None
    title: str
    published_at: datetime | None
    summary: str | None
    metadata: Mapping[str, str] = field(default_factory=dict)
    raw_payload: bytes = b""
    payload_schema_version: str = RSS_PAYLOAD_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ParsedFeed:
    """Deterministic parser output, including invalid and duplicate counts."""

    items: tuple[NormalizedItem, ...]
    parsed_count: int
    invalid_count: int
    duplicate_count: int
    snapshot_sha256: str = ""
    payload_schema_version: str = RSS_PAYLOAD_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """Result suitable for a source-run persistence adapter."""

    classification: FetchClassification
    http_status: int
    fetched_count: int
    parsed_count: int = 0
    persisted_count: int = 0
    duplicate_count: int = 0
    invalid_count: int = 0
    etag: str | None = None
    last_modified: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    items: tuple[NormalizedItem, ...] = ()
    snapshot_sha256: str | None = None
    payload_schema_version: str = RSS_PAYLOAD_SCHEMA_VERSION


def snapshot_sha256(payload: bytes) -> str:
    """Return the immutable hash recorded for a fetched source snapshot."""

    return hashlib.sha256(payload).hexdigest()
