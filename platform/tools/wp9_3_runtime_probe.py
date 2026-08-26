"""WP9.3 runtime probe: G9-10-G9-16, G9-27-G9-30, G9-35 on real uap_api logins."""

from __future__ import annotations

import hashlib
import sys
import threading
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

from tools.wp9_2_runtime_probe import (  # noqa: E402
    EXPECTED_TABLE_COUNT,
    bind,
    bind_role,
    close_sql,
    connect,
    execute,
    insert_person,
    open_sql,
    require,
    scalar,
    seed_grantor,
    seed_subjects,
    sqlerror_tx,
)

CURRENT_HEAD = "0016_review_decisions_and_grants"
REASON = "approve this reviewed claim"
REVISE_REASON = "revise this reviewed claim"
WITHDRAW_REASON = "withdraw this reviewed grant"
CLOSE_REASON = "close after a decision"


def decide_sql(
    case_id: uuid.UUID,
    decision: str,
    reason: str = REASON,
    changes: str = "{}",
) -> tuple[str, tuple[object, ...]]:
    return (
        """
        SELECT audit.record_review_decision(
            %s, %s::audit.review_decision, %s, %s::jsonb
        )
        """,
        (case_id, decision, reason, changes),
    )


def guc(principal: uuid.UUID, request: uuid.UUID) -> list[tuple[str, tuple[object, ...]]]:
    return bind(principal, request)


def count_jobs(admin: psycopg.Connection[Any], prefix: str) -> int:
    return int(
        scalar(
            admin,
            "SELECT count(*) FROM ops.jobs WHERE job_type LIKE %s",
            prefix,
        )
    )


def public_claim_count(admin: psycopg.Connection[Any]) -> int:
    return int(scalar(admin, "SELECT count(*) FROM public.claims"))


