"""WP9.6 runtime probe: G9-23, G9-24, G9-25, G9-32, G9-33, G9-38 on uap_api."""

from __future__ import annotations

import json
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
    insert_analysis,
    insert_model_run,
    insert_span,
    insert_supported_ai_claim,
    seed_document,
    sha256_text,
)
from tools.wp9_2_runtime_probe import (  # noqa: E402
    EXPECTED_TABLE_COUNT,
    bind,
    bind_role,
    connect,
    execute,
    insert_person,
    open_sql,
    require,
    scalar,
    seed_grantor,
    sqlerror_tx,
)
from tools.wp9_4_runtime_probe import insert_active_entity  # noqa: E402
from tools.wp9_5_runtime_probe import call_api, event_count, merge_sql  # noqa: E402
from uap_platform.knowledge.job_types import claimable_job_types  # noqa: E402
from uap_platform.knowledge.reasons import KNOWLEDGE_RELATION_TASK_NOT_IN_WP8  # noqa: E402
from uap_platform.knowledge.worker import KnowledgeJobDispatcher  # noqa: E402

CURRENT_HEAD = "0019_manual_claims_binding"
EVENT_KEY_LOCK_CLASS = 9175
REASON = "authorize claim subject binding"
MANUAL_TEXT = "a visible metallic craft hovered above the field"
CREATE_MANUAL = (
    "audit.create_manual_claim(uuid,text,core.claim_type,core.assertion_status,text,uuid[])"
)
PRIVATE_BIND = "audit._apply_claim_subject_bind(uuid,uuid,uuid)"
PRIVATE_REPLACE = "audit._replace_claim_evidence(uuid,uuid,uuid[])"
PRIVATE_RETIRE = "audit._retire_manual_claim_supports(uuid,uuid)"
CORE_MERGE = "core.merge_entities(uuid,uuid,uuid,text)"


def manual_sql(
    document_version_id: uuid.UUID,
    claim_text: str,
    attribution: str | None,
    span_ids: list[uuid.UUID],
    claim_type: str = "observation",
    assertion_status: str = "reported",
) -> tuple[str, tuple[object, ...]]:
    return (
        """
        SELECT audit.create_manual_claim(
            %s, %s, %s::core.claim_type, %s::core.assertion_status, %s, %s::uuid[]
        )
        """,
        (
            document_version_id,
            claim_text,
            claim_type,
            assertion_status,
            attribution,
            span_ids,
        ),
    )


def decide_sql(
    case_id: uuid.UUID,
    decision: str,
    changes: dict[str, object] | None = None,
    reason: str = REASON,
) -> tuple[str, tuple[object, ...]]:
    payload = "{}" if changes is None else json.dumps(changes, separators=(",", ":"))
    return (
        """
        SELECT audit.record_review_decision(
            %s, %s::audit.review_decision, %s, %s::jsonb
        )
        """,
        (case_id, decision, reason, payload),
    )


def has_execute(admin: psycopg.Connection[Any], role: str, signature: str) -> bool:
    return bool(
        scalar(
            admin,
            "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
            role,
            signature,
        )
    )


def permissions(admin: psycopg.Connection[Any]) -> None:
    require("api execute create_manual_claim", has_execute(admin, "uap_api", CREATE_MANUAL), True)
    for role in (
        "uap_worker",
        "uap_publisher",
        "uap_scheduler",
        "uap_model_governance",
        "uap_public_reader",
    ):
        require(f"{role} no create_manual_claim", has_execute(admin, role, CREATE_MANUAL), False)
    for signature in (PRIVATE_BIND, PRIVATE_REPLACE, PRIVATE_RETIRE, CORE_MERGE):
        for role in (
            "uap_api",
            "uap_worker",
            "uap_publisher",
            "uap_scheduler",
            "uap_model_governance",
            "uap_public_reader",
        ):
            require(f"{role} no {signature}", has_execute(admin, role, signature), False)
    require(
        "no public bind function",
        scalar(
            admin,
            """
            SELECT to_regprocedure('audit.bind_claim_subject_entity(uuid, uuid)') IS NULL
               AND to_regprocedure('core.bind_claim_subject_entity(uuid, uuid)') IS NULL
               AND to_regprocedure('core.create_manual_claim(uuid, text)') IS NULL
            """,
        ),
        True,
    )


