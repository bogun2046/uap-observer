"""Fail-closed disposable object-store gate for G10-25 write probes.

Shared instances such as ``uap-wp3-test-object-store-1`` are rejected. The
probe records endpoint identity, bucket existence, object counts, and an
inventory digest, then deletes only objects created this run. Credentials never
enter argv, logs, or evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterable
from io import BytesIO
from typing import Any, Protocol, cast

from uap_platform.config import Settings, load_settings
from uap_platform.object_store_init import build_client

SENTINEL_KEY = "_uap/g10-25-disposable.json"
SENTINEL_SCHEMA = "uap-g10-25-disposable.v1"
DISPOSABLE_FLAG_VALUES = frozenset({"1", "true", "TRUE", "yes", "YES"})
FORBIDDEN_ENDPOINT_MARKERS = (
    "uap-wp3-test-object-store-1",
    "uap-wp3-test",
    "uap-wp10-impl",
)
SHARED_PLATFORM_ENDPOINTS = frozenset(
    {
        "object-store",
        "object-store:8333",
        "127.0.0.1:8333",
        "localhost:8333",
    }
)
HOST_ENDPOINT_PREFIX = "127.0.0.1:"
SIDECAR_ENDPOINT = "g10-25-object-store:8333"
WRITE_BUCKETS: tuple[str, ...] = ("raw", "derived", "model-io", "public-assets", "backups")


class ObjectStoreGuardError(RuntimeError):
    """Fail-closed object-store error. Message must not contain credentials."""


class ObjectStoreClient(Protocol):
    def bucket_exists(self, name: str) -> bool: ...

    def list_objects(self, bucket: str, *, recursive: bool = True) -> Iterable[object]: ...

    def get_object(self, bucket: str, key: str) -> object: ...

    def put_object(
        self,
        bucket: str,
        key: str,
        data: object,
        length: int,
        content_type: str = "application/json",
    ) -> object: ...

    def remove_object(self, bucket: str, key: str) -> None: ...


def _object_name(item: object) -> str:
    name = getattr(item, "object_name", item)
    return str(name)


def _read_body(response: object) -> bytes:
    if isinstance(response, (bytes, bytearray)):
        return bytes(response)
    read = getattr(response, "read", None)
    try:
        if not callable(read):
            raise ObjectStoreGuardError("object body is unreadable")
        raw = read()
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw)
        raise ObjectStoreGuardError("object body is not bytes")
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
        release = getattr(response, "release_conn", None)
        if callable(release):
            release()


def endpoint_identity(settings: Settings) -> dict[str, object]:
    endpoint = normalize_endpoint(settings.s3_endpoint)
    return {
        "endpoint": endpoint,
        "secure": settings.s3_secure,
        "buckets": list(settings.bucket_names),
        "host_endpoint_ok": endpoint.startswith(HOST_ENDPOINT_PREFIX)
        and not endpoint.endswith(":8333"),
        "sidecar_endpoint_ok": endpoint == SIDECAR_ENDPOINT,
    }


def normalize_endpoint(endpoint: str) -> str:
    """Return a Minio host:port string. Python 3.12 Minio rejects a scheme prefix."""

    value = endpoint.strip()
    lowered = value.lower()
    for prefix in ("http://", "https://"):
        if lowered.startswith(prefix):
            value = value[len(prefix) :]
            lowered = value.lower()
            break
    value = value.rstrip("/")
    if value.lower().startswith("localhost:"):
        value = "127.0.0.1:" + value.split(":", 1)[1]
    elif value.lower() == "localhost":
        value = "127.0.0.1"
    return value


def is_forbidden_endpoint(endpoint: str) -> bool:
    lowered = normalize_endpoint(endpoint).lower()
    if lowered in SHARED_PLATFORM_ENDPOINTS:
        return True
    return any(marker in lowered for marker in FORBIDDEN_ENDPOINT_MARKERS)


def apply_endpoint_contract(settings: Settings) -> Settings:
    settings.s3_endpoint = normalize_endpoint(settings.s3_endpoint)
    if is_forbidden_endpoint(settings.s3_endpoint):
        raise ObjectStoreGuardError("refusing shared object-store endpoint")
    return settings


def require_disposable_id() -> str:
    flag = os.environ.get("UAP_S3_DISPOSABLE", "")
    if flag not in DISPOSABLE_FLAG_VALUES:
        raise ObjectStoreGuardError("object-store is not marked disposable")
    ident = os.environ.get("UAP_S3_DISPOSABLE_ID", "").strip()
    if len(ident) < 8:
        raise ObjectStoreGuardError("missing disposable object-store id")
    return ident


def inventory_digest(keys: Iterable[str]) -> str:
    blob = "\n".join(sorted(keys)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def collect_inventory(client: ObjectStoreClient, buckets: tuple[str, ...]) -> dict[str, Any]:
    exists: dict[str, bool] = {}
    counts: dict[str, int] = {}
    keys: list[str] = []
    for bucket in buckets:
        present = bool(client.bucket_exists(bucket))
        exists[bucket] = present
        if not present:
            counts[bucket] = 0
            continue
        names = [_object_name(item) for item in client.list_objects(bucket, recursive=True)]
        names = [name for name in names if name and name != "None"]
        counts[bucket] = len(names)
        keys.extend(f"{bucket}/{name}" for name in names)
    ordered = tuple(sorted(keys))
    return {
        "bucket_exists": exists,
        "counts": counts,
        "keys": ordered,
        "digest": inventory_digest(ordered),
        "object_count": len(ordered),
    }


def _buckets(settings: Settings) -> tuple[str, ...]:
    names = settings.bucket_names
    return names if names else WRITE_BUCKETS


def _sentinel_bucket(buckets: tuple[str, ...]) -> str:
    if "raw" in buckets:
        return "raw"
    if not buckets:
        raise ObjectStoreGuardError("no object-store buckets configured")
    return buckets[0]


def sentinel_payload(disposable_id: str) -> dict[str, object]:
    return {
        "schema": SENTINEL_SCHEMA,
        "disposable": True,
        "id": disposable_id,
    }


def _get_client(client: ObjectStoreClient | None, settings: Settings) -> ObjectStoreClient:
    if client is not None:
        return client
    return cast(ObjectStoreClient, build_client(settings))


def install_sentinel(
    *,
    client: ObjectStoreClient | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    loaded = settings if settings is not None else load_settings()
    loaded = apply_endpoint_contract(loaded)
    disposable_id = require_disposable_id()
    store = _get_client(client, loaded)
    buckets = _buckets(loaded)
    bucket = _sentinel_bucket(buckets)
    if not store.bucket_exists(bucket):
        raise ObjectStoreGuardError("sentinel bucket does not exist")
    body = json.dumps(sentinel_payload(disposable_id), separators=(",", ":")).encode("utf-8")
    store.put_object(bucket, SENTINEL_KEY, BytesIO(body), len(body), "application/json")
    return {
        "status": "passed",
        "sentinel_bucket": bucket,
        "sentinel_key": SENTINEL_KEY,
        "identity": endpoint_identity(loaded),
        "disposable_id": disposable_id,
    }


def _require_sentinel(client: ObjectStoreClient, bucket: str, disposable_id: str) -> None:
    try:
        body = _read_body(client.get_object(bucket, SENTINEL_KEY))
    except Exception as exc:
        raise ObjectStoreGuardError("disposable sentinel is missing") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObjectStoreGuardError("disposable sentinel is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ObjectStoreGuardError("disposable sentinel has invalid shape")
    if payload.get("schema") != SENTINEL_SCHEMA or payload.get("disposable") is not True:
        raise ObjectStoreGuardError("object-store is not marked disposable")
    if payload.get("id") != disposable_id:
        raise ObjectStoreGuardError("disposable sentinel id mismatch")


def _allowed_preexisting(bucket: str) -> frozenset[str]:
    return frozenset({f"{bucket}/{SENTINEL_KEY}"})


def _evidence_inventory(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "bucket_exists": snapshot["bucket_exists"],
        "counts": snapshot["counts"],
        "digest": snapshot["digest"],
        "object_count": snapshot["object_count"],
    }


class DisposableObjectStoreGuard:
    """Preflight, inventory, and exact-key cleanup for one G10-25 run."""

    def __init__(
        self,
        *,
        client: ObjectStoreClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._before_keys: tuple[str, ...] | None = None
        self._before_digest: str | None = None
        self._preflight_ok = False
        self._writes = 0

    def _load(self) -> tuple[Settings, ObjectStoreClient]:
        loaded = self._settings if self._settings is not None else load_settings()
        loaded = apply_endpoint_contract(loaded)
        return loaded, _get_client(self._client, loaded)

    def preflight(self) -> dict[str, Any]:
        loaded, store = self._load()
        identity = endpoint_identity(loaded)
        disposable_id = require_disposable_id()
        buckets = _buckets(loaded)
        missing = [name for name in buckets if not store.bucket_exists(name)]
        if missing:
            raise ObjectStoreGuardError("required object-store bucket is missing")
        sentinel_bucket = _sentinel_bucket(buckets)
        _require_sentinel(store, sentinel_bucket, disposable_id)
        snapshot = collect_inventory(store, buckets)
        extra = [
            key for key in snapshot["keys"] if key not in _allowed_preexisting(sentinel_bucket)
        ]
        if extra:
            raise ObjectStoreGuardError("pre-existing objects block disposable object-store")
        self._before_keys = snapshot["keys"]
        self._before_digest = snapshot["digest"]
        self._preflight_ok = True
        return {
            "status": "passed",
            "disposable": True,
            "disposable_id": disposable_id,
            "identity": identity,
            "inventory": _evidence_inventory(snapshot),
        }

    def note_write(self) -> None:
        if not self._preflight_ok:
            raise ObjectStoreGuardError("object-store write refused before disposable preflight")
        self._writes += 1

    def cleanup(self) -> dict[str, Any]:
        if not self._preflight_ok or self._before_keys is None:
            return {
                "status": "not_run",
                "residue": None,
                "deleted": 0,
                "detail": "preflight did not pass",
            }
        loaded, store = self._load()
        buckets = _buckets(loaded)
        before = set(self._before_keys)
        snapshot = collect_inventory(store, buckets)
        created = [key for key in snapshot["keys"] if key not in before]
        deleted = 0
        for item in created:
            bucket, _, key = item.partition("/")
            store.remove_object(bucket, key)
            deleted += 1
        final = collect_inventory(store, buckets)
        extra = [key for key in final["keys"] if key not in before]
        missing_pre = [key for key in self._before_keys if key not in final["keys"]]
        if extra:
            raise ObjectStoreGuardError("object-store residue is not zero")
        if missing_pre:
            raise ObjectStoreGuardError("pre-existing object was removed")
        if self._before_digest is not None and final["digest"] != self._before_digest:
            raise ObjectStoreGuardError("inventory digest changed after cleanup")
        return {
            "status": "passed",
            "residue": 0,
            "deleted": deleted,
            "inventory": _evidence_inventory(final),
            "writes_noted": self._writes,
        }


def default_object_store_gate() -> DisposableObjectStoreGuard:
    return DisposableObjectStoreGuard()


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="G10-25 disposable object-store helper")
    parser.add_argument("--install-sentinel", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.install_sentinel:
            _print_json(install_sentinel())
            return 0
        if args.preflight:
            _print_json(DisposableObjectStoreGuard().preflight())
            return 0
    except ObjectStoreGuardError as exc:
        _print_json({"status": "failed", "detail": str(exc)})
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
