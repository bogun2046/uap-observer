from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

import pytest
from pydantic import SecretStr

from uap_platform import readiness
from uap_platform.config import Settings
from uap_platform.readiness import check_object_storage, check_postgres, collect_readiness


def make_settings() -> Settings:
    return Settings(
        database_url=SecretStr("postgresql://user:database-secret@postgres/db"),
        s3_endpoint="object-store:8333",
        s3_access_key=SecretStr("access-secret"),
        s3_secret_key=SecretStr("object-secret"),
    )


def successful_postgres(_: Settings) -> dict[str, object]:
    return {"ready": True, "database": "uap_platform", "user": "uap_platform"}


def successful_object_storage(_: Settings) -> dict[str, object]:
    return {"ready": True, "buckets": ["raw", "derived", "model-io", "public-assets"]}


def failing_check(_: Settings) -> dict[str, object]:
    raise RuntimeError("database-secret object-secret")


def test_collect_readiness_reports_success() -> None:
    status, payload = collect_readiness(
        make_settings(),
        postgres_check=successful_postgres,
        object_storage_check=successful_object_storage,
    )

    assert status == 200
    assert payload["status"] == "ready"


def test_collect_readiness_sanitizes_dependency_errors() -> None:
    checker: Callable[[Settings], dict[str, object]] = failing_check
    status, payload = collect_readiness(
        make_settings(),
        postgres_check=checker,
        object_storage_check=successful_object_storage,
    )

    rendered = repr(payload)
    assert status == 503
    assert payload["status"] == "not_ready"
    assert "database-secret" not in rendered
    assert "object-secret" not in rendered
    assert "dependency unavailable" in rendered


class FakeCursor:
    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: str) -> None:
        assert query == "SELECT current_database(), current_user"

    def fetchone(self) -> tuple[str, str]:
        return ("uap_platform", "uap_platform")


class FakeConnection:
    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor()


def fake_connect(database_url: str, **kwargs: object) -> FakeConnection:
    assert database_url == "postgresql://user:database-secret@postgres/db"
    assert kwargs == {"connect_timeout": 5}
    return FakeConnection()


def test_postgres_check_returns_non_secret_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("uap_platform.readiness.psycopg.connect", fake_connect)

    result = check_postgres(make_settings())

    assert result == {"ready": True, "database": "uap_platform", "user": "uap_platform"}
    assert "database-secret" not in repr(result)


class FakeMinio:
    missing: ClassVar[set[str]] = set()

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        *,
        secure: bool,
    ) -> None:
        assert endpoint == "object-store:8333"
        assert access_key == "access-secret"
        assert secret_key == "object-secret"
        assert secure is False

    def bucket_exists(self, name: str) -> bool:
        return name not in self.missing


def test_object_storage_check_requires_every_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "Minio", FakeMinio)
    assert check_object_storage(make_settings())["ready"] is True

    FakeMinio.missing = {"model-io"}
    try:
        with pytest.raises(RuntimeError, match="model-io"):
            check_object_storage(make_settings())
    finally:
        FakeMinio.missing = set()


def test_readiness_main_returns_nonzero_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(readiness, "load_settings", make_settings)
    monkeypatch.setattr(
        readiness,
        "collect_readiness",
        lambda _: (503, {"status": "not_ready", "error": "dependency unavailable"}),
    )

    with pytest.raises(SystemExit, match="1"):
        readiness.main()

    output = capsys.readouterr().out
    assert "dependency unavailable" in output
    assert "database-secret" not in output
