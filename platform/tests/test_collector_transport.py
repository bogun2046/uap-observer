from __future__ import annotations

from http.client import IncompleteRead
from typing import ClassVar
from urllib.error import URLError

import pytest

from uap_platform.collectors import FetchClassification, UrlLibFetcher, transport


class FakeResponse:
    status = 200
    headers: ClassVar[dict[str, str]] = {"ETag": "fixed"}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return b"payload"


def test_urllib_fetcher_returns_response_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    response = UrlLibFetcher()("https://example.test/feed", {})

    assert response.status_code == 200
    assert response.body == b"payload"
    assert response.header("etag") == "fixed"


def test_urllib_fetcher_maps_network_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        transport,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError(TimeoutError("timed out"))),
    )

    response = UrlLibFetcher()("https://example.test/feed", {})

    assert response.classify() is FetchClassification.TRANSIENT_FAILURE
    assert response.error_code == "timeout"


def test_urllib_fetcher_maps_incomplete_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class IncompleteResponse(FakeResponse):
        def read(self) -> bytes:
            raise IncompleteRead(b"partial", 12)

    monkeypatch.setattr(transport, "urlopen", lambda *_args, **_kwargs: IncompleteResponse())

    response = UrlLibFetcher()("https://example.test/article", {})

    assert response.classify() is FetchClassification.TRANSIENT_FAILURE
    assert response.error_code == "incomplete_response"
    assert "7 bytes" in str(response.error_summary)


def test_urllib_fetcher_rejects_non_http_scheme() -> None:
    with pytest.raises(ValueError, match=r"HTTP\(S\)"):
        UrlLibFetcher()("file:///tmp/feed.xml", {})
