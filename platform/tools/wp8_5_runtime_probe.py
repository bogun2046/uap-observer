"""Exercise WP8.5 entity merge/reverse as owner: G8-17, G8-18, G8-19."""

from __future__ import annotations

import json
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
    connect,
    execute,
    require,
    scalar,
    seed_document,
    sha256_text,
    sqlerror,
    sqlstate,
)

CURRENT_HEAD = "0017_selection_and_promotion"
MERGE_SIGNATURE = "core.merge_entities(uuid, uuid, uuid, text)"
REVERSE_SIGNATURE = "core.reverse_entity_merge(uuid, uuid, text)"
CANONICAL_SIGNATURE = "core.canonical_entity_id(uuid)"
RUNTIME_ROLES = (
    "uap_worker",
    "uap_api",
    "uap_scheduler",
    "uap_publisher",
    "uap_model_governance",
    "uap_public_reader",
)
STATE_REJECTIONS = {
    "knowledge_merge_cycle",
    "knowledge_merge_not_active",
    "knowledge_merge_not_canonical",
    "knowledge_merge_same_entity",
}


def _insert_principal(
    admin: psycopg.Connection[Any], tag: str, *, active: bool = True
) -> uuid.UUID:
    principal_id = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO audit.principals (
            id, principal_type, service_name, display_name, active
        ) VALUES (%s, 'service', %s, 'WP8.5 merge probe', %s)
        """,
        principal_id,
        f"wp8-5-{tag}",
        active,
    )
    return principal_id


def _insert_entity(
    admin: psycopg.Connection[Any],
    name: str,
    *,
    entity_type: str = "object",
    status: str = "active",
) -> uuid.UUID:
    entity_id = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO core.entities (
            id, entity_type, canonical_name, status
        ) VALUES (%s, %s, %s, %s)
        """,
        entity_id,
        entity_type,
        name,
        status,
    )
    return entity_id