def g9_23(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    author: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    _principal, document_id, _source = seed_document(admin, f"g923-{uuid.uuid4().hex[:8]}")
    span = insert_span(admin, document_id, sha256_text(f"g923-{document_id}"))
    empty_request = uuid.uuid4()
    state, primary = sqlerror_tx(
        api,
        [*bind(author, empty_request), manual_sql(document_id, MANUAL_TEXT, "witness", [])],
    )
    require("g9-23 empty sqlstate", state, "23514")
    require("g9-23 empty token", primary, "manual_claim_requires_supports")
    require(
        "g9-23 empty no event_key",
        event_count(admin, f"review.claim.manual:{empty_request}"),
        0,
    )
    request = uuid.uuid4()
    claim_id = call_api(
        api,
        author,
        request,
        manual_sql(document_id, MANUAL_TEXT, "witness", [span]),
    )
    origin, ordinal, created_by, fingerprint = (
        scalar(admin, "SELECT origin_analysis_result_id FROM core.claims WHERE id = %s", claim_id),
        scalar(admin, "SELECT ordinal FROM core.claims WHERE id = %s", claim_id),
        scalar(admin, "SELECT created_by FROM core.claims WHERE id = %s", claim_id),
        scalar(admin, "SELECT claim_fingerprint FROM core.claims WHERE id = %s", claim_id),
    )
    db_fp = scalar(admin, "SELECT core.compute_claim_fingerprint(%s)", MANUAL_TEXT)
    require("g9-23 origin null", origin, None)
    require("g9-23 ordinal null", ordinal, None)
    require("g9-23 created_by", created_by, author)
    require("g9-23 fingerprint", fingerprint, db_fp)
    require("g9-23 supports", scalar(
        admin,
        """
        SELECT count(*) FROM core.claim_evidence
         WHERE claim_id = %s AND support_type = 'supports'
        """,
        claim_id,
    ), 1)
    require("g9-23 event", event_count(admin, f"review.claim.manual:{request}"), 1)
    evidence_id = scalar(
        admin,
        "SELECT id FROM core.claim_evidence WHERE claim_id = %s",
        claim_id,
    )
    owner_state, owner_primary = sqlerror_tx(
        admin,
        [("DELETE FROM core.claim_evidence WHERE claim_id = %s", (claim_id,))],
    )
    require("g9-23 last support sqlstate", owner_state, "23514")
    require("g9-23 last support token", owner_primary, "manual_claim_requires_supports")
    with admin.transaction():
        execute(admin, "DELETE FROM core.claim_evidence WHERE claim_id = %s", claim_id)
        execute(
            admin,
            """
            INSERT INTO core.claim_evidence (
                id, claim_id, evidence_span_id, document_version_id, support_type
            ) VALUES (%s, %s, %s, %s, 'supports')
            """,
            evidence_id,
            claim_id,
            span,
            document_id,
        )
    require(
        "g9-23 restored supports",
        scalar(
            admin,
            """
            SELECT count(*) FROM core.claim_evidence
             WHERE claim_id = %s AND support_type = 'supports'
            """,
            claim_id,
        ),
        1,
    )
    return document_id, span, claim_id


def g9_24(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    author: uuid.UUID,
    senior: uuid.UUID,
    document_id: uuid.UUID,
    span: uuid.UUID,
) -> uuid.UUID:
    claim_id = call_api(
        api,
        author,
        uuid.uuid4(),
        manual_sql(document_id, f"{MANUAL_TEXT} bind", "witness", [span]),
    )
    primary = insert_active_entity(admin, "Bind Primary")
    merged_source = insert_active_entity(admin, "Bind Merged Source")
    relations_before = int(scalar(admin, "SELECT count(*) FROM core.relations"))
    case_id = call_api(api, senior, uuid.uuid4(), open_sql("claim", claim_id, REASON))
    call_api(
        api,
        senior,
        uuid.uuid4(),
        decide_sql(case_id, "approve", {"bind_subject_entity_id": str(primary)}),
    )
    require(
        "g9-24 bound",
        scalar(admin, "SELECT subject_entity_id FROM core.claims WHERE id = %s", claim_id),
        primary,
    )
    call_api(api, senior, uuid.uuid4(), merge_sql(merged_source, primary))
    state, code = sqlerror_tx(
        api,
        [
            *bind(senior, uuid.uuid4()),
            decide_sql(case_id, "revise", {"bind_subject_entity_id": str(merged_source)}),
        ],
    )
    require("g9-24 merged sqlstate", state, "22023")
    require("g9-24 merged token", code, "review_subject_not_canonical")
    require(
        "g9-24 still primary",
        scalar(admin, "SELECT subject_entity_id FROM core.claims WHERE id = %s", claim_id),
        primary,
    )
    require(
        "g9-24 no relations",
        int(scalar(admin, "SELECT count(*) FROM core.relations")),
        relations_before,
    )
    return claim_id


def g9_25(admin: psycopg.Connection[Any], worker: psycopg.Connection[Any]) -> None:
    tag = uuid.uuid4().hex[:12]
    _principal, document_version_id, _source = seed_document(admin, f"g925-{tag}")
    input_hash = sha256_text(f"wp9-6-{tag}")
    relations_before = scalar(admin, "SELECT count(*) FROM core.relations")
    jobs_before = int(
        scalar(admin, "SELECT count(*) FROM ops.jobs WHERE job_type = 'resolve_relations'")
    )
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
        "g9-25 analysis enqueued no relation jobs",
        int(scalar(admin, "SELECT count(*) FROM ops.jobs WHERE job_type = 'resolve_relations'")),
        jobs_before,
    )
    require(
        "g9-25 claimable omits relations",
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
        f"resolve-relations:wp96:{tag}",
    )
    with worker.cursor() as cursor:
        cursor.execute(
            """
            SELECT job_id, attempt_id, job_type, payload, lease_token
              FROM ops.claim_job('worker', %s, %s::text[], 60)
            """,
            (f"wp9-6-misclaim-{tag}", ["resolve_relations"]),
        )
        claimed = cursor.fetchone()
    worker.commit()
    require("g9-25 misclaim leased", claimed is not None, True)
    if claimed is None:
        raise RuntimeError("g9-25 misclaim leased: expected a job row")
    dispatcher = KnowledgeJobDispatcher(worker)
    status = dispatcher.dispatch(tuple(claimed))
    require("g9-25 not succeeded", status != "succeeded", True)
    job_status, attempt_outcome, error_code = (
        scalar(
            admin,
            """
            SELECT jobs.status::text
              FROM ops.jobs AS jobs
             WHERE jobs.id = %s
            """,
            job_id,
        ),
        scalar(
            admin,
            """
            SELECT attempts.outcome::text
              FROM ops.job_attempts AS attempts
             WHERE attempts.job_id = %s
             ORDER BY attempts.attempt_no DESC
             LIMIT 1
            """,
            job_id,
        ),
        scalar(
            admin,
            """
            SELECT attempts.error_code
              FROM ops.job_attempts AS attempts
             WHERE attempts.job_id = %s
             ORDER BY attempts.attempt_no DESC
             LIMIT 1
            """,
            job_id,
        ),
    )
    require("g9-25 job dead", job_status, "dead")
    require("g9-25 terminal_failure", attempt_outcome, "terminal_failure")
    require("g9-25 error", error_code, KNOWLEDGE_RELATION_TASK_NOT_IN_WP8)
    require(
        "g9-25 relations unchanged",
        scalar(admin, "SELECT count(*) FROM core.relations"),
        relations_before,
    )
    require(
        "g9-25 no succeeded relation jobs",
        scalar(
            admin,
            """
            SELECT count(*) FROM ops.jobs
             WHERE job_type='resolve_relations' AND status='succeeded'
            """,
        ),
        0,
    )


