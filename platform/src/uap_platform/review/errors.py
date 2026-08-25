"""Stable review-session error codes. Logs may only use these tokens."""

from __future__ import annotations

from typing import Final

from psycopg.errors import Error as PsycopgError

REVIEW_PRINCIPAL_MISSING: Final = "review_principal_missing"
REVIEW_SERVICE_PRINCIPAL_DENIED: Final = "review_service_principal_denied"
REVIEW_SESSION_ROLE_DENIED: Final = "review_session_role_denied"
REVIEW_ROLE_DENIED: Final = "review_role_denied"
REVIEW_SCOPE_UNSUPPORTED: Final = "review_scope_unsupported"
REVIEW_REQUEST_ID_MISSING: Final = "review_request_id_missing"

FROZEN_REVIEW_CODES: Final[frozenset[str]] = frozenset(
    {
        REVIEW_PRINCIPAL_MISSING,
        REVIEW_SERVICE_PRINCIPAL_DENIED,
        REVIEW_SESSION_ROLE_DENIED,
        REVIEW_ROLE_DENIED,
        REVIEW_SCOPE_UNSUPPORTED,
        REVIEW_REQUEST_ID_MISSING,
    }
)


class ReviewSessionError(Exception):
    """Mapped database denial for review session binding."""

    def __init__(self, code: str, sqlstate: str) -> None:
        super().__init__(code)
        self.code = code
        self.sqlstate = sqlstate


def map_review_error(error: PsycopgError) -> ReviewSessionError:
    """Map a Psycopg error onto a frozen review session code."""

    primary = ""
    if error.diag is not None and error.diag.message_primary:
        primary = error.diag.message_primary
    sqlstate = str(error.sqlstate or "")
    if primary in FROZEN_REVIEW_CODES:
        return ReviewSessionError(primary, sqlstate or "42501")
    if sqlstate == "42501":
        return ReviewSessionError(REVIEW_SESSION_ROLE_DENIED, sqlstate)
    return ReviewSessionError("review_unclassified", sqlstate or "XX000")
