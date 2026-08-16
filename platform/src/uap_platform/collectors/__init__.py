"""Source collector contracts and the first RSS collector implementation."""

from .contracts import (
    CollectionResult,
    FetchClassification,
    FetchResponse,
    NormalizedItem,
    ParsedFeed,
)
from .rss import RssCollector, normalize_url, parse_rss

__all__ = [
    "CollectionResult",
    "FetchClassification",
    "FetchResponse",
    "NormalizedItem",
    "ParsedFeed",
    "RssCollector",
    "normalize_url",
    "parse_rss",
]
