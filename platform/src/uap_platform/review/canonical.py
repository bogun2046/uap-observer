"""Replica of audit._canonical_json / _payload_sha256 for tests and probes.

The database function is authoritative. This replica exists only so static tests
and runtime probes can assert the frozen compact SHA-256 identity.

Number rule (value-based, unique):
- integer-valued numbers emit as integers: 1e2, 100, 100.0, 1.0 -> 100 / 1
- non-integers emit as plain decimals, no exponent, no trailing zeros:
  1e-7 -> 0.0000001, 1.50 -> 1.5
- signed zero is 0
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Final

FROZEN_COMPACT_JSON: Final = '{"a":1,"b":2}'
FROZEN_COMPACT_SHA256: Final = (
    "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
)
FROZEN_NESTED_JSON: Final = '{"m":{"d":"x y"},"z":[{"a":1,"b":2},true,null]}'
FROZEN_NESTED_SHA256: Final = (
    "9c93882fee4567ea2c70a7889eeb3e2c77f0def91327e0c2d4cb0eed458272c1"
)
FROZEN_NUMBER_SOURCE: Final = '{"n":1.0,"e":1e2,"small":1e-7}'
FROZEN_NUMBER_JSON: Final = '{"e":100,"n":1,"small":0.0000001}'
FROZEN_NUMBER_SHA256: Final = (
    "2c39cedbb91a51d5591b068931c00b4204cf539bed72ca2508566841726a5022"
)
FROZEN_NUMBER_ARRAY_SOURCE: Final = '{"z":[1e2,-1.50,0.0,1e-7]}'
FROZEN_NUMBER_ARRAY_JSON: Final = '{"z":[100,-1.5,0,0.0000001]}'
FROZEN_NUMBER_ARRAY_SHA256: Final = (
    "083128b312e13d5eb6940862ae77d7ebb86e0dfdacedd0b35f1cd300179e6c3a"
)


def _canonical_number(value: Decimal) -> str:
    if value == 0:
        return "0"
    if value == value.to_integral_value():
        return str(int(value))
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _as_decimal(value: int | float | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    return Decimal(str(value))


def canonical_json(value: Any) -> str:
    """UTF-8 compact JSON: sorted object keys, value-based numbers, no extra whitespace."""

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float, Decimal)):
        return _canonical_number(_as_decimal(value))
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = [
            json.dumps(str(key), ensure_ascii=False) + ":" + canonical_json(value[key])
            for key in sorted(value, key=str)
        ]
        return "{" + ",".join(parts) + "}"
    raise TypeError(f"unsupported canonical json type: {type(value)!r}")


def loads_canonical(text: str) -> Any:
    """Parse JSON text with exact decimals so 1e2 and 1e-7 keep their values."""

    return json.loads(text, parse_float=Decimal, parse_int=int)


def payload_sha256(value: Any) -> str:
    """SHA-256 hex of canonical_json(value) encoded UTF-8."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def payload_sha256_text(text: str) -> str:
    return payload_sha256(loads_canonical(text))
