"""Validate the frozen WP4 durable-jobs and transactional-Outbox contract."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    actual: object
    expected: object


def result(name: str, passed: bool, actual: object, expected: object) -> Check:
    return Check(name, passed, actual, expected)


def evaluate(platform: Path) -> list[Check]:
    platform = platform.resolve()
    repository = platform.parent
    config = Config(str(platform / "alembic.ini"))
    config.set_main_option("script_location", str(platform / "alembic"))
    script = ScriptDirectory.from_config(config)
    revisions = list(script.walk_revisions(base="base", head="heads"))
    source = (platform / "alembic/versions/0005_durable_jobs.py").read_text(encoding="utf-8")
    required_docs = (
        "docs/wp4/README.md",
        "docs/wp4/acceptance-cases.md",
        "docs/wp4/acceptance-ticket.md",
        "docs/wp4/implementation-ticket.md",
        "docs/wp4/development-self-review.md",
        "platform/tools/build_wp4_evidence.py",
        "platform/tools/wp4_runtime_probe.py",
    )
    missing = [path for path in required_docs if not (repository / path).is_file()]
    tokens = (
        "CREATE FUNCTION ops.enqueue_job",
        "CREATE FUNCTION ops.claim_job",
        "CREATE FUNCTION ops.finish_job",
        "CREATE FUNCTION ops.requeue_dead_letter",
        "CREATE FUNCTION ops.emit_outbox",
        "CREATE FUNCTION ops.claim_outbox",
        "CREATE FUNCTION ops.ack_outbox",
        "CREATE FUNCTION ops.publish_outbox_failure",
        "ops.classify_failure",
        "FOR UPDATE SKIP LOCKED",
        "lease_token",
        "lease_expires_at",
        "retryable_failure",
        "dead_letters",
        "uap_publisher",
        "publish_document",
        "42501",
    )
    return [
        result("required_files", not missing, missing, []),
        result(
            "linear_revision_chain",
            [revision.revision for revision in revisions][:3]
            == ["0006_collectors", "0005_durable_jobs", "0004_g3_semantic_repairs"],
            [revision.revision for revision in revisions],
            "0006 -> 0005 -> 0004 -> 0003 -> 0002 -> 0001",
        ),
        result("migration_revision_id", 'revision = "0005_durable_jobs"' in source, True, True),
        result("durable_job_semantics", all(token in source for token in tokens), True, True),
        result(
            "worker_publisher_boundary",
            "ordinary worker cannot enqueue a publisher job" in source
            and "publisher cannot enqueue an ordinary worker job" in source,
            True,
            True,
        ),
        result(
            "no_process_local_queue",
            not re.search(r"queue\.(Queue|SimpleQueue)|asyncio\.Queue", source),
            "process-local queue absent",
            "process-local queue absent",
        ),
    ]


def main() -> None:
    checks = evaluate(Path(__file__).resolve().parents[1])
    print(json.dumps([asdict(check) for check in checks], indent=2, sort_keys=True))
    failed = [check.name for check in checks if not check.passed]
    if failed:
        raise SystemExit(f"WP4 contract checks failed: {', '.join(failed)}")
    print(f"WP4 contract checks passed: {len(checks)}/{len(checks)}")


if __name__ == "__main__":
    main()
