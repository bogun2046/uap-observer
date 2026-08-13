"""Back up, restore, and verify every PostgreSQL-registered immutable object."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import psycopg

from uap_platform.config import load_settings
from uap_platform.object_store_init import build_client


@dataclass(frozen=True)
class ManifestObject:
    id: str
    storage_domain: str
    bucket_name: str
    object_key: str
    content_sha256: str
    byte_length: int
    media_type: str
    backup_path: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_backup_path(item: ManifestObject, root: Path) -> Path:
    relative = PurePosixPath(item.backup_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("object backup path must stay inside the backup directory")
    path = root.joinpath(*relative.parts).resolve()
    if root.resolve() not in path.parents:
        raise ValueError("object backup path escapes the backup directory")
    return path


def response_bytes(response: Any) -> bytes:
    try:
        return bytes(response.read())
    finally:
        response.close()
        response.release_conn()


def registry_rows(connection: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, storage_domain::text, bucket_name, object_key,
                   content_sha256, byte_length, media_type
              FROM core.stored_objects
             ORDER BY storage_domain, content_sha256
            """
        )
        return list(cursor.fetchall())


def build_manifest(connection: psycopg.Connection[Any]) -> list[ManifestObject]:
    return [
        ManifestObject(
            id=str(row[0]),
            storage_domain=str(row[1]),
            bucket_name=str(row[2]),
            object_key=str(row[3]),
            content_sha256=str(row[4]),
            byte_length=int(row[5]),
            media_type=str(row[6]),
            backup_path=f"objects/{row[1]}/{row[4]}",
        )
        for row in registry_rows(connection)
    ]


def validate_file(item: ManifestObject, path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"missing backup object: {item.backup_path}")
    if path.stat().st_size != item.byte_length:
        raise RuntimeError(f"object length mismatch: {item.backup_path}")
    if sha256_file(path) != item.content_sha256:
        raise RuntimeError(f"object SHA-256 mismatch: {item.backup_path}")


def write_manifest(root: Path, items: list[ManifestObject]) -> None:
    payload = {
        "format": "uap-object-backup-v1",
        "objects": [asdict(item) for item in items],
    }
    manifest = root / "objects-manifest.json"
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / "objects-manifest.sha256").write_text(
        f"{sha256_file(manifest)}  objects-manifest.json\n",
        encoding="utf-8",
    )


def read_manifest(root: Path) -> list[ManifestObject]:
    manifest = root / "objects-manifest.json"
    checksum = (root / "objects-manifest.sha256").read_text(encoding="utf-8").split()[0]
    if sha256_file(manifest) != checksum:
        raise RuntimeError("object manifest SHA-256 mismatch")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if payload.get("format") != "uap-object-backup-v1":
        raise RuntimeError("unsupported object backup format")
    return [ManifestObject(**item) for item in payload["objects"]]


def backup(root: Path) -> None:
    settings = load_settings()
    client = build_client(settings)
    root.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(settings.psycopg_database_url) as connection:
        items = build_manifest(connection)
    for item in items:
        path = safe_backup_path(item, root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response_bytes(client.get_object(item.bucket_name, item.object_key)))
        validate_file(item, path)
    write_manifest(root, items)
    print(json.dumps({"operation": "backup", "objects": len(items)}, sort_keys=True))


def restore(root: Path) -> None:
    settings = load_settings()
    client = build_client(settings)
    items = read_manifest(root)
    for item in items:
        path = safe_backup_path(item, root)
        validate_file(item, path)
        if not client.bucket_exists(item.bucket_name):
            client.make_bucket(item.bucket_name)
        client.put_object(
            item.bucket_name,
            item.object_key,
            path.open("rb"),
            item.byte_length,
            item.media_type,
            metadata={"sha256": item.content_sha256},
        )
        restored = response_bytes(client.get_object(item.bucket_name, item.object_key))
        if hashlib.sha256(restored).hexdigest() != item.content_sha256:
            raise RuntimeError(f"restored object SHA-256 mismatch: {item.object_key}")
    print(json.dumps({"operation": "restore", "objects": len(items)}, sort_keys=True))


def verify() -> None:
    settings = load_settings()
    client = build_client(settings)
    with psycopg.connect(settings.psycopg_database_url) as connection:
        items = build_manifest(connection)
    expected = {(item.bucket_name, item.object_key) for item in items}
    actual: set[tuple[str, str]] = set()
    for bucket in sorted(settings.bucket_names):
        actual.update(
            (bucket, found.object_name)
            for found in client.list_objects(bucket, recursive=True)
        )
    if actual != expected:
        raise RuntimeError(
            "object registry mismatch: "
            f"missing={len(expected - actual)} extra={len(actual - expected)}"
        )
    for item in items:
        payload = response_bytes(client.get_object(item.bucket_name, item.object_key))
        if (
            len(payload) != item.byte_length
            or hashlib.sha256(payload).hexdigest() != item.content_sha256
        ):
            raise RuntimeError(f"registered object verification failed: {item.object_key}")
    print(json.dumps({"operation": "verify", "objects": len(items)}, sort_keys=True))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("backup", "restore", "verify"))
    parser.add_argument("--directory", type=Path, default=Path("/backup"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.operation == "backup":
        backup(args.directory)
    elif args.operation == "restore":
        restore(args.directory)
    else:
        verify()


if __name__ == "__main__":
    main()
