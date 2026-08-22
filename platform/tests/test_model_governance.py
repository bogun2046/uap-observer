from __future__ import annotations

import json
import time
import uuid
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

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
    build_model_request,
    payload_from_claim,
    validate_output,
)
from uap_platform.model_governance.contracts import ModelExecution, json_sha256
from uap_platform.model_governance.schemas import schema_json_schema
from uap_platform.object_registry import RegisteredObject, StorageDomain

DOCUMENT_ID = uuid.UUID("00000000-0000-7000-8000-000000000701")
PROMPT_ID = uuid.UUID("00000000-0000-7000-8000-000000000702")
ATTEMPT_ID = uuid.UUID("00000000-0000-7000-8000-000000000703")
JOB_ID = uuid.UUID("00000000-0000-7000-8000-000000000704")
TOKEN = uuid.UUID("00000000-0000-7000-8000-000000000705")


def prompt(task_type: ModelTaskType = ModelTaskType.SUMMARY) -> PromptVersion:
    values = {
        "task_type": task_type.value,
        "version": "1.0.0",
        "system_template": "Return JSON.",
        "user_template": "Summarize the supplied text.",
        "output_schema": {"type": "object"},
    }
    return PromptVersion(
        id=PROMPT_ID,
        task_type=task_type,
        version="1.0.0",
        system_template="Return JSON.",
        user_template="Summarize the supplied text.",
        output_schema={"type": "object"},
        content_sha256=json_sha256(values),
        active=True,
    )


def model_payload(task_type: str = "summary") -> dict[str, object]:
    return {
        "document_version_id": str(DOCUMENT_ID),
        "prompt_version_id": str(PROMPT_ID),
        "task_type": task_type,
        "provider": "static",
        "model": "test-model",
        "payload_schema_version": "model.v1",
    }


class FakeStore:
    def __init__(self) -> None:
        self.finished: list[tuple[object, ...]] = []
        self.executions: list[ModelExecution] = []
        self.existing: tuple[uuid.UUID, ModelRunStatus] | None = None
        self.call_count = 0
        self.cost_minor_units = 0

    def load_document_input(self, document_version_id: uuid.UUID) -> str:
        assert document_version_id == DOCUMENT_ID
        return "A source document with enough text for the model."

    def load_prompt(self, prompt_version_id: uuid.UUID, task_type: ModelTaskType) -> PromptVersion:
        assert prompt_version_id == PROMPT_ID
        return prompt(task_type)

    def existing_model_run(
        self, idempotency_key: str
    ) -> tuple[uuid.UUID, ModelRunStatus] | None:
        del idempotency_key
        return self.existing

    def acquire_semantic_request(
        self, semantic_idempotency_key: str
    ) -> tuple[uuid.UUID, ModelRunStatus] | None:
        del semantic_idempotency_key
        return self.existing

    def model_call_count(self, semantic_idempotency_key: str) -> int:
        del semantic_idempotency_key
        return self.call_count

    def accumulated_cost_minor_units(self, semantic_idempotency_key: str) -> int:
        del semantic_idempotency_key
        return self.cost_minor_units

    def persist_and_finish_job(
        self,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        token: uuid.UUID,
        execution: ModelExecution,
    ) -> uuid.UUID:
        self.finished.append((job_id, attempt_id, token))
        self.executions.append(execution)
        run_id = uuid.UUID("00000000-0000-7000-8000-000000000706")
        if execution.status is ModelRunStatus.SUCCEEDED:
            self.existing = (run_id, ModelRunStatus.SUCCEEDED)
        return run_id

    def finish_job_only(self, *args: object, **kwargs: object) -> None:
        self.finished.append((*args, kwargs))

    def finish_existing_job(self, *args: object) -> None:
        self.finished.append(args)


def test_build_model_request_is_strict_and_does_not_hash_prompt_text() -> None:
    request = build_model_request(
        model_payload(),
        job_id=JOB_ID,
        job_attempt_id=ATTEMPT_ID,
        input_text="source text",
    )

    assert len(request.input_sha256) == 64
    assert "input_text" not in request.safe_record()
    with pytest.raises(ValueError, match="unknown fields"):
        build_model_request(
            {**model_payload(), "unexpected": True},
            job_id=JOB_ID,
            job_attempt_id=ATTEMPT_ID,
            input_text="source text",
        )


