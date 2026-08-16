"""Source collector contracts and the first RSS collector implementation."""

from .contracts import (
    CollectionResult,
    FetchClassification,
    FetchResponse,
    NormalizedItem,
    ParsedFeed,
)
from .persistence import PostgresSourceRunStore
from .rss import RssCollector, normalize_url, parse_rss
from .transport import UrlLibFetcher
from .workflow import RssSourceRunRunner

__all__ = [
    "CollectionResult",
    "FetchClassification",
    "FetchResponse",
    "NormalizedItem",
    "ParsedFeed",
    "PostgresSourceRunStore",
    "RssCollector",
    "RssSourceRunRunner",
    "UrlLibFetcher",
    "normalize_url",
    "parse_rss",
]
