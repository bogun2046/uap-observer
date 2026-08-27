from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from psycopg import Connection

import uap_platform.documents.persistence as persistence
from uap_platform.documents import (
    ExtractionInput,
    ExtractionOutcome,
    ExtractionResult,
)
from uap_platform.object_registry import ObjectClient, RegisteredObject, StorageDomain

DOCUMENT_VERSION_ID = uuid.UUID("00000000-0000-7000-8000-000000000401")
SOURCE_OBJECT_ID = uuid.UUID("00000000-0000-7000-8000-000000000402")
JOB_ID = uuid.UUID("00000000-0000-7000-8000-000000000403")
ATTEMPT_ID = uuid.UUID("00000000-0000-7000-8000-000000000404")
TOKEN = uuid.UUID("00000000-0000-7000-8000-000000000405")
EXTRACTION_ID = uuid.UUID("00000000-0000-7000-8000-000000000406")
OBJECT_ID = uuid.UUID("00000000-0000-7000-8000-000000000407")


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.row: tuple[Any, ...] | None = None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, parameters: object = ()) -> None:
        self.connection.statements.append((statement, parameters))
        if "document_versions AS dv" in statement:
            self.row = self.connection.load_row
        elif "INSERT INTO core.extractions" in statement:
            if self.connection.fail_insert:
                raise RuntimeError("injected extraction insert failure")
            self.row = self.connection.insert_row
        elif "SELECT id" in statement:
            self.row = self.connection.conflict_row
        elif "ops.finish_job" in statement:
            self.row = self.connection.finish_row
        else:
            self.row = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


class FakeConnection:
    def __init__(self) -> None:
        self.load_row: tuple[Any, ...] | None = (
            "raw",
            "raw/object",
            "a" * 64,
            4,
            "text/html; charset=utf-8",
        )
        self.insert_row: tuple[Any, ...] | None = (EXTRACTION_ID,)
        self.conflict_row: tuple[Any, ...] | None = (EXTRACTION_ID,)
        self.finish_row: tuple[Any, ...] | None = ("succeeded",)
        self.fail_insert = False
        self.statements: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class EchoExtractor:
    name = "test_extractor"
    version = "1.0.0"

    def extract(self, request: ExtractionInput, payload: bytes) -> ExtractionResult:
        return success_result(request, payload.decode())


def request() -> ExtractionInput:
    return ExtractionInput(
        document_version_id=DOCUMENT_VERSION_ID,
        source_object_id=SOURCE_OBJECT_ID,
        media_type="text/html",
        extractor_name=EchoExtractor.name,
        extractor_version=EchoExtractor.version,
    )


def success_result(
    extraction_request: ExtractionInput | None = None,
    text: str = "hello",
) -> ExtractionResult:
    from uap_platform.documents.contracts import text_sha256

    return ExtractionResult(
        request=extraction_request or request(),
        outcome=ExtractionOutcome.SUCCEEDED,
        text=text,
        output_sha256=text_sha256(text),
    )


def failure_result(
    error_code: str = "invalid_pdf",
    extraction_request: ExtractionInput | None = None,
) -> ExtractionResult:
    return ExtractionResult(
        request=extraction_request or request(),
        outcome=ExtractionOutcome.FAILED,
        error_code=error_code,
        error_summary="safe failure summary",
    )


def registered_object(*, created: bool = True) -> RegisteredObject:
    return RegisteredObject(
        storage_domain=StorageDomain.DERIVED,
        bucket_name="derived",
        object_key="derived/" + "b" * 64,
        content_sha256="b" * 64,
        byte_length=5,
        media_type="text/plain; charset=utf-8",
        id=OBJECT_ID,
        reused=not created,
        created=created,
    )


def store(
    connection: FakeConnection,
    object_client: object | None = None,
) -> persistence.PostgresExtractionStore:
    return persistence.PostgresExtractionStore(
        cast(Connection[object], connection),
        cast(ObjectClient, object_client or object()),
    )


