"""Replica of audit._canonical_json / _payload_sha256 for tests and probes.

The database function is authoritative. This replica exists only so static tests
and runtime probes can assert the frozen compact SHA-256 identity.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

FROZEN_COMPACT_JSON: Final = '{"a":1,"b":2}'
FROZEN_COMPACT_SHA256: Final = (
    "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
)
FROZEN_NESTED_JSON: Final = '{"m":{"d":"x y"},"z":[{"a":1,"b":2},true,null]}'
FROZEN_NESTED_SHA256: Final = (
    "9c93882fee4567ea2c70a7889eeb3e2c77f0def91327e0c2d4cb0eed458272c1"
)


def canonical_json(value: Any) -> str:
    """UTF-8 compact JSON: sorted object keys, no extra whitespace."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def payload_sha256(value: Any) -> str:
    """SHA-256 hex of canonical_json(value) encoded UTF-8."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
