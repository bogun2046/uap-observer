"""G10-27 deterministic capacity, HTTP latency, and publication freshness probe.

The capacity fixture creates valid upstream records, uses the real review-decision
function to create v2 grants/manifests, and invokes the controlled rebuild entrypoint.
It never disables constraints or triggers. Freshness is measured on a settled
100,000-document projection through the real grant, Publisher, and anonymous Public
HTTP paths while a concurrent read workload is active.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast
from urllib.parse import quote, urlencode, urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEED = "g10-27-frozen-capacity-v1"
DEFAULT_DOCUMENTS = 100_000
DEFAULT_CLAIMS = 1_000
DEFAULT_ENTITIES = 500
DEFAULT_REQUESTS = 500
DEFAULT_CONCURRENCY = 8
DEFAULT_WARMUP = 50
DEFAULT_FRESHNESS_SAMPLES = 120
DEFAULT_GRANT_RATE = 2.0
DEFAULT_BACKGROUND_CONCURRENCY = 8
DOCUMENT_P95_MS = 300.0
DOCUMENT_P99_MS = 800.0
SEARCH_P95_MS = 500.0
SEARCH_P99_MS = 1200.0
FRESHNESS_P95_SECONDS = 120.0
CATEGORIES = (
    "official_report",
    "government_document",
    "military",
    "scientific_research",
    "historical_event",
    "sighting",
    "disputed_event",
    "other",
)
FACT_STATUSES = (
    "official_record",
    "corroborated",
    "source_reported",
    "unverified",
    "disputed",
    "opinion",
)
REQUIRED_PASSWORDS = {
    "uap_api": "UAP_API_PASSWORD",
    "uap_publisher": "UAP_PUBLISHER_PASSWORD",
    "uap_public_reader": "UAP_PUBLIC_READER_PASSWORD",
}


@dataclass(frozen=True)
class DatasetSpec:
    seed: str = DEFAULT_SEED
    documents: int = DEFAULT_DOCUMENTS
    claims: int = DEFAULT_CLAIMS
    entities: int = DEFAULT_ENTITIES

    def validate(self) -> None:
        if self.documents < 1:
            raise ValueError("documents must be positive")
        if not 0 <= self.claims <= self.documents:
            raise ValueError("claims must be between zero and documents")
        if not 0 <= self.entities <= self.claims:
            raise ValueError("entities must be between zero and claims")


@dataclass(frozen=True)
class LatencySample:
    endpoint: str
    sequence: int
    path: str
    status: int | None
    latency_ms: float
    started_at: str
    finished_at: str
    error: str | None


@dataclass
class LoggedProcess:
    """Child process whose combined output is drained directly to a file."""

    name: str
    process: subprocess.Popen[str]
    log_path: Path
    log_file: TextIO


class LoggedProcessGroup:
    """Own child processes and logs across normal, exceptional, and partial startup."""

    def __init__(self, evidence: Path) -> None:
        self._evidence = evidence
        self._children: list[LoggedProcess] = []

    def __enter__(self) -> LoggedProcessGroup:
        return self

    def __exit__(self, *_error: object) -> None:
        self.stop_all()

    def start(
        self,
        name: str,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> LoggedProcess:
        log_path = self._evidence / f"{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed local executable and arguments
                list(argv),
                cwd=cwd,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except BaseException:
            log_file.close()
            raise
        child = LoggedProcess(name, process, log_path, log_file)
        self._children.append(child)
        return child

    def stop_all(self) -> None:
        for child in reversed(self._children):
            if child.process.poll() is None:
                child.process.terminate()
        for child in reversed(self._children):
            try:
                child.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.process.kill()
                child.process.wait(timeout=5)
            finally:
                child.log_file.flush()
                child.log_file.close()
        self._children.clear()


class StopEventOnExit:
    """Set a worker stop event before executor context managers begin waiting."""

    def __init__(self, event: threading.Event) -> None:
        self._event = event

    def __enter__(self) -> StopEventOnExit:
        return self

    def __exit__(self, *_error: object) -> None:
        self._event.set()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def stable_uuid(seed: str, kind: str, index: int = 0) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"uap:g10-27:{seed}:{kind}:{index}")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def freshness_url(seed: str, index: int) -> str:
    return f"https://g10-27.invalid/fresh/{sha256_text(seed)[:16]}/{index:04d}"


def percentile(values: Sequence[float], quantile: float) -> float:
    """Return the nearest-rank percentile, retaining every measured sample."""

    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0 < quantile <= 1:
        raise ValueError("quantile must be in (0, 1]")
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(quantile * len(ordered)) - 1)])


def summarize(samples: Sequence[LatencySample]) -> dict[str, object]:
    if not samples:
        raise ValueError("summary requires at least one sample")
    values = [item.latency_ms for item in samples]
    failures = [item for item in samples if item.status != 200 or item.error is not None]
    elapsed = max(
        0.000001,
        (
            datetime.fromisoformat(max(item.finished_at for item in samples))
            - datetime.fromisoformat(min(item.started_at for item in samples))
        ).total_seconds(),
    )
    return {
        "samples": len(samples),
        "failures": len(failures),
        "error_rate": len(failures) / len(samples),
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": max(values),
        "throughput_rps": len(samples) / elapsed,
    }


def workload_paths(
    seed: str, document_ids: Sequence[uuid.UUID], requests: int
) -> dict[str, list[str]]:
    if not document_ids:
        raise ValueError("document IDs are required")
    rng = random.Random(seed)  # noqa: S311 - deterministic benchmark schedule
    detail_ids = [str(document_ids[rng.randrange(len(document_ids))]) for _ in range(requests)]
    search_terms = (
        "orbital signal",
        "public observation",
        "科学 观测",
        "historical report",
        "military signal",
    )
    return {
        "documents": [
            "/v1/documents?"
            + urlencode(
                {
                    "limit": 20,
                    "category": CATEGORIES[index % len(CATEGORIES)],
                    "fact_status": FACT_STATUSES[(index // len(CATEGORIES)) % len(FACT_STATUSES)],
                    "nonce": "",
                }
            ).replace("&nonce=", "")
            for index in range(requests)
        ],
        "detail": [f"/v1/documents/{document_id}" for document_id in detail_ids],
        "search": [
            "/v1/search?"
            + urlencode(
                {
                    "q": search_terms[index % len(search_terms)],
                    "limit": 20,
                    "category": CATEGORIES[index % len(CATEGORIES)],
                }
            )
            for index in range(requests)
        ],
    }


def role_url(admin_url: str, role: str) -> str:
    variable = REQUIRED_PASSWORDS[role]
    password = os.environ.get(variable)
    if not password:
        raise RuntimeError(f"{variable} is required")
    parts = conninfo_to_dict(admin_url.replace("postgresql+psycopg://", "postgresql://", 1))
    parts["user"] = role
    parts["password"] = password
    return make_conninfo(**parts)  # type: ignore[arg-type]


def execute(connection: psycopg.Connection[Any], statement: str, *params: object) -> None:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def scalar(connection: psycopg.Connection[Any], statement: str, *params: object) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("query returned no row")
    return row[0]


def _foundation(admin: psycopg.Connection[Any], seed: str) -> dict[str, uuid.UUID]:
    ids = {
        name: stable_uuid(seed, name)
        for name in (
            "opener",
            "reviewer",
            "source",
            "source_config",
            "job",
            "source_run",
            "stored_object",
            "artifact",
            "artifact_version",
        )
    }
    now = datetime.now(UTC)
    with admin.transaction():
        execute(
            admin,
            """
            INSERT INTO audit.principals (id, principal_type, issuer, subject, display_name)
            VALUES (%s, 'person', 'https://g10-27.invalid', %s, 'G10-27 opener'),
                   (%s, 'person', 'https://g10-27.invalid', %s, 'G10-27 reviewer')
            """,
            ids["opener"],
            f"{seed}-opener",
            ids["reviewer"],
            f"{seed}-reviewer",
        )
        execute(
            admin,
            """
            INSERT INTO audit.role_bindings (
                id, principal_id, role, scope_type, scope_id, reason, granted_by, granted_at
            ) VALUES (%s, %s, 'reviewer', 'global', NULL, 'G10-27 fixture', %s, %s)
            """,
            stable_uuid(seed, "reviewer-binding"),
            ids["reviewer"],
            ids["opener"],
            now,
        )
        execute(
            admin,
            """
            INSERT INTO ingest.sources (id, slug, name, source_type, homepage_url)
            VALUES (%s, %s, 'G10-27 deterministic source', 'web', 'https://g10-27.invalid')
            """,
            ids["source"],
            f"g10-27-{sha256_text(seed)[:16]}",
        )
        execute(
            admin,
            """
            INSERT INTO ingest.source_config_versions (
                id, source_id, version_no, configuration, configuration_sha256,
                effective_from, changed_by, change_reason
            ) VALUES (%s, %s, 1, '{}'::jsonb, %s, %s, %s, 'G10-27 fixture')
            """,
            ids["source_config"],
            ids["source"],
            sha256_text("{}"),
            now,
            ids["opener"],
        )
        execute(
            admin,
            """
            INSERT INTO ops.jobs (
                id, job_type, payload, payload_schema_version, idempotency_key,
                status, priority, available_at, max_attempts, timeout_seconds, completed_at
            ) VALUES (%s, 'collect_source', '{}'::jsonb, 'source.v1', %s,
                      'succeeded', 0, %s, 1, 60, %s)
            """,
            ids["job"],
            f"g10-27-{seed}",
            now,
            now,
        )
        execute(
            admin,
            """
            INSERT INTO ingest.source_runs (
                id, source_id, source_config_version_id, job_id, run_key, outcome,
                started_at, finished_at
            ) VALUES (%s, %s, %s, %s, %s, 'succeeded', %s, %s)
            """,
            ids["source_run"],
            ids["source"],
            ids["source_config"],
            ids["job"],
            f"g10-27-run-{seed}",
            now,
            now,
        )
        execute(
            admin,
            """
            INSERT INTO core.stored_objects (
                id, storage_domain, bucket_name, object_key, content_sha256,
                byte_length, media_type, verified_at
            ) VALUES (%s, 'raw', 'raw', %s, %s, 1, 'text/plain', %s)
            """,
            ids["stored_object"],
            f"g10-27/{sha256_text(seed)}",
            sha256_text(f"raw:{seed}"),
            now,
        )
        execute(
            admin,
            """
            INSERT INTO ingest.artifacts (
                id, source_id, canonical_locator, artifact_kind, first_seen_at, last_seen_at
            ) VALUES (%s, %s, %s, 'html', %s, %s)
            """,
            ids["artifact"],
            ids["source"],
            f"https://g10-27.invalid/artifact/{quote(seed, safe='')}",
            now,
            now,
        )
        execute(
            admin,
            """
            INSERT INTO ingest.artifact_versions (
                id, artifact_id, source_run_id, stored_object_id, retrieved_at
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            ids["artifact_version"],
            ids["artifact"],
            ids["source_run"],
            ids["stored_object"],
            now,
        )
    return ids