def test_semantic_idempotency_ignores_job_and_attempt_but_tracks_versions() -> None:
    first = build_model_request(
        model_payload(),
        job_id=JOB_ID,
        job_attempt_id=ATTEMPT_ID,
        input_text="source text",
    )
    second = build_model_request(
        model_payload(),
        job_id=uuid.uuid4(),
        job_attempt_id=uuid.uuid4(),
        input_text="source text",
    )
    changed_model = build_model_request(
        {**model_payload(), "model": "another-model"},
        job_id=JOB_ID,
        job_attempt_id=ATTEMPT_ID,
        input_text="source text",
    )
    assert first.semantic_idempotency_key == second.semantic_idempotency_key
    assert first.idempotency_key != second.idempotency_key
    assert first.semantic_idempotency_key != changed_model.semantic_idempotency_key


def test_claim_output_requires_evidence_and_rejects_extra_fields() -> None:
    valid, errors = validate_output(
        ModelTaskType.CLAIM_EXTRACTION,
        {
            "claims": [
                {
                    "claim": "A claim",
                    "evidence": [{"locator_type": "text", "start": 0, "end": 7}],
                }
            ]
        },
    )
    assert errors == ()
    assert valid is not None

    invalid, errors = validate_output(
        ModelTaskType.CLAIM_EXTRACTION,
        {"claims": [{"claim": "No evidence", "evidence": []}]},
    )
    assert invalid is None
    assert errors

    invalid, errors = validate_output(
        ModelTaskType.SUMMARY,
        {"summary": "ok", "bullets": ["one"], "raw_response": "secret"},
    )
    assert invalid is None
    assert any(item["location"] == "raw_response" for item in errors)


def test_handler_persists_valid_result_without_logging_input() -> None:
    store = FakeStore()
    handler = ModelJobHandler(
        cast(Any, store),
        ProviderRegistry(
            {
                "static": StaticProvider(
                    {"summary": "A safe summary", "bullets": ["one"]}
                )
            }
        ),
    )

    run_id = handler.handle(JOB_ID, ATTEMPT_ID, TOKEN, model_payload())

    assert run_id.int != 0
    assert store.executions[0].status is ModelRunStatus.SUCCEEDED
    assert store.executions[0].validation_status is ValidationStatus.VALID
    assert store.executions[0].request.input_text not in json.dumps(
        store.executions[0].request.safe_record()
    )


def test_handler_persists_invalid_schema_as_invalid_result() -> None:
    store = FakeStore()
    handler = ModelJobHandler(
        cast(Any, store),
        ProviderRegistry({"static": StaticProvider({"summary": "missing bullets"})}),
    )

    handler.handle(JOB_ID, ATTEMPT_ID, TOKEN, model_payload())

    execution = store.executions[0]
    assert execution.status is ModelRunStatus.INVALID
    assert execution.validation_status is ValidationStatus.INVALID
    assert execution.error_code == "schema_validation_failed"


class FailingProvider:
    name = "failing"

    def complete(self, *_args: object) -> Any:
        raise ProviderError(
            "rate_limited",
            "provider returned 429",
            http_status=429,
            retryable=True,
        )


class SequenceProvider:
    name = "sequence"

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def complete(self, *_args: object) -> ProviderResponse:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


def test_provider_rate_limit_maps_to_retryable_failure() -> None:
    store = FakeStore()
    handler = ModelJobHandler(
        cast(Any, store), ProviderRegistry({"static": FailingProvider()})
    )
    payload = {**model_payload(), "provider": "static"}

    handler.handle(JOB_ID, ATTEMPT_ID, TOKEN, payload)

    execution = store.executions[0]
    assert execution.status is ModelRunStatus.FAILED
    assert execution.retryable is True
    assert execution.error_code == "rate_limited"
    assert execution.http_status == 429


def test_retryable_failure_is_not_reused_and_next_attempt_calls_provider_again() -> None:
    store = FakeStore()
    ModelJobHandler(
        cast(Any, store), ProviderRegistry({"static": FailingProvider()})
    ).handle(JOB_ID, ATTEMPT_ID, TOKEN, {**model_payload(), "provider": "static"})
    ModelJobHandler(
        cast(Any, store),
        ProviderRegistry(
            {
                "static": StaticProvider({"summary": "ok", "bullets": ["one"]})
            }
        ),
    ).handle(
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        {**model_payload(), "provider": "static"},
    )
    assert [item.status for item in store.executions] == [
        ModelRunStatus.FAILED,
        ModelRunStatus.SUCCEEDED,
    ]
    assert store.executions[0].response is not None
    assert store.executions[0].response.provider_response_id == "error:rate_limited:429"
    assert store.executions[1].response is not None
    assert store.executions[1].response.provider_response_id == "static-response"


