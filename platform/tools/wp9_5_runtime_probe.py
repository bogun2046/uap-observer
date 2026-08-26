"""WP9.5 runtime probe: G9-20-G9-22, G9-37 on real uap_api logins."""

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

from tools.wp8_6_runtime_probe import g8_16c  # noqa: E402
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
from tools.wp9_4_runtime_probe import (  # noqa: E402
    _waiter_blocked,
    extra_concurrent_cross_resource,
    extra_concurrent_payload_conflict,
    extra_concurrent_same_request,
    insert_active_entity,
)

CURRENT_HEAD = "0018_authorized_entity_merge"
EVENT_KEY_LOCK_CLASS = 9175
MERGE_REASON = "authorize merge of duplicate entities"
REVERSE_REASON = "authorize reverse of entity merge"
CONFLICT_REASON = "a conflicting merge reason"
GRAPH_LOCK_SQL = "SELECT pg_advisory_xact_lock(824, 1)"
CORE_MERGE = "core.merge_entities(uuid,uuid,uuid,text)"
CORE_REVERSE = "core.reverse_entity_merge(uuid,uuid,text)"
AUDIT_MERGE = "audit.apply_entity_merge(uuid,uuid,text)"
AUDIT_REVERSE = "audit.apply_entity_merge_reverse(uuid,text)"


def merge_sql(
    source: uuid.UUID, target: uuid.UUID, reason: str = MERGE_REASON
) -> tuple[str, tuple[object, ...]]:
    return ("SELECT audit.apply_entity_merge(%s, %s, %s)", (source, target, reason))


def reverse_sql(
    merge_event_id: uuid.UUID, reason: str = REVERSE_REASON
) -> tuple[str, tuple[object, ...]]:
    return ("SELECT audit.apply_entity_merge_reverse(%s, %s)", (merge_event_id, reason))


def call_api(
    api: psycopg.Connection[Any],
    actor: uuid.UUID,
    request: uuid.UUID,
    statement: tuple[str, tuple[object, ...]],
) -> uuid.UUID:
    with api.transaction():
        with api.cursor() as cursor:
            for sql, params in [*bind(actor, request), statement]:
                cursor.execute(sql, params)
            row = cursor.fetchone()
    if row is None or row[0] is None:
        raise RuntimeError("review write returned no id")
    return uuid.UUID(str(row[0]))


def event_count(admin: psycopg.Connection[Any], event_key: str) -> int:
    return int(
        scalar(
            admin,
            "SELECT count(*) FROM audit.audit_events WHERE event_key = %s",
            event_key,
        )
    )


def entity_status(admin: psycopg.Connection[Any], entity_id: uuid.UUID) -> str:
    return str(scalar(admin, "SELECT status::text FROM core.entities WHERE id = %s", entity_id))


def canonical_of(admin: psycopg.Connection[Any], entity_id: uuid.UUID) -> uuid.UUID:
    return uuid.UUID(str(scalar(admin, "SELECT core.canonical_entity_id(%s)", entity_id)))