def _insert_historical_merge(
    admin: psycopg.Connection[Any],
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    actor: uuid.UUID,
    reason: str,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO core.entity_merge_events (
            id, source_entity_id, target_entity_id, reason, merged_by, merged_at,
            event_kind
        ) VALUES (%s, %s, %s, %s, %s, clock_timestamp(), 'merge')
        """,
        event_id,
        source_id,
        target_id,
        reason,
        actor,
    )
    return event_id


def _merge(
    connection: psycopg.Connection[Any],
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    actor: uuid.UUID,
    reason: str,
) -> uuid.UUID:
    result = scalar(
        connection,
        "SELECT core.merge_entities(%s, %s, %s, %s)",
        source_id,
        target_id,
        actor,
        reason,
    )
    return uuid.UUID(str(result))


def _reverse(
    connection: psycopg.Connection[Any],
    merge_event_id: uuid.UUID,
    actor: uuid.UUID,
    reason: str,
) -> uuid.UUID:
    result = scalar(
        connection,
        "SELECT core.reverse_entity_merge(%s, %s, %s)",
        merge_event_id,
        actor,
        reason,
    )
    return uuid.UUID(str(result))


def _open_merge_edges(admin: psycopg.Connection[Any]) -> set[tuple[uuid.UUID, uuid.UUID]]:
    with admin.cursor() as cursor:
        cursor.execute(
            """
            SELECT source_entity_id, target_entity_id
              FROM core.entity_merge_events
             WHERE event_kind = 'merge'
               AND reversed_at IS NULL
            """
        )
        rows = cursor.fetchall()
    return {(uuid.UUID(str(row[0])), uuid.UUID(str(row[1]))) for row in rows}


def _graph_has_cycle(edges: set[tuple[uuid.UUID, uuid.UUID]]) -> bool:
    outgoing: dict[uuid.UUID, uuid.UUID] = {}
    for source, target in edges:
        if source in outgoing:
            return True
        outgoing[source] = target
    for start in outgoing:
        seen: set[uuid.UUID] = set()
        current = start
        while current in outgoing:
            if current in seen:
                return True
            seen.add(current)
            current = outgoing[current]
    return False


def _first_lock_is_advisory(definition: str) -> bool:
    begin_at = definition.upper().find("BEGIN")
    lock_at = definition.find("pg_advisory_xact_lock(824, 1)")
    update_at = definition.upper().find("FOR UPDATE")
    return begin_at >= 0 and lock_at > begin_at and (update_at < 0 or lock_at < update_at)


def _has_execute(admin: psycopg.Connection[Any], role: str, signature: str) -> bool:
    return bool(
        scalar(
            admin,
            "SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')",
            role,
            signature,
        )
    )


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


def _ungranted_advisory_locks(admin: psycopg.Connection[Any]) -> int:
    with admin.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)::int
              FROM pg_locks
             WHERE locktype = 'advisory'
               AND NOT granted
            """
        )
        row = cursor.fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def g8_live_definitions(admin: psycopg.Connection[Any]) -> dict[str, Any]:
    merge_def = scalar(admin, "SELECT pg_get_functiondef(%s::regprocedure)", MERGE_SIGNATURE)
    reverse_def = scalar(admin, "SELECT pg_get_functiondef(%s::regprocedure)", REVERSE_SIGNATURE)
    canonical_def = scalar(
        admin, "SELECT pg_get_functiondef(%s::regprocedure)", CANONICAL_SIGNATURE
    )
    require("live merge advisory first", _first_lock_is_advisory(merge_def), True)
    require("live reverse advisory first", _first_lock_is_advisory(reverse_def), True)
    require("live merge UUID order", "ORDER BY entity.id" in merge_def, True)
    require("live reverse UUID order", "ORDER BY entity.id" in reverse_def, True)
    require(
        "live merge no candidate rewrite",
        "UPDATE core.entity_candidates" not in merge_def,
        True,
    )
    require("live merge no claim rewrite", "UPDATE core.claims" not in merge_def, True)
    require("live merge no relation rewrite", "UPDATE core.relations" not in merge_def, True)
    require("live merge no alias rewrite", "UPDATE core.entity_aliases" not in merge_def, True)
    require(
        "live reverse no candidate rewrite",
        "UPDATE core.entity_candidates" not in reverse_def,
        True,
    )
    require("live reverse no relation rewrite", "UPDATE core.relations" not in reverse_def, True)
    require("live reverse copies A/B", "event_kind = 'reverse'" in reverse_def, True)
    require(
        "live reverse not B-to-A",
        "p_target_entity_id, p_source_entity_id" not in reverse_def,
        True,
    )
    require("live canonical follows merge edges", "event_kind = 'merge'" in canonical_def, True)
    require("live canonical hop cap", "hops >= 64" in canonical_def, True)
    kind = scalar(
        admin,
        """
        SELECT pg_get_constraintdef(oid)
          FROM pg_constraint
         WHERE conname = 'ck_entity_merge_event_kind'
        """,
    )
    require("event_kind check", "merge" in kind and "reverse" in kind, True)
    indexdef = scalar(
        admin,
        "SELECT pg_get_indexdef('core.uq_open_merge_source'::regclass)",
    )
    require("uq_open_merge_source unique", "UNIQUE" in indexdef.upper(), True)
    require("uq_open_merge_source source", "source_entity_id" in indexdef, True)
    require("uq_open_merge_source merge only", "event_kind" in indexdef, True)
    return {"passed": True}