def _copy_rows(
    connection: psycopg.Connection[Any], statement: str, rows: Iterable[Sequence[object]]
) -> None:
    with connection.cursor() as cursor, cursor.copy(statement) as copy:
        for row in rows:
            copy.write_row(row)


def _document_title(index: int) -> str:
    if index % 10 == 0:
        return f"科学 观测 orbital signal document {index:06d}"
    if index % 7 == 0:
        return f"Historical report public observation {index:06d}"
    return f"Orbital signal public observation document {index:06d}"


def _seed_capacity_upstream(
    admin: psycopg.Connection[Any], spec: DatasetSpec, ids: dict[str, uuid.UUID]
) -> None:
    now = datetime.now(UTC)
    with admin.transaction():
        _copy_rows(
            admin,
            "COPY core.documents (id, source_id, source_item_key, canonical_url, "
            "document_kind, first_seen_at, last_seen_at) FROM STDIN",
            (
                (
                    stable_uuid(spec.seed, "document", index),
                    ids["source"],
                    f"doc-{index:06d}",
                    f"https://g10-27.invalid/documents/{index:06d}",
                    "article",
                    now,
                    now,
                )
                for index in range(spec.documents)
            ),
        )
        _copy_rows(
            admin,
            "COPY core.document_versions (id, document_id, artifact_version_id, version_no, "
            "original_title, source_published_at, language_code, normalized_content_sha256) "
            "FROM STDIN",
            (
                (
                    stable_uuid(spec.seed, "document-version", index),
                    stable_uuid(spec.seed, "document", index),
                    ids["artifact_version"],
                    1,
                    _document_title(index),
                    now,
                    "zh" if index % 10 == 0 else "en",
                    sha256_text(f"normalized:{spec.seed}:{index}"),
                )
                for index in range(spec.documents)
            ),
        )
        _copy_rows(
            admin,
            "COPY audit.review_cases (id, document_version_id, case_type, status, priority, "
            "assigned_to, opened_by, opened_at) FROM STDIN",
            (
                (
                    stable_uuid(spec.seed, "document-case", index),
                    stable_uuid(spec.seed, "document-version", index),
                    "document",
                    "assigned",
                    0,
                    ids["reviewer"],
                    ids["opener"],
                    now,
                )
                for index in range(spec.documents)
            ),
        )
        _copy_rows(
            admin,
            "COPY core.entities (id, entity_type, canonical_name, description, country_code, "
            "status) FROM STDIN",
            (
                (
                    stable_uuid(spec.seed, "entity", index),
                    ("organization", "location", "person")[index % 3],
                    f"Representative entity {index:05d}",
                    f"Deterministic public entity {index}",
                    ("US", "CN", None)[index % 3],
                    "active",
                )
                for index in range(spec.entities)
            ),
        )
        _copy_rows(
            admin,
            "COPY audit.review_cases (id, entity_id, case_type, status, priority, assigned_to, "
            "opened_by, opened_at) FROM STDIN",
            (
                (
                    stable_uuid(spec.seed, "entity-case", index),
                    stable_uuid(spec.seed, "entity", index),
                    "entity",
                    "assigned",
                    0,
                    ids["reviewer"],
                    ids["opener"],
                    now,
                )
                for index in range(spec.entities)
            ),
        )
        _copy_rows(
            admin,
            "COPY core.evidence_spans (id, document_version_id, evidence_text, locator_type, "
            "char_start, char_end, locator, locator_sha256) FROM STDIN",
            (
                (
                    stable_uuid(spec.seed, "evidence", index),
                    stable_uuid(spec.seed, "document-version", index),
                    f"Public evidence excerpt for observation {index:05d}",
                    "text",
                    0,
                    48,
                    json.dumps({"char_start": 0, "char_end": 48}, separators=(",", ":")),
                    sha256_text(f"locator:{spec.seed}:{index}"),
                )
                for index in range(spec.claims)
            ),
        )
        _copy_rows(
            admin,
            "COPY core.claims (id, subject_entity_id, claim_text, claim_fingerprint, claim_type, "
            "assertion_status, attribution, created_by, document_version_id) FROM STDIN",
            (
                (
                    stable_uuid(spec.seed, "claim", index),
                    stable_uuid(spec.seed, "entity", index % spec.entities)
                    if spec.entities
                    else None,
                    f"Public observation claim {index:05d} orbital signal",
                    sha256_text(f"claim:{spec.seed}:{index}"),
                    ("observation", "attribution", "event", "assessment", "other")[index % 5],
                    ("reported", "corroborated", "disputed", "unverified", "false")[index % 5],
                    None,
                    ids["opener"],
                    stable_uuid(spec.seed, "document-version", index),
                )
                for index in range(spec.claims)
            ),
        )
        _copy_rows(
            admin,
            "COPY core.claim_evidence (id, claim_id, evidence_span_id, document_version_id, "
            "support_type) FROM STDIN",
            (
                (
                    stable_uuid(spec.seed, "claim-evidence", index),
                    stable_uuid(spec.seed, "claim", index),
                    stable_uuid(spec.seed, "evidence", index),
                    stable_uuid(spec.seed, "document-version", index),
                    "supports",
                )
                for index in range(spec.claims)
            ),
        )
        _copy_rows(
            admin,
            "COPY audit.review_cases (id, claim_id, case_type, status, priority, assigned_to, "
            "opened_by, opened_at) FROM STDIN",
            (
                (
                    stable_uuid(spec.seed, "claim-case", index),
                    stable_uuid(spec.seed, "claim", index),
                    "claim",
                    "assigned",
                    0,
                    ids["reviewer"],
                    ids["opener"],
                    now,
                )
                for index in range(spec.claims)
            ),
        )


