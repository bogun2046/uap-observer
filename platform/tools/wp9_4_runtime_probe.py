"""WP9.4 runtime probe: G9-17-G9-19, G9-36 on real uap_api logins."""

from __future__ import annotations

import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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
    insert_candidate_with_evidence,
    insert_model_run,
    insert_span,
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
    require,
    scalar,
    seed_grantor,
    sqlerror_tx,
)

CURRENT_HEAD = "0019_manual_claims_binding"
SELECT_REASON = "select this analysis result"
ACCEPT_REASON = "accept this entity candidate"
BIND_REASON = "bind this entity candidate"
CONFLICT_REASON = "a conflicting reason text"
EVENT_KEY_LOCK_CLASS = 9175


def select_sql(
    analysis_id: uuid.UUID, reason: str = SELECT_REASON
) -> tuple[str, tuple[object, ...]]:
    return ("SELECT audit.select_analysis_result(%s, %s)", (analysis_id, reason))


def accept_sql(
    candidate_id: uuid.UUID, reason: str = ACCEPT_REASON
) -> tuple[str, tuple[object, ...]]:
    return ("SELECT audit.accept_entity_candidate(%s, %s)", (candidate_id, reason))


def bind_sql(
    candidate_id: uuid.UUID, entity_id: uuid.UUID, reason: str = BIND_REASON
) -> tuple[str, tuple[object, ...]]:
    return (
        "SELECT audit.bind_entity_candidate(%s, %s, %s)",
        (candidate_id, entity_id, reason),
    )


def call_api(
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    request: uuid.UUID,
    statement: tuple[str, tuple[object, ...]],
) -> uuid.UUID:
    with api.transaction():
        with api.cursor() as cursor:
            for sql, params in [*bind(reviewer, request), statement]:
                cursor.execute(sql, params)
            row = cursor.fetchone()
    if row is None or row[0] is None:
        raise RuntimeError("review write returned no id")
    return uuid.UUID(str(row[0]))


def resolve_job_count(admin: psycopg.Connection[Any]) -> int:
    return int(
        scalar(
            admin,
            """
            SELECT count(*) FROM ops.jobs
             WHERE job_type IN ('resolve_claims', 'resolve_entities', 'resolve_relations')
            """,
        )
    )


def current_selection(
    admin: psycopg.Connection[Any], document_version_id: uuid.UUID, result_type: str
) -> uuid.UUID | None:
    with admin.cursor() as cursor:
        cursor.execute(
            """
            SELECT analysis_result_id FROM core.analysis_selections
             WHERE document_version_id = %s AND result_type = %s AND superseded_at IS NULL
            """,
            (document_version_id, result_type),
        )
        row = cursor.fetchone()
    if row is None or row[0] is None:
        return None
    return uuid.UUID(str(row[0]))


def current_selection_count(
    admin: psycopg.Connection[Any], document_version_id: uuid.UUID, result_type: str
) -> int:
    return int(
        scalar(
            admin,
            """
            SELECT count(*) FROM core.analysis_selections
             WHERE document_version_id = %s AND result_type = %s AND superseded_at IS NULL
            """,
            document_version_id,
            result_type,
        )
    )


def event_count(admin: psycopg.Connection[Any], event_key: str) -> int:
    return int(
        scalar(
            admin,
            "SELECT count(*) FROM audit.audit_events WHERE event_key = %s",
            event_key,
        )
    )