def test_load_raw_verifies_link_and_media_type(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = FakeConnection()
    object_client = object()
    loaded: list[tuple[object, ...]] = []

    def fake_read(*args: object) -> bytes:
        loaded.append(args)
        return b"<p>x</p>"

    monkeypatch.setattr(
        persistence,
        "read_verified_object",
        fake_read,
    )

    result = store(connection, object_client).load_raw(request())

    assert result == b"<p>x</p>"
    assert loaded == [(
        object_client,
        "raw",
        "raw/object",
        "a" * 64,
        4,
    )]


def test_persist_is_idempotent_and_commits_once(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = FakeConnection()
    registered = registered_object()
    monkeypatch.setattr(persistence, "store_and_register", lambda *_args, **_kwargs: registered)

    extraction_id = store(connection).persist(ATTEMPT_ID, success_result())

    assert extraction_id == EXTRACTION_ID
    assert connection.commits == 1
    assert connection.rollbacks == 0

    connection.insert_row = None
    extraction_id = store(connection).persist(ATTEMPT_ID, success_result())
    assert extraction_id == EXTRACTION_ID
    assert connection.commits == 2


def test_failed_result_has_no_derived_object(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = FakeConnection()
    called = False

    def unexpected_store(*_args: object, **_kwargs: object) -> RegisteredObject:
        nonlocal called
        called = True
        return registered_object()

    monkeypatch.setattr(persistence, "store_and_register", unexpected_store)
    extraction_id = store(connection).persist(ATTEMPT_ID, failure_result())

    assert extraction_id == EXTRACTION_ID
    assert called is False
    assert connection.commits == 1


def test_rollback_compensates_new_derived_object(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = FakeConnection()
    connection.fail_insert = True
    registered = registered_object()
    cleaned: list[RegisteredObject] = []

    def fake_cleanup(
        _connection: object,
        _client: object,
        value: RegisteredObject,
    ) -> bool:
        cleaned.append(value)
        return True

    monkeypatch.setattr(persistence, "store_and_register", lambda *_args, **_kwargs: registered)
    monkeypatch.setattr(
        persistence,
        "cleanup_unregistered_object",
        fake_cleanup,
    )

    with pytest.raises(RuntimeError, match="injected extraction"):
        store(connection).persist(ATTEMPT_ID, success_result())

    assert connection.rollbacks == 1
    assert connection.commits == 1
    assert cleaned == [registered]


def test_persist_and_finish_job_uses_one_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = FakeConnection()
    registered = registered_object()
    monkeypatch.setattr(persistence, "store_and_register", lambda *_args, **_kwargs: registered)

    extraction_id = store(connection).persist_and_finish_job(
        JOB_ID,
        ATTEMPT_ID,
        TOKEN,
        success_result(),
    )

    assert extraction_id == EXTRACTION_ID
    assert connection.commits == 1
    finish_statements = [item for item in connection.statements if "finish_job" in item[0]]
    assert len(finish_statements) == 1
    parameters = cast(tuple[object, ...], finish_statements[0][1])
    assert parameters[:3] == (JOB_ID, ATTEMPT_ID, TOKEN)


def test_run_and_finish_job_maps_failures_and_closes_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    extraction_store = store(connection)
    captured: list[ExtractionResult] = []

    def capture_finish(*args: object) -> uuid.UUID:
        captured.append(cast(ExtractionResult, args[-1]))
        return EXTRACTION_ID

    monkeypatch.setattr(
        extraction_store,
        "persist_and_finish_job",
        capture_finish,
    )

    monkeypatch.setattr(extraction_store, "load_raw", lambda _request: b"hello")
    extraction_id, result = extraction_store.run_and_finish_job(
        JOB_ID, ATTEMPT_ID, TOKEN, request(), EchoExtractor()
    )
    assert extraction_id == EXTRACTION_ID
    assert result.outcome is ExtractionOutcome.SUCCEEDED

    for error, expected_code in [
        (LookupError("missing"), "input_not_found"),
        (RuntimeError("storage down"), "storage_read_failed"),
        (ValueError("bad extractor"), "extractor_error"),
    ]:
        monkeypatch.setattr(
            extraction_store,
            "load_raw",
            lambda _request, error=error: (_ for _ in ()).throw(error),
        )
        _, failed = extraction_store.run_and_finish_job(
            JOB_ID, ATTEMPT_ID, TOKEN, request(), EchoExtractor()
        )
        assert failed.error_code == expected_code

    assert len(captured) == 4
    assert connection.rollbacks == 3