def _grant_capacity(api: psycopg.Connection[Any], spec: DatasetSpec, reviewer: uuid.UUID) -> None:
    blocks = (
        (
            "document",
            """
            SELECT review_case.id, version.original_title,
                   substring(document.source_item_key from '[0-9]+$')::integer AS item_no
              FROM audit.review_cases AS review_case
              JOIN core.document_versions AS version ON version.id=review_case.document_version_id
              JOIN core.documents AS document ON document.id=version.document_id
             WHERE review_case.case_type='document' AND review_case.opened_by=%s
             ORDER BY document.source_item_key
            """,
        ),
        (
            "entity",
            "SELECT id, NULL::text, 0 FROM audit.review_cases "
            "WHERE case_type='entity' AND opened_by=%s ORDER BY id",
        ),
        (
            "claim",
            "SELECT id, NULL::text, 0 FROM audit.review_cases "
            "WHERE case_type='claim' AND opened_by=%s ORDER BY id",
        ),
    )
    opener_literal = f"'{stable_uuid(spec.seed, 'opener')}'::uuid"
    category_array = ",".join(f"'{item}'" for item in CATEGORIES)
    fact_status_array = ",".join(f"'{item}'" for item in FACT_STATUSES)
    for case_type, query in blocks:
        resolved_query = query.replace("%s", opener_literal)
        case_type_literal = f"'{case_type}'"
        with api.transaction(), api.cursor() as cursor:
            cursor.execute("SELECT set_config('uap.principal_id', %s, true)", (str(reviewer),))
            cursor.execute(
                f"""
                DO $g10_27$
                DECLARE row_data record;
                BEGIN
                  FOR row_data IN {resolved_query}
                  LOOP
                    PERFORM set_config(
                      'uap.request_id', md5(row_data.id::text || ':g10-27-grant')::uuid::text, true
                    );
                    IF {case_type_literal} = 'document' THEN
                      PERFORM audit.record_review_decision(
                        row_data.id, 'approve', 'G10-27 deterministic capacity publication',
                        jsonb_build_object('publication', jsonb_build_object(
                          'title', row_data.original_title,
                          'summary', 'Deterministic public summary ' || row_data.item_no::text,
                          'category', (ARRAY[{category_array}])[
                            1 + row_data.item_no % {len(CATEGORIES)}
                          ],
                          'fact_status', (ARRAY[{fact_status_array}])[
                            1 + row_data.item_no % {len(FACT_STATUSES)}
                          ],
                          'summary_analysis_result_id', NULL
                        ))
                      );
                    ELSE
                      PERFORM audit.record_review_decision(
                        row_data.id, 'approve', 'G10-27 deterministic capacity publication',
                        '{{}}'::jsonb
                      );
                    END IF;
                  END LOOP;
                END
                $g10_27$;
                """
            )