def test_authentication_errors_are_always_terminal() -> None:
    class AuthProvider:
        name = "auth"

        def complete(self, *_args: object) -> ProviderResponse:
            raise ProviderError("auth", "secret body", http_status=401, retryable=True)

    store = FakeStore()
    handler = ModelJobHandler(cast(Any, store), ProviderRegistry({"static": AuthProvider()}))
    handler.handle(JOB_ID, ATTEMPT_ID, TOKEN, model_payload())
    assert store.executions[0].retryable is False
    assert store.executions[0].error_summary == "model provider authentication failed"


def test_timeout_is_retryable_and_does_not_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowProvider:
        name = "slow"

        def complete(self, *_args: object) -> ProviderResponse:
            time.sleep(0.5)
            return ProviderResponse(
                structured={"summary": "ok", "bullets": ["one"]},
                raw_response=b"{}",
                provider_response_id="late",
                input_tokens=1,
                output_tokens=1,
                cost_minor_units=1,
                currency="USD",
            )

    from uap_platform.model_governance import workflow as workflow_module

    monkeypatch.setattr(workflow_module, "MODEL_PROVIDER_TIMEOUT_SECONDS", 0.001)
    store = FakeStore()
    handler = ModelJobHandler(cast(Any, store), ProviderRegistry({"static": SlowProvider()}))
    started = time.monotonic()
    handler.handle(JOB_ID, ATTEMPT_ID, TOKEN, model_payload())
    assert time.monotonic() - started < 0.25
    assert store.executions[0].error_code == "timeout"
    assert store.executions[0].retryable is True


def test_call_budget_is_checked_before_provider() -> None:
    provider = SequenceProvider([])
    store = FakeStore()
    store.call_count = 3
    handler = ModelJobHandler(cast(Any, store), ProviderRegistry({"static": provider}))
    handler.handle(JOB_ID, ATTEMPT_ID, TOKEN, model_payload())
    assert provider.calls == 0
    assert store.executions[0].error_code == "model_call_budget_exceeded"


def test_contract_boundaries_reject_unsafe_values() -> None:
    with pytest.raises(ValueError, match="prompt version is required"):
        PromptVersion(
            id=PROMPT_ID,
            task_type=ModelTaskType.SUMMARY,
            version=" ",
            system_template="system",
            user_template="user",
            output_schema={},
            content_sha256="0" * 64,
            active=True,
        )
    with pytest.raises(ValueError, match="prompt content hash"):
        PromptVersion(
            id=PROMPT_ID,
            task_type=ModelTaskType.SUMMARY,
            version="1",
            system_template="system",
            user_template="user",
            output_schema={},
            content_sha256="not-a-hash",
            active=True,
        )
    with pytest.raises(ValueError, match="model input text"):
        build_model_request(
            model_payload(),
            job_id=JOB_ID,
            job_attempt_id=ATTEMPT_ID,
            input_text=" ",
        )
    with pytest.raises(ValueError, match="unsupported model payload"):
        build_model_request(
            {**model_payload(), "payload_schema_version": "model.v0"},
            job_id=JOB_ID,
            job_attempt_id=ATTEMPT_ID,
            input_text="source text",
        )
    with pytest.raises(ValueError, match="unsupported model task"):
        build_model_request(
            {**model_payload(), "task_type": "unknown"},
            job_id=JOB_ID,
            job_attempt_id=ATTEMPT_ID,
            input_text="source text",
        )


