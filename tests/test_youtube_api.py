from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from uap_observer.collectors.youtube_api import YouTubeApiCollector
from uap_observer.models import FactStatus, NewsCategory, Source, SourceType


class FakeRepository:
    def __init__(self) -> None:
        self.next_news_id = 1
        self.fetched_source_ids: list[int] = []
        self.metrics: list[dict[str, Any]] = []

    def record_source_fetch(self, source_id: int) -> None:
        self.fetched_source_ids.append(source_id)

    def get_previous_youtube_views(self, *, video_id: str) -> int | None:
        return None

    def get_news_id(self, *, source_id: int, feed_entry_id: str) -> int | None:
        return None

    def add_news(self, news: Any) -> int:
        news_id = self.next_news_id
        self.next_news_id += 1
        return news_id

    def record_youtube_metric(self, **metric: Any) -> None:
        self.metrics.append(metric)


class YouTubeApiCollectorTests(unittest.TestCase):
    def test_video_details_are_requested_in_batches_of_fifty(self) -> None:
        repository = FakeRepository()
        collector = YouTubeApiCollector(
            repository,
            api_key="test-key",
            channel_ids=",".join(f"channel-{index}" for index in range(10)),
        )
        video_detail_calls: list[list[str]] = []

        def fake_get(endpoint: str, params: dict[str, object]) -> dict[str, object]:
            if endpoint == "search":
                channel_id = str(params["channelId"])
                channel_index = int(channel_id.rsplit("-", 1)[1])
                return {
                    "items": [
                        {"id": {"videoId": f"video-{channel_index}-{video_index}"}}
                        for video_index in range(10)
                    ]
                }
            if endpoint == "videos":
                batch = str(params["id"]).split(",")
                video_detail_calls.append(batch)
                return {
                    "items": [
                        {
                            "id": video_id,
                            "snippet": {
                                "title": video_id,
                                "description": "description",
                                "channelId": "channel",
                                "channelTitle": "Channel",
                                "publishedAt": "2026-08-02T00:00:00Z",
                            },
                            "statistics": {"viewCount": "1"},
                        }
                        for video_id in batch
                    ]
                }
            raise AssertionError(f"Unexpected endpoint: {endpoint}")

        source = Source(
            id=7,
            slug="youtube-uap",
            name="YouTube UAP Channel Watchlist",
            source_type=SourceType.API,
            homepage_url="https://www.youtube.com/",
            default_category=NewsCategory.OTHER,
            default_credibility=2,
            default_fact_status=FactStatus.SOURCE_REPORTED,
        )
        with patch.object(collector, "_get", side_effect=fake_get):
            result = collector.collect(source, limit=10)

        self.assertEqual([len(batch) for batch in video_detail_calls], [50, 50])
        self.assertEqual(result.channels, 10)
        self.assertEqual(result.fetched, 100)
        self.assertEqual(len(repository.metrics), 100)
        self.assertEqual(repository.fetched_source_ids, [7])


if __name__ == "__main__":
    unittest.main()