def g9_32(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    author: uuid.UUID,
    senior: uuid.UUID,
    document_id: uuid.UUID,
    span: uuid.UUID,
) -> None:
    claim_id = call_api(
        api,
        author,
        uuid.uuid4(),
        manual_sql(document_id, f"{MANUAL_TEXT} g932", "witness", [span]),
    )
    entity = insert_active_entity(admin, "G932 Entity")
    case_id = call_api(api, senior, uuid.uuid4(), open_sql("claim", claim_id, REASON))
    decision_id = call_api(
        api,
        senior,
        uuid.uuid4(),
        decide_sql(case_id, "approve", {"bind_subject_entity_id": str(entity)}),
    )
    bound = scalar(admin, "SELECT subject_entity_id FROM core.claims WHERE id = %s", claim_id)
    evidence = int(
        scalar(admin, "SELECT count(*) FROM core.claim_evidence WHERE claim_id = %s", claim_id)
    )
    other = insert_active_entity(admin, "G932 Other")
    bind_state, _bind_primary = sqlerror_tx(
        api,
        [
            *bind(senior, uuid.uuid4()),
            (
                "SELECT audit._apply_claim_subject_bind(%s, %s, %s)",
                (case_id, decision_id, other),
            ),
        ],
    )
    require("g9-32 private bind sqlstate", bind_state, "42501")
    retire_state, _retire_primary = sqlerror_tx(
        api,
        [
            *bind(senior, uuid.uuid4()),
            ("SELECT audit._retire_manual_claim_supports(%s, %s)", (case_id, decision_id)),
        ],
    )
    require("g9-32 private retire sqlstate", retire_state, "42501")
    update_state, _update_primary = sqlerror_tx(
        api,
        [
            (
                "UPDATE core.claims SET subject_entity_id = %s WHERE id = %s",
                (other, claim_id),
            )
        ],
    )
    require("g9-32 raw update sqlstate", update_state, "42501")
    delete_state, _delete_primary = sqlerror_tx(
        api,
        [("DELETE FROM core.claim_evidence WHERE claim_id = %s", (claim_id,))],
    )
    require("g9-32 raw delete sqlstate", delete_state, "42501")
    require(
        "g9-32 subject unchanged",
        scalar(admin, "SELECT subject_entity_id FROM core.claims WHERE id = %s", claim_id),
        bound,
    )
    require(
        "g9-32 evidence unchanged",
        int(
            scalar(
                admin,
                "SELECT count(*) FROM core.claim_evidence WHERE claim_id = %s",
                claim_id,
            )
        ),
        evidence,
    )


