"""X API v2 recent-post collection."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass

from uap_observer.models import News, Source
from uap_observer.repositories import Repository
from uap_observer.url_utils import normalize_url

X_SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
DEFAULT_QUERY = '(uap OR ufo OR "unidentified anomalous phenomena") -is:retweet'


@dataclass(frozen=True)
class XCollectionResult:
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    filtered: int = 0


class XApiCollector:
    def __init__(self, repository: Repository, *, bearer_token: str | None = None) -> None:
        self.repository = repository
        self.bearer_token = bearer_token or os.getenv("X_API_BEARER_TOKEN")

    def collect(
        self,
        source: Source,
        *,
        limit: int = 30,
        query: str | None = None,
    ) -> XCollectionResult:
        if source.id is None:
            raise ValueError("Persisted X source is required")
        if not self.bearer_token:
            raise RuntimeError("X_API_BEARER_TOKEN is not configured")
        if not 10 <= limit <= 100:
            raise ValueError("X API limit must be between 10 and 100")
        parameters = urllib.parse.urlencode(
            {
                "query": query or os.getenv("X_SEARCH_QUERY", DEFAULT_QUERY),
                "max_results": limit,
                "tweet.fields": "created_at,author_id,lang",
                "expansions": "author_id",
                "user.fields": "username,name",
            }
        )
        request = urllib.request.Request(
            f"{X_SEARCH_URL}?{parameters}",
            headers={"Authorization": f"Bearer {self.bearer_token}", "User-Agent": "UAPObserver/0.1"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        users = {
            str(user["id"]): user
            for user in payload.get("includes", {}).get("users", [])
        }
        inserted = duplicates = 0
        posts = payload.get("data", [])
        for post in posts:
            post_id = str(post["id"])
            user = users.get(str(post.get("author_id")), {})
            username = user.get("username") or "i"
            source_url = f"https://x.com/{username}/status/{post_id}"
            canonical_url = normalize_url(source_url)
            if self.repository.news_exists(
                canonical_url=canonical_url,
                source_id=source.id,
                feed_entry_id=post_id,
            ):
                duplicates += 1
                continue
            text = " ".join(str(post.get("text", "")).split())
            title = text[:120] + ("…" if len(text) > 120 else "")
            self.repository.add_news(
                News(
                    title=title or f"X 帖子 {post_id}",
                    original_title=title or f"X 帖子 {post_id}",
                    source=source.name,
                    source_url=source_url,
                    canonical_url=canonical_url,
                    publish_date=post.get("created_at"),
                    country=source.country,
                    category=source.default_category,
                    credibility=source.default_credibility,
                    fact_status=source.default_fact_status,
                    raw_content=text,
                    source_id=source.id,
                    feed_entry_id=post_id,
                )
            )
            inserted += 1
        return XCollectionResult(fetched=len(posts), inserted=inserted, duplicates=duplicates)
