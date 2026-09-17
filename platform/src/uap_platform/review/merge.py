"""WP9.5 authorized entity merge clients. Authority stays in the database."""

from __future__ import annotations

import uuid
from typing import Any

from psycopg import Connection
from psycopg.errors import Error as PsycopgError

from .errors import ReviewSessionError, map_review_error


def apply_entity_merge(
    connection: Connection[Any],
    source_entity_id: uuid.UUID,
    target_entity_id: uuid.UUID,
    reason: str,
) -> uuid.UUID:
    """Call audit.apply_entity_merge. Acting principal comes from GUC only."""

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT audit.apply_entity_merge(%s, %s, %s)",
                (source_entity_id, target_entity_id, reason),
            )
            row = cursor.fetchone()
    except PsycopgError as error:
        raise map_review_error(error) from error
    if row is None or row[0] is None:
        raise ReviewSessionError("review_unclassified", "XX000")
    return uuid.UUID(str(row[0]))


def apply_entity_merge_reverse(
    connection: Connection[Any],
    merge_event_id: uuid.UUID,
    reason: str,
) -> uuid.UUID:
    """Call audit.apply_entity_merge_reverse. Acting principal comes from GUC only."""

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT audit.apply_entity_merge_reverse(%s, %s)",
                (merge_event_id, reason),
            )
            row = cursor.fetchone()
    except PsycopgError as error:
        raise map_review_error(error) from error
    if row is None or row[0] is None:
        raise ReviewSessionError("review_unclassified", "XX000")
    return uuid.UUID(str(row[0]))
