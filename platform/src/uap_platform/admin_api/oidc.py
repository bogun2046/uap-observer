"""Strict bearer OIDC validation with an RS256 JWKS allowlist."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ALLOWED_ALGS = frozenset({"RS256"})
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


class TokenError(ValueError):
    """Raised for every missing, malformed, or cryptographically invalid token."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _b64url_decode(value: str) -> bytes:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if not value or any(character not in alphabet for character in value):
        raise TokenError("api_token_invalid")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as error:
        raise TokenError("api_token_invalid") from error


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TokenError("api_token_invalid") from error
    if not isinstance(payload, dict):
        raise TokenError("api_token_invalid")
    return payload


def _int_from_b64(value: object) -> int:
    if not isinstance(value, str) or not value:
        raise TokenError("api_token_invalid")
    decoded = _b64url_decode(value)
    if not decoded:
        raise TokenError("api_token_invalid")
    return int.from_bytes(decoded, "big")


def rsa_pkcs1_v15_sha256_verify(n: int, e: int, message: bytes, signature: bytes) -> bool:
    try:
        size = (n.bit_length() + 7) // 8
        if size < 11 or len(signature) != size:
            return False
        integer = int.from_bytes(signature, "big")
        if integer >= n:
            return False
        encoded = pow(integer, e, n).to_bytes(size, "big")
        digest = hashlib.sha256(message).digest()
        pad_length = size - len(_SHA256_DIGEST_INFO) - len(digest) - 3
        if pad_length < 8:
            return False
        expected = b"\x00\x01" + (b"\xff" * pad_length) + b"\x00" + _SHA256_DIGEST_INFO + digest
        return hmac.compare_digest(encoded, expected)
    except (OverflowError, TypeError, ValueError):
        return False


@dataclass(frozen=True)
class RsaKey:
    kid: str | None
    n: int
    e: int


@dataclass(frozen=True)
class OidcValidator:
    issuer: str
    audience: str
    keys: tuple[RsaKey, ...]

    @classmethod
    def from_settings(cls, issuer: str, audience: str, jwks: Mapping[str, object]) -> OidcValidator:
        raw_keys = jwks.get("keys")
        if not isinstance(raw_keys, list) or not raw_keys:
            raise TokenError("api_dependency_unavailable")
        parsed: list[RsaKey] = []
        for item in raw_keys:
            if not isinstance(item, dict):
                raise TokenError("api_dependency_unavailable")
            kty = item.get("kty")
            alg = item.get("alg")
            use = item.get("use")
            if kty != "RSA":
                continue
            if alg is not None and alg not in ALLOWED_ALGS:
                continue
            if use is not None and use != "sig":
                continue
            kid = item.get("kid")
            if kid is not None and not isinstance(kid, str):
                raise TokenError("api_dependency_unavailable")
            try:
                parsed.append(
                    RsaKey(kid=kid, n=_int_from_b64(item.get("n")), e=_int_from_b64(item.get("e")))
                )
            except TokenError as error:
                raise TokenError("api_dependency_unavailable") from error
        if not parsed:
            raise TokenError("api_dependency_unavailable")
        if not issuer or not audience:
            raise TokenError("api_dependency_unavailable")
        return cls(issuer=issuer, audience=audience, keys=tuple(parsed))

    def validate(self, token: str, *, now: float | None = None) -> str:
        try:
            if not token or token.count(".") != 2:
                raise TokenError("api_token_invalid")
            encoded_header, encoded_payload, encoded_signature = token.split(".")
            header = _json_object(_b64url_decode(encoded_header))
            payload = _json_object(_b64url_decode(encoded_payload))
            signature = _b64url_decode(encoded_signature)
            if _b64url_encode(_b64url_decode(encoded_header)) != encoded_header:
                raise TokenError("api_token_invalid")
            if _b64url_encode(_b64url_decode(encoded_payload)) != encoded_payload:
                raise TokenError("api_token_invalid")
            alg = header.get("alg")
            if alg not in ALLOWED_ALGS:
                raise TokenError("api_token_invalid")
            if header.get("typ") not in {None, "JWT"}:
                raise TokenError("api_token_invalid")
            kid = header.get("kid")
            key = self._select_key(kid if isinstance(kid, str) else None)
            signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
            if not rsa_pkcs1_v15_sha256_verify(key.n, key.e, signing_input, signature):
                raise TokenError("api_token_invalid")
            self._validate_claims(payload, now=time.time() if now is None else now)
            subject = payload.get("sub")
            if not isinstance(subject, str) or not subject:
                raise TokenError("api_token_invalid")
            return subject
        except TokenError:
            raise
        except Exception as error:
            raise TokenError("api_token_invalid") from error

    def _select_key(self, kid: str | None) -> RsaKey:
        if kid is None:
            if len(self.keys) != 1:
                raise TokenError("api_token_invalid")
            return self.keys[0]
        matches = [key for key in self.keys if key.kid == kid]
        if len(matches) != 1:
            raise TokenError("api_token_invalid")
        return matches[0]

    def _validate_claims(self, payload: Mapping[str, Any], *, now: float) -> None:
        issuer = payload.get("iss")
        if issuer != self.issuer:
            raise TokenError("api_token_invalid")
        audience = payload.get("aud")
        if isinstance(audience, str):
            audiences = [audience]
        elif isinstance(audience, list) and all(isinstance(item, str) for item in audience):
            audiences = audience
        else:
            raise TokenError("api_token_invalid")
        if self.audience not in audiences:
            raise TokenError("api_token_invalid")
        expires = payload.get("exp")
        if type(expires) is not int or expires <= now:
            raise TokenError("api_token_invalid")
        not_before = payload.get("nbf")
        if not_before is not None and (type(not_before) is not int or now < not_before):
            raise TokenError("api_token_invalid")
