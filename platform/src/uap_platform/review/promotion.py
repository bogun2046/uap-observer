"""WP9.4 selection and candidate promotion clients. Authority stays in the database."""

from __future__ import annotations

import uuid
from typing import Any

from psycopg import Connection
from psycopg.errors import Error as PsycopgError

from .errors import ReviewSessionError, map_review_error


def select_analysis_result(
    connection: Connection[Any],
    analysis_result_id: uuid.UUID,
    reason: str,
) -> uuid.UUID:
    """Call audit.select_analysis_result. Acting principal comes from GUC only."""

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT audit.select_analysis_result(%s, %s)",
                (analysis_result_id, reason),
            )
            row = cursor.fetchone()
    except PsycopgError as error:
        raise map_review_error(error) from error
    if row is None or row[0] is None:
        raise ReviewSessionError("review_unclassified", "XX000")
    return uuid.UUID(str(row[0]))


def accept_entity_candidate(
    connection: Connection[Any],
    candidate_id: uuid.UUID,
    reason: str,
) -> uuid.UUID:
    """Call audit.accept_entity_candidate. Acting principal comes from GUC only."""

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT audit.accept_entity_candidate(%s, %s)",
                (candidate_id, reason),
            )
            row = cursor.fetchone()
    except PsycopgError as error:
        raise map_review_error(error) from error
    if row is None or row[0] is None:
        raise ReviewSessionError("review_unclassified", "XX000")
    return uuid.UUID(str(row[0]))


def bind_entity_candidate(
    connection: Connection[Any],
    candidate_id: uuid.UUID,
    entity_id: uuid.UUID,
    reason: str,
) -> uuid.UUID:
    """Call audit.bind_entity_candidate. Acting principal comes from GUC only."""

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT audit.bind_entity_candidate(%s, %s, %s)",
                (candidate_id, entity_id, reason),
            )
            row = cursor.fetchone()
    except PsycopgError as error:
        raise map_review_error(error) from error
    if row is None or row[0] is None:
        raise ReviewSessionError("review_unclassified", "XX000")
    return uuid.UUID(str(row[0]))
