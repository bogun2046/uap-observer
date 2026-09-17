"""Internal V1 library queries and audited manual reanalysis command."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import cast
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from uap_platform.model_governance import ModelTaskType
from uap_platform.object_registry import ObjectClient, read_verified_object

from .bootstrap import BOOTSTRAP_PRINCIPAL_ID

MONTHLY_BUDGET_MICRO_CNY = 20_000_000
MONTHLY_WARNING_MICRO_CNY = 16_000_000
MAX_PROVIDER_CALLS_PER_DOCUMENT_VERSION = 12


def _dsn(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


class InternalLibrary:
    def __init__(
        self,
        read_database_url: str,
        worker_database_url: str,
        model_database_url: str,
        object_client: ObjectClient,
    ) -> None:
        self.read_database_url = _dsn(read_database_url)
        self.worker_database_url = _dsn(worker_database_url)
        self.model_database_url = _dsn(model_database_url)
        self.object_client = object_client

    def list_documents(self, *, query: str | None = None, limit: int = 50) -> dict[str, object]:
        limit = max(1, min(limit, 100))
        search = None if not query else f"%{query.strip()}%"
        with psycopg.connect(self.read_database_url, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT d.id AS document_id, dv.id AS document_version_id,
                           dv.version_no, d.canonical_url, d.first_seen_at, d.last_seen_at,
                           dv.original_title, dv.source_published_at,
                           dv.normalized_content_sha256 AS raw_sha256,
                           s.slug AS source_slug, s.name AS source_name,
                           e.id AS extraction_id, e.outcome AS extraction_outcome,
                           e.error_code AS extraction_error_code, e.text_object_id
                      FROM core.documents AS d
                      JOIN ingest.sources AS s ON s.id = d.source_id
                      JOIN LATERAL (
                          SELECT item.* FROM core.document_versions AS item
                           WHERE item.document_id = d.id
                           ORDER BY coalesce(
                               (item.metadata ->> 'v1_article_raw')::boolean, false
                           ) DESC, item.version_no DESC LIMIT 1
                      ) AS dv ON true
                      LEFT JOIN LATERAL (
                          SELECT item.* FROM core.extractions AS item
                           WHERE item.document_version_id = dv.id
                           ORDER BY item.created_at DESC, item.id DESC LIMIT 1
                      ) AS e ON true
                     WHERE d.deleted_at IS NULL
                       AND (%s::text IS NULL OR dv.original_title ILIKE %s OR s.name ILIKE %s)
                     ORDER BY coalesce(dv.source_published_at, d.first_seen_at) DESC, d.id DESC
                     LIMIT %s
                    """,
                    (search, search, search, limit),
                )
                items = [dict(row) for row in cursor.fetchall()]
        self._attach_analysis(items)
        return {"items": [self._decorate(item) for item in items], "count": len(items)}

    def get_document(self, document_id: uuid.UUID) -> dict[str, object] | None:
        listing = self._base_document(document_id)
        if listing is None:
            return None
        self._attach_analysis([listing])
        text_object_id = listing.get("text_object_id")
        if text_object_id is not None:
            with psycopg.connect(self.read_database_url, row_factory=dict_row) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT bucket_name, object_key, content_sha256, byte_length
                          FROM core.stored_objects WHERE id = %s
                        """,
                        (text_object_id,),
                    )
                    stored = cursor.fetchone()
            if stored is not None:
                data = read_verified_object(
                    self.object_client,
                    str(stored["bucket_name"]),
                    str(stored["object_key"]),
                    str(stored["content_sha256"]),
                    int(stored["byte_length"]),
                )
                listing["source_text"] = data.decode("utf-8", errors="replace")
        return self._decorate(listing)

    def _base_document(self, document_id: uuid.UUID) -> dict[str, object] | None:
        with psycopg.connect(self.read_database_url, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT d.id AS document_id, dv.id AS document_version_id,
                           dv.version_no, d.canonical_url, d.first_seen_at, d.last_seen_at,
                           dv.original_title, dv.source_published_at,
                           dv.normalized_content_sha256 AS raw_sha256,
                           s.slug AS source_slug, s.name AS source_name,
                           e.id AS extraction_id, e.outcome AS extraction_outcome,
                           e.error_code AS extraction_error_code, e.text_object_id
                      FROM core.documents AS d
                      JOIN ingest.sources AS s ON s.id = d.source_id
                      JOIN LATERAL (
                          SELECT item.* FROM core.document_versions AS item
                           WHERE item.document_id = d.id
                           ORDER BY coalesce(
                               (item.metadata ->> 'v1_article_raw')::boolean, false
                           ) DESC, item.version_no DESC LIMIT 1
                      ) AS dv ON true
                      LEFT JOIN LATERAL (
                          SELECT item.* FROM core.extractions AS item
                           WHERE item.document_version_id = dv.id
                           ORDER BY item.created_at DESC, item.id DESC LIMIT 1
                      ) AS e ON true
                     WHERE d.id = %s
                    """,
                    (document_id,),
                )
                row = cursor.fetchone()
        return None if row is None else dict(row)

    def _attach_analysis(self, items: list[dict[str, object]]) -> None:
        version_ids = [item["document_version_id"] for item in items]
        if not version_ids:
            return
        with psycopg.connect(self.model_database_url, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT ON (document_version_id, result_type)
                           document_version_id, result_type, result
                      FROM core.analysis_results
                     WHERE document_version_id = ANY(%s)
                       AND validation_status = 'valid'::core.validation_status
                     ORDER BY document_version_id, result_type, created_at DESC, id DESC
                    """,
                    (version_ids,),
                )
                analysis_rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT DISTINCT ON (document_version_id)
                           document_version_id, id AS model_run_id, status AS latest_model_status,
                           error_code AS latest_model_error_code, provider AS latest_model_provider,
                           model AS latest_model, started_at AS latest_model_started_at,
                           finished_at AS latest_model_finished_at, input_tokens,
                           output_tokens, cost_minor_units, currency, response_object_id
                      FROM ops.model_runs
                     WHERE document_version_id = ANY(%s)
                     ORDER BY document_version_id, started_at DESC, id DESC
                    """,
                    (version_ids,),
                )
                model_rows = cursor.fetchall()
        by_version = {str(item["document_version_id"]): item for item in items}
        for row in analysis_rows:
            target = by_version.get(str(row["document_version_id"]))
            if target is not None:
                target[str(row["result_type"])] = row["result"]
        for row in model_rows:
            target = by_version.get(str(row["document_version_id"]))
            if target is not None:
                target.update(dict(row))
                target["latest_billing"] = self._billing_for_object(row["response_object_id"])

    def _billing_for_object(self, object_id: object) -> dict[str, object] | None:
        if object_id is None:
            return None
        with psycopg.connect(self.model_database_url, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT bucket_name, object_key, content_sha256, byte_length
                      FROM core.stored_objects WHERE id = %s
                    """,
                    (object_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        data = read_verified_object(
            self.object_client,
            str(row["bucket_name"]),
            str(row["object_key"]),
            str(row["content_sha256"]),
            int(row["byte_length"]),
        )
        try:
            value = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        billing = value.get("uap_billing") if isinstance(value, dict) else None
        return cast(dict[str, object] | None, billing if isinstance(billing, dict) else None)

    def usage(self) -> dict[str, object]:
        start, end = _month_bounds(datetime.now(UTC))
        with psycopg.connect(self.model_database_url, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, started_at, provider, model, currency, input_tokens,
                           output_tokens, cost_minor_units, response_object_id
                      FROM ops.model_runs
                     WHERE started_at >= %s AND started_at < %s
                     ORDER BY started_at
                    """,
                    (start, end),
                )
                rows = [dict(row) for row in cursor.fetchall()]
        daily: defaultdict[tuple[str, str, str, str], dict[str, object]] = defaultdict(dict)
        monthly_micro = 0
        for row in rows:
            billing = self._billing_for_object(row.get("response_object_id"))
            micro = (
                int(cast(int | str, billing["cost_microunits"]))
                if billing and "cost_microunits" in billing
                else int(cast(int | str, row.get("cost_minor_units") or 0)) * 10_000
            )
            monthly_micro += micro
            day = cast(datetime, row["started_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date()
            key = (str(day), str(row["provider"]), str(row["model"]), str(row["currency"]))
            item = daily[key]
            item.update(
                usage_date=key[0], provider=key[1], model=key[2], currency=key[3]
            )
            item["call_count"] = int(cast(int | str, item.get("call_count", 0))) + 1
            item["input_tokens"] = int(
                cast(int | str, item.get("input_tokens", 0))
            ) + int(
                cast(int | str, row.get("input_tokens") or 0)
            )
            item["output_tokens"] = int(
                cast(int | str, item.get("output_tokens", 0))
            ) + int(
                cast(int | str, row.get("output_tokens") or 0)
            )
            item["cost_microunits"] = int(
                cast(int | str, item.get("cost_microunits", 0))
            ) + micro
        return {
            "currency": "CNY",
            "monthly_budget_microunits": MONTHLY_BUDGET_MICRO_CNY,
            "monthly_warning_microunits": MONTHLY_WARNING_MICRO_CNY,
            "monthly_cost_microunits": monthly_micro,
            "warning": monthly_micro >= MONTHLY_WARNING_MICRO_CNY,
            "blocked": monthly_micro >= MONTHLY_BUDGET_MICRO_CNY,
            "daily": sorted(
                daily.values(), key=lambda item: str(item["usage_date"]), reverse=True
            ),
        }

    def request_reanalysis(
        self,
        document_id: uuid.UUID,
        task_type: ModelTaskType,
        reason: str,
        request_id: uuid.UUID,
    ) -> dict[str, object]:
        clean_reason = reason.strip()
        if not clean_reason or len(clean_reason) > 2_000:
            raise ValueError("reason must contain 1-2000 characters")
        if task_type is ModelTaskType.TRANSLATION:
            raise ValueError("translation is not part of the V1-1 analysis chain")
        usage = self.usage()
        if bool(usage["blocked"]):
            raise RuntimeError("monthly_model_budget_exhausted")
        with psycopg.connect(self.worker_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT dv.id FROM core.document_versions AS dv
                      JOIN core.extractions AS e ON e.document_version_id = dv.id
                     WHERE dv.document_id = %s
                       AND e.outcome = 'succeeded'::core.extraction_outcome
                     ORDER BY dv.version_no DESC, e.created_at DESC LIMIT 1
                    """,
                    (document_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise LookupError("document_not_ready")
        document_version_id = str(row[0])
        with psycopg.connect(self.model_database_url) as model_connection:
            with model_connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM ops.model_runs WHERE document_version_id = %s",
                    (document_version_id,),
                )
                call_count = cast(tuple[int], cursor.fetchone())[0]
                if call_count >= MAX_PROVIDER_CALLS_PER_DOCUMENT_VERSION:
                    raise RuntimeError("article_model_call_budget_exhausted")
                cursor.execute(
                    """
                    SELECT id FROM ops.prompt_versions
                     WHERE task_type = %s::ops.model_task_type AND active
                    """,
                    (task_type.value,),
                )
                prompt = cursor.fetchone()
        if prompt is None:
            raise LookupError("active_prompt_missing")
        with psycopg.connect(self.worker_database_url) as connection:
            with connection.cursor() as cursor:
                payload = {
                    "document_version_id": document_version_id,
                    "prompt_version_id": str(prompt[0]),
                    "task_type": task_type.value,
                    "provider": "deepseek",
                    "model": "deepseek-flash",
                    "payload_schema_version": "model.v1",
                }
                key = f"v1-manual-model:{document_version_id}:{task_type.value}:{request_id}"
                cursor.execute(
                    """
                    SELECT ops.enqueue_job(
                        'analyze_document', %s::jsonb, 'model.v1', %s,
                        10::smallint, now(), 3, 60
                    )
                    """,
                    (json.dumps(payload, sort_keys=True), key),
                )
                job_id = str(cast(tuple[object], cursor.fetchone())[0])
                metadata = {
                    "task_type": task_type.value,
                    "reason": clean_reason,
                    "job_id": job_id,
                    "budget_cost_microunits_before": usage["monthly_cost_microunits"],
                }
                digest = hashlib.sha256(
                    json.dumps(metadata, sort_keys=True).encode("utf-8")
                ).hexdigest()
                cursor.execute(
                    """
                    INSERT INTO audit.audit_events (
                        id, event_key, actor_id, action, target_type, target_id,
                        request_id, after_digest, metadata, occurred_at
                    ) VALUES (
                        %s, %s, %s, 'v1.reanalysis.requested', 'document', %s,
                        %s, %s, %s::jsonb, %s
                    )
                    """,
                    (
                        uuid.uuid4(),
                        f"v1-reanalysis:{request_id}",
                        BOOTSTRAP_PRINCIPAL_ID,
                        document_id,
                        request_id,
                        digest,
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        datetime.now(UTC),
                    ),
                )
            connection.commit()
        return {"job_id": job_id, "request_id": str(request_id), "status": "queued"}

    @staticmethod
    def _decorate(row: dict[str, object]) -> dict[str, object]:
        classification = row.get("classification")
        summary = row.get("summary")
        if isinstance(summary, dict):
            row["summary_text"] = summary.get("summary")
        if row.get("extraction_outcome") != "succeeded":
            state = "extraction_failed"
        elif row.get("latest_model_status") in {"failed", "invalid"}:
            state = "analysis_failed"
        elif (
            isinstance(classification, dict)
            and isinstance(classification.get("relevance"), dict)
            and cast(dict[str, object], classification["relevance"]).get("decision")
            == "irrelevant"
        ):
            state = "not_relevant"
        elif row.get("summary") and row.get("claim_extraction"):
            state = "analysis_ready"
        elif row.get("latest_model_status") == "succeeded":
            state = "analysis_partial"
        else:
            state = "analysis_pending"
        row["claims"] = row.get("claim_extraction")
        row["entities"] = row.get("entity_extraction")
        row["internal_state"] = state
        row["public_authorized"] = False
        return row
