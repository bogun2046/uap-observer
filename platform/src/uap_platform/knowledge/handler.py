"""Production resolve_claims handler: map -> materialize -> finish in one transaction."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, cast

from psycopg import Connection
from psycopg.errors import Error as PsycopgError
from psycopg.types.json import Jsonb

from uap_platform.object_registry import ObjectClient, read_verified_object

from .anchors import object_consistent
from .bundle import (
    build_knowledge_bundle,
    is_empty_valid,
    is_terminal_mapping,
    terminal_error_code,
)
from .contracts import (
    ExtractionRecord,
    LocatorType,
    MappingReport,
    SourceCandidate,
    SourceLocator,
)
from .mapping import map_knowledge_result
from .metrics import build_attempt_metrics, failure_metrics
from .payload import FrozenKnowledgePayload, KnowledgePayloadError, parse_knowledge_payload
from .reasons import KNOWLEDGE_PAYLOAD_MISMATCH

LOGGER = logging.getLogger(__name__)

_DETERMINISTIC_SQLSTATES = frozenset({"22023", "23514", "23505", "23503"})
_PYTHON_FAIL_TYPES = (KnowledgePayloadError, KeyError, ValueError, LookupError, TypeError)
_LOCATOR_TYPES: frozenset[str] = frozenset({"text", "html", "pdf", "video", "audio"})
_FROZEN_ERROR_TOKENS = (
    "knowledge_payload_mismatch",
    "knowledge_bundle_mismatch",
    "knowledge_schema_unsupported",
    "knowledge_extraction_missing",
    "knowledge_extraction_ambiguous",
    "knowledge_extraction_mismatch",
    "knowledge_locator_hash_conflict",
    "knowledge_locator_unmappable",
    "locator_cross_axis_mismatch",
    "locator_excerpt_too_large",
    "locator_out_of_range",
    "locator_axis_conflict",
    "locator_duplicate",
    "locator_location_map_invalid",
)


class ResolveClaimsHandler:
    """Claim one resolve_claims attempt through mapping and DB materialization."""

    def __init__(
        self, connection: Connection[object], object_client: ObjectClient
    ) -> None:
        self._connection = connection
        self._object_client = object_client

    def handle(
        self,
        job_id: uuid.UUID,
        job_attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        payload: Mapping[str, object] | object,
    ) -> str:
        """Run mapping + materialize + finish. Always closes the attempt except 40001."""

        try:
            parsed = parse_knowledge_payload(payload)
        except KnowledgePayloadError as error:
            return self._finish_unmapped_failure(
                job_id, job_attempt_id, lease_token, error.code
            )

        try:
            return self._handle_in_transaction(
                job_id, job_attempt_id, lease_token, parsed
            )
        except PsycopgError as error:
            if error.sqlstate == "40001":
                self._connection.rollback()
                LOGGER.warning(
                    "resolve_claims lease serialization failure job=%s attempt=%s",
                    job_id,
                    job_attempt_id,
                )
                raise
            self._connection.rollback()
            code = _error_code_from_exception(error)
            if error.sqlstate in _DETERMINISTIC_SQLSTATES:
                return self._finish_unmapped_failure(
                    job_id, job_attempt_id, lease_token, code
                )
            return self._finish_unmapped_failure(
                job_id, job_attempt_id, lease_token, KNOWLEDGE_PAYLOAD_MISMATCH
            )
        except _PYTHON_FAIL_TYPES as error:
            self._connection.rollback()
            code = (
                error.code
                if isinstance(error, KnowledgePayloadError)
                else KNOWLEDGE_PAYLOAD_MISMATCH
            )
            return self._finish_unmapped_failure(
                job_id, job_attempt_id, lease_token, code
            )
        except Exception:
            self._connection.rollback()
            LOGGER.exception(
                "unclassified resolve_claims failure job=%s attempt=%s",
                job_id,
                job_attempt_id,
            )
            return self._finish_unmapped_failure(
                job_id, job_attempt_id, lease_token, KNOWLEDGE_PAYLOAD_MISMATCH
            )

    def _handle_in_transaction(
        self,
        job_id: uuid.UUID,
        job_attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        parsed: FrozenKnowledgePayload,
    ) -> str:
        claims, text_body, location_map, extraction_rows = self._load_mapping_inputs(
            parsed
        )
        candidates = _source_candidates_from_claims(claims)
        report = map_knowledge_result(
            candidates=candidates,
            payload_anchor=parsed.anchor,
            records=extraction_rows,
            document_version_id=parsed.document_version_id,
            input_sha256=parsed.input_sha256,
            extracted_text=text_body,
            location_map=location_map,
            duplicate_policy="claim",
        )

        if is_terminal_mapping(report):
            return self._finish_terminal(
                job_id,
                job_attempt_id,
                lease_token,
                report,
                error_code=terminal_error_code(report),
            )

        bundle = build_knowledge_bundle(
            report,
            analysis_result_id=parsed.analysis_result_id,
            analysis_result_sha256=parsed.analysis_result_sha256,
        )

        with self._connection.cursor() as cursor:
            cursor.execute("SAVEPOINT knowledge_materialize")
            try:
                cursor.execute(
                    """
                    SELECT core.materialize_claim_bundle(%s, %s, %s, %s)
                    """,
                    (job_id, job_attempt_id, lease_token, Jsonb(bundle)),
                )
                receipt_row = cast(tuple[Any, ...] | None, cursor.fetchone())
                receipt = cast(dict[str, Any], receipt_row[0]) if receipt_row else {}
                materialized_candidates = int(receipt.get("materialized_candidates", 0))
                materialized_locators = int(receipt.get("materialized_locators", 0))
                empty_valid = bool(receipt.get("empty_valid_result", False)) or is_empty_valid(
                    report
                )
                metrics = build_attempt_metrics(
                    report,
                    materialized_candidates=materialized_candidates,
                    materialized_locators=materialized_locators,
                    empty_valid_result=empty_valid,
                )
                cursor.execute(
                    """
                    SELECT ops.finish_knowledge_job(
                        %s, %s, %s, 'succeeded'::ops.attempt_outcome,
                        NULL, NULL, NULL, NULL, %s
                    )
                    """,
                    (job_id, job_attempt_id, lease_token, Jsonb(metrics)),
                )
                status_row = cast(tuple[Any, ...] | None, cursor.fetchone())
                self._connection.commit()
                return str(status_row[0]) if status_row else "succeeded"
            except PsycopgError as error:
                if error.sqlstate == "40001":
                    self._connection.rollback()
                    raise
                if error.sqlstate in _DETERMINISTIC_SQLSTATES:
                    cursor.execute("ROLLBACK TO SAVEPOINT knowledge_materialize")
                    error_code = _error_code_from_exception(error)
                    metrics = failure_metrics(error_code)
                    cursor.execute(
                        """
                        SELECT ops.finish_knowledge_job(
                            %s, %s, %s, 'terminal_failure'::ops.attempt_outcome,
                            NULL, %s, %s, NULL, %s
                        )
                        """,
                        (
                            job_id,
                            job_attempt_id,
                            lease_token,
                            error_code,
                            error_code,
                            Jsonb(metrics),
                        ),
                    )
                    status_row = cast(tuple[Any, ...] | None, cursor.fetchone())
                    self._connection.commit()
                    return str(status_row[0]) if status_row else "failed"
                raise
        raise RuntimeError("resolve_claims materialize path did not finish")

    def _finish_terminal(
        self,
        job_id: uuid.UUID,
        job_attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        report: MappingReport,
        *,
        error_code: str,
    ) -> str:
        metrics = build_attempt_metrics(
            report,
            materialized_candidates=0,
            materialized_locators=0,
            empty_valid_result=False,
        )
        return self._call_finish(
            job_id, job_attempt_id, lease_token, error_code, metrics
        )

    def _finish_unmapped_failure(
        self,
        job_id: uuid.UUID,
        job_attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        error_code: str,
    ) -> str:
        """Close a running attempt after a Python or unclassified failure. Never succeeded."""

        try:
            self._connection.rollback()
        except Exception:
            LOGGER.debug(
                "rollback before unmapped finish failed job=%s attempt=%s",
                job_id,
                job_attempt_id,
                exc_info=True,
            )
        try:
            return self._call_finish(
                job_id,
                job_attempt_id,
                lease_token,
                error_code,
                failure_metrics(error_code),
            )
        except PsycopgError as error:
            if error.sqlstate == "40001":
                self._connection.rollback()
                raise
            self._connection.rollback()
            raise

    def _call_finish(
        self,
        job_id: uuid.UUID,
        job_attempt_id: uuid.UUID,
        lease_token: uuid.UUID,
        error_code: str,
        metrics: Mapping[str, Any],
    ) -> str:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ops.finish_knowledge_job(
                    %s, %s, %s, 'terminal_failure'::ops.attempt_outcome,
                    NULL, %s, %s, NULL, %s
                )
                """,
                (
                    job_id,
                    job_attempt_id,
                    lease_token,
                    error_code,
                    error_code,
                    Jsonb(dict(metrics)),
                ),
            )
            status_row = cast(tuple[Any, ...] | None, cursor.fetchone())
        self._connection.commit()
        return str(status_row[0]) if status_row else "failed"

    def _load_mapping_inputs(
        self,
        parsed: FrozenKnowledgePayload,
    ) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]], list[ExtractionRecord]]:
        # Worker has no SELECT on ops.model_runs (G8-05). Hash/run checks stay in
        # SECURITY DEFINER materialize/finish. Python only reads analysis_results.
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT analysis.result
                  FROM core.analysis_results AS analysis
                 WHERE analysis.id = %s
                   AND analysis.document_version_id = %s
                   AND analysis.model_run_id = %s
                   AND analysis.result_sha256 = %s
                """,
                (
                    parsed.analysis_result_id,
                    parsed.document_version_id,
                    parsed.model_run_id,
                    parsed.analysis_result_sha256,
                ),
            )
            row = cast(tuple[Any, ...] | None, cursor.fetchone())
            if row is None:
                raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
            result_map = cast(Mapping[str, Any], row[0] or {})
            claims = list(cast(Sequence[dict[str, Any]], result_map.get("claims") or []))
            cursor.execute(
                """
                SELECT e.id, e.document_version_id, e.outcome::text, e.output_sha256,
                       e.extractor_name, e.extractor_version,
                       coalesce(so.storage_domain::text, ''),
                       coalesce(so.content_sha256, ''),
                       e.location_map
                  FROM core.extractions AS e
                  LEFT JOIN core.stored_objects AS so
                    ON so.id = e.text_object_id
                 WHERE e.document_version_id = %s
                   AND e.outcome = 'succeeded'
                """,
                (parsed.document_version_id,),
            )
            extraction_rows_raw = cast(list[tuple[Any, ...]], cursor.fetchall())

        records: list[ExtractionRecord] = []
        location_map: list[dict[str, Any]] = []
        named_extraction = parsed.anchor.extraction_id
        for row in extraction_rows_raw:
            record = ExtractionRecord(
                extraction_id=uuid.UUID(str(row[0])),
                document_version_id=uuid.UUID(str(row[1])),
                outcome=str(row[2]),
                output_sha256=str(row[3]),
                stored_domain=str(row[6]),
                stored_sha256=str(row[7]),
                extractor_name=str(row[4] or ""),
                extractor_version=str(row[5] or ""),
            )
            records.append(record)
            if (
                named_extraction is not None
                and record.extraction_id == named_extraction
                and object_consistent(record)
            ):
                loc_map = row[8]
                if isinstance(loc_map, list):
                    location_map = [dict(item) for item in loc_map]
                elif loc_map is not None and not isinstance(loc_map, (str, bytes)):
                    location_map = [dict(item) for item in loc_map]

        text_body = ""
        if named_extraction is not None and claims:
            text_body = self._load_derived_text(
                extraction_id=named_extraction,
                document_version_id=parsed.document_version_id,
                input_sha256=parsed.input_sha256,
            )
        return claims, text_body, location_map, records

    def _load_derived_text(
        self,
        *,
        extraction_id: uuid.UUID,
        document_version_id: uuid.UUID,
        input_sha256: str,
    ) -> str:
        """Read the frozen derived object and verify its content hash."""

        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT stored.bucket_name, stored.object_key,
                       stored.content_sha256, stored.byte_length
                  FROM core.extractions AS extraction
                  JOIN core.stored_objects AS stored
                    ON stored.id = extraction.text_object_id
                   AND stored.storage_domain = 'derived'
                 WHERE extraction.id = %s
                   AND extraction.document_version_id = %s
                   AND extraction.outcome = 'succeeded'
                   AND extraction.output_sha256 = %s
                   AND stored.content_sha256 = %s
                """,
                (extraction_id, document_version_id, input_sha256, input_sha256),
            )
            row = cast(tuple[Any, ...] | None, cursor.fetchone())
        if row is None:
            raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
        try:
            data = read_verified_object(
                self._object_client,
                str(row[0]),
                str(row[1]),
                str(row[2]),
                int(row[3]),
            )
            return data.decode("utf-8")
        except (RuntimeError, UnicodeDecodeError, LookupError) as error:
            raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH) from error

