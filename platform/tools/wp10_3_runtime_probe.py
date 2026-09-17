"""Real PostgreSQL/role probe for WP10.3 G10-11 through G10-15."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
for path in (str(PLATFORM_ROOT), str(PLATFORM_ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from tools.wp8_1_runtime_probe import (  # noqa: E402
    insert_analysis,
    insert_model_run,
    sha256_text,
)
from tools.wp10_2_runtime_probe import (  # noqa: E402
    connect_role,
    document_manifest,
    entity_manifest,
    event_payload,
    insert_event,
    insert_principal,
    libpq_url,
    one,
    scalar,
)
from uap_platform.publishing.service import (  # noqa: E402
    PublicationClaim,
    PublicationService,
)

EVIDENCE: list[dict[str, Any]] = []


def execute(connection: psycopg.Connection[Any], statement: str, *params: object) -> None:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def require(name: str, actual: object, expected: object) -> None:
    passed = actual == expected
    EVIDENCE.append(
        {
            "kind": "assertion",
            "name": name,
            "passed": passed,
            "actual": actual,
            "expected": expected,
        }
    )
    if not passed:
        raise RuntimeError(f"{name}: unexpected result")


def error_pair(
    connection: psycopg.Connection[Any], statement: str, *params: object
) -> tuple[str | None, str | None]:
    try:
        execute(connection, statement, *params)
    except psycopg.Error as error:
        connection.rollback()
        primary = getattr(getattr(error, "diag", None), "message_primary", None)
        EVIDENCE.append({"kind": "sql_error", "sqlstate": error.sqlstate, "primary": primary})
        return error.sqlstate, primary
    connection.rollback()
    return None, None


def claim_for(service: PublicationService, event_id: uuid.UUID) -> PublicationClaim:
    for _attempt in range(8):
        for claim in service.claim():
            if claim.event_id == event_id:
                service.connection.commit()
                return claim
        service.connection.commit()
        time.sleep(0.15)
    raise RuntimeError("WP10.3 event was not claimed")


def publish_document(
    admin: psycopg.Connection[Any],
    service: PublicationService,
    original: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, dict[str, Any]],
) -> uuid.UUID:
    _document_id, _version, grant_id, _decision_id, manifest = original
    digest = cast(
        str,
        scalar(
            admin,
            "SELECT publication_payload_sha256 FROM audit.document_publication_grants WHERE id=%s",
            grant_id,
        ),
    )
    payload = event_payload(manifest, event_type="publication.granted")
    payload["payload_sha256"] = digest.strip()
    event_id = insert_event(
        admin,
        aggregate_type="document_publication_grants",
        aggregate_id=grant_id,
        event_type="publication.granted",
        payload=payload,
    )
    service.apply(claim_for(service, event_id))
    service.connection.commit()
    return event_id


def publish_entity(
    admin: psycopg.Connection[Any],
    service: PublicationService,
    original: tuple[uuid.UUID, uuid.UUID, uuid.UUID, dict[str, Any]],
) -> uuid.UUID:
    _entity_id, grant_id, _decision_id, manifest = original
    digest = cast(
        str,
        scalar(
            admin,
            "SELECT publication_payload_sha256 FROM audit.entity_publication_grants WHERE id=%s",
            grant_id,
        ),
    )
    payload = event_payload(manifest, event_type="publication.granted")
    payload["payload_sha256"] = digest.strip()
    event_id = insert_event(
        admin,
        aggregate_type="entity_publication_grants",
        aggregate_id=grant_id,
        event_type="publication.granted",
        payload=payload,
    )
    service.apply(claim_for(service, event_id))
    service.connection.commit()
    return event_id


def claim_manifest(
    admin: psycopg.Connection[Any],
    document: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, dict[str, Any]],
    tag: str,
    *,
    subject_entity_id: uuid.UUID | None = None,
    ai: bool = False,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document_id, document_version_id, _document_grant, _decision, doc_manifest = document
    now = datetime.now(UTC)
    actor = insert_principal(admin, f"claim-{tag}")
    revision = 1 if previous is None else int(previous["revision_no"]) + 1
    claim_id = uuid.uuid4() if previous is None else cast(uuid.UUID, previous["claim_id"])
    case_id = uuid.uuid4() if previous is None else cast(uuid.UUID, previous["case_id"])
    old_grant_id = None if previous is None else cast(uuid.UUID, previous["grant_id"])
    grant_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    span_id = uuid.uuid4()
    excerpt = f"PRIVATE-EVIDENCE-{tag}"
    locator_hash = sha256_text(f"locator-{tag}-{span_id}")
    origin_id: uuid.UUID | None = None
    ordinal: int | None = None
    if previous is None and ai:
        model_run = insert_model_run(
            admin,
            document_version_id=document_version_id,
            task_type="claim_extraction",
            input_sha256=sha256_text(f"input-{tag}"),
            tag=f"wp10-3-{tag}",
        )
        origin_id = insert_analysis(
            admin,
            model_run_id=model_run,
            document_version_id=document_version_id,
            result_type="claim_extraction",
            result={"claims": []},
        )
        ordinal = 0
    claim_text = f"WP10.3 {'AI' if ai else 'manual'} claim {tag} revision {revision}"
    with admin.transaction():
        execute(
            admin,
            """
            INSERT INTO core.evidence_spans (
                id, document_version_id, evidence_text, locator_type,
                char_start, char_end, locator, locator_sha256
            ) VALUES (%s, %s, %s, 'text', 0, %s, %s::jsonb, %s)
            """,
            span_id,
            document_version_id,
            excerpt,
            len(excerpt),
            json.dumps({"char_start": 0, "char_end": len(excerpt)}),
            locator_hash,
        )
        if previous is None:
            execute(
                admin,
                """
                INSERT INTO core.claims (
                    id, origin_analysis_result_id, subject_entity_id, ordinal,
                    claim_text, claim_fingerprint, claim_type, assertion_status,
                    created_by, document_version_id
                ) VALUES (%s, %s, %s, %s, %s, %s, 'observation', 'reported', %s, %s)
                """,
                claim_id,
                origin_id,
                subject_entity_id,
                ordinal,
                claim_text,
                sha256_text(claim_text),
                None if ai else actor,
                document_version_id,
            )
        else:
            execute(
                admin,
                """
                UPDATE core.claims
                   SET claim_text=%s, claim_fingerprint=%s,
                       subject_entity_id=%s
                 WHERE id=%s
                """,
                claim_text,
                sha256_text(claim_text),
                subject_entity_id,
                claim_id,
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
    if previous is None:
        execute(
            admin,
            """
            INSERT INTO audit.review_cases (
                id, claim_id, case_type, status, priority, opened_by, opened_at
            ) VALUES (%s, %s, 'claim', 'approved', 0, %s, %s)
            """,
            case_id,
            claim_id,
            actor,
            now,
        )
    execute(
        admin,
        """
        INSERT INTO audit.review_decisions (
            id, review_case_id, sequence_no, decision, reason,
            structured_changes, decided_by, decided_at
        ) VALUES (%s, %s, %s, %s, 'WP10.3 runtime fixture', '{}'::jsonb, %s, %s)
        """,
        decision_id,
        case_id,
        revision,
        "approve" if revision == 1 else "revise",
        actor,
        now,
    )
    manifest = {
        "schema": "publication-manifest.v2",
        "subject_type": "claim",
        "grant_id": str(grant_id),
        "decision_id": str(decision_id),
        "subject_id": str(claim_id),
        "document_id": str(document_id),
        "document_version_id": str(document_version_id),
        "revision_no": revision,
        "claim_text": claim_text,
        "claim_type": "observation",
        "assertion_status": "reported",
        "attribution": None,
        "subject_entity_id": None if subject_entity_id is None else str(subject_entity_id),
        "evidence": [
            {
                "evidence_span_id": str(span_id),
                "evidence_ordinal": 0,
                "excerpt": excerpt,
                "locator_type": "text",
                "char_start": 0,
                "char_end": len(excerpt),
                "page_start": None,
                "page_end": None,
                "time_start_ms": None,
                "time_end_ms": None,
                "public_locator": {"char_start": 0, "char_end": len(excerpt)},
                "locator_sha256": locator_hash,
                "source_url": str(doc_manifest["canonical_source_url"]),
            }
        ],
    }
    digest = cast(
        str,
        scalar(
            admin,
            "SELECT audit._publication_manifest_sha(%s::jsonb)",
            json.dumps(manifest, separators=(",", ":")),
        ),
    )
    if old_grant_id is not None:
        execute(
            admin,
            "UPDATE audit.claim_publication_grants SET grant_status='superseded' WHERE id=%s",
            old_grant_id,
        )
    with admin.transaction():
        execute(
            admin,
            """
            INSERT INTO audit.claim_publication_grants (
                id, review_case_id, claim_id, decision_id, revision_no,
                grant_status, granted_at, publication_payload_sha256
            ) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s)
            """,
            grant_id,
            case_id,
            claim_id,
            decision_id,
            revision,
            now,
            digest,
        )
        execute(
            admin,
            """
            INSERT INTO audit.claim_publication_manifests (
                grant_id, review_case_id, decision_id, claim_id, document_id,
                document_version_id, claim_text, claim_type, assertion_status,
                attribution, subject_entity_id, manifest_sha256
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'observation', 'reported',
                      NULL, %s, %s)
            """,
            grant_id,
            case_id,
            decision_id,
            claim_id,
            document_id,
            document_version_id,
            claim_text,
            subject_entity_id,
            digest,
        )
        execute(
            admin,
            """
            INSERT INTO audit.claim_publication_manifest_evidence (
                grant_id, evidence_ordinal, evidence_span_id, document_version_id,
                excerpt, locator_type, char_start, char_end, public_locator,
                locator_sha256, source_url
            ) VALUES (%s, 0, %s, %s, %s, 'text', 0, %s, %s::jsonb, %s, %s)
            """,
            grant_id,
            span_id,
            document_version_id,
            excerpt,
            len(excerpt),
            json.dumps({"char_start": 0, "char_end": len(excerpt)}),
            locator_hash,
            doc_manifest["canonical_source_url"],
        )
    manifest["payload_sha256"] = digest.strip()
    return {
        "claim_id": claim_id,
        "case_id": case_id,
        "grant_id": grant_id,
        "decision_id": decision_id,
        "revision_no": revision,
        "span_id": span_id,
        "manifest": manifest,
        "old_grant_id": old_grant_id,
    }


def claim_event(
    admin: psycopg.Connection[Any], fixture: dict[str, Any], event_type: str
) -> uuid.UUID:
    manifest = cast(dict[str, Any], fixture["manifest"])
    old_grant = cast(uuid.UUID | None, fixture["old_grant_id"])
    payload = event_payload(
        manifest,
        event_type=event_type,
        old_grant_id=old_grant if event_type == "publication.superseded" else None,
    )
    payload["payload_sha256"] = manifest["payload_sha256"]
    if event_type == "publication.superseded":
        aggregate_id = cast(uuid.UUID, old_grant)
    else:
        aggregate_id = cast(uuid.UUID, fixture["grant_id"])
    return insert_event(
        admin,
        aggregate_type="claim_publication_grants",
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=payload,
    )


def withdraw_claim(admin: psycopg.Connection[Any], fixture: dict[str, Any]) -> uuid.UUID:
    decision_id = uuid.uuid4()
    now = datetime.now(UTC)
    actor = insert_principal(admin, "claim-withdraw")
    execute(
        admin,
        """
        INSERT INTO audit.review_decisions (
            id, review_case_id, sequence_no, decision, reason,
            structured_changes, decided_by, decided_at
        ) VALUES (%s, %s, %s, 'withdraw', 'WP10.3 withdraw fixture',
                  '{}'::jsonb, %s, %s)
        """,
        decision_id,
        fixture["case_id"],
        int(fixture["revision_no"]) + 1,
        actor,
        now,
    )
    execute(
        admin,
        """
        UPDATE audit.claim_publication_grants
           SET grant_status='withdrawn', withdrawn_by_decision_id=%s, withdrawn_at=%s
         WHERE id=%s
        """,
        decision_id,
        now,
        fixture["grant_id"],
    )
    manifest = dict(cast(dict[str, Any], fixture["manifest"]))
    manifest["decision_id"] = str(decision_id)
    payload = event_payload(manifest, event_type="publication.withdrawn")
    payload["payload_sha256"] = cast(dict[str, Any], fixture["manifest"])["payload_sha256"]
    return insert_event(
        admin,
        aggregate_type="claim_publication_grants",
        aggregate_id=fixture["grant_id"],
        event_type="publication.withdrawn",
        payload=payload,
    )


def withdraw_document(
    admin: psycopg.Connection[Any],
    document: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, dict[str, Any]],
) -> uuid.UUID:
    _document_id, _version_id, grant_id, _decision_id, manifest = document
    decision_id = uuid.uuid4()
    actor = insert_principal(admin, "document-withdraw")
    now = datetime.now(UTC)
    case_id = scalar(
        admin,
        "SELECT review_case_id FROM audit.document_publication_grants WHERE id=%s",
        grant_id,
    )
    execute(
        admin,
        """
        INSERT INTO audit.review_decisions (
            id, review_case_id, sequence_no, decision, reason,
            structured_changes, decided_by, decided_at
        ) VALUES (%s, %s, 2, 'withdraw', 'WP10.3 document withdraw',
                  '{}'::jsonb, %s, %s)
        """,
        decision_id,
        case_id,
        actor,
        now,
    )
    execute(
        admin,
        """
        UPDATE audit.document_publication_grants
           SET grant_status='withdrawn', withdrawn_by_decision_id=%s, withdrawn_at=%s
         WHERE id=%s
        """,
        decision_id,
        now,
        grant_id,
    )
    payload = event_payload(
        {**manifest, "decision_id": str(decision_id)},
        event_type="publication.withdrawn",
    )
    digest = scalar(
        admin,
        "SELECT publication_payload_sha256 FROM audit.document_publication_grants WHERE id=%s",
        grant_id,
    )
    payload["payload_sha256"] = digest.strip()
    return insert_event(
        admin,
        aggregate_type="document_publication_grants",
        aggregate_id=grant_id,
        event_type="publication.withdrawn",
        payload=payload,
    )


def republish_document(
    admin: psycopg.Connection[Any],
    document: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, dict[str, Any]],
) -> tuple[uuid.UUID, uuid.UUID]:
    document_id, version_id, old_grant_id, _decision_id, old_manifest = document
    grant_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    actor = insert_principal(admin, "document-republish")
    now = datetime.now(UTC)
    case_id = scalar(
        admin,
        "SELECT review_case_id FROM audit.document_publication_grants WHERE id=%s",
        old_grant_id,
    )
    execute(
        admin,
        """
        INSERT INTO audit.review_decisions (
            id, review_case_id, sequence_no, decision, reason,
            structured_changes, decided_by, decided_at
        ) VALUES (%s, %s, 3, 'revise', 'WP10.3 document republish',
                  '{}'::jsonb, %s, %s)
        """,
        decision_id,
        case_id,
        actor,
        now,
    )
    manifest = {
        **old_manifest,
        "grant_id": str(grant_id),
        "decision_id": str(decision_id),
        "revision_no": 2,
        "title": "WP10.3 republished document",
    }
    digest = scalar(
        admin,
        "SELECT audit._publication_manifest_sha(%s::jsonb)",
        json.dumps(manifest, separators=(",", ":")),
    )
    execute(
        admin,
        """
        INSERT INTO audit.document_publication_grants (
            id, review_case_id, document_version_id, decision_id, revision_no,
            grant_status, granted_at, publication_payload_sha256
        ) VALUES (%s, %s, %s, %s, 2, 'active', %s, %s)
        """,
        grant_id,
        case_id,
        version_id,
        decision_id,
        now,
        digest,
    )
    execute(
        admin,
        """
        INSERT INTO audit.document_publication_manifests (
            grant_id, review_case_id, decision_id, document_id, document_version_id,
            title, summary, category, fact_status, source_name, canonical_source_url,
            source_published_at, summary_analysis_result_id, manifest_sha256
        ) VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, NULL, %s)
        """,
        grant_id,
        case_id,
        decision_id,
        document_id,
        version_id,
        manifest["title"],
        manifest["category"],
        manifest["fact_status"],
        manifest["source_name"],
        manifest["canonical_source_url"],
        manifest["source_published_at"],
        digest,
    )
    payload = event_payload(manifest, event_type="publication.granted")
    payload["payload_sha256"] = digest.strip()
    return (
        insert_event(
            admin,
            aggregate_type="document_publication_grants",
            aggregate_id=grant_id,
            event_type="publication.granted",
            payload=payload,
        ),
        grant_id,
    )


def projection_snapshot(admin: psycopg.Connection[Any]) -> tuple[Any, ...]:
    return one(
        admin,
        """
        SELECT (SELECT count(*) FROM public.documents),
               (SELECT count(*) FROM public.entities),
               (SELECT count(*) FROM public.claims),
               (SELECT count(*) FROM public.evidence),
               (SELECT count(*) FROM public.claim_evidence),
               (SELECT count(*) FROM public.document_entities),
               (SELECT count(*) FROM public.search_documents),
               ops._wp10_3_projection_digest()
        """,
    )


def event_snapshot(connection: psycopg.Connection[Any], event_id: uuid.UUID) -> tuple[Any, ...]:
    return one(
        connection,
        """
        SELECT published_at, terminal_at, available_at, last_error_code,
               lease_token, lease_expires_at,
               (SELECT count(*)
                  FROM audit.publication_delivery_attempts
                 WHERE event_id = ops.outbox_events.id)
          FROM ops.outbox_events
         WHERE id = %s
        """,
        event_id,
    )


def run(database_url: str) -> None:
    with psycopg.connect(libpq_url(database_url), autocommit=True) as admin:
        publisher = connect_role(database_url, "uap_publisher")
        reader = connect_role(database_url, "uap_public_reader")
        service = PublicationService(
            publisher, dispatcher_id=f"wp10-3-{uuid.uuid4().hex[:8]}", batch_limit=1
        )
        try:
            document = document_manifest(admin, f"g10-11-{uuid.uuid4().hex[:8]}")
            entity = entity_manifest(admin, f"g10-14-{uuid.uuid4().hex[:8]}")
            ai_claim = claim_manifest(admin, document, "ai", subject_entity_id=entity[0], ai=True)
            ai_event = claim_event(admin, ai_claim, "publication.granted")
            dependency_claim = claim_for(service, ai_event)
            dependency_before = {
                "event": event_snapshot(admin, ai_event),
                "projection": projection_snapshot(admin),
            }
            try:
                service.apply(dependency_claim)
            except psycopg.Error as error:
                publisher.rollback()
                require("G10-11 dependency SQLSTATE", error.sqlstate, "40001")
                require(
                    "G10-11 dependency code",
                    getattr(error.diag, "message_primary", None),
                    "publication_dependency_not_ready",
                )
            else:
                raise RuntimeError("claim published before its document")
            dependency_after = {
                "event": event_snapshot(admin, ai_event),
                "projection": projection_snapshot(admin),
            }
            require(
                "G10-11 dependency failure has no state change",
                dependency_after,
                dependency_before,
            )
            require(
                "G10-11 dependency no claim", scalar(admin, "SELECT count(*) FROM public.claims"), 0
            )
            failure = service.fail(
                dependency_claim,
                error_code="publication_dependency_not_ready",
                retry_delay_seconds=0,
            )
            publisher.commit()
            require("G10-11 dependency retry", failure.outcome, "retry_wait")

            publish_document(admin, service, document)
            time.sleep(1.1)
            service.apply(claim_for(service, ai_event))
            publisher.commit()
            require(
                "G10-11 AI claim visible", scalar(admin, "SELECT count(*) FROM public.claims"), 1
            )
            require(
                "G10-11 evidence visible", scalar(admin, "SELECT count(*) FROM public.evidence"), 1
            )
            require(
                "G10-11 claim evidence atomic",
                scalar(admin, "SELECT count(*) FROM public.claim_evidence"),
                1,
            )
            require(
                "G10-14 hidden entity has no link",
                scalar(admin, "SELECT count(*) FROM public.document_entities"),
                0,
            )

            manual_one = claim_manifest(admin, document, "manual-one")
            manual_two = claim_manifest(admin, document, "manual-two")
            event_one = claim_event(admin, manual_one, "publication.granted")
            event_two = claim_event(admin, manual_two, "publication.granted")
            lease_one = claim_for(service, event_one)
            lease_two = claim_for(service, event_two)
            publisher.commit()

            def apply_claim(claim: PublicationClaim) -> None:
                connection = connect_role(database_url, "uap_publisher")
                try:
                    PublicationService(connection, dispatcher_id="concurrent").apply(claim)
                    connection.commit()
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(apply_claim, (lease_one, lease_two)))
            ordinals = cast(
                list[int],
                scalar(
                    admin,
                    "SELECT array_agg(ordinal ORDER BY ordinal) FROM public.claims",
                ),
            )
            require("G10-12 concurrent ordinals unique", len(ordinals), len(set(ordinals)))

            shared_claim = claim_manifest(
                admin,
                document,
                "same-entity",
                subject_entity_id=entity[0],
            )
            shared_event = claim_event(admin, shared_claim, "publication.granted")
            service.apply(claim_for(service, shared_event))
            publisher.commit()

            publish_entity(admin, service, entity)
            require(
                "G10-14 entity publish reconciles link",
                scalar(admin, "SELECT count(*) FROM public.document_entities"),
                1,
            )
            require(
                "G10-14 shared entity links are deduplicated",
                scalar(
                    admin,
                    "SELECT count(*) FROM public.document_entities "
                    "WHERE entity_id=(SELECT public_id FROM audit.entity_public_identities "
                    "WHERE entity_id=%s)",
                    entity[0],
                ),
                1,
            )
            public_dump = cast(
                str,
                scalar(
                    admin,
                    "SELECT coalesce(string_agg(row_to_json(row_data)::text, ''), '') "
                    "FROM (SELECT * FROM public.document_entities) AS row_data",
                ),
            )
            require("G10-14 internal entity id hidden", str(entity[0]) in public_dump, False)
            require(
                "G10-14 internal claim id hidden", str(ai_claim["claim_id"]) in public_dump, False
            )

            revised = claim_manifest(
                admin,
                document,
                "ai-revised",
                subject_entity_id=entity[0],
                previous=ai_claim,
            )
            revised_event = claim_event(admin, revised, "publication.superseded")
            old_identity = one(
                admin,
                "SELECT public_id, display_ordinal "
                "FROM audit.claim_public_identities WHERE claim_id=%s",
                ai_claim["claim_id"],
            )
            service.apply(claim_for(service, revised_event))
            publisher.commit()
            require(
                "G10-12 identity and ordinal stable",
                one(
                    admin,
                    "SELECT public_id, display_ordinal "
                    "FROM audit.claim_public_identities WHERE claim_id=%s",
                    ai_claim["claim_id"],
                ),
                old_identity,
            )
            require(
                "G10-12 revise replaced claim text",
                scalar(
                    admin,
                    "SELECT claim_text FROM public.claims WHERE id=%s",
                    old_identity[0],
                ),
                cast(dict[str, Any], revised["manifest"])["claim_text"],
            )

            search_text, facets, vector_match = one(
                admin,
                """
                SELECT display_text, facets,
                       search_vector = to_tsvector('simple'::regconfig, display_text)
                  FROM public.search_documents
                 WHERE document_id=(SELECT public_id FROM audit.document_public_identities
                                     WHERE document_id=%s)
                """,
                document[0],
            )
            require("G10-15 search vector deterministic", vector_match, True)
            require("G10-15 evidence excluded", "PRIVATE-EVIDENCE" in search_text, False)
            require("G10-15 source URL excluded", "https://" in search_text, False)
            require(
                "G10-15 search facets exact",
                sorted(facets),
                ["category", "fact_status", "source_name"],
            )
            require("G10-15 claim text included", "ai-revised" in search_text, True)

            withdraw_event = withdraw_claim(admin, manual_two)
            withdrawn_text = cast(dict[str, Any], manual_two["manifest"])["claim_text"]
            service.apply(claim_for(service, withdraw_event))
            publisher.commit()
            require(
                "G10-13 claim withdraw removes search text",
                scalar(admin, "SELECT display_text FROM public.search_documents").find(
                    withdrawn_text
                ),
                -1,
            )

            document_public_id = scalar(
                admin,
                "SELECT public_id FROM audit.document_public_identities WHERE document_id=%s",
                document[0],
            )
            document_withdraw_event = withdraw_document(admin, document)
            service.apply(claim_for(service, document_withdraw_event))
            publisher.commit()
            require(
                "G10-13 document withdraw hides document",
                scalar(
                    admin, "SELECT count(*) FROM public.documents WHERE id=%s", document_public_id
                ),
                0,
            )
            require(
                "G10-13 document withdraw cascades claims",
                scalar(
                    admin,
                    "SELECT count(*) FROM public.claims WHERE document_id=%s",
                    document_public_id,
                ),
                0,
            )
            require(
                "G10-13 document withdraw removes search",
                scalar(
                    admin,
                    "SELECT count(*) FROM public.search_documents WHERE document_id=%s",
                    document_public_id,
                ),
                0,
            )
            require(
                "G10-13 claim grants remain active",
                scalar(
                    admin,
                    "SELECT count(*) FROM audit.claim_publication_grants AS grant_row "
                    "JOIN audit.claim_publication_manifests AS manifest "
                    "ON manifest.grant_id=grant_row.id "
                    "WHERE manifest.document_id=%s AND grant_row.grant_status='active'",
                    document[0],
                ),
                3,
            )
            republish_event, republish_grant = republish_document(admin, document)
            service.apply(claim_for(service, republish_event))
            publisher.commit()
            require(
                "G10-13 republish reuses document identity",
                scalar(admin, "SELECT id FROM public.documents WHERE id=%s", document_public_id),
                document_public_id,
            )
            require(
                "G10-13 republish restores active claims",
                scalar(
                    admin,
                    "SELECT count(*) FROM public.claims WHERE document_id=%s",
                    document_public_id,
                ),
                3,
            )
            require(
                "G10-13 republish restores search",
                scalar(
                    admin,
                    "SELECT count(*) FROM public.search_documents WHERE document_id=%s",
                    document_public_id,
                ),
                1,
            )

            require(
                "G10-11 public reader can read claims",
                scalar(reader, "SELECT count(*) FROM public.claims") >= 1,
                True,
            )
            reader.rollback()
            denied_state, _denied_code = error_pair(
                reader,
                "SELECT count(*) FROM audit.claim_publication_manifests",
            )
            require("G10-14 public reader cannot read internal manifests", denied_state, "42501")

            pending_event = insert_event(
                admin,
                aggregate_type="claim_publication_grants",
                aggregate_id=uuid.uuid4(),
                event_type="publication.granted",
                payload={
                    "schema": "publication-outbox.v2",
                    "subject_type": "claim",
                    "grant_id": str(uuid.uuid4()),
                    "decision_id": str(uuid.uuid4()),
                    "subject_id": str(uuid.uuid4()),
                    "revision_no": 1,
                    "payload_sha256": "a" * 64,
                },
            )
            before_rebuild = projection_snapshot(admin)
            rebuild_id = uuid.uuid4()
            with psycopg.connect(libpq_url(database_url), autocommit=False) as migrator:
                execute(migrator, "SET SESSION AUTHORIZATION uap_migrator")
                first = one(
                    migrator,
                    "SELECT * FROM ops.rebuild_public_projection(%s)",
                    rebuild_id,
                )
                migrator.commit()
                second = one(
                    migrator,
                    "SELECT * FROM ops.rebuild_public_projection(%s)",
                    rebuild_id,
                )
                migrator.commit()
            require("G10-15 rebuild succeeded", first[4], "succeeded")
            require("G10-15 rebuild replay", second[5], True)
            require("G10-15 rebuild digest stable", second[2], first[2])
            require(
                "G10-15 rebuild does not ack pending event",
                one(
                    admin,
                    "SELECT published_at, terminal_at FROM ops.outbox_events WHERE id=%s",
                    pending_event,
                ),
                (None, None),
            )
            after_rebuild = projection_snapshot(admin)
            require("G10-15 online/rebuild content digest", after_rebuild[-1], before_rebuild[-1])
            EVIDENCE.append(
                {
                    "kind": "state",
                    "name": "G10-15 rebuild counts and digest",
                    "before": before_rebuild,
                    "after": after_rebuild,
                }
            )

            tamper_grant_id = cast(uuid.UUID, revised["grant_id"])
            original_manifest_text = scalar(
                admin,
                "SELECT claim_text FROM audit.claim_publication_manifests WHERE grant_id=%s",
                tamper_grant_id,
            )
            execute(admin, "SET session_replication_role = replica")
            execute(
                admin,
                "UPDATE audit.claim_publication_manifests SET claim_text=%s WHERE grant_id=%s",
                f"{original_manifest_text} tampered",
                tamper_grant_id,
            )
            execute(admin, "SET session_replication_role = origin")
            before_tampered_replay = projection_snapshot(admin)
            try:
                with psycopg.connect(libpq_url(database_url), autocommit=False) as migrator:
                    execute(migrator, "SET SESSION AUTHORIZATION uap_migrator")
                    tamper_state, tamper_code = error_pair(
                        migrator,
                        "SELECT * FROM ops.rebuild_public_projection(%s)",
                        rebuild_id,
                    )
                require("G10-15 replay tamper SQLSTATE", tamper_state, "22023")
                require("G10-15 replay tamper code", tamper_code, "publication_rebuild_mismatch")
            finally:
                execute(admin, "SET session_replication_role = replica")
                execute(
                    admin,
                    "UPDATE audit.claim_publication_manifests SET claim_text=%s WHERE grant_id=%s",
                    original_manifest_text,
                    tamper_grant_id,
                )
                execute(admin, "SET session_replication_role = origin")
            require(
                "G10-15 replay tamper has no projection change",
                projection_snapshot(admin),
                before_tampered_replay,
            )
            EVIDENCE.append(
                {
                    "kind": "state",
                    "name": "G10-15 replay tamper rollback counts and digest",
                    "before": before_tampered_replay,
                    "after": projection_snapshot(admin),
                }
            )

            conflict_fixture = entity_manifest(admin, f"rebuild-conflict-{uuid.uuid4().hex[:8]}")
            with psycopg.connect(libpq_url(database_url), autocommit=False) as migrator:
                execute(migrator, "SET SESSION AUTHORIZATION uap_migrator")
                state, code = error_pair(
                    migrator,
                    "SELECT * FROM ops.rebuild_public_projection(%s)",
                    rebuild_id,
                )
            require("G10-15 rebuild conflict SQLSTATE", state, "40001")
            require("G10-15 rebuild conflict code", code, "publication_rebuild_id_conflict")

            before_mismatch = projection_snapshot(admin)
            original_hash = scalar(
                admin,
                "SELECT publication_payload_sha256 "
                "FROM audit.entity_publication_grants WHERE id=%s",
                conflict_fixture[1],
            )
            execute(
                admin,
                "UPDATE audit.entity_publication_grants "
                "SET publication_payload_sha256=%s WHERE id=%s",
                "0" * 64,
                conflict_fixture[1],
            )
            failed_id = uuid.uuid4()
            with psycopg.connect(libpq_url(database_url), autocommit=False) as migrator:
                execute(migrator, "SET SESSION AUTHORIZATION uap_migrator")
                failed = one(
                    migrator,
                    "SELECT * FROM ops.rebuild_public_projection(%s)",
                    failed_id,
                )
                migrator.commit()
            require("G10-15 rebuild mismatch status", failed[4], "failed")
            require(
                "G10-15 rebuild mismatch stable code",
                scalar(
                    admin,
                    "SELECT error_code FROM audit.publication_rebuild_runs WHERE rebuild_id=%s",
                    failed_id,
                ),
                "publication_rebuild_mismatch",
            )
            require(
                "G10-15 mismatch projection rollback", projection_snapshot(admin), before_mismatch
            )
            EVIDENCE.append(
                {
                    "kind": "state",
                    "name": "G10-15 mismatch rollback counts and digest",
                    "before": before_mismatch,
                    "after": projection_snapshot(admin),
                }
            )
            execute(
                admin,
                "UPDATE audit.entity_publication_grants "
                "SET publication_payload_sha256=%s WHERE id=%s",
                original_hash,
                conflict_fixture[1],
            )

            missing_manifest_targets = (
                ("document", "audit.document_publication_manifests", republish_grant),
                ("entity", "audit.entity_publication_manifests", entity[1]),
                ("claim", "audit.claim_publication_manifests", manual_one["grant_id"]),
            )
            execute(admin, "SET session_replication_role = replica")
            for _subject_type, table, grant_id in missing_manifest_targets:
                execute(admin, f"DELETE FROM {table} WHERE grant_id=%s", grant_id)  # noqa: S608
            execute(admin, "SET session_replication_role = origin")
            before_missing_manifest = projection_snapshot(admin)
            missing_rebuild_id = uuid.uuid4()
            with psycopg.connect(libpq_url(database_url), autocommit=False) as migrator:
                execute(migrator, "SET SESSION AUTHORIZATION uap_migrator")
                missing_report = one(
                    migrator,
                    "SELECT * FROM ops.rebuild_public_projection(%s)",
                    missing_rebuild_id,
                )
                migrator.commit()
            missing_counts = cast(dict[str, Any], missing_report[3])
            require("G10-15 missing manifests status", missing_report[4], "failed")
            require(
                "G10-15 missing manifests code",
                missing_counts.get("error_code"),
                "publication_rebuild_mismatch",
            )
            require(
                "G10-15 missing manifests have no projection change",
                projection_snapshot(admin),
                before_missing_manifest,
            )
            EVIDENCE.append(
                {
                    "kind": "state",
                    "name": "G10-15 missing manifest categories rollback",
                    "categories": [item[0] for item in missing_manifest_targets],
                    "before": before_missing_manifest,
                    "after": projection_snapshot(admin),
                }
            )

            api = connect_role(database_url, "uap_api")
            try:
                state, _code = error_pair(
                    api,
                    "SELECT * FROM ops.rebuild_public_projection(%s)",
                    uuid.uuid4(),
                )
                require("G10-15 API rebuild denied", state, "42501")
            finally:
                api.close()
            state, _code = error_pair(
                publisher,
                "SELECT * FROM ops.rebuild_public_projection(%s)",
                uuid.uuid4(),
            )
            require("G10-15 Publisher rebuild denied", state, "42501")
        finally:
            publisher.close()
            reader.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.environ.get("UAP_DATABASE_URL"))
    parser.add_argument("--evidence-out", type=Path)
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or UAP_DATABASE_URL is required")
    status = "passed"
    try:
        run(args.database_url)
    except Exception:
        status = "failed"
        raise
    finally:
        if args.evidence_out:
            args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
            args.evidence_out.write_text(
                json.dumps(
                    {
                        "schema": "wp10.3-runtime-evidence.v1",
                        "status": status,
                        "checks": EVIDENCE,
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )
    print("G10-11 G10-12 G10-13 G10-14 G10-15 runtime probe passed")


if __name__ == "__main__":
    main()
