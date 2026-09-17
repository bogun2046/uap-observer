from __future__ import annotations

import json
import uuid
from typing import Any, cast

import pytest
from psycopg import Connection

from uap_platform.collectors import FetchClassification, FetchResponse
from uap_platform.documents import (
    ExtractionInput,
    ExtractionJobHandler,
    ExtractionOutcome,
    ExtractionResult,
)
from uap_platform.model_governance import DeepSeekProvider
from uap_platform.object_registry import ObjectClient, RegisteredObject, StorageDomain
from uap_platform.v1.reddit_source import RedditOfficialApiClient
from uap_platform.v1.worker import V1Worker

SOURCE_RUN_ID = uuid.UUID("00000000-0000-7000-8000-000000009201")
SOURCE_ID = uuid.UUID("00000000-0000-7000-8000-000000009202")
DOCUMENT_ID = uuid.UUID("00000000-0000-7000-8000-000000009203")
VERSION_ID = uuid.UUID("00000000-0000-7000-8000-000000009204")
OBJECT_ID = uuid.UUID("00000000-0000-7000-8000-000000009205")
JOB_ID = uuid.UUID("00000000-0000-7000-8000-000000009206")
ATTEMPT_ID = uuid.UUID("00000000-0000-7000-8000-000000009207")
LEASE_TOKEN = uuid.UUID("00000000-0000-7000-8000-000000009208")
CANONICAL_URL = "https://www.reddit.com/r/UFOs/comments/example1/lights/"
CONTENT_SHA = "a" * 64
TEST_ACCESS_TOKEN = uuid.uuid4().hex


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self._rows: tuple[tuple[object, ...], ...] = ()

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, parameters: object = ()) -> None:
        self.connection.statements.append((statement, parameters))
        if "WHERE sr.job_id" in statement:
            self._rows = self.connection.article_rows
        elif "WHERE dv.id = %s" in statement and "scv.configuration" in statement:
            self._rows = (self.connection.fallback_row,) if self.connection.fallback_row else ()
        else:
            self._rows = ((uuid.uuid4(),),)

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> tuple[tuple[object, ...], ...]:
        return self._rows


class FakeConnection:
    def __init__(self) -> None:
        self.article_rows: tuple[tuple[object, ...], ...] = ()
        self.fallback_row: tuple[object, ...] | None = None
        self.statements: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def make_worker(connection: FakeConnection) -> V1Worker:
    return V1Worker(
        cast(Connection[Any], connection),
        cast(Connection[Any], FakeConnection()),
        cast(ObjectClient, object()),
        DeepSeekProvider(api_key="test-key"),
    )


def enqueued(connection: FakeConnection, job_type: str) -> list[tuple[object, ...]]:
    values: list[tuple[object, ...]] = []
    for statement, parameters in connection.statements:
        if f"'{job_type}'" in statement:
            values.append(cast(tuple[object, ...], parameters))
    return values


def test_atom_primary_enqueues_existing_raw_without_article_fetch() -> None:
    connection = FakeConnection()
    connection.article_rows = (
        (
            SOURCE_RUN_ID,
            SOURCE_ID,
            DOCUMENT_ID,
            CANONICAL_URL,
            VERSION_ID,
            OBJECT_ID,
            CONTENT_SHA,
            "application/xml",
            "reddit_post_atom",
            "reddit_official_api",
        ),
    )

    make_worker(connection)._enqueue_article_fetches(JOB_ID)

    extract_jobs = enqueued(connection, "extract_document")
    assert len(extract_jobs) == 1
    payload = json.loads(cast(str, extract_jobs[0][0]))
    assert payload["document_version_id"] == str(VERSION_ID)
    assert payload["source_object_id"] == str(OBJECT_ID)
    assert payload["extractor_name"] == "reddit_source_text"
    assert enqueued(connection, "fetch_source") == []
    assert CANONICAL_URL not in " ".join(statement for statement, _ in connection.statements)


def test_atom_primary_and_fallback_use_stable_idempotency_keys() -> None:
    connection = FakeConnection()
    connection.article_rows = (
        (
            SOURCE_RUN_ID,
            SOURCE_ID,
            DOCUMENT_ID,
            CANONICAL_URL,
            VERSION_ID,
            OBJECT_ID,
            CONTENT_SHA,
            "application/xml",
            "reddit_post_atom",
            "reddit_official_api",
        ),
    )
    worker = make_worker(connection)
    worker._enqueue_article_fetches(JOB_ID)
    worker._enqueue_article_fetches(JOB_ID)
    extract_keys = [cast(str, values[1]) for values in enqueued(connection, "extract_document")]
    assert len(extract_keys) == 2
    assert len(set(extract_keys)) == 1

    connection.fallback_row = (
        SOURCE_RUN_ID,
        SOURCE_ID,
        DOCUMENT_ID,
        CANONICAL_URL,
        VERSION_ID,
        CONTENT_SHA,
        "reddit_official_api",
        None,
    )
    worker._enqueue_reddit_fallback(VERSION_ID)
    worker._enqueue_reddit_fallback(VERSION_ID)
    fallback_keys = [cast(str, values[1]) for values in enqueued(connection, "fetch_source")]
    assert len(fallback_keys) == 2
    assert len(set(fallback_keys)) == 1