def seed_claim_pair(
    admin: psycopg.Connection[Any], tag: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    _principal, document_id, _source = seed_document(admin, tag)
    first_run = insert_model_run(
        admin,
        document_version_id=document_id,
        task_type="claim_extraction",
        input_sha256=sha256_text(f"{tag}-a"),
        tag=f"{tag}-a",
    )
    second_run = insert_model_run(
        admin,
        document_version_id=document_id,
        task_type="claim_extraction",
        input_sha256=sha256_text(f"{tag}-b"),
        tag=f"{tag}-b",
    )
    first = insert_analysis(
        admin,
        model_run_id=first_run,
        document_version_id=document_id,
        result_type="claim_extraction",
        result={"claims": [{"text": f"{tag}-a"}]},
    )
    second = insert_analysis(
        admin,
        model_run_id=second_run,
        document_version_id=document_id,
        result_type="claim_extraction",
        result={"claims": [{"text": f"{tag}-b"}]},
    )
    return document_id, first, second


def seed_typed_analysis(
    admin: psycopg.Connection[Any],
    tag: str,
    result_type: str,
    validation_status: str = "valid",
) -> uuid.UUID:
    _principal, document_id, _source = seed_document(admin, tag)
    run = insert_model_run(
        admin,
        document_version_id=document_id,
        task_type=result_type,
        input_sha256=sha256_text(tag),
        tag=tag,
    )
    return insert_analysis(
        admin,
        model_run_id=run,
        document_version_id=document_id,
        result_type=result_type,
        result={"payload": tag},
        validation_status=validation_status,
    )


def seed_pending_candidate(
    admin: psycopg.Connection[Any], tag: str, name: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    _principal, document_id, _source = seed_document(admin, tag)
    run = insert_model_run(
        admin,
        document_version_id=document_id,
        task_type="entity_extraction",
        input_sha256=sha256_text(tag),
        tag=tag,
    )
    analysis_id = insert_analysis(
        admin,
        model_run_id=run,
        document_version_id=document_id,
        result_type="entity_extraction",
        result={"entities": [{"name": name}]},
    )
    span_id = insert_span(admin, document_id, sha256_text(f"span-{tag}"))
    candidate_id, _link = insert_candidate_with_evidence(
        admin, analysis_id, document_id, 0, name, span_id
    )
    return document_id, analysis_id, candidate_id


def insert_active_entity(admin: psycopg.Connection[Any], name: str) -> uuid.UUID:
    entity_id = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO core.entities (id, entity_type, canonical_name, status)
        VALUES (%s, 'person', %s, 'active')
        """,
        entity_id,
        name,
    )
    return entity_id


def permissions(admin: psycopg.Connection[Any]) -> None:
    for name, signature in [
        ("select", "audit.select_analysis_result(uuid,text)"),
        ("accept", "audit.accept_entity_candidate(uuid,text)"),
        ("bind", "audit.bind_entity_candidate(uuid,uuid,text)"),
    ]:
        require(
            f"api execute {name}",
            scalar(
                admin,
                "SELECT has_function_privilege('uap_api', %s, 'EXECUTE')",
                signature,
            ),
            True,
        )
        require(
            f"worker execute {name}",
            scalar(
                admin,
                "SELECT has_function_privilege('uap_worker', %s, 'EXECUTE')",
                signature,
            ),
            False,
        )
        require(
            f"publisher execute {name}",
            scalar(
                admin,
                "SELECT has_function_privilege('uap_publisher', %s, 'EXECUTE')",
                signature,
            ),
            False,
        )
    require(
        "api merge closed",
        scalar(
            admin,
            """
            SELECT has_function_privilege(
                'uap_api', 'core.merge_entities(uuid,uuid,uuid,text)', 'EXECUTE'
            )
            """,
        ),
        False,
    )


def g9_17(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
) -> None:
    document_id, first, second = seed_claim_pair(admin, uuid.uuid4().hex[:12])
    jobs_before = resolve_job_count(admin)
    claims_before = int(scalar(admin, "SELECT count(*) FROM core.claims"))
    first_id = call_api(api, reviewer, uuid.uuid4(), select_sql(first))
    require(
        "g9-17 first current",
        current_selection(admin, document_id, "claim_extraction"),
        first,
    )
    second_id = call_api(api, reviewer, uuid.uuid4(), select_sql(second))
    require("g9-17 distinct selections", first_id != second_id, True)
    require(
        "g9-17 current is B",
        current_selection(admin, document_id, "claim_extraction"),
        second,
    )
    require(
        "g9-17 one current",
        current_selection_count(admin, document_id, "claim_extraction"),
        1,
    )
    require(
        "g9-17 first superseded",
        scalar(
            admin,
            "SELECT superseded_at IS NOT NULL FROM core.analysis_selections WHERE id = %s",
            first_id,
        ),
        True,
    )
    require("g9-17 jobs unchanged", resolve_job_count(admin), jobs_before)
    require(
        "g9-17 claims unchanged",
        int(scalar(admin, "SELECT count(*) FROM core.claims")),
        claims_before,
    )
    require(
        "g9-17 selected_by",
        uuid.UUID(
            str(
                scalar(
                    admin,
                    "SELECT selected_by FROM core.analysis_selections WHERE id = %s",
                    second_id,
                )
            )
        ),
        reviewer,
    )


def g9_18(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
) -> None:
    tag = uuid.uuid4().hex[:12]
    document_id, valid, _other = seed_claim_pair(admin, tag)
    selected = call_api(api, reviewer, uuid.uuid4(), select_sql(valid))
    invalid_run = insert_model_run(
        admin,
        document_version_id=document_id,
        task_type="claim_extraction",
        input_sha256=sha256_text(f"{tag}-invalid"),
        tag=f"{tag}-invalid",
    )
    invalid_id = insert_analysis(
        admin,
        model_run_id=invalid_run,
        document_version_id=document_id,
        result_type="claim_extraction",
        result={"claims": []},
        validation_status="invalid",
    )
    pending_run = insert_model_run(
        admin,
        document_version_id=document_id,
        task_type="claim_extraction",
        input_sha256=sha256_text(f"{tag}-pending"),
        tag=f"{tag}-pending",
    )
    pending_id = insert_analysis(
        admin,
        model_run_id=pending_run,
        document_version_id=document_id,
        result_type="claim_extraction",
        result={"claims": []},
        validation_status="pending",
    )
    summary_run = insert_model_run(
        admin,
        document_version_id=document_id,
        task_type="summary",
        input_sha256=sha256_text(f"{tag}-summary"),
        tag=f"{tag}-summary",
    )
    summary_id = insert_analysis(
        admin,
        model_run_id=summary_run,
        document_version_id=document_id,
        result_type="summary",
        result={"summary": tag},
    )
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, uuid.uuid4()), select_sql(invalid_id)]
    )
    require("g9-18 invalid state", state, "22023")
    require("g9-18 invalid code", primary, "review_selection_not_valid")
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, uuid.uuid4()), select_sql(summary_id)]
    )
    require("g9-18 summary state", state, "22023")
    require("g9-18 summary code", primary, "review_selection_type_unsupported")
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, uuid.uuid4()), select_sql(pending_id)]
    )
    require("g9-18 pending state", state, "22023")
    require("g9-18 pending code", primary, "review_selection_not_valid")
    require(
        "g9-18 current unchanged",
        current_selection(admin, document_id, "claim_extraction"),
        valid,
    )
    require(
        "g9-18 selection id unchanged",
        uuid.UUID(
            str(
                scalar(
                    admin,
                    """
                    SELECT id FROM core.analysis_selections
                     WHERE document_version_id = %s
                       AND result_type = 'claim_extraction'
                       AND superseded_at IS NULL
                    """,
                    document_id,
                )
            )
        ),
        selected,
    )


def g9_19(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
) -> None:
    name = f"Same Name {uuid.uuid4().hex[:8]}"
    existing = insert_active_entity(admin, name)
    _document, _analysis, candidate_id = seed_pending_candidate(
        admin, uuid.uuid4().hex[:12], name
    )
    merges_before = int(scalar(admin, "SELECT count(*) FROM core.entity_merge_events"))
    cases_before = int(scalar(admin, "SELECT count(*) FROM audit.review_cases"))
    grants_before = int(
        scalar(admin, "SELECT count(*) FROM audit.entity_publication_grants")
    )
    outbox_before = int(scalar(admin, "SELECT count(*) FROM ops.outbox_events"))
    public_before = int(scalar(admin, "SELECT count(*) FROM public.claims"))
    entity_id = call_api(api, reviewer, uuid.uuid4(), accept_sql(candidate_id))
    require("g9-19 new entity", entity_id != existing, True)
    require(
        "g9-19 both active",
        int(
            scalar(
                admin,
                """
                SELECT count(*) FROM core.entities
                 WHERE canonical_name = %s AND status = 'active'
                """,
                name,
            )
        ),
        2,
    )
    require(
        "g9-19 candidate resolved",
        scalar(
            admin,
            "SELECT status::text FROM core.entity_candidates WHERE id = %s",
            candidate_id,
        ),
        "resolved",
    )
    require(
        "g9-19 bound new",
        uuid.UUID(
            str(
                scalar(
                    admin,
                    "SELECT resolved_entity_id FROM core.entity_candidates WHERE id = %s",
                    candidate_id,
                )
            )
        ),
        entity_id,
    )
    require(
        "g9-19 resolved_by",
        uuid.UUID(
            str(
                scalar(
                    admin,
                    "SELECT resolved_by FROM core.entity_candidates WHERE id = %s",
                    candidate_id,
                )
            )
        ),
        reviewer,
    )
    require(
        "g9-19 no merge",
        int(scalar(admin, "SELECT count(*) FROM core.entity_merge_events")),
        merges_before,
    )
    require(
        "g9-19 no case",
        int(scalar(admin, "SELECT count(*) FROM audit.review_cases")),
        cases_before,
    )
    require(
        "g9-19 no grant",
        int(scalar(admin, "SELECT count(*) FROM audit.entity_publication_grants")),
        grants_before,
    )
    require(
        "g9-19 no outbox",
        int(scalar(admin, "SELECT count(*) FROM ops.outbox_events")),
        outbox_before,
    )
    require(
        "g9-19 no public",
        int(scalar(admin, "SELECT count(*) FROM public.claims")),
        public_before,
    )


def g9_36(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
) -> None:
    document_id, first, second = seed_claim_pair(admin, uuid.uuid4().hex[:12])
    request = uuid.uuid4()
    selection_id = call_api(api, reviewer, request, select_sql(first))
    replay = call_api(api, reviewer, request, select_sql(first))
    require("g9-36 select replay", replay, selection_id)
    require(
        "g9-36 select one current",
        current_selection_count(admin, document_id, "claim_extraction"),
        1,
    )
    require(
        "g9-36 select events",
        event_count(admin, f"review.selection:{request}"),
        1,
    )
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, request), select_sql(first, CONFLICT_REASON)]
    )
    require("g9-36 select reason state", state, "23505")
    require("g9-36 select reason code", primary, "review_idempotency_payload_conflict")
    state, primary = sqlerror_tx(api, [*bind(reviewer, request), select_sql(second)])
    require("g9-36 select id state", state, "23505")
    require("g9-36 select id code", primary, "review_idempotency_payload_conflict")
    require(
        "g9-36 select still first",
        current_selection(admin, document_id, "claim_extraction"),
        first,
    )

    name = f"Replay {uuid.uuid4().hex[:8]}"
    _document, _analysis, candidate_id = seed_pending_candidate(
        admin, uuid.uuid4().hex[:12], name
    )
    accept_request = uuid.uuid4()
    entities_before = int(
        scalar(admin, "SELECT count(*) FROM core.entities WHERE canonical_name = %s", name)
    )
    entity_id = call_api(api, reviewer, accept_request, accept_sql(candidate_id))
    replay_entity = call_api(api, reviewer, accept_request, accept_sql(candidate_id))
    require("g9-36 accept replay", replay_entity, entity_id)
    require(
        "g9-36 accept one entity",
        int(
            scalar(
                admin, "SELECT count(*) FROM core.entities WHERE canonical_name = %s", name
            )
        ),
        entities_before + 1,
    )
    require(
        "g9-36 accept events",
        event_count(admin, f"review.candidate.accept:{accept_request}"),
        1,
    )
    other_candidate = seed_pending_candidate(admin, uuid.uuid4().hex[:12], name)[2]
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, accept_request), accept_sql(candidate_id, CONFLICT_REASON)]
    )
    require("g9-36 accept reason state", state, "23505")
    require("g9-36 accept reason code", primary, "review_idempotency_payload_conflict")
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, accept_request), accept_sql(other_candidate)]
    )
    require("g9-36 accept id state", state, "23505")
    require("g9-36 accept id code", primary, "review_idempotency_payload_conflict")
    require(
        "g9-36 other candidate pending",
        scalar(
            admin,
            "SELECT status::text FROM core.entity_candidates WHERE id = %s",
            other_candidate,
        ),
        "pending",
    )

    bind_candidate = seed_pending_candidate(
        admin, uuid.uuid4().hex[:12], f"Bind {uuid.uuid4().hex[:8]}"
    )[2]
    target = insert_active_entity(admin, f"Target {uuid.uuid4().hex[:8]}")
    other_target = insert_active_entity(admin, f"Other {uuid.uuid4().hex[:8]}")
    bind_request = uuid.uuid4()
    bound = call_api(api, reviewer, bind_request, bind_sql(bind_candidate, target))
    require("g9-36 bind id", bound, target)
    replay_bound = call_api(
        api, reviewer, bind_request, bind_sql(bind_candidate, target)
    )
    require("g9-36 bind replay", replay_bound, target)
    require(
        "g9-36 bind events",
        event_count(admin, f"review.candidate.bind:{bind_request}"),
        1,
    )
    require(
        "g9-36 bind still target",
        uuid.UUID(
            str(
                scalar(
                    admin,
                    "SELECT resolved_entity_id FROM core.entity_candidates WHERE id = %s",
                    bind_candidate,
                )
            )
        ),
        target,
    )
    state, primary = sqlerror_tx(
        api,
        [*bind(reviewer, bind_request), bind_sql(bind_candidate, target, CONFLICT_REASON)],
    )
    require("g9-36 bind reason state", state, "23505")
    require("g9-36 bind reason code", primary, "review_idempotency_payload_conflict")
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, bind_request), bind_sql(bind_candidate, other_target)]
    )
    require("g9-36 bind entity state", state, "23505")
    require("g9-36 bind entity code", primary, "review_idempotency_payload_conflict")
    other_bind = seed_pending_candidate(
        admin, uuid.uuid4().hex[:12], f"Alt {uuid.uuid4().hex[:8]}"
    )[2]
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, bind_request), bind_sql(other_bind, target)]
    )
    require("g9-36 bind candidate state", state, "23505")
    require("g9-36 bind candidate code", primary, "review_idempotency_payload_conflict")
    require(
        "g9-36 other bind pending",
        scalar(
            admin,
            "SELECT status::text FROM core.entity_candidates WHERE id = %s",
            other_bind,
        ),
        "pending",
    )


def extra_cases(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    senior: uuid.UUID,
) -> None:
    missing = uuid.uuid4()
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, uuid.uuid4()), select_sql(missing)]
    )
    require("extra missing selection state", state, "23503")
    require("extra missing selection code", primary, "review_selection_missing")
    classification_id = seed_typed_analysis(
        admin, uuid.uuid4().hex[:12], "classification"
    )
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, uuid.uuid4()), select_sql(classification_id)]
    )
    require("extra classification state", state, "22023")
    require("extra classification code", primary, "review_selection_type_unsupported")

    state, primary = sqlerror_tx(
        api, [*bind(reviewer, uuid.uuid4()), accept_sql(uuid.uuid4())]
    )
    require("extra missing candidate state", state, "23503")
    require("extra missing candidate code", primary, "review_candidate_missing")

    _document, analysis_id, no_evidence = seed_pending_candidate(
        admin, uuid.uuid4().hex[:12], f"Bare {uuid.uuid4().hex[:8]}"
    )
    execute(admin, "SET session_replication_role = replica")
    execute(
        admin,
        "DELETE FROM core.entity_candidate_evidence WHERE entity_candidate_id = %s",
        no_evidence,
    )
    execute(admin, "SET session_replication_role = origin")
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, uuid.uuid4()), accept_sql(no_evidence)]
    )
    require("extra no evidence state", state, "22023")
    require("extra no evidence code", primary, "review_candidate_evidence_missing")

    named = f"Invalid origin {uuid.uuid4().hex[:8]}"
    _d, origin_analysis, origin_candidate = seed_pending_candidate(
        admin, uuid.uuid4().hex[:12], named
    )
    execute(admin, "SET session_replication_role = replica")
    execute(
        admin,
        "UPDATE core.analysis_results SET validation_status = 'invalid' WHERE id = %s",
        origin_analysis,
    )
    execute(admin, "SET session_replication_role = origin")
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, uuid.uuid4()), accept_sql(origin_candidate)]
    )
    require("extra origin state", state, "22023")
    require("extra origin code", primary, "review_candidate_origin_invalid")

    rejected_name = f"Rejected {uuid.uuid4().hex[:8]}"
    rejected = seed_pending_candidate(admin, uuid.uuid4().hex[:12], rejected_name)[2]
    execute(
        admin,
        "UPDATE core.entity_candidates SET status = 'rejected' WHERE id = %s",
        rejected,
    )
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, uuid.uuid4()), accept_sql(rejected)]
    )
    require("extra rejected accept state", state, "22023")
    require("extra rejected accept code", primary, "review_candidate_not_pending")

    resolved = seed_pending_candidate(
        admin, uuid.uuid4().hex[:12], f"Resolved {uuid.uuid4().hex[:8]}"
    )[2]
    call_api(api, reviewer, uuid.uuid4(), accept_sql(resolved))
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, uuid.uuid4()), accept_sql(resolved)]
    )
    require("extra resolved accept state", state, "22023")
    require("extra resolved accept code", primary, "review_candidate_not_pending")

    bind_pending = seed_pending_candidate(
        admin, uuid.uuid4().hex[:12], f"Bind extra {uuid.uuid4().hex[:8]}"
    )[2]
    retired = insert_active_entity(admin, f"Retired {uuid.uuid4().hex[:8]}")
    execute(
        admin,
        "UPDATE core.entities SET status = 'retired' WHERE id = %s",
        retired,
    )
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, uuid.uuid4()), bind_sql(bind_pending, retired)]
    )
    require("extra retired state", state, "22023")
    require("extra retired code", primary, "review_bind_target_not_active")

    disputed = insert_active_entity(admin, f"Disputed {uuid.uuid4().hex[:8]}")
    execute(
        admin,
        "UPDATE core.entities SET status = 'disputed' WHERE id = %s",
        disputed,
    )
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, uuid.uuid4()), bind_sql(bind_pending, disputed)]
    )
    require("extra disputed state", state, "22023")
    require("extra disputed code", primary, "review_bind_target_not_active")

    source = insert_active_entity(admin, f"Source {uuid.uuid4().hex[:8]}")
    target = insert_active_entity(admin, f"Canonical {uuid.uuid4().hex[:8]}")
    execute(
        admin,
        "SELECT core.merge_entities(%s, %s, %s, %s)",
        source,
        target,
        senior,
        "merge two probe entities",
    )
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, uuid.uuid4()), bind_sql(bind_pending, source)]
    )
    require("extra merged/non-canonical state", state, "22023")
    require(
        "extra merged/non-canonical code",
        primary,
        "review_bind_target_not_canonical",
    )
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, uuid.uuid4()), bind_sql(bind_pending, uuid.uuid4())]
    )
    require("extra missing entity state", state, "23503")
    require("extra missing entity code", primary, "review_entity_missing")
    state, primary = sqlerror_tx(
        api, [*bind(reviewer, uuid.uuid4()), bind_sql(resolved, target)]
    )
    require("extra resolved bind state", state, "22023")
    require("extra resolved bind code", primary, "review_candidate_not_pending")

    worker = connect("uap_worker")
    try:
        analysis_id = seed_claim_pair(admin, uuid.uuid4().hex[:12])[1]
        state, primary = sqlerror_tx(
            worker, [*bind(reviewer, uuid.uuid4()), select_sql(analysis_id)]
        )
    finally:
        worker.close()
    require("extra worker state", state, "42501")
    pair = seed_claim_pair(admin, uuid.uuid4().hex[:12])
    state, primary = sqlerror_tx(
        api,
        [
            (
                """
                INSERT INTO core.analysis_selections (
                    id, document_version_id, analysis_result_id, result_type,
                    selected_by, selection_reason
                ) VALUES (%s, %s, %s, 'claim_extraction', %s, %s)
                """,
                (uuid.uuid4(), pair[0], pair[1], reviewer, SELECT_REASON),
            )
        ],
    )
    require("extra api selection insert state", state, "42501")


def _activity_wait_event(
    admin: psycopg.Connection[Any], application_name: str
) -> str | None:
    with admin.cursor() as cursor:
        cursor.execute(
            """
            SELECT coalesce(wait_event_type, '') || ':' || coalesce(wait_event, '')
              FROM pg_stat_activity
             WHERE application_name = %s
             LIMIT 1
            """,
            (application_name,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return str(row[0])


def _ungranted_locks_for(admin: psycopg.Connection[Any], application_name: str) -> int:
    with admin.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)::int
              FROM pg_locks AS lock_row
              JOIN pg_stat_activity AS activity
                ON activity.pid = lock_row.pid
             WHERE activity.application_name = %s
               AND NOT lock_row.granted
            """,
            (application_name,),
        )
        row = cursor.fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _waiter_blocked(admin: psycopg.Connection[Any], application_name: str) -> bool:
    if _ungranted_locks_for(admin, application_name) > 0:
        return True
    wait_event = _activity_wait_event(admin, application_name)
    if wait_event is None:
        return False
    lowered = wait_event.lower()
    return wait_event.startswith("Lock:") or "lock" in lowered


def extra_concurrent_same_request(
    admin: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    lock_sql: str,
    lock_params: tuple[object, ...],
    statement: tuple[str, tuple[object, ...]],
    request: uuid.UUID,
    label: str,
) -> list[uuid.UUID]:
    tag = uuid.uuid4().hex[:8]
    names = (f"wp94-{label}-{tag}-a", f"wp94-{label}-{tag}-b")
    locker_held = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    results: list[uuid.UUID] = []

    def locker() -> None:
        connection = connect()
        connection.autocommit = False
        try:
            with connection.cursor() as cursor:
                cursor.execute(lock_sql, lock_params)
            locker_held.set()
            if not release.wait(timeout=20):
                raise TimeoutError(f"{label} workers did not block on row lock")
            connection.commit()
        except BaseException as error:
            connection.rollback()
            errors.append(error)
        finally:
            connection.close()

    def worker(name: str) -> None:
        if not locker_held.wait(timeout=10):
            errors.append(TimeoutError(f"{label} locker did not acquire row lock"))
            return
        connection = connect("uap_api")
        connection.autocommit = False
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('application_name', %s, false)", (name,))
                for sql, params in [*bind(reviewer, request), statement]:
                    cursor.execute(sql, params)
                row = cursor.fetchone()
            connection.commit()
            if row is None or row[0] is None:
                raise RuntimeError(f"{label} concurrent same request returned no id")
            results.append(uuid.UUID(str(row[0])))
        except BaseException as error:
            connection.rollback()
            errors.append(error)
        finally:
            connection.close()

    locker_thread = threading.Thread(target=locker)
    locker_thread.start()
    if not locker_held.wait(timeout=10):
        release.set()
        locker_thread.join(timeout=5)
        raise RuntimeError(f"{label} locker failed to hold row")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, name) for name in names]
        deadline = time.monotonic() + 15
        blocked = False
        while time.monotonic() < deadline:
            if all(_waiter_blocked(admin, name) for name in names):
                blocked = True
                break
            time.sleep(0.05)
        require(f"extra concurrent {label} waiters blocked", blocked, True)
        release.set()
        locker_thread.join(timeout=10)
        for future in futures:
            future.result(timeout=30)
    require(f"extra concurrent {label} errors", [str(item) for item in errors], [])
    require(f"extra concurrent {label} both", len(results), 2)
    require(f"extra concurrent {label} same id", results[0], results[1])
    return results


