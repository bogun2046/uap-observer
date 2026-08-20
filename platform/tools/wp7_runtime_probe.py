"""Exercise WP7 model governance against real PostgreSQL and object storage."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Mapping
from typing import Any, cast

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from wp6_runtime_probe import (  # type: ignore[import-not-found]
    extraction_jobs,
    object_client,
    role_connection,
    run_claimed,
    seed_document,
    seed_source,
)

import uap_platform.model_governance.workflow as workflow_module
from uap_platform.config import Settings, load_settings
from uap_platform.model_governance import (
    ModelJobHandler,
    ModelRunStatus,
    ModelTaskType,
    PromptVersion,
    ProviderError,
    ProviderRegistry,
    ProviderResponse,
    StaticProvider,
    ValidationStatus,
)
from uap_platform.model_governance.contracts import (
    MODEL_MAX_CALLS_PER_SEMANTIC_KEY,
    MODEL_MAX_COST_MINOR_UNITS,
    json_sha256,
)
from uap_platform.model_governance.persistence import PostgresModelGovernanceStore
from uap_platform.model_governance.workflow import payload_from_claim
from uap_platform.object_registry import ObjectClient


def admin_connection(admin_url: str) -> psycopg.Connection[Any]:
    return psycopg.connect(admin_url.replace("postgresql+psycopg://", "postgresql://"))


def model_governance_connection(admin_url: str) -> psycopg.Connection[Any]:
    params = conninfo_to_dict(admin_url.replace("postgresql+psycopg://", "postgresql://"))
    params.pop("user", None)
    params.pop("password", None)
    base = make_conninfo(**params)  # type: ignore[arg-type]
    return psycopg.connect(
        make_conninfo(
            base,
            user="uap_model_governance",
            password=os.environ["UAP_MODEL_GOVERNANCE_PASSWORD"],
        )
    )


def public_reader_connection(admin_url: str) -> psycopg.Connection[Any]:
    params = conninfo_to_dict(admin_url.replace("postgresql+psycopg://", "postgresql://"))
    params.pop("user", None)
    params.pop("password", None)
    base = make_conninfo(**params)  # type: ignore[arg-type]
    return psycopg.connect(
        make_conninfo(
            base,
            user="uap_public_reader",
            password=os.environ["UAP_PUBLIC_READER_PASSWORD"],
        )
    )


class SequenceProvider:
    name = "static"

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = outcomes
        self.calls = 0

    def complete(self, *_args: object) -> ProviderResponse:
        if self.calls >= len(self._outcomes):
            raise RuntimeError("runtime probe provider sequence exhausted")
        outcome = self._outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return cast(ProviderResponse, outcome)


class FailIfCalledProvider:
    name = "static"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *_args: object) -> ProviderResponse:
        self.calls += 1
        raise RuntimeError("duplicate semantic request invoked Provider")


class SlowProvider:
    name = "static"

    def complete(self, *_args: object) -> ProviderResponse:
        time.sleep(0.05)
        return ProviderResponse(
            structured={"summary": "late", "bullets": ["late"]},
            raw_response=b"{}",
            provider_response_id="late-response",
            input_tokens=1,
            output_tokens=1,
            cost_minor_units=1,
            currency="USD",
        )


def valid_response(response_id: str, *, cost_minor_units: int = 1) -> ProviderResponse:
    return ProviderResponse(
        structured={"summary": "Stable summary", "bullets": ["one"]},
        raw_response=b'{"summary":"Stable summary","bullets":["one"]}',
        provider_response_id=response_id,
        input_tokens=3,
        output_tokens=2,
        cost_minor_units=cost_minor_units,
        currency="USD",
    )


def job_status(connection: psycopg.Connection[Any], job_id: uuid.UUID) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT status::text FROM ops.jobs WHERE id = %s", (job_id,))
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError(f"job {job_id} was not found")
    return str(row[0])


def model_runs_for_job(
    connection: psycopg.Connection[Any], job_id: uuid.UUID
) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT mr.id, mr.status::text, mr.error_code, ja.http_status,
                   mr.provider_response_id, mr.input_tokens, mr.output_tokens,
                   mr.cost_minor_units, mr.currency, mr.started_at, mr.finished_at
              FROM ops.model_runs AS mr
              JOIN ops.job_attempts AS ja ON ja.id = mr.job_attempt_id
             WHERE ja.job_id = %s
             ORDER BY ja.attempt_no
            """,
            (job_id,),
        )
        return cast(list[tuple[Any, ...]], cursor.fetchall())


