"""Foundation for knowledge job handover, constraints, and write authority.

Revision ID: 0010_knowledge_foundation
Revises: 0009_model_governance_boundaries
Create Date: 2026-08-21
"""

from __future__ import annotations

from alembic import op

revision = "0010_knowledge_foundation"
down_revision = "0009_model_governance_boundaries"
branch_labels = None
depends_on = None

KNOWLEDGE_TABLES = (
    "core.claims",
    "core.claim_evidence",
    "core.evidence_spans",
    "core.entities",
    "core.entity_candidates",
    "core.entity_candidate_evidence",
    "core.entity_aliases",
    "core.entity_merge_events",
    "core.relations",
    "core.relation_evidence",
    "core.tags",
    "core.document_tags",
    "core.entity_tags",
    "core.claim_tags",
)

ORIGINAL_KNOWLEDGE_TABLES = tuple(
    table for table in KNOWLEDGE_TABLES if table != "core.entity_candidate_evidence"
)


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        ALTER TABLE core.claims
            ADD COLUMN document_version_id uuid;
        ALTER TABLE core.claim_evidence
            ADD COLUMN document_version_id uuid;

        UPDATE core.claims AS claim
           SET document_version_id = analysis.document_version_id
          FROM core.analysis_results AS analysis
         WHERE claim.origin_analysis_result_id = analysis.id
           AND claim.document_version_id IS NULL;

        DO $backfill_manual_claims$
        DECLARE
            claim_row record;
            versions uuid[];
        BEGIN
            FOR claim_row IN
                SELECT id
                  FROM core.claims
                 WHERE origin_analysis_result_id IS NULL
                   AND document_version_id IS NULL
            LOOP
                SELECT array_agg(DISTINCT span.document_version_id)
                  INTO versions
                  FROM core.claim_evidence AS evidence
                  JOIN core.evidence_spans AS span
                    ON span.id = evidence.evidence_span_id
                 WHERE evidence.claim_id = claim_row.id;
                IF versions IS NULL OR coalesce(array_length(versions, 1), 0) <> 1 THEN
                    RAISE EXCEPTION 'knowledge_claim_backfill_required'
                        USING ERRCODE = '23514';
                END IF;
                UPDATE core.claims
                   SET document_version_id = versions[1]
                 WHERE id = claim_row.id;
            END LOOP;
        END
        $backfill_manual_claims$;

        UPDATE core.claim_evidence AS evidence
           SET document_version_id = claim.document_version_id
          FROM core.claims AS claim
         WHERE evidence.claim_id = claim.id
           AND evidence.document_version_id IS NULL;

        DO $verify_claim_evidence_version$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM core.claim_evidence AS evidence
                  JOIN core.evidence_spans AS span
                    ON span.id = evidence.evidence_span_id
                 WHERE evidence.document_version_id IS DISTINCT FROM span.document_version_id
                    OR evidence.document_version_id IS NULL
            ) THEN
                RAISE EXCEPTION 'knowledge_claim_backfill_required'
                    USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1 FROM core.claims WHERE document_version_id IS NULL
            ) THEN
                RAISE EXCEPTION 'knowledge_claim_backfill_required'
                    USING ERRCODE = '23514';
            END IF;
        END
        $verify_claim_evidence_version$;

        ALTER TABLE core.claims
            ALTER COLUMN document_version_id SET NOT NULL,
            ADD CONSTRAINT fk_claims_document_version
                FOREIGN KEY (document_version_id) REFERENCES core.document_versions(id),
            ADD CONSTRAINT uq_claims_id_document UNIQUE (id, document_version_id),
            ADD CONSTRAINT fk_claims_origin_same_document
                FOREIGN KEY (origin_analysis_result_id, document_version_id)
                REFERENCES core.analysis_results (id, document_version_id),
            ADD CONSTRAINT ck_claims_manual_actor CHECK (
                (origin_analysis_result_id IS NULL) = (created_by IS NOT NULL)
            );
        ALTER TABLE core.claim_evidence
            ALTER COLUMN document_version_id SET NOT NULL,
            ADD CONSTRAINT fk_claim_evidence_claim_document
                FOREIGN KEY (claim_id, document_version_id)
                REFERENCES core.claims (id, document_version_id),
            ADD CONSTRAINT fk_claim_evidence_span_document
                FOREIGN KEY (evidence_span_id, document_version_id)
                REFERENCES core.evidence_spans (id, document_version_id);

        ALTER TABLE core.entity_candidates
            ADD CONSTRAINT uq_entity_candidates_id_document UNIQUE (id, document_version_id);

        CREATE TABLE core.entity_candidate_evidence (
            id uuid PRIMARY KEY,
            entity_candidate_id uuid NOT NULL,
            evidence_span_id uuid NOT NULL,
            document_version_id uuid NOT NULL,
            evidence_ordinal integer NOT NULL CHECK (evidence_ordinal >= 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (entity_candidate_id, evidence_ordinal),
            FOREIGN KEY (entity_candidate_id, document_version_id)
                REFERENCES core.entity_candidates (id, document_version_id),
            FOREIGN KEY (evidence_span_id, document_version_id)
                REFERENCES core.evidence_spans (id, document_version_id)
        );

        INSERT INTO core.entity_candidate_evidence (
            id, entity_candidate_id, evidence_span_id, document_version_id,
            evidence_ordinal, created_at
        )
        SELECT md5(candidate.id::text || coalesce(candidate.evidence_span_id::text, ''))::uuid,
               candidate.id,
               candidate.evidence_span_id,
               candidate.document_version_id,
               0,
               now()
          FROM core.entity_candidates AS candidate
         WHERE candidate.evidence_span_id IS NOT NULL;

        DO $verify_candidate_evidence$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM core.entity_candidate_evidence AS link
                  JOIN core.evidence_spans AS span
                    ON span.id = link.evidence_span_id
                 WHERE link.document_version_id IS DISTINCT FROM span.document_version_id
            ) THEN
                RAISE EXCEPTION 'knowledge_candidate_backfill_required'
                    USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM core.entity_candidates AS candidate
                 WHERE NOT EXISTS (
                    SELECT 1
                      FROM core.entity_candidate_evidence AS link
                     WHERE link.entity_candidate_id = candidate.id
                 )
            ) THEN
                RAISE EXCEPTION 'knowledge_candidate_backfill_required'
                    USING ERRCODE = '23514';
            END IF;
        END
        $verify_candidate_evidence$;

        CREATE FUNCTION core.require_ai_claim_origin() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = core, ops, pg_catalog
        AS $require_ai_claim_origin$
        BEGIN
            IF NEW.origin_analysis_result_id IS NOT NULL AND NOT EXISTS (
                SELECT 1
                  FROM core.analysis_results
                 WHERE id = NEW.origin_analysis_result_id
                   AND document_version_id = NEW.document_version_id
                   AND result_type = 'claim_extraction'::ops.model_task_type
                   AND validation_status = 'valid'::core.validation_status
            ) THEN
                RAISE EXCEPTION 'knowledge_invalid_origin' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $require_ai_claim_origin$;
        CREATE TRIGGER claims_require_ai_origin
            BEFORE INSERT OR UPDATE ON core.claims
            FOR EACH ROW EXECUTE FUNCTION core.require_ai_claim_origin();

        CREATE FUNCTION core.require_ai_claim_supports() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = core, ops, pg_catalog
        AS $require_ai_claim_supports$
        DECLARE
            target uuid;
        BEGIN
            IF TG_TABLE_NAME = 'claims' THEN
                IF NEW.origin_analysis_result_id IS NULL THEN
                    RETURN NULL;
                END IF;
                target := NEW.id;
            ELSE
                SELECT claim.id INTO target
                  FROM core.claims AS claim
                 WHERE claim.id = COALESCE(NEW.claim_id, OLD.claim_id)
                   AND claim.origin_analysis_result_id IS NOT NULL;
                IF target IS NULL THEN
                    RETURN NULL;
                END IF;
            END IF;
            IF NOT EXISTS (
                SELECT 1
                  FROM core.claim_evidence
                 WHERE claim_id = target
                   AND support_type = 'supports'::core.support_type
            ) THEN
                RAISE EXCEPTION 'AI claim requires at least one supports evidence'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END
        $require_ai_claim_supports$;
        CREATE CONSTRAINT TRIGGER claims_require_supports
            AFTER INSERT OR UPDATE ON core.claims
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION core.require_ai_claim_supports();
        CREATE CONSTRAINT TRIGGER claim_evidence_require_supports
            AFTER INSERT OR UPDATE OR DELETE ON core.claim_evidence
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION core.require_ai_claim_supports();

        CREATE FUNCTION core.require_valid_entity_origin() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = core, ops, pg_catalog
        AS $require_valid_entity_origin$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                  FROM core.analysis_results
                 WHERE id = NEW.analysis_result_id
                   AND document_version_id = NEW.document_version_id
                   AND result_type = 'entity_extraction'::ops.model_task_type
                   AND validation_status = 'valid'::core.validation_status
            ) THEN
                RAISE EXCEPTION 'knowledge_invalid_origin' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $require_valid_entity_origin$;
        CREATE TRIGGER entity_candidates_require_valid_origin
            BEFORE INSERT OR UPDATE ON core.entity_candidates
            FOR EACH ROW EXECUTE FUNCTION core.require_valid_entity_origin();

        CREATE FUNCTION core.require_entity_candidate_evidence() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = core, pg_catalog
        AS $require_entity_candidate_evidence$
        DECLARE
            target uuid;
        BEGIN
            IF TG_TABLE_NAME = 'entity_candidates' THEN
                target := NEW.id;
            ELSE
                target := COALESCE(NEW.entity_candidate_id, OLD.entity_candidate_id);
            END IF;
            IF EXISTS (SELECT 1 FROM core.entity_candidates WHERE id = target)
               AND NOT EXISTS (
                    SELECT 1
                      FROM core.entity_candidate_evidence
                     WHERE entity_candidate_id = target
               ) THEN
                RAISE EXCEPTION 'entity candidate requires at least one evidence row'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END
        $require_entity_candidate_evidence$;
        CREATE CONSTRAINT TRIGGER entity_candidates_require_evidence
            AFTER INSERT OR UPDATE ON core.entity_candidates
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION core.require_entity_candidate_evidence();
        CREATE CONSTRAINT TRIGGER entity_candidate_evidence_required
            AFTER INSERT OR UPDATE OR DELETE ON core.entity_candidate_evidence
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION core.require_entity_candidate_evidence();

        CREATE FUNCTION core.compute_evidence_locator_sha256(p_envelope jsonb) RETURNS text
        LANGUAGE plpgsql IMMUTABLE STRICT SECURITY DEFINER
        SET search_path = core, pg_catalog
        AS $compute_evidence_locator_sha256$
        BEGIN
            IF jsonb_typeof(p_envelope) <> 'object' THEN
                RAISE EXCEPTION 'knowledge_locator_hash_conflict' USING ERRCODE = '22023';
            END IF;
            RETURN encode(sha256(convert_to(p_envelope::text, 'UTF8')), 'hex');
        END
        $compute_evidence_locator_sha256$;

        CREATE FUNCTION core.compute_claim_fingerprint(p_text text) RETURNS text
        LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path = core, pg_catalog
        AS $compute_claim_fingerprint$
        DECLARE
            normalized text;
        BEGIN
            IF current_setting('server_encoding') <> 'UTF8' THEN
                RAISE EXCEPTION 'knowledge_schema_unsupported' USING ERRCODE = '22023';
            END IF;
            IF p_text IS NULL THEN
                RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
            END IF;
            normalized := btrim(
                regexp_replace(normalize(p_text, NFKC), '[[:space:]]+', ' ', 'g'),
                ' '
            );
            RETURN encode(sha256(convert_to(normalized, 'UTF8')), 'hex');
        END
        $compute_claim_fingerprint$;

        CREATE FUNCTION ops.enqueue_followup_job(p_analysis_result_id uuid) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, core, pg_catalog
        AS $enqueue_followup_job$
        DECLARE
            analysis core.analysis_results%ROWTYPE;
            model_run ops.model_runs%ROWTYPE;
            match_count integer;
            match_id uuid;
            anchor_status text;
            payload jsonb;
            v_job_type text;
            v_idempotency_key text;
            result_id uuid;
            existing ops.jobs%ROWTYPE;
        BEGIN
            SELECT * INTO analysis
              FROM core.analysis_results
             WHERE id = p_analysis_result_id;
            IF analysis.id IS NULL
               OR analysis.validation_status <> 'valid'::core.validation_status
               OR analysis.result_type NOT IN (
                    'claim_extraction'::ops.model_task_type,
                    'entity_extraction'::ops.model_task_type
               ) THEN
                RAISE EXCEPTION 'knowledge_invalid_origin' USING ERRCODE = '23514';
            END IF;
            SELECT * INTO model_run
              FROM ops.model_runs
             WHERE id = analysis.model_run_id
               AND document_version_id = analysis.document_version_id
               AND task_type = analysis.result_type;
            IF model_run.id IS NULL
               OR model_run.status <> 'succeeded'::ops.model_run_status THEN
                RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '23514';
            END IF;

            SELECT count(*),
                   CASE
                       WHEN count(*) = 1 THEN (array_agg(extraction.id))[1]
                   END
              INTO match_count, match_id
              FROM core.extractions AS extraction
              JOIN core.stored_objects AS stored
                ON stored.id = extraction.text_object_id
               AND stored.storage_domain = 'derived'::core.storage_domain
               AND stored.content_sha256 = extraction.output_sha256
             WHERE extraction.document_version_id = analysis.document_version_id
               AND extraction.outcome = 'succeeded'::core.extraction_outcome
               AND extraction.output_sha256 = model_run.input_sha256;

            IF match_count = 1 THEN
                anchor_status := 'matched';
            ELSIF match_count = 0 THEN
                anchor_status := 'missing';
                match_id := NULL;
            ELSE
                anchor_status := 'ambiguous';
                match_id := NULL;
            END IF;

            IF analysis.result_type = 'claim_extraction'::ops.model_task_type THEN
                v_job_type := 'resolve_claims';
                v_idempotency_key := 'resolve-claims:' || analysis.id::text;
            ELSE
                v_job_type := 'resolve_entities';
                v_idempotency_key := 'resolve-entities:' || analysis.id::text;
            END IF;

            payload := jsonb_build_object(
                'payload_schema_version', 'knowledge.v2',
                'analysis_result_id', analysis.id::text,
                'analysis_result_sha256', analysis.result_sha256,
                'analysis_schema_version', analysis.schema_version,
                'document_version_id', analysis.document_version_id::text,
                'result_type', analysis.result_type::text,
                'model_run_id', analysis.model_run_id::text,
                'input_sha256', model_run.input_sha256,
                'extraction_anchor_status', anchor_status,
                'extraction_id', match_id
            );

            INSERT INTO ops.jobs (
                id, job_type, payload, payload_schema_version, idempotency_key,
                priority, available_at, max_attempts, timeout_seconds
            ) VALUES (
                md5(random()::text || clock_timestamp()::text)::uuid,
                v_job_type, payload, 'knowledge.v2', v_idempotency_key,
                0, clock_timestamp(), 8, 60
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id INTO result_id;

            IF result_id IS NOT NULL THEN
                RETURN result_id;
            END IF;

            SELECT jobs.* INTO existing
              FROM ops.jobs AS jobs
             WHERE jobs.idempotency_key = v_idempotency_key
             FOR UPDATE;
            IF existing.job_type IS NOT DISTINCT FROM v_job_type
               AND existing.payload_schema_version IS NOT DISTINCT FROM 'knowledge.v2'
               AND existing.payload IS NOT DISTINCT FROM payload THEN
                RETURN existing.id;
            END IF;
            RAISE EXCEPTION 'knowledge_idempotency_payload_conflict'
                USING ERRCODE = '23505';
        END
        $enqueue_followup_job$;

        CREATE FUNCTION core.tg_enqueue_knowledge_followup() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = core, ops, pg_catalog
        AS $tg_enqueue_knowledge_followup$
        BEGIN
            IF NEW.validation_status = 'valid'::core.validation_status
               AND NEW.result_type IN (
                    'claim_extraction'::ops.model_task_type,
                    'entity_extraction'::ops.model_task_type
               ) THEN
                PERFORM ops.enqueue_followup_job(NEW.id);
            END IF;
            RETURN NEW;
        END
        $tg_enqueue_knowledge_followup$;
        CREATE TRIGGER analysis_results_enqueue_knowledge
            AFTER INSERT ON core.analysis_results
            FOR EACH ROW EXECUTE FUNCTION core.tg_enqueue_knowledge_followup();

        DO $backfill_knowledge_jobs$
        DECLARE
            analysis_row record;
        BEGIN
            FOR analysis_row IN
                SELECT id
                  FROM core.analysis_results
                 WHERE validation_status = 'valid'::core.validation_status
                   AND result_type IN (
                        'claim_extraction'::ops.model_task_type,
                        'entity_extraction'::ops.model_task_type
                   )
                 ORDER BY created_at, id
            LOOP
                PERFORM ops.enqueue_followup_job(analysis_row.id);
            END LOOP;
        END
        $backfill_knowledge_jobs$;

        CREATE FUNCTION ops.reconcile_knowledge_jobs(
            p_after_created_at timestamptz,
            p_after_id uuid,
            p_created_before timestamptz,
            p_limit integer DEFAULT 500
        ) RETURNS TABLE (
            analysis_result_id uuid,
            job_id uuid,
            extraction_anchor_status text,
            next_after_created_at timestamptz,
            next_after_id uuid
        )
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, core, pg_catalog
        AS $reconcile_knowledge_jobs$
        DECLARE
            analysis_row record;
            processed integer := 0;
            last_created_at timestamptz;
            last_id uuid;
            enqueued uuid;
            payload jsonb;
            page_limit integer;
            result_ids uuid[] := ARRAY[]::uuid[];
            result_jobs uuid[] := ARRAY[]::uuid[];
            result_anchors text[] := ARRAY[]::text[];
            idx integer;
        BEGIN
            IF session_user <> 'uap_scheduler' THEN
                RAISE EXCEPTION 'database role cannot reconcile knowledge jobs'
                    USING ERRCODE = '42501';
            END IF;
            IF p_created_before IS NULL THEN
                RAISE EXCEPTION 'created_before cursor bound is required'
                    USING ERRCODE = '22023';
            END IF;
            page_limit := coalesce(p_limit, 500);
            IF page_limit < 1 OR page_limit > 1000 THEN
                RAISE EXCEPTION 'reconcile limit must be between 1 and 1000'
                    USING ERRCODE = '22023';
            END IF;
            IF (p_after_created_at IS NULL) <> (p_after_id IS NULL) THEN
                RAISE EXCEPTION 'reconciliation cursor fields must be used together'
                    USING ERRCODE = '22023';
            END IF;

            FOR analysis_row IN
                SELECT analysis.id, analysis.created_at
                  FROM core.analysis_results AS analysis
                 WHERE analysis.validation_status = 'valid'::core.validation_status
                   AND analysis.result_type IN (
                        'claim_extraction'::ops.model_task_type,
                        'entity_extraction'::ops.model_task_type
                   )
                   AND analysis.created_at <= p_created_before
                   AND (
                        p_after_created_at IS NULL
                        OR (analysis.created_at, analysis.id)
                           > (p_after_created_at, p_after_id)
                   )
                 ORDER BY analysis.created_at, analysis.id
                 LIMIT page_limit
            LOOP
                enqueued := ops.enqueue_followup_job(analysis_row.id);
                SELECT jobs.payload INTO payload
                  FROM ops.jobs AS jobs
                 WHERE jobs.id = enqueued;
                processed := processed + 1;
                last_created_at := analysis_row.created_at;
                last_id := analysis_row.id;
                result_ids := result_ids || analysis_row.id;
                result_jobs := result_jobs || enqueued;
                result_anchors := result_anchors || (payload ->> 'extraction_anchor_status');
            END LOOP;

            IF processed = page_limit THEN
                next_after_created_at := last_created_at;
                next_after_id := last_id;
            ELSE
                next_after_created_at := NULL;
                next_after_id := NULL;
            END IF;

            FOR idx IN 1..processed LOOP
                analysis_result_id := result_ids[idx];
                job_id := result_jobs[idx];
                extraction_anchor_status := result_anchors[idx];
                RETURN NEXT;
            END LOOP;
        END
        $reconcile_knowledge_jobs$;
        """
    )
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION ops.require_active_resolution_job_lease(
            p_job_id uuid,
            p_attempt_id uuid,
            p_lease_token uuid,
            p_expected_type text
        ) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, pg_catalog
        AS $require_active_resolution_job_lease$
        DECLARE
            current_job ops.jobs%ROWTYPE;
            observed_at timestamptz;
        BEGIN
            IF session_user <> 'uap_worker' THEN
                RAISE EXCEPTION 'only worker may use a resolution lease'
                    USING ERRCODE = '42501';
            END IF;
            IF p_expected_type NOT IN ('resolve_claims', 'resolve_entities') THEN
                RAISE EXCEPTION 'resolution lease type is not allowed'
                    USING ERRCODE = '42501';
            END IF;
            SELECT * INTO current_job
              FROM ops.jobs
             WHERE id = p_job_id
             FOR UPDATE;
            observed_at := clock_timestamp();
            IF current_job.id IS NULL
               OR current_job.status <> 'running'
               OR current_job.lease_token IS DISTINCT FROM p_lease_token
               OR current_job.lease_expires_at <= observed_at THEN
                RAISE EXCEPTION 'resolution job lease is missing, expired, or owned by another worker'
                    USING ERRCODE = '40001';
            END IF;
            IF current_job.job_type <> p_expected_type THEN
                RAISE EXCEPTION 'resolution lease type is not allowed'
                    USING ERRCODE = '42501';
            END IF;
            PERFORM 1
              FROM ops.job_attempts
             WHERE id = p_attempt_id
               AND job_id = p_job_id
               AND lease_token = p_lease_token
               AND outcome = 'running'
               AND finished_at IS NULL
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'resolution job attempt is missing or already finished'
                    USING ERRCODE = '40001';
            END IF;
            RETURN current_job.payload;
        END
        $require_active_resolution_job_lease$;

        CREATE FUNCTION ops.validate_knowledge_attempt_metrics(
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

        CREATE FUNCTION ops.finish_knowledge_job(
            p_job_id uuid,
            p_attempt_id uuid,
            p_lease_token uuid,
            p_outcome ops.attempt_outcome,
            p_http_status smallint DEFAULT NULL,
            p_error_code text DEFAULT NULL,
            p_error_summary text DEFAULT NULL,
            p_retry_delay_seconds integer DEFAULT NULL,
            p_metrics jsonb DEFAULT NULL
        ) RETURNS ops.job_status
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, core, pg_catalog
        AS $finish_knowledge_job$
        DECLARE
            current_job ops.jobs%ROWTYPE;
            payload jsonb;
            v_analysis_id uuid;
            v_result_type text;
            v_array_length integer;
            materialized_candidates integer;
            materialized_locators integer;
            payload_parsed boolean := FALSE;
        BEGIN
            IF session_user <> 'uap_worker' THEN
                RAISE EXCEPTION 'only worker may finish a knowledge job'
                    USING ERRCODE = '42501';
            END IF;
            SELECT * INTO current_job
              FROM ops.jobs
             WHERE id = p_job_id
             FOR UPDATE;
            IF current_job.id IS NULL
               OR current_job.job_type NOT IN ('resolve_claims', 'resolve_entities') THEN
                RAISE EXCEPTION 'knowledge finish is limited to resolve jobs'
                    USING ERRCODE = '42501';
            END IF;
            payload := ops.require_active_resolution_job_lease(
                p_job_id, p_attempt_id, p_lease_token, current_job.job_type
            );
            PERFORM ops.validate_knowledge_attempt_metrics(p_metrics, p_outcome);

            BEGIN
                IF current_job.payload_schema_version IS DISTINCT FROM 'knowledge.v2'
                   OR payload ->> 'payload_schema_version' IS DISTINCT FROM 'knowledge.v2' THEN
                    RAISE EXCEPTION 'knowledge_schema_unsupported' USING ERRCODE = '22023';
                END IF;
                v_analysis_id := (payload ->> 'analysis_result_id')::uuid;
                v_result_type := payload ->> 'result_type';
                IF (current_job.job_type = 'resolve_claims'
                    AND v_result_type IS DISTINCT FROM 'claim_extraction')
                   OR (current_job.job_type = 'resolve_entities'
                    AND v_result_type IS DISTINCT FROM 'entity_extraction') THEN
                    RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                      FROM core.analysis_results AS analysis
                      JOIN ops.model_runs AS model_run
                        ON model_run.id = analysis.model_run_id
                     WHERE analysis.id = v_analysis_id
                       AND analysis.document_version_id::text
                           = payload ->> 'document_version_id'
                       AND analysis.result_type::text = v_result_type
                       AND analysis.model_run_id::text = payload ->> 'model_run_id'
                       AND analysis.result_sha256 = payload ->> 'analysis_result_sha256'
                       AND analysis.schema_version = payload ->> 'analysis_schema_version'
                       AND analysis.schema_version = 'ai.v1'
                       AND analysis.validation_status = 'valid'::core.validation_status
                       AND model_run.status = 'succeeded'::ops.model_run_status
                       AND model_run.input_sha256 = payload ->> 'input_sha256'
                       AND model_run.document_version_id = analysis.document_version_id
                       AND model_run.task_type::text = v_result_type
                ) THEN
                    RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
                END IF;
                SELECT CASE
                           WHEN v_result_type = 'claim_extraction'
                               THEN jsonb_array_length(analysis.result -> 'claims')
                           ELSE jsonb_array_length(analysis.result -> 'entities')
                       END
                  INTO v_array_length
                  FROM core.analysis_results AS analysis
                 WHERE analysis.id = v_analysis_id;
                IF v_array_length IS NULL THEN
                    RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
                END IF;
                IF (p_metrics ->> 'empty_valid_result')::boolean
                   AND v_array_length <> 0 THEN
                    RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
                END IF;
                payload_parsed := TRUE;
                IF v_array_length > 0
                   AND p_outcome = 'succeeded'
                   AND payload ->> 'extraction_anchor_status' IS DISTINCT FROM 'matched' THEN
                    RAISE EXCEPTION 'knowledge_extraction_mismatch' USING ERRCODE = '22023';
                END IF;
                IF current_job.job_type = 'resolve_claims' THEN
                    SELECT count(*) INTO materialized_candidates
                      FROM core.claims
                     WHERE origin_analysis_result_id = v_analysis_id;
                    SELECT count(*) INTO materialized_locators
                      FROM core.claim_evidence AS evidence
                      JOIN core.claims AS claim
                        ON claim.id = evidence.claim_id
                     WHERE claim.origin_analysis_result_id = v_analysis_id;
                ELSE
                    SELECT count(*) INTO materialized_candidates
                      FROM core.entity_candidates
                     WHERE analysis_result_id = v_analysis_id;
                    SELECT count(*) INTO materialized_locators
                      FROM core.entity_candidate_evidence AS evidence
                      JOIN core.entity_candidates AS candidate
                        ON candidate.id = evidence.entity_candidate_id
                     WHERE candidate.analysis_result_id = v_analysis_id;
                END IF;
                IF p_outcome = 'succeeded'
                   AND (
                        (p_metrics ->> 'materialized_candidates')::integer
                            IS DISTINCT FROM materialized_candidates
                        OR (p_metrics ->> 'materialized_locators')::integer
                            IS DISTINCT FROM materialized_locators
                   ) THEN
                    RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
                END IF;
                IF p_outcome IN ('terminal_failure', 'retryable_failure')
                   AND (materialized_candidates <> 0 OR materialized_locators <> 0) THEN
                    RAISE EXCEPTION 'knowledge_payload_mismatch' USING ERRCODE = '22023';
                END IF;
            EXCEPTION
                WHEN OTHERS THEN
                    IF p_outcome = 'succeeded' THEN
                        RAISE;
                    END IF;
                    IF payload_parsed OR p_error_code IS DISTINCT FROM 'knowledge_payload_mismatch' THEN
                        RAISE;
                    END IF;
            END;

            UPDATE ops.job_attempts
               SET metrics = p_metrics
             WHERE id = p_attempt_id;
            RETURN ops.finish_job(
                p_job_id, p_attempt_id, p_lease_token, p_outcome,
                p_http_status, p_error_code, p_error_summary, p_retry_delay_seconds
            );
        END
        $finish_knowledge_job$;
        """
    )
    revoke_dml = ", ".join(KNOWLEDGE_TABLES)
    op.execute(
        f"""
        SET ROLE uap_owner;
        GRANT SELECT ON core.analysis_results TO uap_worker;
        GRANT SELECT ON core.entity_candidate_evidence
            TO uap_worker, uap_api, uap_publisher, uap_audit_reader, uap_backup;
        REVOKE INSERT, UPDATE, DELETE ON {revoke_dml}
            FROM uap_worker, uap_api;
        REVOKE ALL ON FUNCTION core.require_ai_claim_origin() FROM PUBLIC;
        REVOKE ALL ON FUNCTION core.require_ai_claim_supports() FROM PUBLIC;
        REVOKE ALL ON FUNCTION core.require_valid_entity_origin() FROM PUBLIC;
        REVOKE ALL ON FUNCTION core.require_entity_candidate_evidence() FROM PUBLIC;
        REVOKE ALL ON FUNCTION ops.enqueue_followup_job(uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION core.tg_enqueue_knowledge_followup() FROM PUBLIC;
        REVOKE ALL ON FUNCTION core.compute_evidence_locator_sha256(jsonb) FROM PUBLIC;
        REVOKE ALL ON FUNCTION core.compute_claim_fingerprint(text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION ops.validate_knowledge_attempt_metrics(
            jsonb, ops.attempt_outcome
        ) FROM PUBLIC;
        REVOKE ALL ON FUNCTION ops.reconcile_knowledge_jobs(
            timestamptz, uuid, timestamptz, integer
        ) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION ops.reconcile_knowledge_jobs(
            timestamptz, uuid, timestamptz, integer
        ) TO uap_scheduler;
        REVOKE ALL ON FUNCTION ops.require_active_resolution_job_lease(
            uuid, uuid, uuid, text
        ) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION ops.require_active_resolution_job_lease(
            uuid, uuid, uuid, text
        ) TO uap_worker;
        REVOKE ALL ON FUNCTION ops.finish_knowledge_job(
            uuid, uuid, uuid, ops.attempt_outcome, smallint, text, text, integer, jsonb
        ) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION ops.finish_knowledge_job(
            uuid, uuid, uuid, ops.attempt_outcome, smallint, text, text, integer, jsonb
        ) TO uap_worker;
        RESET ROLE;
        """
    )