def _seed_identities(admin: psycopg.Connection[Any], spec: DatasetSpec) -> None:
    with admin.transaction():
        _copy_rows(
            admin,
            "COPY audit.document_public_identities (document_id, public_id, slug) FROM STDIN",
            (
                (
                    stable_uuid(spec.seed, "document", index),
                    stable_uuid(spec.seed, "public-document", index),
                    "d-" + stable_uuid(spec.seed, "public-document", index).hex,
                )
                for index in range(spec.documents)
            ),
        )
        _copy_rows(
            admin,
            "COPY audit.entity_public_identities (entity_id, public_id, slug) FROM STDIN",
            (
                (
                    stable_uuid(spec.seed, "entity", index),
                    stable_uuid(spec.seed, "public-entity", index),
                    "e-" + stable_uuid(spec.seed, "public-entity", index).hex,
                )
                for index in range(spec.entities)
            ),
        )
        _copy_rows(
            admin,
            "COPY audit.claim_public_identities (claim_id, document_id, public_id, "
            "display_ordinal) FROM STDIN",
            (
                (
                    stable_uuid(spec.seed, "claim", index),
                    stable_uuid(spec.seed, "document", index),
                    stable_uuid(spec.seed, "public-claim", index),
                    0,
                )
                for index in range(spec.claims)
            ),
        )
        _copy_rows(
            admin,
            "COPY audit.evidence_public_identities (evidence_span_id, public_id) FROM STDIN",
            (
                (
                    stable_uuid(spec.seed, "evidence", index),
                    stable_uuid(spec.seed, "public-evidence", index),
                )
                for index in range(spec.claims)
            ),
        )


def _rebuild(admin: psycopg.Connection[Any], spec: DatasetSpec) -> dict[str, object]:
    rebuild_id = stable_uuid(spec.seed, "rebuild")
    admin.commit()
    with admin.cursor() as cursor:
        cursor.execute("SET SESSION AUTHORIZATION uap_migrator")
    admin.commit()
    row: Sequence[object] | None = None
    try:
        with admin.transaction(), admin.cursor() as cursor:
            cursor.execute("SELECT * FROM ops.rebuild_public_projection(%s)", (rebuild_id,))
            row = cursor.fetchone()
    finally:
        admin.rollback()
        with admin.cursor() as cursor:
            cursor.execute("RESET SESSION AUTHORIZATION")
        admin.commit()
    if row is None:
        raise RuntimeError("rebuild returned no result")
    return {
        "rebuild_id": str(row[0]),
        "input_digest": str(row[1]).strip(),
        "result_digest": str(row[2]).strip(),
        "counts": row[3],
        "status": row[4],
        "replayed": row[5],
    }


def prepare_capacity(admin_url: str, spec: DatasetSpec, evidence: Path) -> dict[str, object]:
    spec.validate()
    started = utc_now()
    monotonic = time.perf_counter()
    with psycopg.connect(admin_url) as admin:
        ids = _foundation(admin, spec.seed)
        _seed_capacity_upstream(admin, spec, ids)
        with psycopg.connect(role_url(admin_url, "uap_api")) as api:
            _grant_capacity(api, spec, ids["reviewer"])
        _seed_identities(admin, spec)
        rebuild = _rebuild(admin, spec)
        with admin.cursor() as cursor:
            cursor.execute("ANALYZE")
        admin.commit()
        count_queries = {
            "public.documents": "SELECT count(*) FROM public.documents",
            "public.claims": "SELECT count(*) FROM public.claims",
            "public.evidence": "SELECT count(*) FROM public.evidence",
            "public.entities": "SELECT count(*) FROM public.entities",
            "public.document_entities": "SELECT count(*) FROM public.document_entities",
            "public.search_documents": "SELECT count(*) FROM public.search_documents",
        }
        counts = {name: int(scalar(admin, query)) for name, query in count_queries.items()}
    result = {
        "schema": "g10-27-capacity-dataset.v1",
        "started_at": started,
        "finished_at": utc_now(),
        "duration_seconds": time.perf_counter() - monotonic,
        "spec": asdict(spec),
        "distribution": {
            "claims_per_document": {"zero": spec.documents - spec.claims, "one": spec.claims},
            "evidence_per_claim": 1,
            "subject_entity_links": spec.claims if spec.entities else 0,
            "representative_scale_rationale": "claims cover all five claim/assertion variants; "
            "entities cover three entity types; proportions were not frozen",
            "category_cycle": list(CATEGORIES),
            "fact_status_cycle": list(FACT_STATUSES),
            "title_language": {"zh_en_percent": 10, "en_percent": 90},
            "title_length_chars": {"min": 40, "max": 56},
            "summary_length_chars": {"min": 30, "max": 40},
        },
        "counts": counts,
        "rebuild": rebuild,
        "fixture_path": "upstream rows -> audit.record_review_decision -> active v2 manifests -> "
        "ops.rebuild_public_projection",
        "constraints_or_triggers_disabled": False,
    }
    (evidence / "dataset-summary.json").write_text(
        json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return result


def _request(base_url: str, path: str, endpoint: str, sequence: int) -> LatencySample:
    parsed = urlsplit(base_url)
    if parsed.hostname is None:
        raise ValueError("base URL requires a hostname")
    started_at = utc_now()
    started = time.perf_counter_ns()
    status: int | None = None
    error: str | None = None
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
    try:
        connection.request("GET", path, headers={"X-Request-ID": str(uuid.uuid4())})
        response = connection.getresponse()
        status = response.status
        response.read()
    except (OSError, TimeoutError, http.client.HTTPException) as caught:
        error = type(caught).__name__
    finally:
        connection.close()
    finished = time.perf_counter_ns()
    return LatencySample(
        endpoint=endpoint,
        sequence=sequence,
        path=path,
        status=status,
        latency_ms=(finished - started) / 1_000_000,
        started_at=started_at,
        finished_at=utc_now(),
        error=error,
    )


def _wait_http(base_url: str, child: LoggedProcess) -> None:
    for _ in range(120):
        if child.process.poll() is not None:
            child.log_file.flush()
            output = child.log_path.read_text(encoding="utf-8")
            raise RuntimeError(f"Public API exited before readiness: {output[-500:]}")
        sample = _request(base_url, "/healthz", "healthz", 0)
        if sample.status == 200:
            return
        time.sleep(0.1)
    raise RuntimeError("Public API readiness timed out")


def _start_public_api(
    admin_url: str, port: int, processes: LoggedProcessGroup
) -> tuple[LoggedProcess, str]:
    reader_url = role_url(admin_url, "uap_public_reader")
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join((str(PLATFORM_ROOT / "src"), str(PLATFORM_ROOT))),
        "UAP_PUBLIC_DATABASE_URL": reader_url,
        "UAP_PUBLIC_CURSOR_SECRET": sha256_text(DEFAULT_SEED),
        "UAP_PUBLIC_HOST": "127.0.0.1",
        "UAP_PUBLIC_PORT": str(port),
        "UAP_PUBLIC_POOL_MIN_SIZE": "1",
        "UAP_PUBLIC_POOL_MAX_SIZE": "16",
    }
    child = processes.start(
        "public-api",
        [sys.executable, "-m", "uap_platform.public_api.server"],
        cwd=PLATFORM_ROOT,
        env=environment,
    )
    base_url = f"http://127.0.0.1:{port}"
    _wait_http(base_url, child)
    return child, base_url