def make_retry_available(
    administrator: psycopg.Connection[Any], job_id: uuid.UUID
) -> None:
    with administrator.cursor() as cursor:
        cursor.execute(
            "UPDATE ops.jobs SET available_at = clock_timestamp() WHERE id = %s",
            (job_id,),
        )
    administrator.commit()


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
    worker: psycopg.Connection[Any],
    governance: psycopg.Connection[Any],
    settings: Settings,
    job_id: uuid.UUID,
    provider_response: dict[str, object] | None = None,
    provider: object | None = None,
) -> uuid.UUID:
    claimed = claim(worker, job_id)
    selected_provider = provider or StaticProvider(provider_response or {})
    handler = ModelJobHandler(
        PostgresModelGovernanceStore(
            governance,
            cast(ObjectClient, object_client(settings)),
        ),
        ProviderRegistry({"static": cast(Any, selected_provider)}),
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
    governance = model_governance_connection(database_url)
    public_reader = public_reader_connection(database_url)
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
            governance,
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
            governance,
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
            governance,
            settings,
            invalid_job,
            {"summary": "Missing bullets"},
        )

        with governance.cursor() as cursor:
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

        duplicate_provider = FailIfCalledProvider()
        duplicate_job = enqueue(worker, valid_payload, f"wp7-duplicate-{key}")
        duplicate_run_id = run_model_job(
            worker,
            governance,
            settings,
            duplicate_job,
            provider=duplicate_provider,
        )
        if (
            duplicate_run_id != first_run_id
            or job_status(administrator, duplicate_job) != "succeeded"
        ):
            raise RuntimeError("semantic duplicate was not closed without a Provider call")

        with administrator.cursor() as cursor:
            cursor.execute(
                "UPDATE ops.prompt_versions SET active = false WHERE id = %s",
                (first_prompt.id,),
            )
        administrator.commit()
        second_prompt = prompt_version("2.0.0")
        PostgresModelGovernanceStore(
            governance,
            cast(ObjectClient, object_client(settings)),
        ).create_prompt_version(second_prompt, principal_id)
        version_job = enqueue(
            worker,
            valid_payload | {"prompt_version_id": str(second_prompt.id)},
            f"wp7-prompt-version-{key}",
        )
        version_run_id = run_model_job(
            worker,
            governance,
            settings,
            version_job,
            provider=StaticProvider({"summary": "Version two", "bullets": ["two"]}),
        )
        if version_run_id == first_run_id or job_status(administrator, version_job) != "succeeded":
            raise RuntimeError("Prompt version change did not append a new model run")
        valid_payload = valid_payload | {"prompt_version_id": str(second_prompt.id)}

        for http_status in (401, 403):
            auth_job = enqueue(
                worker,
                valid_payload | {"model": f"auth-{http_status}"},
                f"wp7-auth-{http_status}-{key}",
                max_attempts=1,
            )
            auth_provider = SequenceProvider(
                [
                    ProviderError(
                        "auth_failed",
                        "secret body must never become an error summary",
                        http_status=http_status,
                        retryable=True,
                        raw_response=b"secret body api_key=hidden",
                    )
                ]
            )
            run_model_job(
                worker,
                governance,
                settings,
                auth_job,
                provider=auth_provider,
            )
            auth_runs = model_runs_for_job(administrator, auth_job)
            if (
                job_status(administrator, auth_job) != "dead"
                or len(auth_runs) != 1
                or auth_runs[0][2] != "auth_failed"
                or auth_runs[0][3] != http_status
            ):
                raise RuntimeError(f"authentication failure {http_status} was not terminal")

        retry_job = enqueue(
            worker,
            valid_payload | {"model": "retry-model"},
            f"wp7-retry-{key}",
            max_attempts=2,
        )
        retry_provider_first = SequenceProvider(
            [ProviderError("rate_limited", "ignored", http_status=429, retryable=False)]
        )
        run_model_job(worker, governance, settings, retry_job, provider=retry_provider_first)
        if job_status(administrator, retry_job) != "retry_wait":
            raise RuntimeError("429 did not enter retry_wait")
        make_retry_available(administrator, retry_job)
        retry_run_id = run_model_job(
            worker,
            governance,
            settings,
            retry_job,
            provider=SequenceProvider([valid_response("retry-success")]),
        )
        retry_runs = model_runs_for_job(administrator, retry_job)
        if (
            job_status(administrator, retry_job) != "succeeded"
            or len(retry_runs) != 2
            or retry_runs[-1][0] != retry_run_id
            or retry_runs[0][4] != "error:rate_limited:429"
            or retry_runs[1][4] != "retry-success"
        ):
            raise RuntimeError("429 retry did not call Provider again and succeed")

        upstream_job = enqueue(
            worker,
            valid_payload | {"model": "upstream-503"},
            f"wp7-upstream-{key}",
            max_attempts=1,
        )
        run_model_job(
            worker,
            governance,
            settings,
            upstream_job,
            provider=SequenceProvider(
                [ProviderError("upstream", "ignored", http_status=503, retryable=False)]
            ),
        )
        if job_status(administrator, upstream_job) != "dead":
            raise RuntimeError("5xx failure did not close as dead at max attempts")

        workflow_globals = vars(workflow_module)
        previous_timeout = float(workflow_globals["MODEL_PROVIDER_TIMEOUT_SECONDS"])
        workflow_globals["MODEL_PROVIDER_TIMEOUT_SECONDS"] = 0.001
        try:
            timeout_job = enqueue(
                worker,
                valid_payload | {"model": "timeout-model"},
                f"wp7-timeout-{key}",
                max_attempts=1,
            )
            run_model_job(
                worker,
                governance,
                settings,
                timeout_job,
                provider=SlowProvider(),
            )
        finally:
            workflow_globals["MODEL_PROVIDER_TIMEOUT_SECONDS"] = previous_timeout
        timeout_runs = model_runs_for_job(administrator, timeout_job)
        if (
            job_status(administrator, timeout_job) != "dead"
            or len(timeout_runs) != 1
            or timeout_runs[0][2] != "timeout"
        ):
            raise RuntimeError("Provider timeout did not produce a terminal model failure")

        budget_job = enqueue(
            worker,
            valid_payload | {"model": "budget-model"},
            f"wp7-budget-{key}",
            max_attempts=2,
        )
        run_model_job(
            worker,
            governance,
            settings,
            budget_job,
            provider=SequenceProvider(
                [valid_response("over-budget", cost_minor_units=MODEL_MAX_COST_MINOR_UNITS + 1)]
            ),
        )
        budget_runs = model_runs_for_job(administrator, budget_job)
        if (
            job_status(administrator, budget_job) != "dead"
            or len(budget_runs) != 1
            or budget_runs[0][2] != "model_cost_budget_exceeded"
            or budget_runs[0][7] != MODEL_MAX_COST_MINOR_UNITS + 1
        ):
            raise RuntimeError("cost budget was not enforced at the Provider boundary")

        output_limit_job = enqueue(
            worker,
            valid_payload | {"model": "output-limit-model"},
            f"wp7-output-limit-{key}",
            max_attempts=2,
        )
        oversized = valid_response("oversized-output")
        oversized = ProviderResponse(
            structured=oversized.structured,
            raw_response=b"x" * 2_000_001,
            provider_response_id=oversized.provider_response_id,
            input_tokens=oversized.input_tokens,
            output_tokens=oversized.output_tokens,
            cost_minor_units=oversized.cost_minor_units,
            currency=oversized.currency,
        )
        run_model_job(
            worker,
            governance,
            settings,
            output_limit_job,
            provider=SequenceProvider([oversized]),
        )
        output_runs = model_runs_for_job(administrator, output_limit_job)
        if (
            job_status(administrator, output_limit_job) != "dead"
            or len(output_runs) != 1
            or output_runs[0][2] != "model_output_too_large"
        ):
            raise RuntimeError("output size limit was not enforced")

        call_budget_job = enqueue(
            worker,
            valid_payload | {"model": "call-budget-model"},
            f"wp7-call-budget-{key}",
            max_attempts=MODEL_MAX_CALLS_PER_SEMANTIC_KEY + 2,
        )
        call_budget_provider = SequenceProvider(
            [
                ProviderError("rate_limited", "ignored", http_status=429),
                ProviderError("rate_limited", "ignored", http_status=429),
                ProviderError("rate_limited", "ignored", http_status=429),
                valid_response("must-not-be-called"),
            ]
        )
        for _ in range(MODEL_MAX_CALLS_PER_SEMANTIC_KEY):
            run_model_job(
                worker,
                governance,
                settings,
                call_budget_job,
                provider=call_budget_provider,
            )
            if job_status(administrator, call_budget_job) != "retry_wait":
                raise RuntimeError("call budget setup did not enter retry_wait")
            make_retry_available(administrator, call_budget_job)
        run_model_job(
            worker,
            governance,
            settings,
            call_budget_job,
            provider=call_budget_provider,
        )
        call_budget_runs = model_runs_for_job(administrator, call_budget_job)
        if (
            job_status(administrator, call_budget_job) != "dead"
            or len(call_budget_runs) != MODEL_MAX_CALLS_PER_SEMANTIC_KEY + 1
            or call_budget_runs[-1][2] != "model_call_budget_exceeded"
            or call_budget_runs[-1][4] != "not-called:model_call_budget_exceeded"
        ):
            raise RuntimeError("model call budget was not enforced before Provider invocation")

        lease_job = enqueue(
            worker,
            valid_payload | {"model": "lease-model"},
            f"wp7-lease-{key}",
            max_attempts=1,
        )
        lease_claim = claim(worker, lease_job)
        lease_handler = ModelJobHandler(
            PostgresModelGovernanceStore(
                governance,
                cast(ObjectClient, object_client(settings)),
            ),
            ProviderRegistry({"static": StaticProvider({"summary": "late", "bullets": ["one"]})}),
        )
        try:
            lease_handler.handle(
                lease_job,
                uuid.UUID(str(lease_claim[1])),
                uuid.uuid4(),
                payload_from_claim(lease_claim),
            )
        except psycopg.Error as error:
            if error.sqlstate != "40001":
                raise
        else:
            raise RuntimeError("invalid lease was accepted by model governance")
        if job_status(administrator, lease_job) != "running" or model_runs_for_job(
            administrator, lease_job
        ):
            raise RuntimeError("invalid lease changed model governance state")

        for statement in (
            "SELECT id, system_template FROM ops.prompt_versions LIMIT 1",
            "SELECT id, response_object_id FROM ops.model_runs LIMIT 1",
        ):
            try:
                public_reader.execute(statement)
            except psycopg.errors.InsufficientPrivilege:
                public_reader.rollback()
            else:
                public_reader.rollback()
                raise RuntimeError("public reader can access internal model governance data")

        try:
            with worker.transaction():
                with worker.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO ops.model_runs (id) VALUES (%s)",
                        (uuid.uuid4(),),
                    )
        except psycopg.errors.InsufficientPrivilege:
            pass
        else:
            raise RuntimeError("ordinary worker can write model governance tables")

        try:
            with governance.transaction():
                with governance.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO core.stored_objects (
                            id, storage_domain, bucket_name, object_key,
                            content_sha256, byte_length, media_type, verified_at
                        ) VALUES (%s, 'raw'::core.storage_domain, 'raw', %s, %s, 0,
                                  'application/octet-stream', clock_timestamp())
                        """,
                        (
                            uuid.uuid4(),
                            f"wp7-illegal-raw-{key}",
                            "a" * 64,
                        ),
                    )
        except psycopg.errors.InsufficientPrivilege:
            pass
        else:
            raise RuntimeError("model governance role can write a non-model_io object")

        print(
            json.dumps(
                {
                    "model_runs": len(rows),
                    "valid_result": True,
                    "invalid_result": True,
                    "semantic_duplicate_without_provider_call": True,
                    "prompt_version_append": version_run_id != first_run_id,
                    "auth_failures_terminal": True,
                    "rate_limit_retry": True,
                    "upstream_failure_closed": True,
                    "timeout_closed": True,
                    "cost_budget_closed": True,
                    "output_limit_closed": True,
                    "call_budget_provider_attempt_records": MODEL_MAX_CALLS_PER_SEMANTIC_KEY,
                    "call_budget_model_runs": len(call_budget_runs),
                    "invalid_lease_rolled_back": True,
                    "public_reader_internal_access_denied": True,
                    "model_governance_role": True,
                    "worker_model_write_denied": True,
                    "model_governance_cross_domain_write_denied": True,
                }
            )
        )
    finally:
        public_reader.close()
        governance.close()
        worker.close()
        administrator.close()


if __name__ == "__main__":
    main()