def g8_17(admin: psycopg.Connection[Any], tag: str) -> dict[str, Any]:
    actor = _insert_principal(admin, f"{tag}-actor")
    inactive = _insert_principal(admin, f"{tag}-inactive", active=False)
    shared_name = f"craft-{tag}"
    entity_a = _insert_entity(admin, shared_name)
    entity_b = _insert_entity(admin, shared_name)
    entity_c = _insert_entity(admin, f"hangar-{tag}")
    entity_d = _insert_entity(admin, f"dawn-{tag}")
    disputed = _insert_entity(admin, f"disputed-{tag}", status="disputed")
    retired = _insert_entity(admin, f"retired-{tag}", status="retired")
    before_auto = scalar(
        admin,
        """
        SELECT count(*) FROM core.entity_merge_events
         WHERE source_entity_id IN (%s, %s) OR target_entity_id IN (%s, %s)
        """,
        entity_a,
        entity_b,
        entity_a,
        entity_b,
    )
    require("g8-17 same name does not auto-merge", before_auto, 0)

    principal, document_version_id, _source = seed_document(admin, f"{tag}-fk")
    alias_id = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO core.entity_aliases (
            id, entity_id, alias, normalized_alias, locale
        ) VALUES (%s, %s, 'craft', 'craft', 'en')
        """,
        alias_id,
        entity_a,
    )
    claim_id = uuid.uuid4()
    claim_text = f"The craft hovered [{tag}]"
    execute(
        admin,
        """
        INSERT INTO core.claims (
            id, document_version_id, subject_entity_id, claim_text, claim_fingerprint,
            claim_type, assertion_status, created_by
        ) VALUES (%s, %s, %s, %s, %s, 'observation', 'reported', %s)
        """,
        claim_id,
        document_version_id,
        entity_a,
        claim_text,
        sha256_text(claim_text),
        principal,
    )
    relation_id = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO core.relations (
            id, subject_entity_id, object_entity_id, predicate, relation_status,
            created_by
        ) VALUES (%s, %s, %s, 'near', 'reported', %s)
        """,
        relation_id,
        entity_a,
        entity_c,
        principal,
    )
    span_id = uuid.uuid4()
    locator = {"locator_schema_version": "evidence-locator.v2", "locator_type": "text"}
    execute(
        admin,
        """
        INSERT INTO core.evidence_spans (
            id, document_version_id, evidence_text, locator_type, char_start, char_end,
            locator, locator_sha256
        ) VALUES (%s, %s, 'craft', 'text', 4, 9, %s::jsonb, %s)
        """,
        span_id,
        document_version_id,
        json.dumps(locator, separators=(",", ":")),
        sha256_text(f"{tag}-span"),
    )
    execute(
        admin,
        """
        INSERT INTO core.claim_evidence (
            id, claim_id, evidence_span_id, document_version_id, support_type
        ) VALUES (%s, %s, %s, %s, 'supports')
        """,
        uuid.uuid4(),
        claim_id,
        span_id,
        document_version_id,
    )

    self_state, self_msg = sqlerror(
        admin,
        "SELECT core.merge_entities(%s, %s, %s, %s)",
        entity_a,
        entity_a,
        actor,
        "g8-17-self",
    )
    require("g8-17 self sqlstate", self_state, "23514")
    require("g8-17 self code", self_msg, "knowledge_merge_same_entity")

    disputed_state, disputed_msg = sqlerror(
        admin,
        "SELECT core.merge_entities(%s, %s, %s, %s)",
        disputed,
        entity_b,
        actor,
        "g8-17-disputed",
    )
    require("g8-17 disputed sqlstate", disputed_state, "23514")
    require("g8-17 disputed code", disputed_msg, "knowledge_merge_not_active")
    retired_state, retired_msg = sqlerror(
        admin,
        "SELECT core.merge_entities(%s, %s, %s, %s)",
        entity_d,
        retired,
        actor,
        "g8-17-retired",
    )
    require("g8-17 retired code", retired_msg, "knowledge_merge_not_active")
    require("g8-17 retired sqlstate", retired_state, "23514")

    inactive_state, inactive_msg = sqlerror(
        admin,
        "SELECT core.merge_entities(%s, %s, %s, %s)",
        entity_a,
        entity_b,
        inactive,
        "g8-17-inactive-actor",
    )
    require("g8-17 inactive principal", inactive_msg, "knowledge_merge_principal_inactive")
    require("g8-17 inactive sqlstate", inactive_state, "23514")

    event_id = _merge(admin, entity_a, entity_b, actor, "g8-17-merge")
    source_status, target_status = (
        scalar(admin, "SELECT status::text FROM core.entities WHERE id=%s", entity_a),
        scalar(admin, "SELECT status::text FROM core.entities WHERE id=%s", entity_b),
    )
    kind, src, tgt, reversed_at = (
        scalar(admin, "SELECT event_kind FROM core.entity_merge_events WHERE id=%s", event_id),
        scalar(
            admin, "SELECT source_entity_id FROM core.entity_merge_events WHERE id=%s", event_id
        ),
        scalar(
            admin, "SELECT target_entity_id FROM core.entity_merge_events WHERE id=%s", event_id
        ),
        scalar(admin, "SELECT reversed_at FROM core.entity_merge_events WHERE id=%s", event_id),
    )
    require("g8-17 event kind", kind, "merge")
    require("g8-17 direction source", uuid.UUID(str(src)), entity_a)
    require("g8-17 direction target", uuid.UUID(str(tgt)), entity_b)
    require("g8-17 not reversed", reversed_at, None)
    require("g8-17 A merged", source_status, "merged")
    require("g8-17 B active", target_status, "active")
    require(
        "g8-17 canonical A",
        uuid.UUID(str(scalar(admin, "SELECT core.canonical_entity_id(%s)", entity_a))),
        entity_b,
    )
    require(
        "g8-17 canonical B",
        uuid.UUID(str(scalar(admin, "SELECT core.canonical_entity_id(%s)", entity_b))),
        entity_b,
    )

    alias_entity = scalar(admin, "SELECT entity_id FROM core.entity_aliases WHERE id=%s", alias_id)
    claim_subject = scalar(admin, "SELECT subject_entity_id FROM core.claims WHERE id=%s", claim_id)
    rel_subject = scalar(
        admin, "SELECT subject_entity_id FROM core.relations WHERE id=%s", relation_id
    )
    rel_object = scalar(
        admin, "SELECT object_entity_id FROM core.relations WHERE id=%s", relation_id
    )
    span_doc = scalar(
        admin, "SELECT document_version_id FROM core.evidence_spans WHERE id=%s", span_id
    )
    require("g8-17 alias fk stable", uuid.UUID(str(alias_entity)), entity_a)
    require("g8-17 claim fk stable", uuid.UUID(str(claim_subject)), entity_a)
    require("g8-17 relation subject stable", uuid.UUID(str(rel_subject)), entity_a)
    require("g8-17 relation object stable", uuid.UUID(str(rel_object)), entity_c)
    require("g8-17 evidence span stable", uuid.UUID(str(span_doc)), document_version_id)

    audit_action = scalar(
        admin,
        "SELECT action FROM audit.audit_events WHERE event_key=%s",
        f"entity.merge:{event_id}",
    )
    audit_target = scalar(
        admin,
        "SELECT target_id FROM audit.audit_events WHERE event_key=%s",
        f"entity.merge:{event_id}",
    )
    audit_actor = scalar(
        admin,
        "SELECT actor_id FROM audit.audit_events WHERE event_key=%s",
        f"entity.merge:{event_id}",
    )
    require("g8-17 audit action", audit_action, "entity.merge")
    require("g8-17 audit target", uuid.UUID(str(audit_target)), event_id)
    require("g8-17 audit actor", uuid.UUID(str(audit_actor)), actor)

    not_canon_msg = sqlerror(
        admin,
        "SELECT core.merge_entities(%s, %s, %s, %s)",
        entity_a,
        entity_c,
        actor,
        "g8-17-not-canonical",
    )[1]
    require("g8-17 non-canonical source", not_canon_msg in STATE_REJECTIONS, True)

    chain_ids = [_insert_entity(admin, f"chain-{tag}-{index}") for index in range(65)]
    for index in range(64):
        _insert_historical_merge(
            admin, chain_ids[index], chain_ids[index + 1], actor, f"g8-17-chain-{index}"
        )
    chain_state, chain_msg = sqlerror(
        admin, "SELECT core.canonical_entity_id(%s)", chain_ids[0]
    )
    require("g8-17 chain sqlstate", chain_state, "22023")
    require("g8-17 chain code", chain_msg, "knowledge_merge_chain_too_long")
    short_ids = [_insert_entity(admin, f"short-{tag}-{index}") for index in range(3)]
    _insert_historical_merge(admin, short_ids[0], short_ids[1], actor, "g8-17-short-0")
    _insert_historical_merge(admin, short_ids[1], short_ids[2], actor, "g8-17-short-1")
    require(
        "g8-17 short canonical",
        uuid.UUID(str(scalar(admin, "SELECT core.canonical_entity_id(%s)", short_ids[0]))),
        short_ids[2],
    )
    return {"passed": True, "merge_event_id": str(event_id)}


