"""Document extraction contracts and deterministic adapters."""

from .contracts import (
    EXTRACTION_PAYLOAD_SCHEMA_VERSION,
    ExtractionInput,
    ExtractionOutcome,
    ExtractionResult,
    Extractor,
)
from .html import HtmlExtractor

__all__ = [
    "EXTRACTION_PAYLOAD_SCHEMA_VERSION",
    "ExtractionInput",
    "ExtractionOutcome",
    "ExtractionResult",
    "Extractor",
    "HtmlExtractor",
]
