"""Deterministic extraction for Reddit Atom entries and official API responses."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from defusedxml import ElementTree

from .contracts import (
    ExtractionInput,
    ExtractionOutcome,
    ExtractionResult,
    normalize_text,
    text_sha256,
)

_REMOVED_VALUES = frozenset({"[deleted]", "[removed]"})
_BLOCK_TAGS = frozenset({"blockquote", "br", "div", "h1", "h2", "h3", "li", "p", "pre"})
_VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "source", "wbr"})
_ALWAYS_CHALLENGE = (
    re.compile(r"\bprove\s+your\s+humanity\b", re.IGNORECASE),
    re.compile(r"\bnot\s+for\s+bots\b", re.IGNORECASE),
)
_HTML_CHALLENGE = (
    re.compile(r"\bcaptcha\b", re.IGNORECASE),
    re.compile(r"(?:id|class)=[\"'][^\"']*captcha[^\"']*[\"']", re.IGNORECASE),
    re.compile(r"<title[^>]*>[^<]*(?:captcha|security verification|challenge)", re.IGNORECASE),
    re.compile(r"(?:reddit[^<]{0,80})?(?:security verification|challenge page)", re.IGNORECASE),
)


def _base_media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().casefold()


def is_reddit_bot_challenge(payload: bytes, media_type: str) -> bool:
    """Identify explicit Reddit bot/security pages without attempting to bypass them."""

    text = payload.decode("utf-8", errors="replace")
    if any(pattern.search(text) for pattern in _ALWAYS_CHALLENGE):
        return True
    if _base_media_type(media_type) in {"text/html", "application/xhtml+xml"}:
        return any(pattern.search(text) for pattern in _HTML_CHALLENGE)
    return False


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _first(root: Any, *names: str) -> Any | None:
    wanted = {name.casefold() for name in names}
    return next((item for item in root.iter() if _local_name(item.tag) in wanted), None)


def _node_text(node: Any | None) -> str | None:
    if node is None:
        return None
    value = normalize_text(" ".join(node.itertext()))
    return value or None


class _MarkdownBodyParser(HTMLParser):
    """Read only Reddit's ``div.md`` body and ignore surrounding action links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._target_depth = 0
        self._skip_depth = 0
        self._parts: list[str] = []
        self.found_target = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if self._target_depth:
            if tag in _VOID_TAGS:
                if tag in _BLOCK_TAGS:
                    self._parts.append("\n")
                return
            self._target_depth += 1
            if tag in {"script", "style"}:
                self._skip_depth += 1
            elif tag in _BLOCK_TAGS:
                self._parts.append("\n")
            return
        classes = set(attributes.get("class", "").split())
        if tag == "div" and "md" in classes:
            self.found_target = True
            self._target_depth = 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if self._target_depth and tag.casefold() in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self._target_depth:
            return
        if self._skip_depth and tag.casefold() in {"script", "style"}:
            self._skip_depth -= 1
        if tag.casefold() in _BLOCK_TAGS:
            self._parts.append("\n")
        self._target_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._target_depth and not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return normalize_text("".join(self._parts))


def _body_from_atom_content(content: Any | None) -> str:
    if content is None:
        return ""
    raw = "".join(content.itertext())
    content_type = str(content.attrib.get("type", "")).casefold()
    if content_type == "html" or "<div" in raw.casefold():
        parser = _MarkdownBodyParser()
        parser.feed(raw)
        parser.close()
        return parser.text() if parser.found_target else ""
    return normalize_text(raw)


def _source_date_from_epoch(value: object) -> str | None:
    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


