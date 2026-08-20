"""Document extraction contracts and deterministic adapters."""

from .contracts import (
    EXTRACTION_PAYLOAD_SCHEMA_VERSION,
    ExtractionInput,
    ExtractionOutcome,
    ExtractionResult,
    Extractor,
)
from .html import HtmlExtractor
from .pdf import PdfExtractor
from .subtitles import SrtExtractor, WebVttExtractor
from .workflow import ExtractionJobHandler, build_extraction_request, payload_from_claim

__all__ = [
    "EXTRACTION_PAYLOAD_SCHEMA_VERSION",
    "ExtractionInput",
    "ExtractionJobHandler",
    "ExtractionOutcome",
    "ExtractionResult",
    "Extractor",
    "HtmlExtractor",
    "PdfExtractor",
    "SrtExtractor",
    "WebVttExtractor",
    "build_extraction_request",
    "payload_from_claim",
]
