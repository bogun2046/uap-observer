"""Load version-controlled source definitions."""

from __future__ import annotations

import json
from pathlib import Path

from uap_observer.models import (
    FactStatus,
    NewsCategory,
    Source,
    SourceType,
)


def load_sources(path: Path) -> list[Source]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items = payload.get("sources")
    if not isinstance(items, list):
        raise TypeError("Source configuration must contain a 'sources' list")

    sources: list[Source] = []
    seen_slugs: set[str] = set()
    for raw in items:
        slug = str(raw["slug"]).strip()
        if not slug or slug in seen_slugs:
            raise ValueError(f"Source slug must be non-empty and unique: {slug!r}")
        seen_slugs.add(slug)
        source = Source(
            slug=slug,
            name=str(raw["name"]).strip(),
            source_type=SourceType(raw["source_type"]),
            homepage_url=str(raw["homepage_url"]).strip(),
            fallback_urls=[
                str(value).strip()
                for value in raw.get("fallback_urls", [])
                if str(value).strip()
            ],
            feed_url=raw.get("feed_url"),
            country=raw.get("country"),
            language=raw.get("language"),
            default_category=NewsCategory(raw["default_category"]),
            default_credibility=int(raw["default_credibility"]),
            default_fact_status=FactStatus(raw["default_fact_status"]),
            include_keywords=[str(value) for value in raw.get("include_keywords", [])],
            exclude_keywords=[str(value) for value in raw.get("exclude_keywords", [])],
            enabled=bool(raw.get("enabled", True)),
            refresh_interval_hours=int(raw.get("refresh_interval_hours", 24)),
        )
        if source.source_type is SourceType.RSS and not source.feed_url:
            raise ValueError(f"RSS source {slug!r} requires feed_url")
        if not 1 <= source.default_credibility <= 5:
            raise ValueError(f"Source {slug!r} credibility must be between 1 and 5")
        if source.refresh_interval_hours < 1:
            raise ValueError(f"Source {slug!r} refresh_interval_hours must be at least 1")
        sources.append(source)
    return sources
