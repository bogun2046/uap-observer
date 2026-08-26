"""G8-16C: resolve_relations is never queued; mis-claim finishes terminal_failure."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

_PLATFORM_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLATFORM_ROOT / "src"
for _path in (str(_PLATFORM_ROOT), str(_SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import psycopg  # noqa: E402

from tools.wp8_1_runtime_probe import (  # noqa: E402
    connect,
    execute,
    insert_analysis,
    insert_model_run,
    one,
    require,
    scalar,
    seed_document,
    sha256_text,
)
from uap_platform.knowledge.job_types import claimable_job_types  # noqa: E402
from uap_platform.knowledge.reasons import KNOWLEDGE_RELATION_TASK_NOT_IN_WP8  # noqa: E402
from uap_platform.knowledge.worker import KnowledgeJobDispatcher  # noqa: E402

CURRENT_HEAD = "0016_review_decisions_and_grants"


def g8_16c(
    admin: psycopg.Connection[Any], worker: psycopg.Connection[Any], tag: str
) -> dict[str, Any]:
    _principal, document_version_id, _source = seed_document(admin, tag)
    input_hash = sha256_text(f"wp8-6-{tag}")
    relations_before = scalar(admin, "SELECT count(*) FROM core.relations")
    claim_run = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="claim_extraction",
        input_sha256=input_hash,
        tag=f"{tag}-claim",
    )
    entity_run = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="entity_extraction",
        input_sha256=input_hash,
        tag=f"{tag}-entity",
    )
    insert_analysis(
        admin,
        model_run_id=claim_run,
        document_version_id=document_version_id,
        result_type="claim_extraction",
        result={"claims": []},
    )
    insert_analysis(
        admin,
        model_run_id=entity_run,
        document_version_id=document_version_id,
        result_type="entity_extraction",
        result={"entities": []},
    )
    require(
        "g8-16c no relation jobs from analysis",
        scalar(admin, "SELECT count(*) FROM ops.jobs WHERE job_type='resolve_relations'"),
        0,
    )
    require(
        "g8-16c claimable set omits relations",
        "resolve_relations"
        not in claimable_job_types(claims_handler_active=True, entities_handler_active=True),
        True,
    )

    job_id = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO ops.jobs (
            id, job_type, payload, payload_schema_version, idempotency_key,
            priority, available_at, max_attempts, timeout_seconds
        ) VALUES (
            %s, 'resolve_relations', '{}'::jsonb, 'knowledge.v2', %s, 0, now(), 8, 60
        )
        """,
        job_id,
        f"resolve-relations:{tag}",
    )
    with worker.cursor() as cursor:
        cursor.execute(
            """
            SELECT job_id, attempt_id, job_type, payload, lease_token
              FROM ops.claim_job('worker', %s, %s::text[], 60)
            """,
            (f"wp8-6-misclaim-{tag}", ["resolve_relations"]),
        )
        claimed = cursor.fetchone()
    worker.commit()
    require("g8-16c misclaim leased", claimed is not None, True)
    if claimed is None:
        raise RuntimeError("g8-16c misclaim leased")
    require("g8-16c misclaim type", claimed[2], "resolve_relations")
    dispatcher = KnowledgeJobDispatcher(worker)
    status = dispatcher.dispatch(tuple(claimed))
    require("g8-16c finish status not succeeded", status != "succeeded", True)
    job_status, attempt_outcome, error_code = one(
        admin,
        """
        SELECT jobs.status::text, attempts.outcome::text, attempts.error_code
          FROM ops.jobs AS jobs
          JOIN ops.job_attempts AS attempts ON attempts.job_id = jobs.id
         WHERE jobs.id = %s
         ORDER BY attempts.attempt_no DESC
         LIMIT 1
        """,
        job_id,
    )
    require("g8-16c job dead", job_status, "dead")
    require("g8-16c attempt terminal_failure", attempt_outcome, "terminal_failure")
    require("g8-16c error code", error_code, KNOWLEDGE_RELATION_TASK_NOT_IN_WP8)
    require(
        "g8-16c frozen token",
        KNOWLEDGE_RELATION_TASK_NOT_IN_WP8,
        "knowledge_relation_task_not_in_wp8",
    )
    require(
        "g8-16c relations unchanged",
        scalar(admin, "SELECT count(*) FROM core.relations"),
        relations_before,
    )
    require(
        "g8-16c no succeeded relation jobs",
        scalar(
            admin,
            """
            SELECT count(*) FROM ops.jobs
             WHERE job_type='resolve_relations' AND status='succeeded'
            """,
        ),
        0,
    )
    return {"passed": True, "job_id": str(job_id), "status": status}


def main() -> None:
    tag = uuid.uuid4().hex
    admin = connect()
    worker = connect("uap_worker")
    head = scalar(admin, "SELECT version_num FROM public.alembic_version")
    require("alembic head", head, CURRENT_HEAD)
    table_count = scalar(
        admin,
        """
        SELECT count(*) FROM pg_tables
         WHERE schemaname IN ('ingest','core','ops','audit','public')
           AND tablename <> 'alembic_version'
        """,
    )
    require("table count remains 50", table_count, 50)
    result = g8_16c(admin, worker, tag)
    print(result)
    print("WP8.6 runtime probe passed: G8-16C")


if __name__ == "__main__":
    main()
