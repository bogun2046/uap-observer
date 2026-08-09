from __future__ import annotations

import tempfile
import unittest
from email.message import Message
from pathlib import Path
from subprocess import CompletedProcess
from typing import ClassVar
from unittest.mock import patch
from urllib.error import HTTPError

from uap_observer.database import Database
from uap_observer.http_fetch import HttpFetcher
from uap_observer.models import FactStatus, NewsCategory, Source, SourceType
from uap_observer.repositories import Repository

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    status = 200
    headers: ClassVar[Message] = Message()

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class HttpFetchTests(unittest.TestCase):
    def test_429_retry_honors_retry_after(self) -> None:
        headers = Message()
        headers["Retry-After"] = "5"
        rate_limited = HTTPError(
            "https://example.test/feed.xml",
            429,
            "Too Many Requests",
            headers,
            None,
        )
        sleeps: list[float] = []
        fetcher = HttpFetcher(
            timeout=1,
            max_retries=1,
            min_host_interval=0,
            allow_curl_fallback=False,
            sleep=sleeps.append,
        )

        with patch(
            "uap_observer.http_fetch.urllib.request.urlopen",
            side_effect=[rate_limited, FakeResponse(b"feed")],
        ):
            response = fetcher.fetch(
                "https://example.test/feed.xml",
                accept="application/rss+xml",
                etag=None,
                last_modified=None,
            )

        self.assertEqual(response.body, b"feed")
        self.assertEqual(sleeps, [5.0])

    def test_curl_accepts_non_empty_partial_2xx_response(self) -> None:
        result = CompletedProcess(
            args=["curl"],
            returncode=18,
            stdout=b"<html>partial</html>\nUAP_HTTP_STATUS:200\n",
            stderr=b"curl: (18) transfer closed with outstanding read data remaining\n",
        )
        fetcher = HttpFetcher(
            timeout=1,
            max_retries=0,
            min_host_interval=0,
            sleep=lambda _: None,
        )

        with (
            patch("uap_observer.http_fetch.urllib.request.urlopen", side_effect=OSError("TLS")),
            patch("uap_observer.http_fetch.shutil.which", return_value="/usr/bin/curl"),
            patch("uap_observer.http_fetch.subprocess.run", return_value=result),
        ):
            response = fetcher.fetch(
                "https://example.test/page",
                accept="text/html",
                etag=None,
                last_modified=None,
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"<html>partial</html>")

    def test_curl_retries_http2_failure_with_http1_1(self) -> None:
        http2_failure = CompletedProcess(
            args=["curl"],
            returncode=92,
            stdout=b"\nUAP_HTTP_STATUS:000\n",
            stderr=b"curl: (92) HTTP/2 stream was not closed cleanly: INTERNAL_ERROR\n",
        )
        http1_success = CompletedProcess(
            args=["curl"],
            returncode=0,
            stdout=b"<html>naa record</html>\nUAP_HTTP_STATUS:200\n",
            stderr=b"HTTP/1.1 200 OK\r\nETag: \"naa-v1\"\r\n\r\n",
        )
        fetcher = HttpFetcher(
            timeout=1,
            max_retries=0,
            min_host_interval=0,
            sleep=lambda _: None,
        )

        with (
            patch("uap_observer.http_fetch.urllib.request.urlopen", side_effect=OSError("TLS")),
            patch("uap_observer.http_fetch.shutil.which", return_value="/usr/bin/curl"),
            patch(
                "uap_observer.http_fetch.subprocess.run",
                side_effect=[http2_failure, http1_success],
            ) as run,
        ):
            response = fetcher.fetch(
                "https://www.naa.gov.au/record",
                accept="text/html",
                etag=None,
                last_modified=None,
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"<html>naa record</html>")
        self.assertEqual(response.etag, '"naa-v1"')
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("--http1.1", run.call_args_list[0].args[0])
        self.assertIn("--http1.1", run.call_args_list[1].args[0])

    def test_curl_retries_zero_byte_timeout_with_http1_1(self) -> None:
        timeout = CompletedProcess(
            args=["curl"],
            returncode=28,
            stdout=b"\nUAP_HTTP_STATUS:000\n",
            stderr=b"curl: (28) Operation timed out with 0 bytes received\n",
        )
        http1_success = CompletedProcess(
            args=["curl"],
            returncode=0,
            stdout=b"<html>naa record</html>\nUAP_HTTP_STATUS:200\n",
            stderr=b"HTTP/1.1 200 OK\r\n\r\n",
        )
        fetcher = HttpFetcher(
            timeout=1,
            max_retries=0,
            min_host_interval=0,
            sleep=lambda _: None,
        )

        with (
            patch("uap_observer.http_fetch.urllib.request.urlopen", side_effect=OSError("TLS")),
            patch("uap_observer.http_fetch.shutil.which", return_value="/usr/bin/curl"),
            patch(
                "uap_observer.http_fetch.subprocess.run",
                side_effect=[timeout, http1_success],
            ) as run,
        ):
            response = fetcher.fetch(
                "https://www.naa.gov.au/record",
                accept="text/html",
                etag=None,
                last_modified=None,
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"<html>naa record</html>")
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("--http1.1", run.call_args_list[0].args[0])
        self.assertIn("--http1.1", run.call_args_list[1].args[0])

    def test_source_failure_records_cooldown_and_success_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "test.db", PROJECT_ROOT / "migrations")
            database.initialize()
            repository = Repository(database)
            source = Source(
                slug="cooldown-test",
                name="Cooldown Test",
                source_type=SourceType.RSS,
                homepage_url="https://example.test/",
                feed_url="https://example.test/feed.xml",
                default_category=NewsCategory.OFFICIAL_REPORT,
                default_credibility=5,
                default_fact_status=FactStatus.OFFICIAL_RECORD,
            )
            source.id = repository.upsert_source(source)

            repository.record_source_fetch(
                source.id,
                error="HTTP Error 429",
                cooldown_seconds=900,
            )
            failed = repository.get_sources(slug=source.slug)[0]
            self.assertEqual(failed.consecutive_failures, 1)
            self.assertIsNotNone(failed.next_retry_at)

            repository.record_source_fetch(source.id)
            recovered = repository.get_sources(slug=source.slug)[0]
            self.assertEqual(recovered.consecutive_failures, 0)
            self.assertIsNone(recovered.next_retry_at)


if __name__ == "__main__":
    unittest.main()
