from __future__ import annotations

import uuid

import pytest

from uap_platform.documents import (
    EXTRACTION_PAYLOAD_SCHEMA_VERSION,
    ExtractionInput,
    ExtractionOutcome,
    ExtractionResult,
    HtmlExtractor,
)

DOCUMENT_VERSION_ID = uuid.UUID("00000000-0000-7000-8000-000000000101")
SOURCE_OBJECT_ID = uuid.UUID("00000000-0000-7000-8000-000000000102")


def request(media_type: str = "text/html") -> ExtractionInput:
    return ExtractionInput(
        document_version_id=DOCUMENT_VERSION_ID,
        source_object_id=SOURCE_OBJECT_ID,
        media_type=media_type,
        extractor_name=HtmlExtractor.name,
        extractor_version=HtmlExtractor.version,
    )


HTML_FIXTURE = b"""
<!doctype html>
<html lang="en">
  <head>
    <title>  Example &amp; Report </title>
    <meta name="author" content=" Ada Lovelace ">
    <meta property="article:published_time" content="2026-08-19T10:00:00Z">
  </head>
  <body>
    <header>Do not include this header</header>
    <nav class="main-menu">Do not include this menu</nav>
    <article>
      <h1>Example &amp; Report</h1>
      <p> First   paragraph with <strong>inline</strong> text. </p>
      <p>Second paragraph.</p>
    </article>
    <aside class="social-share">Do not include this aside</aside>
    <script>secrets.should.not.appear()</script>
  </body>
</html>
"""


def test_html_extraction_is_deterministic_and_excludes_noise() -> None:
    extractor = HtmlExtractor()

    first = extractor.extract(request(), HTML_FIXTURE)
    second = extractor.extract(request(), HTML_FIXTURE)

    assert first == second
    assert first.outcome is ExtractionOutcome.SUCCEEDED
    assert first.text == (
        "Example & Report\n\nFirst paragraph with inline text.\n\nSecond paragraph."
    )
    assert first.title == "Example & Report"
    assert first.author == "Ada Lovelace"
    assert first.language_code == "en"
    assert first.source_date == "2026-08-19T10:00:00Z"
    assert "Do not include" not in first.text
    assert first.output_sha256 is not None
    assert len(first.location_map) == 3
    assert first.location_map[1]["tag"] == "p"


def test_html_accepts_media_type_parameters() -> None:
    result = HtmlExtractor().extract(request("text/html; charset=utf-8"), HTML_FIXTURE)

    assert result.outcome is ExtractionOutcome.SUCCEEDED
    assert result.source_date == "2026-08-19T10:00:00Z"


def test_html_ignores_invalid_source_date() -> None:
    payload = HTML_FIXTURE.replace(
        b'content="2026-08-19T10:00:00Z"', b'content="not-a-date"'
    )

    result = HtmlExtractor().extract(request(), payload)

    assert result.outcome is ExtractionOutcome.SUCCEEDED
    assert result.source_date is None


def test_html_result_record_is_json_safe_and_versioned() -> None:
    result = HtmlExtractor().extract(request(), HTML_FIXTURE)

    record = result.as_record()

    assert record["payload_schema_version"] == EXTRACTION_PAYLOAD_SCHEMA_VERSION
    assert record["document_version_id"] == str(DOCUMENT_VERSION_ID)
    assert record["outcome"] == "succeeded"
    assert isinstance(record["location_map"], list)


def test_html_extractor_returns_structured_failures() -> None:
    extractor = HtmlExtractor(max_input_bytes=3)

    too_large = extractor.extract(request(), b"<p>too large</p>")
    empty = HtmlExtractor().extract(request(), b"<html><body><script>x</script></body></html>")
    wrong_type = HtmlExtractor().extract(request("application/pdf"), b"pdf")

    assert too_large.outcome is ExtractionOutcome.FAILED
    assert too_large.error_code == "input_too_large"
    assert empty.error_code == "empty_document"
    assert wrong_type.error_code == "unsupported_media_type"


def test_extraction_input_rejects_unknown_schema() -> None:
    with pytest.raises(ValueError, match="unsupported extraction payload schema"):
        ExtractionInput(
            document_version_id=DOCUMENT_VERSION_ID,
            source_object_id=SOURCE_OBJECT_ID,
            media_type="text/html",
            extractor_name="html_readable_text",
            extractor_version="1.0.0",
            payload_schema_version="extract.v0",
        )


def test_extraction_result_rejects_mismatched_success_hash() -> None:
    with pytest.raises(ValueError, match="hash does not match"):
        ExtractionResult(
            request=request(),
            outcome=ExtractionOutcome.SUCCEEDED,
            text="body",
            output_sha256="0" * 64,
        )
