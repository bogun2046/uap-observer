"""Real-role WP10.2 probe for G10-06 through G10-10.

The probe uses only deterministic database fixtures and the four configured
role passwords.  It records stable SQLSTATE/primary-code pairs and never
prints SQL text, credentials, manifest contents, or exception detail.
"""

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
from threading import Event
from typing import Any, cast

import psycopg
from psycopg.conninfo import make_conninfo

_PLATFORM_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(_PLATFORM_ROOT), str(_PLATFORM_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tools.wp8_1_runtime_probe import seed_document  # noqa: E402
from tools.wp8_6_runtime_probe import g8_16c  # noqa: E402
from tools.wp9_2_runtime_probe import (  # noqa: E402
    bind_role,
    g9_08,
    insert_person,
    seed_grantor,
)
from uap_platform.publishing.service import PublicationClaim, PublicationService  # noqa: E402

PUBLISHER_PASSWORD_ENV = "UAP_" + "PUBLISHER_PASSWORD"
ROLE_PASSWORDS = {
    "uap_api": "UAP_API_PASSWORD",
    "uap_worker": "UAP_WORKER_PASSWORD",
    "uap_public_reader": "UAP_PUBLIC_READER_PASSWORD",
    "uap_publisher": PUBLISHER_PASSWORD_ENV,
}
PROBE_EVIDENCE: list[dict[str, Any]] = []


def libpq_url(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def connect_role(url: str, role: str) -> psycopg.Connection[Any]:
    password = os.environ.get(ROLE_PASSWORDS.get(role, PUBLISHER_PASSWORD_ENV))
    if not password:
        raise RuntimeError(f"missing password for {role}")
    connection = psycopg.connect(make_conninfo(libpq_url(url), user=role, password=password))
    connection.autocommit = False
    return connection


def scalar(connection: psycopg.Connection[Any], statement: str, *params: object) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("probe query returned no row")
    return row[0]


def one(connection: psycopg.Connection[Any], statement: str, *params: object) -> tuple[Any, ...]:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("probe query returned no row")
    return tuple(row)


def execute(connection: psycopg.Connection[Any], statement: str, *params: object) -> None:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def publication_state_snapshot(
    admin: psycopg.Connection[Any], event_id: uuid.UUID, document_id: uuid.UUID
) -> tuple[Any, ...]:
    """Capture event, attempt, and document projection state for zero-write probes."""

    event_state = one(
        admin,
        """
        SELECT published_at, terminal_at, publish_attempts, lease_token,
               lease_expires_at, available_at, last_error_code, terminal_error_code
          FROM ops.outbox_events
         WHERE id=%s
        """,
        event_id,
    )
    attempt_state = one(
        admin,
        """
        SELECT count(*), coalesce(max(attempt_no), 0), max(outcome::text),
               max(operation_payload_hash), max(sanitized_error_code),
               max(sanitized_error_summary), max(finished_at), max(terminal_at)
          FROM audit.publication_delivery_attempts
         WHERE event_id=%s
        """,
        event_id,
    )
    projection_state = one(
        admin,
        """
        SELECT
            (SELECT count(*)
               FROM audit.document_public_identities
              WHERE document_id=%s),
            (SELECT count(*)
               FROM public.documents
              WHERE id=(SELECT public_id
                          FROM audit.document_public_identities
                         WHERE document_id=%s))
        """,
        document_id,
        document_id,
    )
    return event_state, attempt_state, projection_state


def error_pair(
    connection: psycopg.Connection[Any], statement: str, *params: object
) -> tuple[str | None, str | None]:
    try:
        execute(connection, statement, *params)
    except psycopg.Error as error:
        connection.rollback()
        primary = getattr(getattr(error, "diag", None), "message_primary", None)
        PROBE_EVIDENCE.append({"kind": "sql_error", "sqlstate": error.sqlstate, "primary": primary})
        return error.sqlstate, primary
    connection.rollback()
    PROBE_EVIDENCE.append({"kind": "sql_error", "sqlstate": None, "primary": None})
    return None, None


def require(name: str, actual: object, expected: object) -> None:
    PROBE_EVIDENCE.append({"kind": "assertion", "name": name, "passed": actual == expected})
    if actual != expected:
        raise RuntimeError(f"{name}: unexpected result")


def insert_principal(admin: psycopg.Connection[Any], tag: str) -> uuid.UUID:
    principal = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO audit.principals (id, principal_type, service_name, display_name)
        VALUES (%s, 'service', %s, 'WP10.2 runtime probe')
        """,
        principal,
        f"wp10-2-{tag}-{principal.hex[:8]}",
    )
    return principal


def document_manifest(
    admin: psycopg.Connection[Any],
    tag: str,
    *,
    revision: int = 1,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, dict[str, Any]]:
    _principal_id, document_version_id, _source_id = seed_document(
        admin, f"wp10-2-{tag}-{uuid.uuid4().hex[:8]}"
    )
    document_id = cast(
        uuid.UUID,
        scalar(
            admin,
            "SELECT document_id FROM core.document_versions WHERE id=%s",
            document_version_id,
        ),
    )
    actor = insert_principal(admin, f"document-{tag}")
    case_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    now = datetime.now(UTC)
    execute(
        admin,
        """
        INSERT INTO audit.review_cases (
            id, document_version_id, case_type, status, priority, opened_by, opened_at
        ) VALUES (%s, %s, 'document', 'approved', 0, %s, %s)
        """,
        case_id,
        document_version_id,
        actor,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO audit.review_decisions (
            id, review_case_id, sequence_no, decision, reason, structured_changes,
            decided_by, decided_at
        ) VALUES (%s, %s, %s, 'approve', 'WP10.2 runtime fixture', %s::jsonb, %s, %s)
        """,
        decision_id,
        case_id,
        revision,
        json.dumps({}, separators=(",", ":")),
        actor,
        now,
    )
    source_name, source_url, source_published_at = one(
        admin,
        """
        SELECT source.name, document.canonical_url, version.source_published_at
          FROM core.document_versions AS version
          JOIN core.documents AS document ON document.id = version.document_id
          JOIN ingest.sources AS source ON source.id = document.source_id
         WHERE version.id=%s
        """,
        document_version_id,
    )
    manifest = {
        "schema": "publication-manifest.v2",
        "subject_type": "document",
        "grant_id": str(grant_id),
        "decision_id": str(decision_id),
        "subject_id": str(document_version_id),
        "document_id": str(document_id),
        "document_version_id": str(document_version_id),
        "revision_no": revision,
        "title": f"WP10.2 {tag}",
        "summary": None,
        "category": "official_report",
        "fact_status": "source_reported",
        "source_name": str(source_name),
        "canonical_source_url": str(source_url),
        "source_published_at": (
            None
            if source_published_at is None
            else source_published_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        ),
        "summary_analysis_result_id": None,
    }
    digest = str(
        scalar(
            admin,
            "SELECT audit._publication_manifest_sha(%s::jsonb)",
            json.dumps(manifest),
        )
    )
    execute(
        admin,
        """
        INSERT INTO audit.document_publication_grants (
            id, review_case_id, document_version_id, decision_id, revision_no,
            grant_status, granted_at, publication_payload_sha256
        ) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s)
        """,
        grant_id,
        case_id,
        document_version_id,
        decision_id,
        revision,
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
        ) VALUES (%s, %s, %s, %s, %s, %s, NULL, %s::public.document_category,
                  %s::public.fact_status, %s, %s, %s, NULL, %s)
        """,
        grant_id,
        case_id,
        decision_id,
        document_id,
        document_version_id,
        manifest["title"],
        manifest["category"],
        manifest["fact_status"],
        source_name,
        source_url,
        source_published_at,
        digest,
    )
    return document_id, document_version_id, grant_id, decision_id, manifest


def revise_document_manifest(
    admin: psycopg.Connection[Any],
    original: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, dict[str, Any]],
) -> tuple[uuid.UUID, dict[str, Any], uuid.UUID]:
    """Create a revision fixture for the same logical document."""

    document_id, document_version_id, old_grant_id, _old_decision_id, old_manifest = original
    actor = insert_principal(admin, "document-revise")
    case_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    now = datetime.now(UTC)
    execute(
        admin,
        "UPDATE audit.review_cases SET status='closed', closed_at=clock_timestamp() "
        "WHERE id=(SELECT review_case_id FROM audit.document_publication_grants WHERE id=%s)",
        old_grant_id,
    )
    execute(
        admin,
        """
        INSERT INTO audit.review_cases (
            id, document_version_id, case_type, status, priority, opened_by, opened_at
        ) VALUES (%s, %s, 'document', 'approved', 0, %s, %s)
        """,
        case_id,
        document_version_id,
        actor,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO audit.review_decisions (
            id, review_case_id, sequence_no, decision, reason, structured_changes,
            decided_by, decided_at
        ) VALUES (%s, %s, 1, 'revise', 'WP10.2 revision fixture', '{}'::jsonb, %s, %s)
        """,
        decision_id,
        case_id,
        actor,
        now,
    )
    revised = dict(old_manifest)
    revised.update(
        {
            "grant_id": str(grant_id),
            "decision_id": str(decision_id),
            "revision_no": 2,
            "title": "WP10.2 revised document",
        }
    )
    digest = str(
        scalar(
            admin,
            "SELECT audit._publication_manifest_sha(%s::jsonb)",
            json.dumps(revised),
        )
    )
    execute(
        admin,
        """
        UPDATE audit.document_publication_grants
           SET grant_status = 'superseded'
         WHERE id = %s
        """,
        old_grant_id,
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
        document_version_id,
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
        ) VALUES (%s, %s, %s, %s, %s, %s, NULL, %s::public.document_category,
                  %s::public.fact_status, %s, %s, %s, NULL, %s)
        """,
        grant_id,
        case_id,
        decision_id,
        document_id,
        document_version_id,
        revised["title"],
        revised["category"],
        revised["fact_status"],
        revised["source_name"],
        revised["canonical_source_url"],
        revised["source_published_at"],
        digest,
    )
    revised["payload_sha256"] = digest
    return grant_id, revised, old_grant_id


def entity_manifest(
    admin: psycopg.Connection[Any],
    tag: str,
    *,
    revision: int = 1,
    canonical_name: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, dict[str, Any]]:
    actor = insert_principal(admin, f"entity-{tag}")
    entity_id = uuid.uuid4()
    name = canonical_name or f"WP10.2 {tag}"
    execute(
        admin,
        """
        INSERT INTO core.entities (id, entity_type, canonical_name, description, status)
        VALUES (%s, 'organization', %s, 'WP10.2 fixture', 'active')
        """,
        entity_id,
        name,
    )
    case_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    now = datetime.now(UTC)
    execute(
        admin,
        """
        INSERT INTO audit.review_cases (
            id, entity_id, case_type, status, priority, opened_by, opened_at
        ) VALUES (%s, %s, 'entity', 'approved', 0, %s, %s)
        """,
        case_id,
        entity_id,
        actor,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO audit.review_decisions (
            id, review_case_id, sequence_no, decision, reason, structured_changes,
            decided_by, decided_at
        ) VALUES (%s, %s, %s, 'approve', 'WP10.2 runtime fixture', '{}'::jsonb, %s, %s)
        """,
        decision_id,
        case_id,
        revision,
        actor,
        now,
    )
    manifest = {
        "schema": "publication-manifest.v2",
        "subject_type": "entity",
        "grant_id": str(grant_id),
        "decision_id": str(decision_id),
        "subject_id": str(entity_id),
        "revision_no": revision,
        "entity_type": "organization",
        "canonical_name": name,
        "description": "WP10.2 fixture",
        "country_code": None,
    }
    digest = str(
        scalar(
            admin,
            "SELECT audit._publication_manifest_sha(%s::jsonb)",
            json.dumps(manifest),
        )
    )
    execute(
        admin,
        """
        INSERT INTO audit.entity_publication_grants (
            id, review_case_id, entity_id, decision_id, revision_no,
            grant_status, granted_at, publication_payload_sha256
        ) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s)
        """,
        grant_id,
        case_id,
        entity_id,
        decision_id,
        revision,
        now,
        digest,
    )
    execute(
        admin,
        """
        INSERT INTO audit.entity_publication_manifests (
            grant_id, review_case_id, decision_id, entity_id, entity_type,
            canonical_name, description, country_code, manifest_sha256
        ) VALUES (%s, %s, %s, %s, 'organization', %s, %s, NULL, %s)
        """,
        grant_id,
        case_id,
        decision_id,
        entity_id,
        manifest["canonical_name"],
        manifest["description"],
        digest,
    )
    return entity_id, grant_id, decision_id, manifest


