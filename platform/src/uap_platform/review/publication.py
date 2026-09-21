"""V1-3.1 publication review submission client.

The database SECURITY DEFINER wrapper is the authority for publication review
eligibility and the editorial-admin boundary.  This module intentionally does
not create grants, manifests, or outbox events.
"""

from __future__ import annotations

import uuid
from typing import Any

from psycopg import Connection
from psycopg.errors import Error as PsycopgError

from .errors import ReviewSessionError, map_review_error


def open_document_publication_review_case(
    connection: Connection[Any],
    document_version_id: uuid.UUID,
    editorial_revision_id: uuid.UUID,
    priority: int,
    reason: str,
) -> uuid.UUID:
    """Submit one immutable Editorial revision for publication review."""

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT audit.open_document_publication_review_case(
                    %s::uuid, %s::uuid, %s::smallint, %s
                )
                """,
                (document_version_id, editorial_revision_id, priority, reason),
            )
            row = cursor.fetchone()
    except PsycopgError as error:
        raise map_review_error(error) from error
    if row is None or row[0] is None:
        raise ReviewSessionError("review_unclassified", "XX000")
    return uuid.UUID(str(row[0]))
