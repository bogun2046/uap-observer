"""Minimal official Reddit API fallback for one already discovered post."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from urllib.parse import urlencode

from uap_platform.collectors import FetchResponse, UrlLibFetcher

_POST_FULLNAME = re.compile(r"^t3_[a-z0-9]+$")
_OAUTH_API_INFO = "https://oauth.reddit.com/api/info"

Fetch = Callable[[str, Mapping[str, str]], FetchResponse]


class RedditOfficialApiClient:
    """Fetch one provenance-bound post through Reddit's OAuth API."""

    def __init__(
        self,
        *,
        access_token: str | None,
        user_agent: str | None,
        fetch: Fetch | None = None,
    ) -> None:
        self._access_token = (access_token or "").strip()
        self._user_agent = (user_agent or "").strip()
        self._fetch = fetch or UrlLibFetcher(timeout_seconds=20, user_agent=self._user_agent)

    def fetch_post(self, fullname: str) -> FetchResponse:
        if not self._access_token or not self._user_agent:
            return FetchResponse(
                status_code=401,
                error_code="reddit_api_credentials_unavailable",
                error_summary="Reddit OAuth access token and user agent are required",
            )
        if not _POST_FULLNAME.fullmatch(fullname):
            return FetchResponse(
                status_code=422,
                error_code="reddit_api_invalid_post_id",
                error_summary="Reddit post fullname is invalid",
            )
        query = urlencode({"id": fullname, "raw_json": "1"})
        response = self._fetch(
            f"{_OAUTH_API_INFO}?{query}",
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {self._access_token}",
                "User-Agent": self._user_agent,
            },
        )
        if response.status_code in {401, 403}:
            return self._error(response, "reddit_api_auth_failed", "Reddit OAuth failed")
        if response.status_code == 429:
            return self._error(
                response, "reddit_api_rate_limited", "Reddit API rate limit reached"
            )
        if response.status_code >= 500:
            return self._error(
                response, "reddit_api_server_error", "Reddit API server failure"
            )
        if not 200 <= response.status_code < 300:
            return self._error(response, "reddit_api_request_failed", "Reddit API request failed")
        media_type = (response.header("content-type") or "").split(";", 1)[0].casefold()
        if media_type == "application/json":
            mismatch = self._provenance_error(response.body, fullname)
            if mismatch is not None:
                return mismatch
        return response

    @staticmethod
    def _provenance_error(payload: bytes, fullname: str) -> FetchResponse | None:
        try:
            decoded = json.loads(payload)
            children = decoded["data"]["children"]
            post = children[0]["data"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
            return None
        if not isinstance(post, dict) or post.get("name") != fullname:
            return FetchResponse(
                status_code=422,
                error_code="reddit_api_provenance_mismatch",
                error_summary="Reddit API response does not match the discovered post",
            )
        return None

    @staticmethod
    def _error(response: FetchResponse, code: str, summary: str) -> FetchResponse:
        return FetchResponse(
            status_code=response.status_code,
            body=response.body,
            headers=response.headers,
            retrieved_at=response.retrieved_at,
            error_code=code,
            error_summary=summary,
            payload_schema_version=response.payload_schema_version,
        )
