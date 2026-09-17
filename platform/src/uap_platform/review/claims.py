"""WP9.6 manual claim client. Authority stays in the database."""

from __future__ import annotations

import uuid
from typing import Any

from psycopg import Connection
from psycopg.errors import Error as PsycopgError

from .errors import ReviewSessionError, map_review_error


def create_manual_claim(
    connection: Connection[Any],
    document_version_id: uuid.UUID,
    claim_text: str,
    claim_type: str,
    assertion_status: str,
    attribution: str | None,
    span_ids: list[uuid.UUID],
) -> uuid.UUID:
    """Call audit.create_manual_claim. Acting principal comes from GUC only."""

    try:
        with connection.cursor() as cursor:
            cursor.execute(
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
            row = cursor.fetchone()
    except PsycopgError as error:
        raise map_review_error(error) from error
    if row is None or row[0] is None:
        raise ReviewSessionError("review_unclassified", "XX000")
    return uuid.UUID(str(row[0]))
