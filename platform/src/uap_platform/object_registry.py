"""Content-addressed object storage with authoritative PostgreSQL registration."""

from __future__ import annotations

import hashlib
import io
import secrets
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, cast

from psycopg import Connection


class StorageDomain(StrEnum):
    RAW = "raw"
    DERIVED = "derived"
    MODEL_IO = "model_io"
    PUBLIC_ASSETS = "public_assets"
    BACKUP = "backup"


BUCKETS: dict[StorageDomain, str] = {
    StorageDomain.RAW: "raw",
    StorageDomain.DERIVED: "derived",
    StorageDomain.MODEL_IO: "model-io",
    StorageDomain.PUBLIC_ASSETS: "public-assets",
    StorageDomain.BACKUP: "backups",
}


class ObjectClient(Protocol):
    def bucket_exists(self, bucket_name: str) -> bool: ...

    def make_bucket(self, bucket_name: str) -> None: ...

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: io.BytesIO,
        length: int,
        content_type: str,
        metadata: dict[str, str],
    ) -> object: ...

    def get_object(self, bucket_name: str, object_name: str) -> object: ...

    def stat_object(self, bucket_name: str, object_name: str) -> object: ...

    def remove_object(self, bucket_name: str, object_name: str) -> None: ...


class ObjectStat(Protocol):
    size: int


class ObjectResponse(Protocol):
    def read(self) -> bytes: ...

    def close(self) -> None: ...

    def release_conn(self) -> None: ...


@dataclass(frozen=True)
class PhysicalObject:
    storage_domain: StorageDomain
    bucket_name: str
    object_key: str
    content_sha256: str
    byte_length: int
    media_type: str
    created: bool = field(default=False, kw_only=True)


@dataclass(frozen=True)
class RegisteredObject(PhysicalObject):
    id: uuid.UUID
    reused: bool


def uuid7() -> uuid.UUID:
    """Generate an RFC 9562 UUIDv7 without relying on Python 3.14's uuid.uuid7."""

    unix_ms = int(time.time_ns() // 1_000_000)
    random_bits = secrets.randbits(74)
    value = (unix_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((random_bits >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= random_bits & ((1 << 62) - 1)
    return uuid.UUID(int=value)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def object_key(domain: StorageDomain, digest: str) -> str:
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("digest must be a lowercase SHA-256 value")
    return f"{domain.value}/{digest}"


def _response_bytes(response: object) -> bytes:
    typed = cast(ObjectResponse, response)
    try:
        return bytes(typed.read())
    finally:
        typed.close()
        typed.release_conn()


def put_verified(
    client: ObjectClient,
    domain: StorageDomain,
    data: bytes,
    media_type: str,
    *,
    expected_sha256: str | None = None,
) -> PhysicalObject:
    """Write immutable content and verify it by reading the stored bytes back."""

    digest = sha256_bytes(data)
    if expected_sha256 is not None and not secrets.compare_digest(digest, expected_sha256):
        raise ValueError("content SHA-256 does not match the expected digest")

    bucket = BUCKETS[domain]
    key = object_key(domain, digest)
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)

    exists = False
    try:
        stat = client.stat_object(bucket, key)
        exists = True
        if int(cast(ObjectStat, stat).size) != len(data):
            raise RuntimeError("stored object length conflicts with its content address")
    except Exception as error:
        if error.__class__.__name__ not in {"S3Error", "NoSuchKey", "NoSuchObject"}:
            raise
        code = str(getattr(error, "code", ""))
        if code not in {"NoSuchKey", "NoSuchObject", "NoSuchBucket", "XMinioInvalidObjectName"}:
            raise

    if not exists:
        client.put_object(
            bucket,
            key,
            io.BytesIO(data),
            len(data),
            media_type,
            {"sha256": digest},
        )

    stored = _response_bytes(client.get_object(bucket, key))
    if len(stored) != len(data) or not secrets.compare_digest(sha256_bytes(stored), digest):
        if not exists:
            client.remove_object(bucket, key)
        raise RuntimeError("object storage verification failed")

    return PhysicalObject(
        domain,
        bucket,
        key,
        digest,
        len(data),
        media_type,
        created=not exists,
    )


def register_object(connection: Connection[object], physical: PhysicalObject) -> RegisteredObject:
    """Register or reuse one object row per storage domain and SHA-256."""

    candidate_id = uuid7()
    with connection.cursor() as cursor:
        # Serialise registration and rollback compensation for this content address.
        # The lock is transaction-scoped and therefore also covers another writer
        # that raced the physical PUT before either transaction registered a row.
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{physical.storage_domain.value}:{physical.content_sha256}",),
        )
        cursor.execute(
            """
            INSERT INTO core.stored_objects (
                id, storage_domain, bucket_name, object_key, content_sha256,
                byte_length, media_type, verified_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (storage_domain, content_sha256) DO UPDATE
            SET verified_at = EXCLUDED.verified_at
            RETURNING id, bucket_name, object_key, byte_length, media_type
            """,
            (
                candidate_id,
                physical.storage_domain.value,
                physical.bucket_name,
                physical.object_key,
                physical.content_sha256,
                physical.byte_length,
                physical.media_type,
            ),
        )
        row = cast(tuple[uuid.UUID, str, str, int, str] | None, cursor.fetchone())
    if row is None:
        raise RuntimeError("stored object registration did not return a row")
    object_id, bucket, key, length, media_type = row
    if (bucket, key, int(length), media_type) != (
        physical.bucket_name,
        physical.object_key,
        physical.byte_length,
        physical.media_type,
    ):
        raise RuntimeError("stored object registry conflicts with physical object metadata")
    return RegisteredObject(
        physical.storage_domain,
        str(bucket),
        str(key),
        physical.content_sha256,
        int(length),
        str(media_type),
        uuid.UUID(str(object_id)),
        uuid.UUID(str(object_id)) != candidate_id,
        created=physical.created,
    )


def cleanup_unregistered_object(
    connection: Connection[object], client: ObjectClient, registered: RegisteredObject
) -> bool:
    """Remove a physical object created by a failed transaction if it is unreferenced.

    The same transaction-scoped advisory lock used by ``register_object`` prevents a
    concurrent registrar from appearing between the reference check and the delete.
    Existing or concurrently reused content is never deleted.
    """

    if not registered.created:
        return False
    lock_key = f"{registered.storage_domain.value}:{registered.content_sha256}"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (lock_key,),
        )
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM core.stored_objects
                 WHERE storage_domain = %s::core.storage_domain
                   AND content_sha256 = %s
            )
            """,
            (registered.storage_domain.value, registered.content_sha256),
        )
        row = cast(tuple[bool] | None, cursor.fetchone())
    if row is not None and bool(row[0]):
        return False
    client.remove_object(registered.bucket_name, registered.object_key)
    return True


def store_and_register(
    client: ObjectClient,
    connection: Connection[object],
    domain: StorageDomain,
    data: bytes,
    media_type: str,
    *,
    expected_sha256: str | None = None,
) -> RegisteredObject:
    physical = put_verified(
        client,
        domain,
        data,
        media_type,
        expected_sha256=expected_sha256,
    )
    return register_object(connection, physical)
