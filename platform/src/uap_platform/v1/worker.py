"""V1-1 worker: RSS/Atom -> extraction -> relevant DeepSeek task chain."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Mapping
from typing import Any, cast

import psycopg
from psycopg import Connection

from uap_platform.collectors import (
    CollectionResult,
    PostgresSourceRunStore,
    RssSourceRunRunner,
    UrlLibFetcher,
)
from uap_platform.documents import (
    ExtractionJobHandler,
    FeedXmlExtractor,
    HtmlExtractor,
    PdfExtractor,
    RedditSourceExtractor,
)
from uap_platform.documents.persistence import PostgresExtractionStore
from uap_platform.model_governance import (
    DeepSeekProvider,
    ModelJobHandler,
    ModelTaskType,
    ProviderRegistry,
)
from uap_platform.model_governance.contracts import ModelProvider
from uap_platform.model_governance.persistence import PostgresModelGovernanceStore
from uap_platform.object_registry import (
    ObjectClient,
    RegisteredObject,
    StorageDomain,
    cleanup_unregistered_object,
    store_and_register,
)
from uap_platform.object_store_init import build_client

from .config import load_v1_config
from .reddit_source import RedditOfficialApiClient

LOGGER = logging.getLogger(__name__)
_JOB_TYPES = ("fetch_source", "extract_document", "analyze_document")
_MAX_ARTICLE_BYTES = 10_000_000
_ANALYSIS_ORDER = (
    ModelTaskType.SUMMARY,
    ModelTaskType.CLAIM_EXTRACTION,
    ModelTaskType.ENTITY_EXTRACTION,
)


def _uuid(payload: Mapping[str, object], key: str) -> uuid.UUID:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} is required")
    return uuid.UUID(value)


def _string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value


class V1Worker:
    """One local worker process with separate ordinary/model database capabilities."""

    def __init__(
        self,
        worker_connection: Connection[Any],
        model_connection: Connection[Any],
        object_client: ObjectClient,
        deepseek_provider: DeepSeekProvider,
        reddit_api_client: RedditOfficialApiClient | None = None,
        *,
        worker_id: str = "v1-local-worker",
        lease_seconds: int = 180,
    ) -> None:
        self.worker_connection = worker_connection
        self.model_connection = model_connection
        self.object_client = object_client
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self._reddit_api_client = reddit_api_client or RedditOfficialApiClient(
            access_token=None, user_agent=None
        )
        self._model_handler = ModelJobHandler(
            PostgresModelGovernanceStore(model_connection, object_client),
            ProviderRegistry({"deepseek": cast(ModelProvider, deepseek_provider)}),
        )

    def claim_one(self) -> tuple[Any, ...] | None:
        with self.worker_connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT job_id, attempt_id, job_type, payload, lease_token
                  FROM ops.claim_job('worker', %s, %s::text[], %s)
                """,
                (self.worker_id, list(_JOB_TYPES), self.lease_seconds),
            )
            row = cursor.fetchone()
        self.worker_connection.commit()
        return None if row is None else tuple(row)

    def run_once(self) -> str | None:
        claim = self.claim_one()
        if claim is None:
            return None
        job_id = uuid.UUID(str(claim[0]))
        attempt_id = uuid.UUID(str(claim[1]))
        job_type = str(claim[2])
        payload = claim[3]
        lease_token = uuid.UUID(str(claim[4]))
        if not isinstance(payload, Mapping):
            self._finish_invalid(job_id, attempt_id, lease_token, "invalid_v1_payload")
            return job_type
        try:
            if job_type == "fetch_source":
                if payload.get("payload_schema_version") == "v1.article-fetch.v1":
                    self._fetch_document(job_id, attempt_id, lease_token, payload)
                else:
                    self._fetch(job_id, attempt_id, lease_token, payload)
            elif job_type == "extract_document":
                self._extract(job_id, attempt_id, lease_token, payload)
            elif job_type == "analyze_document":
                self._analyze(job_id, attempt_id, lease_token, payload)
            else:
                self._finish_invalid(job_id, attempt_id, lease_token, "unsupported_v1_job")
        except Exception:
            LOGGER.exception("V1 worker dispatch failed job_id=%s job_type=%s", job_id, job_type)
            raise
        return job_type

    def _fetch(
        self,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        payload: Mapping[str, object],
    ) -> None:
        if _string(payload, "payload_schema_version") != "v1.fetch.v1":
            raise ValueError("unsupported V1 fetch payload")
        if _string(payload, "source_type") != "rss":
            self._finish_invalid(job_id, attempt_id, lease_token, "web_source_not_active_v1_1")
            return
        source_id = _uuid(payload, "source_id")
        config_version_id = _uuid(payload, "source_config_version_id")
        with self.worker_connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT etag, last_modified
                  FROM ingest.source_runs
                 WHERE source_id = %s AND outcome IN ('succeeded', 'not_modified')
                 ORDER BY started_at DESC LIMIT 1
                """,
                (source_id,),
            )
            previous = cast(tuple[str | None, str | None] | None, cursor.fetchone())
        self.worker_connection.rollback()
        etag, last_modified = previous or (None, None)
        store = PostgresSourceRunStore(self.worker_connection, self.object_client)
        runner = RssSourceRunRunner(
            UrlLibFetcher(timeout_seconds=20, user_agent="uap-platform-v1/1.0"), store
        )
        result = runner.run(
            source_id,
            job_id,
            _string(payload, "run_key"),
            _string(payload, "source_url"),
            attempt_id=attempt_id,
            lease_token=lease_token,
            etag=etag,
            last_modified=last_modified,
            source_config_version_id=config_version_id,
        )
        if result.classification.value in {"success", "not_modified", "empty"}:
            self._enqueue_article_fetches(job_id)

    def _enqueue_article_fetches(self, source_job_id: uuid.UUID) -> None:
        with self.worker_connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT sr.id, sr.source_id, d.id, d.canonical_url, dv.id,
                       so.id, so.content_sha256, so.media_type,
                       scv.configuration ->> 'document_fetch_method',
                       scv.configuration ->> 'fallback'
                  FROM ingest.source_runs AS sr
                  JOIN ingest.source_config_versions AS scv
                    ON scv.id = sr.source_config_version_id
                  JOIN ingest.artifact_versions AS av ON av.source_run_id = sr.id
                  JOIN core.document_versions AS dv ON dv.artifact_version_id = av.id
                  JOIN core.documents AS d ON d.id = dv.document_id
                  JOIN core.stored_objects AS so ON so.id = av.stored_object_id
                 WHERE sr.job_id = %s
                   AND d.canonical_url IS NOT NULL
                """,
                (source_job_id,),
            )
            rows = tuple(cursor.fetchall())
            for (
                source_run_id,
                source_id,
                document_id,
                url,
                feed_version_id,
                feed_object_id,
                feed_sha,
                feed_media_type,
                document_fetch_method,
                fallback,
            ) in rows:
                if document_fetch_method == "reddit_post_atom":
                    self._enqueue_extraction_with_cursor(
                        cursor,
                        document_version_id=uuid.UUID(str(feed_version_id)),
                        source_object_id=uuid.UUID(str(feed_object_id)),
                        media_type=str(feed_media_type),
                        extractor=RedditSourceExtractor(),
                        content_sha256=str(feed_sha),
                    )
                    continue
                job_payload = {
                    "source_run_id": str(source_run_id),
                    "source_id": str(source_id),
                    "document_id": str(document_id),
                    "feed_document_version_id": str(feed_version_id),
                    "source_url": str(url),
                    "payload_schema_version": "v1.article-fetch.v1",
                }
                if document_fetch_method is not None:
                    job_payload["document_fetch_method"] = str(document_fetch_method)
                if fallback is not None:
                    job_payload["fallback"] = str(fallback)
                cursor.execute(
                    """
                    SELECT ops.enqueue_job(
                        'fetch_source', %s::jsonb, 'v1.article-fetch.v1',
                        %s, 0::smallint, now(), 3, 120
                    )
                    """,
                    (
                        json.dumps(job_payload, sort_keys=True),
                        f"v1-article:{document_id}:{feed_sha}",
                    ),
                )
        self.worker_connection.commit()

    @staticmethod
    def _enqueue_extraction_with_cursor(
        cursor: Any,
        *,
        document_version_id: uuid.UUID,
        source_object_id: uuid.UUID,
        media_type: str,
        extractor: Any,
        content_sha256: str,
    ) -> None:
        extraction_payload = {
            "document_version_id": str(document_version_id),
            "source_object_id": str(source_object_id),
            "media_type": media_type,
            "extractor_name": extractor.name,
            "extractor_version": extractor.version,
            "payload_schema_version": "extract.v1",
        }
        cursor.execute(
            """
            SELECT ops.enqueue_job(
                'extract_document', %s::jsonb, 'extract.v1', %s,
                0::smallint, now(), 3, 120
            )
            """,
            (
                json.dumps(extraction_payload, sort_keys=True),
                f"v1-extract:{document_version_id}:{content_sha256}:{extractor.name}",
            ),
        )

    def _fetch_document(
        self,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        payload: Mapping[str, object],
    ) -> None:
        if _string(payload, "payload_schema_version") != "v1.article-fetch.v1":
            raise ValueError("unsupported V1 article fetch payload")
        source_run_id = _uuid(payload, "source_run_id")
        source_id = _uuid(payload, "source_id")
        document_id = _uuid(payload, "document_id")
        feed_version_id = _uuid(payload, "feed_document_version_id")
        source_url = _string(payload, "source_url")
        method = payload.get("document_fetch_method")
        if method not in {
            None,
            "canonical_url",
            "reddit_post_atom",
            "reddit_official_api",
        }:
            raise ValueError("unsupported document fetch method")
        if method == "reddit_post_atom":
            self._enqueue_existing_atom_extraction(
                job_id,
                attempt_id,
                lease_token,
                source_id=source_id,
                document_id=document_id,
                feed_version_id=feed_version_id,
                source_url=source_url,
            )
            return
        if method == "reddit_official_api":
            with self.worker_connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT d.source_item_key
                      FROM core.document_versions AS dv
                      JOIN core.documents AS d ON d.id = dv.document_id
                     WHERE dv.id = %s AND d.id = %s AND d.source_id = %s
                       AND d.canonical_url = %s
                    """,
                    (feed_version_id, document_id, source_id, source_url),
                )
                source_item = cursor.fetchone()
            self.worker_connection.rollback()
            if source_item is None or not isinstance(source_item[0], str):
                self._finish_invalid(
                    job_id, attempt_id, lease_token, "reddit_api_provenance_missing"
                )
                return
            response = self._reddit_api_client.fetch_post(str(source_item[0]))
            fetched_url = "https://oauth.reddit.com/api/info"
        else:
            fetched_url = source_url
            response = UrlLibFetcher(timeout_seconds=20, user_agent="uap-platform-v1/1.0")(
                fetched_url, {}
            )
        classification = response.classify()
        if classification.value != "success":
            result = CollectionResult(
                classification=classification,
                http_status=response.status_code,
                fetched_count=1,
                error_code=response.error_code,
                error_summary=response.error_summary,
            )
            PostgresSourceRunStore(self.worker_connection, self.object_client).finish_job(
                job_id, attempt_id, lease_token, result
            )
            return
        media_type = (response.header("content-type") or "").split(";", 1)[0].strip().lower()
        extractor: Any
        if method == "reddit_official_api":
            extractor = (
                RedditSourceExtractor()
                if media_type in RedditSourceExtractor.supported_media_types
                else None
            )
        else:
            extractor = (
                HtmlExtractor()
                if media_type in {"text/html", "application/xhtml+xml"}
                else PdfExtractor()
                if media_type == "application/pdf"
                else FeedXmlExtractor()
                if media_type
                in {"application/atom+xml", "application/rss+xml", "application/xml"}
                else None
            )
        if extractor is None or len(response.body) > _MAX_ARTICLE_BYTES:
            code = (
                "article_too_large"
                if len(response.body) > _MAX_ARTICLE_BYTES
                else "unsupported_article_media"
            )
            self._finish_invalid(job_id, attempt_id, lease_token, code)
            return

        registered: RegisteredObject | None = None
        try:
            registered = store_and_register(
                self.object_client,
                self.worker_connection,
                StorageDomain.RAW,
                response.body,
                media_type,
            )
            headers = {
                name: value
                for name in ("etag", "last-modified", "content-type", "content-length")
                if (value := response.header(name)) is not None
            }
            with self.worker_connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT dv.original_title, dv.source_published_at
                      FROM core.document_versions AS dv
                      JOIN core.documents AS d ON d.id = dv.document_id
                     WHERE dv.id = %s AND d.id = %s AND d.source_id = %s
                       AND d.canonical_url = %s
                    """,
                    (feed_version_id, document_id, source_id, source_url),
                )
                provenance = cursor.fetchone()
                if provenance is None:
                    raise ValueError("article fetch provenance does not match the stored feed item")
                title, published_at = provenance
                cursor.execute(
                    """
                    INSERT INTO ingest.artifacts (
                        id, source_id, canonical_locator, artifact_kind,
                        first_seen_at, last_seen_at
                    ) VALUES (
                        %s, %s, %s, %s::ingest.artifact_kind, now(), now()
                    )
                    ON CONFLICT (source_id, canonical_locator) DO UPDATE SET
                        last_seen_at = now()
                    RETURNING id
                    """,
                    (
                        uuid.uuid4(),
                        source_id,
                        f"v1-article:{source_url}",
                        "json"
                        if media_type == "application/json"
                        else "pdf"
                        if media_type == "application/pdf"
                        else "rss_item"
                        if media_type in {
                            "application/atom+xml",
                            "application/rss+xml",
                            "application/xml",
                        }
                        else "html",
                    ),
                )
                artifact_id = str(cast(tuple[object], cursor.fetchone())[0])
                cursor.execute(
                    """
                    INSERT INTO ingest.artifact_versions (
                        id, artifact_id, source_run_id, stored_object_id,
                        storage_domain, http_status, response_headers,
                        retrieved_at, source_published_at, metadata
                    ) VALUES (
                        %s, %s, %s, %s, 'raw'::core.storage_domain, %s,
                        %s::jsonb, now(), %s, %s::jsonb
                    )
                    ON CONFLICT (artifact_id, stored_object_id) DO UPDATE SET
                        response_headers = EXCLUDED.response_headers
                    RETURNING id
                    """,
                    (
                        uuid.uuid4(),
                        artifact_id,
                        source_run_id,
                        registered.id,
                        response.status_code,
                        json.dumps(headers, sort_keys=True),
                        published_at,
                        json.dumps(
                            {
                                "v1_article_raw": True,
                                "source_url": source_url,
                                "fetched_url": fetched_url,
                                "fetch_method": method or "canonical_url",
                                "feed_document_version_id": str(feed_version_id),
                                "fetch_job_id": str(job_id),
                            },
                            sort_keys=True,
                        ),
                    ),
                )
                artifact_version_id = str(cast(tuple[object], cursor.fetchone())[0])
                cursor.execute(
                    """
                    SELECT id FROM core.document_versions
                     WHERE document_id = %s AND normalized_content_sha256 = %s
                    """,
                    (document_id, registered.content_sha256),
                )
                existing = cursor.fetchone()
                if existing is None:
                    cursor.execute(
                        "SELECT id FROM core.documents WHERE id = %s FOR UPDATE",
                        (document_id,),
                    )
                    cursor.execute(
                        """
                        SELECT coalesce(max(version_no), 0) + 1
                          FROM core.document_versions
                         WHERE document_id = %s
                        """,
                        (document_id,),
                    )
                    version_no = cast(tuple[int], cursor.fetchone())[0]
                    article_version_id = uuid.uuid4()
                    cursor.execute(
                        """
                        INSERT INTO core.document_versions (
                            id, document_id, artifact_version_id, version_no,
                            original_title, source_published_at,
                            normalized_content_sha256, metadata, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                        """,
                        (
                            article_version_id,
                            document_id,
                            artifact_version_id,
                            version_no,
                            title,
                            published_at,
                            registered.content_sha256,
                            json.dumps(
                                {
                                    "v1_article_raw": True,
                                    "source_url": source_url,
                                    "fetched_url": fetched_url,
                                    "fetch_method": method or "canonical_url",
                                    "feed_document_version_id": str(feed_version_id),
                                },
                                sort_keys=True,
                            ),
                        ),
                    )
                else:
                    article_version_id = uuid.UUID(str(existing[0]))
                self._enqueue_extraction_with_cursor(
                    cursor,
                    document_version_id=article_version_id,
                    source_object_id=registered.id,
                    media_type=media_type,
                    extractor=extractor,
                    content_sha256=registered.content_sha256,
                )
                cursor.execute(
                    """
                    SELECT ops.finish_job(
                        %s, %s, %s, 'succeeded'::ops.attempt_outcome,
                        %s, NULL, NULL, NULL
                    )
                    """,
                    (job_id, attempt_id, lease_token, response.status_code),
                )
            self.worker_connection.commit()
        except Exception:
            self.worker_connection.rollback()
            if registered is not None:
                cleanup_unregistered_object(
                    self.worker_connection, self.object_client, registered
                )
                self.worker_connection.commit()
            raise

    def _enqueue_existing_atom_extraction(
        self,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        *,
        source_id: uuid.UUID,
        document_id: uuid.UUID,
        feed_version_id: uuid.UUID,
        source_url: str,
    ) -> None:
        with self.worker_connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT so.id, so.content_sha256, so.media_type
                  FROM core.document_versions AS dv
                  JOIN core.documents AS d ON d.id = dv.document_id
                  JOIN ingest.artifact_versions AS av ON av.id = dv.artifact_version_id
                  JOIN core.stored_objects AS so ON so.id = av.stored_object_id
                 WHERE dv.id = %s AND d.id = %s AND d.source_id = %s
                   AND d.canonical_url = %s
                   AND av.storage_domain = 'raw'::core.storage_domain
                """,
                (feed_version_id, document_id, source_id, source_url),
            )
            row = cursor.fetchone()
            if row is None:
                self.worker_connection.rollback()
                self._finish_invalid(
                    job_id, attempt_id, lease_token, "reddit_atom_provenance_missing"
                )
                return
            source_object_id, content_sha256, media_type = row
            self._enqueue_extraction_with_cursor(
                cursor,
                document_version_id=feed_version_id,
                source_object_id=uuid.UUID(str(source_object_id)),
                media_type=str(media_type),
                extractor=RedditSourceExtractor(),
                content_sha256=str(content_sha256),
            )
            cursor.execute(
                """
                SELECT ops.finish_job(
                    %s, %s, %s, 'succeeded'::ops.attempt_outcome,
                    NULL, NULL, NULL, NULL
                )
                """,
                (job_id, attempt_id, lease_token),
            )
        self.worker_connection.commit()

    def _extract(
        self,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        payload: Mapping[str, object],
    ) -> None:
        handler = ExtractionJobHandler(
            PostgresExtractionStore(self.worker_connection, self.object_client)
        )
        _, result = handler.handle(job_id, attempt_id, lease_token, payload)
        if result.outcome.value == "succeeded":
            self._enqueue_analysis(result.request.document_version_id, ModelTaskType.CLASSIFICATION)
        elif (
            result.request.extractor_name == RedditSourceExtractor.name
            and result.error_code == "reddit_body_unavailable"
        ):
            self._enqueue_reddit_fallback(result.request.document_version_id)

    def _enqueue_reddit_fallback(self, document_version_id: uuid.UUID) -> None:
        with self.worker_connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT sr.id, d.source_id, d.id, d.canonical_url, dv.id,
                       so.content_sha256,
                       scv.configuration ->> 'fallback',
                       dv.metadata ->> 'fetch_method'
                  FROM core.document_versions AS dv
                  JOIN core.documents AS d ON d.id = dv.document_id
                  JOIN ingest.artifact_versions AS av ON av.id = dv.artifact_version_id
                  JOIN core.stored_objects AS so ON so.id = av.stored_object_id
                  JOIN ingest.source_runs AS sr ON sr.id = av.source_run_id
                  JOIN ingest.source_config_versions AS scv
                    ON scv.id = sr.source_config_version_id
                 WHERE dv.id = %s
                """,
                (document_version_id,),
            )
            row = cursor.fetchone()
            if row is None:
                self.worker_connection.rollback()
                return
            (
                source_run_id,
                source_id,
                document_id,
                source_url,
                feed_version_id,
                feed_sha,
                fallback,
                current_fetch_method,
            ) = row
            if fallback != "reddit_official_api" or current_fetch_method == fallback:
                self.worker_connection.rollback()
                return
            job_payload = {
                "source_run_id": str(source_run_id),
                "source_id": str(source_id),
                "document_id": str(document_id),
                "feed_document_version_id": str(feed_version_id),
                "source_url": str(source_url),
                "document_fetch_method": "reddit_official_api",
                "payload_schema_version": "v1.article-fetch.v1",
            }
            cursor.execute(
                """
                SELECT ops.enqueue_job(
                    'fetch_source', %s::jsonb, 'v1.article-fetch.v1',
                    %s, 0::smallint, now(), 3, 120
                )
                """,
                (
                    json.dumps(job_payload, sort_keys=True),
                    f"v1-reddit-api:{document_id}:{feed_sha}",
                ),
            )
        self.worker_connection.commit()

    def _enqueue_analysis(
        self, document_version_id: uuid.UUID, task_type: ModelTaskType, *, suffix: str = "auto"
    ) -> str:
        with self.model_connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id FROM ops.prompt_versions
                 WHERE task_type = %s::ops.model_task_type AND active
                """,
                (task_type.value,),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError(f"active prompt missing for {task_type.value}")
            prompt_id = str(row[0])
        self.model_connection.rollback()
        with self.worker_connection.cursor() as cursor:
            payload = {
                "document_version_id": str(document_version_id),
                "prompt_version_id": prompt_id,
                "task_type": task_type.value,
                "provider": "deepseek",
                "model": "deepseek-flash",
                "payload_schema_version": "model.v1",
            }
            key = f"v1-model:{document_version_id}:{task_type.value}:{prompt_id}:{suffix}"
            cursor.execute(
                """
                SELECT ops.enqueue_job(
                    'analyze_document', %s::jsonb, 'model.v1', %s,
                    0::smallint, now(), 3, 60
                )
                """,
                (json.dumps(payload, sort_keys=True), key),
            )
            job_id = str(cast(tuple[object], cursor.fetchone())[0])
        self.worker_connection.commit()
        return job_id

    def _analyze(
        self,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        payload: Mapping[str, object],
    ) -> None:
        run_id = self._model_handler.handle(job_id, attempt_id, lease_token, payload)
        if _string(payload, "task_type") != ModelTaskType.CLASSIFICATION.value:
            return
        with self.model_connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ar.result
                  FROM core.analysis_results AS ar
                 WHERE ar.model_run_id = %s
                   AND ar.result_type = 'classification'::ops.model_task_type
                   AND ar.validation_status = 'valid'::core.validation_status
                """,
                (run_id,),
            )
            row = cast(tuple[dict[str, object]] | None, cursor.fetchone())
        self.model_connection.rollback()
        relevance = row[0].get("relevance") if row is not None else None
        if not isinstance(relevance, Mapping) or relevance.get("decision") == "irrelevant":
            return
        document_version_id = _uuid(payload, "document_version_id")
        for task_type in _ANALYSIS_ORDER:
            self._enqueue_analysis(document_version_id, task_type)

    def _finish_invalid(
        self,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        code: str,
    ) -> None:
        with self.worker_connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ops.finish_job(
                    %s, %s, %s, 'terminal_failure'::ops.attempt_outcome,
                    422, %s, 'V1 worker rejected the job payload', NULL
                )
                """,
                (job_id, attempt_id, lease_token, code),
            )
        self.worker_connection.commit()


def main() -> None:
    logging.basicConfig(level=os.environ.get("UAP_LOG_LEVEL", "INFO"))
    worker_url = os.environ.get("UAP_V1_WORKER_DATABASE_URL")
    model_url = os.environ.get("UAP_V1_MODEL_DATABASE_URL")
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not worker_url or not model_url or not api_key:
        raise SystemExit(
            "UAP_V1_WORKER_DATABASE_URL, UAP_V1_MODEL_DATABASE_URL and "
            "DEEPSEEK_API_KEY are required"
        )
    config = load_v1_config()
    if str(config.deepseek_monthly_budget_cny) != "20.00":
        raise SystemExit("unapproved DeepSeek budget")
    worker_dsn = worker_url.replace("postgresql+psycopg://", "postgresql://", 1)
    model_dsn = model_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with (
        psycopg.connect(worker_dsn) as worker_connection,
        psycopg.connect(model_dsn) as model_connection,
    ):
        object_client = cast(ObjectClient, build_client_from_environment(worker_url))
        worker = V1Worker(
            worker_connection,
            model_connection,
            object_client,
            DeepSeekProvider(api_key=api_key),
            RedditOfficialApiClient(
                access_token=os.environ.get("REDDIT_OAUTH_ACCESS_TOKEN"),
                user_agent=os.environ.get("REDDIT_USER_AGENT"),
            ),
        )
        while True:
            if worker.run_once() is None:
                time.sleep(2)


def build_client_from_environment(worker_url: str) -> Any:
    """Use shared object settings while keeping the worker DSN out of logs."""

    from pydantic import SecretStr

    from uap_platform.config import Settings

    settings = Settings(database_url=SecretStr(worker_url))  # type: ignore[call-arg]
    return build_client(settings)
