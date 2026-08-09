"""Shared resilient HTTP fetching for public source collectors."""

from __future__ import annotations

import http.client
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import ClassVar
from urllib.parse import urlparse

USER_AGENT = "UAPObserver/0.1 (+public-source research; contact via repository)"
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    etag: str | None = None
    last_modified: str | None = None


class FetchError(RuntimeError):
    """A source fetch failed after the configured retry and fallback policy."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_seconds = retry_after_seconds


class HttpFetcher:
    """Fetch HTTP resources with retry, host pacing, and optional curl fallback."""

    _host_lock: ClassVar[Lock] = Lock()
    _last_request_at: ClassVar[dict[str, float]] = {}

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
        max_retry_sleep_seconds: float = 30.0,
        min_host_interval: float = 0.25,
        allow_curl_fallback: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if retry_base_seconds < 0 or max_retry_sleep_seconds < 0 or min_host_interval < 0:
            raise ValueError("retry and host interval values must not be negative")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.max_retry_sleep_seconds = max_retry_sleep_seconds
        self.min_host_interval = min_host_interval
        self.allow_curl_fallback = allow_curl_fallback
        self.sleep = sleep

    def fetch(
        self,
        url: str,
        *,
        accept: str,
        etag: str | None,
        last_modified: str | None,
        accept_partial: bool = False,
    ) -> HttpResponse:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        last_error: Exception | None = None
        retry_after_seconds: int | None = None
        for attempt in range(self.max_retries + 1):
            self._pace(url)
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = _read_response_body(response, accept_partial=accept_partial)
                    return HttpResponse(
                        status=response.status,
                        body=body,
                        etag=response.headers.get("ETag"),
                        last_modified=response.headers.get("Last-Modified"),
                    )
            except urllib.error.HTTPError as error:
                if error.code == 304:
                    return HttpResponse(
                        status=304,
                        body=b"",
                        etag=error.headers.get("ETag"),
                        last_modified=error.headers.get("Last-Modified"),
                    )
                last_error = FetchError(
                    f"HTTP Error {error.code}: {error.reason}",
                    status=error.code,
                    retry_after_seconds=_retry_after_seconds(error.headers.get("Retry-After")),
                )
                retry_after_seconds = getattr(last_error, "retry_after_seconds", None)
                if error.code not in RETRYABLE_HTTP_STATUSES:
                    break
            except http.client.IncompleteRead as error:
                if error.partial and accept_partial:
                    return HttpResponse(status=200, body=error.partial)
                last_error = FetchError("IncompleteRead: empty response")
            except (
                OSError,
                TimeoutError,
                http.client.RemoteDisconnected,
                urllib.error.URLError,
            ) as error:
                last_error = error

            if attempt == self.max_retries:
                break
            delay = self.retry_base_seconds * (2**attempt)
            if retry_after_seconds is not None:
                delay = max(delay, float(retry_after_seconds))
            self.sleep(min(delay, self.max_retry_sleep_seconds))

        if self.allow_curl_fallback and shutil.which("curl"):
            try:
                return self._fetch_with_curl(
                    url,
                    accept=accept,
                    etag=etag,
                    last_modified=last_modified,
                )
            except FetchError:
                raise
            except Exception as error:  # noqa: BLE001 - preserve the original fetch error
                last_error = error

        if last_error:
            if isinstance(last_error, FetchError):
                raise last_error
            raise FetchError(str(last_error)) from last_error
        raise FetchError("HTTP fetch failed without an error detail")

    def _pace(self, url: str) -> None:
        host = urlparse(url).netloc.casefold()
        if not host or self.min_host_interval == 0:
            return
        with self._host_lock:
            now = time.monotonic()
            previous = self._last_request_at.get(host)
            delay = self.min_host_interval - (now - previous) if previous is not None else 0
            if delay > 0:
                self.sleep(delay)
                now = time.monotonic()
            self._last_request_at[host] = now

    def _fetch_with_curl(
        self,
        url: str,
        *,
        accept: str,
        etag: str | None,
        last_modified: str | None,
    ) -> HttpResponse:
        completed = self._run_curl(
            url,
            accept=accept,
            etag=etag,
            last_modified=last_modified,
        )
        if _curl_should_retry_with_http1_1(completed):
            completed = self._run_curl(
                url,
                accept=accept,
                etag=etag,
                last_modified=last_modified,
                http1_1=True,
            )

        return _parse_curl_response(completed, etag=etag, last_modified=last_modified)

    def _run_curl(
        self,
        url: str,
        *,
        accept: str,
        etag: str | None,
        last_modified: str | None,
        http1_1: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        command = [
            "curl",
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            str(max(1, int(self.timeout))),
            "--header",
            f"User-Agent: {USER_AGENT}",
            "--header",
            f"Accept: {accept}",
            "--header",
            "Accept-Encoding: identity",
            "--dump-header",
            "/dev/stderr",
            "--write-out",
            "\nUAP_HTTP_STATUS:%{http_code}\n",
        ]
        if http1_1:
            command.insert(1, "--http1.1")
        if etag:
            command.extend(("--header", f"If-None-Match: {etag}"))
        if last_modified:
            command.extend(("--header", f"If-Modified-Since: {last_modified}"))
        command.append(url)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=self.timeout + 5,
            )
        except subprocess.TimeoutExpired as error:
            raise FetchError("curl fallback timed out") from error
        return completed


def _parse_curl_response(
    completed: subprocess.CompletedProcess[bytes],
    *,
    etag: str | None,
    last_modified: str | None,
) -> HttpResponse:
    stderr = completed.stderr.decode("utf-8", errors="replace")
    stdout = completed.stdout
    stdout_text = stdout.decode("utf-8", errors="replace")
    status_matches = re.findall(r"UAP_HTTP_STATUS:(\d{3})", f"{stderr}\n{stdout_text}")
    body = _remove_curl_status_marker(stdout)
    if not status_matches:
        detail = _curl_error_detail(stderr, completed.returncode)
        raise FetchError(f"curl fallback failed: {detail}")
    status = int(status_matches[-1])
    if completed.returncode != 0 and not (200 <= status < 300 and body):
        detail = _curl_error_detail(stderr, completed.returncode)
        raise FetchError(f"curl fallback failed: {detail}", status=status)
    retry_after = _header_value(stderr, "Retry-After")
    if status == 304:
        return HttpResponse(status=304, body=b"", etag=etag, last_modified=last_modified)
    if status >= 400:
        raise FetchError(
            f"curl fallback returned HTTP {status}",
            status=status,
            retry_after_seconds=_retry_after_seconds(retry_after),
        )
    return HttpResponse(
        status=status,
        body=body,
        etag=_header_value(stderr, "ETag"),
        last_modified=_header_value(stderr, "Last-Modified"),
    )


def _curl_should_retry_with_http1_1(completed: subprocess.CompletedProcess[bytes]) -> bool:
    if completed.returncode == 0:
        return False
    stderr = completed.stderr.decode("utf-8", errors="replace")
    stdout = completed.stdout
    status_matches = re.findall(
        r"UAP_HTTP_STATUS:(\d{3})",
        f"{stderr}\n{stdout.decode('utf-8', errors='replace')}",
    )
    body = _remove_curl_status_marker(stdout)
    if status_matches and 200 <= int(status_matches[-1]) < 300 and body:
        return False
    return "http/2" in stderr.casefold() or completed.returncode in {16, 28, 52, 56, 92}


def _read_response_body(response: object, *, accept_partial: bool) -> bytes:
    try:
        return response.read()  # type: ignore[attr-defined]
    except http.client.IncompleteRead as error:
        if error.partial and accept_partial:
            return error.partial
        raise


def _header_value(headers: str, name: str) -> str | None:
    matches = re.findall(rf"(?im)^{re.escape(name)}:\s*(.+?)\r?$", headers)
    return matches[-1].strip() if matches else None


def _curl_error_detail(stderr: str, returncode: int) -> str:
    for line in reversed(stderr.splitlines()):
        if line.lower().startswith("curl:"):
            return line.strip()
    return stderr.strip() or f"curl exited with {returncode}"


def _remove_curl_status_marker(body: bytes) -> bytes:
    marker = b"\nUAP_HTTP_STATUS:"
    marker_start = body.rfind(marker)
    return body if marker_start == -1 else body[:marker_start]


def _retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(value.strip()))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, int((parsed - datetime.now(timezone.utc)).total_seconds()))


def cooldown_seconds_for_error(error: Exception) -> int:
    """Return a conservative source cooldown after a failed fetch."""
    if isinstance(error, FetchError):
        if error.status == 429:
            return max(900, error.retry_after_seconds or 0)
        if error.status == 403:
            return 24 * 60 * 60
        if error.status in {500, 502, 503, 504}:
            return 30 * 60
    return 30 * 60
