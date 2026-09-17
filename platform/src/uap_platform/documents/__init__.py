"""Document extraction contracts and deterministic adapters."""

from .contracts import (
    EXTRACTION_PAYLOAD_SCHEMA_VERSION,
    ExtractionInput,
    ExtractionOutcome,
    ExtractionResult,
    Extractor,
)
from .feed_xml import FeedXmlExtractor
from .html import HtmlExtractor
from .pdf import PdfExtractor
from .reddit import RedditSourceExtractor, is_reddit_bot_challenge
from .subtitles import SrtExtractor, WebVttExtractor
from .workflow import ExtractionJobHandler, build_extraction_request, payload_from_claim

__all__ = [
    "EXTRACTION_PAYLOAD_SCHEMA_VERSION",
    "ExtractionInput",
    "ExtractionJobHandler",
    "ExtractionOutcome",
    "ExtractionResult",
    "Extractor",
    "FeedXmlExtractor",
    "HtmlExtractor",
    "PdfExtractor",
    "RedditSourceExtractor",
    "SrtExtractor",
    "WebVttExtractor",
    "build_extraction_request",
    "is_reddit_bot_challenge",
    "payload_from_claim",
]
