"""Deterministic HTML-to-text extraction with auditable block locations."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser

from .contracts import (
    ExtractionInput,
    ExtractionOutcome,
    ExtractionResult,
    normalize_text,
    text_sha256,
)

_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "dd",
        "div",
        "dt",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "pre",
        "section",
        "td",
        "th",
    }
)
_EXCLUDED_TAGS = frozenset(
    {
        "aside",
        "canvas",
        "footer",
        "form",
        "header",
        "nav",
        "noscript",
        "script",
        "style",
        "svg",
        "template",
    }
)
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_NOISE_MARKERS = re.compile(
    r"(?:^|[-_ ])(?:advert|cookie|footer|header|menu|nav|promo|share|sidebar|social)(?:$|[-_ ])",
    re.IGNORECASE,
)


def _base_media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().casefold()


def _normalize_source_date(value: str) -> str | None:
    candidate = value.strip()
    if candidate.endswith(("Z", "z")):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class _Block:
    tag: str
    text: str


class _HtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[_Block] = []
        self.language_code: str | None = None
        self.author: str | None = None
        self.source_date: str | None = None
        self._current_tag: str | None = None
        self._current_parts: list[str] = []
        self._title_parts: list[str] = []
        self._h1: str | None = None
        self._in_title = False
        self._skip_depth = 0

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {name.lower(): value or "" for name, value in attrs}

    @staticmethod
    def _is_noise(attrs: Mapping[str, str]) -> bool:
        identity = f"{attrs.get('id', '')} {attrs.get('class', '')}"
        return bool(_NOISE_MARKERS.search(identity))

    def _flush(self) -> None:
        if self._current_tag is None:
            return
        text = normalize_text("".join(self._current_parts))
        if text:
            block = _Block(self._current_tag, text)
            self.blocks.append(block)
            if block.tag == "h1" and self._h1 is None:
                self._h1 = block.text
        self._current_tag = None
        self._current_parts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = self._attrs(attrs)
        if self._skip_depth:
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        if tag in _EXCLUDED_TAGS or self._is_noise(attributes):
            self._flush()
            if tag not in _VOID_TAGS:
                self._skip_depth = 1
            return
        if tag == "html":
            self.language_code = attributes.get("lang") or None
        elif tag == "meta":
            self._read_meta(attributes)
        elif tag == "title":
            self._in_title = True
            self._title_parts = []
        if tag in _BLOCK_TAGS:
            self._flush()
            self._current_tag = tag
        elif self._current_tag is None and tag not in {"head", "html", "body", "title"}:
            self._current_tag = "document"

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_depth:
            if tag not in _VOID_TAGS:
                self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._current_tag is None:
            self._current_tag = "document"
        self._current_parts.append(data)

    def _read_meta(self, attrs: Mapping[str, str]) -> None:
        key = (attrs.get("name") or attrs.get("property") or "").casefold()
        content = attrs.get("content", "").strip()
        if not content:
            return
        if key in {"author", "article:author"} and self.author is None:
            self.author = normalize_text(content)
        elif key in {"article:published_time", "date", "datepublished", "pubdate"}:
            if self.source_date is None:
                self.source_date = _normalize_source_date(content)

    def finish(self) -> tuple[str | None, str | None, str | None, tuple[_Block, ...]]:
        self._flush()
        title = normalize_text("".join(self._title_parts)) or self._h1
        return title, self.author, self.source_date, tuple(self.blocks)


class HtmlExtractor:
    """Extract visible HTML blocks without network access or script execution."""

    name = "html_readable_text"
    version = "1.0.0"

    def __init__(
        self,
        *,
        max_input_bytes: int = 10 * 1024 * 1024,
        max_output_chars: int = 2_000_000,
    ) -> None:
        if max_input_bytes < 1 or max_output_chars < 1:
            raise ValueError("HTML extraction limits must be positive")
        self.max_input_bytes = max_input_bytes
        self.max_output_chars = max_output_chars

    def extract(self, request: ExtractionInput, payload: bytes) -> ExtractionResult:
        if _base_media_type(request.media_type) not in {
            "text/html",
            "application/xhtml+xml",
        }:
            return self._failure(
                request,
                "unsupported_media_type",
                "HTML extractor requires an HTML media type",
            )
        if len(payload) > self.max_input_bytes:
            return self._failure(
                request, "input_too_large", "HTML input exceeds the configured byte limit"
            )
        parser = _HtmlParser()
        parser.feed(payload.decode("utf-8", errors="replace"))
        parser.close()
        title, author, source_date, blocks = parser.finish()
        text = normalize_text("\n\n".join(block.text for block in blocks))
        if not text:
            return self._failure(
                request, "empty_document", "HTML document contains no extractable text"
            )
        if len(text) > self.max_output_chars:
            return self._failure(
                request,
                "output_too_large",
                "extracted HTML text exceeds the configured character limit",
            )

        location_map: list[dict[str, object]] = []
        offset = 0
        for block in blocks:
            start = text.find(block.text, offset)
            if start < 0:
                continue
            end = start + len(block.text)
            location_map.append(
                {
                    "kind": "html_block",
                    "tag": block.tag,
                    "char_start": start,
                    "char_end": end,
                }
            )
            offset = end

        return ExtractionResult(
            request=request,
            outcome=ExtractionOutcome.SUCCEEDED,
            text=text,
            output_sha256=text_sha256(text),
            title=title,
            author=author,
            language_code=parser.language_code,
            source_date=source_date,
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