def extra_concurrent_payload_conflict(
    admin: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    lock_sql: str,
    lock_params: tuple[object, ...],
    first_statement: tuple[str, tuple[object, ...]],
    second_statement: tuple[str, tuple[object, ...]],
    request: uuid.UUID,
    label: str,
) -> None:
    tag = uuid.uuid4().hex[:8]
    names = (f"wp94-conf-{label}-{tag}-a", f"wp94-conf-{label}-{tag}-b")
    locker_held = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    ids: list[uuid.UUID] = []
    denied: list[tuple[str, str]] = []
    statements = (first_statement, second_statement)

    def locker() -> None:
        connection = connect()
        connection.autocommit = False
        try:
            with connection.cursor() as cursor:
                cursor.execute(lock_sql, lock_params)
            locker_held.set()
            if not release.wait(timeout=20):
                raise TimeoutError(f"{label} conflict workers did not block")
            connection.commit()
        except BaseException as error:
            connection.rollback()
            errors.append(error)
        finally:
            connection.close()

    def worker(name: str, statement: tuple[str, tuple[object, ...]]) -> None:
        if not locker_held.wait(timeout=10):
            errors.append(TimeoutError(f"{label} locker did not acquire row lock"))
            return
        connection = connect("uap_api")
        connection.autocommit = False
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('application_name', %s, false)", (name,))
                for sql, params in [*bind(reviewer, request), statement]:
                    cursor.execute(sql, params)
                row = cursor.fetchone()
            connection.commit()
            if row is None or row[0] is None:
                raise RuntimeError(f"{label} concurrent conflict returned no id")
            ids.append(uuid.UUID(str(row[0])))
        except psycopg.Error as error:
            connection.rollback()
            primary = ""
            if error.diag is not None and error.diag.message_primary:
                primary = error.diag.message_primary
            denied.append((str(error.sqlstate), primary))
        except BaseException as error:
            connection.rollback()
            errors.append(error)
        finally:
            connection.close()

    locker_thread = threading.Thread(target=locker)
    locker_thread.start()
    if not locker_held.wait(timeout=10):
        release.set()
        locker_thread.join(timeout=5)
        raise RuntimeError(f"{label} conflict locker failed to hold row")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(worker, name, statement)
            for name, statement in zip(names, statements, strict=True)
        ]
        deadline = time.monotonic() + 15
        blocked = False
        while time.monotonic() < deadline:
            if all(_waiter_blocked(admin, name) for name in names):
                blocked = True
                break
            time.sleep(0.05)
        require(f"extra concurrent {label} conflict waiters blocked", blocked, True)
        release.set()
        locker_thread.join(timeout=10)
        for future in futures:
            future.result(timeout=30)
    require(f"extra concurrent {label} conflict errors", [str(item) for item in errors], [])
    require(f"extra concurrent {label} conflict one id", len(ids), 1)
    require(f"extra concurrent {label} conflict one denial", len(denied), 1)
    require(f"extra concurrent {label} conflict state", denied[0][0], "23505")
    require(
        f"extra concurrent {label} conflict code",
        denied[0][1],
        "review_idempotency_payload_conflict",
    )