def test_provider_and_schema_variants_are_explicit() -> None:
    static = StaticProvider({"summary": "ok", "bullets": ["one"]})
    response = static.complete(
        build_model_request(
            model_payload(),
            job_id=JOB_ID,
            job_attempt_id=ATTEMPT_ID,
            input_text="source text",
        ),
        prompt(),
    )
    assert response.provider_response_id == "static-response"
    assert response.currency == "USD"
    with pytest.raises(ValueError, match="unsupported model provider"):
        ProviderRegistry({}).get("not-allowed")

    translated, errors = validate_output(
        ModelTaskType.TRANSLATION,
        {"language_code": "zh-CN", "text": "译文"},
    )
    assert translated == {"language_code": "zh-CN", "text": "译文"}
    assert errors == ()
    classified, errors = validate_output(
        ModelTaskType.CLASSIFICATION,
        {"labels": ["policy"], "confidence": 0.9},
    )
    assert classified is not None
    assert errors == ()
    entities, errors = validate_output(
        ModelTaskType.ENTITY_EXTRACTION,
        {
            "entities": [
                {
                    "name": "UAP",
                    "entity_type": "organization",
                    "evidence": [{"locator_type": "text", "start": 0, "end": 3}],
                }
            ]
        },
    )
    assert entities is not None
    assert errors == ()
    assert "properties" in schema_json_schema(ModelTaskType.SUMMARY)


def test_handler_duplicate_and_bad_payload_close_claim_without_provider() -> None:
    store = FakeStore()
    store.existing = (PROMPT_ID, ModelRunStatus.SUCCEEDED)
    handler = ModelJobHandler(
        cast(Any, store), ProviderRegistry({"static": StaticProvider({})})
    )
    assert handler.handle(JOB_ID, ATTEMPT_ID, TOKEN, model_payload()) == PROMPT_ID
    assert store.finished[-1] == (JOB_ID, ATTEMPT_ID, TOKEN)

    with pytest.raises(ValueError, match="model payload field document_version_id"):
        handler.handle(
            JOB_ID,
            ATTEMPT_ID,
            TOKEN,
            {**model_payload(), "document_version_id": "bad"},
        )
    assert isinstance(store.finished[-1], tuple)


def test_handler_maps_unknown_provider_and_unexpected_provider_failure() -> None:
    store = FakeStore()
    handler = ModelJobHandler(cast(Any, store), ProviderRegistry({}))
    with pytest.raises(ValueError, match="unsupported model provider"):
        handler.handle(JOB_ID, ATTEMPT_ID, TOKEN, model_payload())
    assert store.finished

    class BrokenProvider:
        name = "broken"

        def complete(self, *_args: object) -> Any:
            raise RuntimeError("secret provider detail")

    store = FakeStore()
    handler = ModelJobHandler(
        cast(Any, store), ProviderRegistry({"broken": BrokenProvider()})
    )
    handler.handle(
        JOB_ID,
        ATTEMPT_ID,
        TOKEN,
        {**model_payload(), "provider": "broken"},
    )
    assert store.executions[0].error_code == "provider_error"
    assert store.executions[0].error_summary == "model provider upstream failure"


def test_payload_claim_and_provider_error_preserve_safe_metadata() -> None:
    with pytest.raises(ValueError, match="JSON object payload"):
        payload_from_claim((1, 2, 3, "not-json", 5))
    with pytest.raises(ValueError, match="JSON object payload"):
        payload_from_claim((1, 2, 3))

    error = ProviderError(
        "bad_gateway",
        "  provider returned 502 with secret body  ",
        http_status=502,
        retryable=True,
        raw_response=b"{}",
        input_tokens=3,
        output_tokens=4,
        cost_minor_units=5,
        currency="USD",
    )
    store = FakeStore()
    handler = ModelJobHandler(cast(Any, store), ProviderRegistry({"static": StaticProvider({})}))
    execution = handler._provider_failure(
        build_model_request(
            model_payload(),
            job_id=JOB_ID,
            job_attempt_id=ATTEMPT_ID,
            input_text="source text",
        ),
        error,
    )
    assert execution.response is not None
    assert execution.error_summary == "model provider upstream failure"
    assert "secret body" not in (execution.error_summary or "")

    oversized_error = ProviderError(
        "bad_gateway",
        "ignored",
        http_status=502,
        raw_response=b"x" * 100_000,
    )
    assert oversized_error.raw_response is not None
    assert len(oversized_error.raw_response) == 64_000


def _request() -> Any:
    return build_model_request(
        model_payload(),
        job_id=JOB_ID,
        job_attempt_id=ATTEMPT_ID,
        input_text="source text",
    )


def _registered(number: int, *, created: bool = True) -> RegisteredObject:
    return RegisteredObject(
        storage_domain=StorageDomain.MODEL_IO,
        bucket_name="model-io",
        object_key=f"model_io/{number}",
        content_sha256=f"{number:064x}"[-64:],
        byte_length=2,
        media_type="application/json",
        id=uuid.UUID(f"00000000-0000-7000-8000-0000000007{number:02d}"),
        reused=not created,
        created=created,
    )