def open_merge_count(admin: psycopg.Connection[Any], source_id: uuid.UUID) -> int:
    return int(
        scalar(
            admin,
            """
            SELECT count(*) FROM core.entity_merge_events
             WHERE source_entity_id = %s
               AND event_kind = 'merge'
               AND reversed_at IS NULL
            """,
            source_id,
        )
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
    require("api execute merge wrapper", has_execute(admin, "uap_api", AUDIT_MERGE), True)
    require(
        "api execute reverse wrapper",
        has_execute(admin, "uap_api", AUDIT_REVERSE),
        True,
    )
    for role in (
        "uap_worker",
        "uap_publisher",
        "uap_scheduler",
        "uap_model_governance",
        "uap_public_reader",
    ):
        require(f"{role} merge wrapper closed", has_execute(admin, role, AUDIT_MERGE), False)
        require(
            f"{role} reverse wrapper closed",
            has_execute(admin, role, AUDIT_REVERSE),
            False,
        )
        require(f"{role} core merge closed", has_execute(admin, role, CORE_MERGE), False)
        require(f"{role} core reverse closed", has_execute(admin, role, CORE_REVERSE), False)
    require("api core merge closed", has_execute(admin, "uap_api", CORE_MERGE), False)
    require("api core reverse closed", has_execute(admin, "uap_api", CORE_REVERSE), False)


def g9_20(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    senior: uuid.UUID,
) -> uuid.UUID:
    source = insert_active_entity(admin, f"MergeSrc {uuid.uuid4().hex[:8]}")
    target = insert_active_entity(admin, f"MergeTgt {uuid.uuid4().hex[:8]}")
    merges_before = int(scalar(admin, "SELECT count(*) FROM core.entity_merge_events"))
    cases_before = int(scalar(admin, "SELECT count(*) FROM audit.review_cases"))
    grants_before = int(scalar(admin, "SELECT count(*) FROM audit.entity_publication_grants"))
    decisions_before = int(scalar(admin, "SELECT count(*) FROM audit.review_decisions"))
    outbox_before = int(scalar(admin, "SELECT count(*) FROM ops.outbox_events"))
    public_before = int(scalar(admin, "SELECT count(*) FROM public.claims"))
    jobs_before = int(
        scalar(admin, "SELECT count(*) FROM ops.jobs WHERE job_type LIKE 'publish_%'")
    )
    request = uuid.uuid4()
    event_id = call_api(api, senior, request, merge_sql(source, target))
    require("g9-20 source merged", entity_status(admin, source), "merged")
    require("g9-20 target active", entity_status(admin, target), "active")
    require("g9-20 canonical source", canonical_of(admin, source), target)
    require("g9-20 canonical target", canonical_of(admin, target), target)
    require("g9-20 one open merge", open_merge_count(admin, source), 1)
    require(
        "g9-20 merge events +1",
        int(scalar(admin, "SELECT count(*) FROM core.entity_merge_events")),
        merges_before + 1,
    )
    kind, merged_by, reversed_at = _merge_row(admin, event_id)
    require("g9-20 event kind", kind, "merge")
    require("g9-20 merged_by guc", merged_by, senior)
    require("g9-20 not reversed", reversed_at, None)
    require(
        "g9-20 review audit",
        event_count(admin, f"review.entity.merge:{request}"),
        1,
    )
    require(
        "g9-20 core audit",
        event_count(admin, f"entity.merge:{event_id}"),
        1,
    )
    require(
        "g9-20 no case",
        int(scalar(admin, "SELECT count(*) FROM audit.review_cases")),
        cases_before,
    )
    require(
        "g9-20 no decision",
        int(scalar(admin, "SELECT count(*) FROM audit.review_decisions")),
        decisions_before,
    )
    require(
        "g9-20 no grant",
        int(scalar(admin, "SELECT count(*) FROM audit.entity_publication_grants")),
        grants_before,
    )
    require(
        "g9-20 no outbox",
        int(scalar(admin, "SELECT count(*) FROM ops.outbox_events")),
        outbox_before,
    )
    require(
        "g9-20 no public",
        int(scalar(admin, "SELECT count(*) FROM public.claims")),
        public_before,
    )
    require(
        "g9-20 no publish jobs",
        int(scalar(admin, "SELECT count(*) FROM ops.jobs WHERE job_type LIKE 'publish_%'")),
        jobs_before,
    )
    return event_id


def _merge_row(
    admin: psycopg.Connection[Any], event_id: uuid.UUID
) -> tuple[str, uuid.UUID, object]:
    with admin.cursor() as cursor:
        cursor.execute(
            """
            SELECT event_kind, merged_by, reversed_at
              FROM core.entity_merge_events WHERE id = %s
            """,
            (event_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("missing merge event")
    return str(row[0]), uuid.UUID(str(row[1])), row[2]


def g9_21(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    senior: uuid.UUID,
) -> None:
    source = insert_active_entity(admin, f"DeniedSrc {uuid.uuid4().hex[:8]}")
    target = insert_active_entity(admin, f"DeniedTgt {uuid.uuid4().hex[:8]}")
    state, primary = sqlerror_tx(api, [*bind(reviewer, uuid.uuid4()), merge_sql(source, target)])
    require("g9-21 reviewer state", state, "42501")
    require("g9-21 reviewer code", primary, "review_role_denied")
    require("g9-21 source still active", entity_status(admin, source), "active")
    require("g9-21 target still active", entity_status(admin, target), "active")
    require("g9-21 no merge edge", open_merge_count(admin, source), 0)
    state, primary = sqlerror_tx(
        api,
        [
            *bind(senior, uuid.uuid4()),
            (
                "SELECT core.merge_entities(%s, %s, %s, %s)",
                (source, target, senior, MERGE_REASON),
            ),
        ],
    )
    require("g9-21 core sqlstate", state, "42501")
    require("g9-21 source unmerged", entity_status(admin, source), "active")
    worker = connect("uap_worker")
    try:
        state, _worker_primary = sqlerror_tx(
            worker, [*bind(senior, uuid.uuid4()), merge_sql(source, target)]
        )
    finally:
        worker.close()
    require("g9-21 worker state", state, "42501")
    require("g9-21 worker source active", entity_status(admin, source), "active")


def g9_22(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    senior: uuid.UUID,
) -> None:
    source = insert_active_entity(admin, f"RevSrc {uuid.uuid4().hex[:8]}")
    target = insert_active_entity(admin, f"RevTgt {uuid.uuid4().hex[:8]}")
    merge_id = call_api(api, senior, uuid.uuid4(), merge_sql(source, target))
    require("g9-22 pre-reverse merged", entity_status(admin, source), "merged")
    state, primary = sqlerror_tx(api, [*bind(reviewer, uuid.uuid4()), reverse_sql(merge_id)])
    require("g9-22 reviewer reverse state", state, "42501")
    require("g9-22 reviewer reverse code", primary, "review_role_denied")
    require("g9-22 still merged after reviewer", entity_status(admin, source), "merged")
    request = uuid.uuid4()
    reverse_id = call_api(api, senior, request, reverse_sql(merge_id))
    require("g9-22 reverse distinct", reverse_id != merge_id, True)
    require("g9-22 source restored", entity_status(admin, source), "active")
    require("g9-22 target active", entity_status(admin, target), "active")
    require("g9-22 no open merge", open_merge_count(admin, source), 0)
    require("g9-22 canonical source self", canonical_of(admin, source), source)
    require(
        "g9-22 reverse audit",
        event_count(admin, f"review.entity.merge_reverse:{request}"),
        1,
    )


def g9_37(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    senior: uuid.UUID,
) -> None:
    source = insert_active_entity(admin, f"IdemSrc {uuid.uuid4().hex[:8]}")
    target = insert_active_entity(admin, f"IdemTgt {uuid.uuid4().hex[:8]}")
    other_source = insert_active_entity(admin, f"IdemOther {uuid.uuid4().hex[:8]}")
    request = uuid.uuid4()
    first = call_api(api, senior, request, merge_sql(source, target))
    replay = call_api(api, senior, request, merge_sql(source, target))
    require("g9-37 merge replay", replay, first)
    require("g9-37 one open merge", open_merge_count(admin, source), 1)
    require(
        "g9-37 merge events",
        event_count(admin, f"review.entity.merge:{request}"),
        1,
    )
    require("g9-37 source stays merged", entity_status(admin, source), "merged")
    state, primary = sqlerror_tx(
        api, [*bind(senior, request), merge_sql(source, target, CONFLICT_REASON)]
    )
    require("g9-37 merge reason state", state, "23505")
    require("g9-37 merge reason code", primary, "review_idempotency_payload_conflict")
    state, primary = sqlerror_tx(api, [*bind(senior, request), merge_sql(other_source, target)])
    require("g9-37 merge source state", state, "23505")
    require("g9-37 merge source code", primary, "review_idempotency_payload_conflict")
    require("g9-37 other source unmerged", entity_status(admin, other_source), "active")
    state, primary = sqlerror_tx(api, [*bind(senior, request), merge_sql(source, other_source)])
    require("g9-37 merge target state", state, "23505")
    require("g9-37 merge target code", primary, "review_idempotency_payload_conflict")

    reverse_request = uuid.uuid4()
    reverse_id = call_api(api, senior, reverse_request, reverse_sql(first))
    replay_reverse = call_api(api, senior, reverse_request, reverse_sql(first))
    require("g9-37 reverse replay", replay_reverse, reverse_id)
    require("g9-37 source restored once", entity_status(admin, source), "active")
    require("g9-37 still no open merge", open_merge_count(admin, source), 0)
    require(
        "g9-37 reverse events",
        event_count(admin, f"review.entity.merge_reverse:{reverse_request}"),
        1,
    )
    state, primary = sqlerror_tx(
        api, [*bind(senior, reverse_request), reverse_sql(first, CONFLICT_REASON)]
    )
    require("g9-37 reverse reason state", state, "23505")
    require("g9-37 reverse reason code", primary, "review_idempotency_payload_conflict")
    other_merge = call_api(
        api,
        senior,
        uuid.uuid4(),
        merge_sql(
            insert_active_entity(admin, f"IdemAltS {uuid.uuid4().hex[:8]}"),
            insert_active_entity(admin, f"IdemAltT {uuid.uuid4().hex[:8]}"),
        ),
    )
    state, primary = sqlerror_tx(api, [*bind(senior, reverse_request), reverse_sql(other_merge)])
    require("g9-37 reverse id state", state, "23505")
    require("g9-37 reverse id code", primary, "review_idempotency_payload_conflict")
    require(
        "g9-37 other merge still open",
        int(
            scalar(
                admin,
                """
                SELECT count(*) FROM core.entity_merge_events
                 WHERE id = %s AND reversed_at IS NULL
                """,
                other_merge,
            )
        ),
        1,
    )


def extra_illegal_states(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    senior: uuid.UUID,
) -> None:
    source = insert_active_entity(admin, f"CaseSrc {uuid.uuid4().hex[:8]}")
    target = insert_active_entity(admin, f"CaseTgt {uuid.uuid4().hex[:8]}")
    case_id = call_api(
        api, reviewer, uuid.uuid4(), open_sql("entity", source, "open entity before merge")
    )
    cases_before = int(scalar(admin, "SELECT count(*) FROM audit.review_cases"))
    decisions_before = int(scalar(admin, "SELECT count(*) FROM audit.review_decisions"))
    merge_id = call_api(api, senior, uuid.uuid4(), merge_sql(source, target))
    require(
        "extra open case unchanged",
        str(
            scalar(
                admin,
                "SELECT status::text FROM audit.review_cases WHERE id = %s",
                case_id,
            )
        ),
        "open",
    )
    require(
        "extra merge does not add case",
        int(scalar(admin, "SELECT count(*) FROM audit.review_cases")),
        cases_before,
    )
    require(
        "extra merge does not add decision",
        int(scalar(admin, "SELECT count(*) FROM audit.review_decisions")),
        decisions_before,
    )
    state, primary = sqlerror_tx(
        api,
        [
            *bind(reviewer, uuid.uuid4()),
            open_sql("entity", source, "open merged entity case"),
        ],
    )
    require("extra merged open state", state, "22023")
    require("extra merged open code", primary, "review_subject_not_active")
    execute(admin, "UPDATE core.entities SET status = 'active' WHERE id = %s", source)
    state, primary = sqlerror_tx(
        api,
        [
            *bind(reviewer, uuid.uuid4()),
            open_sql("entity", source, "open non-canonical entity"),
        ],
    )
    require("extra non-canonical open state", state, "22023")
    require("extra non-canonical open code", primary, "review_subject_not_canonical")
    execute(admin, "UPDATE core.entities SET status = 'merged' WHERE id = %s", source)
    retired = insert_active_entity(admin, f"Retired {uuid.uuid4().hex[:8]}")
    execute(admin, "UPDATE core.entities SET status = 'retired' WHERE id = %s", retired)
    state, primary = sqlerror_tx(
        api,
        [*bind(reviewer, uuid.uuid4()), open_sql("entity", retired, "open retired entity")],
    )
    require("extra retired open state", state, "22023")
    require("extra retired open code", primary, "review_subject_not_active")
    live = insert_active_entity(admin, f"Live {uuid.uuid4().hex[:8]}")
    opened = call_api(
        api, reviewer, uuid.uuid4(), open_sql("entity", live, "open active canonical")
    )
    require("extra live case id", opened is not None, True)

    disputed = insert_active_entity(admin, f"Disp {uuid.uuid4().hex[:8]}")
    execute(admin, "UPDATE core.entities SET status = 'disputed' WHERE id = %s", disputed)
    partner = insert_active_entity(admin, f"DispT {uuid.uuid4().hex[:8]}")
    state, primary = sqlerror_tx(api, [*bind(senior, uuid.uuid4()), merge_sql(disputed, partner)])
    require("extra disputed merge state", state, "23514")
    require("extra disputed merge code", primary, "knowledge_merge_not_active")
    require("extra disputed unmerged", entity_status(admin, disputed), "disputed")
    require("extra partner unmerged", entity_status(admin, partner), "active")

    state, primary = sqlerror_tx(api, [*bind(senior, uuid.uuid4()), merge_sql(source, partner)])
    require("extra already merged state", state, "23514")
    require(
        "extra already merged code",
        primary in {"knowledge_merge_not_active", "knowledge_merge_not_canonical"},
        True,
    )
    missing = uuid.uuid4()
    state, primary = sqlerror_tx(api, [*bind(senior, uuid.uuid4()), merge_sql(missing, partner)])
    require("extra missing entity state", state, "23503")
    require("extra missing entity code", primary, "knowledge_merge_missing_entity")
    state, primary = sqlerror_tx(api, [*bind(senior, uuid.uuid4()), merge_sql(partner, partner)])
    require("extra same entity state", state, "23514")
    require("extra same entity code", primary, "knowledge_merge_same_entity")
    state, primary = sqlerror_tx(api, [*bind(senior, uuid.uuid4()), reverse_sql(uuid.uuid4())])
    require("extra missing event state", state, "23503")
    require("extra missing event code", primary, "knowledge_merge_missing_event")
    call_api(api, senior, uuid.uuid4(), reverse_sql(merge_id))
    require("extra reverse restored", entity_status(admin, source), "active")
    state, primary = sqlerror_tx(api, [*bind(senior, uuid.uuid4()), reverse_sql(merge_id)])
    require("extra reverse twice state", state, "23514")
    require("extra reverse twice code", primary, "knowledge_merge_already_reversed")
    state, primary = sqlerror_tx(
        api,
        [
            *bind(senior, None),
            merge_sql(partner, insert_active_entity(admin, f"NoReq {uuid.uuid4().hex[:8]}")),
        ],
    )
    require("extra missing request state", state, "42501")
    require("extra missing request code", primary, "review_request_id_missing")
    state, primary = sqlerror_tx(
        api, [*bind(senior, uuid.uuid4()), merge_sql(partner, partner, "short")]
    )
    require("extra short reason state", state, "22023")
    require("extra short reason code", primary, "review_reason_too_short")


def extra_concurrent_different_request(
    admin: psycopg.Connection[Any],
    senior: uuid.UUID,
    source: uuid.UUID,
    target: uuid.UUID,
) -> None:
    names = (
        f"wp95-diff-{uuid.uuid4().hex[:8]}-a",
        f"wp95-diff-{uuid.uuid4().hex[:8]}-b",
    )
    locker_held = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    ids: list[uuid.UUID] = []
    denied: list[tuple[str, str]] = []

    def locker() -> None:
        connection = connect()
        connection.autocommit = False
        try:
            with connection.cursor() as cursor:
                cursor.execute(GRAPH_LOCK_SQL)
            locker_held.set()
            if not release.wait(timeout=20):
                raise TimeoutError("different-request workers did not block")
            connection.commit()
        except BaseException as error:
            connection.rollback()
            errors.append(error)
        finally:
            connection.close()

    def worker(name: str) -> None:
        if not locker_held.wait(timeout=10):
            errors.append(TimeoutError("locker did not acquire graph lock"))
            return
        connection = connect("uap_api")
        connection.autocommit = False
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('application_name', %s, false)", (name,))
                for sql, params in [
                    *bind(senior, uuid.uuid4()),
                    merge_sql(source, target),
                ]:
                    cursor.execute(sql, params)
                row = cursor.fetchone()
            connection.commit()
            if row is None or row[0] is None:
                raise RuntimeError("different-request merge returned no id")
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
        raise RuntimeError("different-request locker failed")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, name) for name in names]
        deadline = time.monotonic() + 15
        blocked = False
        while time.monotonic() < deadline:
            if all(_waiter_blocked(admin, name) for name in names):
                blocked = True
                break
            time.sleep(0.05)
        require("extra concurrent different-request waiters blocked", blocked, True)
        release.set()
        locker_thread.join(timeout=10)
        for future in futures:
            future.result(timeout=30)
    require("extra concurrent different-request errors", [str(item) for item in errors], [])
    require("extra concurrent different-request one success", len(ids), 1)
    require("extra concurrent different-request one denial", len(denied), 1)
    require("extra concurrent different-request denied sqlstate", denied[0][0], "23514")
    require(
        "extra concurrent different-request denied code",
        denied[0][1] in {"knowledge_merge_not_active", "knowledge_merge_not_canonical"},
        True,
    )
    require("extra concurrent different-request one edge", open_merge_count(admin, source), 1)


def run_concurrency(
    admin: psycopg.Connection[Any],
    senior: uuid.UUID,
) -> None:
    source = insert_active_entity(admin, f"ConcSrc {uuid.uuid4().hex[:8]}")
    target = insert_active_entity(admin, f"ConcTgt {uuid.uuid4().hex[:8]}")
    request = uuid.uuid4()
    extra_concurrent_same_request(
        admin,
        senior,
        GRAPH_LOCK_SQL,
        (),
        merge_sql(source, target),
        request,
        "merge",
    )
    require(
        "extra concurrent merge events",
        event_count(admin, f"review.entity.merge:{request}"),
        1,
    )
    require("extra concurrent merge one edge", open_merge_count(admin, source), 1)

    conflict_src = insert_active_entity(admin, f"ConfSrc {uuid.uuid4().hex[:8]}")
    conflict_tgt = insert_active_entity(admin, f"ConfTgt {uuid.uuid4().hex[:8]}")
    extra_concurrent_payload_conflict(
        admin,
        senior,
        GRAPH_LOCK_SQL,
        (),
        merge_sql(conflict_src, conflict_tgt),
        merge_sql(conflict_src, conflict_tgt, CONFLICT_REASON),
        uuid.uuid4(),
        "merge",
    )

    reverse_request = uuid.uuid4()
    merge_event = uuid.UUID(
        str(
            scalar(
                admin,
                """
                SELECT id FROM core.entity_merge_events
                 WHERE source_entity_id = %s
                   AND event_kind = 'merge'
                   AND reversed_at IS NULL
                """,
                source,
            )
        )
    )
    extra_concurrent_same_request(
        admin,
        senior,
        GRAPH_LOCK_SQL,
        (),
        reverse_sql(merge_event),
        reverse_request,
        "reverse",
    )
    require(
        "extra concurrent reverse events",
        event_count(admin, f"review.entity.merge_reverse:{reverse_request}"),
        1,
    )
    require("extra concurrent reverse restored", entity_status(admin, source), "active")
    api = connect("uap_api")
    try:
        payload_src = insert_active_entity(admin, f"RevConfS {uuid.uuid4().hex[:8]}")
        payload_tgt = insert_active_entity(admin, f"RevConfT {uuid.uuid4().hex[:8]}")
        payload_merge = call_api(api, senior, uuid.uuid4(), merge_sql(payload_src, payload_tgt))
        extra_concurrent_payload_conflict(
            admin,
            senior,
            GRAPH_LOCK_SQL,
            (),
            reverse_sql(payload_merge),
            reverse_sql(payload_merge, CONFLICT_REASON),
            uuid.uuid4(),
            "reverse",
        )
        require(
            "extra concurrent reverse conflict restored",
            entity_status(admin, payload_src),
            "active",
        )
    finally:
        api.close()

    pair_a_src = insert_active_entity(admin, f"CrossASrc {uuid.uuid4().hex[:8]}")
    pair_a_tgt = insert_active_entity(admin, f"CrossATgt {uuid.uuid4().hex[:8]}")
    pair_b_src = insert_active_entity(admin, f"CrossBSrc {uuid.uuid4().hex[:8]}")
    pair_b_tgt = insert_active_entity(admin, f"CrossBTgt {uuid.uuid4().hex[:8]}")
    cross_request = uuid.uuid4()
    extra_concurrent_cross_resource(
        admin,
        senior,
        f"review.entity.merge:{cross_request}",
        merge_sql(pair_a_src, pair_a_tgt),
        merge_sql(pair_b_src, pair_b_tgt),
        cross_request,
        "merge",
    )
    require(
        "extra cross merge events",
        event_count(admin, f"review.entity.merge:{cross_request}"),
        1,
    )
    merged_a = open_merge_count(admin, pair_a_src)
    merged_b = open_merge_count(admin, pair_b_src)
    require("extra cross merge one winner", (merged_a == 1) != (merged_b == 1), True)
    if merged_a == 1:
        require("extra cross loser b unmerged", merged_b, 0)
        require("extra cross loser b active", entity_status(admin, pair_b_src), "active")
    else:
        require("extra cross loser a unmerged", merged_a, 0)
        require("extra cross loser a active", entity_status(admin, pair_a_src), "active")

    race_src = insert_active_entity(admin, f"RaceSrc {uuid.uuid4().hex[:8]}")
    race_tgt = insert_active_entity(admin, f"RaceTgt {uuid.uuid4().hex[:8]}")
    extra_concurrent_different_request(admin, senior, race_src, race_tgt)


def main() -> None:
    admin = connect()
    api = connect("uap_api")
    worker = connect("uap_worker")
    try:
        head = scalar(admin, "SELECT version_num FROM public.alembic_version")
        require("alembic head", head, CURRENT_HEAD)
        require("event key lock class", EVENT_KEY_LOCK_CLASS, 9175)
        require("event-key lock class", EVENT_KEY_LOCK_CLASS, 9175)
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
        g9_20(admin, api, senior)
        g9_21(admin, api, reviewer, senior)
        g9_22(admin, api, reviewer, senior)
        g9_37(admin, api, senior)
        extra_illegal_states(admin, api, reviewer, senior)
        run_concurrency(admin, senior)
        g8_16c(admin, worker, uuid.uuid4().hex[:12])
    finally:
        worker.close()
        api.close()
        admin.close()
    print("WP9.5 runtime probe passed: G9-20 G9-21 G9-22 G9-37 G8-16C")


if __name__ == "__main__":
    main()