def revise_entity_manifest(
    admin: psycopg.Connection[Any],
    original: tuple[uuid.UUID, uuid.UUID, uuid.UUID, dict[str, Any]],
) -> tuple[uuid.UUID, dict[str, Any], uuid.UUID]:
    """Create a v2 entity revision for the same canonical entity."""

    entity_id, old_grant_id, _old_decision_id, old_manifest = original
    actor = insert_principal(admin, "entity-revise")
    case_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    now = datetime.now(UTC)
    execute(
        admin,
        "UPDATE audit.review_cases SET status='closed', closed_at=clock_timestamp() "
        "WHERE id=(SELECT review_case_id FROM audit.entity_publication_grants WHERE id=%s)",
        old_grant_id,
    )
    execute(
        admin,
        """
        INSERT INTO audit.review_cases (
            id, entity_id, case_type, status, priority, opened_by, opened_at
        ) VALUES (%s, %s, 'entity', 'approved', 0, %s, %s)
        """,
        case_id,
        entity_id,
        actor,
        now,
    )
    execute(
        admin,
        """
        INSERT INTO audit.review_decisions (
            id, review_case_id, sequence_no, decision, reason, structured_changes,
            decided_by, decided_at
        ) VALUES (%s, %s, 1, 'revise', 'WP10.2 entity revision fixture', '{}'::jsonb, %s, %s)
        """,
        decision_id,
        case_id,
        actor,
        now,
    )
    revised = dict(old_manifest)
    revised.update(
        {
            "grant_id": str(grant_id),
            "decision_id": str(decision_id),
            "revision_no": 2,
            "canonical_name": f"{old_manifest['canonical_name']} revised",
        }
    )
    digest = str(
        scalar(
            admin,
            "SELECT audit._publication_manifest_sha(%s::jsonb)",
            json.dumps(revised),
        )
    )
    execute(
        admin,
        "UPDATE audit.entity_publication_grants SET grant_status='superseded' WHERE id=%s",
        old_grant_id,
    )
    execute(
        admin,
        """
        INSERT INTO audit.entity_publication_grants (
            id, review_case_id, entity_id, decision_id, revision_no,
            grant_status, granted_at, publication_payload_sha256
        ) VALUES (%s, %s, %s, %s, 2, 'active', %s, %s)
        """,
        grant_id,
        case_id,
        entity_id,
        decision_id,
        now,
        digest,
    )
    execute(
        admin,
        """
        INSERT INTO audit.entity_publication_manifests (
            grant_id, review_case_id, decision_id, entity_id, entity_type,
            canonical_name, description, country_code, manifest_sha256
        ) VALUES (%s, %s, %s, %s, %s::core.entity_type, %s, %s, NULL, %s)
        """,
        grant_id,
        case_id,
        decision_id,
        entity_id,
        revised["entity_type"],
        revised["canonical_name"],
        revised["description"],
        digest,
    )
    execute(
        admin,
        "UPDATE core.entities SET canonical_name=%s, updated_at=clock_timestamp() WHERE id=%s",
        revised["canonical_name"],
        entity_id,
    )
    revised["payload_sha256"] = digest
    return grant_id, revised, old_grant_id