def _connection(*fetches: object) -> Any:
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = list(fetches)
    return connection


def test_postgres_store_prompt_and_input_boundaries() -> None:
    from uap_platform.model_governance.persistence import PostgresModelGovernanceStore

    connection = _connection((PROMPT_ID,))
    store = PostgresModelGovernanceStore(cast(Any, connection), cast(Any, MagicMock()))
    assert store.create_prompt_version(prompt(), JOB_ID) == PROMPT_ID
    connection.commit.assert_called_once()

    connection = _connection(None)
    store = PostgresModelGovernanceStore(cast(Any, connection), cast(Any, MagicMock()))
    with pytest.raises(ValueError, match="missing, inactive"):
        store.load_prompt(PROMPT_ID, ModelTaskType.SUMMARY)

    connection = _connection(None)
    store = PostgresModelGovernanceStore(cast(Any, connection), cast(Any, MagicMock()))
    with pytest.raises(LookupError, match="successful extracted text"):
        store.load_document_input(DOCUMENT_ID)

    bad_hash = PromptVersion(
        id=PROMPT_ID,
        task_type=ModelTaskType.SUMMARY,
        version="1.0.0",
        system_template="Return JSON.",
        user_template="Summarize the supplied text.",
        output_schema={"type": "object"},
        content_sha256="0" * 64,
        active=True,
    )
    connection = _connection(None)
    store = PostgresModelGovernanceStore(cast(Any, connection), cast(Any, MagicMock()))
    with pytest.raises(ValueError, match="content hash"):
        store.create_prompt_version(bad_hash, JOB_ID)


def test_postgres_store_reads_verified_derived_text_and_rejects_bad_utf8() -> None:
    from uap_platform.model_governance.persistence import PostgresModelGovernanceStore

    data = b"derived model input"
    response = MagicMock()
    response.read.return_value = data
    client = MagicMock()
    client.get_object.return_value = response
    from uap_platform.object_registry import sha256_bytes

    connection = _connection()
    connection.cursor.return_value.__enter__.return_value.fetchone.side_effect = [
        ("derived", "derived/key", sha256_bytes(data), len(data))
    ]
    store = PostgresModelGovernanceStore(cast(Any, connection), cast(Any, client))
    assert store.load_document_input(DOCUMENT_ID) == "derived model input"
    response.read.return_value = b"\xff"
    connection.cursor.return_value.__enter__.return_value.fetchone.side_effect = [
        ("derived", "derived/key", sha256_bytes(b"\xff"), 1)
    ]
    with pytest.raises(RuntimeError, match="UTF-8"):
        store.load_document_input(DOCUMENT_ID)


def test_postgres_store_persists_success_and_closes_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from uap_platform.model_governance import persistence as persistence_module
    from uap_platform.model_governance.persistence import PostgresModelGovernanceStore

    request_object = _registered(1)
    response_object = _registered(2)
    monkeypatch.setattr(
        persistence_module,
        "store_and_register",
        MagicMock(side_effect=[request_object, response_object]),
    )
    connection = _connection(
        None,
        (uuid.UUID("00000000-0000-7000-8000-000000000799"),),
        ("succeeded",),
    )
    store = PostgresModelGovernanceStore(cast(Any, connection), cast(Any, MagicMock()))
    response = ProviderResponse(
        structured={"summary": "ok", "bullets": ["one"]},
        raw_response=b"{\"summary\":\"ok\"}",
        provider_response_id="provider-1",
        input_tokens=2,
        output_tokens=3,
        cost_minor_units=4,
        currency="USD",
    )
    execution = ModelExecution(
        request=_request(),
        status=ModelRunStatus.SUCCEEDED,
        validation_status=ValidationStatus.VALID,
        result={"summary": "ok", "bullets": ["one"]},
        response=response,
    )
    run_id = store.persist_and_finish_job(JOB_ID, ATTEMPT_ID, TOKEN, execution)
    assert run_id.int != 0
    connection.commit.assert_called_once()
    assert connection.rollback.call_count == 0