def extra_concurrent_cross_resource(
    admin: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    event_key: str,
    first_statement: tuple[str, tuple[object, ...]],
    second_statement: tuple[str, tuple[object, ...]],
    request: uuid.UUID,
    label: str,
) -> None:
    extra_concurrent_payload_conflict(
        admin,
        reviewer,
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
        (EVENT_KEY_LOCK_CLASS, event_key),
        first_statement,
        second_statement,
        request,
        f"cross-{label}",
    )


def run_concurrency(
    admin: psycopg.Connection[Any],
    reviewer: uuid.UUID,
) -> None:
    _document, first, _second = seed_claim_pair(admin, uuid.uuid4().hex[:12])
    select_request = uuid.uuid4()
    extra_concurrent_same_request(
        admin,
        reviewer,
        "SELECT id FROM core.analysis_results WHERE id = %s FOR UPDATE",
        (first,),
        select_sql(first),
        select_request,
        "select",
    )
    require(
        "extra concurrent select events",
        event_count(admin, f"review.selection:{select_request}"),
        1,
    )
    require(
        "extra concurrent select rows",
        int(
            scalar(
                admin,
                "SELECT count(*) FROM core.analysis_selections WHERE analysis_result_id = %s",
                first,
            )
        ),
        1,
    )
    extra_concurrent_payload_conflict(
        admin,
        reviewer,
        "SELECT id FROM core.analysis_results WHERE id = %s FOR UPDATE",
        (first,),
        select_sql(first),
        select_sql(first, CONFLICT_REASON),
        uuid.uuid4(),
        "select",
    )

    candidate = seed_pending_candidate(
        admin, uuid.uuid4().hex[:12], f"Conc {uuid.uuid4().hex[:8]}"
    )[2]
    accept_request = uuid.uuid4()
    extra_concurrent_same_request(
        admin,
        reviewer,
        "SELECT id FROM core.entity_candidates WHERE id = %s FOR UPDATE",
        (candidate,),
        accept_sql(candidate),
        accept_request,
        "accept",
    )
    require(
        "extra concurrent accept events",
        event_count(admin, f"review.candidate.accept:{accept_request}"),
        1,
    )
    require(
        "extra concurrent accept entities",
        int(
            scalar(
                admin,
                """
                SELECT count(*) FROM core.entities
                 WHERE id = (
                    SELECT resolved_entity_id FROM core.entity_candidates WHERE id = %s
                 )
                """,
                candidate,
            )
        ),
        1,
    )

    bind_candidate = seed_pending_candidate(
        admin, uuid.uuid4().hex[:12], f"Bindconc {uuid.uuid4().hex[:8]}"
    )[2]
    target = insert_active_entity(admin, f"Bind target {uuid.uuid4().hex[:8]}")
    bind_request = uuid.uuid4()
    extra_concurrent_same_request(
        admin,
        reviewer,
        "SELECT id FROM core.entity_candidates WHERE id = %s FOR UPDATE",
        (bind_candidate,),
        bind_sql(bind_candidate, target),
        bind_request,
        "bind",
    )
    require(
        "extra concurrent bind events",
        event_count(admin, f"review.candidate.bind:{bind_request}"),
        1,
    )
    require(
        "extra concurrent bind resolved",
        uuid.UUID(
            str(
                scalar(
                    admin,
                    "SELECT resolved_entity_id FROM core.entity_candidates WHERE id = %s",
                    bind_candidate,
                )
            )
        ),
        target,
    )

    doc_a, analysis_a, _other_a = seed_claim_pair(admin, uuid.uuid4().hex[:12])
    doc_b, analysis_b, _other_b = seed_claim_pair(admin, uuid.uuid4().hex[:12])
    cross_select_request = uuid.uuid4()
    extra_concurrent_cross_resource(
        admin,
        reviewer,
        f"review.selection:{cross_select_request}",
        select_sql(analysis_a),
        select_sql(analysis_b),
        cross_select_request,
        "select",
    )
    require(
        "extra cross select events",
        event_count(admin, f"review.selection:{cross_select_request}"),
        1,
    )
    selected_a = current_selection(admin, doc_a, "claim_extraction")
    selected_b = current_selection(admin, doc_b, "claim_extraction")
    require(
        "extra cross select one winner",
        (selected_a is None) != (selected_b is None),
        True,
    )
    if selected_a is not None:
        require("extra cross select winner a", selected_a, analysis_a)
        require("extra cross select loser b empty", selected_b, None)
        require(
            "extra cross select loser b rows",
            current_selection_count(admin, doc_b, "claim_extraction"),
            0,
        )
    else:
        require("extra cross select winner b", selected_b, analysis_b)
        require("extra cross select loser a empty", selected_a, None)
        require(
            "extra cross select loser a rows",
            current_selection_count(admin, doc_a, "claim_extraction"),
            0,
        )

    name_a = f"CrossA {uuid.uuid4().hex[:8]}"
    name_b = f"CrossB {uuid.uuid4().hex[:8]}"
    cand_a = seed_pending_candidate(admin, uuid.uuid4().hex[:12], name_a)[2]
    cand_b = seed_pending_candidate(admin, uuid.uuid4().hex[:12], name_b)[2]
    entities_before_a = int(
        scalar(admin, "SELECT count(*) FROM core.entities WHERE canonical_name = %s", name_a)
    )
    entities_before_b = int(
        scalar(admin, "SELECT count(*) FROM core.entities WHERE canonical_name = %s", name_b)
    )
    cross_accept_request = uuid.uuid4()
    extra_concurrent_cross_resource(
        admin,
        reviewer,
        f"review.candidate.accept:{cross_accept_request}",
        accept_sql(cand_a),
        accept_sql(cand_b),
        cross_accept_request,
        "accept",
    )
    require(
        "extra cross accept events",
        event_count(admin, f"review.candidate.accept:{cross_accept_request}"),
        1,
    )
    status_a = str(
        scalar(admin, "SELECT status::text FROM core.entity_candidates WHERE id = %s", cand_a)
    )
    status_b = str(
        scalar(admin, "SELECT status::text FROM core.entity_candidates WHERE id = %s", cand_b)
    )
    require(
        "extra cross accept one resolved",
        (status_a == "resolved") != (status_b == "resolved"),
        True,
    )
    require(
        "extra cross accept one pending",
        (status_a == "pending") != (status_b == "pending"),
        True,
    )
    count_a = int(
        scalar(admin, "SELECT count(*) FROM core.entities WHERE canonical_name = %s", name_a)
    )
    count_b = int(
        scalar(admin, "SELECT count(*) FROM core.entities WHERE canonical_name = %s", name_b)
    )
    if status_a == "resolved":
        require("extra cross accept winner a entity", count_a, entities_before_a + 1)
        require("extra cross accept loser b entity", count_b, entities_before_b)
        require("extra cross accept loser b pending", status_b, "pending")
    else:
        require("extra cross accept winner b entity", count_b, entities_before_b + 1)
        require("extra cross accept loser a entity", count_a, entities_before_a)
        require("extra cross accept loser a pending", status_a, "pending")

    bind_a = seed_pending_candidate(
        admin, uuid.uuid4().hex[:12], f"CrossBindA {uuid.uuid4().hex[:8]}"
    )[2]
    bind_b = seed_pending_candidate(
        admin, uuid.uuid4().hex[:12], f"CrossBindB {uuid.uuid4().hex[:8]}"
    )[2]
    entity_a = insert_active_entity(admin, f"CrossTargetA {uuid.uuid4().hex[:8]}")
    entity_b = insert_active_entity(admin, f"CrossTargetB {uuid.uuid4().hex[:8]}")
    cross_bind_request = uuid.uuid4()
    extra_concurrent_cross_resource(
        admin,
        reviewer,
        f"review.candidate.bind:{cross_bind_request}",
        bind_sql(bind_a, entity_a),
        bind_sql(bind_b, entity_b),
        cross_bind_request,
        "bind",
    )
    require(
        "extra cross bind events",
        event_count(admin, f"review.candidate.bind:{cross_bind_request}"),
        1,
    )
    bind_status_a = str(
        scalar(admin, "SELECT status::text FROM core.entity_candidates WHERE id = %s", bind_a)
    )
    bind_status_b = str(
        scalar(admin, "SELECT status::text FROM core.entity_candidates WHERE id = %s", bind_b)
    )
    require(
        "extra cross bind one resolved",
        (bind_status_a == "resolved") != (bind_status_b == "resolved"),
        True,
    )
    if bind_status_a == "resolved":
        require(
            "extra cross bind winner a target",
            uuid.UUID(
                str(
                    scalar(
                        admin,
                        "SELECT resolved_entity_id FROM core.entity_candidates WHERE id = %s",
                        bind_a,
                    )
                )
            ),
            entity_a,
        )
        require("extra cross bind loser b pending", bind_status_b, "pending")
        require(
            "extra cross bind loser b unbound",
            scalar(
                admin,
                "SELECT resolved_entity_id FROM core.entity_candidates WHERE id = %s",
                bind_b,
            ),
            None,
        )
    else:
        require(
            "extra cross bind winner b target",
            uuid.UUID(
                str(
                    scalar(
                        admin,
                        "SELECT resolved_entity_id FROM core.entity_candidates WHERE id = %s",
                        bind_b,
                    )
                )
            ),
            entity_b,
        )
        require("extra cross bind loser a pending", bind_status_a, "pending")
        require(
            "extra cross bind loser a unbound",
            scalar(
                admin,
                "SELECT resolved_entity_id FROM core.entity_candidates WHERE id = %s",
                bind_a,
            ),
            None,
        )


def main() -> None:
    admin = connect()
    api = connect("uap_api")
    try:
        head = scalar(admin, "SELECT version_num FROM public.alembic_version")
        require("alembic head", head, CURRENT_HEAD)
        tables = scalar(
            admin,
            """
            SELECT count(*) FROM pg_tables
             WHERE schemaname IN ('ingest','core','ops','audit','public')
               AND tablename <> 'alembic_version'
            """,
        )
        require("table count", int(tables), EXPECTED_TABLE_COUNT)
        seed_grantor(admin)
        permissions(admin)
        reviewer = insert_person(admin)
        bind_role(admin, reviewer, "reviewer")
        senior = insert_person(admin)
        bind_role(admin, senior, "senior_reviewer")
        g9_17(admin, api, reviewer)
        g9_18(admin, api, reviewer)
        g9_19(admin, api, reviewer)
        g9_36(admin, api, reviewer)
        extra_cases(admin, api, reviewer, senior)
        run_concurrency(admin, reviewer)
    finally:
        api.close()
        admin.close()
    print("WP9.4 runtime probe passed: G9-17 G9-18 G9-19 G9-36")


if __name__ == "__main__":
    main()