def downgrade() -> None:
    restore_dml = ", ".join(ORIGINAL_KNOWLEDGE_TABLES)
    op.execute(
        f"""
        SET ROLE uap_owner;
        DO $protect_multi_evidence$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM core.entity_candidate_evidence
                 GROUP BY entity_candidate_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'knowledge_entity_evidence_downgrade_blocked'
                    USING ERRCODE = '55000';
            END IF;
        END
        $protect_multi_evidence$;

        REVOKE EXECUTE ON FUNCTION ops.finish_knowledge_job(
            uuid, uuid, uuid, ops.attempt_outcome, smallint, text, text, integer, jsonb
        ) FROM uap_worker;
        DROP FUNCTION ops.finish_knowledge_job(
            uuid, uuid, uuid, ops.attempt_outcome, smallint, text, text, integer, jsonb
        );
        REVOKE EXECUTE ON FUNCTION ops.require_active_resolution_job_lease(
            uuid, uuid, uuid, text
        ) FROM uap_worker;
        DROP FUNCTION ops.require_active_resolution_job_lease(uuid, uuid, uuid, text);
        DROP FUNCTION ops.validate_knowledge_attempt_metrics(jsonb, ops.attempt_outcome);
        REVOKE EXECUTE ON FUNCTION ops.reconcile_knowledge_jobs(
            timestamptz, uuid, timestamptz, integer
        ) FROM uap_scheduler;
        DROP FUNCTION ops.reconcile_knowledge_jobs(timestamptz, uuid, timestamptz, integer);
        DROP TRIGGER analysis_results_enqueue_knowledge ON core.analysis_results;
        DROP FUNCTION core.tg_enqueue_knowledge_followup();
        DROP FUNCTION ops.enqueue_followup_job(uuid);
        DROP FUNCTION core.compute_claim_fingerprint(text);
        DROP FUNCTION core.compute_evidence_locator_sha256(jsonb);
        DROP TRIGGER entity_candidate_evidence_required ON core.entity_candidate_evidence;
        DROP TRIGGER entity_candidates_require_evidence ON core.entity_candidates;
        DROP FUNCTION core.require_entity_candidate_evidence();
        DROP TRIGGER entity_candidates_require_valid_origin ON core.entity_candidates;
        DROP FUNCTION core.require_valid_entity_origin();
        DROP TRIGGER claim_evidence_require_supports ON core.claim_evidence;
        DROP TRIGGER claims_require_supports ON core.claims;
        DROP FUNCTION core.require_ai_claim_supports();
        DROP TRIGGER claims_require_ai_origin ON core.claims;
        DROP FUNCTION core.require_ai_claim_origin();
        DROP TABLE core.entity_candidate_evidence;
        ALTER TABLE core.entity_candidates
            DROP CONSTRAINT uq_entity_candidates_id_document;
        ALTER TABLE core.claim_evidence
            DROP CONSTRAINT fk_claim_evidence_span_document,
            DROP CONSTRAINT fk_claim_evidence_claim_document,
            DROP COLUMN document_version_id;
        ALTER TABLE core.claims
            DROP CONSTRAINT ck_claims_manual_actor,
            DROP CONSTRAINT fk_claims_origin_same_document,
            DROP CONSTRAINT uq_claims_id_document,
            DROP CONSTRAINT fk_claims_document_version,
            DROP COLUMN document_version_id;
        GRANT SELECT, INSERT, UPDATE ON {restore_dml} TO uap_worker, uap_api;
        REVOKE SELECT ON core.analysis_results FROM uap_worker;
        RESET ROLE;
        """
    )
