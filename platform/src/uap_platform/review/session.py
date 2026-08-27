"""Transaction-local review session GUC and require_active_role client."""

from __future__ import annotations

import uuid
from typing import Any

from psycopg import Connection
from psycopg.errors import Error as PsycopgError

from .errors import ReviewSessionError, map_review_error


def bind_review_session(
    connection: Connection[Any],
    principal_id: uuid.UUID,
    request_id: uuid.UUID | None = None,
) -> None:
    """SET LOCAL acting principal (and optional request id). Never pass as SQL args to RBAC."""

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('uap.principal_id', %s, true)",
            (str(principal_id),),
        )
        if request_id is not None:
            cursor.execute(
                "SELECT set_config('uap.request_id', %s, true)",
                (str(request_id),),
            )


def require_active_role(connection: Connection[Any], role: str) -> uuid.UUID:
    """Call audit.require_active_role. Role name is the only SQL argument."""

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT audit.require_active_role(%s::audit.application_role)",
                (role,),
            )
            row = cursor.fetchone()
    except PsycopgError as error:
        mapped = map_review_error(error)
        raise mapped from error
    if row is None or row[0] is None:
        raise ReviewSessionError("review_principal_missing", "42501")
    return uuid.UUID(str(row[0]))