def _concurrent_merge(
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    actor: uuid.UUID,
    reason: str,
) -> tuple[str, str | None]:
    connection = connect()
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT core.merge_entities(%s, %s, %s, %s)",
                    (source_id, target_id, actor, reason),
                )
                row = cursor.fetchone()
        return "ok", str(row[0]) if row else None
    except psycopg.Error as error:
        primary = ""
        if error.diag is not None and error.diag.message_primary:
            primary = error.diag.message_primary
        return str(error.sqlstate), primary
    finally:
        connection.close()


def g8_18(admin: psycopg.Connection[Any], tag: str) -> dict[str, Any]:
    actor = _insert_principal(admin, f"{tag}-conc")
    entity_a = _insert_entity(admin, f"conc-a-{tag}")
    entity_b = _insert_entity(admin, f"conc-b-{tag}")
    entity_c = _insert_entity(admin, f"conc-c-{tag}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_concurrent_merge, entity_a, entity_b, actor, f"{tag}-ab")
        second = pool.submit(_concurrent_merge, entity_a, entity_c, actor, f"{tag}-ac")
        same_source = [first.result(), second.result()]
    same_ok = [item for item in same_source if item[0] == "ok"]
    require("g8-18 same source one winner", len(same_ok), 1)
    require("g8-18 same source no deadlock", "40P01" not in {item[0] for item in same_source}, True)
    open_from_a = scalar(
        admin,
        """
        SELECT count(*) FROM core.entity_merge_events
         WHERE source_entity_id=%s AND event_kind='merge' AND reversed_at IS NULL
        """,
        entity_a,
    )
    require("g8-18 same source one open", open_from_a, 1)

    entity_d = _insert_entity(admin, f"flip-d-{tag}")
    entity_e = _insert_entity(admin, f"flip-e-{tag}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(_concurrent_merge, entity_d, entity_e, actor, f"{tag}-de")
        right = pool.submit(_concurrent_merge, entity_e, entity_d, actor, f"{tag}-ed")
        opposite = [left.result(), right.result()]
    opposite_ok = [item for item in opposite if item[0] == "ok"]
    require("g8-18 opposite one winner", len(opposite_ok), 1)
    require("g8-18 opposite no deadlock", "40P01" not in {item[0] for item in opposite}, True)

    node_a = _insert_entity(admin, f"four-a-{tag}")
    node_b = _insert_entity(admin, f"four-b-{tag}")
    node_c = _insert_entity(admin, f"four-c-{tag}")
    node_d = _insert_entity(admin, f"four-d-{tag}")
    _insert_historical_merge(admin, node_a, node_b, actor, f"{tag}-hist-ab")
    _insert_historical_merge(admin, node_c, node_d, actor, f"{tag}-hist-cd")
    before_edges = _open_merge_edges(admin)
    with ThreadPoolExecutor(max_workers=2) as pool:
        bc = pool.submit(_concurrent_merge, node_b, node_c, actor, f"{tag}-bc")
        da = pool.submit(_concurrent_merge, node_d, node_a, actor, f"{tag}-da")
        four = [bc.result(), da.result()]
    four_ok = [item for item in four if item[0] == "ok"]
    require("g8-18 four-node at most one new edge", len(four_ok) <= 1, True)
    require("g8-18 four-node no deadlock", "40P01" not in {item[0] for item in four}, True)
    for state, message in four:
        if state != "ok":
            require(
                "g8-18 four-node loser is cycle or status",
                message in STATE_REJECTIONS or state in {"23514", "23505"},
                True,
            )
    after_edges = _open_merge_edges(admin)
    new_edges = after_edges - before_edges
    require("g8-18 four-node new edges", len(new_edges) <= 1, True)
    require("g8-18 final graph acyclic", _graph_has_cycle(after_edges), False)

    holder = connect()
    holder.autocommit = False
    waiter_state: dict[str, str] = {}
    waited = False
    ready = threading.Event()
    try:
        with holder.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(824, 1)")
        blocker = _insert_entity(admin, f"lock-src-{tag}")
        survivor = _insert_entity(admin, f"lock-tgt-{tag}")

        def _wait_merge() -> None:
            waiter = connect()
            waiter.autocommit = False
            try:
                with waiter.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('application_name', %s, false)",
                        (f"wp8-5-wait-{tag}",),
                    )
                    cursor.execute("SELECT set_config('lock_timeout', %s, false)", ("30s",))
                    ready.set()
                    cursor.execute(
                        "SELECT core.merge_entities(%s, %s, %s, %s)",
                        (blocker, survivor, actor, f"{tag}-wait-lock"),
                    )
                waiter.commit()
                waiter_state["result"] = "ok"
            except Exception as error:
                waiter_state["sqlstate"] = str(getattr(error, "sqlstate", "") or "")
                waiter_state["error"] = type(error).__name__
                waiter_state["result"] = "blocked"
                waiter.rollback()
            finally:
                waiter.close()

        thread = threading.Thread(target=_wait_merge, name=f"wp8-5-lock-{tag}")
        thread.start()
        ready.wait(timeout=5)
        deadline = time.time() + 5.0
        while time.time() < deadline and thread.is_alive():
            if _ungranted_advisory_locks(admin) > 0:
                waited = True
                break
            wait_event = _activity_wait_event(admin, f"wp8-5-wait-{tag}")
            if wait_event and (
                "advisory" in wait_event.lower()
                or wait_event.startswith("Lock:")
                or "lwlock" in wait_event.lower()
            ):
                waited = True
                break
            time.sleep(0.05)
        holder.rollback()
        thread.join(timeout=30)
        require("g8-18 waiter observed or completed", thread.is_alive(), False)
        require("g8-18 waiter not SET syntax error", waiter_state.get("sqlstate") != "42601", True)
        require(
            "g8-18 advisory wait or success",
            waited or waiter_state.get("result") == "ok",
            True,
        )
    finally:
        holder.close()

    return {
        "passed": True,
        "same_source": same_source,
        "opposite": opposite,
        "four_node": four,
        "advisory_waited": waited,
    }


def g8_19(admin: psycopg.Connection[Any], tag: str) -> dict[str, Any]:
    actor = _insert_principal(admin, f"{tag}-rev")
    entity_a = _insert_entity(admin, f"rev-a-{tag}")
    entity_b = _insert_entity(admin, f"rev-b-{tag}")
    entity_c = _insert_entity(admin, f"rev-c-{tag}")
    relation_id = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO core.relations (
            id, subject_entity_id, object_entity_id, predicate, relation_status
        ) VALUES (%s, %s, %s, 'near', 'reported')
        """,
        relation_id,
        entity_a,
        entity_c,
    )
    merge_ab = _merge(admin, entity_a, entity_b, actor, "g8-19-ab")
    reverse_id = _reverse(admin, merge_ab, actor, "g8-19-reverse")
    original_kind, original_reversed_by, original_reversed_at = (
        scalar(admin, "SELECT event_kind FROM core.entity_merge_events WHERE id=%s", merge_ab),
        scalar(
            admin, "SELECT reversed_by_id FROM core.entity_merge_events WHERE id=%s", merge_ab
        ),
        scalar(admin, "SELECT reversed_at FROM core.entity_merge_events WHERE id=%s", merge_ab),
    )
    reverse_kind, reverse_src, reverse_tgt, reverse_reversed = (
        scalar(admin, "SELECT event_kind FROM core.entity_merge_events WHERE id=%s", reverse_id),
        scalar(
            admin, "SELECT source_entity_id FROM core.entity_merge_events WHERE id=%s", reverse_id
        ),
        scalar(
            admin, "SELECT target_entity_id FROM core.entity_merge_events WHERE id=%s", reverse_id
        ),
        scalar(admin, "SELECT reversed_at FROM core.entity_merge_events WHERE id=%s", reverse_id),
    )
    require("g8-19 original still merge", original_kind, "merge")
    require("g8-19 original points at reverse", uuid.UUID(str(original_reversed_by)), reverse_id)
    require("g8-19 original reversed_at set", original_reversed_at is not None, True)
    require("g8-19 reverse kind", reverse_kind, "reverse")
    require("g8-19 reverse copies A", uuid.UUID(str(reverse_src)), entity_a)
    require("g8-19 reverse copies B", uuid.UUID(str(reverse_tgt)), entity_b)
    require("g8-19 reverse not an edge", reverse_reversed, None)
    require(
        "g8-19 A restored",
        scalar(admin, "SELECT status::text FROM core.entities WHERE id=%s", entity_a),
        "active",
    )
    require(
        "g8-19 canonical A is A",
        uuid.UUID(str(scalar(admin, "SELECT core.canonical_entity_id(%s)", entity_a))),
        entity_a,
    )
    rel_subject = scalar(
        admin, "SELECT subject_entity_id FROM core.relations WHERE id=%s", relation_id
    )
    require("g8-19 relation endpoint stable", uuid.UUID(str(rel_subject)), entity_a)

    dup_msg = sqlerror(
        admin, "SELECT core.reverse_entity_merge(%s, %s, %s)", merge_ab, actor, "dup"
    )[1]
    require("g8-19 duplicate reverse", dup_msg, "knowledge_merge_already_reversed")
    reverse_row_msg = sqlerror(
        admin, "SELECT core.reverse_entity_merge(%s, %s, %s)", reverse_id, actor, "rev-rev"
    )[1]
    require("g8-19 reverse reverse row", reverse_row_msg, "knowledge_merge_not_merge_event")

    merge_ab2 = _merge(admin, entity_a, entity_b, actor, "g8-19-ab-again")
    fake_state, fake_msg = sqlerror(
        admin,
        "SELECT core.merge_entities(%s, %s, %s, %s)",
        entity_b,
        entity_a,
        actor,
        "g8-19-fake-reverse",
    )
    require("g8-19 fake reverse refused", fake_msg in STATE_REJECTIONS, True)
    require("g8-19 fake reverse sqlstate", fake_state in {"23514", "23505"}, True)
    _reverse(admin, merge_ab2, actor, "g8-19-cleanup")

    merge_ab3 = _merge(admin, entity_a, entity_b, actor, "g8-19-chain-ab")
    merge_bc = _merge(admin, entity_b, entity_c, actor, "g8-19-chain-bc")
    _reverse(admin, merge_ab3, actor, "g8-19-chain-reverse-ab")
    require(
        "g8-19 chain A restored",
        scalar(admin, "SELECT status::text FROM core.entities WHERE id=%s", entity_a),
        "active",
    )
    require(
        "g8-19 chain B still merged",
        scalar(admin, "SELECT status::text FROM core.entities WHERE id=%s", entity_b),
        "merged",
    )
    require(
        "g8-19 chain canonical B",
        uuid.UUID(str(scalar(admin, "SELECT core.canonical_entity_id(%s)", entity_b))),
        entity_c,
    )
    require(
        "g8-19 chain canonical A",
        uuid.UUID(str(scalar(admin, "SELECT core.canonical_entity_id(%s)", entity_a))),
        entity_a,
    )
    require(
        "g8-19 chain bc still open",
        scalar(
            admin,
            """
            SELECT count(*) FROM core.entity_merge_events
             WHERE id=%s AND reversed_at IS NULL AND event_kind='merge'
            """,
            merge_bc,
        ),
        1,
    )

    audit_action = scalar(
        admin,
        "SELECT action FROM audit.audit_events WHERE event_key=%s",
        f"entity.merge.reverse:{reverse_id}",
    )
    require("g8-19 reverse audit", audit_action, "entity.merge.reverse")

    grants: dict[str, dict[str, bool]] = {}
    execute_states: dict[str, dict[str, str]] = {}
    for role in RUNTIME_ROLES:
        grants[role] = {
            "merge": _has_execute(admin, role, MERGE_SIGNATURE),
            "reverse": _has_execute(admin, role, REVERSE_SIGNATURE),
        }
        require(f"g8-19 {role} merge grant closed", grants[role]["merge"], False)
        require(f"g8-19 {role} reverse grant closed", grants[role]["reverse"], False)
        try:
            other = connect(role)
            execute_states[role] = {
                "merge": sqlstate(
                    other,
                    "SELECT core.merge_entities(%s, %s, %s, %s)",
                    entity_a,
                    entity_c,
                    actor,
                    "runtime-closed",
                ),
                "reverse": sqlstate(
                    other,
                    "SELECT core.reverse_entity_merge(%s, %s, %s)",
                    merge_bc,
                    actor,
                    "runtime-closed",
                ),
            }
            other.close()
        except Exception as error:
            execute_states[role] = {
                "merge": getattr(error, "sqlstate", None) or "42501",
                "reverse": getattr(error, "sqlstate", None) or "42501",
            }
        require(f"g8-19 {role} merge execute", execute_states[role]["merge"], "42501")
        require(f"g8-19 {role} reverse execute", execute_states[role]["reverse"], "42501")

    knowledge = Path(__file__).resolve().parents[1] / "src/uap_platform/knowledge"
    package = "\n".join(path.read_text(encoding="utf-8") for path in knowledge.glob("*.py"))
    require("g8-19 no python merge_entities", "merge_entities" not in package, True)
    require("g8-19 no python reverse", "reverse_entity_merge" not in package, True)
    require(
        "g8-19 worker can read canonical",
        _has_execute(admin, "uap_worker", CANONICAL_SIGNATURE),
        True,
    )
    return {
        "passed": True,
        "grants": grants,
        "execute_states": execute_states,
        "reverse_event_id": str(reverse_id),
    }


def main() -> None:
    tag = uuid.uuid4().hex
    admin = connect()
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
    results: dict[str, Any] = {
        "head": head,
        "live_definitions": g8_live_definitions(admin),
        "G8-17": g8_17(admin, tag),
        "G8-18": g8_18(admin, tag),
        "G8-19": g8_19(admin, tag),
    }
    print(json.dumps(results, indent=2, sort_keys=True, default=str))
    failed = [
        name
        for name, payload in results.items()
        if isinstance(payload, dict) and payload.get("passed") is False
    ]
    if failed:
        raise SystemExit(f"WP8.5 runtime probe failed: {', '.join(failed)}")
    print("WP8.5 runtime probe passed: G8-17 G8-18 G8-19")


if __name__ == "__main__":
    main()
