"""Exercise WP6 extraction provenance, adapters, idempotency, and job closure."""

from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
from minio import Minio
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from uap_platform.config import Settings, load_settings
from uap_platform.documents import (
    ExtractionJobHandler,
    ExtractionOutcome,
    payload_from_claim,
)
from uap_platform.documents.persistence import PostgresExtractionStore
from uap_platform.object_registry import (
    ObjectClient,
    StorageDomain,
    read_verified_object,
    sha256_bytes,
    store_and_register,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "documents"


def role_connection(admin_url: str) -> psycopg.Connection[Any]:
    params = conninfo_to_dict(admin_url.replace("postgresql+psycopg://", "postgresql://"))
    params.pop("user", None)
    params.pop("password", None)
    base = make_conninfo(**params)  # type: ignore[arg-type]
    return psycopg.connect(
        make_conninfo(base, user="uap_worker", password=os.environ["UAP_WORKER_PASSWORD"])
    )


def admin_connection(admin_url: str) -> psycopg.Connection[Any]:
    return psycopg.connect(admin_url.replace("postgresql+psycopg://", "postgresql://"))


def object_client(settings: Settings) -> Minio:
    return Minio(
        settings.s3_endpoint,
        access_key=settings.s3_access_key.get_secret_value(),
        secret_key=settings.s3_secret_key.get_secret_value(),
        secure=settings.s3_secure,
    )


def enqueue(
    connection: psycopg.Connection[Any],
    job_type: str,
    payload: dict[str, object],
    key: str,
    max_attempts: int = 3,
    payload_schema_version: str = "extract.v1",
) -> uuid.UUID:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT ops.enqueue_job(
                %s, %s::jsonb, %s, %s, 32767::smallint,
                'epoch'::timestamptz, %s, 60
            )
            """,
            (
                job_type,
                json.dumps(payload, sort_keys=True),
                payload_schema_version,
                key,
                max_attempts,
            ),
        )
        row = cursor.fetchone()
    connection.commit()
    if row is None:
        raise RuntimeError("enqueue_job returned no job id")
    return uuid.UUID(str(row[0]))


def claim_expected(
    connection: psycopg.Connection[Any], expected_job_id: uuid.UUID
) -> tuple[Any, ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT * FROM ops.claim_job(
                'worker', 'wp6-runtime-worker', ARRAY['extract_document'], 60)
            """
        )
        claim = cursor.fetchone()
    connection.commit()
    if claim is None or uuid.UUID(str(claim[0])) != expected_job_id:
        actual = None if claim is None else str(claim[0])
        raise RuntimeError(f"extract probe claimed {actual}; expected {expected_job_id}")
    return cast(tuple[Any, ...], claim)