def g9_33(admin: psycopg.Connection[Any], api: psycopg.Connection[Any], senior: uuid.UUID) -> None:
    tag = uuid.uuid4().hex[:8]
    _principal, document_id, _source = seed_document(admin, f"g933-{tag}")
    span = insert_span(admin, document_id, sha256_text(f"g933-{tag}"))
    input_hash = sha256_text(f"g933-{tag}")
    run_id = insert_model_run(
        admin,
        document_version_id=document_id,
        task_type="claim_extraction",
        input_sha256=input_hash,
        tag=f"{tag}-ai",
    )
    analysis_id = insert_analysis(
        admin,
        model_run_id=run_id,
        document_version_id=document_id,
        result_type="claim_extraction",
        result={"claims": []},
    )
    text = f"AI claim {tag} hovered"
    claim_id, _evidence_id = insert_supported_ai_claim(
        admin,
        analysis_id,
        document_id,
        0,
        text,
        sha256_text(text),
        span,
    )
    case_id = call_api(api, senior, uuid.uuid4(), open_sql("claim", claim_id, REASON))
    decisions_before = int(scalar(admin, "SELECT count(*) FROM audit.review_decisions"))
    supports_before = int(
        scalar(
            admin,
            """
            SELECT count(*) FROM core.claim_evidence
             WHERE claim_id = %s AND support_type = 'supports'
            """,
            claim_id,
        )
    )
    state, primary = sqlerror_tx(
        api,
        [
            *bind(senior, uuid.uuid4()),
            decide_sql(case_id, "reject", {"retire_supporting_evidence": True}),
        ],
    )
    require("g9-33 retire sqlstate", state, "22023")
    require("g9-33 retire token", primary, "review_ai_evidence_immutable")
    require(
        "g9-33 no decision",
        int(scalar(admin, "SELECT count(*) FROM audit.review_decisions")),
        decisions_before,
    )
    require(
        "g9-33 evidence kept",
        int(
            scalar(
                admin,
                """
                SELECT count(*) FROM core.claim_evidence
                 WHERE claim_id = %s AND support_type = 'supports'
                """,
                claim_id,
            )
        ),
        supports_before,
    )
    owner_state, _owner_primary = sqlerror_tx(
        admin,
        [("DELETE FROM core.claim_evidence WHERE claim_id = %s", (claim_id,))],
    )
    require("g9-33 owner sqlstate", owner_state, "23514")
    call_api(api, senior, uuid.uuid4(), decide_sql(case_id, "reject"))
    require(
        "g9-33 rejected",
        scalar(admin, "SELECT status::text FROM audit.review_cases WHERE id = %s", case_id),
        "rejected",
    )
    require(
        "g9-33 evidence after reject",
        int(
            scalar(
                admin,
                """
                SELECT count(*) FROM core.claim_evidence
                 WHERE claim_id = %s AND support_type = 'supports'
                """,
                claim_id,
            )
        ),
        supports_before,
    )


