"""WP9.3 review decision client. Authority stays in the database."""

from __future__ import annotations

import uuid
from typing import Any

from psycopg import Connection
from psycopg.errors import Error as PsycopgError
from psycopg.types.json import Jsonb

from .errors import ReviewSessionError, map_review_error


def record_review_decision(
    connection: Connection[Any],
    case_id: uuid.UUID,
    decision: str,
    reason: str,
    structured_changes: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Call audit.record_review_decision. Acting principal comes from GUC only."""

    payload = {} if structured_changes is None else structured_changes
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT audit.record_review_decision(
                    %s, %s::audit.review_decision, %s, %s::jsonb
                )
                """,
                (case_id, decision, reason, Jsonb(payload)),
            )
            row = cursor.fetchone()
    except PsycopgError as error:
        raise map_review_error(error) from error
    if row is None or row[0] is None:
        raise ReviewSessionError("review_unclassified", "XX000")
    return uuid.UUID(str(row[0]))
