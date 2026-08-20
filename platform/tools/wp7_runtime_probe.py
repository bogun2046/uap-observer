"""Exercise WP7 model governance against real PostgreSQL and object storage."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from typing import Any, cast

import psycopg
from wp6_runtime_probe import (  # type: ignore[import-not-found]
    extraction_jobs,
    object_client,
    role_connection,
    run_claimed,
    seed_document,
    seed_source,
)

from uap_platform.config import Settings, load_settings
from uap_platform.model_governance import (
    ModelJobHandler,
    ModelRunStatus,
    ModelTaskType,
    PromptVersion,
    ProviderRegistry,
    StaticProvider,
    ValidationStatus,
)
from uap_platform.model_governance.contracts import json_sha256
from uap_platform.model_governance.persistence import PostgresModelGovernanceStore
from uap_platform.model_governance.workflow import payload_from_claim
from uap_platform.object_registry import ObjectClient


def admin_connection(admin_url: str) -> psycopg.Connection[Any]:
    return psycopg.connect(admin_url.replace("postgresql+psycopg://", "postgresql://"))


def enqueue(
    connection: psycopg.Connection[Any],
    payload: Mapping[str, object],
    key: str,
    *,
    max_attempts: int = 1,
) -> uuid.UUID:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT ops.enqueue_job(
                'analyze_document', %s::jsonb, 'model.v1', %s, 32767::smallint,
                'epoch'::timestamptz, %s, 60
            )
            """,
            (json.dumps(payload, sort_keys=True), key, max_attempts),
        )
        row = cursor.fetchone()
    connection.commit()
    if row is None:
        raise RuntimeError("model probe enqueue returned no job id")
    return uuid.UUID(str(row[0]))


def claim(
    connection: psycopg.Connection[Any], expected_job_id: uuid.UUID
) -> tuple[Any, ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT * FROM ops.claim_job(
                'worker', 'wp7-runtime-worker', ARRAY['analyze_document'], 60)
            """
        )
        row = cursor.fetchone()
    connection.commit()
    if row is None or uuid.UUID(str(row[0])) != expected_job_id:
        actual = None if row is None else str(row[0])
        raise RuntimeError(f"model probe claimed {actual}; expected {expected_job_id}")
    return cast(tuple[Any, ...], row)


def prompt_version(version: str, *, active: bool = True) -> PromptVersion:
    content = {
        "task_type": ModelTaskType.SUMMARY.value,
        "version": version,
        "system_template": "Return strict JSON.",
        "user_template": "Summarize the supplied text.",
        "output_schema": {"type": "object", "required": ["summary", "bullets"]},
    }
    return PromptVersion(
        id=uuid.uuid4(),
        task_type=ModelTaskType.SUMMARY,
        version=version,
        system_template=str(content["system_template"]),
        user_template=str(content["user_template"]),
        output_schema=cast(dict[str, object], content["output_schema"]),
        content_sha256=json_sha256(content),
        active=active,
    )


def run_model_job(
    connection: psycopg.Connection[Any],
    settings: Settings,
    job_id: uuid.UUID,
    provider_response: dict[str, object],
) -> uuid.UUID:
    claimed = claim(connection, job_id)
    handler = ModelJobHandler(
        PostgresModelGovernanceStore(
            connection,
            cast(ObjectClient, object_client(settings)),
        ),
        ProviderRegistry({"static": StaticProvider(provider_response)}),
    )
    return handler.handle(
        job_id,
        uuid.UUID(str(claimed[1])),
        uuid.UUID(str(claimed[7])),
        payload_from_claim(claimed),
    )


def main() -> None:
    settings = load_settings()
    database_url = os.environ["UAP_DATABASE_URL"]
    administrator = admin_connection(database_url)
    worker = role_connection(database_url)
    key = uuid.uuid4().hex
    try:
        source_id, _config_id, source_run_id = seed_source(administrator, worker, key)
        raw = (
            b"<html><body><p>WP7 model governance source text with a stable extraction "
            b"result.</p></body></html>"
        )
        document_version_id, _source_object_id = seed_document(
            administrator,
            worker,
            cast(ObjectClient, object_client(settings)),
            source_id,
            source_run_id,
            raw,
            "text/html",
            "html",
            f"wp7-{key}",
        )
        extraction_claim = extraction_jobs(
            worker,
            key,
            1,
            document_version_id,
            _source_object_id,
            "text/html",
            "html_readable_text",
            "1.0.0",
        )[0]
        run_claimed(database_url, settings, extraction_claim)

        principal_id = uuid.uuid4()
        with administrator.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO audit.principals
                    (id, principal_type, service_name, display_name)
                VALUES (%s, 'service', %s, 'WP7 runtime probe')
                """,
                (principal_id, f"wp7-runtime-{key}"),
            )
        administrator.commit()

        first_prompt = prompt_version("1.0.0")
        PostgresModelGovernanceStore(
            worker,
            cast(ObjectClient, object_client(settings)),
        ).create_prompt_version(first_prompt, principal_id)
        valid_payload = {
            "document_version_id": str(document_version_id),
            "prompt_version_id": str(first_prompt.id),
            "task_type": "summary",
            "provider": "static",
            "model": "probe-model-v1",
            "payload_schema_version": "model.v1",
        }
        valid_job = enqueue(worker, valid_payload, f"wp7-valid-{key}")
        first_run_id = run_model_job(
            worker,
            settings,
            valid_job,
            {"summary": "Stable summary", "bullets": ["one"]},
        )

        invalid_job = enqueue(
            worker,
            valid_payload | {"model": "probe-invalid"},
            f"wp7-invalid-{key}",
        )
        invalid_run_id = run_model_job(
            worker,
            settings,
            invalid_job,
            {"summary": "Missing bullets"},
        )

        with worker.cursor() as cursor:
            cursor.execute(
                """
                SELECT mr.status, ar.validation_status, j.status
                  FROM ops.model_runs AS mr
                  JOIN core.analysis_results AS ar ON ar.model_run_id = mr.id
                  JOIN ops.jobs AS j ON j.id = (
                      SELECT ja.job_id FROM ops.job_attempts AS ja WHERE ja.id = mr.job_attempt_id
                  )
                 WHERE mr.id IN (%s, %s)
                 ORDER BY mr.id
                """,
                (first_run_id, invalid_run_id),
            )
            rows = cursor.fetchall()
            cursor.execute(
                "SELECT count(*) FROM core.analysis_results WHERE model_run_id = %s",
                (first_run_id,),
            )
            first_result_count = cursor.fetchone()
        if len(rows) != 2 or first_result_count != (1,):
            raise RuntimeError(f"unexpected model governance rows: {rows}, {first_result_count}")
        if not any(
            row[0] == ModelRunStatus.SUCCEEDED.value
            and row[1] == ValidationStatus.VALID.value
            and row[2] == "succeeded"
            for row in rows
        ):
            raise RuntimeError(f"valid model result did not close successfully: {rows}")
        if not any(
            row[0] == ModelRunStatus.INVALID.value
            and row[1] == ValidationStatus.INVALID.value
            and row[2] == "dead"
            for row in rows
        ):
            raise RuntimeError(f"invalid model result did not enter dead state: {rows}")

        print(json.dumps({"model_runs": len(rows), "valid_result": True, "invalid_result": True}))
    finally:
        worker.close()
        administrator.close()


if __name__ == "__main__":
    main()
