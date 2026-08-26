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
REVIEW_CASE_ALREADY_OPEN: Final = "review_case_already_open"
REVIEW_CASE_NOT_DECIDABLE: Final = "review_case_not_decidable"
REVIEW_IDEMPOTENCY_PAYLOAD_CONFLICT: Final = "review_idempotency_payload_conflict"
KNOWLEDGE_RELATION_REVIEW_NOT_IN_WP9: Final = "knowledge_relation_review_not_in_wp9"
REVIEW_REASON_TOO_SHORT: Final = "review_reason_too_short"
REVIEW_CASE_MISSING: Final = "review_case_missing"
REVIEW_ASSIGNEE_INVALID: Final = "review_assignee_invalid"
REVIEW_CASE_NOT_ASSIGNABLE: Final = "review_case_not_assignable"
REVIEW_CASE_ALREADY_CLOSED: Final = "review_case_already_closed"
REVIEW_SELF_REVIEW_DENIED: Final = "review_self_review_denied"
REVIEW_STRUCTURED_CHANGES_UNSUPPORTED: Final = "review_structured_changes_unsupported"
REVIEW_GRANT_NOT_ACTIVE: Final = "review_grant_not_active"
REVIEW_GRANT_ALREADY_ACTIVE: Final = "review_grant_already_active"
REVIEW_DECISION_NOT_ALLOWED: Final = "review_decision_not_allowed"
REVIEW_ASSIGNEE_MISMATCH: Final = "review_assignee_mismatch"
REVIEW_GRANT_SUPERSEDED_BLOCKS_DOWNGRADE: Final = (
    "review_grant_superseded_blocks_downgrade"
)

FROZEN_REVIEW_CODES: Final[frozenset[str]] = frozenset(
    {
        REVIEW_PRINCIPAL_MISSING,
        REVIEW_SERVICE_PRINCIPAL_DENIED,
        REVIEW_SESSION_ROLE_DENIED,
        REVIEW_ROLE_DENIED,
        REVIEW_SCOPE_UNSUPPORTED,
        REVIEW_REQUEST_ID_MISSING,
        REVIEW_CASE_ALREADY_OPEN,
        REVIEW_CASE_NOT_DECIDABLE,
        REVIEW_IDEMPOTENCY_PAYLOAD_CONFLICT,
        KNOWLEDGE_RELATION_REVIEW_NOT_IN_WP9,
        REVIEW_REASON_TOO_SHORT,
        REVIEW_CASE_MISSING,
        REVIEW_ASSIGNEE_INVALID,
        REVIEW_CASE_NOT_ASSIGNABLE,
        REVIEW_CASE_ALREADY_CLOSED,
        REVIEW_SELF_REVIEW_DENIED,
        REVIEW_STRUCTURED_CHANGES_UNSUPPORTED,
        REVIEW_GRANT_NOT_ACTIVE,
        REVIEW_GRANT_ALREADY_ACTIVE,
        REVIEW_DECISION_NOT_ALLOWED,
        REVIEW_ASSIGNEE_MISMATCH,
        REVIEW_GRANT_SUPERSEDED_BLOCKS_DOWNGRADE,
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
    if sqlstate == "23505":
        return ReviewSessionError(REVIEW_IDEMPOTENCY_PAYLOAD_CONFLICT, sqlstate)
    return ReviewSessionError("review_unclassified", sqlstate or "XX000")