def test_postgres_store_persists_invalid_and_failure_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from uap_platform.model_governance import persistence as persistence_module
    from uap_platform.model_governance.persistence import PostgresModelGovernanceStore

    monkeypatch.setattr(
        persistence_module,
        "store_and_register",
        MagicMock(side_effect=[_registered(3)]),
    )
    connection = _connection(
        None,
        (uuid.UUID("00000000-0000-7000-8000-000000000798"),),
        ("dead",),
    )
    store = PostgresModelGovernanceStore(cast(Any, connection), cast(Any, MagicMock()))
    invalid = ModelExecution(
        request=_request(),
        status=ModelRunStatus.INVALID,
        validation_status=ValidationStatus.INVALID,
        validation_errors=({"location": "summary"},),
        error_code="schema_validation_failed",
        error_summary="invalid output",
        http_status=422,
    )
    assert store.persist_and_finish_job(JOB_ID, ATTEMPT_ID, TOKEN, invalid).int != 0

    monkeypatch.setattr(
        persistence_module,
        "store_and_register",
        MagicMock(side_effect=[_registered(4)]),
    )
    connection = _connection(
        None,
        (uuid.UUID("00000000-0000-7000-8000-000000000797"),),
        ("retry_wait",),
    )
    store = PostgresModelGovernanceStore(cast(Any, connection), cast(Any, MagicMock()))
    failed = ModelExecution(
        request=_request(),
        status=ModelRunStatus.FAILED,
        validation_status=ValidationStatus.INVALID,
        error_code="rate_limited",
        error_summary="try later",
        http_status=429,
        retryable=True,
    )
    store.persist_and_finish_job(JOB_ID, ATTEMPT_ID, TOKEN, failed)
    assert connection.commit.call_count == 1


def test_postgres_store_duplicate_lookup_and_transaction_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uap_platform.model_governance import persistence as persistence_module
    from uap_platform.model_governance.persistence import PostgresModelGovernanceStore

    existing_id = uuid.UUID("00000000-0000-7000-8000-000000000796")
    connection = _connection((existing_id, "succeeded"))
    store = PostgresModelGovernanceStore(cast(Any, connection), cast(Any, MagicMock()))
    assert store.existing_model_run("model-job:one") == (existing_id, ModelRunStatus.SUCCEEDED)
    connection = _connection(None)
    store = PostgresModelGovernanceStore(cast(Any, connection), cast(Any, MagicMock()))
    assert store.existing_model_run("model-job:none") is None

    duplicate_store = MagicMock()
    monkeypatch.setattr(persistence_module, "store_and_register", duplicate_store)
    connection = _connection((existing_id,), ("succeeded",))
    store = PostgresModelGovernanceStore(cast(Any, connection), cast(Any, MagicMock()))
    duplicate = ModelExecution(
        request=_request(),
        status=ModelRunStatus.FAILED,
        validation_status=ValidationStatus.INVALID,
        error_code="provider_error",
        error_summary="duplicate call",
    )
    assert store.persist_and_finish_job(JOB_ID, ATTEMPT_ID, TOKEN, duplicate) == existing_id
    duplicate_store.assert_not_called()

    connection = _connection(("succeeded",))
    store = PostgresModelGovernanceStore(cast(Any, connection), cast(Any, MagicMock()))
    store.finish_existing_job(JOB_ID, ATTEMPT_ID, TOKEN)
    connection = _connection(("dead",))
    store = PostgresModelGovernanceStore(cast(Any, connection), cast(Any, MagicMock()))
    store.finish_job_only(
        JOB_ID,
        ATTEMPT_ID,
        TOKEN,
        http_status=422,
        error_code="invalid_model_input",
        error_summary="bad payload",
    )

    registered = _registered(5)
    monkeypatch.setattr(
        persistence_module,
        "store_and_register",
        MagicMock(return_value=registered),
    )
    cleanup = MagicMock()
    monkeypatch.setattr(persistence_module, "cleanup_unregistered_object", cleanup)
    connection = _connection(None)
    connection.cursor.return_value.__enter__.return_value.execute.side_effect = [
        None,
        None,
        RuntimeError("insert failed"),
    ]
    store = PostgresModelGovernanceStore(cast(Any, connection), cast(Any, MagicMock()))
    failed = ModelExecution(
        request=_request(),
        status=ModelRunStatus.FAILED,
        validation_status=ValidationStatus.INVALID,
        error_code="provider_error",
        error_summary="provider failed",
    )
    with pytest.raises(RuntimeError, match="insert failed"):
        store.persist_and_finish_job(JOB_ID, ATTEMPT_ID, TOKEN, failed)
    connection.rollback.assert_called_once()
    cleanup.assert_called_once()
