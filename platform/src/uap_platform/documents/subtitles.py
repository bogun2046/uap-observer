"""Deterministic WebVTT and SRT extraction with millisecond locations."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from .contracts import (
    ExtractionInput,
    ExtractionOutcome,
    ExtractionResult,
    normalize_text,
    text_sha256,
)

_TIMING_RE = re.compile(
    r"^(?P<start>\d{1,}:\d{2}(?::\d{2})?[\.,]\d{1,3})\s+-->\s+"
    r"(?P<end>\d{1,}:\d{2}(?::\d{2})?[\.,]\d{1,3})(?:\s+.*)?$"
)
_TAG_RE = re.compile(r"<[^>]*>")
_VTT_MEDIA_TYPES = frozenset({"text/vtt", "text/webvtt"})
_SRT_MEDIA_TYPES = frozenset({"application/x-subrip", "application/srt", "text/srt"})


@dataclass(frozen=True, slots=True)
class _Cue:
    start_ms: int
    end_ms: int
    text: str


def _media_type(request: ExtractionInput) -> str:
    return request.media_type.split(";", 1)[0].strip().casefold()


def _timestamp_ms(value: str) -> int:
    normalized = value.replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 2:
        hours = 0
        minutes_text, seconds_text = parts
    elif len(parts) == 3:
        hours_text, minutes_text, seconds_text = parts
        hours = int(hours_text)
    else:
        raise ValueError("timestamp must contain two or three components")

    minutes = int(minutes_text)
    seconds_text, fraction_text = seconds_text.split(".", 1)
    seconds = int(seconds_text)
    if minutes >= 60 or seconds >= 60:
        raise ValueError("timestamp minute and second fields must be below 60")
    milliseconds = int(fraction_text.ljust(3, "0"))
    if milliseconds >= 1000:
        raise ValueError("timestamp fraction is invalid")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def _parse_timing(line: str) -> tuple[int, int]:
    match = _TIMING_RE.fullmatch(line.strip())
    if match is None:
        raise ValueError("invalid subtitle timing line")
    start_ms = _timestamp_ms(match.group("start"))
    end_ms = _timestamp_ms(match.group("end"))
    if end_ms < start_ms:
        raise ValueError("subtitle cue ends before it starts")
    return start_ms, end_ms


def _cue_text(lines: list[str]) -> str:
    without_tags = _TAG_RE.sub("", "\n".join(lines))
    return normalize_text(html.unescape(without_tags))


def _split_blocks(payload: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in payload.splitlines():
        if line.strip():
            current.append(line.rstrip())
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _parse_vtt(payload: str) -> list[_Cue]:
    lines = payload.splitlines()
    if not lines or lines[0].lstrip("\ufeff") != "WEBVTT":
        raise ValueError("WebVTT header is missing")

    cues: list[_Cue] = []
    blocks = _split_blocks("\n".join(lines[1:]))
    previous_start = -1
    for block in blocks:
        if block[0].startswith(("NOTE", "STYLE", "REGION")) and not any(
            "-->" in line for line in block
        ):
            continue
        timing_index = next(
            (index for index, line in enumerate(block) if "-->" in line),
            None,
        )
        if timing_index is None or timing_index == len(block) - 1:
            raise ValueError("WebVTT cue is missing timing or text")
        start_ms, end_ms = _parse_timing(block[timing_index])
        if start_ms < previous_start:
            raise ValueError("subtitle cue times are not in source order")
        text = _cue_text(block[timing_index + 1 :])
        if not text:
            raise ValueError("subtitle cue text is empty")
        cues.append(_Cue(start_ms, end_ms, text))
        previous_start = start_ms
    return cues


def _parse_srt(payload: str) -> list[_Cue]:
    cues: list[_Cue] = []
    previous_start = -1
    for block in _split_blocks(payload):
        timing_index = next(
            (index for index, line in enumerate(block) if "-->" in line),
            None,
        )
        if timing_index is None or timing_index == 0:
            raise ValueError("SRT cue is missing a numeric identifier or timing")
        if timing_index != 1 or not block[0].strip().isdigit():
            raise ValueError("SRT cue identifier is invalid")
        if timing_index == len(block) - 1:
            raise ValueError("SRT cue text is missing")
        start_ms, end_ms = _parse_timing(block[timing_index])
        if start_ms < previous_start:
            raise ValueError("subtitle cue times are not in source order")
        text = _cue_text(block[timing_index + 1 :])
        if not text:
            raise ValueError("subtitle cue text is empty")
        cues.append(_Cue(start_ms, end_ms, text))
        previous_start = start_ms
    return cues


class _SubtitleExtractor:
    name = "subtitle_timeline_text"
    version = "1.0.0"

    def __init__(
        self,
        *,
        max_input_bytes: int = 10 * 1024 * 1024,
        max_output_chars: int = 2_000_000,
        max_cues: int = 100_000,
    ) -> None:
        if min(max_input_bytes, max_output_chars, max_cues) < 1:
            raise ValueError("subtitle extraction limits must be positive")
        self.max_input_bytes = max_input_bytes
        self.max_output_chars = max_output_chars
        self.max_cues = max_cues

    def extract(self, request: ExtractionInput, payload: bytes) -> ExtractionResult:
        expected_types = self._media_types
        if _media_type(request) not in expected_types:
            return self._failure(
                request,
                "unsupported_media_type",
                "subtitle extractor does not support this media type",
            )
        if len(payload) > self.max_input_bytes:
            return self._failure(
                request,
                "input_too_large",
                "subtitle input exceeds the configured byte limit",
            )
        try:
            source = payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            return self._failure(
                request,
                "invalid_encoding",
                "subtitle input is not valid UTF-8",
            )
        try:
            cues = self._parse(source)
        except ValueError as error:
            return self._failure(request, "invalid_subtitle", str(error))
        if len(cues) > self.max_cues:
            return self._failure(
                request,
                "too_many_cues",
                "subtitle cue count exceeds the configured limit",
            )

        text = normalize_text("\n\n".join(cue.text for cue in cues))
        if not text:
            return self._failure(request, "empty_document", "subtitle contains no text")
        if len(text) > self.max_output_chars:
            return self._failure(
                request,
                "output_too_large",
                "extracted subtitle text exceeds the configured character limit",
            )

        location_map: list[dict[str, object]] = []
        offset = 0
        format_name = self._format_name
        for index, cue in enumerate(cues):
            start = text.find(cue.text, offset)
            if start < 0:
                continue
            end = start + len(cue.text)
            location_map.append(
                {
                    "kind": "subtitle_cue",
                    "format": format_name,
                    "cue_index": index,
                    "time_start_ms": cue.start_ms,
                    "time_end_ms": cue.end_ms,
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
            location_map=tuple(location_map),
        )

    @property
    def _media_types(self) -> frozenset[str]:
        raise NotImplementedError

    @property
    def _format_name(self) -> str:
        raise NotImplementedError

    def _parse(self, payload: str) -> list[_Cue]:
        raise NotImplementedError

    @staticmethod
    def _failure(request: ExtractionInput, code: str, summary: str) -> ExtractionResult:
        return ExtractionResult(
            request=request,
            outcome=ExtractionOutcome.FAILED,
            error_code=code,
            error_summary=summary,
        )


class WebVttExtractor(_SubtitleExtractor):
    """Extract WebVTT cues in source order."""

    @property
    def _media_types(self) -> frozenset[str]:
        return _VTT_MEDIA_TYPES

    @property
    def _format_name(self) -> str:
        return "webvtt"

    def _parse(self, payload: str) -> list[_Cue]:
        return _parse_vtt(payload)


class SrtExtractor(_SubtitleExtractor):
    """Extract SubRip cues in source order."""

    @property
    def _media_types(self) -> frozenset[str]:
        return _SRT_MEDIA_TYPES

    @property
    def _format_name(self) -> str:
        return "srt"

    def _parse(self, payload: str) -> list[_Cue]:
        return _parse_srt(payload)