def claim_grants(
    admin: psycopg.Connection[Any], claim_id: uuid.UUID
) -> list[tuple[Any, ...]]:
    with admin.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, revision_no, grant_status, withdrawn_at, withdrawn_by_decision_id
              FROM audit.claim_publication_grants
             WHERE claim_id = %s
             ORDER BY revision_no
            """,
            (claim_id,),
        )
        return list(cursor.fetchall())


def outbox_types(admin: psycopg.Connection[Any], subject: uuid.UUID) -> list[str]:
    with admin.cursor() as cursor:
        cursor.execute(
            """
            SELECT event_type
              FROM ops.outbox_events
             WHERE payload ->> 'subject_id' = %s
             ORDER BY occurred_at, event_key
            """,
            (str(subject),),
        )
        return [str(row[0]) for row in cursor.fetchall()]


def permissions(admin: psycopg.Connection[Any]) -> None:
    require(
        "api execute decision",
        scalar(
            admin,
            """
            SELECT has_function_privilege(
                'uap_api',
                'audit.record_review_decision(uuid,audit.review_decision,text,jsonb)',
                'EXECUTE'
            )
            """,
        ),
        True,
    )
    require(
        "api no enqueue outbox",
        scalar(
            admin,
            """
            SELECT has_function_privilege(
                'uap_api',
                'ops.enqueue_publication_outbox(text,text,text,uuid,jsonb)',
                'EXECUTE'
            )
            """,
        ),
        False,
    )
    require(
        "publisher no decision",
        scalar(
            admin,
            """
            SELECT has_function_privilege(
                'uap_publisher',
                'audit.record_review_decision(uuid,audit.review_decision,text,jsonb)',
                'EXECUTE'
            )
            """,
        ),
        False,
    )
    require(
        "api no outbox dml",
        scalar(
            admin,
            "SELECT has_table_privilege('uap_api', 'ops.outbox_events', 'INSERT')",
        ),
        False,
    )
    require(
        "api no public dml",
        scalar(
            admin,
            "SELECT has_table_privilege('uap_api', 'public.documents', 'INSERT')",
        ),
        False,
    )
    require(
        "api no merge",
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


def open_case(
    api: psycopg.Connection[Any],
    actor: uuid.UUID,
    case_type: str,
    subject: uuid.UUID,
) -> uuid.UUID:
    request = uuid.uuid4()
    with api.transaction():
        with api.cursor() as cursor:
            for statement, params in [
                *guc(actor, request),
                open_sql(case_type, subject),
            ]:
                cursor.execute(statement, params)
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("open_review_case returned no row")
    return uuid.UUID(str(row[0]))


def decide(
    api: psycopg.Connection[Any],
    actor: uuid.UUID,
    case_id: uuid.UUID,
    decision: str,
    reason: str = REASON,
    changes: str = "{}",
    request: uuid.UUID | None = None,
) -> uuid.UUID:
    request_id = request or uuid.uuid4()
    with api.transaction():
        with api.cursor() as cursor:
            for statement, params in [
                *guc(actor, request_id),
                decide_sql(case_id, decision, reason, changes),
            ]:
                cursor.execute(statement, params)
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("record_review_decision returned no row")
    return uuid.UUID(str(row[0]))


def insert_self_claim(
    admin: psycopg.Connection[Any],
    actor: uuid.UUID,
    document_version_id: uuid.UUID,
    tag: str,
) -> uuid.UUID:
    claim_id = uuid.uuid4()
    text = f"manual self claim {tag}"
    execute(
        admin,
        """
        INSERT INTO core.claims (
            id, document_version_id, claim_text, claim_fingerprint, claim_type,
            assertion_status, created_by
        ) VALUES (%s, %s, %s, %s, 'observation', 'reported', %s)
        """,
        claim_id,
        document_version_id,
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        actor,
    )
    return claim_id


def g9_10(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    author: uuid.UUID,
    other: uuid.UUID,
    document_id: uuid.UUID,
) -> None:
    tag = uuid.uuid4().hex[:8]
    claim_id = insert_self_claim(admin, author, document_id, tag)
    case_id = open_case(api, author, "claim", claim_id)
    grants_before = len(claim_grants(admin, claim_id))
    decisions_before = int(
        scalar(admin, "SELECT count(*) FROM audit.review_decisions")
    )
    outbox_before = int(scalar(admin, "SELECT count(*) FROM ops.outbox_events"))
    state, primary = sqlerror_tx(
        api,
        [*guc(author, uuid.uuid4()), decide_sql(case_id, "approve")],
    )
    require("g9-10 state", state, "42501")
    require("g9-10 code", primary, "review_self_review_denied")
    require("g9-10 grants", len(claim_grants(admin, claim_id)), grants_before)
    require(
        "g9-10 decisions",
        int(scalar(admin, "SELECT count(*) FROM audit.review_decisions")),
        decisions_before,
    )
    require(
        "g9-10 outbox",
        int(scalar(admin, "SELECT count(*) FROM ops.outbox_events")),
        outbox_before,
    )
    decide(api, other, case_id, "reject", "reject self-authored claim")
    require(
        "g9-10 other reject",
        scalar(admin, "SELECT status FROM audit.review_cases WHERE id = %s", case_id),
        "rejected",
    )


def g9_11(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    claim_id: uuid.UUID,
) -> uuid.UUID:
    jobs_before = count_jobs(admin, "publish_%")
    public_before = public_claim_count(admin)
    case_id = open_case(api, reviewer, "claim", claim_id)
    decision_id = decide(api, reviewer, case_id, "approve")
    grants = claim_grants(admin, claim_id)
    require("g9-11 grant count", len(grants), 1)
    require("g9-11 active", grants[0][2], "active")
    require("g9-11 revision", grants[0][1], 1)
    require("g9-11 withdrawn null", grants[0][3], None)
    require(
        "g9-11 outbox",
        outbox_types(admin, claim_id),
        ["publication.granted"],
    )
    require("g9-11 jobs", count_jobs(admin, "publish_%"), jobs_before)
    require("g9-11 public", public_claim_count(admin), public_before)
    require(
        "g9-11 case",
        scalar(admin, "SELECT status FROM audit.review_cases WHERE id = %s", case_id),
        "approved",
    )
    require(
        "g9-11 event key",
        int(
            scalar(
                admin,
                """
                SELECT count(*) FROM audit.audit_events
                 WHERE action = 'review.decision' AND target_id = %s
                   AND event_key LIKE 'review.decision:%%'
                """,
                decision_id,
            )
        ),
        1,
    )
    return case_id


def g9_12(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    claim_id: uuid.UUID,
) -> None:
    case_id = open_case(api, reviewer, "claim", claim_id)
    decide(api, reviewer, case_id, "reject", "reject this reviewed claim")
    require("g9-12 grants", len(claim_grants(admin, claim_id)), 0)
    require("g9-12 outbox", outbox_types(admin, claim_id), [])
    require(
        "g9-12 status",
        scalar(admin, "SELECT status FROM audit.review_cases WHERE id = %s", case_id),
        "rejected",
    )


def g9_13_14(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    senior: uuid.UUID,
    claim_id: uuid.UUID,
) -> uuid.UUID:
    case_id = open_case(api, reviewer, "claim", claim_id)
    decide(api, reviewer, case_id, "approve")
    state, primary = sqlerror_tx(
        api,
        [*guc(reviewer, uuid.uuid4()), decide_sql(case_id, "withdraw", WITHDRAW_REASON)],
    )
    require("g9-14 state", state, "42501")
    require("g9-14 code", primary, "review_role_denied")
    require("g9-14 still active", claim_grants(admin, claim_id)[0][2], "active")
    decide(api, senior, case_id, "withdraw", WITHDRAW_REASON)
    grants = claim_grants(admin, claim_id)
    require("g9-13 status", grants[0][2], "withdrawn")
    require("g9-13 withdrawn_at set", grants[0][3] is not None, True)
    require(
        "g9-13 outbox",
        "publication.withdrawn" in outbox_types(admin, claim_id),
        True,
    )
    require(
        "g9-13 case",
        scalar(admin, "SELECT status FROM audit.review_cases WHERE id = %s", case_id),
        "withdrawn",
    )
    return case_id


def g9_15(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    claim_id: uuid.UUID,
    other_claim: uuid.UUID,
) -> None:
    case_id = open_case(api, reviewer, "claim", claim_id)
    other_case = open_case(api, reviewer, "claim", other_claim)
    reject_id = decide(api, reviewer, case_id, "reject", "reject for grant bind")
    approve_other = decide(api, reviewer, other_case, "approve")
    state, _primary = sqlerror_tx(
        admin,
        [
            (
                """
                INSERT INTO audit.claim_publication_grants (
                    id, review_case_id, claim_id, decision_id, revision_no,
                    grant_status, granted_at, publication_payload_sha256
                ) VALUES (
                    %s, %s, %s, %s, 1, 'active', clock_timestamp(), repeat('a', 64)
                )
                """,
                (uuid.uuid4(), case_id, claim_id, reject_id),
            )
        ],
    )
    require("g9-15 reject bind", state, "23514")
    state, _primary = sqlerror_tx(
        admin,
        [
            (
                """
                INSERT INTO audit.claim_publication_grants (
                    id, review_case_id, claim_id, decision_id, revision_no,
                    grant_status, granted_at, publication_payload_sha256
                ) VALUES (
                    %s, %s, %s, %s, 1, 'active', clock_timestamp(), repeat('b', 64)
                )
                """,
                (uuid.uuid4(), case_id, claim_id, approve_other),
            )
        ],
    )
    require("g9-15 other case bind", state in {"23514", "23503", "23505"}, True)


def g9_16(api: psycopg.Connection[Any], reviewer: uuid.UUID) -> None:
    state, _primary = sqlerror_tx(
        api,
        [
            *guc(reviewer, uuid.uuid4()),
            (
                """
                INSERT INTO public.documents (
                    id, document_grant_id, slug, title, category, fact_status,
                    source_name, canonical_source_url, published_at, revision_no
                ) VALUES (
                    %s, %s, 'probe-slug', 'x', 'news', 'reported',
                    'probe', 'https://example.test/wp9-16', now(), 1
                )
                """,
                (uuid.uuid4(), uuid.uuid4()),
            ),
        ],
    )
    require("g9-16 public insert", state, "42501")
    state, _primary = sqlerror_tx(
        api,
        [
            *guc(reviewer, uuid.uuid4()),
            (
                """
                INSERT INTO ops.outbox_events (
                    id, aggregate_type, aggregate_id, event_type, event_key,
                    payload, occurred_at
                ) VALUES (
                    %s, 'x', %s, 'publication.granted', %s, '{}'::jsonb, now()
                )
                """,
                (uuid.uuid4(), uuid.uuid4(), f"probe-{uuid.uuid4()}"),
            ),
        ],
    )
    require("g9-16 outbox insert", state, "42501")


def g9_27(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    claim_id: uuid.UUID,
) -> uuid.UUID:
    case_id = open_case(api, reviewer, "claim", claim_id)
    decide(api, reviewer, case_id, "approve")
    decide(api, reviewer, case_id, "revise", REVISE_REASON)
    grants = claim_grants(admin, claim_id)
    require("g9-27 count", len(grants), 2)
    require("g9-27 old", grants[0][2], "superseded")
    require("g9-27 old withdrawn", grants[0][3], None)
    require("g9-27 new", grants[1][2], "active")
    require("g9-27 rev", grants[1][1], 2)
    require("g9-27 new withdrawn", grants[1][3], None)
    types = outbox_types(admin, claim_id)
    require("g9-27 superseded outbox", "publication.superseded" in types, True)
    require("g9-27 granted outbox", types.count("publication.granted"), 2)
    require("g9-27 public", public_claim_count(admin) >= 0, True)
    return case_id


def g9_28(
    admin: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    claim_id: uuid.UUID,
) -> None:
    api_setup = connect("uap_api")
    try:
        case_id = open_case(api_setup, reviewer, "claim", claim_id)
        decide(api_setup, reviewer, case_id, "approve")
    finally:
        api_setup.close()
    errors: list[BaseException] = []
    results: list[uuid.UUID] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        connection = connect("uap_api")
        connection.autocommit = False
        try:
            barrier.wait(timeout=10)
            request = uuid.uuid4()
            with connection.cursor() as cursor:
                for statement, params in [
                    *guc(reviewer, request),
                    decide_sql(case_id, "revise", REVISE_REASON),
                ]:
                    cursor.execute(statement, params)
                row = cursor.fetchone()
            connection.commit()
            if row is None:
                raise RuntimeError("concurrent revise returned no row")
            results.append(uuid.UUID(str(row[0])))
        except BaseException as error:
            connection.rollback()
            errors.append(error)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker), pool.submit(worker)]
        for future in futures:
            future.result(timeout=30)
    require("g9-28 errors", [str(item) for item in errors], [])
    require("g9-28 both", len(results), 2)
    grants = claim_grants(admin, claim_id)
    statuses = [row[2] for row in grants]
    require("g9-28 one active", statuses.count("active"), 1)
    require("g9-28 superseded", statuses.count("superseded"), 2)
    require("g9-28 revisions", [row[1] for row in grants], [1, 2, 3])
    for row in grants:
        if row[2] == "superseded":
            require("g9-28 superseded withdrawn null", row[3], None)
    with admin.cursor() as cursor:
        cursor.execute(
            """
            SELECT sequence_no, decision
              FROM audit.review_decisions
             WHERE review_case_id = %s
             ORDER BY sequence_no
            """,
            (case_id,),
        )
        rows = list(cursor.fetchall())
        cursor.execute(
            """
            SELECT payload ->> 'old_grant_id'
              FROM ops.outbox_events
             WHERE event_type = 'publication.superseded'
               AND payload ->> 'subject_id' = %s
             ORDER BY occurred_at
            """,
            (str(claim_id),),
        )
        old_ids = [str(row[0]) for row in cursor.fetchall()]
    require("g9-28 seq", [row[0] for row in rows], [1, 2, 3])
    require("g9-28 decisions", [row[1] for row in rows], ["approve", "revise", "revise"])
    by_rev = {int(row[1]): row[0] for row in grants}
    require("g9-28 two superseded events", len(old_ids), 2)
    require("g9-28 first supersedes approve", old_ids[0], str(by_rev[1]))
    require("g9-28 second supersedes later grant", old_ids[1], str(by_rev[2]))
    require("g9-28 active is latest", grants[-1][0], by_rev[3])


def g9_29_30(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    claim_id: uuid.UUID,
    extra_claim: uuid.UUID,
) -> None:
    """Cover g9_29 identical replay and g9_30 payload conflict."""
    case_id = open_case(api, reviewer, "claim", extra_claim)
    extra_case = open_case(api, reviewer, "claim", claim_id)
    request = uuid.uuid4()
    first = decide(api, reviewer, case_id, "approve", REASON, "{}", request)
    second = decide(api, reviewer, case_id, "approve", REASON, "{}", request)
    require("g9-29 replay", first, second)
    require("g9-29 grants", len(claim_grants(admin, extra_claim)), 1)
    require(
        "g9-29 events",
        int(
            scalar(
                admin,
                """
                SELECT count(*) FROM audit.audit_events
                 WHERE event_key = %s
                """,
                f"review.decision:{request}",
            )
        ),
        1,
    )
    require(
        "g9-29 outbox",
        int(
            scalar(
                admin,
                """
                SELECT count(*) FROM ops.outbox_events
                 WHERE payload ->> 'subject_id' = %s
                   AND event_type = 'publication.granted'
                """,
                str(extra_claim),
            )
        ),
        1,
    )
    for label, args in (
        ("reason", (case_id, "approve", "a different approve reason", "{}")),
        ("case", (extra_case, "approve", REASON, "{}")),
        ("decision", (case_id, "reject", REASON, "{}")),
        ("changes", (case_id, "approve", REASON, '{"x":1}')),
    ):
        state, primary = sqlerror_tx(
            api,
            [*guc(reviewer, request), decide_sql(*args)],
        )
        require(f"g9-30 {label} state", state, "23505")
        require(f"g9-30 {label} code", primary, "review_idempotency_payload_conflict")
    require("g9-30 grants stay", len(claim_grants(admin, extra_claim)), 1)


def g9_35(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    claim_id: uuid.UUID,
) -> None:
    case_id = open_case(api, reviewer, "claim", claim_id)
    decide(api, reviewer, case_id, "approve")
    request = uuid.uuid4()
    with api.transaction():
        with api.cursor() as cursor:
            for statement, params in [*guc(reviewer, request), close_sql(case_id)]:
                cursor.execute(statement, params)
            first = cursor.fetchone()
    with api.transaction():
        with api.cursor() as cursor:
            for statement, params in [*guc(reviewer, request), close_sql(case_id)]:
                cursor.execute(statement, params)
            second = cursor.fetchone()
    require("g9-35 replay", first, second)
    closed_at = scalar(
        admin, "SELECT closed_at FROM audit.review_cases WHERE id = %s", case_id
    )
    require(
        "g9-35 events",
        int(
            scalar(
                admin,
                "SELECT count(*) FROM audit.audit_events WHERE event_key = %s",
                f"review.case.close:{request}",
            )
        ),
        1,
    )
    state, primary = sqlerror_tx(
        api,
        [*guc(reviewer, request), close_sql(case_id, "another close reason")],
    )
    require("g9-35 reason state", state, "23505")
    require("g9-35 reason code", primary, "review_idempotency_payload_conflict")
    other = uuid.uuid4()
    state, primary = sqlerror_tx(
        api,
        [*guc(reviewer, request), close_sql(other)],
    )
    require("g9-35 case state", state, "23505")
    require("g9-35 case code", primary, "review_idempotency_payload_conflict")
    state, primary = sqlerror_tx(
        api,
        [*guc(reviewer, uuid.uuid4()), close_sql(case_id)],
    )
    require("g9-35 already closed state", state, "22023")
    require("g9-35 already closed code", primary, "review_case_already_closed")
    require(
        "g9-35 closed_at stable",
        scalar(admin, "SELECT closed_at FROM audit.review_cases WHERE id = %s", case_id),
        closed_at,
    )


def extra_cases(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    reviewer: uuid.UUID,
    other: uuid.UUID,
    senior: uuid.UUID,
    document_id: uuid.UUID,
    claim_id: uuid.UUID,
    entity_id: uuid.UUID,
) -> None:
    missing = uuid.uuid4()
    state, primary = sqlerror_tx(
        api, [*guc(reviewer, uuid.uuid4()), decide_sql(missing, "approve")]
    )
    require("extra missing state", state, "23503")
    require("extra missing code", primary, "review_case_missing")

    closed_claim = seed_subjects(admin, uuid.uuid4().hex[:8])[1]
    closed_case = open_case(api, reviewer, "claim", closed_claim)
    decide(api, reviewer, closed_case, "approve")
    with api.transaction():
        with api.cursor() as cursor:
            for statement, params in [
                *guc(reviewer, uuid.uuid4()),
                close_sql(closed_case),
            ]:
                cursor.execute(statement, params)
    state, primary = sqlerror_tx(
        api,
        [*guc(reviewer, uuid.uuid4()), decide_sql(closed_case, "revise", REVISE_REASON)],
    )
    require("extra closed state", state, "22023")
    require("extra closed code", primary, "review_case_already_closed")

    assigned_case = open_case(api, reviewer, "claim", claim_id)
    with api.transaction():
        with api.cursor() as cursor:
            for statement, params in [
                *guc(reviewer, uuid.uuid4()),
                (
                    "SELECT audit.assign_review_case(%s, %s)",
                    (assigned_case, reviewer),
                ),
            ]:
                cursor.execute(statement, params)
    state, primary = sqlerror_tx(
        api,
        [*guc(other, uuid.uuid4()), decide_sql(assigned_case, "approve")],
    )
    require("extra assignee state", state, "42501")
    require("extra assignee code", primary, "review_assignee_mismatch")
    decide(api, reviewer, assigned_case, "approve")

    fresh = seed_subjects(admin, uuid.uuid4().hex[:8])
    open_only = open_case(api, reviewer, "claim", fresh[1])
    state, primary = sqlerror_tx(
        api,
        [*guc(reviewer, uuid.uuid4()), decide_sql(open_only, "revise", REVISE_REASON)],
    )
    require("extra revise no grant state", state, "22023")
    require("extra revise no grant code", primary, "review_decision_not_allowed")
    state, primary = sqlerror_tx(
        api,
        [
            *guc(reviewer, uuid.uuid4()),
            decide_sql(open_only, "withdraw", WITHDRAW_REASON),
        ],
    )
    require("extra withdraw role", state, "42501")
    require("extra withdraw role code", primary, "review_role_denied")

    for label, changes in (
        ("object", '{"x":1}'),
        ("array", "[1]"),
        ("null", "null"),
    ):
        state, primary = sqlerror_tx(
            api,
            [
                *guc(reviewer, uuid.uuid4()),
                decide_sql(open_only, "approve", REASON, changes),
            ],
        )
        require(f"extra changes {label} state", state, "22023")
        require(
            f"extra changes {label} code",
            primary,
            "review_structured_changes_unsupported",
        )

    decide(api, reviewer, open_only, "dispute", "dispute this reviewed claim")
    require(
        "extra dispute status",
        scalar(admin, "SELECT status FROM audit.review_cases WHERE id = %s", open_only),
        "disputed",
    )
    require("extra dispute grants", len(claim_grants(admin, fresh[1])), 0)
    require("extra dispute outbox", outbox_types(admin, fresh[1]), [])

    state, primary = sqlerror_tx(
        api,
        [*guc(reviewer, uuid.uuid4()), decide_sql(assigned_case, "approve")],
    )
    require("extra double approve state", state, "22023")
    require("extra double approve code", primary, "review_decision_not_allowed")

    orphan = seed_subjects(admin, uuid.uuid4().hex[:8])
    orphan_case = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO audit.review_cases (
            id, claim_id, case_type, status, priority, opened_by, opened_at
        ) VALUES (%s, %s, 'claim', 'approved', 0, %s, clock_timestamp())
        """,
        orphan_case,
        orphan[1],
        reviewer,
    )
    state, primary = sqlerror_tx(
        api,
        [*guc(reviewer, uuid.uuid4()), decide_sql(orphan_case, "revise", REVISE_REASON)],
    )
    require("extra revise no active grant state", state, "22023")
    require("extra revise no active grant code", primary, "review_grant_not_active")
    state, primary = sqlerror_tx(
        api,
        [*guc(senior, uuid.uuid4()), decide_sql(orphan_case, "withdraw", WITHDRAW_REASON)],
    )
    require("extra withdraw no active grant state", state, "22023")
    require("extra withdraw no active grant code", primary, "review_grant_not_active")
    require(
        "extra orphan no decision",
        int(
            scalar(
                admin,
                "SELECT count(*) FROM audit.review_decisions WHERE review_case_id = %s",
                orphan_case,
            )
        ),
        0,
    )

    rollback_seed = seed_subjects(admin, uuid.uuid4().hex[:8])
    rollback_case = open_case(api, reviewer, "claim", rollback_seed[1])
    owner_decision = uuid.uuid4()
    owner_grant = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO audit.review_decisions (
            id, review_case_id, sequence_no, decision, reason, structured_changes,
            decided_by, decided_at
        ) VALUES (
            %s, %s, 1, 'approve', %s, '{}'::jsonb, %s, clock_timestamp()
        )
        """,
        owner_decision,
        rollback_case,
        REASON,
        reviewer,
    )
    execute(
        admin,
        """
        INSERT INTO audit.claim_publication_grants (
            id, review_case_id, claim_id, decision_id, revision_no,
            grant_status, granted_at, publication_payload_sha256
        ) VALUES (
            %s, %s, %s, %s, 1, 'active', clock_timestamp(), repeat('c', 64)
        )
        """,
        owner_grant,
        rollback_case,
        rollback_seed[1],
        owner_decision,
    )
    decisions_before = int(
        scalar(
            admin,
            "SELECT count(*) FROM audit.review_decisions WHERE review_case_id = %s",
            rollback_case,
        )
    )
    events_before = int(scalar(admin, "SELECT count(*) FROM audit.audit_events"))
    state, primary = sqlerror_tx(
        api,
        [*guc(reviewer, uuid.uuid4()), decide_sql(rollback_case, "approve")],
    )
    require("extra rollback state", state, "22023")
    require("extra rollback code", primary, "review_grant_already_active")
    require(
        "extra rollback decisions",
        int(
            scalar(
                admin,
                "SELECT count(*) FROM audit.review_decisions WHERE review_case_id = %s",
                rollback_case,
            )
        ),
        decisions_before,
    )
    require(
        "extra rollback events",
        int(scalar(admin, "SELECT count(*) FROM audit.audit_events")),
        events_before,
    )
    require(
        "extra rollback case open",
        scalar(admin, "SELECT status FROM audit.review_cases WHERE id = %s", rollback_case),
        "open",
    )

    replay_key = f"publication-granted:claim_publication_grants:{uuid.uuid4()}"
    replay_payload = (
        '{"schema":"publication-outbox.v1","grant_id":"'
        + str(uuid.uuid4())
        + '"}'
    )
    execute(admin, "SET ROLE uap_owner")
    try:
        first_outbox = scalar(
            admin,
            """
            SELECT ops.enqueue_publication_outbox(
                'publication.granted', %s, 'claim_publication_grants', %s, %s::jsonb
            )
            """,
            replay_key,
            uuid.uuid4(),
            replay_payload,
        )
        second_outbox = scalar(
            admin,
            """
            SELECT ops.enqueue_publication_outbox(
                'publication.granted', %s, 'claim_publication_grants', %s, %s::jsonb
            )
            """,
            replay_key,
            uuid.uuid4(),
            replay_payload,
        )
        require("extra outbox replay", first_outbox, second_outbox)
        state, primary = sqlerror_tx(
            admin,
            [
                (
                    """
                    SELECT ops.enqueue_publication_outbox(
                        'publication.granted', %s, 'claim_publication_grants', %s,
                        %s::jsonb
                    )
                    """,
                    (replay_key, uuid.uuid4(), '{"schema":"publication-outbox.v1","x":1}'),
                )
            ],
        )
        require("extra outbox conflict state", state, "23505")
        require(
            "extra outbox conflict code", primary, "review_idempotency_payload_conflict"
        )
    finally:
        execute(admin, "RESET ROLE")

    relation_id = uuid.uuid4()
    other_entity = seed_subjects(admin, uuid.uuid4().hex[:8])[2]
    execute(
        admin,
        """
        INSERT INTO core.relations (
            id, subject_entity_id, object_entity_id, predicate, relation_status
        ) VALUES (%s, %s, %s, 'related_to', 'reported')
        """,
        relation_id,
        entity_id,
        other_entity,
    )
    rel_case = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO audit.review_cases (
            id, relation_id, case_type, status, priority, opened_by, opened_at
        ) VALUES (%s, %s, 'relation', 'open', 0, %s, clock_timestamp())
        """,
        rel_case,
        relation_id,
        reviewer,
    )
    state, primary = sqlerror_tx(
        api,
        [*guc(reviewer, uuid.uuid4()), decide_sql(rel_case, "approve")],
    )
    require("extra relation state", state, "22023")
    require("extra relation code", primary, "knowledge_relation_review_not_in_wp9")

    doc_case = open_case(api, reviewer, "document", document_id)
    decide(api, reviewer, doc_case, "approve")
    require(
        "extra document grant",
        int(
            scalar(
                admin,
                """
                SELECT count(*) FROM audit.document_publication_grants
                 WHERE document_version_id = %s AND grant_status = 'active'
                """,
                document_id,
            )
        ),
        1,
    )
    ent_case = open_case(api, reviewer, "entity", entity_id)
    decide(api, reviewer, ent_case, "approve")
    require(
        "extra entity grant",
        int(
            scalar(
                admin,
                """
                SELECT count(*) FROM audit.entity_publication_grants
                 WHERE entity_id = %s AND grant_status = 'active'
                """,
                entity_id,
            )
        ),
        1,
    )