class RedditSourceExtractor:
    """Extract a Reddit post from discovery Atom RAW or official API JSON RAW."""

    name = "reddit_source_text"
    version = "1.0.0"
    supported_media_types = frozenset(
        {
            "application/atom+xml",
            "application/json",
            "application/rss+xml",
            "application/xml",
            "application/xhtml+xml",
            "text/html",
            "text/xml",
        }
    )

    def __init__(
        self, *, max_input_bytes: int = 2_000_000, max_output_chars: int = 500_000
    ) -> None:
        if max_input_bytes < 1 or max_output_chars < 1:
            raise ValueError("Reddit extraction limits must be positive")
        self.max_input_bytes = max_input_bytes
        self.max_output_chars = max_output_chars

    def extract(self, request: ExtractionInput, payload: bytes) -> ExtractionResult:
        media_type = _base_media_type(request.media_type)
        if media_type not in self.supported_media_types:
            return self._failure(
                request, "unsupported_media_type", "Reddit extractor requires Atom, JSON or HTML"
            )
        if len(payload) > self.max_input_bytes:
            return self._failure(request, "input_too_large", "Reddit source exceeds byte limit")
        if is_reddit_bot_challenge(payload, media_type):
            return self._failure(
                request,
                "source_bot_challenge",
                "Reddit returned a bot or security verification page",
            )
        if media_type in {"text/html", "application/xhtml+xml"}:
            return self._failure(
                request,
                "unexpected_reddit_response",
                "Reddit returned HTML instead of Atom or official API JSON",
            )
        parsed = (
            self._extract_api_json(request, payload)
            if media_type == "application/json"
            else self._extract_atom(request, payload)
        )
        if isinstance(parsed, ExtractionResult):
            return parsed
        title, body, author, source_date = parsed
        body = normalize_text(body)
        if not body or body.casefold() in _REMOVED_VALUES:
            return self._failure(
                request, "reddit_body_unavailable", "Reddit post body is unavailable"
            )
        text = normalize_text("\n\n".join(value for value in (title, body) if value))
        if len(text) > self.max_output_chars:
            return self._failure(request, "output_too_large", "Reddit text exceeds limit")
        body_start = text.find(body)
        location_map: tuple[dict[str, object], ...] = (
            {
                "locator_type": "text",
                "start": body_start,
                "end": body_start + len(body),
                "kind": "reddit_post_body",
            },
        )
        return ExtractionResult(
            request=request,
            outcome=ExtractionOutcome.SUCCEEDED,
            text=text,
            output_sha256=text_sha256(text),
            title=title,
            author=author,
            language_code="en",
            source_date=source_date,
            location_map=location_map,
        )

    def _extract_atom(
        self, request: ExtractionInput, payload: bytes
    ) -> tuple[str | None, str, str | None, str | None] | ExtractionResult:
        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError:
            return self._failure(request, "invalid_xml", "Reddit Atom entry is not valid XML")
        first_entry = _first(root, "entry", "item")
        entry = root if first_entry is None else first_entry
        title = _node_text(_first(entry, "title"))
        author_node = _first(entry, "author", "creator")
        author = _node_text(_first(author_node, "name")) if author_node is not None else None
        author = author or _node_text(author_node)
        source_date = _node_text(_first(entry, "published", "updated", "pubdate"))
        body = _body_from_atom_content(_first(entry, "content", "description", "summary"))
        return title, body, author, source_date

    def _extract_api_json(
        self, request: ExtractionInput, payload: bytes
    ) -> tuple[str | None, str, str | None, str | None] | ExtractionResult:
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return self._failure(
                request, "reddit_api_invalid_json", "Reddit API response is not valid JSON"
            )
        try:
            children = decoded["data"]["children"]
            post = children[0]["data"]
        except (KeyError, IndexError, TypeError):
            return self._failure(
                request, "reddit_api_invalid_json", "Reddit API response has no post object"
            )
        if not isinstance(post, dict):
            return self._failure(
                request, "reddit_api_invalid_json", "Reddit API post is not an object"
            )
        title_value = post.get("title")
        body_value = post.get("selftext")
        author_value = post.get("author")
        title: str | None = title_value if isinstance(title_value, str) else None
        body: str = body_value if isinstance(body_value, str) else ""
        author: str | None = author_value if isinstance(author_value, str) else None
        return title, body, author, _source_date_from_epoch(post.get("created_utc"))

    @staticmethod
    def _failure(request: ExtractionInput, code: str, summary: str) -> ExtractionResult:
        return ExtractionResult(
            request=request,
            outcome=ExtractionOutcome.FAILED,
            error_code=code,
            error_summary=summary,
        )
