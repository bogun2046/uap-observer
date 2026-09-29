from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping

import pytest

from uap_platform.admin_api.cursor import CursorCodec, CursorError, filters_digest

KEY = b"k" * 32
FILTERS = {"status": "open", "limit": 10}
FILTERS_SHA = filters_digest(FILTERS)


def _codec(*, max_length: int = 2048) -> CursorCodec:
    return CursorCodec(KEY, max_length=max_length)


def _signed_token(payload: Mapping[str, object], *, raw: bytes | None = None) -> str:
    data = raw if raw is not None else json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    encoded_payload = base64.urlsafe_b64encode(data).rstrip(b"=").decode()
    signature = hmac.digest(KEY, data, "sha256")
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{encoded_payload}.{encoded_signature}"


def _payload(**overrides: object) -> dict[str, object]:
    return {
        "filters_sha256": FILTERS_SHA,
        "last": ["2026-09-29T00:00:00Z", "item-1"],
        "resource": "review-cases",
        "sort": "opened_at_desc",
        "v": 1,
        **overrides,
    }


def _decode(token: str) -> list[object]:
    return _codec().decode(
        token,
        resource="review-cases",
        sort="opened_at_desc",
        filters_sha256=FILTERS_SHA,
    )


def test_cursor_round_trip_and_filter_digest_are_canonical() -> None:
    assert filters_digest({"status": "open", "limit": 10}) == FILTERS_SHA
    assert filters_digest({"limit": 10, "status": "open"}) == FILTERS_SHA
    token = _codec().encode(
        resource="review-cases",
        sort="opened_at_desc",
        last=["2026-09-29T00:00:00Z", "item-1"],
        filters_sha256=FILTERS_SHA,
    )
    assert _decode(token) == ["2026-09-29T00:00:00Z", "item-1"]


@pytest.mark.parametrize(
    ("key", "max_length"),
    [(b"short", 2048), (KEY, 255), (KEY, 8193)],
)
def test_codec_rejects_invalid_configuration(key: bytes, max_length: int) -> None:
    with pytest.raises(ValueError, match="configuration"):
        CursorCodec(key, max_length)


def test_encode_rejects_token_over_configured_limit() -> None:
    with pytest.raises(CursorError, match="invalid"):
        _codec(max_length=256).encode(
            resource="r" * 300,
            sort="opened_at_desc",
            last=["last"],
            filters_sha256=FILTERS_SHA,
        )


@pytest.mark.parametrize(
    "token",
    ["", "x" * 2049, "a.b.c", "!." + "A" * 43, "A." + "A" * 43],
)
def test_decode_rejects_malformed_tokens(token: str) -> None:
    with pytest.raises(CursorError, match="invalid"):
        _decode(token)


def test_decode_rejects_bad_signature_and_noncanonical_base64() -> None:
    codec = _codec()
    token = codec.encode(
        resource="review-cases",
        sort="opened_at_desc",
        last=["item-1"],
        filters_sha256=FILTERS_SHA,
    )
    payload, signature = token.split(".")
    changed_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(CursorError, match="invalid"):
        _decode(f"{payload}.{changed_signature}")
    with pytest.raises(CursorError, match="invalid"):
        _decode(f"{payload}=.{signature}")
    raw = b"{}"
    noncanonical_payload = "e31"
    signature = base64.urlsafe_b64encode(hmac.digest(KEY, raw, "sha256")).rstrip(b"=").decode()
    with pytest.raises(CursorError, match="invalid"):
        _decode(f"{noncanonical_payload}.{signature}")


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b"\xff",
        b"[]",
        b'{"v":1, "resource":"review-cases"}',
        (
            b'{"filters_sha256":"'
            + FILTERS_SHA.encode()
            + b'","last":[NaN],"resource":"review-cases",'
            b'"sort":"opened_at_desc","v":1}'
        ),
    ],
)
def test_decode_rejects_invalid_or_noncanonical_json(raw: bytes) -> None:
    with pytest.raises(CursorError, match="invalid"):
        _decode(_signed_token({}, raw=raw))


@pytest.mark.parametrize(
    "payload",
    [
        _payload(extra=True),
        _payload(v=True),
        _payload(v=2),
        _payload(resource=1),
        _payload(sort=1),
        _payload(filters_sha256=1),
        _payload(last="not-a-list"),
        _payload(resource="entities"),
        _payload(sort="id"),
        _payload(filters_sha256="0" * 64),
        _payload(filters_sha256="G" * 64),
    ],
)
def test_decode_rejects_signed_payload_contract_violations(payload: dict[str, object]) -> None:
    with pytest.raises(CursorError, match="invalid"):
        _decode(_signed_token(payload))


def test_decode_rejects_signature_with_valid_alphabet_but_invalid_base64_length() -> None:
    with pytest.raises(CursorError, match="invalid"):
        _decode("eyJ2IjoxfQ.A")


def test_decode_rejects_digest_with_invalid_length_after_matching_expected_context() -> None:
    token = _signed_token(_payload(filters_sha256="bad"))
    with pytest.raises(CursorError, match="invalid"):
        _codec().decode(
            token,
            resource="review-cases",
            sort="opened_at_desc",
            filters_sha256="bad",
        )


def test_cursor_digest_uses_sha256() -> None:
    assert len(filters_digest({"q": "值"})) == hashlib.sha256(b"{}").digest_size * 2
