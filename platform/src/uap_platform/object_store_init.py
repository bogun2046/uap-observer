"""Idempotently create the private S3-compatible buckets required by UAP."""

from __future__ import annotations

import json
import time

from minio import Minio

from uap_platform.config import Settings, load_settings


def build_client(settings: Settings) -> Minio:
    """Build a client without exposing credentials in logs or exceptions."""

    return Minio(
        settings.s3_endpoint,
        access_key=settings.s3_access_key.get_secret_value(),
        secret_key=settings.s3_secret_key.get_secret_value(),
        secure=settings.s3_secure,
    )


def ensure_buckets(client: Minio, bucket_names: tuple[str, ...]) -> dict[str, object]:
    """Create missing buckets and return a secret-free result."""

    created: list[str] = []
    for name in bucket_names:
        if not client.bucket_exists(name):
            client.make_bucket(name)
            created.append(name)
    return {"ready": True, "buckets": list(bucket_names), "created": created}


def initialize_with_retry(settings: Settings, attempts: int = 30) -> dict[str, object]:
    """Wait for object storage and initialize buckets with bounded retries."""

    for attempt in range(1, attempts + 1):
        try:
            return ensure_buckets(build_client(settings), settings.bucket_names)
        except Exception:
            if attempt == attempts:
                raise RuntimeError("object-storage initialization failed") from None
            time.sleep(2)
    raise AssertionError("retry loop exhausted unexpectedly")


def main() -> None:
    """Initialize required buckets and print only non-sensitive state."""

    result = initialize_with_retry(load_settings())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
