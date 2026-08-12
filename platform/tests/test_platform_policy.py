from __future__ import annotations

from pathlib import Path

from tools.validate_platform import evaluate


def test_frozen_wp2_policy() -> None:
    root = Path(__file__).resolve().parents[1]
    failures = [item for item in evaluate(root) if not item.passed]

    assert failures == []