def _optional_int(item: Mapping[str, Any], key: str) -> int | None:
    value = item.get(key)
    if value is None:
        return None
    return int(value)


def _source_candidates_from_claims(
    claims: Sequence[Mapping[str, Any]],
) -> tuple[SourceCandidate, ...]:
    candidates: list[SourceCandidate] = []
    for ordinal, claim in enumerate(claims):
        evidence = claim.get("evidence") or []
        if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
            raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
        locators: list[SourceLocator] = []
        for item in evidence:
            if not isinstance(item, Mapping):
                raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
            locator_type = item.get("locator_type")
            if locator_type not in _LOCATOR_TYPES:
                raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
            locators.append(
                SourceLocator(
                    locator_type=cast(LocatorType, locator_type),
                    start=int(item["start"]),
                    end=int(item["end"]),
                    page_start=_optional_int(item, "page_start"),
                    page_end=_optional_int(item, "page_end"),
                    time_start_ms=_optional_int(item, "time_start_ms"),
                    time_end_ms=_optional_int(item, "time_end_ms"),
                )
            )
        candidates.append(SourceCandidate(ordinal=ordinal, locators=tuple(locators)))
    return tuple(candidates)


def _error_code_from_exception(error: BaseException) -> str:
    message = str(error)
    for code in _FROZEN_ERROR_TOKENS:
        if code in message:
            return code
    return KNOWLEDGE_PAYLOAD_MISMATCH


def payload_from_claim(claim: tuple[Any, ...]) -> Mapping[str, object]:
    """Return the JSON payload column from the WP4 claim_job tuple."""

    if len(claim) < 5 or not isinstance(claim[3], Mapping):
        raise KnowledgePayloadError(KNOWLEDGE_PAYLOAD_MISMATCH)
    return claim[3]