def insert_event(
    admin: psycopg.Connection[Any],
    *,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    event_key: str | None = None,
    occurred_at: str | None = None,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    if event_key is not None:
        resolved_event_key = event_key
    elif event_type == "publication.granted":
        resolved_event_key = (
            f"publication-granted:{aggregate_type}:{payload.get('grant_id', event_id)}"
        )
    elif event_type == "publication.withdrawn":
        resolved_event_key = (
            f"publication-withdrawn:{aggregate_type}:{payload.get('grant_id', event_id)}:"
            f"{payload.get('decision_id', event_id)}"
        )
    elif event_type == "publication.superseded":
        resolved_event_key = (
            f"publication-superseded:{aggregate_type}:{payload.get('old_grant_id', event_id)}:"
            f"{payload.get('grant_id', event_id)}"
        )
    else:
        resolved_event_key = f"wp10-2-runtime-{event_id}"
    execute(
        admin,
        """
        INSERT INTO ops.outbox_events (
            id, aggregate_type, aggregate_id, event_type, event_key, payload, occurred_at
        ) VALUES (%s, %s, %s, %s, %s, %s::jsonb,
                  COALESCE(%s::timestamptz, clock_timestamp()))
        """,
        event_id,
        aggregate_type,
        aggregate_id,
        event_type,
        resolved_event_key,
        json.dumps(payload, separators=(",", ":")),
        occurred_at,
    )
    return event_id


def event_payload(
    manifest: dict[str, Any],
    *,
    event_type: str,
    old_grant_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    payload = {
        "schema": "publication-outbox.v2",
        "grant_id": manifest["grant_id"],
        "decision_id": manifest["decision_id"],
        "subject_type": manifest["subject_type"],
        "subject_id": manifest["subject_id"],
        "revision_no": manifest["revision_no"],
        "payload_sha256": "",
    }
    # The caller fills this from the grant fixture's manifest digest.
    if old_grant_id is not None:
        payload["old_grant_id"] = str(old_grant_id)
    return payload


def claim_for(service: PublicationService, event_id: uuid.UUID) -> PublicationClaim:
    for _ in range(5):
        claims = service.claim()
        service.connection.commit()
        for claim in claims:
            if claim.event_id == event_id:
                return claim
    raise RuntimeError("target event was not claimed")


def create_document_event(
    admin: psycopg.Connection[Any],
    tag: str,
    *,
    aggregate_id: uuid.UUID | None = None,
    payload_sha256: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a valid document grant and a v2 outbox event for a probe case."""

    document_id, _version, grant_id, _decision, manifest = document_manifest(admin, tag)
    digest = str(
        scalar(
            admin,
            (
                "SELECT publication_payload_sha256 "
                "FROM audit.document_publication_grants WHERE id=%s"
            ),
            grant_id,
        )
    )
    payload = event_payload(manifest, event_type="publication.granted")
    payload["payload_sha256"] = payload_sha256 or digest
    event_id = insert_event(
        admin,
        aggregate_type="document_publication_grants",
        aggregate_id=aggregate_id or grant_id,
        event_type="publication.granted",
        payload=payload,
    )
    admin.commit()
    return document_id, event_id


def g10_06(admin: psycopg.Connection[Any], publisher_url: str) -> None:
    payload = {
        "schema": "publication-outbox.v2",
        "grant_id": str(uuid.uuid4()),
        "decision_id": str(uuid.uuid4()),
        "subject_type": "document",
        "subject_id": str(uuid.uuid4()),
        "revision_no": 1,
        "payload_sha256": "0" * 64,
    }
    event_id = insert_event(
        admin,
        aggregate_type="document_publication_grants",
        aggregate_id=uuid.uuid4(),
        event_type="publication.granted",
        payload=payload,
        occurred_at="1970-01-01T00:00:00+00:00",
    )
    relation_id = insert_event(
        admin,
        aggregate_type="relation_publication_grants",
        aggregate_id=uuid.uuid4(),
        event_type="publication.granted",
        payload={**payload, "subject_type": "relation"},
    )
    excluded_ids = [
        insert_event(
            admin,
            aggregate_type="document_publication_grants",
            aggregate_id=uuid.uuid4(),
            event_type="publication.granted",
            payload={"schema": "publication-outbox.v1"},
        ),
        insert_event(
            admin,
            aggregate_type="document_publication_grants",
            aggregate_id=uuid.uuid4(),
            event_type="publication.unknown",
            payload=payload,
        ),
    ]
    terminal_id = insert_event(
        admin,
        aggregate_type="document_publication_grants",
        aggregate_id=uuid.uuid4(),
        event_type="publication.granted",
        payload={**payload, "grant_id": str(uuid.uuid4())},
    )
    execute(
        admin,
        "UPDATE ops.outbox_events SET terminal_at=clock_timestamp(), "
        "terminal_error_code='publication_event_schema_unsupported' WHERE id=%s",
        terminal_id,
    )
    admin.commit()

    def one_claim() -> tuple[tuple[Any, ...], ...]:
        connection = connect_role(publisher_url, "uap_publisher")
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM ops.claim_publication_outbox(%s, 30, 1)",
                    (f"g10-06-{uuid.uuid4().hex[:8]}",),
                )
                rows = tuple(cursor.fetchall())
            connection.commit()
            return rows
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _value: one_claim(), (1, 2)))
    claimed = [row for rows in results for row in rows if row[0] == event_id]
    require("G10-06 one concurrent lease", len(claimed), 1)
    before_invalid_attempts = scalar(
        admin,
        "SELECT count(*) FROM audit.publication_delivery_attempts",
    )
    connection = connect_role(publisher_url, "uap_publisher")
    try:
        wrong_token_before = publication_state_snapshot(
            admin, event_id, uuid.UUID(str(payload["subject_id"]))
        )
        wrong_state, wrong_code = error_pair(
            connection,
            "SELECT * FROM ops.apply_publication_event(%s, %s)",
            event_id,
            uuid.uuid4(),
        )
        require("G10-06 wrong token SQLSTATE", wrong_state, "40001")
        require("G10-06 wrong token code", wrong_code, "publication_lease_lost")
        require(
            "G10-06 wrong token has no state change",
            publication_state_snapshot(admin, event_id, uuid.UUID(str(payload["subject_id"]))),
            wrong_token_before,
        )
        forged_fail_before = publication_state_snapshot(
            admin, event_id, uuid.UUID(str(payload["subject_id"]))
        )
        wrong_fail_state, wrong_fail_code = error_pair(
            connection,
            "SELECT * FROM ops.fail_publication_event(%s, %s, "
            "'publication_dependency_not_ready', "
            "'forged token summary', 0, false)",
            event_id,
            uuid.uuid4(),
        )
        require("G10-06 wrong fail token SQLSTATE", wrong_fail_state, "40001")
        require("G10-06 wrong fail token code", wrong_fail_code, "publication_lease_lost")
        require(
            "G10-06 forged fail token has no state change",
            publication_state_snapshot(admin, event_id, uuid.UUID(str(payload["subject_id"]))),
            forged_fail_before,
        )
        generic_ack_state, _generic_ack_code = error_pair(
            connection,
            "SELECT ops.ack_outbox(%s, %s)",
            event_id,
            claimed[0][8],
        )
        require("G10-06 generic ack denied", generic_ack_state, "42501")
        direct_public_state, _direct_public_code = error_pair(
            connection,
            "UPDATE public.documents SET title=title WHERE false",
        )
        require("G10-06 publisher public DML denied", direct_public_state, "42501")
        invalid_parameters_before = publication_state_snapshot(
            admin, event_id, uuid.UUID(str(payload["subject_id"]))
        )
        invalid_state, invalid_code = error_pair(
            connection,
            "SELECT * FROM ops.claim_publication_outbox(%s, %s, %s)",
            "g10-06-invalid",
            0,
            1,
        )
        require("G10-06 invalid lease parameter", invalid_state, "22023")
        require(
            "G10-06 invalid lease parameter code",
            invalid_code,
            "publication_claim_parameters_invalid",
        )
        invalid_limit_state, invalid_limit_code = error_pair(
            connection,
            "SELECT * FROM ops.claim_publication_outbox(%s, %s, %s)",
            "g10-06-invalid",
            30,
            101,
        )
        require("G10-06 invalid limit parameter", invalid_limit_state, "22023")
        require(
            "G10-06 invalid limit parameter code",
            invalid_limit_code,
            "publication_claim_parameters_invalid",
        )
        after_invalid_attempts = scalar(
            admin,
            "SELECT count(*) FROM audit.publication_delivery_attempts",
        )
        require(
            "G10-06 invalid parameters do not create attempts",
            after_invalid_attempts,
            before_invalid_attempts,
        )
        require(
            "G10-06 invalid parameters have no state change",
            publication_state_snapshot(admin, event_id, uuid.UUID(str(payload["subject_id"]))),
            invalid_parameters_before,
        )
        claim = PublicationService._claim(claimed[0])
        failure_service = PublicationService(connection, dispatcher_id="g10-06-cleanup")
        failure_service.fail(
            claim,
            error_code="publication_grant_missing",
            terminal=True,
        )
        connection.commit()
    finally:
        connection.close()
        require(
            "G10-06 invalid/terminal events not claimed",
            scalar(
                admin,
                "SELECT count(*) FROM audit.publication_delivery_attempts WHERE event_id = ANY(%s)",
                [*excluded_ids, terminal_id],
            ),
            0,
        )
        require(
            "G10-06 relation terminal attempt",
            one(
                admin,
                "SELECT count(*), max(outcome::text), max(sanitized_error_code) "
                "FROM audit.publication_delivery_attempts WHERE event_id=%s",
                relation_id,
            ),
            (1, "terminal_failure", "publication_event_schema_unsupported"),
        )
        relation_event_state = one(
            admin,
            "SELECT published_at, terminal_at, terminal_error_code "
            "FROM ops.outbox_events WHERE id=%s",
            relation_id,
        )
        require("G10-06 relation remains unpublished", relation_event_state[0], None)
        require("G10-06 relation is terminal", relation_event_state[1] is not None, True)
        require(
            "G10-06 relation terminal code",
            relation_event_state[2],
            "publication_event_schema_unsupported",
        )


def g10_07_08(admin: psycopg.Connection[Any], publisher_url: str) -> None:
    publisher = connect_role(publisher_url, "uap_publisher")
    service = PublicationService(
        publisher, dispatcher_id="g10-07-08", lease_seconds=30, batch_limit=1
    )
    try:
        original = document_manifest(admin, "document-first")
        document_id, _version, grant_id, _decision_id, manifest = original
        digest = str(
            scalar(
                admin,
                (
                    "SELECT publication_payload_sha256 "
                    "FROM audit.document_publication_grants WHERE id=%s"
                ),
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
        first = service.apply(claim_for(service, event_id))
        publisher.commit()
        public_id, slug = one(
            admin,
            "SELECT public_id, slug FROM audit.document_public_identities WHERE document_id=%s",
            document_id,
        )
        require("G10-07 stable document slug", slug, f"d-{str(public_id).replace('-', '')}")
        require(
            "G10-07 revision one",
            scalar(admin, "SELECT revision_no FROM public.documents WHERE id=%s", public_id),
            1,
        )
        first_published_at = scalar(
            admin, "SELECT published_at FROM public.documents WHERE id=%s", public_id
        )

        revised_grant, revised_manifest, old_grant = revise_document_manifest(admin, original)
        revised_digest = str(
            scalar(
                admin,
                (
                    "SELECT publication_payload_sha256 "
                    "FROM audit.document_publication_grants WHERE id=%s"
                ),
                revised_grant,
            )
        )
        revised_manifest["payload_sha256"] = revised_digest
        superseded_event = insert_event(
            admin,
            aggregate_type="document_publication_grants",
            aggregate_id=old_grant,
            event_type="publication.superseded",
            payload=event_payload(
                revised_manifest,
                event_type="publication.superseded",
                old_grant_id=old_grant,
            )
            | {"payload_sha256": revised_digest},
        )
        granted_revision_event = insert_event(
            admin,
            aggregate_type="document_publication_grants",
            aggregate_id=revised_grant,
            event_type="publication.granted",
            payload=event_payload(revised_manifest, event_type="publication.granted")
            | {"payload_sha256": revised_digest},
        )
        admin.commit()
        service.apply(claim_for(service, superseded_event))
        publisher.commit()
        service.apply(claim_for(service, granted_revision_event))
        publisher.commit()
        revised_public_id, revised_slug, revised_revision, revised_published_at = one(
            admin,
            "SELECT id, slug, revision_no, published_at FROM public.documents WHERE id=%s",
            public_id,
        )
        require("G10-07 stable id on revise", revised_public_id, public_id)
        require("G10-07 stable slug on revise", revised_slug, slug)
        require("G10-07 revision two", revised_revision, 2)
        require("G10-07 published_at preserved", revised_published_at, first_published_at)

        stale_manifest = dict(manifest)
        stale_manifest["payload_sha256"] = digest
        stale_event = insert_event(
            admin,
            aggregate_type="document_publication_grants",
            aggregate_id=grant_id,
            event_type="publication.granted",
            payload=event_payload(stale_manifest, event_type="publication.granted")
            | {"payload_sha256": digest},
            event_key=f"g10-07-stale-{uuid.uuid4()}",
        )
        admin.commit()
        service.apply(claim_for(service, stale_event))
        publisher.commit()
        require(
            "G10-07 stale event does not downgrade",
            scalar(admin, "SELECT revision_no FROM public.documents WHERE id=%s", public_id),
            2,
        )

        withdraw_case = cast(
            uuid.UUID,
            scalar(
                admin,
                "SELECT review_case_id FROM audit.document_publication_grants WHERE id=%s",
                revised_grant,
            ),
        )
        withdraw_actor = insert_principal(admin, "document-withdraw")
        withdraw_decision = uuid.uuid4()
        execute(
            admin,
            """
            INSERT INTO audit.review_decisions (
                id, review_case_id, sequence_no, decision, reason, structured_changes,
                decided_by, decided_at
            ) VALUES (%s, %s, 2, 'withdraw', 'WP10.2 withdrawal fixture', '{}'::jsonb, %s, %s)
            """,
            withdraw_decision,
            withdraw_case,
            withdraw_actor,
            datetime.now(UTC),
        )
        execute(
            admin,
            """
            UPDATE audit.document_publication_grants
               SET grant_status='withdrawn', withdrawn_by_decision_id=%s,
                   withdrawn_at=clock_timestamp()
             WHERE id=%s
            """,
            withdraw_decision,
            revised_grant,
        )
        withdrawn_manifest = dict(revised_manifest)
        withdrawn_manifest["decision_id"] = str(withdraw_decision)
        withdraw_event = insert_event(
            admin,
            aggregate_type="document_publication_grants",
            aggregate_id=revised_grant,
            event_type="publication.withdrawn",
            payload=event_payload(withdrawn_manifest, event_type="publication.withdrawn")
            | {"payload_sha256": revised_digest},
        )
        admin.commit()
        service.apply(claim_for(service, withdraw_event))
        publisher.commit()
        require(
            "G10-07 withdraw hides document",
            scalar(admin, "SELECT count(*) FROM public.documents WHERE id=%s", public_id),
            0,
        )
        require(
            "G10-07 identity survives withdraw",
            scalar(
                admin,
                "SELECT count(*) FROM audit.document_public_identities WHERE document_id=%s",
                document_id,
            ),
            1,
        )

        retired_entity_id, retired_entity_grant, _retired_decision, retired_manifest = (
            entity_manifest(admin, "entity-retired-before-publish")
        )
        retired_digest = str(
            scalar(
                admin,
                (
                    "SELECT publication_payload_sha256 "
                    "FROM audit.entity_publication_grants WHERE id=%s"
                ),
                retired_entity_grant,
            )
        )
        retired_event = insert_event(
            admin,
            aggregate_type="entity_publication_grants",
            aggregate_id=retired_entity_grant,
            event_type="publication.granted",
            payload=event_payload(retired_manifest, event_type="publication.granted")
            | {"payload_sha256": retired_digest},
        )
        admin.commit()
        retired_claim = claim_for(service, retired_event)
        execute(
            admin,
            "UPDATE core.entities SET status='retired' WHERE id=%s",
            retired_entity_id,
        )
        admin.commit()
        retired_result = service.apply(retired_claim)
        publisher.commit()
        require("G10-08 retired entity apply is not replay", retired_result.replayed, False)
        require(
            "G10-08 retired entity does not project",
            scalar(
                admin,
                "SELECT count(*) FROM public.entities WHERE entity_grant_id=%s",
                retired_entity_grant,
            ),
            0,
        )
        require(
            "G10-08 retired entity does not create identity",
            scalar(
                admin,
                "SELECT count(*) FROM audit.entity_public_identities WHERE entity_id=%s",
                retired_entity_id,
            ),
            0,
        )

        merged_entity_id, merged_entity_grant, _merged_decision, merged_manifest = entity_manifest(
            admin, "entity-merged-before-publish"
        )
        _merge_target_id, _merge_target_grant, _merge_target_decision, _merge_target_manifest = (
            entity_manifest(admin, "entity-merge-target")
        )
        merge_actor = insert_principal(admin, "entity-merge-before-publish")
        merge_event_id = uuid.uuid4()
        execute(
            admin,
            """
            INSERT INTO core.entity_merge_events (
                id, source_entity_id, target_entity_id, reason, merged_by, merged_at,
                event_kind
            ) VALUES (%s, %s, %s, 'WP10.2 merge-before-publish fixture', %s,
                      clock_timestamp(), 'merge')
            """,
            merge_event_id,
            merged_entity_id,
            _merge_target_id,
            merge_actor,
        )
        execute(
            admin,
            "UPDATE core.entities SET status='merged' WHERE id=%s",
            merged_entity_id,
        )
        merged_digest = str(
            scalar(
                admin,
                (
                    "SELECT publication_payload_sha256 "
                    "FROM audit.entity_publication_grants WHERE id=%s"
                ),
                merged_entity_grant,
            )
        )
        merged_event = insert_event(
            admin,
            aggregate_type="entity_publication_grants",
            aggregate_id=merged_entity_grant,
            event_type="publication.granted",
            payload=event_payload(merged_manifest, event_type="publication.granted")
            | {"payload_sha256": merged_digest},
        )
        admin.commit()
        merged_claim = claim_for(service, merged_event)
        merged_result = service.apply(merged_claim)
        publisher.commit()
        require("G10-08 merged entity apply is not replay", merged_result.replayed, False)
        require(
            "G10-08 merged entity does not project",
            scalar(
                admin,
                "SELECT count(*) FROM public.entities WHERE entity_grant_id=%s",
                merged_entity_grant,
            ),
            0,
        )
        require(
            "G10-08 merged entity does not create identity",
            scalar(
                admin,
                "SELECT count(*) FROM audit.entity_public_identities WHERE entity_id=%s",
                merged_entity_id,
            ),
            0,
        )

        disputed_entity_id, disputed_entity_grant, _disputed_decision, disputed_manifest = (
            entity_manifest(admin, "entity-disputed-before-publish")
        )
        disputed_digest = str(
            scalar(
                admin,
                (
                    "SELECT publication_payload_sha256 "
                    "FROM audit.entity_publication_grants WHERE id=%s"
                ),
                disputed_entity_grant,
            )
        )
        disputed_event = insert_event(
            admin,
            aggregate_type="entity_publication_grants",
            aggregate_id=disputed_entity_grant,
            event_type="publication.granted",
            payload=event_payload(disputed_manifest, event_type="publication.granted")
            | {"payload_sha256": disputed_digest},
        )
        admin.commit()
        disputed_claim = claim_for(service, disputed_event)
        status_lock_ready = Event()
        release_status_lock = Event()
        apply_done = Event()

        def hold_disputed_status_lock() -> None:
            status_connection = psycopg.connect(libpq_url(publisher_url), autocommit=False)
            try:
                with status_connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT 1 FROM core.entities WHERE id=%s FOR UPDATE",
                        (disputed_entity_id,),
                    )
                    cursor.execute(
                        "UPDATE core.entities SET status='disputed' WHERE id=%s",
                        (disputed_entity_id,),
                    )
                status_lock_ready.set()
                if not release_status_lock.wait(5):
                    raise RuntimeError("disputed status lock was not released")
                status_connection.commit()
            finally:
                status_connection.close()

        def apply_disputed_event() -> Any:
            try:
                return service.apply(disputed_claim)
            finally:
                apply_done.set()

        with ThreadPoolExecutor(max_workers=2) as executor:
            status_future = executor.submit(hold_disputed_status_lock)
            if not status_lock_ready.wait(5):
                release_status_lock.set()
                status_future.result(timeout=5)
                raise RuntimeError("disputed status lock was not acquired")
            apply_future = executor.submit(apply_disputed_event)
            apply_waited_for_status = not apply_done.wait(0.2)
            release_status_lock.set()
            status_future.result(timeout=5)
            disputed_result = apply_future.result(timeout=5)
        publisher.commit()
        require("G10-08 disputed apply waits for status lock", apply_waited_for_status, True)
        require("G10-08 disputed entity apply is not replay", disputed_result.replayed, False)
        require(
            "G10-08 disputed entity does not project",
            scalar(
                admin,
                "SELECT count(*) FROM public.entities WHERE entity_grant_id=%s",
                disputed_entity_grant,
            ),
            0,
        )
        require(
            "G10-08 disputed entity does not create identity",
            scalar(
                admin,
                "SELECT count(*) FROM audit.entity_public_identities WHERE entity_id=%s",
                disputed_entity_id,
            ),
            0,
        )
        disputed_event_state = one(
            admin,
            "SELECT published_at, terminal_at, last_error_code FROM ops.outbox_events WHERE id=%s",
            disputed_event,
        )
        require(
            "G10-08 disputed event is acknowledged without publication",
            (disputed_event_state[0] is not None, disputed_event_state[1], disputed_event_state[2]),
            (True, None, None),
        )

        entity_fixture = entity_manifest(admin, "entity-first", canonical_name="Same entity name")
        entity_id, entity_grant, entity_decision, entity = entity_fixture
        entity_digest = str(
            scalar(
                admin,
                (
                    "SELECT publication_payload_sha256 "
                    "FROM audit.entity_publication_grants WHERE id=%s"
                ),
                entity_grant,
            )
        )
        entity_event = insert_event(
            admin,
            aggregate_type="entity_publication_grants",
            aggregate_id=entity_grant,
            event_type="publication.granted",
            payload=event_payload(entity, event_type="publication.granted")
            | {"payload_sha256": entity_digest},
        )
        admin.commit()
        service.apply(claim_for(service, entity_event))
        publisher.commit()
        entity_public_id, entity_slug = one(
            admin,
            "SELECT public_id, slug FROM audit.entity_public_identities WHERE entity_id=%s",
            entity_id,
        )
        require(
            "G10-08 stable entity slug",
            entity_slug,
            f"e-{str(entity_public_id).replace('-', '')}",
        )
        require(
            "G10-08 entity visible",
            scalar(admin, "SELECT count(*) FROM public.entities WHERE id=%s", entity_public_id),
            1,
        )
        other_entity_id, other_grant, _other_decision, other_entity = entity_manifest(
            admin,
            "entity-same-name",
            canonical_name="Same entity name",
        )
        other_digest = str(
            scalar(
                admin,
                (
                    "SELECT publication_payload_sha256 "
                    "FROM audit.entity_publication_grants WHERE id=%s"
                ),
                other_grant,
            )
        )
        other_event = insert_event(
            admin,
            aggregate_type="entity_publication_grants",
            aggregate_id=other_grant,
            event_type="publication.granted",
            payload=event_payload(other_entity, event_type="publication.granted")
            | {"payload_sha256": other_digest},
        )
        admin.commit()
        service.apply(claim_for(service, other_event))
        publisher.commit()
        other_public_id = scalar(
            admin,
            "SELECT public_id FROM audit.entity_public_identities WHERE entity_id=%s",
            other_entity_id,
        )
        require("G10-08 same names use different IDs", other_public_id != entity_public_id, True)

        revised_entity_grant, revised_entity, old_entity_grant = revise_entity_manifest(
            admin, entity_fixture
        )
        revised_entity_digest = str(
            scalar(
                admin,
                (
                    "SELECT publication_payload_sha256 "
                    "FROM audit.entity_publication_grants WHERE id=%s"
                ),
                revised_entity_grant,
            )
        )
        revised_entity["payload_sha256"] = revised_entity_digest
        entity_superseded_event = insert_event(
            admin,
            aggregate_type="entity_publication_grants",
            aggregate_id=old_entity_grant,
            event_type="publication.superseded",
            payload=event_payload(
                revised_entity,
                event_type="publication.superseded",
                old_grant_id=old_entity_grant,
            )
            | {"payload_sha256": revised_entity_digest},
        )
        entity_granted_revision_event = insert_event(
            admin,
            aggregate_type="entity_publication_grants",
            aggregate_id=revised_entity_grant,
            event_type="publication.granted",
            payload=event_payload(revised_entity, event_type="publication.granted")
            | {"payload_sha256": revised_entity_digest},
        )
        admin.commit()
        service.apply(claim_for(service, entity_superseded_event))
        publisher.commit()
        service.apply(claim_for(service, entity_granted_revision_event))
        publisher.commit()
        entity_revision, entity_revision_grant, entity_revision_no = one(
            admin,
            "SELECT id, entity_grant_id, revision_no FROM public.entities WHERE id=%s",
            entity_public_id,
        )
        require("G10-08 stable entity ID on revise", entity_revision, entity_public_id)
        require("G10-08 entity grant on revise", entity_revision_grant, revised_entity_grant)
        require("G10-08 entity revision two", entity_revision_no, 2)

        entity_withdraw_case = cast(
            uuid.UUID,
            scalar(
                admin,
                "SELECT review_case_id FROM audit.entity_publication_grants WHERE id=%s",
                revised_entity_grant,
            ),
        )
        withdraw_actor = insert_principal(admin, "entity-withdraw")
        entity_withdraw_decision = uuid.uuid4()
        execute(
            admin,
            """
            INSERT INTO audit.review_decisions (
                id, review_case_id, sequence_no, decision, reason, structured_changes,
                decided_by, decided_at
            ) VALUES (
                %s, %s, 2, 'withdraw', 'WP10.2 entity withdrawal fixture',
                '{}'::jsonb, %s, %s
            )
            """,
            entity_withdraw_decision,
            entity_withdraw_case,
            withdraw_actor,
            datetime.now(UTC),
        )
        execute(
            admin,
            """
            UPDATE audit.entity_publication_grants
               SET grant_status='withdrawn', withdrawn_by_decision_id=%s,
                   withdrawn_at=clock_timestamp()
             WHERE id=%s
            """,
            entity_withdraw_decision,
            revised_entity_grant,
        )
        withdrawn_entity = dict(revised_entity)
        withdrawn_entity["decision_id"] = str(entity_withdraw_decision)
        entity_withdraw_event = insert_event(
            admin,
            aggregate_type="entity_publication_grants",
            aggregate_id=revised_entity_grant,
            event_type="publication.withdrawn",
            payload=event_payload(withdrawn_entity, event_type="publication.withdrawn")
            | {"payload_sha256": revised_entity_digest},
        )
        admin.commit()
        service.apply(claim_for(service, entity_withdraw_event))
        publisher.commit()
        require(
            "G10-08 withdraw hides entity",
            scalar(admin, "SELECT count(*) FROM public.entities WHERE id=%s", entity_public_id),
            0,
        )
        require(
            "G10-08 identity survives entity withdraw",
            scalar(
                admin,
                "SELECT count(*) FROM audit.entity_public_identities WHERE entity_id=%s",
                entity_id,
            ),
            1,
        )
        _ = first
        _ = entity_decision
    finally:
        publisher.close()


def g10_09(admin: psycopg.Connection[Any], publisher_url: str) -> None:
    tag = uuid.uuid4().hex[:8]
    relation_tables_before = (
        scalar(admin, "SELECT count(*) FROM audit.relation_publication_grants"),
        scalar(admin, "SELECT count(*) FROM core.relations"),
        scalar(admin, "SELECT count(*) FROM public.relations"),
        scalar(admin, "SELECT count(*) FROM public.relation_evidence"),
    )
    worker = connect_role(publisher_url, "uap_worker")
    try:
        g8_result = g8_16c(admin, worker, f"g10-09-{tag}")
        for assertion in (
            "G10-09 normal analysis does not enqueue resolve_relations",
            "G10-09 worker claim set omits resolve_relations",
            "G10-09 misclaim is terminal",
            "G10-09 misclaim uses knowledge_relation_task_not_in_wp8",
            "G10-09 misclaim does not write core.relations",
        ):
            require(assertion, g8_result["passed"], True)
    finally:
        worker.close()

    seed_grantor(admin)
    reviewer = insert_person(admin)
    bind_role(admin, reviewer, "reviewer")
    api = connect_role(publisher_url, "uap_api")
    try:
        g9_08(admin, api, reviewer)
        require("G10-09 relation review rejected", True, True)
        require(
            "G10-09 relation review leaves no case",
            scalar(admin, "SELECT count(*) FROM audit.review_cases WHERE case_type='relation'"),
            0,
        )
    finally:
        api.close()

    event_id = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO ops.outbox_events (
            id, aggregate_type, aggregate_id, event_type, event_key, payload,
            occurred_at, available_at
        ) VALUES (%s, 'relation_publication_grants', %s, 'publication.granted', %s,
                  '{"schema":"publication-outbox.v2","subject_type":"relation"}'::jsonb,
                  clock_timestamp(), clock_timestamp())
        """,
        event_id,
        uuid.uuid4(),
        f"g10-09-publisher-{event_id}",
    )
    admin.commit()
    publisher = connect_role(publisher_url, "uap_publisher")
    try:
        service = PublicationService(publisher, dispatcher_id="g10-09", batch_limit=10)
        claims = service.claim()
        publisher.commit()
        require(
            "G10-09 Publisher does not claim relation",
            any(claim.event_id == event_id for claim in claims),
            False,
        )
        relation_event_state = one(
            admin,
            "SELECT published_at, terminal_at, terminal_error_code, "
            "last_error_code, publish_attempts FROM ops.outbox_events WHERE id=%s",
            event_id,
        )
        require("G10-09 relation remains unpublished", relation_event_state[0], None)
        require("G10-09 relation is terminal", relation_event_state[1] is not None, True)
        require(
            "G10-09 relation terminal state",
            relation_event_state[2:],
            ("publication_event_schema_unsupported", "publication_event_schema_unsupported", 1),
        )
        require(
            "G10-09 relation terminal attempt recorded",
            one(
                admin,
                "SELECT count(*), max(outcome::text), max(sanitized_error_code), "
                "max(sanitized_error_summary) "
                "FROM audit.publication_delivery_attempts WHERE event_id=%s",
                event_id,
            ),
            (
                1,
                "terminal_failure",
                "publication_event_schema_unsupported",
                "publication event schema is unsupported",
            ),
        )
        require(
            "G10-09 relation tables unchanged",
            (
                scalar(admin, "SELECT count(*) FROM audit.relation_publication_grants"),
                scalar(admin, "SELECT count(*) FROM core.relations"),
                scalar(admin, "SELECT count(*) FROM public.relations"),
                scalar(admin, "SELECT count(*) FROM public.relation_evidence"),
            ),
            relation_tables_before,
        )
    finally:
        publisher.close()


def g10_10_tamper_case(
    admin: psycopg.Connection[Any],
    service: PublicationService,
    publisher: psycopg.Connection[Any],
    *,
    aggregate_tamper: bool,
) -> None:
    """Prove a hash/aggregate tamper fails terminally before public writes."""

    tag = "tamper-aggregate" if aggregate_tamper else "tamper-hash"
    document_id, event_id = create_document_event(
        admin,
        tag,
        aggregate_id=uuid.uuid4() if aggregate_tamper else None,
        payload_sha256="f" * 64 if not aggregate_tamper else None,
    )
    claim = claim_for(service, event_id)
    before = publication_state_snapshot(admin, event_id, document_id)
    state, code = error_pair(
        publisher,
        "SELECT * FROM ops.apply_publication_event(%s, %s)",
        event_id,
        claim.lease_token,
    )
    expected_code = (
        "publication_event_aggregate_mismatch"
        if aggregate_tamper
        else "publication_payload_hash_mismatch"
    )
    require(f"G10-10 {tag} SQLSTATE", state, "22023")
    require(f"G10-10 {tag} code", code, expected_code)
    after = publication_state_snapshot(admin, event_id, document_id)
    require(f"G10-10 {tag} state unchanged", after, before)
    PROBE_EVIDENCE.append(
        {"kind": "state", "name": f"G10-10 {tag}", "before": before, "after": after}
    )
    for summary_label, summary in (
        ("summary", "caller supplied summary"),
        ("null-summary", None),
        ("overlong-summary", "x" * 301),
    ):
        invalid_before = publication_state_snapshot(admin, event_id, document_id)
        invalid_summary_state, invalid_summary_code = error_pair(
            publisher,
            "SELECT * FROM ops.fail_publication_event(%s, %s, %s, %s, 0, true)",
            event_id,
            claim.lease_token,
            expected_code,
            summary,
        )
        require(
            f"G10-10 {tag} {summary_label} SQLSTATE",
            invalid_summary_state,
            "22023",
        )
        require(
            f"G10-10 {tag} {summary_label} code",
            invalid_summary_code,
            "publication_failure_parameters_invalid",
        )
        invalid_after = publication_state_snapshot(admin, event_id, document_id)
        require(
            f"G10-10 {tag} {summary_label} has no state change",
            invalid_after,
            invalid_before,
        )
        PROBE_EVIDENCE.append(
            {
                "kind": "state",
                "name": f"G10-10 {tag} {summary_label}",
                "before": invalid_before,
                "after": invalid_after,
            }
        )
    failure = service.fail(claim, error_code=expected_code, terminal=True)
    publisher.commit()
    require(f"G10-10 {tag} terminal outcome", failure.outcome, "terminal")
    require(
        f"G10-10 {tag} remains unpublished",
        scalar(admin, "SELECT published_at FROM ops.outbox_events WHERE id=%s", event_id),
        None,
    )


def g10_10(admin: psycopg.Connection[Any], publisher_url: str) -> None:
    publisher = connect_role(publisher_url, "uap_publisher")
    service = PublicationService(publisher, dispatcher_id="g10-10", lease_seconds=1, batch_limit=1)
    try:
        for point in ("attempt", "identity", "public_upsert", "deferred_constraint", "ack"):
            document_id, _version, grant_id, _decision, manifest = document_manifest(
                admin, f"inject-{point}"
            )
            digest = str(
                scalar(
                    admin,
                    (
                        "SELECT publication_payload_sha256 "
                        "FROM audit.document_publication_grants WHERE id=%s"
                    ),
                    grant_id,
                )
            )
            manifest["payload_sha256"] = digest
            event_id = insert_event(
                admin,
                aggregate_type="document_publication_grants",
                aggregate_id=grant_id,
                event_type="publication.granted",
                payload=event_payload(manifest, event_type="publication.granted")
                | {"payload_sha256": digest},
            )
            admin.commit()
            claim = claim_for(service, event_id)
            injected_before = publication_state_snapshot(admin, event_id, document_id)
            try:
                with publisher.transaction():
                    with publisher.cursor() as cursor:
                        cursor.execute(
                            "SELECT set_config('uap.publisher_fail_at', %s, true)",
                            (point,),
                        )
                        cursor.execute(
                            "SELECT * FROM ops.apply_publication_event(%s, %s)",
                            (event_id, claim.lease_token),
                        )
            except psycopg.Error as error:
                require(f"G10-10 injected {point} SQLSTATE", error.sqlstate, "40001")
                injected_after = publication_state_snapshot(admin, event_id, document_id)
                require(
                    f"G10-10 injected {point} rolls back state",
                    injected_after,
                    injected_before,
                )
                PROBE_EVIDENCE.append(
                    {
                        "kind": "state",
                        "name": f"G10-10 injected {point}",
                        "before": injected_before,
                        "after": injected_after,
                    }
                )
            else:
                raise RuntimeError(f"failure injection did not fire at {point}")
            time.sleep(1.1)
            retry_claim = claim_for(service, event_id)
            service.apply(retry_claim)
            publisher.commit()
            require(
                f"G10-10 event visible after {point}",
                scalar(
                    admin,
                    (
                        "SELECT count(*) FROM public.documents WHERE id=("
                        "SELECT public_id FROM audit.document_public_identities "
                        "WHERE document_id=%s)"
                    ),
                    document_id,
                ),
                1,
            )

        g10_10_tamper_case(admin, service, publisher, aggregate_tamper=False)
        g10_10_tamper_case(admin, service, publisher, aggregate_tamper=True)

        retry_document_id, retry_event_id = create_document_event(admin, "old-token")
        old_claim = claim_for(service, retry_event_id)
        retry_result = service.fail(
            old_claim,
            error_code="publication_dependency_not_ready",
            retry_delay_seconds=0,
        )
        publisher.commit()
        require("G10-10 retry outcome", retry_result.outcome, "retry_wait")
        time.sleep(1.1)
        new_claim = claim_for(service, retry_event_id)
        old_token_apply_before = publication_state_snapshot(
            admin, retry_event_id, retry_document_id
        )
        old_apply_state, old_apply_code = error_pair(
            publisher,
            "SELECT * FROM ops.apply_publication_event(%s, %s)",
            retry_event_id,
            old_claim.lease_token,
        )
        require("G10-10 old token after new attempt SQLSTATE", old_apply_state, "40001")
        require("G10-10 old token after new attempt code", old_apply_code, "publication_lease_lost")
        require(
            "G10-10 expired apply token has no state change",
            publication_state_snapshot(admin, retry_event_id, retry_document_id),
            old_token_apply_before,
        )
        old_token_fail_before = publication_state_snapshot(admin, retry_event_id, retry_document_id)
        old_fail_state, old_fail_code = error_pair(
            publisher,
            (
                "SELECT * FROM ops.fail_publication_event("
                "%s, %s, 'publication_dependency_not_ready', "
                "'expired token summary', 0, false)"
            ),
            retry_event_id,
            old_claim.lease_token,
        )
        require("G10-10 old fail token SQLSTATE", old_fail_state, "40001")
        require("G10-10 old fail token code", old_fail_code, "publication_lease_lost")
        require(
            "G10-10 expired fail token has no state change",
            publication_state_snapshot(admin, retry_event_id, retry_document_id),
            old_token_fail_before,
        )
        service.apply(new_claim)
        publisher.commit()
        require(
            "G10-10 new attempt applies",
            scalar(
                admin,
                (
                    "SELECT count(*) FROM public.documents WHERE id=("
                    "SELECT public_id FROM audit.document_public_identities "
                    "WHERE document_id=%s)"
                ),
                retry_document_id,
            ),
            1,
        )

        # Same-token replay and conflict are checked after a committed apply.
        _document_id, _version, grant_id, _decision, manifest = document_manifest(admin, "replay")
        digest = str(
            scalar(
                admin,
                (
                    "SELECT publication_payload_sha256 "
                    "FROM audit.document_publication_grants WHERE id=%s"
                ),
                grant_id,
            )
        )
        manifest["payload_sha256"] = digest
        event_id = insert_event(
            admin,
            aggregate_type="document_publication_grants",
            aggregate_id=grant_id,
            event_type="publication.granted",
            payload=event_payload(manifest, event_type="publication.granted")
            | {"payload_sha256": digest},
        )
        admin.commit()
        claim = claim_for(service, event_id)
        first_apply = service.apply(claim)
        publisher.commit()
        replay_before = (
            scalar(
                admin,
                "SELECT count(*) FROM audit.document_public_identities WHERE document_id=%s",
                _document_id,
            ),
            scalar(
                admin,
                "SELECT count(*) FROM public.documents WHERE id=(SELECT public_id FROM "
                "audit.document_public_identities WHERE document_id=%s)",
                _document_id,
            ),
            scalar(
                admin,
                "SELECT count(*) FROM audit.publication_delivery_attempts WHERE event_id=%s",
                event_id,
            ),
            scalar(admin, "SELECT published_at FROM ops.outbox_events WHERE id=%s", event_id),
        )
        replay = service.apply(claim)
        publisher.commit()
        require("G10-10 same-token replay", replay.replayed, True)
        require("G10-10 replay returns same attempt", replay.attempt_no, first_apply.attempt_no)
        require(
            "G10-10 replay returns same digest",
            replay.projection_digest,
            first_apply.projection_digest,
        )
        replay_after = (
            scalar(
                admin,
                "SELECT count(*) FROM audit.document_public_identities WHERE document_id=%s",
                _document_id,
            ),
            scalar(
                admin,
                "SELECT count(*) FROM public.documents WHERE id=(SELECT public_id FROM "
                "audit.document_public_identities WHERE document_id=%s)",
                _document_id,
            ),
            scalar(
                admin,
                "SELECT count(*) FROM audit.publication_delivery_attempts WHERE event_id=%s",
                event_id,
            ),
            scalar(admin, "SELECT published_at FROM ops.outbox_events WHERE id=%s", event_id),
        )
        require("G10-10 replay has no writes", replay_after, replay_before)
        conflict_before = publication_state_snapshot(admin, event_id, _document_id)
        state, code = error_pair(
            publisher,
            (
                "SELECT * FROM ops.fail_publication_event("
                "%s, %s, 'publication_dependency_not_ready', "
                "'publication dependency is not ready', 0, false)"
            ),
            event_id,
            claim.lease_token,
        )
        require("G10-10 operation conflict SQLSTATE", state, "40001")
        require("G10-10 operation conflict code", code, "publication_delivery_attempt_conflict")
        conflict_after = publication_state_snapshot(admin, event_id, _document_id)
        require("G10-10 operation conflict has no state change", conflict_after, conflict_before)
        PROBE_EVIDENCE.append(
            {
                "kind": "state",
                "name": "G10-10 operation conflict",
                "before": conflict_before,
                "after": conflict_after,
            }
        )
    finally:
        publisher.close()


def run(database_url: str) -> None:
    with psycopg.connect(libpq_url(database_url), autocommit=True) as admin:
        publisher_url = database_url
        for _role, password_env in ROLE_PASSWORDS.items():
            if not os.environ.get(password_env):
                raise RuntimeError(f"missing {password_env}")
        require(
            "G10-06 publisher-only claim",
            scalar(
                admin,
                (
                    "SELECT has_function_privilege('uap_publisher', "
                    "'ops.claim_publication_outbox(text,integer,integer)', 'EXECUTE')"
                ),
            ),
            True,
        )
        require(
            "G10-06 api cannot claim",
            scalar(
                admin,
                (
                    "SELECT has_function_privilege('uap_api', "
                    "'ops.claim_publication_outbox(text,integer,integer)', 'EXECUTE')"
                ),
            ),
            False,
        )
        for role in ("uap_api", "uap_worker", "uap_public_reader"):
            role_connection = connect_role(publisher_url, role)
            role_connection.close()
        for role in ("uap_api", "uap_worker", "uap_scheduler", "uap_public_reader"):
            require(
                f"G10-06 {role} cannot use Publisher functions",
                scalar(
                    admin,
                    "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                    role,
                    "ops.apply_publication_event(uuid,uuid)",
                ),
                False,
            )
        for signature in (
            "ops.ack_outbox(uuid,uuid)",
            "audit.record_review_decision(uuid,audit.review_decision,text,jsonb)",
            "core.merge_entities(uuid,uuid,uuid,text)",
            "core.reverse_entity_merge(uuid,uuid,text)",
        ):
            require(
                f"G10-06 publisher cannot execute {signature}",
                scalar(
                    admin,
                    "SELECT has_function_privilege('uap_publisher', %s, 'EXECUTE')",
                    signature,
                ),
                False,
            )
        g10_06(admin, publisher_url)
        g10_07_08(admin, publisher_url)
        g10_09(admin, publisher_url)
        g10_10(admin, publisher_url)
    print("G10-06 G10-07 G10-08 G10-09 G10-10 runtime probe passed")


def write_evidence(path: Path, status: str) -> None:
    """Write redacted probe evidence suitable for attaching to the review."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "wp10.2-runtime-evidence.v1",
                "status": status,
                "checks": PROBE_EVIDENCE,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.environ.get("UAP_DATABASE_URL"))
    parser.add_argument("--evidence-out", type=Path)
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("--database-url or UAP_DATABASE_URL is required")
    try:
        run(args.database_url)
    except Exception:
        if args.evidence_out:
            write_evidence(args.evidence_out, "failed")
        raise
    if args.evidence_out:
        write_evidence(args.evidence_out, "passed")


if __name__ == "__main__":
    main()
