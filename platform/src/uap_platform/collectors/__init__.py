"""Source collector contracts and the first RSS collector implementation."""

from .contracts import (
    RSS_PAYLOAD_SCHEMA_VERSION,
    CollectionResult,
    FetchClassification,
    FetchResponse,
    NormalizedItem,
    ParsedFeed,
    snapshot_sha256,
)
from .persistence import PostgresSourceRunStore
from .policy import (
    SourceCoolingDown,
    SourceHealth,
    SourceHealthTracker,
    SourcePolicy,
    SourceRateLimiter,
)
from .rss import RssCollector, normalize_url, parse_rss
from .transport import UrlLibFetcher
from .workflow import RssSourceRunRunner

__all__ = [
    "RSS_PAYLOAD_SCHEMA_VERSION",
    "CollectionResult",
    "FetchClassification",
    "FetchResponse",
    "NormalizedItem",
    "ParsedFeed",
    "PostgresSourceRunStore",
    "RssCollector",
    "RssSourceRunRunner",
    "SourceCoolingDown",
    "SourceHealth",
    "SourceHealthTracker",
    "SourcePolicy",
    "SourceRateLimiter",
    "UrlLibFetcher",
    "normalize_url",
    "parse_rss",
    "snapshot_sha256",
]