def g9_38(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    author: uuid.UUID,
) -> None:
    _principal, document_id, _source = seed_document(admin, f"g938-{uuid.uuid4().hex[:8]}")
    span = insert_span(admin, document_id, sha256_text(f"g938-{document_id}"))
    alt_span = insert_span(admin, document_id, sha256_text(f"g938-alt-{document_id}"))
    _p2, other_doc, _s2 = seed_document(admin, f"g938b-{uuid.uuid4().hex[:8]}")
    request = uuid.uuid4()
    first = call_api(
        api,
        author,
        request,
        manual_sql(document_id, MANUAL_TEXT, "witness", [span]),
    )
    replay = call_api(
        api,
        author,
        request,
        manual_sql(document_id, MANUAL_TEXT, "witness", [span]),
    )
    require("g9-38 replay id", replay, first)
    require(
        "g9-38 one claim",
        scalar(admin, "SELECT count(*) FROM core.claims WHERE id = %s", first),
        1,
    )
    require("g9-38 one event", event_count(admin, f"review.claim.manual:{request}"), 1)
    claims_before = int(scalar(admin, "SELECT count(*) FROM core.claims"))
    for name, statement in (
        ("attribution", manual_sql(document_id, MANUAL_TEXT, "other witness", [span])),
        ("document", manual_sql(other_doc, MANUAL_TEXT, "witness", [span])),
        ("text", manual_sql(document_id, f"{MANUAL_TEXT} variant", "witness", [span])),
        ("spans", manual_sql(document_id, MANUAL_TEXT, "witness", [alt_span])),
    ):
        state, primary = sqlerror_tx(api, [*bind(author, request), statement])
        require(f"g9-38 {name} sqlstate", state, "23505")
        require(f"g9-38 {name} token", primary, "review_idempotency_payload_conflict")
    require(
        "g9-38 no extra claims",
        int(scalar(admin, "SELECT count(*) FROM core.claims")),
        claims_before,
    )


def g9_26(admin: psycopg.Connection[Any]) -> None:
    require(
        "g9-26 head",
        scalar(admin, "SELECT version_num FROM public.alembic_version"),
        CURRENT_HEAD,
    )
    require(
        "g9-26 tables",
        int(
            scalar(
                admin,
                """
                SELECT count(*) FROM pg_tables
                 WHERE schemaname IN ('ingest','core','ops','audit','public')
                   AND tablename <> 'alembic_version'
                """,
            )
        ),
        EXPECTED_TABLE_COUNT,
    )
    require(
        "g9-26 create_manual_claim",
        scalar(
            admin,
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_proc
                 WHERE proname = 'create_manual_claim'
                   AND pronamespace = 'audit'::regnamespace
            )
            """,
        ),
        True,
    )


def main() -> None:
    admin = connect()
    api = connect("uap_api")
    worker = connect("uap_worker")
    try:
        head = scalar(admin, "SELECT version_num FROM public.alembic_version")
        require("alembic head", head, CURRENT_HEAD)
        require("event key lock class", EVENT_KEY_LOCK_CLASS, 9175)
        seed_grantor(admin)
        permissions(admin)
        author = insert_person(admin)
        bind_role(admin, author, "reviewer")
        senior = insert_person(admin)
        bind_role(admin, senior, "senior_reviewer")
        g9_26(admin)
        document_id, span, _claim = g9_23(admin, api, author)
        g9_24(admin, api, author, senior, document_id, span)
        g9_32(admin, api, author, senior, document_id, span)
        g9_33(admin, api, senior)
        g9_38(admin, api, author)
        g9_25(admin, worker)
    finally:
        worker.close()
        api.close()
        admin.close()
    print("WP9.6 runtime probe passed: G9-23 G9-24 G9-25 G9-32 G9-33 G9-38")


if __name__ == "__main__":
    main()