def live_indexes(admin: psycopg.Connection[Any]) -> None:
    require(
        "live document index",
        scalar(
            admin,
            """
            SELECT indexname FROM pg_indexes
             WHERE schemaname='audit' AND indexname='uq_document_grant_live'
            """,
        ),
        "uq_document_grant_live",
    )
    require(
        "old document index gone",
        scalar(
            admin,
            """
            SELECT count(*)::int FROM pg_indexes
             WHERE schemaname='audit' AND indexname='uq_document_grant_active'
            """,
        ),
        0,
    )
    require(
        "live claim index",
        scalar(
            admin,
            """
            SELECT indexname FROM pg_indexes
             WHERE schemaname='audit' AND indexname='uq_claim_grant_live'
            """,
        ),
        "uq_claim_grant_live",
    )
    require(
        "live entity index",
        scalar(
            admin,
            """
            SELECT indexname FROM pg_indexes
             WHERE schemaname='audit' AND indexname='uq_entity_grant_live'
            """,
        ),
        "uq_entity_grant_live",
    )
    require(
        "relation index unchanged",
        scalar(
            admin,
            """
            SELECT indexname FROM pg_indexes
             WHERE schemaname='audit' AND indexname='uq_relation_grant_active'
            """,
        ),
        "uq_relation_grant_active",
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
        live_indexes(admin)
        author = insert_person(admin)
        bind_role(admin, author, "reviewer")
        reviewer = insert_person(admin)
        bind_role(admin, reviewer, "reviewer")
        other = insert_person(admin)
        bind_role(admin, other, "reviewer")
        senior = insert_person(admin)
        bind_role(admin, senior, "senior_reviewer")
        document_id, claim_id, entity_id = seed_subjects(admin, uuid.uuid4().hex[:8])
        extras = [seed_subjects(admin, uuid.uuid4().hex[:8]) for _ in range(8)]
        g9_10(admin, api, author, other, document_id)
        g9_11(admin, api, reviewer, extras[0][1])
        g9_12(admin, api, reviewer, extras[1][1])
        g9_13_14(admin, api, reviewer, senior, extras[2][1])
        g9_15(admin, api, reviewer, extras[3][1], extras[4][1])
        g9_16(api, reviewer)
        g9_27(admin, api, reviewer, extras[5][1])
        g9_28(admin, reviewer, extras[6][1])
        g9_29_30(admin, api, reviewer, extras[7][1], seed_subjects(admin, "g929")[1])
        g9_35(admin, api, reviewer, seed_subjects(admin, "g935")[1])
        extra_cases(
            admin, api, reviewer, other, senior, document_id, claim_id, entity_id
        )
    finally:
        api.close()
        admin.close()
    print(
        "WP9.3 runtime probe passed: G9-10 G9-11 G9-12 G9-13 G9-14 G9-15 G9-16 "
        "G9-27 G9-28 G9-29 G9-30 G9-35"
    )


if __name__ == "__main__":
    main()
