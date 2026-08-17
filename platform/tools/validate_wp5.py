"""Validate the frozen WP5 collector contract without external services."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    actual: object
    expected: object


def check(name: str, passed: bool, actual: object, expected: object = True) -> Check:
    return Check(name, passed, actual, expected)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(platform: Path) -> list[Check]:
    platform = platform.resolve()
    repository = platform.parent
    required = (
        "docs/wp5/README.md",
        "docs/wp5/acceptance-cases.md",
        "docs/wp5/acceptance-ticket.md",
        "docs/wp5/development-self-review.md",
        "docs/wp5/implementation-ticket.md",
        "platform/alembic/versions/0006_collectors.py",
        "platform/scripts/verify-migration-chain.sh",
        "platform/tools/wp5_runtime_probe.py",
        "platform/src/uap_platform/collectors/contracts.py",
        "platform/src/uap_platform/collectors/persistence.py",
        "platform/src/uap_platform/collectors/policy.py",
        "platform/src/uap_platform/collectors/rss.py",
        "platform/src/uap_platform/collectors/transport.py",
        "platform/src/uap_platform/collectors/workflow.py",
        "platform/src/uap_platform/object_registry.py",
        "platform/tools/reconcile_object_storage.py",
        "platform/tests/fixtures/rss/g5-fixed-feed.xml",
    )
    missing = [path for path in required if not (repository / path).is_file()]
    source = "\n".join(
        (repository / path).read_text(encoding="utf-8")
        for path in required
        if (repository / path).is_file()
    )
    acceptance = (repository / "docs/wp5/acceptance-cases.md").read_text(encoding="utf-8")
    fixture = repository / "platform/tests/fixtures/rss/g5-fixed-feed.xml"
    expected_fixture_hash = "b3a998a48fecd9c18bfb75d294a60465aad12a55490b1c72e6629ebcf9dd73c8"
    return [
        check("required_files", not missing, missing, []),
        check("frozen_cases", all(f"G5-0{i}" in acceptance for i in range(1, 9)), True),
        check(
            "versioned_payload",
            all(
                token in source
                for token in ("RSS_PAYLOAD_SCHEMA_VERSION", "payload_schema_version")
            ),
            True,
        ),
        check(
            "snapshot_hash",
            fixture.is_file() and sha256_file(fixture) == expected_fixture_hash,
            expected_fixture_hash,
        ),
        check(
            "source_pacing_health",
            all(
                token in source
                for token in ("SourceRateLimiter", "SourceHealthTracker", "cooldown_until")
            ),
            True,
        ),
        check(
            "source_config_and_job_lifecycle",
            all(
                token in source
                for token in (
                    "fk_source_run_config_same_source",
                    "source_config_version_id, source_id",
                    "ALTER COLUMN source_config_version_id SET NOT NULL",
                    "finish_job",
                    "attempt_id",
                    "lease_token",
                )
            ),
            True,
        ),
        check(
            "object_consistency_scan",
            "reconcile_unregistered_objects" in source and "hashtextextended" in source,
            True,
        ),
    ]


def main() -> None:
    checks = evaluate(Path(__file__).resolve().parents[1])
    print(json.dumps([asdict(item) for item in checks], indent=2, sort_keys=True))
    failed = [item.name for item in checks if not item.passed]
    if failed:
        raise SystemExit(f"WP5 contract checks failed: {', '.join(failed)}")
    print(f"WP5 contract checks passed: {len(checks)}/{len(checks)}")


if __name__ == "__main__":
    main()
