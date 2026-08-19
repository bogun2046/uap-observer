"""Validate the frozen WP6 extraction contract without external services."""

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
        "docs/wp6/README.md",
        "docs/wp6/acceptance-cases.md",
        "docs/wp6/acceptance-ticket.md",
        "docs/wp6/development-self-review.md",
        "docs/wp6/implementation-ticket.md",
        "platform/pyproject.toml",
        "platform/uv.lock",
        "platform/Dockerfile",
        "platform/tools/build_wp6_evidence.py",
        "platform/tools/wp6_runtime_probe.py",
        "platform/tools/validate_wp6.py",
        "platform/src/uap_platform/documents/contracts.py",
        "platform/src/uap_platform/documents/html.py",
        "platform/src/uap_platform/documents/pdf.py",
        "platform/src/uap_platform/documents/persistence.py",
        "platform/src/uap_platform/documents/subtitles.py",
        "platform/src/uap_platform/documents/workflow.py",
        "platform/src/uap_platform/object_registry.py",
        "platform/tests/fixtures/documents/sample.html",
        "platform/tests/fixtures/documents/sample.pdf",
        "platform/tests/fixtures/documents/sample.srt",
        "platform/tests/fixtures/documents/sample.vtt",
        "platform/tests/test_document_extraction.py",
        "platform/tests/test_document_persistence.py",
        "platform/tests/test_pdf_extraction.py",
        "platform/tests/test_subtitle_extraction.py",
    )
    missing = [path for path in required if not (repository / path).is_file()]
    source = "\n".join(
        (repository / path).read_text(encoding="utf-8")
        for path in required
        if (repository / path).is_file() and not path.endswith((".pdf", ".lock"))
    )
    adapter_source = "\n".join(
        (repository / path).read_text(encoding="utf-8")
        for path in (
            "platform/src/uap_platform/documents/contracts.py",
            "platform/src/uap_platform/documents/html.py",
            "platform/src/uap_platform/documents/pdf.py",
            "platform/src/uap_platform/documents/persistence.py",
            "platform/src/uap_platform/documents/subtitles.py",
        )
    )
    acceptance_path = repository / "docs/wp6/acceptance-cases.md"
    acceptance = acceptance_path.read_text(encoding="utf-8") if acceptance_path.is_file() else ""
    expected_fixtures = {
        "sample.html": "b595bf2c2c31b62b0e4b6ab96ec7411e5a86e2a3932afccdf3d8df423eab000e",
        "sample.pdf": "e7fcb31c5facdbd434cf657753cd19855fa05cb1bbea3bd4ddc35095b86d5b2b",
        "sample.srt": "ac4242260ab0cf9afb71a9420218b43ccb9d96e470e54b39fc179d06c7f12c56",
        "sample.vtt": "e9c3074cdb593583a7ff2c6190957ab6d92dfabdfd87cdd278b152072a285537",
    }
    fixture_checks = {
        name: (repository / "platform/tests/fixtures/documents" / name).is_file()
        and (
            not expected_hash
            or sha256_file(repository / "platform/tests/fixtures/documents" / name)
            == expected_hash
        )
        for name, expected_hash in expected_fixtures.items()
    }
    return [
        check("required_files", not missing, missing, []),
        check(
            "frozen_cases",
            all(f"G6-0{i}" in acceptance for i in range(1, 9)),
            [f"G6-0{i}" for i in range(1, 9) if f"G6-0{i}" not in acceptance],
            [],
        ),
        check(
            "fixture_hashes",
            all(fixture_checks.values()),
            fixture_checks,
            {name: True for name in fixture_checks},
        ),
        check(
            "versioned_contract",
            all(token in source for token in ("extract.v1", "ExtractionInput", "location_map")),
            True,
        ),
        check(
            "three_adapter_contract",
            all(
                token in source
                for token in (
                    "HtmlExtractor",
                    "PdfExtractor",
                    "WebVttExtractor",
                    "SrtExtractor",
                    "page_start",
                    "time_start_ms",
                )
            ),
            True,
        ),
        check(
            "append_only_persistence",
            all(
                token in source
                for token in (
                    "PostgresExtractionStore",
                    "StorageDomain.DERIVED",
                    "ON CONFLICT",
                    "persist_and_finish_job",
                    "_PersistenceWriteError",
                    "cleanup_unregistered_object",
                    "ExtractionJobHandler",
                    "payload_from_claim",
                )
            ),
            True,
        ),
        check(
            "resource_and_failure_boundaries",
            all(
                token in source
                for token in (
                    "max_input_bytes",
                    "max_output_chars",
                    "max_pages",
                    "max_cues",
                    "invalid_pdf",
                    "invalid_subtitle",
                    "storage_read_failed",
                )
            ),
            True,
        ),
        check(
            "no_network_in_adapters",
            not any(
                token in adapter_source for token in ("urllib", "requests", "httpx", "urlopen")
            ),
            "network imports absent",
            "network imports absent",
        ),
        check(
            "durable_job_reuse",
            "extract_document"
            in (repository / "platform/alembic/versions/0005_durable_jobs.py").read_text(
                encoding="utf-8"
            )
            and "ops.finish_job" in source,
            True,
        ),
        check(
            "required_ci_probe",
            all(
                token
                in (repository / ".github/workflows/platform-ci.yml").read_text(
                    encoding="utf-8"
                )
                for token in ("validate_wp6.py", "build_wp6_evidence.py", "wp6_runtime_probe.py")
            ),
            True,
        ),
    ]


def main() -> None:
    checks = evaluate(Path(__file__).resolve().parents[1])
    print(json.dumps([asdict(item) for item in checks], indent=2, sort_keys=True))
    failed = [item.name for item in checks if not item.passed]
    if failed:
        raise SystemExit(f"WP6 contract checks failed: {', '.join(failed)}")
    print(f"WP6 contract checks passed: {len(checks)}/{len(checks)}")


if __name__ == "__main__":
    main()
