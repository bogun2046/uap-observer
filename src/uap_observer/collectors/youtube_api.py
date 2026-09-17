"""YouTube Data API metadata and statistics collector."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass

from uap_observer.models import News, ProcessingStatus, Source
from uap_observer.repositories import Repository
from uap_observer.url_utils import normalize_url

API_ROOT = "https://www.googleapis.com/youtube/v3"
MAX_VIDEO_IDS_PER_REQUEST = 50


@dataclass(frozen=True)
class YouTubeCollectionResult:
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    channels: int = 0
    priority: int = 0


class YouTubeApiCollector:
    def __init__(self, repository: Repository, *, api_key: str | None = None, channel_ids: str | None = None) -> None:
        self.repository = repository
        self.api_key = api_key or os.getenv("YOUTUBE_API_KEY")
        raw_channels = channel_ids or os.getenv("YOUTUBE_CHANNEL_IDS", "")
        self.channel_ids = [value.strip() for value in raw_channels.split(",") if value.strip()]

    def _get(self, endpoint: str, params: dict[str, object]) -> dict:
        if not self.api_key:
            raise RuntimeError("YOUTUBE_API_KEY is not configured")
        query = urllib.parse.urlencode({**params, "key": self.api_key})
        request = urllib.request.Request(
            f"{API_ROOT}/{endpoint}?{query}",
            headers={"User-Agent": "UAPObserver/0.1"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def collect(self, source: Source, *, limit: int = 10) -> YouTubeCollectionResult:
        if source.id is None:
            raise ValueError("Persisted YouTube source is required")
        if not self.api_key:
            raise RuntimeError("YOUTUBE_API_KEY is not configured")
        if not self.channel_ids:
            raise RuntimeError("YOUTUBE_CHANNEL_IDS is not configured")
        if not 1 <= limit <= 50:
            raise ValueError("YouTube limit must be between 1 and 50")

        video_ids: list[str] = []
        for channel_id in self.channel_ids:
            payload = self._get(
                "search",
                {
                    "part": "snippet",
                    "channelId": channel_id,
                    "type": "video",
                    "order": "date",
                    "maxResults": limit,
                },
            )
            video_ids.extend(
                str(item["id"]["videoId"])
                for item in payload.get("items", [])
                if item.get("id", {}).get("videoId")
            )
        if not video_ids:
            self.repository.record_source_fetch(source.id)
            return YouTubeCollectionResult(channels=len(self.channel_ids))

        unique_video_ids = list(dict.fromkeys(video_ids))
        details: list[dict] = []
        for start in range(0, len(unique_video_ids), MAX_VIDEO_IDS_PER_REQUEST):
            batch_ids = unique_video_ids[start : start + MAX_VIDEO_IDS_PER_REQUEST]
            details.extend(
                self._get(
                    "videos",
                    {
                        "part": "snippet,contentDetails,statistics",
                        "id": ",".join(batch_ids),
                    },
                ).get("items", [])
            )
        inserted = duplicates = 0
        priority_count = 0
        hot_threshold = int(os.getenv("YOUTUBE_HOT_VIEW_THRESHOLD", "100000"))
        for item in details:
            video_id = str(item["id"])
            snippet = item.get("snippet", {})
            statistics = item.get("statistics", {})
            view_count = int(statistics.get("viewCount", 0))
            previous_views = self.repository.get_previous_youtube_views(video_id=video_id)
            growth = max(0, view_count - previous_views) if previous_views is not None else 0
            is_priority = view_count >= hot_threshold or growth >= hot_threshold
            priority_count += int(is_priority)
            source_url = f"https://www.youtube.com/watch?v={video_id}"
            canonical_url = normalize_url(source_url)
            news_id = self.repository.get_news_id(source_id=source.id, feed_entry_id=video_id)
            if news_id is None:
                description = " ".join(str(snippet.get("description", "")).split())
                title = str(snippet.get("title") or f"YouTube video {video_id}")
                raw_content = json.dumps(
                    {
                        "description": description,
                        "channel_id": snippet.get("channelId"),
                        "channel_title": snippet.get("channelTitle"),
                        "video_id": video_id,
                    },
                    ensure_ascii=False,
                )
                news_id = self.repository.add_news(
                    News(
                        title=title,
                        original_title=title,
                        source=source.name,
                        source_url=source_url,
                        canonical_url=canonical_url,
                        publish_date=snippet.get("publishedAt"),
                        country=source.country,
                        category=source.default_category,
                        credibility=source.default_credibility,
                        fact_status=source.default_fact_status,
                        summary=description or None,
                        raw_content=raw_content,
                        processing_status=(
                            ProcessingStatus.PENDING if is_priority else ProcessingStatus.SKIPPED
                        ),
                        source_id=source.id,
                        feed_entry_id=video_id,
                    )
                )
                inserted += 1
            else:
                duplicates += 1
            self.repository.record_youtube_metric(
                news_id=news_id,
                video_id=video_id,
                view_count=view_count,
                like_count=int(statistics.get("likeCount", 0)),
                comment_count=int(statistics.get("commentCount", 0)),
                view_growth_24h=growth,
                priority=is_priority,
            )
        self.repository.record_source_fetch(source.id)
        return YouTubeCollectionResult(
            fetched=len(details),
            inserted=inserted,
            duplicates=duplicates,
            channels=len(self.channel_ids),
            priority=priority_count,
        )
