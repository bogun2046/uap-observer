"""Bounded PDF text extraction with page-level locations."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from io import BytesIO
from typing import Any

from pypdf import PdfReader

from .contracts import (
    ExtractionInput,
    ExtractionOutcome,
    ExtractionResult,
    normalize_text,
    text_sha256,
)


def _media_type(request: ExtractionInput) -> str:
    return request.media_type.split(";", 1)[0].strip().casefold()


def _metadata_value(metadata: Any, key: str) -> str | None:
    if metadata is None:
        return None
    value = metadata.get(key)
    if value is None:
        return None
    normalized = normalize_text(str(value))
    return normalized or None


_PDF_DATE_RE = re.compile(
    r"^D:(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})"
    r"(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})"
    r"(?P<zone>Z|(?P<sign>[+-])(?P<zone_hour>\d{2})'?"
    r"(?P<zone_minute>\d{2})'?)?$"
)


def _creation_date(metadata: Any) -> str | None:
    raw = _metadata_value(metadata, "/CreationDate")
    if raw is None:
        return None
    match = _PDF_DATE_RE.fullmatch(raw)
    if match is None:
        return None
    try:
        value = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
        )
    except ValueError:
        return None
    zone = match.group("zone")
    if zone == "Z":
        return value.replace(tzinfo=UTC).isoformat()
    if zone is not None:
        minutes = int(match.group("zone_hour")) * 60 + int(match.group("zone_minute"))
        offset = timedelta(minutes=minutes)
        if match.group("sign") == "-":
            offset = -offset
        return value.replace(tzinfo=timezone(offset)).isoformat()
    return value.isoformat()


class PdfExtractor:
    """Extract text from bounded, non-encrypted PDF inputs without network access."""

    name = "pdf_text"
    version = "1.0.0"

    def __init__(
        self,
        *,
        max_input_bytes: int = 50 * 1024 * 1024,
        max_output_chars: int = 5_000_000,
        max_pages: int = 500,
    ) -> None:
        if min(max_input_bytes, max_output_chars, max_pages) < 1:
            raise ValueError("PDF extraction limits must be positive")
        self.max_input_bytes = max_input_bytes
        self.max_output_chars = max_output_chars
        self.max_pages = max_pages

    def extract(self, request: ExtractionInput, payload: bytes) -> ExtractionResult:
        if _media_type(request) != "application/pdf":
            return self._failure(
                request,
                "unsupported_media_type",
                "PDF extractor requires an application/pdf media type",
            )
        if len(payload) > self.max_input_bytes:
            return self._failure(
                request,
                "input_too_large",
                "PDF input exceeds the configured byte limit",
            )
        if payload[:1024].find(b"%PDF-") < 0:
            return self._failure(request, "invalid_pdf", "PDF could not be parsed safely")
        try:
            reader = PdfReader(BytesIO(payload), strict=False)
            if reader.is_encrypted:
                return self._failure(
                    request,
                    "encrypted_pdf",
                    "encrypted PDF input is not supported",
                )
            page_count = len(reader.pages)
            if page_count > self.max_pages:
                return self._failure(
                    request,
                    "page_limit_exceeded",
                    "PDF page count exceeds the configured limit",
                )
            metadata = reader.metadata
            title = _metadata_value(metadata, "/Title")
            author = _metadata_value(metadata, "/Author")
            source_date = _creation_date(metadata)
            page_texts: list[tuple[int, str]] = []
            for page_number, page in enumerate(reader.pages, start=1):
                page_text = normalize_text(page.extract_text() or "")
                if page_text:
                    page_texts.append((page_number, page_text))
        except Exception:
            return self._failure(request, "invalid_pdf", "PDF could not be parsed safely")

        text = normalize_text("\n\n".join(page_text for _, page_text in page_texts))
        if not text:
            return self._failure(
                request,
                "no_extractable_text",
                "PDF contains no extractable text",
            )
        if len(text) > self.max_output_chars:
            return self._failure(
                request,
                "output_too_large",
                "extracted PDF text exceeds the configured character limit",
            )

        location_map: list[dict[str, object]] = []
        offset = 0
        for page_number, page_text in page_texts:
            start = text.find(page_text, offset)
            if start < 0:
                continue
            end = start + len(page_text)
            location_map.append(
                {
                    "kind": "pdf_page",
                    "page_start": page_number,
                    "page_end": page_number,
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
