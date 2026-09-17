"""Canonical HMAC-authenticated keyset cursors for the Admin API.

Copied from the Public API module so the two processes do not share a runtime object."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class CursorError(ValueError):
    """Raised for every unsafe, malformed, or context-mismatched cursor."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64_decode(value: str) -> bytes:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if not value or any(character not in alphabet for character in value):
        raise CursorError("cursor is invalid")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as error:
        raise CursorError("cursor is invalid") from error


def filters_digest(filters: Mapping[str, object]) -> str:
    """Bind a cursor to normalized filters without exposing those filters."""

    return hashlib.sha256(_canonical_json(filters)).hexdigest()


@dataclass(frozen=True)
class CursorCodec:
    key: bytes
    max_length: int = 2048

    def __post_init__(self) -> None:
        if len(self.key) < 32 or not 256 <= self.max_length <= 8192:
            raise ValueError("cursor codec configuration is invalid")

    def encode(
        self,
        *,
        resource: str,
        sort: str,
        last: Sequence[object],
        filters_sha256: str,
    ) -> str:
        payload = {
            "filters_sha256": filters_sha256,
            "last": list(last),
            "resource": resource,
            "sort": sort,
            "v": 1,
        }
        raw = _canonical_json(payload)
        token = f"{_b64_encode(raw)}.{_b64_encode(hmac.digest(self.key, raw, 'sha256'))}"
        if len(token) > self.max_length:
            raise CursorError("cursor is invalid")
        return token

    def decode(
        self,
        token: str,
        *,
        resource: str,
        sort: str,
        filters_sha256: str,
    ) -> list[Any]:
        if not token or len(token) > self.max_length or token.count(".") != 1:
            raise CursorError("cursor is invalid")
        encoded_payload, encoded_signature = token.split(".", 1)
        raw = _b64_decode(encoded_payload)
        signature = _b64_decode(encoded_signature)
        if _b64_encode(raw) != encoded_payload or _b64_encode(signature) != encoded_signature:
            raise CursorError("cursor is invalid")
        if not hmac.compare_digest(signature, hmac.digest(self.key, raw, "sha256")):
            raise CursorError("cursor is invalid")
        try:
            payload = json.loads(
                raw,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    CursorError("cursor is invalid")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CursorError("cursor is invalid") from error
        if not isinstance(payload, dict) or _canonical_json(payload) != raw:
            raise CursorError("cursor is invalid")
        if set(payload) != {"v", "resource", "sort", "last", "filters_sha256"}:
            raise CursorError("cursor is invalid")
        if (
            type(payload["v"]) is not int
            or payload["v"] != 1
            or not isinstance(payload["resource"], str)
            or not isinstance(payload["sort"], str)
            or not isinstance(payload["filters_sha256"], str)
            or payload["resource"] != resource
            or payload["sort"] != sort
            or payload["filters_sha256"] != filters_sha256
            or not isinstance(payload["last"], list)
        ):
            raise CursorError("cursor is invalid")
        digest = payload["filters_sha256"]
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise CursorError("cursor is invalid")
        return payload["last"]
