"""Claim materialization write authority for resolve_claims.

Revision ID: 0011_claim_materialization
Revises: 0010_knowledge_foundation
Create Date: 2026-08-23
"""

from __future__ import annotations

from alembic import op

revision = "0011_claim_materialization"
down_revision = "0010_knowledge_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE OR REPLACE FUNCTION ops.validate_knowledge_attempt_metrics(
            p_metrics jsonb,
            p_outcome ops.attempt_outcome
        ) RETURNS void
        LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path = ops, pg_catalog
        AS $validate_knowledge_attempt_metrics$
        DECLARE
            allowed_keys text[] := ARRAY[
                'schema_version', 'input_candidates', 'materialized_candidates',
                'input_locators', 'materialized_locators', 'rejected_candidates',
                'rejected_locators', 'empty_valid_result', 'rejected_by_code', 'samples'
            ];
            allowed_codes text[] := ARRAY[
                'locator_end_not_after_start', 'locator_out_of_range',
                'locator_axis_conflict', 'locator_pdf_page_missing',
                'locator_time_missing', 'locator_page_range_invalid',
                'locator_time_range_invalid', 'locator_location_map_invalid',
                'locator_cross_axis_mismatch', 'locator_excerpt_too_large',
                'locator_duplicate', 'knowledge_extraction_missing',
                'knowledge_extraction_ambiguous', 'knowledge_extraction_mismatch',
                'knowledge_locator_unmappable', 'knowledge_invalid_origin',
                'knowledge_schema_unsupported', 'knowledge_payload_mismatch',
                'knowledge_bundle_mismatch', 'knowledge_locator_hash_conflict'
            ];
            unknown_key text;
            input_candidates integer;
            materialized_candidates integer;
            input_locators integer;
            materialized_locators integer;
            rejected_candidates integer;
            rejected_locators integer;
            empty_valid boolean;
            code_sum integer;
            sample jsonb;
            sample_key text;
            code_value jsonb;
        BEGIN
            IF p_metrics IS NULL OR jsonb_typeof(p_metrics) <> 'object' THEN
                RAISE EXCEPTION 'knowledge attempt metrics must be an object'
                    USING ERRCODE = '22023';
            END IF;
            IF pg_column_size(p_metrics) > 65536 THEN
                RAISE EXCEPTION 'knowledge attempt metrics exceed 64KiB'
                    USING ERRCODE = '22023';
            END IF;
            SELECT key INTO unknown_key
              FROM jsonb_object_keys(p_metrics) AS key
             WHERE key <> ALL (allowed_keys)
             LIMIT 1;
            IF unknown_key IS NOT NULL THEN
                RAISE EXCEPTION 'knowledge attempt metrics contain unknown keys'
                    USING ERRCODE = '22023';
            END IF;
            IF p_metrics ->> 'schema_version' <> 'knowledge-attempt-metrics.v1' THEN
                RAISE EXCEPTION 'knowledge attempt metrics schema is unsupported'
                    USING ERRCODE = '22023';
            END IF;
            IF jsonb_typeof(p_metrics -> 'empty_valid_result') <> 'boolean' THEN
                RAISE EXCEPTION 'empty_valid_result must be boolean'
                    USING ERRCODE = '22023';
            END IF;
            empty_valid := (p_metrics ->> 'empty_valid_result')::boolean;
            BEGIN
                input_candidates := (p_metrics ->> 'input_candidates')::integer;
                materialized_candidates := (p_metrics ->> 'materialized_candidates')::integer;
                input_locators := (p_metrics ->> 'input_locators')::integer;
                materialized_locators := (p_metrics ->> 'materialized_locators')::integer;
                rejected_candidates := (p_metrics ->> 'rejected_candidates')::integer;
                rejected_locators := (p_metrics ->> 'rejected_locators')::integer;
            EXCEPTION
                WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'knowledge attempt metrics counts are required'
                        USING ERRCODE = '22023';
            END;
            IF input_candidates IS NULL OR materialized_candidates IS NULL
               OR input_locators IS NULL OR materialized_locators IS NULL
               OR rejected_candidates IS NULL OR rejected_locators IS NULL THEN
                RAISE EXCEPTION 'knowledge attempt metrics counts are required'
                    USING ERRCODE = '22023';
            END IF;
            IF input_candidates < 0 OR materialized_candidates < 0
               OR input_locators < 0 OR materialized_locators < 0
               OR rejected_candidates < 0 OR rejected_locators < 0 THEN
                RAISE EXCEPTION 'knowledge attempt metrics counts cannot be negative'
                    USING ERRCODE = '22023';
            END IF;
            IF materialized_candidates + rejected_candidates <> input_candidates
               OR materialized_locators + rejected_locators <> input_locators THEN
                RAISE EXCEPTION 'knowledge attempt metrics counts are inconsistent'
                    USING ERRCODE = '22023';
            END IF;
            IF jsonb_typeof(p_metrics -> 'rejected_by_code') IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'rejected_by_code must be an object'
                    USING ERRCODE = '22023';
            END IF;
            SELECT key INTO unknown_key
              FROM jsonb_object_keys(p_metrics -> 'rejected_by_code') AS key
             WHERE key <> ALL (allowed_codes)
             LIMIT 1;
            IF unknown_key IS NOT NULL THEN
                RAISE EXCEPTION 'rejected_by_code contains an unknown reason'
                    USING ERRCODE = '22023';
            END IF;
            FOR code_value IN
                SELECT value FROM jsonb_each(p_metrics -> 'rejected_by_code')
            LOOP
                IF jsonb_typeof(code_value) <> 'number'
                   OR trunc((code_value #>> '{}')::numeric) <> (code_value #>> '{}')::numeric
                   OR (code_value #>> '{}')::integer < 0 THEN
                    RAISE EXCEPTION 'rejected_by_code counts must be non-negative integers'
                        USING ERRCODE = '22023';
                END IF;
            END LOOP;
            SELECT coalesce(sum((value)::integer), 0) INTO code_sum
              FROM jsonb_each_text(p_metrics -> 'rejected_by_code');
            IF code_sum <> rejected_locators THEN
                RAISE EXCEPTION 'rejected_by_code does not match rejected locators'
                    USING ERRCODE = '22023';
            END IF;
            IF jsonb_typeof(p_metrics -> 'samples') IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION 'samples must be an array'
                    USING ERRCODE = '22023';
            END IF;
            IF jsonb_array_length(p_metrics -> 'samples') > 50 THEN
                RAISE EXCEPTION 'samples exceed the frozen maximum'
                    USING ERRCODE = '22023';
            END IF;
            FOR sample IN SELECT value FROM jsonb_array_elements(p_metrics -> 'samples')
            LOOP
                IF jsonb_typeof(sample) <> 'object' THEN
                    RAISE EXCEPTION 'sample rows must be objects'
                        USING ERRCODE = '22023';
                END IF;
                SELECT key INTO sample_key
                  FROM jsonb_object_keys(sample) AS key
                 WHERE key NOT IN ('candidate_ordinal', 'locator_ordinal', 'reason_code')
                 LIMIT 1;
                IF sample_key IS NOT NULL THEN
                    RAISE EXCEPTION 'sample rows contain unknown keys'
                        USING ERRCODE = '22023';
                END IF;
                IF coalesce(sample ->> 'reason_code', '') <> ALL (allowed_codes) THEN
                    RAISE EXCEPTION 'sample reason_code is not frozen'
                        USING ERRCODE = '22023';
                END IF;
                IF jsonb_typeof(sample -> 'candidate_ordinal') IS DISTINCT FROM 'number'
                   OR jsonb_typeof(sample -> 'locator_ordinal') IS DISTINCT FROM 'number' THEN
                    RAISE EXCEPTION 'sample ordinals must be numbers'
                        USING ERRCODE = '22023';
                END IF;
            END LOOP;
            IF empty_valid THEN
                IF p_outcome <> 'succeeded'
                   OR input_candidates <> 0 OR materialized_candidates <> 0 THEN
                    RAISE EXCEPTION 'empty valid metrics require a zero success'
                        USING ERRCODE = '22023';
                END IF;
            END IF;
            IF p_outcome = 'succeeded'
               AND NOT (materialized_candidates > 0 OR empty_valid) THEN
                RAISE EXCEPTION 'successful knowledge metrics require materialization or empty valid'
                    USING ERRCODE = '22023';
            END IF;
            IF p_outcome IN ('terminal_failure', 'retryable_failure')
               AND materialized_candidates <> 0 THEN
                RAISE EXCEPTION 'failed knowledge metrics cannot report materialization'
                    USING ERRCODE = '22023';
            END IF;
        END
        $validate_knowledge_attempt_metrics$;

        CREATE FUNCTION core._claim_source_locator_json(p_src jsonb) RETURNS jsonb
        LANGUAGE plpgsql IMMUTABLE STRICT
        SET search_path = core, pg_catalog
        AS $_claim_source_locator_json$
        DECLARE
            result jsonb;
        BEGIN
            IF p_src IS NULL OR jsonb_typeof(p_src) <> 'object' THEN
                RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
            END IF;
            IF p_src ->> 'locator_type' IS NULL
               OR p_src ->> 'start' IS NULL
               OR p_src ->> 'end' IS NULL THEN
                RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
            END IF;
            result := jsonb_build_object(
                'locator_type', p_src ->> 'locator_type',
                'start', (p_src ->> 'start')::integer,
                'end', (p_src ->> 'end')::integer
            );
            IF p_src ? 'page_start' AND p_src ->> 'page_start' IS NOT NULL THEN
                result := result || jsonb_build_object(
                    'page_start', (p_src ->> 'page_start')::integer
                );
            END IF;
            IF p_src ? 'page_end' AND p_src ->> 'page_end' IS NOT NULL THEN
                result := result || jsonb_build_object(
                    'page_end', (p_src ->> 'page_end')::integer
                );
            END IF;
            IF p_src ? 'time_start_ms' AND p_src ->> 'time_start_ms' IS NOT NULL THEN
                result := result || jsonb_build_object(
                    'time_start_ms', (p_src ->> 'time_start_ms')::bigint
                );
            END IF;
            IF p_src ? 'time_end_ms' AND p_src ->> 'time_end_ms' IS NOT NULL THEN
                result := result || jsonb_build_object(
                    'time_end_ms', (p_src ->> 'time_end_ms')::bigint
                );
            END IF;
            RETURN result;
        END
        $_claim_source_locator_json$;

        CREATE FUNCTION core._claim_cross_axis_ok(
            p_locator_type text,
            p_start integer,
            p_end integer,
            p_page_start integer,
            p_page_end integer,
            p_time_start_ms bigint,
            p_time_end_ms bigint,
            p_location_map jsonb
        ) RETURNS boolean
        LANGUAGE plpgsql STABLE
        SET search_path = core, pg_catalog
        AS $_claim_cross_axis_ok$
        DECLARE
            row_json jsonb;
            row_idx integer := 0;
            c_set integer[] := ARRAY[]::integer[];
            a_set integer[] := ARRAY[]::integer[];
            kind text;
            r_char_start integer;
            r_char_end integer;
            r_page_start integer;
            r_page_end integer;
            r_time_start bigint;
            r_time_end bigint;
        BEGIN
            IF p_locator_type IN ('text', 'html') THEN
                RETURN TRUE;
            END IF;
            IF p_location_map IS NULL OR jsonb_typeof(p_location_map) <> 'array' THEN
                RETURN FALSE;
            END IF;
            FOR row_json IN SELECT value FROM jsonb_array_elements(p_location_map)
            LOOP
                IF jsonb_typeof(row_json) <> 'object' THEN
                    RETURN FALSE;
                END IF;
                kind := row_json ->> 'kind';
                r_char_start := NULLIF(row_json ->> 'char_start', '')::integer;
                r_char_end := NULLIF(row_json ->> 'char_end', '')::integer;
                IF r_char_start IS NULL OR r_char_end IS NULL
                   OR r_char_start >= r_char_end THEN
                    RETURN FALSE;
                END IF;
                IF p_locator_type = 'pdf' THEN
                    IF kind IS DISTINCT FROM 'pdf_page' THEN
                        row_idx := row_idx + 1;
                        CONTINUE;
                    END IF;
                    r_page_start := NULLIF(row_json ->> 'page_start', '')::integer;
                    r_page_end := NULLIF(row_json ->> 'page_end', '')::integer;
                    IF r_page_start IS NULL OR r_page_end IS NULL
                       OR r_page_start > r_page_end THEN
                        RETURN FALSE;
                    END IF;
                    IF p_start < r_char_end AND r_char_start < p_end THEN
                        c_set := array_append(c_set, row_idx);
                    END IF;
                    IF p_page_start IS NOT NULL AND p_page_end IS NOT NULL
                       AND p_page_start <= r_page_end AND r_page_start <= p_page_end THEN
                        a_set := array_append(a_set, row_idx);
                    END IF;
                ELSIF p_locator_type IN ('audio', 'video') THEN
                    IF kind IS DISTINCT FROM 'subtitle_cue' THEN
                        row_idx := row_idx + 1;
                        CONTINUE;
                    END IF;
                    r_time_start := NULLIF(row_json ->> 'time_start_ms', '')::bigint;
                    r_time_end := NULLIF(row_json ->> 'time_end_ms', '')::bigint;
                    IF r_time_start IS NULL OR r_time_end IS NULL
                       OR r_time_start >= r_time_end THEN
                        RETURN FALSE;
                    END IF;
                    IF p_start < r_char_end AND r_char_start < p_end THEN
                        c_set := array_append(c_set, row_idx);
                    END IF;
                    IF p_time_start_ms IS NOT NULL AND p_time_end_ms IS NOT NULL
                       AND p_time_start_ms < r_time_end AND r_time_start < p_time_end_ms THEN
                        a_set := array_append(a_set, row_idx);
                    END IF;
                ELSE
                    RETURN FALSE;
                END IF;
                row_idx := row_idx + 1;
            END LOOP;
            IF coalesce(array_length(c_set, 1), 0) = 0 THEN
                RETURN FALSE;
            END IF;
            RETURN c_set = a_set;
        END
        $_claim_cross_axis_ok$;

        CREATE FUNCTION core._jsonb_keys_exact(p_obj jsonb, p_keys text[]) RETURNS boolean
        LANGUAGE sql IMMUTABLE
        SET search_path = pg_catalog
        AS $_jsonb_keys_exact$
            SELECT p_obj IS NOT NULL
               AND jsonb_typeof(p_obj) = 'object'
               AND NOT EXISTS (
                    SELECT 1
                      FROM jsonb_object_keys(p_obj) AS object_keys(key)
                     WHERE object_keys.key <> ALL (p_keys)
               )
               AND NOT EXISTS (
                    SELECT 1
                      FROM unnest(p_keys) AS required_keys(key)
                     WHERE NOT (p_obj ? required_keys.key)
               );
        $_jsonb_keys_exact$;
        """
    )
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION core.materialize_claim_bundle(
            p_job_id uuid,
            p_attempt_id uuid,
            p_lease_token uuid,
            p_bundle jsonb
        ) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = core, ops, pg_catalog
        AS $materialize_claim_bundle$
        DECLARE
            payload jsonb;
            analysis core.analysis_results%ROWTYPE;
            model_run ops.model_runs%ROWTYPE;
            extraction core.extractions%ROWTYPE;
            claims_json jsonb;
            claim_count integer;
            accepted jsonb;
            rejected jsonb;
            acc_item jsonb;
            rej_item jsonb;
            loc_item jsonb;
            src_claim jsonb;
            src_loc jsonb;
            claim_ordinal integer;
            locator_ordinal integer;
            claim_text text;
            fingerprint text;
            existing_claim core.claims%ROWTYPE;
            v_claim_id uuid;
            existing_span core.evidence_spans%ROWTYPE;
            v_span_id uuid;
            envelope jsonb;
            locator_sha text;
            evidence_text text;
            locator_type text;
            char_start integer;
            char_end integer;
            page_start integer;
            page_end integer;
            time_start_ms bigint;
            time_end_ms bigint;
            axes_char_start integer;
            axes_char_end integer;
            axes_page_start integer;
            axes_page_end integer;
            axes_time_start bigint;
            axes_time_end bigint;
            doc_version_id uuid;
            extraction_id uuid;
            input_sha text;
            seen_claim_ordinals integer[] := ARRAY[]::integer[];
            seen_loc_ordinals integer[];
            source_loc_count integer;
            materialized_candidates integer := 0;
            materialized_locators integer := 0;
            evidence_bytes integer;
            location_map jsonb;
            src_start integer;
            src_end integer;
        BEGIN
            IF session_user <> 'uap_worker' THEN
                RAISE EXCEPTION 'only worker may materialize claim bundles'
                    USING ERRCODE = '42501';
            END IF;

            payload := ops.require_active_resolution_job_lease(
                p_job_id, p_attempt_id, p_lease_token, 'resolve_claims'
            );

            IF payload ->> 'payload_schema_version' IS DISTINCT FROM 'knowledge.v2' THEN
                RAISE EXCEPTION 'knowledge_schema_unsupported' USING ERRCODE = '22023';
            END IF;

            IF p_bundle IS NULL OR jsonb_typeof(p_bundle) <> 'object' THEN
                RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
            END IF;
            IF NOT core._jsonb_keys_exact(
                p_bundle,
                ARRAY[
                    'accepted_candidates', 'analysis_result_id',
                    'analysis_result_sha256', 'bundle_schema_version',
                    'rejected_candidates'
                ]
            ) THEN
                RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
            END IF;
            IF p_bundle ->> 'bundle_schema_version' IS DISTINCT FROM 'knowledge-bundle.v2' THEN
                RAISE EXCEPTION 'knowledge_schema_unsupported' USING ERRCODE = '22023';
            END IF;
            IF (p_bundle ->> 'analysis_result_id') IS DISTINCT FROM (payload ->> 'analysis_result_id')
               OR (p_bundle ->> 'analysis_result_sha256')
                  IS DISTINCT FROM (payload ->> 'analysis_result_sha256') THEN
                RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
            END IF;

            SELECT * INTO analysis
              FROM core.analysis_results
             WHERE id = (payload ->> 'analysis_result_id')::uuid
             FOR SHARE;
            IF analysis.id IS NULL THEN
                RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
            END IF;

            SELECT * INTO model_run
              FROM ops.model_runs
             WHERE id = analysis.model_run_id
             FOR SHARE;
            IF model_run.id IS NULL THEN
                RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
            END IF;

            IF analysis.document_version_id::text IS DISTINCT FROM payload ->> 'document_version_id'
               OR analysis.result_type::text IS DISTINCT FROM payload ->> 'result_type'
               OR analysis.result_type::text IS DISTINCT FROM 'claim_extraction'
               OR analysis.model_run_id::text IS DISTINCT FROM payload ->> 'model_run_id'
               OR analysis.result_sha256 IS DISTINCT FROM payload ->> 'analysis_result_sha256'
               OR analysis.schema_version IS DISTINCT FROM payload ->> 'analysis_schema_version'
               OR analysis.schema_version IS DISTINCT FROM 'ai.v1'
               OR analysis.validation_status IS DISTINCT FROM 'valid'::core.validation_status
               OR model_run.status IS DISTINCT FROM 'succeeded'::ops.model_run_status
               OR model_run.input_sha256 IS DISTINCT FROM payload ->> 'input_sha256'
               OR model_run.document_version_id IS DISTINCT FROM analysis.document_version_id
               OR model_run.task_type::text IS DISTINCT FROM 'claim_extraction' THEN
                RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
            END IF;

            doc_version_id := analysis.document_version_id;
            input_sha := model_run.input_sha256;
            claims_json := analysis.result -> 'claims';
            IF claims_json IS NULL OR jsonb_typeof(claims_json) <> 'array' THEN
                RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
            END IF;
            claim_count := jsonb_array_length(claims_json);

            accepted := p_bundle -> 'accepted_candidates';
            rejected := p_bundle -> 'rejected_candidates';
            IF jsonb_typeof(accepted) <> 'array' OR jsonb_typeof(rejected) <> 'array' THEN
                RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
            END IF;

            IF claim_count = 0 THEN
                IF jsonb_array_length(accepted) <> 0 OR jsonb_array_length(rejected) <> 0 THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;
                RETURN jsonb_build_object(
                    'materialized_candidates', 0,
                    'materialized_locators', 0,
                    'empty_valid_result', true
                );
            END IF;

            IF payload ->> 'extraction_anchor_status' IS DISTINCT FROM 'matched'
               OR payload ->> 'extraction_id' IS NULL THEN
                IF payload ->> 'extraction_anchor_status' = 'missing' THEN
                    RAISE EXCEPTION 'knowledge_extraction_missing' USING ERRCODE = '22023';
                ELSIF payload ->> 'extraction_anchor_status' = 'ambiguous' THEN
                    RAISE EXCEPTION 'knowledge_extraction_ambiguous' USING ERRCODE = '22023';
                ELSE
                    RAISE EXCEPTION 'knowledge_extraction_mismatch' USING ERRCODE = '22023';
                END IF;
            END IF;
            extraction_id := (payload ->> 'extraction_id')::uuid;

            SELECT * INTO extraction
              FROM core.extractions
             WHERE id = extraction_id
               AND document_version_id = doc_version_id
               AND outcome = 'succeeded'
               AND output_sha256 = input_sha
             FOR SHARE;
            IF extraction.id IS NULL THEN
                RAISE EXCEPTION 'knowledge_extraction_mismatch' USING ERRCODE = '22023';
            END IF;
            location_map := extraction.location_map;

            FOR claim_ordinal IN 0 .. claim_count - 1 LOOP
                IF NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(accepted) AS a(value)
                     WHERE (a.value ->> 'ordinal')::integer = claim_ordinal
                ) AND NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(rejected) AS r(value)
                     WHERE (r.value ->> 'ordinal')::integer = claim_ordinal
                ) THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;
            END LOOP;

            FOR rej_item IN SELECT value FROM jsonb_array_elements(rejected)
            LOOP
                IF NOT core._jsonb_keys_exact(
                    rej_item, ARRAY['ordinal', 'reason_code', 'rejected_locators']
                ) THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;
                claim_ordinal := (rej_item ->> 'ordinal')::integer;
                IF claim_ordinal IS NULL OR claim_ordinal < 0 OR claim_ordinal >= claim_count THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;
                IF claim_ordinal = ANY (seen_claim_ordinals) THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;
                seen_claim_ordinals := array_append(seen_claim_ordinals, claim_ordinal);
                IF rej_item ->> 'reason_code' IS NULL THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;
                src_claim := claims_json -> claim_ordinal;
                source_loc_count := jsonb_array_length(
                    coalesce(src_claim -> 'evidence', '[]'::jsonb)
                );
                seen_loc_ordinals := ARRAY[]::integer[];
                FOR loc_item IN
                    SELECT value FROM jsonb_array_elements(rej_item -> 'rejected_locators')
                LOOP
                    IF NOT core._jsonb_keys_exact(
                        loc_item, ARRAY['locator_ordinal', 'reason_code']
                    ) THEN
                        RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                    END IF;
                    locator_ordinal := (loc_item ->> 'locator_ordinal')::integer;
                    IF locator_ordinal IS NULL OR locator_ordinal < 0
                       OR locator_ordinal >= source_loc_count
                       OR locator_ordinal = ANY (seen_loc_ordinals) THEN
                        RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                    END IF;
                    seen_loc_ordinals := array_append(seen_loc_ordinals, locator_ordinal);
                END LOOP;
                IF coalesce(array_length(seen_loc_ordinals, 1), 0) <> source_loc_count THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;
            END LOOP;

            FOR acc_item IN SELECT value FROM jsonb_array_elements(accepted)
            LOOP
                IF NOT core._jsonb_keys_exact(
                    acc_item,
                    ARRAY['accepted_locators', 'ordinal', 'rejected_locators']
                ) THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;
                claim_ordinal := (acc_item ->> 'ordinal')::integer;
                IF claim_ordinal IS NULL OR claim_ordinal < 0 OR claim_ordinal >= claim_count THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;
                IF claim_ordinal = ANY (seen_claim_ordinals) THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;
                seen_claim_ordinals := array_append(seen_claim_ordinals, claim_ordinal);

                src_claim := claims_json -> claim_ordinal;
                claim_text := src_claim ->> 'claim';
                IF claim_text IS NULL THEN
                    RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
                END IF;
                fingerprint := core.compute_claim_fingerprint(claim_text);
                source_loc_count := jsonb_array_length(
                    coalesce(src_claim -> 'evidence', '[]'::jsonb)
                );

                IF jsonb_typeof(acc_item -> 'accepted_locators') <> 'array'
                   OR jsonb_array_length(acc_item -> 'accepted_locators') < 1 THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;

                seen_loc_ordinals := ARRAY[]::integer[];
                FOR loc_item IN
                    SELECT value FROM jsonb_array_elements(acc_item -> 'accepted_locators')
                LOOP
                    IF NOT core._jsonb_keys_exact(
                        loc_item,
                        ARRAY[
                            'char_end', 'char_start', 'evidence_text', 'locator_ordinal',
                            'page_end', 'page_start', 'time_end_ms', 'time_start_ms'
                        ]
                    ) THEN
                        RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                    END IF;
                    locator_ordinal := (loc_item ->> 'locator_ordinal')::integer;
                    IF locator_ordinal IS NULL OR locator_ordinal < 0
                       OR locator_ordinal >= source_loc_count
                       OR locator_ordinal = ANY (seen_loc_ordinals) THEN
                        RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                    END IF;
                    seen_loc_ordinals := array_append(seen_loc_ordinals, locator_ordinal);
                END LOOP;
                FOR loc_item IN
                    SELECT value FROM jsonb_array_elements(acc_item -> 'rejected_locators')
                LOOP
                    IF NOT core._jsonb_keys_exact(
                        loc_item, ARRAY['locator_ordinal', 'reason_code']
                    ) THEN
                        RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                    END IF;
                    locator_ordinal := (loc_item ->> 'locator_ordinal')::integer;
                    IF locator_ordinal IS NULL OR locator_ordinal < 0
                       OR locator_ordinal >= source_loc_count
                       OR locator_ordinal = ANY (seen_loc_ordinals) THEN
                        RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                    END IF;
                    seen_loc_ordinals := array_append(seen_loc_ordinals, locator_ordinal);
                END LOOP;
                IF coalesce(array_length(seen_loc_ordinals, 1), 0) <> source_loc_count THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;

                SELECT * INTO existing_claim
                  FROM core.claims
                 WHERE origin_analysis_result_id = analysis.id
                   AND ordinal = claim_ordinal
                 FOR UPDATE;
                IF existing_claim.id IS NOT NULL THEN
                    IF existing_claim.claim_text IS DISTINCT FROM claim_text
                       OR existing_claim.claim_fingerprint IS DISTINCT FROM fingerprint
                       OR existing_claim.claim_type::text IS DISTINCT FROM 'other'
                       OR existing_claim.assertion_status::text IS DISTINCT FROM 'reported'
                       OR existing_claim.subject_entity_id IS NOT NULL
                       OR existing_claim.document_version_id IS DISTINCT FROM doc_version_id THEN
                        RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
                    END IF;
                    v_claim_id := existing_claim.id;
                ELSE
                    v_claim_id := gen_random_uuid();
                    INSERT INTO core.claims (
                        id, origin_analysis_result_id, subject_entity_id, ordinal,
                        claim_text, claim_fingerprint, claim_type, assertion_status,
                        attribution, created_by, document_version_id
                    ) VALUES (
                        v_claim_id, analysis.id, NULL, claim_ordinal,
                        claim_text, fingerprint,
                        'other'::core.claim_type, 'reported'::core.assertion_status,
                        NULL, NULL, doc_version_id
                    );
                END IF;

                FOR loc_item IN
                    SELECT value FROM jsonb_array_elements(acc_item -> 'accepted_locators')
                LOOP
                    locator_ordinal := (loc_item ->> 'locator_ordinal')::integer;
                    src_loc := (src_claim -> 'evidence') -> locator_ordinal;
                    locator_type := src_loc ->> 'locator_type';
                    src_start := (src_loc ->> 'start')::integer;
                    src_end := (src_loc ->> 'end')::integer;
                    page_start := NULLIF(src_loc ->> 'page_start', '')::integer;
                    page_end := NULLIF(src_loc ->> 'page_end', '')::integer;
                    time_start_ms := NULLIF(src_loc ->> 'time_start_ms', '')::bigint;
                    time_end_ms := NULLIF(src_loc ->> 'time_end_ms', '')::bigint;

                    evidence_text := loc_item ->> 'evidence_text';
                    IF evidence_text IS NULL THEN
                        RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                    END IF;
                    evidence_bytes := octet_length(convert_to(evidence_text, 'UTF8'));
                    IF evidence_bytes > 8192 THEN
                        RAISE EXCEPTION 'locator_excerpt_too_large' USING ERRCODE = '22023';
                    END IF;

                    axes_char_start := NULLIF(loc_item ->> 'char_start', '')::integer;
                    axes_char_end := NULLIF(loc_item ->> 'char_end', '')::integer;
                    axes_page_start := NULLIF(loc_item ->> 'page_start', '')::integer;
                    axes_page_end := NULLIF(loc_item ->> 'page_end', '')::integer;
                    axes_time_start := NULLIF(loc_item ->> 'time_start_ms', '')::bigint;
                    axes_time_end := NULLIF(loc_item ->> 'time_end_ms', '')::bigint;

                    IF locator_type IN ('text', 'html') THEN
                        IF axes_char_start IS DISTINCT FROM src_start
                           OR axes_char_end IS DISTINCT FROM src_end
                           OR axes_page_start IS NOT NULL OR axes_page_end IS NOT NULL
                           OR axes_time_start IS NOT NULL OR axes_time_end IS NOT NULL
                           OR page_start IS NOT NULL OR page_end IS NOT NULL
                           OR time_start_ms IS NOT NULL OR time_end_ms IS NOT NULL THEN
                            RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                        END IF;
                        char_start := src_start;
                        char_end := src_end;
                        page_start := NULL;
                        page_end := NULL;
                        time_start_ms := NULL;
                        time_end_ms := NULL;
                    ELSIF locator_type = 'pdf' THEN
                        IF axes_page_start IS DISTINCT FROM page_start
                           OR axes_page_end IS DISTINCT FROM page_end
                           OR axes_char_start IS NOT NULL OR axes_char_end IS NOT NULL
                           OR axes_time_start IS NOT NULL OR axes_time_end IS NOT NULL
                           OR page_start IS NULL OR page_end IS NULL
                           OR time_start_ms IS NOT NULL OR time_end_ms IS NOT NULL THEN
                            RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                        END IF;
                        IF NOT core._claim_cross_axis_ok(
                            locator_type, src_start, src_end,
                            page_start, page_end, NULL, NULL, location_map
                        ) THEN
                            RAISE EXCEPTION 'locator_cross_axis_mismatch' USING ERRCODE = '22023';
                        END IF;
                        char_start := NULL;
                        char_end := NULL;
                        time_start_ms := NULL;
                        time_end_ms := NULL;
                    ELSIF locator_type IN ('audio', 'video') THEN
                        IF axes_time_start IS DISTINCT FROM time_start_ms
                           OR axes_time_end IS DISTINCT FROM time_end_ms
                           OR axes_char_start IS NOT NULL OR axes_char_end IS NOT NULL
                           OR axes_page_start IS NOT NULL OR axes_page_end IS NOT NULL
                           OR time_start_ms IS NULL OR time_end_ms IS NULL
                           OR page_start IS NOT NULL OR page_end IS NOT NULL THEN
                            RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                        END IF;
                        IF NOT core._claim_cross_axis_ok(
                            locator_type, src_start, src_end,
                            NULL, NULL, time_start_ms, time_end_ms, location_map
                        ) THEN
                            RAISE EXCEPTION 'locator_cross_axis_mismatch' USING ERRCODE = '22023';
                        END IF;
                        char_start := NULL;
                        char_end := NULL;
                        page_start := NULL;
                        page_end := NULL;
                    ELSE
                        RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                    END IF;

                    envelope := jsonb_build_object(
                        'locator_schema_version', 'evidence-locator.v2',
                        'document_version_id', doc_version_id::text,
                        'extraction_id', extraction_id::text,
                        'input_sha256', input_sha,
                        'source_locator', core._claim_source_locator_json(src_loc)
                    );
                    locator_sha := core.compute_evidence_locator_sha256(envelope);

                    SELECT * INTO existing_span
                      FROM core.evidence_spans
                     WHERE document_version_id = doc_version_id
                       AND locator_sha256 = locator_sha
                     FOR UPDATE;
                    IF existing_span.id IS NOT NULL THEN
                        IF existing_span.locator IS DISTINCT FROM envelope
                           OR existing_span.extraction_id IS DISTINCT FROM extraction_id
                           OR existing_span.evidence_text IS DISTINCT FROM evidence_text
                           OR existing_span.locator_type::text IS DISTINCT FROM locator_type
                           OR existing_span.char_start IS DISTINCT FROM char_start
                           OR existing_span.char_end IS DISTINCT FROM char_end
                           OR existing_span.page_start IS DISTINCT FROM page_start
                           OR existing_span.page_end IS DISTINCT FROM page_end
                           OR existing_span.time_start_ms IS DISTINCT FROM time_start_ms
                           OR existing_span.time_end_ms IS DISTINCT FROM time_end_ms THEN
                            RAISE EXCEPTION 'knowledge_locator_hash_conflict'
                                USING ERRCODE = '22023';
                        END IF;
                        v_span_id := existing_span.id;
                    ELSE
                        v_span_id := gen_random_uuid();
                        INSERT INTO core.evidence_spans (
                            id, document_version_id, extraction_id, evidence_text,
                            locator_type, char_start, char_end, page_start, page_end,
                            time_start_ms, time_end_ms, locator, locator_sha256
                        ) VALUES (
                            v_span_id, doc_version_id, extraction_id, evidence_text,
                            locator_type::core.locator_type,
                            char_start, char_end, page_start, page_end,
                            time_start_ms, time_end_ms, envelope, locator_sha
                        );
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM core.claim_evidence AS evidence
                         WHERE evidence.claim_id = v_claim_id
                           AND evidence.evidence_span_id = v_span_id
                    ) THEN
                        INSERT INTO core.claim_evidence (
                            id, claim_id, evidence_span_id, support_type, document_version_id
                        ) VALUES (
                            gen_random_uuid(), v_claim_id, v_span_id,
                            'supports'::core.support_type, doc_version_id
                        );
                    END IF;
                END LOOP;
            END LOOP;

            IF coalesce(array_length(seen_claim_ordinals, 1), 0) <> claim_count THEN
                RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
            END IF;

            SELECT count(*) INTO materialized_candidates
              FROM core.claims
             WHERE origin_analysis_result_id = analysis.id;
            SELECT count(*) INTO materialized_locators
              FROM core.claim_evidence AS evidence
              JOIN core.claims AS claim ON claim.id = evidence.claim_id
             WHERE claim.origin_analysis_result_id = analysis.id;

            RETURN jsonb_build_object(
                'materialized_candidates', materialized_candidates,
                'materialized_locators', materialized_locators,
                'empty_valid_result', false
            );
        END
        $materialize_claim_bundle$;

        REVOKE ALL ON FUNCTION core._claim_source_locator_json(jsonb) FROM PUBLIC;
        REVOKE ALL ON FUNCTION core._claim_cross_axis_ok(
            text, integer, integer, integer, integer, bigint, bigint, jsonb
        ) FROM PUBLIC;
        REVOKE ALL ON FUNCTION core._jsonb_keys_exact(jsonb, text[]) FROM PUBLIC;
        REVOKE ALL ON FUNCTION core.materialize_claim_bundle(uuid, uuid, uuid, jsonb)
            FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION core.materialize_claim_bundle(uuid, uuid, uuid, jsonb)
            TO uap_worker;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;
        REVOKE EXECUTE ON FUNCTION core.materialize_claim_bundle(uuid, uuid, uuid, jsonb)
            FROM uap_worker;
        DROP FUNCTION IF EXISTS core.materialize_claim_bundle(uuid, uuid, uuid, jsonb);
        DROP FUNCTION IF EXISTS core._claim_cross_axis_ok(
            text, integer, integer, integer, integer, bigint, bigint, jsonb
        );
        DROP FUNCTION IF EXISTS core._claim_source_locator_json(jsonb);
        DROP FUNCTION IF EXISTS core._jsonb_keys_exact(jsonb, text[]);

        CREATE OR REPLACE FUNCTION ops.validate_knowledge_attempt_metrics(
            p_metrics jsonb,
            p_outcome ops.attempt_outcome
        ) RETURNS void
        LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path = ops, pg_catalog
        AS $validate_knowledge_attempt_metrics$
        DECLARE
            allowed_keys text[] := ARRAY[
                'schema_version', 'input_candidates', 'materialized_candidates',
                'input_locators', 'materialized_locators', 'rejected_candidates',
                'rejected_locators', 'empty_valid_result', 'rejected_by_code', 'samples'
            ];
            allowed_codes text[] := ARRAY[
                'locator_end_not_after_start', 'locator_out_of_range',
                'locator_axis_conflict', 'locator_pdf_page_missing',
                'locator_time_missing', 'locator_page_range_invalid',
                'locator_time_range_invalid', 'locator_location_map_invalid',
                'locator_cross_axis_mismatch', 'locator_excerpt_too_large',
                'locator_duplicate', 'knowledge_extraction_missing',
                'knowledge_extraction_ambiguous', 'knowledge_extraction_mismatch',
                'knowledge_locator_unmappable', 'knowledge_invalid_origin',
                'knowledge_schema_unsupported', 'knowledge_payload_mismatch',
                'knowledge_bundle_mismatch'
            ];
            unknown_key text;
            input_candidates integer;
            materialized_candidates integer;
            input_locators integer;
            materialized_locators integer;
            rejected_candidates integer;
            rejected_locators integer;
            empty_valid boolean;
            code_sum integer;
            sample jsonb;
            sample_key text;
            code_value jsonb;
        BEGIN
            IF p_metrics IS NULL OR jsonb_typeof(p_metrics) <> 'object' THEN
                RAISE EXCEPTION 'knowledge attempt metrics must be an object'
                    USING ERRCODE = '22023';
            END IF;
            IF pg_column_size(p_metrics) > 65536 THEN
                RAISE EXCEPTION 'knowledge attempt metrics exceed 64KiB'
                    USING ERRCODE = '22023';
            END IF;
            SELECT key INTO unknown_key
              FROM jsonb_object_keys(p_metrics) AS key
             WHERE key <> ALL (allowed_keys)
             LIMIT 1;
            IF unknown_key IS NOT NULL THEN
                RAISE EXCEPTION 'knowledge attempt metrics contain unknown keys'
                    USING ERRCODE = '22023';
            END IF;
            IF p_metrics ->> 'schema_version' <> 'knowledge-attempt-metrics.v1' THEN
                RAISE EXCEPTION 'knowledge attempt metrics schema is unsupported'
                    USING ERRCODE = '22023';
            END IF;
            IF jsonb_typeof(p_metrics -> 'empty_valid_result') <> 'boolean' THEN
                RAISE EXCEPTION 'empty_valid_result must be boolean'
                    USING ERRCODE = '22023';
            END IF;
            empty_valid := (p_metrics ->> 'empty_valid_result')::boolean;
            BEGIN
                input_candidates := (p_metrics ->> 'input_candidates')::integer;
                materialized_candidates := (p_metrics ->> 'materialized_candidates')::integer;
                input_locators := (p_metrics ->> 'input_locators')::integer;
                materialized_locators := (p_metrics ->> 'materialized_locators')::integer;
                rejected_candidates := (p_metrics ->> 'rejected_candidates')::integer;
                rejected_locators := (p_metrics ->> 'rejected_locators')::integer;
            EXCEPTION
                WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'knowledge attempt metrics counts are required'
                        USING ERRCODE = '22023';
            END;
            IF input_candidates IS NULL OR materialized_candidates IS NULL
               OR input_locators IS NULL OR materialized_locators IS NULL
               OR rejected_candidates IS NULL OR rejected_locators IS NULL THEN
                RAISE EXCEPTION 'knowledge attempt metrics counts are required'
                    USING ERRCODE = '22023';
            END IF;
            IF input_candidates < 0 OR materialized_candidates < 0
               OR input_locators < 0 OR materialized_locators < 0
               OR rejected_candidates < 0 OR rejected_locators < 0 THEN
                RAISE EXCEPTION 'knowledge attempt metrics counts cannot be negative'
                    USING ERRCODE = '22023';
            END IF;
            IF materialized_candidates + rejected_candidates <> input_candidates
               OR materialized_locators + rejected_locators <> input_locators THEN
                RAISE EXCEPTION 'knowledge attempt metrics counts are inconsistent'
                    USING ERRCODE = '22023';
            END IF;
            IF jsonb_typeof(p_metrics -> 'rejected_by_code') IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'rejected_by_code must be an object'
                    USING ERRCODE = '22023';
            END IF;
            SELECT key INTO unknown_key
              FROM jsonb_object_keys(p_metrics -> 'rejected_by_code') AS key
             WHERE key <> ALL (allowed_codes)
             LIMIT 1;
            IF unknown_key IS NOT NULL THEN
                RAISE EXCEPTION 'rejected_by_code contains an unknown reason'
                    USING ERRCODE = '22023';
            END IF;
            FOR code_value IN
                SELECT value FROM jsonb_each(p_metrics -> 'rejected_by_code')
            LOOP
                IF jsonb_typeof(code_value) <> 'number'
                   OR truncate((code_value #>> '{}')::numeric) <> (code_value #>> '{}')::numeric
                   OR (code_value #>> '{}')::integer < 0 THEN
                    RAISE EXCEPTION 'rejected_by_code counts must be non-negative integers'
                        USING ERRCODE = '22023';
                END IF;
            END LOOP;
            SELECT coalesce(sum((value)::integer), 0) INTO code_sum
              FROM jsonb_each_text(p_metrics -> 'rejected_by_code');
            IF code_sum <> rejected_locators THEN
                RAISE EXCEPTION 'rejected_by_code does not match rejected locators'
                    USING ERRCODE = '22023';
            END IF;
            IF jsonb_typeof(p_metrics -> 'samples') IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION 'samples must be an array'
                    USING ERRCODE = '22023';
            END IF;
            IF jsonb_array_length(p_metrics -> 'samples') > 50 THEN
                RAISE EXCEPTION 'samples exceed the frozen maximum'
                    USING ERRCODE = '22023';
            END IF;
            FOR sample IN SELECT value FROM jsonb_array_elements(p_metrics -> 'samples')
            LOOP
                IF jsonb_typeof(sample) <> 'object' THEN
                    RAISE EXCEPTION 'sample rows must be objects'
                        USING ERRCODE = '22023';
                END IF;
                SELECT key INTO sample_key
                  FROM jsonb_object_keys(sample) AS key
                 WHERE key NOT IN ('candidate_ordinal', 'locator_ordinal', 'reason_code')
                 LIMIT 1;
                IF sample_key IS NOT NULL THEN
                    RAISE EXCEPTION 'sample rows contain unknown keys'
                        USING ERRCODE = '22023';
                END IF;
                IF coalesce(sample ->> 'reason_code', '') <> ALL (allowed_codes) THEN
                    RAISE EXCEPTION 'sample reason_code is not frozen'
                        USING ERRCODE = '22023';
                END IF;
                IF jsonb_typeof(sample -> 'candidate_ordinal') IS DISTINCT FROM 'number'
                   OR jsonb_typeof(sample -> 'locator_ordinal') IS DISTINCT FROM 'number' THEN
                    RAISE EXCEPTION 'sample ordinals must be numbers'
                        USING ERRCODE = '22023';
                END IF;
            END LOOP;
            IF empty_valid THEN
                IF p_outcome <> 'succeeded'
                   OR input_candidates <> 0 OR materialized_candidates <> 0 THEN
                    RAISE EXCEPTION 'empty valid metrics require a zero success'
                        USING ERRCODE = '22023';
                END IF;
            END IF;
            IF p_outcome = 'succeeded'
               AND NOT (materialized_candidates > 0 OR empty_valid) THEN
                RAISE EXCEPTION 'successful knowledge metrics require materialization or empty valid'
                    USING ERRCODE = '22023';
            END IF;
            IF p_outcome IN ('terminal_failure', 'retryable_failure')
               AND materialized_candidates <> 0 THEN
                RAISE EXCEPTION 'failed knowledge metrics cannot report materialization'
                    USING ERRCODE = '22023';
            END IF;
        END
        $validate_knowledge_attempt_metrics$;
        """
    )