def _run_workload(
    base_url: str,
    paths: dict[str, list[str]],
    *,
    concurrency: int,
    warmup: int,
) -> list[LatencySample]:
    for endpoint, endpoint_paths in paths.items():
        for sequence, path in enumerate(endpoint_paths[:warmup]):
            sample = _request(base_url, path, f"{endpoint}-warmup", sequence)
            if sample.status != 200:
                raise RuntimeError(f"{endpoint} warmup failed with {sample.status}")
    measured: list[LatencySample] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(_request, base_url, path, endpoint, sequence)
            for endpoint, endpoint_paths in paths.items()
            for sequence, path in enumerate(endpoint_paths)
        ]
        for future in as_completed(futures):
            measured.append(future.result())
    return sorted(measured, key=lambda item: (item.endpoint, item.sequence))


def _explain(reader: psycopg.Connection[Any], document_id: uuid.UUID) -> dict[str, object]:
    statements = {
        "documents": (
            "SELECT id FROM public.documents WHERE category=%s::public.document_category "
            "AND fact_status=%s::public.fact_status ORDER BY published_at DESC,id DESC LIMIT 21",
            ("official_report", "official_record"),
        ),
        "detail_document": ("SELECT * FROM public.documents WHERE id=%s", (document_id,)),
        "detail_claims": (
            "SELECT * FROM public.claims WHERE document_id=%s ORDER BY ordinal,id",
            (document_id,),
        ),
        "detail_entities": (
            "SELECT entity.* FROM public.document_entities link JOIN public.entities entity "
            "ON entity.id=link.entity_id WHERE link.document_id=%s "
            'ORDER BY entity.name COLLATE "C",entity.id',
            (document_id,),
        ),
        "search": (
            "WITH query_input AS (SELECT websearch_to_tsquery('simple', %s) AS query), "
            "ranked AS (SELECT document.id, document.slug, document.title, document.summary, "
            "document.category, document.fact_status, document.source_name, "
            "document.canonical_source_url, document.source_published_at, "
            "document.published_at, document.revised_at, document.revision_no, "
            "search.display_text, "
            "ts_rank_cd(search.search_vector, query_input.query)::real AS rank, "
            "ts_headline('simple', search.display_text, query_input.query, "
            "'StartSel=,StopSel=,MaxFragments=2,MaxWords=20,MinWords=5') AS headline "
            "FROM public.search_documents AS search "
            "JOIN public.documents AS document ON document.id=search.document_id "
            "CROSS JOIN query_input WHERE search.search_vector @@ query_input.query "
            "AND (%s::public.document_category IS NULL OR "
            "document.category=%s::public.document_category) "
            "AND (%s::public.fact_status IS NULL OR "
            "document.fact_status=%s::public.fact_status)) "
            "SELECT * FROM ranked WHERE (%s::real IS NULL OR "
            "(rank,published_at,id)<(%s::real,%s::timestamptz,%s::uuid)) "
            "ORDER BY rank DESC,published_at DESC,id DESC LIMIT %s",
            (
                "orbital signal",
                "official_report",
                "official_report",
                None,
                None,
                None,
                None,
                None,
                None,
                21,
            ),
        ),
    }
    result: dict[str, object] = {}
    with reader.transaction(), reader.cursor() as cursor:
        for name, (statement, params) in statements.items():
            cursor.execute("EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON) " + statement, params)
            row = cursor.fetchone()
            result[name] = {
                "sql": statement,
                "parameters": [str(item) if item is not None else None for item in params],
                "plan": None if row is None else row[0],
            }
    return result


def environment_snapshot(admin: psycopg.Connection[Any]) -> dict[str, object]:
    setting_names = (
        "shared_buffers",
        "effective_cache_size",
        "work_mem",
        "maintenance_work_mem",
        "max_connections",
        "random_page_cost",
        "effective_io_concurrency",
        "max_parallel_workers_per_gather",
        "jit",
    )
    with admin.cursor() as cursor:
        cursor.execute(
            "SELECT name, setting, unit, source FROM pg_settings WHERE name=ANY(%s) ORDER BY name",
            (list(setting_names),),
        )
        settings = [
            dict(zip(("name", "setting", "unit", "source"), row, strict=True)) for row in cursor
        ]
    return {
        "captured_at": utc_now(),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "logical_cpu_count": os.cpu_count(),
            "python": sys.version,
        },
        "postgresql_version": str(scalar(admin, "SELECT version()")),
        "postgresql_settings": settings,
    }