def failed_result(code: str) -> ExtractionResult:
    request = ExtractionInput(
        document_version_id=VERSION_ID,
        source_object_id=OBJECT_ID,
        media_type="text/html",
        extractor_name="reddit_source_text",
        extractor_version="1.0.0",
    )
    return ExtractionResult(
        request=request,
        outcome=ExtractionOutcome.FAILED,
        error_code=code,
        error_summary="safe failure",
    )


@pytest.mark.parametrize("code", ["source_bot_challenge", "reddit_api_invalid_json"])
def test_failed_reddit_extraction_never_enqueues_deepseek(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    connection = FakeConnection()
    worker = make_worker(connection)
    analysis_calls: list[object] = []
    fallback_calls: list[object] = []

    monkeypatch.setattr(
        ExtractionJobHandler,
        "handle",
        lambda *_args: (uuid.uuid4(), failed_result(code)),
    )
    monkeypatch.setattr(
        worker, "_enqueue_analysis", lambda *args, **_kwargs: analysis_calls.append(args)
    )
    monkeypatch.setattr(
        worker, "_enqueue_reddit_fallback", lambda *args: fallback_calls.append(args)
    )

    worker._extract(JOB_ID, ATTEMPT_ID, LEASE_TOKEN, {})

    assert analysis_calls == []
    assert fallback_calls == []


def test_body_unavailable_enqueues_only_fallback_not_deepseek(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = make_worker(FakeConnection())
    analysis_calls: list[object] = []
    fallback_calls: list[object] = []
    monkeypatch.setattr(
        ExtractionJobHandler,
        "handle",
        lambda *_args: (uuid.uuid4(), failed_result("reddit_body_unavailable")),
    )
    monkeypatch.setattr(
        worker, "_enqueue_analysis", lambda *args, **_kwargs: analysis_calls.append(args)
    )
    monkeypatch.setattr(
        worker, "_enqueue_reddit_fallback", lambda *args: fallback_calls.append(args)
    )

    worker._extract(JOB_ID, ATTEMPT_ID, LEASE_TOKEN, {})

    assert analysis_calls == []
    assert fallback_calls == [(VERSION_ID,)]


def api_payload(fullname: str = "t3_example1", body: str = "real body") -> bytes:
    return json.dumps(
        {
            "data": {
                "children": [
                    {"data": {"name": fullname, "title": "title", "selftext": body}}
                ]
            }
        }
    ).encode()


def test_official_api_client_is_provenance_bound_and_authenticated() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fetch(url: str, headers: Any) -> FetchResponse:
        calls.append((url, dict(headers)))
        return FetchResponse(
            status_code=200,
            body=api_payload(),
            headers={"content-type": "application/json"},
        )

    response = RedditOfficialApiClient(
        access_token=TEST_ACCESS_TOKEN, user_agent="uap-test/1.0", fetch=fetch
    ).fetch_post("t3_example1")

    assert response.classify() is FetchClassification.SUCCESS
    assert len(calls) == 1
    assert "id=t3_example1" in calls[0][0]
    assert calls[0][1]["Authorization"] == f"Bearer {TEST_ACCESS_TOKEN}"
    assert CANONICAL_URL not in calls[0][0]

    mismatch = RedditOfficialApiClient(
        access_token=TEST_ACCESS_TOKEN,
        user_agent="uap-test/1.0",
        fetch=lambda *_args: FetchResponse(
            status_code=200,
            body=api_payload("t3_other"),
            headers={"content-type": "application/json"},
        ),
    ).fetch_post("t3_example1")
    assert mismatch.error_code == "reddit_api_provenance_mismatch"


def test_official_api_failure_codes_and_retry_classification() -> None:
    missing = RedditOfficialApiClient(access_token=None, user_agent=None).fetch_post(
        "t3_example1"
    )
    assert missing.error_code == "reddit_api_credentials_unavailable"
    assert missing.classify() is FetchClassification.TERMINAL_FAILURE

    cases = (
        (401, "reddit_api_auth_failed", FetchClassification.TERMINAL_FAILURE),
        (429, "reddit_api_rate_limited", FetchClassification.RATE_LIMITED),
        (500, "reddit_api_server_error", FetchClassification.TRANSIENT_FAILURE),
    )
    for status, code, classification in cases:
        client = RedditOfficialApiClient(
            access_token=TEST_ACCESS_TOKEN,
            user_agent="agent",
            fetch=lambda *_args, status=status: FetchResponse(status_code=status),
        )
        response = client.fetch_post("t3_example1")
        assert response.error_code == code
        assert response.classify() is classification


def test_invalid_api_json_is_preserved_for_extraction_failure() -> None:
    response = RedditOfficialApiClient(
        access_token=TEST_ACCESS_TOKEN,
        user_agent="agent",
        fetch=lambda *_args: FetchResponse(
            status_code=200,
            body=b"not-json",
            headers={"content-type": "application/json"},
        ),
    ).fetch_post("t3_example1")

    assert response.classify() is FetchClassification.SUCCESS
    assert response.body == b"not-json"


class ApiCursor:
    def __init__(self, connection: ApiConnection) -> None:
        self.connection = connection
        self.row: tuple[object, ...] | None = None

    def __enter__(self) -> ApiCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, parameters: object = ()) -> None:
        self.connection.statements.append((statement, parameters))
        if "SELECT d.source_item_key" in statement:
            self.row = ("t3_example1",)
        elif "SELECT dv.original_title" in statement:
            self.row = ("Original title", None)
        elif "INSERT INTO ingest.artifacts" in statement:
            self.row = (uuid.uuid4(),)
        elif "INSERT INTO ingest.artifact_versions" in statement:
            self.row = (uuid.uuid4(),)
        elif "SELECT id FROM core.document_versions" in statement:
            self.row = None
        elif "SELECT id FROM core.documents" in statement:
            self.row = (DOCUMENT_ID,)
        elif "coalesce(max(version_no)" in statement:
            self.row = (2,)
        elif "ops.enqueue_job" in statement:
            self.row = (uuid.uuid4(),)
        elif "ops.finish_job" in statement:
            self.row = ("succeeded",)
        else:
            self.row = None

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row


class ApiConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> ApiCursor:
        return ApiCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_official_api_fallback_saves_json_raw_and_preserves_document_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = api_payload()

    class ApiClient:
        def fetch_post(self, fullname: str) -> FetchResponse:
            assert fullname == "t3_example1"
            return FetchResponse(
                status_code=200,
                body=raw,
                headers={"content-type": "application/json"},
            )

    registered = RegisteredObject(
        storage_domain=StorageDomain.RAW,
        bucket_name="raw",
        object_key=f"raw/{CONTENT_SHA}",
        content_sha256=CONTENT_SHA,
        byte_length=len(raw),
        media_type="application/json",
        id=OBJECT_ID,
        reused=False,
        created=True,
    )
    stored: list[tuple[object, ...]] = []

    def fake_store(*args: object, **_kwargs: object) -> RegisteredObject:
        stored.append(args)
        return registered

    monkeypatch.setattr("uap_platform.v1.worker.store_and_register", fake_store)
    connection = ApiConnection()
    worker = V1Worker(
        cast(Connection[Any], connection),
        cast(Connection[Any], FakeConnection()),
        cast(ObjectClient, object()),
        DeepSeekProvider(api_key="test-key"),
        cast(RedditOfficialApiClient, ApiClient()),
    )
    payload = {
        "source_run_id": str(SOURCE_RUN_ID),
        "source_id": str(SOURCE_ID),
        "document_id": str(DOCUMENT_ID),
        "feed_document_version_id": str(VERSION_ID),
        "source_url": CANONICAL_URL,
        "document_fetch_method": "reddit_official_api",
        "payload_schema_version": "v1.article-fetch.v1",
    }

    worker._fetch_document(JOB_ID, ATTEMPT_ID, LEASE_TOKEN, payload)

    assert stored
    assert stored[0][2] is StorageDomain.RAW
    assert stored[0][3] == raw
    artifact_insert = next(
        values for sql, values in connection.statements if "INSERT INTO ingest.artifacts" in sql
    )
    assert cast(tuple[object, ...], artifact_insert)[3] == "json"
    extraction = next(
        values for sql, values in connection.statements if "'extract_document'" in sql
    )
    extraction_payload = json.loads(cast(str, cast(tuple[object, ...], extraction)[0]))
    assert extraction_payload["source_object_id"] == str(OBJECT_ID)
    assert extraction_payload["extractor_name"] == "reddit_source_text"
    provenance_queries = [
        values for sql, values in connection.statements if "d.canonical_url = %s" in sql
    ]
    assert all(
        cast(tuple[object, ...], values)[1:] == (DOCUMENT_ID, SOURCE_ID, CANONICAL_URL)
        for values in provenance_queries
    )
    metadata_values = next(
        cast(tuple[object, ...], values)
        for sql, values in connection.statements
        if "INSERT INTO core.document_versions" in sql
    )
    assert json.loads(cast(str, metadata_values[7]))["fetch_method"] == "reddit_official_api"
