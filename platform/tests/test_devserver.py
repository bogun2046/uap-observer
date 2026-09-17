from __future__ import annotations

import json
from io import BytesIO

import pytest
from pydantic import SecretStr

from uap_platform import devserver
from uap_platform.config import Settings


def make_settings() -> Settings:
    return Settings(
        database_url=SecretStr("postgresql://user:database-secret@postgres/db"),
        s3_endpoint="object-store:8333",
        s3_access_key=SecretStr("access-secret"),
        s3_secret_key=SecretStr("object-secret"),
    )


def test_health_handler_serves_ready_and_rejects_unknown_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    devserver.HealthHandler.settings = make_settings()
    monkeypatch.setattr(
        devserver,
        "collect_readiness",
        lambda _: (200, {"status": "ready", "checks": {}}),
    )
    handler = object.__new__(StubHealthHandler)
    handler.path = "/healthz"
    handler.wfile = BytesIO()
    handler.do_GET()

    assert handler.status_code == 200
    assert json.loads(handler.wfile.getvalue())["status"] == "ready"

    handler.path = "/missing"
    handler.do_GET()
    assert handler.error_code == 404


class StubHealthHandler(devserver.HealthHandler):
    status_code: int | None = None
    error_code: int | None = None

    def send_response(self, code: int, message: str | None = None) -> None:
        self.status_code = code

    def send_header(self, keyword: str, value: str) -> None:
        return None

    def end_headers(self) -> None:
        return None

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        self.error_code = code


class FakeServer:
    served = False

    def __init__(self, address: tuple[str, int], handler: object) -> None:
        assert address == ("0.0.0.0", 8080)
        assert handler is devserver.HealthHandler

    def serve_forever(self) -> None:
        self.served = True


def test_main_uses_safe_summary_and_starts_server(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = make_settings()
    monkeypatch.setattr(devserver, "load_settings", lambda: settings)
    monkeypatch.setattr(devserver, "ThreadingHTTPServer", FakeServer)

    devserver.main()

    assert "database-secret" not in caplog.text
    assert "object-secret" not in caplog.text
