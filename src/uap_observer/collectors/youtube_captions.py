"""Optional YouTube caption retrieval for priority videos via OAuth."""

from __future__ import annotations

import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

from uap_observer.repositories import Repository

API_ROOT = "https://www.googleapis.com/youtube/v3"
TAG_RE = re.compile(r"<[^>]+>")
TIMECODE_RE = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s+-->\s+")


@dataclass(frozen=True)
class CaptionCollectionResult:
    requested: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    token_count: int = 0


class YouTubeCaptionCollector:
    def __init__(
        self,
        repository: Repository,
        *,
        oauth_token: str | None = None,
        max_videos: int | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.repository = repository
        self.oauth_token = oauth_token or os.getenv("YOUTUBE_OAUTH_TOKEN")
        self.max_videos = max_videos or int(os.getenv("YOUTUBE_TRANSCRIPT_LIMIT", "5"))
        self.max_tokens = max_tokens or int(os.getenv("YOUTUBE_TRANSCRIPT_MAX_TOKENS", "12000"))

    def _request(self, endpoint: str, params: dict[str, object]) -> bytes:
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{API_ROOT}/{endpoint}?{query}",
            headers={"Authorization": f"Bearer {self.oauth_token}", "User-Agent": "UAPObserver/0.1"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    @staticmethod
    def _clean_vtt(body: bytes, *, max_tokens: int) -> tuple[str, int]:
        lines: list[str] = []
        for raw_line in body.decode("utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.upper() == "WEBVTT" or TIMECODE_RE.match(line):
                continue
            line = TAG_RE.sub("", line)
            if line and (not lines or line != lines[-1]):
                lines.append(line)
        text = " ".join(lines)
        max_chars = max_tokens * 4
        if len(text) > max_chars:
            text = text[:max_chars].rsplit(" ", 1)[0]
        tokens = max(1, (len(text) + 3) // 4) if text else 0
        return text, tokens

    def collect(self) -> CaptionCollectionResult:
        if not self.oauth_token:
            raise RuntimeError("YOUTUBE_OAUTH_TOKEN is not configured")
        if self.max_videos < 1 or self.max_tokens < 100:
            raise ValueError("YouTube transcript limits are invalid")
        candidates = self.repository.get_priority_youtube_news(limit=self.max_videos)
        completed = skipped = failed = token_count = 0
        for candidate in candidates:
            news_id = int(candidate["id"])
            video_id = str(candidate["feed_entry_id"])
            try:
                self.repository.update_youtube_transcript(news_id=news_id, status="pending")
                import json

                payload = json.loads(self._request("captions", {"part": "snippet", "videoId": video_id}))
                tracks = payload.get("items", [])
                if not tracks:
                    self.repository.update_youtube_transcript(news_id=news_id, status="skipped")
                    skipped += 1
                    continue
                tracks.sort(key=lambda item: 0 if item.get("snippet", {}).get("language", "").startswith("en") else 1)
                track_id = tracks[0]["id"]
                transcript, tokens = self._clean_vtt(
                    self._request("captions/download", {"id": track_id, "tfmt": "vtt"}),
                    max_tokens=self.max_tokens,
                )
                if not transcript:
                    self.repository.update_youtube_transcript(news_id=news_id, status="skipped")
                    skipped += 1
                    continue
                self.repository.update_youtube_transcript(
                    news_id=news_id, status="completed", transcript=transcript, token_count=tokens
                )
                completed += 1
                token_count += tokens
            except Exception:
                self.repository.update_youtube_transcript(news_id=news_id, status="failed")
                failed += 1
        return CaptionCollectionResult(
            requested=len(candidates),
            completed=completed,
            skipped=skipped,
            failed=failed,
            token_count=token_count,
        )
