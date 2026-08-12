from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import SecretStr

from uap_platform import object_store_init
from uap_platform.config import Settings


def make_settings() -> Settings:
    return Settings(
        database_url=SecretStr("postgresql://user:database-secret@postgres/db"),
        s3_endpoint="object-store:8333",
        s3_access_key=SecretStr("access-secret"),
        s3_secret_key=SecretStr("object-secret"),
    )


class FakeClient:
    existing: ClassVar[set[str]] = {"raw"}
    created: ClassVar[list[str]] = []

    def bucket_exists(self, name: str) -> bool:
        return name in self.existing

    def make_bucket(self, name: str) -> None:
        self.existing.add(name)
        self.created.append(name)


def test_ensure_buckets_is_idempotent() -> None:
    client = FakeClient()
    names = ("raw", "derived", "model-io", "public-assets")

    first = object_store_init.ensure_buckets(client, names)  # type: ignore[arg-type]
    second = object_store_init.ensure_buckets(client, names)  # type: ignore[arg-type]

    assert first["created"] == ["derived", "model-io", "public-assets"]
    assert second["created"] == []
    assert first["buckets"] == list(names)
    FakeClient.existing = {"raw"}
    FakeClient.created = []


def test_initialize_retries_without_leaking_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def flaky_client(_: Settings) -> FakeClient:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database-secret object-secret")
        return FakeClient()

    monkeypatch.setattr(object_store_init, "build_client", flaky_client)
    monkeypatch.setattr("uap_platform.object_store_init.time.sleep", lambda _: None)

    result = object_store_init.initialize_with_retry(make_settings(), attempts=2)

    assert result["ready"] is True
    assert "database-secret" not in repr(result)
    assert "object-secret" not in repr(result)


def test_initialize_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_client(_: Settings) -> FakeClient:
        raise RuntimeError("database-secret object-secret")

    monkeypatch.setattr(object_store_init, "build_client", failing_client)

    with pytest.raises(RuntimeError, match="object-storage initialization failed") as error:
        object_store_init.initialize_with_retry(make_settings(), attempts=1)

    assert "database-secret" not in str(error.value)
    assert "object-secret" not in str(error.value)
