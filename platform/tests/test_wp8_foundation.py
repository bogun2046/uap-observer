from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from tools.validate_wp3 import evaluate as evaluate_wp3
from tools.validate_wp4 import evaluate as evaluate_wp4
from tools.validate_wp8 import evaluate as evaluate_wp8


def claim_fingerprint(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    collapsed = re.sub(r"\s+", " ", normalized)
    trimmed = collapsed.strip(" ")
    return hashlib.sha256(trimmed.encode("utf-8")).hexdigest()


def platform_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_wp3_and_wp4_accept_later_head() -> None:
    wp3 = evaluate_wp3(platform_root())
    wp4 = evaluate_wp4(platform_root())
    assert all(check.passed for check in wp3)
    assert all(check.passed for check in wp4)


def test_wp8_1_static_contract() -> None:
    checks = evaluate_wp8(platform_root())
    failed = [check.name for check in checks if not check.passed]
    assert failed == []


def test_claim_fingerprint_algorithm() -> None:
    digest = claim_fingerprint("  Hello   World  ")
    assert digest == hashlib.sha256(b"Hello World").hexdigest()
    assert claim_fingerprint("A") != claim_fingerprint("a")


def test_wp8_1_does_not_implement_later_stages() -> None:
    source = (platform_root() / "alembic/versions/0010_knowledge_foundation.py").read_text(
        encoding="utf-8"
    )
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in source
    assert "COALESCE(NEW.claim_id, OLD.claim_id)" not in source
    assert "COALESCE(NEW.entity_candidate_id, OLD.entity_candidate_id)" not in source
    assert "NEW.claim_id IS DISTINCT FROM OLD.claim_id" in source
    assert "NEW.entity_candidate_id IS DISTINCT FROM OLD.entity_candidate_id" in source
    assert "materialize_claim_bundle" not in source
    assert "materialize_entity_bundle" not in source
    assert "merge_entities" not in source
    knowledge = platform_root() / "src/uap_platform/knowledge"
    assert knowledge.is_dir()
    package = "\n".join(path.read_text(encoding="utf-8") for path in knowledge.glob("*.py"))
    assert "materialize_claim_bundle" not in package
    assert "def resolve_claims" not in package
    assert "ORDER BY" not in package