def seed_source(
    administrator: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    key: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    source_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    config_id = uuid.uuid4()
    source_job_id = enqueue(
        worker,
        "fetch_source",
        {},
        f"wp6-source-{key}",
        max_attempts=1,
        payload_schema_version="rss.v1",
    )
    now = datetime.now(UTC)
    with administrator.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO audit.principals
                (id, principal_type, service_name, display_name)
            VALUES (%s, 'service', %s, 'WP6 runtime probe')
            """,
            (principal_id, f"wp6-runtime-{key}"),
        )
        cursor.execute(
            """
            INSERT INTO ingest.sources
                (id, slug, name, source_type, homepage_url)
            VALUES (%s, %s, %s, 'web', %s)
            """,
            (source_id, f"wp6-runtime-{key}", "WP6 runtime probe", "https://example.test"),
        )
        cursor.execute(
            """
            INSERT INTO ingest.source_config_versions
                (id, source_id, version_no, configuration, configuration_sha256,
                 effective_from, changed_by, change_reason)
            VALUES (%s, %s, 1, '{}'::jsonb, repeat('a', 64), %s, %s, 'WP6 runtime probe')
            """,
            (config_id, source_id, now, principal_id),
        )
        cursor.execute(
            """
            INSERT INTO ingest.source_runs
                (id, source_id, source_config_version_id, job_id, run_key,
                 outcome, payload_schema_version, started_at, finished_at)
            VALUES (%s, %s, %s, %s, %s, 'succeeded', 'rss.v1', %s, %s)
            """,
            (uuid.uuid4(), source_id, config_id, source_job_id, f"wp6-source-run-{key}", now, now),
        )
    administrator.commit()
    with administrator.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM ingest.source_runs WHERE job_id = %s",
            (source_job_id,),
        )
        source_run = cursor.fetchone()
    if source_run is None:
        raise RuntimeError("runtime source run seed was not created")
    return source_id, config_id, uuid.UUID(str(source_run[0]))


def seed_document(
    administrator: psycopg.Connection[Any],
    worker: psycopg.Connection[Any],
    client: ObjectClient,
    source_id: uuid.UUID,
    source_run_id: uuid.UUID,
    payload: bytes,
    media_type: str,
    artifact_kind: str,
    key: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    stored = store_and_register(client, worker, StorageDomain.RAW, payload, media_type)
    worker.commit()
    artifact_id = uuid.uuid4()
    artifact_version_id = uuid.uuid4()
    document_id = uuid.uuid4()
    document_version_id = uuid.uuid4()
    now = datetime.now(UTC)
    canonical_url = f"https://example.test/wp6/{key}"
    with administrator.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO ingest.artifacts
                (id, source_id, canonical_locator, artifact_kind, first_seen_at, last_seen_at)
            VALUES (%s, %s, %s, %s::ingest.artifact_kind, %s, %s)
            """,
            (artifact_id, source_id, canonical_url, artifact_kind, now, now),
        )
        cursor.execute(
            """
            INSERT INTO ingest.artifact_versions
                (id, artifact_id, source_run_id, stored_object_id, retrieved_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (artifact_version_id, artifact_id, source_run_id, stored.id, now),
        )
        cursor.execute(
            """
            INSERT INTO core.documents
                (id, source_id, source_item_key, canonical_url, document_kind,
                 first_seen_at, last_seen_at)
            VALUES (%s, %s, %s, %s, 'article', %s, %s)
            """,
            (document_id, source_id, key, canonical_url, now, now),
        )
        cursor.execute(
            """
            INSERT INTO core.document_versions
                (id, document_id, artifact_version_id, version_no,
                 normalized_content_sha256)
            VALUES (%s, %s, %s, 1, %s)
            """,
            (document_version_id, document_id, artifact_version_id, sha256_bytes(payload)),
        )
    administrator.commit()
    return document_version_id, stored.id


def run_claimed(
    database_url: str,
    settings: Settings,
    claim: tuple[Any, ...],
) -> tuple[uuid.UUID, ExtractionOutcome, str | None]:
    job_id = uuid.UUID(str(claim[0]))
    attempt_id = uuid.UUID(str(claim[1]))
    lease_token = uuid.UUID(str(claim[7]))
    connection = role_connection(database_url)
    try:
        store = PostgresExtractionStore(
            connection,
            cast(ObjectClient, object_client(settings)),
        )
        extraction_id, result = ExtractionJobHandler(store).handle(
            job_id,
            attempt_id,
            lease_token,
            payload_from_claim(claim),
        )
        return extraction_id, result.outcome, result.error_code
    finally:
        connection.close()


def extraction_jobs(
    worker: psycopg.Connection[Any],
    key: str,
    count: int,
    document_version_id: uuid.UUID,
    source_object_id: uuid.UUID,
    media_type: str,
    extractor_name: str,
    extractor_version: str,
) -> list[tuple[Any, ...]]:
    claims = []
    for index in range(count):
        job_id = enqueue(
            worker,
            "extract_document",
            {
                "document_version_id": str(document_version_id),
                "source_object_id": str(source_object_id),
                "media_type": media_type,
                "extractor_name": extractor_name,
                "extractor_version": extractor_version,
                "payload_schema_version": "extract.v1",
            },
            f"wp6-extract-{key}-{index}",
        )
        claims.append(claim_expected(worker, job_id))
    return claims


def main() -> None:
    settings = load_settings()
    database_url = os.environ["UAP_DATABASE_URL"]
    administrator = admin_connection(database_url)
    worker = role_connection(database_url)
    key = uuid.uuid4().hex
    try:
        source_id, _config_id, run_id = seed_source(administrator, worker, key)
        raw_client = cast(ObjectClient, object_client(settings))
        html_payload = (FIXTURE_DIR / "sample.html").read_bytes().replace(
            b'content="2026-08-19T10:00:00Z"', b'content="not-a-date"'
        )
        fixtures = (
            (
                "html",
                html_payload,
                "text/html; charset=utf-8",
                "html",
            ),
            ("pdf", (FIXTURE_DIR / "sample.pdf").read_bytes(), "application/pdf", "pdf"),
            ("vtt", (FIXTURE_DIR / "sample.vtt").read_bytes(), "text/vtt", "subtitle"),
            ("srt", (FIXTURE_DIR / "sample.srt").read_bytes(), "text/srt", "subtitle"),
            ("bad-pdf", b"%PDF-1.7\n%%EOF\n", "application/pdf", "pdf"),
        )
        seeded: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}
        for fixture_key, payload, media_type, artifact_kind in fixtures:
            seeded[fixture_key] = seed_document(
                administrator,
                worker,
                raw_client,
                source_id,
                run_id,
                payload,
                media_type,
                artifact_kind,
                f"{key}-{fixture_key}",
            )

        html_claim = extraction_jobs(
            worker,
            f"{key}-html",
            1,
            *seeded["html"],
            "text/html",
            "html_readable_text",
            "1.0.0",
        )[0]
        pdf_claim = extraction_jobs(
            worker,
            f"{key}-pdf",
            1,
            *seeded["pdf"],
            "application/pdf",
            "pdf_text",
            "1.0.0",
        )[0]
        vtt_claim = extraction_jobs(
            worker,
            f"{key}-vtt",
            1,
            *seeded["vtt"],
            "text/vtt",
            "subtitle_timeline_text",
            "1.0.0",
        )[0]
        srt_claims = extraction_jobs(
            worker,
            f"{key}-srt",
            2,
            *seeded["srt"],
            "text/srt",
            "subtitle_timeline_text",
            "1.0.0",
        )
        bad_pdf_claim = extraction_jobs(
            worker,
            f"{key}-bad-pdf",
            1,
            *seeded["bad-pdf"],
            "application/pdf",
            "pdf_text",
            "1.0.0",
        )[0]
        outcomes = [
            run_claimed(
                database_url,
                settings,
                html_claim,
            ),
            run_claimed(
                database_url,
                settings,
                pdf_claim,
            ),
            run_claimed(
                database_url,
                settings,
                vtt_claim,
            ),
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    run_claimed,
                    database_url,
                    settings,
                    claim,
                )
                for claim in srt_claims
            ]
            outcomes.extend(future.result() for future in futures)
        outcomes.append(
            run_claimed(
                database_url,
                settings,
                bad_pdf_claim,
            )
        )
        if len(outcomes) != 6 or any(
            outcome is not ExtractionOutcome.SUCCEEDED for _, outcome, _ in outcomes[:5]
        ):
            raise RuntimeError(f"successful extraction outcomes were unexpected: {outcomes}")
        if outcomes[-1][1] is not ExtractionOutcome.FAILED or outcomes[-1][2] != "invalid_pdf":
            raise RuntimeError(f"invalid PDF outcome was unexpected: {outcomes[-1]}")

        with administrator.cursor() as cursor:
            for fixture_key in ("html", "pdf", "vtt", "srt"):
                document_version_id, _source_object_id = seeded[fixture_key]
                cursor.execute(
                    """
                    SELECT count(*), count(DISTINCT text_object_id)
                      FROM core.extractions
                     WHERE document_version_id = %s AND outcome = 'succeeded'
                    """,
                    (document_version_id,),
                )
                count_row = cursor.fetchone()
                if count_row != (1, 1):
                    raise RuntimeError(
                        f"non-idempotent extraction rows for {fixture_key}: {count_row}"
                    )
                cursor.execute(
                    """
                    SELECT e.output_sha256, so.bucket_name, so.object_key, so.byte_length,
                           jsonb_array_length(e.location_map)
                      FROM core.extractions AS e
                      JOIN core.stored_objects AS so ON so.id = e.text_object_id
                     WHERE e.document_version_id = %s AND e.outcome = 'succeeded'
                    """,
                    (document_version_id,),
                )
                row = cursor.fetchone()
                if row is None or int(row[4]) == 0:
                    raise RuntimeError(f"missing extraction provenance for {fixture_key}")
                if fixture_key == "html":
                    cursor.execute(
                        "SELECT source_date FROM core.extractions WHERE document_version_id = %s",
                        (document_version_id,),
                    )
                    if cursor.fetchone() != (None,):
                        raise RuntimeError("invalid HTML source date was not ignored")
                data = read_verified_object(
                    raw_client,
                    str(row[1]),
                    str(row[2]),
                    str(row[0]),
                    int(row[3]),
                )
                if len(data) != int(row[3]):
                    raise RuntimeError(f"derived object verification failed for {fixture_key}")

            bad_document_version_id, _bad_source_object_id = seeded["bad-pdf"]
            cursor.execute(
                "SELECT outcome, error_code FROM core.extractions WHERE document_version_id = %s",
                (bad_document_version_id,),
            )
            if cursor.fetchone() != ("failed", "invalid_pdf"):
                raise RuntimeError("invalid PDF did not persist a structured failure")
            cursor.execute(
                "SELECT count(*) FROM core.extractions WHERE outcome = 'succeeded'"
            )
            total_row = cursor.fetchone()
            if total_row is None:
                raise RuntimeError("extraction success count query returned no row")
            total_successes = int(total_row[0])
        print(
            json.dumps(
                {"documents": len(seeded), "successful_extractions": total_successes},
                sort_keys=True,
            )
        )
    finally:
        worker.close()
        administrator.close()


if __name__ == "__main__":
    main()
