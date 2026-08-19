from __future__ import annotations

import uuid
from pathlib import Path

from uap_platform.documents import ExtractionInput, ExtractionOutcome, PdfExtractor

DOCUMENT_VERSION_ID = uuid.UUID("00000000-0000-7000-8000-000000000301")
SOURCE_OBJECT_ID = uuid.UUID("00000000-0000-7000-8000-000000000302")
PDF_FIXTURE = (Path(__file__).parent / "fixtures" / "documents" / "sample.pdf").read_bytes()


def request(media_type: str = "application/pdf") -> ExtractionInput:
    return ExtractionInput(
        document_version_id=DOCUMENT_VERSION_ID,
        source_object_id=SOURCE_OBJECT_ID,
        media_type=media_type,
        extractor_name=PdfExtractor.name,
        extractor_version=PdfExtractor.version,
    )


def test_pdf_extraction_is_deterministic_and_has_page_locations() -> None:
    extractor = PdfExtractor()
    first = extractor.extract(request(), PDF_FIXTURE)
    second = extractor.extract(request(), PDF_FIXTURE)

    assert first == second
    assert first.outcome is ExtractionOutcome.SUCCEEDED
    assert first.text == "Fixed PDF fixture"
    assert first.title == "Fixed PDF fixture"
    assert first.author is None
    assert first.location_map == (
        {
            "kind": "pdf_page",
            "page_start": 1,
            "page_end": 1,
            "char_start": 0,
            "char_end": 17,
        },
    )


def test_pdf_extractor_returns_structured_failures() -> None:
    extractor = PdfExtractor(max_input_bytes=3)
    too_large = extractor.extract(request(), b"%PDF-1.7")
    invalid = PdfExtractor().extract(request(), b"not a PDF")
    empty = PdfExtractor().extract(request(), b"%PDF-1.7\n%%EOF")
    wrong_type = PdfExtractor().extract(request("text/html"), b"<p>not pdf</p>")

    assert too_large.error_code == "input_too_large"
    assert invalid.error_code == "invalid_pdf"
    assert empty.error_code in {"invalid_pdf", "no_extractable_text"}
    assert wrong_type.error_code == "unsupported_media_type"
