"""Deterministic extraction from one document-specific RSS/Atom response."""

from __future__ import annotations

from typing import Any

from defusedxml import ElementTree

from .contracts import (
    ExtractionInput,
    ExtractionOutcome,
    ExtractionResult,
    normalize_text,
    text_sha256,
)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _text(element: Any | None) -> str | None:
    if element is None:
        return None
    value = normalize_text(" ".join(element.itertext()))
    return value or None


def _first(root: Any, *names: str) -> Any | None:
    wanted = {name.casefold() for name in names}
    return next((item for item in root.iter() if _local_name(item.tag) in wanted), None)


class FeedXmlExtractor:
    """Extract the first entry from a separately fetched document-level feed."""

    name = "document_feed_entry_text"
    version = "1.0.0"

    def __init__(
        self, *, max_input_bytes: int = 2_000_000, max_output_chars: int = 500_000
    ) -> None:
        if max_input_bytes < 1 or max_output_chars < 1:
            raise ValueError("feed XML extraction limits must be positive")
        self.max_input_bytes = max_input_bytes
        self.max_output_chars = max_output_chars

    def extract(self, request: ExtractionInput, payload: bytes) -> ExtractionResult:
        media_type = request.media_type.split(";", 1)[0].strip().casefold()
        if media_type not in {
            "application/atom+xml",
            "application/rss+xml",
            "application/xml",
            "text/xml",
        }:
            return self._failure(request, "unsupported_media_type", "feed extractor requires XML")
        if len(payload) > self.max_input_bytes:
            return self._failure(request, "input_too_large", "feed XML exceeds byte limit")
        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError:
            return self._failure(request, "invalid_xml", "document feed is not valid XML")
        first_entry = _first(root, "entry", "item")
        entry = root if first_entry is None else first_entry
        title = _text(_first(entry, "title"))
        author_node = _first(entry, "author", "creator")
        author = _text(_first(author_node, "name")) if author_node is not None else None
        author = author or _text(author_node)
        published = _text(_first(entry, "published", "updated", "pubdate"))
        blocks = tuple(
            value
            for value in (title, _text(_first(entry, "content", "description", "summary")))
            if value
        )
        text = normalize_text("\n\n".join(blocks))
        if not text:
            return self._failure(request, "empty_document", "feed entry has no text")
        if len(text) > self.max_output_chars:
            return self._failure(request, "output_too_large", "feed text exceeds limit")
        location_map: list[dict[str, object]] = []
        offset = 0
        for block in blocks:
            start = text.find(block, offset)
            if start >= 0:
                end = start + len(block)
                location_map.append(
                    {"locator_type": "text", "start": start, "end": end, "kind": "entry"}
                )
                offset = end
        return ExtractionResult(
            request=request,
            outcome=ExtractionOutcome.SUCCEEDED,
            text=text,
            output_sha256=text_sha256(text),
            title=title,
            author=author,
            source_date=published,
            location_map=tuple(location_map),
        )

    @staticmethod
    def _failure(request: ExtractionInput, code: str, summary: str) -> ExtractionResult:
        return ExtractionResult(
            request=request,
            outcome=ExtractionOutcome.FAILED,
            error_code=code,
            error_summary=summary,
        )
