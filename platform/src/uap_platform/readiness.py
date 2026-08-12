"""Readiness checks for the WP2 PostgreSQL and object-storage services."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import psycopg
from minio import Minio

from uap_platform.config import Settings, load_settings


def check_postgres(settings: Settings) -> dict[str, object]:
    """Verify that PostgreSQL accepts a query and report no credentials."""

    database_url = settings.database_url.get_secret_value().replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    with psycopg.connect(database_url, connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("PostgreSQL readiness query returned no row")
            database, user = row
    return {"ready": True, "database": database, "user": user}


def check_object_storage(settings: Settings) -> dict[str, object]:
    """Verify all required S3-compatible buckets exist."""

    client = Minio(
        settings.s3_endpoint,
        access_key=settings.s3_access_key.get_secret_value(),
        secret_key=settings.s3_secret_key.get_secret_value(),
        secure=settings.s3_secure,
    )
    missing = [name for name in settings.bucket_names if not client.bucket_exists(name)]
    if missing:
        raise RuntimeError(f"required object-storage buckets are missing: {', '.join(missing)}")
    return {"ready": True, "buckets": list(settings.bucket_names)}


def collect_readiness(
    settings: Settings,
    postgres_check: Callable[[Settings], dict[str, object]] = check_postgres,
    object_storage_check: Callable[[Settings], dict[str, object]] = check_object_storage,
) -> tuple[int, dict[str, Any]]:
    """Collect checks without exposing exception details that may contain secrets."""

    payload: dict[str, Any] = {"status": "ready", "checks": {}}
    status_code = 200
    for name, checker in (
        ("postgres", postgres_check),
        ("object_storage", object_storage_check),
    ):
        try:
            payload["checks"][name] = checker(settings)
        except Exception:
            status_code = 503
            payload["status"] = "not_ready"
            payload["checks"][name] = {"ready": False, "error": "dependency unavailable"}
    return status_code, payload


def main() -> None:
    """Run readiness once for Compose and deployment scripts."""

    status_code, payload = collect_readiness(load_settings())
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if status_code != 200:
        raise SystemExit(1)
