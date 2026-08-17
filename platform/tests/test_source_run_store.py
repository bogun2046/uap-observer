from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime

import pytest

from uap_platform.collectors import (
    CollectionResult,
    FetchClassification,
    NormalizedItem,
    PostgresSourceRunStore,
)


class NoSuchKey(Exception):
    code = "NoSuchKey"


class Response(io.BytesIO):
    def close(self) -> None:
        super().close()

    def release_conn(self) -> None:
        return None


class FakeObjectClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def bucket_exists(self, _bucket_name: str) -> bool:
        return True

    def make_bucket(self, _bucket_name: str) -> None:
        return None

    def stat_object(self, bucket_name: str, object_name: str) -> object:
        if (bucket_name, object_name) not in self.objects:
            raise NoSuchKey()
        return type("Stat", (), {"size": len(self.objects[(bucket_name, object_name)])})()

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: io.BytesIO,
        length: int,
        content_type: str,
        metadata: dict[str, str],
    ) -> object:
        del content_type, metadata
        payload = data.read()
        assert len(payload) == length
        self.objects[(bucket_name, object_name)] = payload
        return object()

    def get_object(self, bucket_name: str, object_name: str) -> Response:
        return Response(self.objects[(bucket_name, object_name)])

    def remove_object(self, bucket_name: str, object_name: str) -> None:
        self.objects.pop((bucket_name, object_name), None)


class FakeCursor:
    def __init__(
        self,
        *,
        existing_url: bool = False,
        same_version: bool = False,
        fail_document_version: bool = False,
    ) -> None:
        self.existing_url = existing_url
        self.same_version = same_version
        self.fail_document_version = fail_document_version
        self.last_query = ""
        self.parameters: object = None
        self.rowcount = 1
        self.executed: list[str] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, parameters: object = None) -> None:
        if self.fail_document_version and "INSERT INTO core.document_versions" in query:
            raise RuntimeError("injected document write failure")
        self.last_query = query
        self.parameters = parameters
        self.executed.append(query)

    def fetchone(self) -> tuple[object, ...] | None:
        if "SELECT ops.finish_job" in self.last_query:
            return ("succeeded",)
        if "INSERT INTO core.stored_objects" in self.last_query:
            params = self.parameters
            assert isinstance(params, tuple)
            return (
                uuid.UUID("00000000-0000-7000-8000-000000000020"),
                params[2],
                params[3],
                params[5],
                params[6],
            )
        if "INSERT INTO ingest.artifacts" in self.last_query:
            return (uuid.UUID("00000000-0000-7000-8000-000000000021"),)
        if "INSERT INTO ingest.artifact_versions" in self.last_query:
            return (uuid.UUID("00000000-0000-7000-8000-000000000022"),)
        if "SELECT id" in self.last_query and "canonical_url" in self.last_query:
            return (
                (uuid.UUID("00000000-0000-7000-8000-000000000023"),)
                if self.existing_url
                else None
            )
        if "INSERT INTO core.documents" in self.last_query:
            return (
                uuid.UUID("00000000-0000-7000-8000-000000000023"),
                not self.existing_url,
            )
        if "SELECT version_no" in self.last_query:
            return (1, "" + "0" * 64) if self.same_version else None
        if "SELECT id" in self.last_query and "artifact_versions" in self.last_query:
            return (uuid.UUID("00000000-0000-7000-8000-000000000022"),)
        return None


class FakeConnection:
    def __init__(
        self,
        *,
        existing_url: bool = False,
        same_version: bool = False,
        fail_document_version: bool = False,
    ) -> None:
        self.cursor_value = FakeCursor(
            existing_url=existing_url,
            same_version=same_version,
            fail_document_version=fail_document_version,
        )
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return self.cursor_value

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def item() -> NormalizedItem:
    return NormalizedItem(
        source_item_key="item-1",
        canonical_url="https://example.test/item-1",
        title="Item one",
        published_at=None,
        summary="Summary",
        raw_payload=b"<item id='item-1'/>",
    )


def test_store_links_source_run_to_artifact_and_document_versions() -> None:
    connection = FakeConnection()
    store = PostgresSourceRunStore(connection, FakeObjectClient())  # type: ignore[arg-type]
    source_id = uuid.UUID("00000000-0000-7000-8000-000000000010")
    job_id = uuid.UUID("00000000-0000-7000-8000-000000000011")
    run_id = store.start_source_run(
        source_id, job_id, "run-1", datetime.now(UTC), uuid.uuid4()
    )

    assert store.persist_items(source_id, run_id, (item(),), datetime.now(UTC)) == 1
    sql = "\n".join(connection.cursor_value.executed)
    assert "source_run_id" in sql
    assert "document_versions" in sql

    store.finish_source_run(
        run_id,
        CollectionResult(FetchClassification.SUCCESS, 200, 1, parsed_count=1, persisted_count=1),
        datetime.now(UTC),
    )
    assert connection.commits == 2


def test_store_reuses_document_by_canonical_url() -> None:
    connection = FakeConnection(existing_url=True)
    store = PostgresSourceRunStore(connection, FakeObjectClient())  # type: ignore[arg-type]

    assert store.persist_items(uuid.uuid4(), uuid.uuid4(), (item(),), datetime.now(UTC)) == 0
    assert any("ON CONFLICT (canonical_url)" in query for query in connection.cursor_value.executed)


def test_store_failure_removes_new_unregistered_object() -> None:
    connection = FakeConnection(fail_document_version=True)
    object_client = FakeObjectClient()
    store = PostgresSourceRunStore(connection, object_client)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="injected document write failure"):
        store.persist_items(uuid.uuid4(), uuid.uuid4(), (item(),), datetime.now(UTC))

    store.fail_source_run(
        uuid.uuid4(),
        CollectionResult(
            FetchClassification.TERMINAL_FAILURE,
            599,
            0,
            error_code="collector_error",
            error_summary="persist failed",
        ),
        datetime.now(UTC),
    )

    assert object_client.objects == {}


def test_store_failure_rolls_back_business_writes_and_commits_failure() -> None:
    connection = FakeConnection()
    store = PostgresSourceRunStore(connection, FakeObjectClient())  # type: ignore[arg-type]
    run_id = uuid.uuid4()
    result = CollectionResult(
        FetchClassification.TERMINAL_FAILURE,
        599,
        0,
        error_code="collector_error",
        error_summary="persist failed",
    )

    store.fail_source_run(run_id, result, datetime.now(UTC))

    assert connection.rollbacks == 1
    assert connection.commits == 1


@pytest.mark.parametrize("status", [304, 403, 429, 503])
def test_store_finish_accepts_non_success_source_run(status: int) -> None:
    connection = FakeConnection()
    store = PostgresSourceRunStore(connection, FakeObjectClient())  # type: ignore[arg-type]
    classification = (
        FetchClassification.NOT_MODIFIED
        if status == 304
        else FetchClassification.TRANSIENT_FAILURE
    )

    store.finish_source_run(
        uuid.uuid4(), CollectionResult(classification, status, 1), datetime.now(UTC)
    )

    assert connection.commits == 1


def test_store_finishes_wp4_job_with_collector_outcome() -> None:
    connection = FakeConnection()
    store = PostgresSourceRunStore(connection, FakeObjectClient())  # type: ignore[arg-type]
    store.finish_job(
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        CollectionResult(FetchClassification.SUCCESS, 200, 1),
    )

    assert connection.commits == 1
    assert "ops.finish_job" in connection.cursor_value.last_query
