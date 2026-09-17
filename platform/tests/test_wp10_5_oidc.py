"""G10-21 OIDC and principal binding unit tests with real RSA crypto."""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import secrets
import time
from typing import Protocol, cast

import pytest

from uap_platform.admin_api.oidc import (
    ALLOWED_ALGS,
    OidcValidator,
    TokenError,
    rsa_pkcs1_v15_sha256_verify,
)


def _probable_prime(value: int) -> bool:
    if value < 2:
        return False
    for small in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31):
        if value == small:
            return True
        if value % small == 0:
            return False
    exponent = value - 1
    zeros = 0
    while exponent % 2 == 0:
        exponent //= 2
        zeros += 1
    for _ in range(8):
        base = secrets.randbelow(value - 3) + 2
        witness = pow(base, exponent, value)
        if witness in (1, value - 1):
            continue
        for _ in range(zeros - 1):
            witness = pow(witness, 2, value)
            if witness == value - 1:
                break
        else:
            return False
    return True


def _prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _probable_prime(candidate):
            return candidate


class _RSAPublicNumbers(Protocol):
    n: int
    e: int


class _RSAPrivateNumbers(Protocol):
    d: int
    public_numbers: _RSAPublicNumbers


class _RSAPrivateKey(Protocol):
    def private_numbers(self) -> _RSAPrivateNumbers: ...


class _RSAModule(Protocol):
    def generate_private_key(self, *, public_exponent: int, key_size: int) -> _RSAPrivateKey: ...


def _runtime_rsa() -> tuple[int, int, int]:
    try:
        rsa = cast(
            _RSAModule,
            importlib.import_module("cryptography.hazmat.primitives.asymmetric.rsa"),
        )
    except ImportError:
        public_exponent = 65537
        while True:
            first = _prime(1024)
            second = _prime(1024)
            if first == second:
                continue
            totient = (first - 1) * (second - 1)
            if totient % public_exponent == 0:
                continue
            modulus = first * second
            private_exponent = pow(public_exponent, -1, totient)
            return modulus, public_exponent, private_exponent
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.private_numbers()
    return numbers.public_numbers.n, numbers.public_numbers.e, numbers.d


N, E, D = _runtime_rsa()

ISSUER = "https://issuer.test/wp10.5"
AUDIENCE = "uap-admin"
KID = "wp10-5-test-key"
DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _jwk() -> dict[str, object]:
    def encode_int(value: int) -> str:
        size = (value.bit_length() + 7) // 8
        return _b64(value.to_bytes(size, "big"))

    return {
        "kty": "RSA",
        "kid": KID,
        "alg": "RS256",
        "use": "sig",
        "n": encode_int(N),
        "e": encode_int(E),
    }


def sign(message: bytes) -> bytes:
    size = (N.bit_length() + 7) // 8
    digest = hashlib.sha256(message).digest()
    pad_length = size - len(DIGEST_INFO) - len(digest) - 3
    encoded = b"\x00\x01" + (b"\xff" * pad_length) + b"\x00" + DIGEST_INFO + digest
    signature = pow(int.from_bytes(encoded, "big"), D, N)
    return signature.to_bytes(size, "big")


def mint(
    *,
    sub: str = "reviewer-subject",
    iss: str = ISSUER,
    aud: object = AUDIENCE,
    exp: int | None = None,
    nbf: int | None = None,
    alg: str = "RS256",
    kid: str | None = KID,
    extra: dict[str, object] | None = None,
    key_n: int = N,
    key_d: int = D,
) -> str:
    header = {"alg": alg, "typ": "JWT"}
    if kid is not None:
        header["kid"] = kid
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "exp": now + 3600 if exp is None else exp,
    }
    if nbf is not None:
        payload["nbf"] = nbf
    if extra:
        payload.update(extra)
    encoded_header = _b64(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
    encoded_payload = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    size = (key_n.bit_length() + 7) // 8
    digest = hashlib.sha256(signing_input).digest()
    pad_length = size - len(DIGEST_INFO) - len(digest) - 3
    encoded = b"\x00\x01" + (b"\xff" * pad_length) + b"\x00" + DIGEST_INFO + digest
    signature = pow(int.from_bytes(encoded, "big"), key_d, key_n).to_bytes(size, "big")
    return f"{encoded_header}.{encoded_payload}.{_b64(signature)}"


def validator() -> OidcValidator:
    return OidcValidator.from_settings(ISSUER, AUDIENCE, {"keys": [_jwk()]})


def test_rs256_roundtrip_and_allowlist() -> None:
    assert ALLOWED_ALGS == frozenset({"RS256"})
    token = mint()
    assert validator().validate(token) == "reviewer-subject"
    message = b"signing-input"
    signature = sign(message)
    assert rsa_pkcs1_v15_sha256_verify(N, E, message, signature)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"alg": "none"},
        {"alg": "HS256"},
        {"iss": "https://evil.test"},
        {"aud": "other-audience"},
        {"exp": 1},
        {"nbf": int(time.time()) + 3600},
        {"sub": ""},
    ],
)
def test_invalid_claims_and_alg_confusion(kwargs: dict[str, object]) -> None:
    token = mint(**kwargs)  # type: ignore[arg-type]
    if kwargs.get("sub") == "":
        # empty sub is added after defaults
        pass
    with pytest.raises(TokenError) as raised:
        validator().validate(token)
    assert raised.value.code == "api_token_invalid"


def test_missing_sub_is_invalid() -> None:
    token = mint()
    header, payload, _signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims.pop("sub")
    encoded_payload = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{header}.{encoded_payload}".encode("ascii")
    size = (N.bit_length() + 7) // 8
    digest = hashlib.sha256(signing_input).digest()
    pad_length = size - len(DIGEST_INFO) - len(digest) - 3
    encoded = b"\x00\x01" + (b"\xff" * pad_length) + b"\x00" + DIGEST_INFO + digest
    sig = pow(int.from_bytes(encoded, "big"), D, N).to_bytes(size, "big")
    bad = f"{header}.{encoded_payload}.{_b64(sig)}"
    with pytest.raises(TokenError) as raised:
        validator().validate(bad)
    assert raised.value.code == "api_token_invalid"


def test_bad_signature_is_invalid() -> None:
    token = mint()
    header, payload, signature = token.split(".")
    raw = bytearray(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
    raw[0] ^= 0xFF
    mutated = _b64(bytes(raw))
    with pytest.raises(TokenError) as raised:
        validator().validate(f"{header}.{payload}.{mutated}")
    assert raised.value.code == "api_token_invalid"


def test_jwks_without_rsa_keys_fails_closed() -> None:
    with pytest.raises(TokenError) as raised:
        OidcValidator.from_settings(ISSUER, AUDIENCE, {"keys": [{"kty": "oct", "k": "abc"}]})
    assert raised.value.code == "api_dependency_unavailable"


def test_role_claim_is_ignored_by_validator() -> None:
    token = mint(extra={"role": "platform_admin", "scope": "admin"})
    assert validator().validate(token) == "reviewer-subject"
