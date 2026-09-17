"""WP9.2 review case write client. Authority stays in the database."""

from __future__ import annotations

import uuid
from typing import Any

from psycopg import Connection
from psycopg.errors import Error as PsycopgError

from .errors import ReviewSessionError, map_review_error


def open_review_case(
    connection: Connection[Any],
    case_type: str,
    subject_id: uuid.UUID,
    priority: int,
    reason: str,
) -> uuid.UUID:
    """Call audit.open_review_case. Acting principal comes from GUC only."""

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT audit.open_review_case(
                    %s::audit.review_case_type, %s, %s::smallint, %s
                )
                """,
                (case_type, subject_id, priority, reason),
            )
            row = cursor.fetchone()
    except PsycopgError as error:
        raise map_review_error(error) from error
    if row is None or row[0] is None:
        raise ReviewSessionError("review_unclassified", "XX000")
    return uuid.UUID(str(row[0]))


def assign_review_case(
    connection: Connection[Any],
    case_id: uuid.UUID,
    assignee_id: uuid.UUID,
) -> uuid.UUID:
    """Call audit.assign_review_case."""

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT audit.assign_review_case(%s, %s)",
                (case_id, assignee_id),
            )
            row = cursor.fetchone()
    except PsycopgError as error:
        raise map_review_error(error) from error
    if row is None or row[0] is None:
        raise ReviewSessionError("review_unclassified", "XX000")
    return uuid.UUID(str(row[0]))


def close_review_case(
    connection: Connection[Any],
    case_id: uuid.UUID,
    reason: str,
) -> uuid.UUID:
    """Call audit.close_review_case."""

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT audit.close_review_case(%s, %s)",
                (case_id, reason),
            )
            row = cursor.fetchone()
    except PsycopgError as error:
        raise map_review_error(error) from error
    if row is None or row[0] is None:
        raise ReviewSessionError("review_unclassified", "XX000")
    return uuid.UUID(str(row[0]))
