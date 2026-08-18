from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tools.object_backup import (
    ManifestObject,
    read_manifest,
    safe_backup_path,
    validate_file,
    write_manifest,
)


def item(content: bytes, backup_path: str = "objects/raw/file") -> ManifestObject:
    return ManifestObject(
        id="00000000-0000-7000-8000-000000000001",
        storage_domain="raw",
        bucket_name="raw",
        object_key="raw/digest",
        content_sha256=hashlib.sha256(content).hexdigest(),
        byte_length=len(content),
        media_type="application/octet-stream",
        backup_path=backup_path,
    )


def test_manifest_round_trip_and_file_validation(tmp_path: Path) -> None:
    content = b"backup content"
    expected = item(content)
    path = safe_backup_path(expected, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    validate_file(expected, path)
    write_manifest(tmp_path, [expected])

    assert read_manifest(tmp_path) == [expected]


def test_manifest_checksum_fails_closed(tmp_path: Path) -> None:
    write_manifest(tmp_path, [item(b"content")])
    (tmp_path / "objects-manifest.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="manifest SHA-256 mismatch"):
        read_manifest(tmp_path)


def test_object_checksum_fails_closed(tmp_path: Path) -> None:
    expected = item(b"expected")
    path = safe_backup_path(expected, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"tampered")

    with pytest.raises(RuntimeError, match="object SHA-256 mismatch"):
        validate_file(expected, path)


def test_backup_path_cannot_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inside the backup directory"):
        safe_backup_path(item(b"x", "../outside"), tmp_path)
