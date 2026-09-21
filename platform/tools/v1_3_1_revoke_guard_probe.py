"""Isolated runtime probe for the V1-3.1 pre-publication revoke guard.

The probe is intended for a disposable database migrated through 0031.  It
uses the real review decision and Publisher SQL boundaries and emits only a
small, non-sensitive result summary.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import psycopg

from tools.wp9_2_runtime_probe import bind_role, insert_person, seed_grantor
from tools.wp10_2_runtime_probe import (
    claim_for,
    document_manifest,
    event_payload,
    insert_event,
    scalar,
)
from uap_platform.publishing.service import PublicationService


def connect(name: str) -> psycopg.Connection[Any]:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing {name}")
    connection = psycopg.connect(value)
    connection.autocommit = False
    return connection


def require(name: str, condition: bool) -> None:
    if not condition:
        raise RuntimeError(name)


def create_granted_event(
    admin: psycopg.Connection[Any], tag: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    document_id, version_id, grant_id, _decision_id, manifest = document_manifest(admin, tag)
    case_id = uuid.UUID(
        str(
            scalar(
                admin,
                "SELECT review_case_id FROM audit.document_publication_grants WHERE id=%s",
                grant_id,
            )
        )
    )
    digest = str(
        scalar(
            admin,
            "SELECT publication_payload_sha256 "
            "FROM audit.document_publication_grants WHERE id=%s",
            grant_id,
        )
    )
    payload = event_payload(manifest, event_type="publication.granted")
    payload["payload_sha256"] = digest
    event_id = insert_event(
        admin,
        aggregate_type="document_publication_grants",
        aggregate_id=grant_id,
        event_type="publication.granted",
        payload=payload,
    )
    admin.commit()
    return document_id, version_id, case_id, grant_id, event_id


def decide_withdraw(
    api: psycopg.Connection[Any],
    *,
    principal_id: uuid.UUID,
    request_id: uuid.UUID,
    case_id: uuid.UUID,
    reason: str,
) -> uuid.UUID:
    with api.transaction():
        with api.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('uap.principal_id', %s, true)",
                (str(principal_id),),
            )
            cursor.execute(
                "SELECT set_config('uap.request_id', %s, true)",
                (str(request_id),),
            )
            cursor.execute(
                "SELECT audit.record_review_decision("
                "%s, 'withdraw'::audit.review_decision, %s, '{}'::jsonb)",
                (case_id, reason),
            )
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("withdraw returned no decision")
    return uuid.UUID(str(row[0]))


def rejected_code(operation: Any) -> tuple[str | None, str | None]:
    try:
        operation()
    except psycopg.Error as error:
        primary = error.diag.message_primary if error.diag is not None else None
        return error.sqlstate, primary
    raise RuntimeError("forbidden operation was accepted")


def main() -> None:
    with connect("PROBE_ADMIN_DATABASE_URL") as admin, connect(
        "PROBE_API_DATABASE_URL"
    ) as api, connect("PROBE_PUBLISHER_DATABASE_URL") as publisher:
        seed_grantor(admin)
        senior = insert_person(admin)
        reviewer = insert_person(admin)
        bind_role(admin, senior, "senior_reviewer")
        bind_role(admin, reviewer, "reviewer")
        admin.commit()

        service = PublicationService(
            publisher,
            dispatcher_id="v1-3-1-revoke-guard-probe",
            batch_limit=10,
        )

        # A queued, unleased authorization is revocable.  A replay with the
        # same request identity is idempotent and the old grant event remains
        # unable to create a public projection.
        pre_document, _pre_version, pre_case, pre_grant, pre_event = create_granted_event(
            admin, "v131-prepublication"
        )
        request_id = uuid.uuid4()
        decision_id = decide_withdraw(
            api,
            principal_id=senior,
            request_id=request_id,
            case_id=pre_case,
            reason="Withdraw before Publisher apply for the V1-3.1 guard probe.",
        )
        replay_id = decide_withdraw(
            api,
            principal_id=senior,
            request_id=request_id,
            case_id=pre_case,
            reason="Withdraw before Publisher apply for the V1-3.1 guard probe.",
        )
        require("idempotent replay changed decision", replay_id == decision_id)
        require(
            "pre-publication grant not withdrawn",
            scalar(
                admin,
                "SELECT grant_status::text FROM audit.document_publication_grants WHERE id=%s",
                pre_grant,
            )
            == "withdrawn",
        )
        require(
            "withdraw replay duplicated outbox",
            int(
                scalar(
                    admin,
                    "SELECT count(*) FROM ops.outbox_events "
                    "WHERE aggregate_id=%s AND event_type='publication.withdrawn'",
                    pre_grant,
                )
            )
            == 1,
        )
        conflict = rejected_code(
            lambda: decide_withdraw(
                api,
                principal_id=senior,
                request_id=request_id,
                case_id=pre_case,
                reason="A different payload under the same request identity must fail.",
            )
        )
        require(
            "idempotency payload conflict not fail-closed",
            conflict == ("23505", "review_idempotency_payload_conflict"),
        )
        pre_claim = claim_for(service, pre_event)
        service.apply(pre_claim)
        publisher.commit()
        require(
            "revoked queued grant became public",
            int(
                scalar(
                    admin,
                    "SELECT count(*) FROM public.documents WHERE id=%s",
                    pre_document,
                )
            )
            == 0,
        )

        # A successful apply/ack is terminal for V1-3.1 revoke.  The failed
        # mutation must leave grant, manifest, outbox and projection intact.
        post_document, _post_version, post_case, post_grant, post_event = create_granted_event(
            admin, "v131-postpublication"
        )
        post_claim = claim_for(service, post_event)
        service.apply(post_claim)
        publisher.commit()
        before = tuple(
            scalar(admin, statement, identity)
            for statement, identity in (
                (
                    "SELECT count(*) FROM audit.document_publication_manifests WHERE grant_id=%s",
                    post_grant,
                ),
                (
                    "SELECT count(*) FROM ops.outbox_events WHERE aggregate_id=%s",
                    post_grant,
                ),
                ("SELECT count(*) FROM public.documents WHERE id=%s", post_document),
            )
        )
        projected = rejected_code(
            lambda: decide_withdraw(
                api,
                principal_id=senior,
                request_id=uuid.uuid4(),
                case_id=post_case,
                reason="A published authorization cannot be withdrawn in V1-3.1.",
            )
        )
        require(
            "post-publication revoke did not use stable conflict",
            projected == ("23505", "publication_already_projected"),
        )
        after = tuple(
            scalar(admin, statement, identity)
            for statement, identity in (
                (
                    "SELECT count(*) FROM audit.document_publication_manifests WHERE grant_id=%s",
                    post_grant,
                ),
                (
                    "SELECT count(*) FROM ops.outbox_events WHERE aggregate_id=%s",
                    post_grant,
                ),
                ("SELECT count(*) FROM public.documents WHERE id=%s", post_document),
            )
        )
        require("post-publication rejection mutated state", before == after)
        require(
            "post-publication rejection changed grant",
            scalar(
                admin,
                "SELECT grant_status::text FROM audit.document_publication_grants WHERE id=%s",
                post_grant,
            )
            == "active",
        )

        # A live Publisher lease owns the right to apply and therefore fences
        # a concurrent revoke before either side can produce a mixed state.
        _lease_document, _lease_version, lease_case, lease_grant, lease_event = (
            create_granted_event(admin, "v131-live-lease")
        )
        lease_claim = claim_for(service, lease_event)
        leased = rejected_code(
            lambda: decide_withdraw(
                api,
                principal_id=senior,
                request_id=uuid.uuid4(),
                case_id=lease_case,
                reason="A live Publisher lease must fence this revoke request.",
            )
        )
        require(
            "live lease did not fence revoke",
            leased == ("23505", "publication_already_projected"),
        )
        require(
            "lease rejection changed grant",
            scalar(
                admin,
                "SELECT grant_status::text FROM audit.document_publication_grants WHERE id=%s",
                lease_grant,
            )
            == "active",
        )
        service.fail(
            lease_claim,
            error_code="publication_database_unavailable",
            terminal=True,
        )
        publisher.commit()

        # The existing senior-reviewer separation remains the authority gate.
        unauthorized = rejected_code(
            lambda: decide_withdraw(
                api,
                principal_id=reviewer,
                request_id=uuid.uuid4(),
                case_id=lease_case,
                reason="A reviewer without senior authority cannot withdraw.",
            )
        )
        require(
            "reviewer role crossed withdrawal authority boundary",
            unauthorized[0] == "42501",
        )

    print(
        json.dumps(
            {
                "pre_publication_revoke": "PASS",
                "idempotent_replay": "PASS",
                "payload_conflict": "PASS",
                "revoked_grant_apply_suppressed": "PASS",
                "post_publication_revoke": "REJECTED",
                "post_publication_state": "UNCHANGED",
                "live_lease_race_guard": "PASS",
                "senior_reviewer_boundary": "PASS",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
