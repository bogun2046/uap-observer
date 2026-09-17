"""Stable Admin HTTP error mapping. Unknown DB messages collapse to api_internal_error."""

from __future__ import annotations

from typing import Final

from psycopg.errors import Error as PsycopgError

from uap_platform.review.errors import FROZEN_REVIEW_CODES, map_review_error

STATUS_BY_CODE: Final[dict[str, int]] = {
    "api_auth_required": 401,
    "api_token_invalid": 401,  # nosec B105  # error-code key, not a credential
    "api_principal_not_provisioned": 403,
    "api_request_invalid": 422,
    "review_request_id_missing": 400,
    "review_request_id_invalid": 400,
    "api_cursor_invalid": 400,
    "api_resource_not_found": 404,
    "api_capability_closed": 404,
    "api_rate_limited": 429,
    "api_internal_error": 500,
    "api_dependency_unavailable": 503,
    "editorial_document_not_found": 404,
    "editorial_document_version_mismatch": 409,
    "editorial_revision_conflict": 409,
    "editorial_idempotency_conflict": 409,
    "editorial_document_trashed": 409,
    "editorial_document_already_trashed": 409,
    "editorial_document_not_trashed": 409,
    "editorial_ai_result_invalid": 422,
    "editorial_ai_result_foreign": 422,
    "editorial_adopt_field_invalid": 422,
    "editorial_reanalysis_invalid": 422,
    "article_model_call_budget_exhausted": 429,
    "monthly_model_budget_exhausted": 429,
    "review_principal_missing": 403,
    "review_service_principal_denied": 403,
    "review_session_role_denied": 403,
    "review_role_denied": 403,
    "review_scope_unsupported": 403,
    "review_self_review_denied": 403,
    "review_assignee_mismatch": 403,
    "knowledge_merge_principal_required": 403,
    "knowledge_merge_principal_inactive": 403,
    "review_subject_missing": 404,
    "review_case_missing": 404,
    "review_selection_missing": 404,
    "review_candidate_missing": 404,
    "review_entity_missing": 404,
    "knowledge_merge_missing_entity": 404,
    "knowledge_merge_missing_event": 404,
    "review_idempotency_payload_conflict": 409,
    "review_case_already_open": 409,
    "review_case_not_assignable": 409,
    "review_case_already_closed": 409,
    "review_case_not_decidable": 409,
    "review_decision_not_allowed": 409,
    "review_grant_not_active": 409,
    "review_grant_already_active": 409,
    "review_candidate_not_pending": 409,
    "review_subject_already_bound": 409,
    "review_bind_target_not_active": 409,
    "review_bind_target_not_canonical": 409,
    "review_subject_not_active": 409,
    "review_subject_not_canonical": 409,
    "knowledge_merge_same_entity": 409,
    "knowledge_merge_not_active": 409,
    "knowledge_merge_not_canonical": 409,
    "knowledge_merge_cycle": 409,
    "knowledge_merge_chain_too_long": 409,
    "knowledge_merge_not_merge_event": 409,
    "knowledge_merge_already_reversed": 409,
    "publication_document_grant_required": 409,
    "publication_event_not_terminal": 409,
    "review_reason_too_short": 422,
    "review_structured_changes_unsupported": 422,
    "review_assignee_invalid": 422,
    "review_selection_type_unsupported": 422,
    "review_selection_not_valid": 422,
    "review_candidate_evidence_missing": 422,
    "review_candidate_origin_invalid": 422,
    "manual_claim_requires_supports": 422,
    "review_ai_evidence_immutable": 422,
    "review_decision_not_in_transaction": 422,
    "knowledge_merge_reason_required": 422,
    "publication_manifest_invalid": 422,
    "publication_source_url_missing": 422,
    "publication_evidence_required": 422,
    "publication_evidence_excerpt_too_long": 422,
    "publication_replay_reason_too_short": 422,
}

SAFE_DETAIL: Final[dict[str, str | None]] = {
    "api_auth_required": "Authentication is required.",
    "api_token_invalid": "Token is invalid.",  # nosec B105  # error-code copy, not a credential
    "api_principal_not_provisioned": "Principal is not provisioned.",
    "api_request_invalid": "Request is invalid.",
    "review_request_id_missing": "Idempotency-Key is required.",
    "review_request_id_invalid": "Idempotency-Key is invalid.",
    "api_cursor_invalid": "Cursor is invalid.",
    "api_resource_not_found": "Resource not found.",
    "api_capability_closed": "Capability is closed.",
    "api_rate_limited": "Rate limit exceeded.",
    "api_internal_error": None,
    "api_dependency_unavailable": "Service dependency is unavailable.",
    "editorial_document_not_found": "Document not found.",
    "editorial_document_version_mismatch": "Document version does not belong to this document.",
    "editorial_revision_conflict": "Editorial revision is stale.",
    "editorial_idempotency_conflict": "Request was already used with a different payload.",
    "editorial_document_trashed": "Trashed documents cannot be edited or reanalyzed.",
    "editorial_document_already_trashed": "Document is already in the recycle bin.",
    "editorial_document_not_trashed": "Document is not in the recycle bin.",
    "editorial_ai_result_invalid": "AI result is not valid for adoption.",
    "editorial_ai_result_foreign": "AI result belongs to another document version.",
    "editorial_adopt_field_invalid": "Adopted fields do not match the AI result.",
    "editorial_reanalysis_invalid": "Reanalysis request is invalid.",
    "article_model_call_budget_exhausted": "Per-document model call budget is exhausted.",
    "monthly_model_budget_exhausted": "Monthly model budget is exhausted.",
}

PUBLIC_CODE_REWRITE: Final[dict[str, str]] = {
    "knowledge_relation_review_not_in_wp9": "api_capability_closed",
    "review_unclassified": "api_internal_error",
    "knowledge_merge_reverse_copy_failed": "api_internal_error",
}

KNOWN_PRIMARY_CODES: Final[frozenset[str]] = (
    frozenset(STATUS_BY_CODE) | frozenset(PUBLIC_CODE_REWRITE) | FROZEN_REVIEW_CODES
)


class AdminError(ValueError):
    def __init__(self, code: str, status: int | None = None, detail: str | None = None) -> None:
        public_code = PUBLIC_CODE_REWRITE.get(code, code)
        if public_code not in STATUS_BY_CODE:
            public_code = "api_internal_error"
        super().__init__(public_code)
        self.code = public_code
        self.status = status if status is not None else STATUS_BY_CODE[public_code]
        self.detail = SAFE_DETAIL.get(public_code, SAFE_DETAIL["api_request_invalid"])
        if detail is not None and public_code != "api_internal_error":
            self.detail = detail


def map_database_error(error: PsycopgError) -> AdminError:
    primary = ""
    if error.diag is not None and error.diag.message_primary:
        primary = error.diag.message_primary
    if primary in PUBLIC_CODE_REWRITE or primary in STATUS_BY_CODE:
        return AdminError(primary)
    mapped = map_review_error(error)
    if mapped.code in PUBLIC_CODE_REWRITE or mapped.code in STATUS_BY_CODE:
        return AdminError(mapped.code)
    return AdminError("api_internal_error")
