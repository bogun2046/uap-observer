from __future__ import annotations

import io
import uuid
from dataclasses import dataclass

import pytest

from uap_platform.object_registry import (
    PhysicalObject,
    StorageDomain,
    object_key,
    put_verified,
    register_object,
    sha256_bytes,
)


class NoSuchKey(Exception):
    code = "NoSuchKey"


@dataclass
class Stat:
    size: int


class Response(io.BytesIO):
    def release_conn(self) -> None:
        return None


class FakeClient:
    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_count = 0
        self.corrupt_reads = False

    def bucket_exists(self, bucket_name: str) -> bool:
        return bucket_name in self.buckets

    def make_bucket(self, bucket_name: str) -> None:
        self.buckets.add(bucket_name)

    def stat_object(self, bucket_name: str, object_name: str) -> Stat:
        try:
            return Stat(len(self.objects[(bucket_name, object_name)]))
        except KeyError as error:
            raise NoSuchKey from error

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: io.BytesIO,
        length: int,
        content_type: str,
        metadata: dict[str, str],
    ) -> object:
        del content_type, metadata
        payload = data.read()
        assert len(payload) == length
        self.objects[(bucket_name, object_name)] = payload
        self.put_count += 1
        return object()

    def get_object(self, bucket_name: str, object_name: str) -> Response:
        payload = self.objects[(bucket_name, object_name)]
        return Response(b"corrupt" if self.corrupt_reads else payload)

    def remove_object(self, bucket_name: str, object_name: str) -> None:
        self.objects.pop((bucket_name, object_name), None)


def test_content_address_is_domain_scoped() -> None:
    digest = "a" * 64
    assert object_key(StorageDomain.RAW, digest) == f"raw/{digest}"
    assert object_key(StorageDomain.DERIVED, digest) == f"derived/{digest}"
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        object_key(StorageDomain.RAW, "not-a-hash")


def test_put_verified_reuses_same_physical_object() -> None:
    client = FakeClient()
    content = b"fixed G3 content\x00"
    digest = sha256_bytes(content)

    first = put_verified(client, StorageDomain.RAW, content, "application/octet-stream")
    second = put_verified(
        client,
        StorageDomain.RAW,
        content,
        "application/octet-stream",
        expected_sha256=digest,
    )

    assert first == second
    assert first.content_sha256 == digest
    assert client.put_count == 1
    assert len(client.objects) == 1


def test_same_content_in_different_domains_is_not_reused() -> None:
    client = FakeClient()
    content = b"same bytes"

    raw = put_verified(client, StorageDomain.RAW, content, "text/plain")
    derived = put_verified(client, StorageDomain.DERIVED, content, "text/plain")

    assert raw.content_sha256 == derived.content_sha256
    assert raw.object_key != derived.object_key
    assert client.put_count == 2


def test_expected_hash_mismatch_writes_nothing() -> None:
    client = FakeClient()

    with pytest.raises(ValueError, match="expected digest"):
        put_verified(client, StorageDomain.RAW, b"content", "text/plain", expected_sha256="0" * 64)

    assert client.objects == {}


def test_read_after_write_corruption_is_removed() -> None:
    client = FakeClient()
    client.corrupt_reads = True

    with pytest.raises(RuntimeError, match="verification failed"):
        put_verified(client, StorageDomain.RAW, uuid.uuid4().bytes, "application/octet-stream")

    assert client.objects == {}


class FakeCursor:
    def __init__(self, row: tuple[object, ...]) -> None:
        self.row = row
        self.parameters: tuple[object, ...] | None = None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _query: str, parameters: tuple[object, ...]) -> None:
        self.parameters = parameters

    def fetchone(self) -> tuple[object, ...]:
        return self.row


class FakeConnection:
    def __init__(self, row: tuple[object, ...]) -> None:
        self.cursor_value = FakeCursor(row)

    def cursor(self) -> FakeCursor:
        return self.cursor_value


def test_register_object_returns_new_and_reused_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    physical = PhysicalObject(
        StorageDomain.RAW,
        "raw",
        "raw/" + "a" * 64,
        "a" * 64,
        3,
        "text/plain",
    )
    candidate = uuid.UUID("00000000-0000-7000-8000-000000000001")
    monkeypatch.setattr("uap_platform.object_registry.uuid7", lambda: candidate)

    created_connection = FakeConnection(
        (candidate, "raw", physical.object_key, 3, "text/plain")
    )
    reused_connection = FakeConnection(
        (uuid.uuid4(), "raw", physical.object_key, 3, "text/plain")
    )
    created = register_object(created_connection, physical)  # type: ignore[arg-type]
    reused = register_object(reused_connection, physical)  # type: ignore[arg-type]

    assert created.reused is False
    assert reused.reused is True


def test_register_object_rejects_conflicting_metadata() -> None:
    physical = PhysicalObject(
        StorageDomain.RAW, "raw", "raw/" + "b" * 64, "b" * 64, 3, "text/plain"
    )
    connection = FakeConnection((uuid.uuid4(), "raw", physical.object_key, 4, "text/plain"))

    with pytest.raises(RuntimeError, match="registry conflicts"):
        register_object(connection, physical)  # type: ignore[arg-type]
