"""Entity candidate materialization write authority for resolve_entities.

Revision ID: 0012_entity_materialization
Revises: 0011_claim_materialization
Create Date: 2026-08-24
"""

from __future__ import annotations

from alembic import op

revision = "0012_entity_materialization"
down_revision = "0011_claim_materialization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION core.materialize_entity_bundle(
            p_job_id uuid,
            p_attempt_id uuid,
            p_lease_token uuid,
            p_bundle jsonb
        ) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = core, ops, pg_catalog
        AS $materialize_entity_bundle$
        DECLARE
            payload jsonb;
            analysis core.analysis_results%ROWTYPE;
            model_run ops.model_runs%ROWTYPE;
            extraction core.extractions%ROWTYPE;
            entities_json jsonb;
            entity_count integer;
            accepted jsonb;
            rejected jsonb;
            acc_item jsonb;
            rej_item jsonb;
            loc_item jsonb;
            src_entity jsonb;
            src_loc jsonb;
            entity_ordinal integer;
            locator_ordinal integer;
            proposed_name text;
            proposed_type text;
            existing_candidate core.entity_candidates%ROWTYPE;
            v_candidate_id uuid;
            existing_span core.evidence_spans%ROWTYPE;
            existing_link core.entity_candidate_evidence%ROWTYPE;
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
            seen_entity_ordinals integer[] := ARRAY[]::integer[];
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
                RAISE EXCEPTION 'only worker may materialize entity bundles'
                    USING ERRCODE = '42501';
            END IF;

            payload := ops.require_active_resolution_job_lease(
                p_job_id, p_attempt_id, p_lease_token, 'resolve_entities'
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
               OR analysis.result_type::text IS DISTINCT FROM 'entity_extraction'
               OR analysis.model_run_id::text IS DISTINCT FROM payload ->> 'model_run_id'
               OR analysis.result_sha256 IS DISTINCT FROM payload ->> 'analysis_result_sha256'
               OR analysis.schema_version IS DISTINCT FROM payload ->> 'analysis_schema_version'
               OR analysis.schema_version IS DISTINCT FROM 'ai.v1'
               OR analysis.validation_status IS DISTINCT FROM 'valid'::core.validation_status
               OR model_run.status IS DISTINCT FROM 'succeeded'::ops.model_run_status
               OR model_run.input_sha256 IS DISTINCT FROM payload ->> 'input_sha256'
               OR model_run.document_version_id IS DISTINCT FROM analysis.document_version_id
               OR model_run.task_type::text IS DISTINCT FROM 'entity_extraction' THEN
                RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
            END IF;

            doc_version_id := analysis.document_version_id;
            input_sha := model_run.input_sha256;
            entities_json := analysis.result -> 'entities';
            IF entities_json IS NULL OR jsonb_typeof(entities_json) <> 'array' THEN
                RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
            END IF;
            entity_count := jsonb_array_length(entities_json);

            accepted := p_bundle -> 'accepted_candidates';
            rejected := p_bundle -> 'rejected_candidates';
            IF jsonb_typeof(accepted) <> 'array' OR jsonb_typeof(rejected) <> 'array' THEN
                RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
            END IF;

            IF entity_count = 0 THEN
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

            FOR entity_ordinal IN 0 .. entity_count - 1 LOOP
                IF NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(accepted) AS a(value)
                     WHERE (a.value ->> 'ordinal')::integer = entity_ordinal
                ) AND NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(rejected) AS r(value)
                     WHERE (r.value ->> 'ordinal')::integer = entity_ordinal
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
                entity_ordinal := (rej_item ->> 'ordinal')::integer;
                IF entity_ordinal IS NULL OR entity_ordinal < 0 OR entity_ordinal >= entity_count THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;
                IF entity_ordinal = ANY (seen_entity_ordinals) THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;
                seen_entity_ordinals := array_append(seen_entity_ordinals, entity_ordinal);
                IF rej_item ->> 'reason_code' IS NULL THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;
                src_entity := entities_json -> entity_ordinal;
                source_loc_count := jsonb_array_length(src_entity -> 'evidence');
                IF source_loc_count IS NULL OR source_loc_count < 1 OR source_loc_count > 20 THEN
                    RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
                END IF;
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
                entity_ordinal := (acc_item ->> 'ordinal')::integer;
                IF entity_ordinal IS NULL OR entity_ordinal < 0 OR entity_ordinal >= entity_count THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;
                IF entity_ordinal = ANY (seen_entity_ordinals) THEN
                    RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
                END IF;
                seen_entity_ordinals := array_append(seen_entity_ordinals, entity_ordinal);

                src_entity := entities_json -> entity_ordinal;
                proposed_name := src_entity ->> 'name';
                proposed_type := src_entity ->> 'entity_type';
                IF proposed_name IS NULL OR proposed_name = '' THEN
                    RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
                END IF;
                IF proposed_type IS NULL
                   OR proposed_type NOT IN (
                        'person', 'organization', 'location',
                        'event', 'object', 'concept'
                   ) THEN
                    RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
                END IF;
                source_loc_count := jsonb_array_length(src_entity -> 'evidence');
                IF source_loc_count IS NULL OR source_loc_count < 1 OR source_loc_count > 20 THEN
                    RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
                END IF;

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

                SELECT * INTO existing_candidate
                  FROM core.entity_candidates
                 WHERE analysis_result_id = analysis.id
                   AND ordinal = entity_ordinal
                 FOR UPDATE;
                IF existing_candidate.id IS NOT NULL THEN
                    IF existing_candidate.proposed_name IS DISTINCT FROM proposed_name
                       OR existing_candidate.proposed_entity_type::text
                          IS DISTINCT FROM proposed_type
                       OR existing_candidate.proposed_aliases IS DISTINCT FROM '[]'::jsonb
                       OR existing_candidate.candidate_payload IS DISTINCT FROM src_entity
                       OR existing_candidate.status::text IS DISTINCT FROM 'pending'
                       OR existing_candidate.evidence_span_id IS NOT NULL
                       OR existing_candidate.resolved_entity_id IS NOT NULL
                       OR existing_candidate.document_version_id IS DISTINCT FROM doc_version_id THEN
                        RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
                    END IF;
                    v_candidate_id := existing_candidate.id;
                ELSE
                    v_candidate_id := gen_random_uuid();
                    INSERT INTO core.entity_candidates (
                        id, analysis_result_id, document_version_id, evidence_span_id,
                        resolved_entity_id, ordinal, result_type, proposed_entity_type,
                        proposed_name, proposed_aliases, candidate_payload, status
                    ) VALUES (
                        v_candidate_id, analysis.id, doc_version_id, NULL,
                        NULL, entity_ordinal, 'entity_extraction'::ops.model_task_type,
                        proposed_type::core.entity_type, proposed_name, '[]'::jsonb,
                        src_entity, 'pending'::core.candidate_status
                    );
                END IF;

                FOR loc_item IN
                    SELECT value FROM jsonb_array_elements(acc_item -> 'accepted_locators')
                LOOP
                    locator_ordinal := (loc_item ->> 'locator_ordinal')::integer;
                    src_loc := (src_entity -> 'evidence') -> locator_ordinal;
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

                    SELECT * INTO existing_link
                      FROM core.entity_candidate_evidence AS evidence
                     WHERE evidence.entity_candidate_id = v_candidate_id
                       AND evidence.evidence_ordinal = locator_ordinal
                     FOR UPDATE;
                    IF existing_link.id IS NOT NULL THEN
                        IF existing_link.evidence_span_id IS DISTINCT FROM v_span_id
                           OR existing_link.document_version_id IS DISTINCT FROM doc_version_id THEN
                            RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
                        END IF;
                    ELSE
                        INSERT INTO core.entity_candidate_evidence (
                            id, entity_candidate_id, evidence_span_id, document_version_id,
                            evidence_ordinal
                        ) VALUES (
                            gen_random_uuid(), v_candidate_id, v_span_id, doc_version_id,
                            locator_ordinal
                        );
                    END IF;
                END LOOP;
            END LOOP;

            IF coalesce(array_length(seen_entity_ordinals, 1), 0) <> entity_count THEN
                RAISE EXCEPTION 'knowledge_bundle_mismatch' USING ERRCODE = '22023';
            END IF;

            SELECT count(*) INTO materialized_candidates
              FROM core.entity_candidates
             WHERE analysis_result_id = analysis.id;
            SELECT count(*) INTO materialized_locators
              FROM core.entity_candidate_evidence AS evidence
              JOIN core.entity_candidates AS candidate
                ON candidate.id = evidence.entity_candidate_id
             WHERE candidate.analysis_result_id = analysis.id;

            RETURN jsonb_build_object(
                'materialized_candidates', materialized_candidates,
                'materialized_locators', materialized_locators,
                'empty_valid_result', false
            );
        END
        $materialize_entity_bundle$;

        REVOKE ALL ON FUNCTION core.materialize_entity_bundle(uuid, uuid, uuid, jsonb)
            FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION core.materialize_entity_bundle(uuid, uuid, uuid, jsonb)
            TO uap_worker;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;
        REVOKE EXECUTE ON FUNCTION core.materialize_entity_bundle(uuid, uuid, uuid, jsonb)
            FROM uap_worker;
        DROP FUNCTION IF EXISTS core.materialize_entity_bundle(uuid, uuid, uuid, jsonb);
        """
    )
