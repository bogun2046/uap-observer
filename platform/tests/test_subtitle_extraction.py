from __future__ import annotations

import uuid
from pathlib import Path

from uap_platform.documents import (
    ExtractionInput,
    ExtractionOutcome,
    SrtExtractor,
    WebVttExtractor,
)

DOCUMENT_VERSION_ID = uuid.UUID("00000000-0000-7000-8000-000000000201")
SOURCE_OBJECT_ID = uuid.UUID("00000000-0000-7000-8000-000000000202")
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "documents"


def request(media_type: str, extractor_name: str) -> ExtractionInput:
    return ExtractionInput(
        document_version_id=DOCUMENT_VERSION_ID,
        source_object_id=SOURCE_OBJECT_ID,
        media_type=media_type,
        extractor_name=extractor_name,
        extractor_version="1.0.0",
    )


WEBVTT_FIXTURE = (FIXTURE_DIR / "sample.vtt").read_bytes()
SRT_FIXTURE = (FIXTURE_DIR / "sample.srt").read_bytes()


def test_webvtt_is_deterministic_and_preserves_timeline() -> None:
    extractor = WebVttExtractor()
    first = extractor.extract(request("text/vtt", extractor.name), WEBVTT_FIXTURE)
    second = extractor.extract(request("text/vtt", extractor.name), WEBVTT_FIXTURE)

    assert first == second
    assert first.outcome is ExtractionOutcome.SUCCEEDED
    assert first.text == "Hello & welcome\n\nSecond cue."
    assert first.output_sha256 is not None
    assert first.location_map[0]["time_start_ms"] == 1500
    assert first.location_map[0]["time_end_ms"] == 3000
    assert first.location_map[1]["char_start"] == 17


def test_srt_is_deterministic_and_supports_media_parameters() -> None:
    extractor = SrtExtractor()
    result = extractor.extract(
        request("application/x-subrip; charset=utf-8", extractor.name),
        SRT_FIXTURE,
    )

    assert result.outcome is ExtractionOutcome.SUCCEEDED
    assert result.text == "Hello & welcome\n\nSecond cue."
    assert result.location_map[1]["time_start_ms"] == 4000
    assert result.location_map[1]["time_end_ms"] == 5250
    assert result.as_record()["location_map"] == list(result.location_map)


def test_subtitle_rejects_invalid_timeline_encoding_and_limits() -> None:
    extractor = WebVttExtractor()
    invalid_time = extractor.extract(
        request("text/vtt", extractor.name),
        b"WEBVTT\n\n00:00:03.000 --> 00:00:02.000\ninvalid\n",
    )
    invalid_encoding = extractor.extract(
        request("text/vtt", extractor.name), b"WEBVTT\n\n\xff"
    )
    too_many = WebVttExtractor(max_cues=1).extract(
        request("text/vtt", extractor.name), WEBVTT_FIXTURE
    )
    wrong_type = SrtExtractor().extract(request("text/vtt", SrtExtractor.name), SRT_FIXTURE)

    assert invalid_time.error_code == "invalid_subtitle"
    assert invalid_encoding.error_code == "invalid_encoding"
    assert too_many.error_code == "too_many_cues"
    assert wrong_type.error_code == "unsupported_media_type"
