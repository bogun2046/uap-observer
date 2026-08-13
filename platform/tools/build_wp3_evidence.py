"""Build deterministic WP3 evidence and a separated-anchor checksum manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

EVIDENCE_REQUIRED = (
    "docs/wp3/README.md",
    "docs/wp3/acceptance-cases.md",
    "docs/wp3/acceptance-ticket.md",
    "docs/wp3/development-self-review.md",
    "docs/wp3/g3-rejection-record.md",
    "docs/wp3/g3-r2-remediation-report.md",
    "docs/wp3/implementation-ticket.md",
    ".github/workflows/platform-ci.yml",
    "platform/.env.example",
    "platform/Dockerfile",
    "platform/Makefile",
    "platform/compose.yaml",
    "platform/pyproject.toml",
    "platform/scripts/bootstrap-env.sh",
    "platform/scripts/backup-platform.sh",
    "platform/scripts/restore-platform.sh",
    "platform/scripts/migrate-platform.sh",
    "platform/scripts/deploy-staging.sh",
    "platform/scripts/verify-migrator-failure-close.sh",
    "platform/scripts/verify-migration-chain.sh",
    "platform/alembic/versions/0001_roles_and_schemas.py",
    "platform/alembic/versions/0002_authoritative_schema.py",
    "platform/alembic/versions/0003_permissions_and_guards.py",
    "platform/alembic/versions/0004_g3_semantic_repairs.py",
    "platform/alembic/env.py",
    "platform/src/uap_platform/config.py",
    "platform/src/uap_platform/object_registry.py",
    "platform/src/uap_platform/readiness.py",
    "platform/tests/test_configure_roles.py",
    "platform/tests/test_config.py",
    "platform/tests/test_object_backup.py",
    "platform/tests/test_object_registry.py",
    "platform/tests/test_object_store_init.py",
    "platform/tools/build_wp3_evidence.py",
    "platform/tools/configure_roles.py",
    "platform/tools/object_backup.py",
    "platform/tools/validate_platform.py",
    "platform/tools/validate_wp3.py",
    "platform/tools/wp3_runtime_probe.py",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    platform = Path(__file__).resolve().parents[1]
    repository = platform.parent
    output = args.output if args.output.is_absolute() else repository / args.output
    output.mkdir(parents=True, exist_ok=True)
    files = sorted({repository / path for path in EVIDENCE_REQUIRED})
    missing = [str(path.relative_to(repository)) for path in files if not path.is_file()]
    if missing:
        raise SystemExit(f"missing WP3 evidence files: {', '.join(missing)}")
    rows = [
        {
            "path": path.relative_to(repository).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in files
    ]
    report = output / "delivery-report.json"
    report.write_text(
        json.dumps(
            {"freeze": "G3-FROZEN-20260812-01", "files": rows},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = output / "MANIFEST.sha256"
    manifest.write_text(
        "\n".join(f"{row['sha256']}  {row['path']}" for row in rows) + "\n",
        encoding="utf-8",
    )
    anchor = output.parent / f"{output.name}.MANIFEST.sha256"
    anchor.write_text(f"{sha256_file(manifest)}  {output.name}/MANIFEST.sha256\n", encoding="utf-8")
    print(
        json.dumps(
            {"files": len(rows), "manifest_sha256": sha256_file(manifest)}, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
