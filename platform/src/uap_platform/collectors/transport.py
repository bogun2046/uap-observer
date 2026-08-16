"""HTTP transport adapters for collectors."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .contracts import FetchResponse


class UrlLibFetcher:
    """Small standard-library transport with explicit timeout classification."""

    def __init__(
        self, *, timeout_seconds: float = 20.0, user_agent: str = "uap-collector/0.1"
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._user_agent = user_agent

    def __call__(self, url: str, headers: Mapping[str, str]) -> FetchResponse:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("collector transport requires an absolute HTTP(S) URL")
        request_headers = {"User-Agent": self._user_agent, **headers}
        request = Request(url, headers=request_headers, method="GET")  # noqa: S310
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                return FetchResponse(
                    status_code=int(response.status),
                    body=response.read(),
                    headers=dict(response.headers.items()),
                )
        except HTTPError as error:
            return FetchResponse(
                status_code=error.code,
                headers=dict(error.headers.items()) if error.headers else {},
            )
        except TimeoutError as error:
            return FetchResponse(
                status_code=599,
                error_code="timeout",
                error_summary=str(error) or "source fetch timed out",
            )
        except URLError as error:
            reason = error.reason
            if isinstance(reason, TimeoutError):
                return FetchResponse(
                    status_code=599,
                    error_code="timeout",
                    error_summary=str(reason) or "source fetch timed out",
                )
            return FetchResponse(
                status_code=599,
                error_code="transport_error",
                error_summary=str(reason),
            )