def measure_capacity(
    admin_url: str,
    evidence: Path,
    *,
    port: int,
    requests: int,
    concurrency: int,
    warmup: int,
    seed: str,
) -> dict[str, object]:
    with psycopg.connect(admin_url) as admin:
        count = int(scalar(admin, "SELECT count(*) FROM public.documents"))
        if count < DEFAULT_DOCUMENTS:
            raise RuntimeError(f"capacity database has {count} documents; 100000 required")
        with admin.cursor() as cursor:
            cursor.execute("SELECT id FROM public.documents ORDER BY id")
            document_ids = [row[0] for row in cursor]
        environment = environment_snapshot(admin)
    paths = workload_paths(seed, document_ids, requests)
    started = utc_now()
    with LoggedProcessGroup(evidence) as processes:
        _process, base_url = _start_public_api(admin_url, port, processes)
        samples = _run_workload(
            base_url, paths, concurrency=concurrency, warmup=min(warmup, requests)
        )
    raw = evidence / "latency-samples.jsonl"
    raw.write_text(
        "".join(json.dumps(asdict(item), separators=(",", ":")) + "\n" for item in samples),
        encoding="utf-8",
    )
    grouped: dict[str, dict[str, object]] = {
        endpoint: summarize([item for item in samples if item.endpoint == endpoint])
        for endpoint in ("documents", "detail", "search")
    }
    thresholds = {
        "documents": {"p95_ms": DOCUMENT_P95_MS, "p99_ms": DOCUMENT_P99_MS},
        "detail": {"p95_ms": DOCUMENT_P95_MS, "p99_ms": DOCUMENT_P99_MS},
        "search": {"p95_ms": SEARCH_P95_MS, "p99_ms": SEARCH_P99_MS},
    }
    results: dict[str, object] = {}
    for endpoint, summary in grouped.items():
        target = thresholds[endpoint]
        results[endpoint] = {
            "status": "PASS"
            if summary["failures"] == 0
            and cast(float, summary["p95_ms"]) <= target["p95_ms"]
            and cast(float, summary["p99_ms"]) <= target["p99_ms"]
            else "FAIL",
            "statistics": summary,
            "thresholds": target,
        }
    with psycopg.connect(role_url(admin_url, "uap_public_reader")) as reader:
        plans = _explain(reader, document_ids[len(document_ids) // 2])
    (evidence / "explain.json").write_text(
        json.dumps(plans, indent=2, default=str) + "\n", encoding="utf-8"
    )
    output = {
        "schema": "g10-27-capacity-results.v1",
        "started_at": started,
        "finished_at": utc_now(),
        "measurement_conditions": {
            "entrypoint": "real anonymous Public HTTP process",
            "cache_miss_definition": "no CDN/application response cache exists; every request "
            "executes a new HTTP handler call and database transaction; PostgreSQL buffers "
            "are warmed",
            "warmup_requests_per_endpoint": min(warmup, requests),
            "measured_requests_per_endpoint": requests,
            "concurrency": concurrency,
            "request_timeout_seconds": 15,
            "percentile_method": "nearest-rank over all requests, including failed requests",
            "timing_boundary": "client before HTTP request through complete response body read",
        },
        "dataset_document_count": count,
        "environment": environment,
        "endpoints": results,
        "overall_status": "PASS"
        if all(cast(dict[str, object], item)["status"] == "PASS" for item in results.values())
        else "FAIL",
    }
    (evidence / "capacity-results.json").write_text(
        json.dumps(output, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return output


def _insert_freshness_document(
    admin: psycopg.Connection[Any], ids: dict[str, uuid.UUID], seed: str, index: int
) -> tuple[uuid.UUID, uuid.UUID, str]:
    document_id = stable_uuid(seed, "fresh-document", index)
    version_id = stable_uuid(seed, "fresh-document-version", index)
    title = f"G10-27 freshness marker {index:04d} {stable_uuid(seed, 'marker', index).hex[:12]}"
    now = datetime.now(UTC)
    with admin.transaction():
        execute(
            admin,
            """
            INSERT INTO core.documents (
                id, source_id, source_item_key, canonical_url, document_kind,
                first_seen_at, last_seen_at
            ) VALUES (%s, %s, %s, %s, 'article', %s, %s)
            """,
            document_id,
            ids["source"],
            f"fresh-{index:04d}",
            freshness_url(seed, index),
            now,
            now,
        )
        execute(
            admin,
            """
            INSERT INTO core.document_versions (
                id, document_id, artifact_version_id, version_no, original_title,
                source_published_at, language_code, normalized_content_sha256
            ) VALUES (%s, %s, %s, 1, %s, %s, 'en', %s)
            """,
            version_id,
            document_id,
            ids["artifact_version"],
            title,
            now,
            sha256_text(f"fresh:{seed}:{index}"),
        )
    return document_id, version_id, title


def _api_call(
    api: psycopg.Connection[Any],
    principal: uuid.UUID,
    request_id: uuid.UUID,
    query: str,
    params: tuple[object, ...],
) -> Any:
    with api.transaction(), api.cursor() as cursor:
        cursor.execute("SELECT set_config('uap.principal_id', %s, true)", (str(principal),))
        cursor.execute("SELECT set_config('uap.request_id', %s, true)", (str(request_id),))
        cursor.execute(query, params)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("API function returned no row")
    return row[0]


def _request_json(
    base_url: str, path: str, endpoint: str, sequence: int
) -> tuple[LatencySample, dict[str, object] | None]:
    parsed = urlsplit(base_url)
    if parsed.hostname is None:
        raise ValueError("base URL requires a hostname")
    started_at = utc_now()
    started = time.perf_counter_ns()
    status: int | None = None
    error: str | None = None
    body: dict[str, object] | None = None
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
    try:
        connection.request("GET", path, headers={"X-Request-ID": str(uuid.uuid4())})
        response = connection.getresponse()
        status = response.status
        payload = response.read()
        if status == 200:
            decoded = json.loads(payload)
            if isinstance(decoded, dict):
                body = cast(dict[str, object], decoded)
    except (OSError, TimeoutError, http.client.HTTPException, json.JSONDecodeError) as caught:
        error = type(caught).__name__
    finally:
        connection.close()
    finished = time.perf_counter_ns()
    return (
        LatencySample(
            endpoint=endpoint,
            sequence=sequence,
            path=path,
            status=status,
            latency_ms=(finished - started) / 1_000_000,
            started_at=started_at,
            finished_at=utc_now(),
            error=error,
        ),
        body,
    )


def _queue_snapshot(admin: psycopg.Connection[Any], label: str) -> dict[str, object]:
    with admin.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FILTER (WHERE published_at IS NULL AND terminal_at IS NULL),
                   count(*) FILTER (WHERE published_at IS NOT NULL),
                   count(*) FILTER (WHERE terminal_at IS NOT NULL),
                   coalesce(sum(publish_attempts), 0)
              FROM ops.outbox_events
             WHERE event_type LIKE 'publication.%'
            """
        )
        row = cursor.fetchone()
    admin.commit()
    if row is None:
        raise RuntimeError("publication queue snapshot returned no row")
    return {
        "label": label,
        "captured_at": utc_now(),
        "pending": int(row[0]),
        "published": int(row[1]),
        "terminal": int(row[2]),
        "publish_attempts": int(row[3]),
    }


def _poll_visibility(
    admin_url: str,
    base_url: str,
    document_id: uuid.UUID,
    expected_title: str,
    committed_monotonic: float,
    committed_at: str,
    *,
    sequence: int,
    poll_interval: float,
    timeout: float,
) -> dict[str, object]:
    deadline = committed_monotonic + timeout
    polls = 0
    last_status: int | None = None
    public_id: uuid.UUID | None = None
    last_error: str | None = None
    with psycopg.connect(admin_url) as admin:
        while time.perf_counter() < deadline:
            polls += 1
            if public_id is None:
                with admin.cursor() as cursor:
                    cursor.execute(
                        "SELECT public_id FROM audit.document_public_identities "
                        "WHERE document_id=%s",
                        (document_id,),
                    )
                    row = cursor.fetchone()
                admin.rollback()
                if row is not None:
                    public_id = row[0]
            if public_id is not None:
                sample, body = _request_json(
                    base_url,
                    f"/v1/documents/{public_id}",
                    "freshness-visibility",
                    sequence,
                )
                last_status = sample.status
                last_error = sample.error
                if (
                    sample.status == 200
                    and body is not None
                    and body.get("id") == str(public_id)
                    and body.get("title") == expected_title
                ):
                    visible_at = utc_now()
                    return {
                        "public_id": str(public_id),
                        "grant_committed_at": committed_at,
                        "visible_at": visible_at,
                        "latency_seconds": time.perf_counter() - committed_monotonic,
                        "polls": polls,
                        "last_http_status": last_status,
                        "last_error": last_error,
                        "timed_out": False,
                    }
            time.sleep(poll_interval)
    return {
        "public_id": None if public_id is None else str(public_id),
        "grant_committed_at": committed_at,
        "visible_at": None,
        "latency_seconds": timeout,
        "polls": polls,
        "last_http_status": last_status,
        "last_error": last_error or "VisibilityTimeout",
        "timed_out": True,
    }


def _run_background_worker(
    base_url: str,
    schedule: Sequence[tuple[str, str]],
    worker: int,
    workers: int,
    stop: threading.Event,
    results: list[LatencySample],
    lock: threading.Lock,
) -> None:
    iteration = 0
    while not stop.is_set():
        endpoint, path = schedule[(worker + iteration * workers) % len(schedule)]
        sample = _request(base_url, path, f"background-{endpoint}", worker * 1_000_000 + iteration)
        with lock:
            results.append(sample)
        iteration += 1


def measure_freshness(
    admin_url: str,
    evidence: Path,
    *,
    port: int,
    samples: int,
    poll_interval: float,
    timeout: float,
    seed: str,
    grant_rate: float,
    background_concurrency: int,
) -> dict[str, object]:
    if grant_rate <= 0:
        raise ValueError("grant rate must be positive")
    if background_concurrency < 1:
        raise ValueError("background concurrency must be positive")
    with (
        psycopg.connect(admin_url) as admin,
        psycopg.connect(role_url(admin_url, "uap_api")) as api,
    ):
        document_count = int(scalar(admin, "SELECT count(*) FROM public.documents"))
        if document_count < DEFAULT_DOCUMENTS:
            raise RuntimeError(
                f"freshness database has {document_count} public documents; 100000 required"
            )
        with admin.cursor() as cursor:
            cursor.execute("SELECT id FROM public.documents ORDER BY id LIMIT 10000")
            background_document_ids = [row[0] for row in cursor]
        admin.commit()
        ids = _foundation(admin, seed)
        environment = environment_snapshot(admin)
        admin.commit()
        prepared: list[dict[str, object]] = []
        for index in range(samples):
            document_id, version_id, title = _insert_freshness_document(admin, ids, seed, index)
            case_id = _api_call(
                api,
                ids["reviewer"],
                stable_uuid(seed, "fresh-open-request", index),
                "SELECT audit.open_review_case("
                "'document'::audit.review_case_type, %s, 0::smallint, %s)",
                (version_id, "G10-27 normal-load freshness publication review case"),
            )
            prepared.append(
                {"document_id": document_id, "case_id": case_id, "title": title, "index": index}
            )
        baseline_queue = _queue_snapshot(admin, "settled_baseline_precondition")
        if baseline_queue["pending"] != 0 or baseline_queue["terminal"] != 0:
            raise RuntimeError(
                "normal-load freshness requires a settled publication queue baseline"
            )
    publisher_environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join((str(PLATFORM_ROOT / "src"), str(PLATFORM_ROOT))),
        "UAP_DATABASE_URL": role_url(admin_url, "uap_publisher"),
    }
    records: list[dict[str, object]] = []
    background_samples: list[LatencySample] = []
    background_lock = threading.Lock()
    background_stop = threading.Event()
    workload = workload_paths(seed + ":background", background_document_ids, 300)
    background_schedule = [
        (endpoint, workload[endpoint][index])
        for index in range(300)
        for endpoint in ("documents", "detail", "search")
    ]
    queue_snapshots: list[dict[str, object]] = [baseline_queue]
    grant_interval = 1.0 / grant_rate
    publisher_exit_code: int | None = None
    with LoggedProcessGroup(evidence) as processes:
        publisher = processes.start(
            "publisher",
            [sys.executable, "-c", "from uap_platform.publishing.loop import main; main()"],
            cwd=PLATFORM_ROOT,
            env=publisher_environment,
        )
        _public_api, base_url = _start_public_api(admin_url, port, processes)
        with (
            psycopg.connect(admin_url) as admin,
            psycopg.connect(role_url(admin_url, "uap_api")) as api,
            ThreadPoolExecutor(max_workers=background_concurrency) as background_pool,
            ThreadPoolExecutor(max_workers=min(32, max(4, samples))) as visibility_pool,
            StopEventOnExit(background_stop),
        ):
            queue_snapshots.append(_queue_snapshot(admin, "before_background_and_grants"))
            background_futures = [
                background_pool.submit(
                    _run_background_worker,
                    base_url,
                    background_schedule,
                    worker,
                    background_concurrency,
                    background_stop,
                    background_samples,
                    background_lock,
                )
                for worker in range(background_concurrency)
            ]
            time.sleep(2.0)
            schedule_started = time.perf_counter()
            visibility_futures: list[tuple[dict[str, object], Any]] = []
            for prepared_item in prepared:
                index = cast(int, prepared_item["index"])
                target = schedule_started + index * grant_interval
                if time.perf_counter() < target:
                    time.sleep(target - time.perf_counter())
                commit_started_at = utc_now()
                try:
                    decision_id = _api_call(
                        api,
                        ids["reviewer"],
                        stable_uuid(seed, "fresh-decision-request", index),
                        "SELECT audit.record_review_decision(%s, 'approve', %s, %s::jsonb)",
                        (
                            prepared_item["case_id"],
                            "G10-27 normal-load freshness publication decision",
                            json.dumps(
                                {
                                    "publication": {
                                        "title": prepared_item["title"],
                                        "summary": "G10-27 normal-load freshness summary",
                                        "category": "official_report",
                                        "fact_status": "source_reported",
                                        "summary_analysis_result_id": None,
                                    }
                                },
                                separators=(",", ":"),
                            ),
                        ),
                    )
                except Exception as error:
                    records.append(
                        {
                            "sequence": index,
                            "document_id": str(prepared_item["document_id"]),
                            "case_id": str(prepared_item["case_id"]),
                            "decision_id": None,
                            "event_id": None,
                            "scheduled_offset_seconds": index * grant_interval,
                            "grant_commit_call_started_at": commit_started_at,
                            "grant_committed_at": None,
                            "visible_at": None,
                            "latency_seconds": timeout,
                            "polls": 0,
                            "last_http_status": None,
                            "last_error": type(error).__name__,
                            "timed_out": True,
                        }
                    )
                    continue
                committed_at = utc_now()
                committed_monotonic = time.perf_counter()
                event_id = scalar(
                    admin,
                    "SELECT event.id FROM ops.outbox_events event JOIN "
                    "audit.document_publication_grants grant_row "
                    "ON grant_row.id=event.aggregate_id "
                    "WHERE grant_row.decision_id=%s AND event.event_type='publication.granted'",
                    decision_id,
                )
                admin.commit()
                base_record = {
                    "sequence": index,
                    "document_id": str(prepared_item["document_id"]),
                    "case_id": str(prepared_item["case_id"]),
                    "decision_id": str(decision_id),
                    "event_id": str(event_id),
                    "scheduled_offset_seconds": index * grant_interval,
                    "actual_commit_offset_seconds": committed_monotonic - schedule_started,
                    "grant_commit_call_started_at": commit_started_at,
                }
                future = visibility_pool.submit(
                    _poll_visibility,
                    admin_url,
                    base_url,
                    cast(uuid.UUID, prepared_item["document_id"]),
                    cast(str, prepared_item["title"]),
                    committed_monotonic,
                    committed_at,
                    sequence=index,
                    poll_interval=poll_interval,
                    timeout=timeout,
                )
                visibility_futures.append((base_record, future))
            queue_snapshots.append(_queue_snapshot(admin, "after_grant_submissions"))
            for base_record, future in visibility_futures:
                records.append({**base_record, **future.result()})
            queue_snapshots.append(_queue_snapshot(admin, "after_visibility_wait"))
            background_stop.set()
            for background_future in background_futures:
                background_future.result(timeout=30)
            publisher_exit_code = publisher.process.poll()
    (evidence / "freshness-samples.jsonl").write_text(
        "".join(
            json.dumps(item, separators=(",", ":")) + "\n"
            for item in sorted(records, key=lambda item: cast(int, item["sequence"]))
        ),
        encoding="utf-8",
    )
    (evidence / "background-latency-samples.jsonl").write_text(
        "".join(
            json.dumps(asdict(item), separators=(",", ":")) + "\n"
            for item in sorted(background_samples, key=lambda item: (item.endpoint, item.sequence))
        ),
        encoding="utf-8",
    )
    latencies = [cast(float, item["latency_seconds"]) for item in records]
    failures = sum(bool(item["timed_out"]) or item["last_http_status"] != 200 for item in records)
    background_summary = {
        endpoint: summarize([item for item in background_samples if item.endpoint == endpoint])
        for endpoint in ("background-documents", "background-detail", "background-search")
    }
    background_failures = sum(cast(int, item["failures"]) for item in background_summary.values())
    p95 = percentile(latencies, 0.95)
    output = {
        "schema": "g10-27-freshness-results.v1",
        "started_at": records[0]["grant_committed_at"] if records else utc_now(),
        "finished_at": utc_now(),
        "samples": len(records),
        "failures": failures,
        "p50_seconds": percentile(latencies, 0.50),
        "p95_seconds": p95,
        "p99_seconds": percentile(latencies, 0.99),
        "max_seconds": max(latencies),
        "threshold_p95_seconds": FRESHNESS_P95_SECONDS,
        "status": "PASS"
        if failures == 0
        and background_failures == 0
        and publisher_exit_code is None
        and p95 <= FRESHNESS_P95_SECONDS
        else "FAIL",
        "measurement_conditions": {
            "grant_path": "uap_api audit.open_review_case + audit.record_review_decision",
            "publisher_path": "dedicated uap_publisher process claim/apply loop",
            "visibility_path": "anonymous GET /v1/documents",
            "dataset_public_documents_before_grants": document_count,
            "arrival_model": "fixed schedule independent of prior visibility",
            "grant_rate_per_second": grant_rate,
            "planned_duration_seconds": (samples - 1) * grant_interval,
            "background_mix": "equal documents/detail/search continuous HTTP requests",
            "background_concurrency": background_concurrency,
            "poll_interval_seconds": poll_interval,
            "timeout_seconds": timeout,
            "timing_boundary": "record_review_decision commit return through public HTTP "
            "visibility; grant_committed_at is recorded immediately before the monotonic timer",
        },
        "queue_snapshots": queue_snapshots,
        "background": {
            "samples": len(background_samples),
            "failures": background_failures,
            "statistics": background_summary,
        },
        "publisher_unexpected_exit_code": publisher_exit_code,
        "environment": environment,
    }
    (evidence / "freshness-results.json").write_text(
        json.dumps(output, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return output


def plan(args: argparse.Namespace) -> dict[str, object]:
    return {
        "status": "plan",
        "dataset": asdict(DatasetSpec(args.seed, args.documents, args.claims, args.entities)),
        "capacity": {
            "endpoints": ["GET /v1/documents", "GET /v1/documents/{document_id}", "GET /v1/search"],
            "requests_per_endpoint": args.requests,
            "concurrency": args.concurrency,
            "warmup_per_endpoint": args.warmup,
            "thresholds": {
                "documents_and_detail": {"p95_ms": DOCUMENT_P95_MS, "p99_ms": DOCUMENT_P99_MS},
                "search": {"p95_ms": SEARCH_P95_MS, "p99_ms": SEARCH_P99_MS},
            },
        },
        "freshness": {
            "samples": args.freshness_samples,
            "grant_rate_per_second": args.grant_rate,
            "background_concurrency": args.background_concurrency,
            "poll_interval_seconds": args.poll_interval,
            "timeout_seconds": args.freshness_timeout,
            "threshold_p95_seconds": FRESHNESS_P95_SECONDS,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run G10-27 capacity and freshness evidence")
    parser.add_argument(
        "mode", choices=("plan", "prepare-capacity", "measure-capacity", "freshness")
    )
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--documents", type=int, default=DEFAULT_DOCUMENTS)
    parser.add_argument("--claims", type=int, default=DEFAULT_CLAIMS)
    parser.add_argument("--entities", type=int, default=DEFAULT_ENTITIES)
    parser.add_argument("--requests", type=int, default=DEFAULT_REQUESTS)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    parser.add_argument("--freshness-samples", type=int, default=DEFAULT_FRESHNESS_SAMPLES)
    parser.add_argument("--grant-rate", type=float, default=DEFAULT_GRANT_RATE)
    parser.add_argument(
        "--background-concurrency", type=int, default=DEFAULT_BACKGROUND_CONCURRENCY
    )
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument("--freshness-timeout", type=float, default=180.0)
    parser.add_argument("--port", type=int, default=18081)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "plan":
        print(json.dumps(plan(args), indent=2))
        return 0
    if args.evidence_dir is None:
        raise SystemExit("--evidence-dir is required")
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    admin_url = os.environ.get("UAP_DATABASE_URL")
    if not admin_url:
        raise SystemExit("UAP_DATABASE_URL is required")
    spec = DatasetSpec(args.seed, args.documents, args.claims, args.entities)
    if args.mode == "prepare-capacity":
        result = prepare_capacity(admin_url, spec, evidence)
    elif args.mode == "measure-capacity":
        result = measure_capacity(
            admin_url,
            evidence,
            port=args.port,
            requests=args.requests,
            concurrency=args.concurrency,
            warmup=args.warmup,
            seed=args.seed,
        )
    else:
        result = measure_freshness(
            admin_url,
            evidence,
            port=args.port,
            samples=args.freshness_samples,
            poll_interval=args.poll_interval,
            timeout=args.freshness_timeout,
            seed=args.seed + ":freshness",
            grant_rate=args.grant_rate,
            background_concurrency=args.background_concurrency,
        )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status", result.get("overall_status", "PASS")) == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
