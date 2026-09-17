"""Add the WP10.2 document/entity publication projector.

Revision ID: 0021_wp10_publisher_projection
Revises: 0020_wp10_publication_contract
Create Date: 2026-08-28
"""

from __future__ import annotations

from alembic import op

revision = "0021_wp10_publisher_projection"
down_revision = "0020_wp10_publication_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE TABLE audit.publication_delivery_attempts (
            event_id uuid NOT NULL REFERENCES ops.outbox_events(id),
            attempt_no integer NOT NULL CHECK (attempt_no > 0),
            dispatcher text NOT NULL CHECK (char_length(dispatcher) BETWEEN 1 AND 200),
            lease_token_hash char(64) NOT NULL
                CHECK (lease_token_hash ~ '^[0-9a-f]{64}$'),
            operation_payload_hash char(64)
                CHECK (operation_payload_hash IS NULL OR operation_payload_hash ~ '^[0-9a-f]{64}$'),
            outcome ops.attempt_outcome NOT NULL,
            sanitized_error_code text,
            sanitized_error_summary text,
            projection_digest char(64)
                CHECK (projection_digest IS NULL OR projection_digest ~ '^[0-9a-f]{64}$'),
            started_at timestamptz NOT NULL,
            finished_at timestamptz,
            available_at timestamptz,
            terminal_at timestamptz,
            PRIMARY KEY (event_id, attempt_no),
            CHECK (finished_at IS NULL OR finished_at >= started_at),
            CHECK ((outcome = 'running'::ops.attempt_outcome) = (finished_at IS NULL)),
            CHECK (outcome <> 'succeeded'::ops.attempt_outcome OR projection_digest IS NOT NULL),
            CHECK (outcome <> 'retryable_failure'::ops.attempt_outcome OR terminal_at IS NULL),
            CHECK (outcome <> 'terminal_failure'::ops.attempt_outcome OR terminal_at IS NOT NULL),
            CHECK (sanitized_error_code IS NULL OR char_length(sanitized_error_code) <= 100),
            CHECK (sanitized_error_summary IS NULL OR char_length(sanitized_error_summary) <= 300)
        );
        CREATE INDEX ix_publication_delivery_attempts_event
            ON audit.publication_delivery_attempts(event_id, attempt_no DESC);

        CREATE FUNCTION audit.guard_publication_delivery_attempt_mutation() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = audit, pg_catalog
        AS $guard_publication_delivery_attempt_mutation$
        BEGIN
            IF TG_OP = 'DELETE'
               OR NEW.event_id IS DISTINCT FROM OLD.event_id
               OR NEW.attempt_no IS DISTINCT FROM OLD.attempt_no
               OR NEW.dispatcher IS DISTINCT FROM OLD.dispatcher
               OR NEW.lease_token_hash IS DISTINCT FROM OLD.lease_token_hash
               OR NEW.started_at IS DISTINCT FROM OLD.started_at
            THEN
                RAISE EXCEPTION 'publication delivery-attempt history is immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END
        $guard_publication_delivery_attempt_mutation$;

        CREATE TRIGGER publication_delivery_attempts_immutable
            BEFORE UPDATE OR DELETE ON audit.publication_delivery_attempts
            FOR EACH ROW EXECUTE FUNCTION audit.guard_publication_delivery_attempt_mutation();

        CREATE FUNCTION ops._publication_inject_failure(p_point text) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, pg_catalog
        AS $_publication_inject_failure$
        BEGIN
            IF current_setting('uap.publisher_fail_at', true) = p_point
               OR current_setting('uap.publication_fail_at', true) = p_point
            THEN
                RAISE EXCEPTION 'publication_failure_injected' USING ERRCODE = '40001';
            END IF;
        END
        $_publication_inject_failure$;

        CREATE FUNCTION ops._publication_sanitized_summary(p_error_code text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        STRICT
        SET search_path = pg_catalog
        AS $_publication_sanitized_summary$
            SELECT CASE p_error_code
                WHEN 'publication_dependency_not_ready' THEN 'publication dependency is not ready'
                WHEN 'publication_lease_lost' THEN 'publication lease is no longer valid'
                WHEN 'publication_payload_hash_mismatch' THEN 'publication payload integrity check failed'
                WHEN 'publication_event_schema_unsupported' THEN 'publication event schema is unsupported'
                WHEN 'publication_event_aggregate_mismatch' THEN 'publication event aggregate is invalid'
                WHEN 'publication_grant_missing' THEN 'publication grant is missing'
                WHEN 'publication_grant_state_stale' THEN 'publication grant is stale'
                WHEN 'publication_manifest_invalid' THEN 'publication manifest is invalid'
                WHEN 'publication_retry_exhausted' THEN 'publication retry limit was exhausted'
                WHEN 'publication_database_unavailable' THEN 'publication database dependency failed'
                ELSE NULL
            END
        $_publication_sanitized_summary$;

        CREATE FUNCTION ops.claim_publication_outbox(
            p_dispatcher_id text,
            p_lease_seconds integer,
            p_limit integer DEFAULT 10
        ) RETURNS TABLE (
            event_id uuid,
            causation_job_id uuid,
            aggregate_type text,
            aggregate_id uuid,
            event_type text,
            event_key text,
            payload jsonb,
            attempt_no integer,
            lease_token uuid,
            lease_expires_at timestamptz
        )
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, audit, pg_catalog
        AS $claim_publication_outbox$
        DECLARE
            event_row ops.outbox_events%ROWTYPE;
            v_token uuid;
            v_now timestamptz;
            v_deadline timestamptz;
            v_attempt_no integer;
            v_dispatcher text;
            v_relation_attempt_no integer;
            v_relation_token uuid;
        BEGIN
            IF session_user <> 'uap_publisher' THEN
                RAISE EXCEPTION 'only publisher may claim publication events'
                    USING ERRCODE = '42501';
            END IF;
            IF p_dispatcher_id IS NULL
               OR char_length(btrim(p_dispatcher_id)) NOT BETWEEN 1 AND 200
               OR p_lease_seconds IS NULL
               OR p_lease_seconds NOT BETWEEN 1 AND 86400
               OR p_limit IS NULL
               OR p_limit NOT BETWEEN 1 AND 100
            THEN
                RAISE EXCEPTION 'publication_claim_parameters_invalid'
                    USING ERRCODE = '22023';
            END IF;

            v_dispatcher := btrim(p_dispatcher_id);
            v_now := clock_timestamp();

            -- Relation publication is closed, but an already-enqueued
            -- relation event must not remain an invisible pending event.  The
            -- Publisher claim transaction terminalizes it here without
            -- returning it as a claim and without marking it published.
            FOR event_row IN
                SELECT event_row_candidate.*
                  FROM ops.outbox_events AS event_row_candidate
                 WHERE event_row_candidate.published_at IS NULL
                   AND event_row_candidate.terminal_at IS NULL
                   AND event_row_candidate.available_at <= v_now
                   AND (
                        event_row_candidate.lease_expires_at IS NULL
                        OR event_row_candidate.lease_expires_at <= v_now
                   )
                   AND event_row_candidate.event_type LIKE 'publication.%'
                   AND event_row_candidate.aggregate_type = 'relation_publication_grants'
                 ORDER BY event_row_candidate.occurred_at, event_row_candidate.id
                 FOR UPDATE SKIP LOCKED
                 LIMIT p_limit
            LOOP
                v_relation_attempt_no := event_row.publish_attempts;
                IF v_relation_attempt_no > 0
                   AND EXISTS (
                        SELECT 1
                          FROM audit.publication_delivery_attempts AS delivery_attempt
                         WHERE delivery_attempt.event_id = event_row.id
                           AND delivery_attempt.attempt_no = v_relation_attempt_no
                           AND delivery_attempt.outcome = 'running'::ops.attempt_outcome
                   )
                THEN
                    UPDATE audit.publication_delivery_attempts AS delivery_attempt
                       SET operation_payload_hash = audit._payload_sha256(
                               jsonb_build_object(
                                   'error_code', 'publication_event_schema_unsupported',
                                   'error_summary', ops._publication_sanitized_summary(
                                       'publication_event_schema_unsupported'
                                   ),
                                   'event_id', lower(event_row.id::text),
                                   'op', 'fail',
                                   'retry_delay_seconds', 0,
                                   'terminal', true
                               )
                           ),
                           outcome = 'terminal_failure'::ops.attempt_outcome,
                           sanitized_error_code = 'publication_event_schema_unsupported',
                           sanitized_error_summary = ops._publication_sanitized_summary(
                               'publication_event_schema_unsupported'
                           ),
                           finished_at = v_now,
                           available_at = NULL,
                           terminal_at = v_now
                     WHERE delivery_attempt.event_id = event_row.id
                       AND delivery_attempt.attempt_no = v_relation_attempt_no;
                ELSE
                    v_relation_attempt_no := event_row.publish_attempts + 1;
                    v_relation_token := gen_random_uuid();
                    INSERT INTO audit.publication_delivery_attempts (
                        event_id, attempt_no, dispatcher, lease_token_hash,
                        operation_payload_hash, outcome, sanitized_error_code,
                        sanitized_error_summary, started_at, finished_at, terminal_at
                    ) VALUES (
                        event_row.id,
                        v_relation_attempt_no,
                        v_dispatcher,
                        audit._payload_sha256(
                            jsonb_build_object(
                                'lease_token', lower(v_relation_token::text)
                            )
                        ),
                        audit._payload_sha256(
                            jsonb_build_object(
                                'error_code', 'publication_event_schema_unsupported',
                                'error_summary', ops._publication_sanitized_summary(
                                    'publication_event_schema_unsupported'
                                ),
                                'event_id', lower(event_row.id::text),
                                'op', 'fail',
                                'retry_delay_seconds', 0,
                                'terminal', true
                            )
                        ),
                        'terminal_failure'::ops.attempt_outcome,
                        'publication_event_schema_unsupported',
                        ops._publication_sanitized_summary(
                            'publication_event_schema_unsupported'
                        ),
                        v_now,
                        v_now,
                        v_now
                    );
                END IF;

                UPDATE ops.outbox_events AS outbox
                   SET publish_attempts = v_relation_attempt_no,
                       lease_owner = NULL,
                       lease_token = NULL,
                       lease_expires_at = NULL,
                       last_error_code = 'publication_event_schema_unsupported',
                       last_error_summary = ops._publication_sanitized_summary(
                           'publication_event_schema_unsupported'
                       ),
                       terminal_at = v_now,
                       terminal_error_code = 'publication_event_schema_unsupported'
                 WHERE outbox.id = event_row.id
                   AND outbox.published_at IS NULL
                   AND outbox.terminal_at IS NULL;
            END LOOP;

            FOR event_row IN
                SELECT event_row_candidate.*
                  FROM ops.outbox_events AS event_row_candidate
                 WHERE event_row_candidate.published_at IS NULL
                   AND event_row_candidate.terminal_at IS NULL
                   AND event_row_candidate.available_at <= v_now
                   AND (
                        event_row_candidate.lease_expires_at IS NULL
                        OR event_row_candidate.lease_expires_at <= v_now
                   )
                   AND event_row_candidate.event_type IN (
                        'publication.granted',
                        'publication.superseded',
                        'publication.withdrawn'
                   )
                   AND event_row_candidate.aggregate_type IN (
                        'document_publication_grants',
                        'entity_publication_grants'
                   )
                   AND jsonb_typeof(event_row_candidate.payload) = 'object'
                   AND event_row_candidate.payload ->> 'schema' = 'publication-outbox.v2'
                   AND (
                        (
                            event_row_candidate.aggregate_type = 'document_publication_grants'
                            AND event_row_candidate.payload ->> 'subject_type' = 'document'
                        ) OR (
                            event_row_candidate.aggregate_type = 'entity_publication_grants'
                            AND event_row_candidate.payload ->> 'subject_type' = 'entity'
                        )
                   )
                   AND (
                        event_row_candidate.payload - ARRAY[
                            'schema', 'grant_id', 'decision_id', 'subject_type',
                            'subject_id', 'revision_no', 'payload_sha256', 'old_grant_id'
                        ]
                   ) = '{}'::jsonb
                   AND event_row_candidate.payload ? 'grant_id'
                   AND event_row_candidate.payload ? 'decision_id'
                   AND event_row_candidate.payload ? 'subject_id'
                   AND event_row_candidate.payload ? 'revision_no'
                   AND event_row_candidate.payload ? 'payload_sha256'
                   AND (
                        (event_row_candidate.event_type = 'publication.superseded')
                        = (event_row_candidate.payload ? 'old_grant_id')
                 )
                 ORDER BY event_row_candidate.occurred_at, event_row_candidate.id
                 FOR UPDATE SKIP LOCKED
                 LIMIT p_limit
            LOOP
                v_token := gen_random_uuid();
                v_deadline := v_now + make_interval(secs => p_lease_seconds);
                v_attempt_no := event_row.publish_attempts + 1;

                IF event_row.publish_attempts > 0 THEN
                    UPDATE audit.publication_delivery_attempts AS delivery_attempt
                       SET outcome = 'retryable_failure'::ops.attempt_outcome,
                           sanitized_error_code = 'publication_lease_lost',
                           sanitized_error_summary = ops._publication_sanitized_summary(
                               'publication_lease_lost'
                           ),
                           finished_at = v_now,
                           available_at = v_now
                     WHERE delivery_attempt.event_id = event_row.id
                       AND delivery_attempt.attempt_no = event_row.publish_attempts
                       AND delivery_attempt.outcome = 'running'::ops.attempt_outcome;
                END IF;

                UPDATE ops.outbox_events
                   SET lease_owner = v_dispatcher,
                       lease_token = v_token,
                       lease_expires_at = v_deadline,
                       publish_attempts = v_attempt_no
                 WHERE id = event_row.id;

                INSERT INTO audit.publication_delivery_attempts (
                    event_id, attempt_no, dispatcher, lease_token_hash,
                    outcome, started_at
                ) VALUES (
                    event_row.id,
                    v_attempt_no,
                    v_dispatcher,
                    audit._payload_sha256(
                        jsonb_build_object('lease_token', lower(v_token::text))
                    ),
                    'running'::ops.attempt_outcome,
                    v_now
                );

                event_id := event_row.id;
                causation_job_id := event_row.causation_job_id;
                aggregate_type := event_row.aggregate_type;
                aggregate_id := event_row.aggregate_id;
                event_type := event_row.event_type;
                event_key := event_row.event_key;
                payload := event_row.payload;
                attempt_no := v_attempt_no;
                lease_token := v_token;
                lease_expires_at := v_deadline;
                RETURN NEXT;
            END LOOP;
        END
        $claim_publication_outbox$;

        CREATE FUNCTION ops.apply_publication_event(
            p_event_id uuid,
            p_lease_token uuid
        ) RETURNS TABLE (
            event_id uuid,
            published_at timestamptz,
            projection_digest char(64),
            attempt_no integer,
            replayed boolean
        )
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, audit, core, ingest, pg_catalog
        SET timezone = 'UTC'
        AS $apply_publication_event$
        DECLARE
            event_row ops.outbox_events%ROWTYPE;
            attempt_row audit.publication_delivery_attempts%ROWTYPE;
            v_operation_hash text;
            v_lease_hash text;
            v_now timestamptz;
            v_grant_id uuid;
            v_old_grant_id uuid;
            v_subject_id uuid;
            v_subject_type text;
            v_event_decision_id uuid;
            v_schema text;
            v_payload jsonb;
            v_payload_sha text;
            v_revision integer;
            v_grant_revision integer;
            v_grant_status audit.grant_status;
            v_withdrawn_decision_id uuid;
            v_manifest_sha text;
            v_recomputed_sha text;
            v_manifest jsonb;
            v_document_id uuid;
            v_public_id uuid;
            v_slug text;
            v_current_revision integer;
            v_current_grant_id uuid;
            v_projection_digest text;
            v_present boolean;
            v_replayed boolean := false;
            v_public_published_at timestamptz;
            v_public_revised_at timestamptz;
            v_category public.document_category;
            v_fact_status public.fact_status;
            v_title text;
            v_summary text;
            v_source_name text;
            v_source_url text;
            v_source_published_at timestamptz;
            v_summary_result uuid;
            v_entity_type core.entity_type;
            v_entity_status core.entity_status;
            v_entity_canonical_id uuid;
            v_entity_is_active_canonical boolean := true;
            v_entity_name text;
            v_entity_description text;
            v_country_code char(2);
            v_table text;
            v_manifest_grant_id uuid;
            v_manifest_decision_id uuid;
            v_manifest_subject_id uuid;
            v_manifest_document_version_id uuid;
        BEGIN
            IF session_user <> 'uap_publisher' THEN
                RAISE EXCEPTION 'only publisher may apply publication events'
                    USING ERRCODE = '42501';
            END IF;
            IF p_event_id IS NULL OR p_lease_token IS NULL THEN
                RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
            END IF;

            v_operation_hash := audit._payload_sha256(
                jsonb_build_object('event_id', lower(p_event_id::text), 'op', 'apply')
            );
            v_lease_hash := audit._payload_sha256(
                jsonb_build_object('lease_token', lower(p_lease_token::text))
            );
            SELECT * INTO event_row
              FROM ops.outbox_events
             WHERE id = p_event_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
            END IF;

            SELECT * INTO attempt_row
              FROM audit.publication_delivery_attempts AS attempt
             WHERE attempt.event_id = p_event_id
               AND attempt.lease_token_hash = v_lease_hash
             ORDER BY attempt.attempt_no DESC
            LIMIT 1;
            IF FOUND THEN
                IF event_row.publish_attempts > attempt_row.attempt_no THEN
                    RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
                END IF;
                IF attempt_row.operation_payload_hash IS NOT NULL THEN
                    IF attempt_row.operation_payload_hash IS DISTINCT FROM v_operation_hash THEN
                        RAISE EXCEPTION 'publication_delivery_attempt_conflict'
                            USING ERRCODE = '40001';
                    END IF;
                    IF attempt_row.outcome = 'succeeded'::ops.attempt_outcome
                       AND event_row.published_at IS NOT NULL
                    THEN
                        v_replayed := true;
                    ELSE
                        RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
                    END IF;
                ELSIF attempt_row.outcome <> 'running'::ops.attempt_outcome THEN
                    RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
                END IF;
            END IF;

            IF NOT v_replayed THEN
                v_now := clock_timestamp();
                IF event_row.published_at IS NOT NULL
                   OR event_row.terminal_at IS NOT NULL
                   OR event_row.lease_token IS DISTINCT FROM p_lease_token
                   OR event_row.lease_expires_at IS NULL
                   OR event_row.lease_expires_at <= v_now
                THEN
                    RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
                END IF;
                SELECT * INTO attempt_row
                  FROM audit.publication_delivery_attempts AS attempt
                 WHERE attempt.event_id = p_event_id
                   AND attempt.attempt_no = event_row.publish_attempts
                 FOR UPDATE;
                IF NOT FOUND OR attempt_row.lease_token_hash IS DISTINCT FROM v_lease_hash
                   OR attempt_row.outcome <> 'running'::ops.attempt_outcome
                THEN
                    RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
                END IF;
                PERFORM ops._publication_inject_failure('attempt');
                UPDATE audit.publication_delivery_attempts AS delivery_attempt
                   SET operation_payload_hash = v_operation_hash
                 WHERE delivery_attempt.event_id = p_event_id
                   AND delivery_attempt.attempt_no = attempt_row.attempt_no;
            END IF;

            v_payload := event_row.payload;
            v_schema := v_payload ->> 'schema';
            v_subject_type := v_payload ->> 'subject_type';
            IF jsonb_typeof(v_payload) IS DISTINCT FROM 'object'
               OR v_schema IS DISTINCT FROM 'publication-outbox.v2'
               OR event_row.event_type NOT IN (
                    'publication.granted',
                    'publication.superseded',
                    'publication.withdrawn'
               )
               OR v_subject_type NOT IN ('document', 'entity')
               OR (event_row.aggregate_type, v_subject_type) NOT IN (
                    ('document_publication_grants', 'document'),
                    ('entity_publication_grants', 'entity')
               )
               OR (v_payload - ARRAY[
                    'schema', 'grant_id', 'decision_id', 'subject_type',
                    'subject_id', 'revision_no', 'payload_sha256', 'old_grant_id'
                  ]) <> '{}'::jsonb
               OR (event_row.event_type <> 'publication.superseded'
                   AND v_payload ? 'old_grant_id')
               OR (event_row.event_type = 'publication.superseded'
                   AND NOT v_payload ? 'old_grant_id')
            THEN
                RAISE EXCEPTION 'publication_event_schema_unsupported'
                    USING ERRCODE = '22023';
            END IF;

            BEGIN
                v_grant_id := (v_payload ->> 'grant_id')::uuid;
                v_event_decision_id := (v_payload ->> 'decision_id')::uuid;
                v_subject_id := (v_payload ->> 'subject_id')::uuid;
                v_revision := (v_payload ->> 'revision_no')::integer;
                v_payload_sha := v_payload ->> 'payload_sha256';
                IF event_row.event_type = 'publication.superseded' THEN
                    v_old_grant_id := (v_payload ->> 'old_grant_id')::uuid;
                END IF;
            EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                RAISE EXCEPTION 'publication_event_schema_unsupported'
                    USING ERRCODE = '22023';
            END;
            IF v_grant_id IS NULL OR v_event_decision_id IS NULL
               OR v_subject_id IS NULL OR v_revision IS NULL
               OR v_revision < 1 OR v_payload_sha IS NULL
               OR v_payload_sha !~ '^[0-9a-f]{64}$'
            THEN
                RAISE EXCEPTION 'publication_event_schema_unsupported'
                    USING ERRCODE = '22023';
            END IF;
            PERFORM pg_advisory_xact_lock(
                hashtextextended(
                    'publication-' || v_subject_type || ':' || v_subject_id::text,
                    0
                )
            );
            IF (event_row.event_type IN ('publication.granted', 'publication.withdrawn')
                AND event_row.aggregate_id IS DISTINCT FROM v_grant_id)
               OR (event_row.event_type = 'publication.superseded'
                   AND event_row.aggregate_id IS DISTINCT FROM v_old_grant_id)
            THEN
                RAISE EXCEPTION 'publication_event_aggregate_mismatch'
                    USING ERRCODE = '22023';
            END IF;
            IF v_subject_type = 'document' THEN
                v_table := 'document_publication_grants';
                SELECT grant_row.document_version_id, grant_row.revision_no,
                       grant_row.grant_status, grant_row.withdrawn_by_decision_id,
                       grant_row.publication_payload_sha256,
                       manifest.grant_id, manifest.decision_id, manifest.document_id,
                       manifest.document_version_id, manifest.title, manifest.summary,
                       manifest.category, manifest.fact_status, manifest.source_name,
                       manifest.canonical_source_url, manifest.source_published_at,
                       manifest.summary_analysis_result_id, manifest.manifest_sha256
                  INTO v_manifest_subject_id, v_grant_revision, v_grant_status,
                       v_withdrawn_decision_id, v_manifest_sha, v_manifest_grant_id,
                       v_manifest_decision_id,
                       v_document_id, v_manifest_document_version_id, v_title, v_summary,
                       v_category, v_fact_status, v_source_name, v_source_url,
                       v_source_published_at, v_summary_result, v_recomputed_sha
                  FROM audit.document_publication_grants AS grant_row
                  JOIN audit.document_publication_manifests AS manifest
                    ON manifest.grant_id = grant_row.id
                 WHERE grant_row.id = v_grant_id;
            ELSE
                v_table := 'entity_publication_grants';
                SELECT grant_row.entity_id, grant_row.revision_no,
                       grant_row.grant_status, grant_row.withdrawn_by_decision_id,
                       grant_row.publication_payload_sha256,
                       manifest.grant_id, manifest.decision_id, manifest.entity_id,
                       manifest.entity_id, manifest.entity_type, manifest.canonical_name,
                       manifest.description, manifest.country_code, manifest.manifest_sha256
                  INTO v_manifest_subject_id, v_grant_revision, v_grant_status,
                       v_withdrawn_decision_id, v_manifest_sha, v_manifest_grant_id,
                       v_manifest_decision_id,
                       v_document_id, v_manifest_document_version_id, v_entity_type,
                       v_entity_name, v_entity_description, v_country_code, v_recomputed_sha
                  FROM audit.entity_publication_grants AS grant_row
                  JOIN audit.entity_publication_manifests AS manifest
                    ON manifest.grant_id = grant_row.id
                 WHERE grant_row.id = v_grant_id;
            END IF;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'publication_grant_missing' USING ERRCODE = '22023';
            END IF;
            IF v_subject_type = 'document' THEN
                v_manifest := jsonb_build_object(
                    'schema', 'publication-manifest.v2',
                    'subject_type', 'document',
                    'grant_id', lower(v_manifest_grant_id::text),
                    'decision_id', lower(v_manifest_decision_id::text),
                    'subject_id', lower(v_manifest_subject_id::text),
                    'document_id', lower(v_document_id::text),
                    'document_version_id', lower(v_manifest_document_version_id::text),
                    'revision_no', v_grant_revision,
                    'title', v_title,
                    'summary', to_jsonb(v_summary),
                    'category', v_category::text,
                    'fact_status', v_fact_status::text,
                    'source_name', v_source_name,
                    'canonical_source_url', v_source_url,
                    'source_published_at', CASE
                        WHEN v_source_published_at IS NULL THEN 'null'::jsonb
                        ELSE to_jsonb(to_char(
                            v_source_published_at AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                        ))
                    END,
                    'summary_analysis_result_id', to_jsonb(v_summary_result)
                );
            ELSE
                v_manifest := jsonb_build_object(
                    'schema', 'publication-manifest.v2',
                    'subject_type', 'entity',
                    'grant_id', lower(v_manifest_grant_id::text),
                    'decision_id', lower(v_manifest_decision_id::text),
                    'subject_id', lower(v_manifest_subject_id::text),
                    'revision_no', v_grant_revision,
                    'entity_type', v_entity_type::text,
                    'canonical_name', v_entity_name,
                    'description', to_jsonb(v_entity_description),
                    'country_code', to_jsonb(v_country_code)
                );
            END IF;
            v_recomputed_sha := audit._publication_manifest_sha(v_manifest);
            IF v_manifest_subject_id IS DISTINCT FROM v_subject_id
               OR v_manifest_grant_id IS DISTINCT FROM v_grant_id
               OR v_grant_revision IS DISTINCT FROM v_revision
               OR (
                    event_row.event_type <> 'publication.withdrawn'
                    AND v_event_decision_id IS DISTINCT FROM v_manifest_decision_id
               )
               OR v_payload_sha IS DISTINCT FROM btrim(v_manifest_sha::text)
               OR btrim(v_manifest_sha::text) IS DISTINCT FROM btrim(v_recomputed_sha::text)
            THEN
                RAISE EXCEPTION 'publication_payload_hash_mismatch'
                    USING ERRCODE = '22023';
            END IF;

            IF NOT v_replayed
               AND v_subject_type = 'entity'
               AND event_row.event_type <> 'publication.withdrawn'
            THEN
                -- Merge/retire may occur after the review grant is frozen but
                -- before delivery.  Lock the source row before resolving its
                -- canonical target so a concurrent merge cannot race the
                -- public upsert.
                SELECT entity.status
                  INTO v_entity_status
                  FROM core.entities AS entity
                 WHERE entity.id = v_manifest_subject_id
                 FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'publication_grant_missing' USING ERRCODE = '22023';
                END IF;
                v_entity_canonical_id := core.canonical_entity_id(v_manifest_subject_id);
                v_entity_is_active_canonical :=
                    v_entity_status = 'active'::core.entity_status
                    AND v_entity_canonical_id = v_manifest_subject_id;
            END IF;

            IF event_row.event_type = 'publication.superseded' THEN
                IF v_old_grant_id IS NULL THEN
                    RAISE EXCEPTION 'publication_event_aggregate_mismatch'
                        USING ERRCODE = '22023';
                END IF;
                IF v_subject_type = 'document' THEN
                    IF NOT EXISTS (
                        SELECT 1 FROM audit.document_publication_grants AS old_grant
                         WHERE old_grant.id = v_old_grant_id
                           AND old_grant.document_version_id = v_subject_id
                           AND old_grant.grant_status = 'superseded'::audit.grant_status
                           AND old_grant.revision_no < v_revision
                    ) THEN
                        RAISE EXCEPTION 'publication_event_aggregate_mismatch'
                            USING ERRCODE = '22023';
                    END IF;
                ELSIF NOT EXISTS (
                    SELECT 1 FROM audit.entity_publication_grants AS old_grant
                     WHERE old_grant.id = v_old_grant_id
                       AND old_grant.entity_id = v_subject_id
                       AND old_grant.grant_status = 'superseded'::audit.grant_status
                       AND old_grant.revision_no < v_revision
                ) THEN
                    RAISE EXCEPTION 'publication_event_aggregate_mismatch'
                        USING ERRCODE = '22023';
                END IF;
            END IF;

            IF v_replayed THEN
                IF v_subject_type = 'document' THEN
                    SELECT identity.public_id INTO v_public_id
                      FROM audit.document_public_identities AS identity
                     WHERE identity.document_id = v_document_id;
                ELSE
                    SELECT identity.public_id INTO v_public_id
                      FROM audit.entity_public_identities AS identity
                     WHERE identity.entity_id = v_manifest_subject_id;
                END IF;
            ELSIF event_row.event_type = 'publication.granted'
               AND v_grant_status <> 'active'::audit.grant_status
            THEN
                -- A grant withdrawn or superseded before its delivery is a
                -- valid stale event.  It is acknowledged without making it
                -- visible, while the newer event remains authoritative.
                IF v_subject_type = 'document' THEN
                    SELECT identity.public_id INTO v_public_id
                      FROM audit.document_public_identities AS identity
                     WHERE identity.document_id = v_document_id;
                ELSE
                    SELECT identity.public_id INTO v_public_id
                      FROM audit.entity_public_identities AS identity
                     WHERE identity.entity_id = v_manifest_subject_id;
                END IF;
                v_present := false;
            ELSIF event_row.event_type = 'publication.superseded'
               AND v_grant_status <> 'active'::audit.grant_status
            THEN
                IF v_subject_type = 'document' THEN
                    SELECT identity.public_id INTO v_public_id
                      FROM audit.document_public_identities AS identity
                     WHERE identity.document_id = v_document_id;
                ELSE
                    SELECT identity.public_id INTO v_public_id
                      FROM audit.entity_public_identities AS identity
                     WHERE identity.entity_id = v_manifest_subject_id;
                END IF;
                v_present := false;
            ELSIF event_row.event_type = 'publication.withdrawn' THEN
                IF v_subject_type = 'document' THEN
                    SELECT identity.public_id INTO v_public_id
                      FROM audit.document_public_identities AS identity
                     WHERE identity.document_id = v_document_id;
                ELSE
                    SELECT identity.public_id INTO v_public_id
                      FROM audit.entity_public_identities AS identity
                     WHERE identity.entity_id = v_manifest_subject_id;
                END IF;
                v_present := false;
                IF v_public_id IS NOT NULL THEN
                    IF v_subject_type = 'document' THEN
                        SELECT document.revision_no, document.document_grant_id
                          INTO v_current_revision, v_current_grant_id
                          FROM public.documents AS document
                         WHERE document.id = v_public_id
                         FOR UPDATE;
                        IF FOUND AND v_grant_status = 'withdrawn'::audit.grant_status
                           AND v_withdrawn_decision_id = v_event_decision_id
                           AND v_current_revision = v_revision
                           AND v_current_grant_id = v_grant_id
                        THEN
                            DELETE FROM public.documents WHERE id = v_public_id;
                        END IF;
                    ELSE
                        SELECT entity.revision_no, entity.entity_grant_id
                          INTO v_current_revision, v_current_grant_id
                          FROM public.entities AS entity
                         WHERE entity.id = v_public_id
                         FOR UPDATE;
                        IF FOUND AND v_grant_status = 'withdrawn'::audit.grant_status
                           AND v_withdrawn_decision_id = v_event_decision_id
                           AND v_current_revision = v_revision
                           AND v_current_grant_id = v_grant_id
                        THEN
                            DELETE FROM public.entities WHERE id = v_public_id;
                        END IF;
                    END IF;
                END IF;
            ELSIF v_subject_type = 'entity' AND NOT v_entity_is_active_canonical THEN
                -- A grant for an entity that is no longer active/canonical is
                -- stale.  Acknowledge it without creating an identity or a
                -- public row; a later authoritative lifecycle event owns
                -- visibility.
                v_public_id := NULL;
                v_present := false;
            ELSE
                IF v_subject_type = 'document' THEN
                    PERFORM pg_advisory_xact_lock(
                        hashtextextended('publication-document:' || v_document_id::text, 0)
                    );
                    SELECT identity.public_id, identity.slug
                      INTO v_public_id, v_slug
                      FROM audit.document_public_identities AS identity
                     WHERE identity.document_id = v_document_id
                     FOR SHARE;
                    IF NOT FOUND THEN
                        PERFORM ops._publication_inject_failure('identity');
                        v_public_id := gen_random_uuid();
                        v_slug := 'd-' || replace(v_public_id::text, '-', '');
                        INSERT INTO audit.document_public_identities (
                            document_id, public_id, slug
                        ) VALUES (v_document_id, v_public_id, v_slug);
                    END IF;
                    SELECT document.revision_no INTO v_current_revision
                      FROM public.documents AS document
                     WHERE document.id = v_public_id
                     FOR UPDATE;
                    IF v_current_revision IS NOT NULL AND v_current_revision > v_revision THEN
                        v_present := true;
                    ELSE
                        PERFORM ops._publication_inject_failure('public_upsert');
                        v_public_published_at := clock_timestamp();
                        INSERT INTO public.documents (
                            id, document_grant_id, slug, title, summary, category,
                            fact_status, source_name, canonical_source_url,
                            source_published_at, published_at, revised_at, revision_no
                        ) VALUES (
                            v_public_id, v_grant_id, v_slug, v_title, v_summary, v_category,
                            v_fact_status, v_source_name, v_source_url, v_source_published_at,
                            v_public_published_at, NULL, v_revision
                        )
                        ON CONFLICT (id) DO UPDATE SET
                            document_grant_id = EXCLUDED.document_grant_id,
                            slug = EXCLUDED.slug,
                            title = EXCLUDED.title,
                            summary = EXCLUDED.summary,
                            category = EXCLUDED.category,
                            fact_status = EXCLUDED.fact_status,
                            source_name = EXCLUDED.source_name,
                            canonical_source_url = EXCLUDED.canonical_source_url,
                            source_published_at = EXCLUDED.source_published_at,
                            revised_at = CASE
                                WHEN public.documents.revision_no < EXCLUDED.revision_no
                                THEN v_public_published_at
                                ELSE public.documents.revised_at
                            END,
                            revision_no = EXCLUDED.revision_no
                         WHERE public.documents.revision_no <= EXCLUDED.revision_no;
                        v_present := true;
                    END IF;
                ELSE
                    PERFORM pg_advisory_xact_lock(
                        hashtextextended('publication-entity:' || v_manifest_subject_id::text, 0)
                    );
                    SELECT identity.public_id, identity.slug
                      INTO v_public_id, v_slug
                      FROM audit.entity_public_identities AS identity
                     WHERE identity.entity_id = v_manifest_subject_id
                     FOR SHARE;
                    IF NOT FOUND THEN
                        PERFORM ops._publication_inject_failure('identity');
                        v_public_id := gen_random_uuid();
                        v_slug := 'e-' || replace(v_public_id::text, '-', '');
                        INSERT INTO audit.entity_public_identities (
                            entity_id, public_id, slug
                        ) VALUES (v_manifest_subject_id, v_public_id, v_slug);
                    END IF;
                    SELECT entity.revision_no INTO v_current_revision
                      FROM public.entities AS entity
                     WHERE entity.id = v_public_id
                     FOR UPDATE;
                    IF v_current_revision IS NOT NULL AND v_current_revision > v_revision THEN
                        v_present := true;
                    ELSE
                        PERFORM ops._publication_inject_failure('public_upsert');
                        v_public_published_at := clock_timestamp();
                        INSERT INTO public.entities (
                            id, entity_grant_id, slug, entity_type, name, description,
                            country_code, published_at, revision_no
                        ) VALUES (
                            v_public_id, v_grant_id, v_slug, v_entity_type, v_entity_name,
                            v_entity_description, v_country_code, v_public_published_at, v_revision
                        )
                        ON CONFLICT (id) DO UPDATE SET
                            entity_grant_id = EXCLUDED.entity_grant_id,
                            slug = EXCLUDED.slug,
                            entity_type = EXCLUDED.entity_type,
                            name = EXCLUDED.name,
                            description = EXCLUDED.description,
                            country_code = EXCLUDED.country_code,
                            revision_no = EXCLUDED.revision_no
                         WHERE public.entities.revision_no <= EXCLUDED.revision_no;
                        v_present := true;
                    END IF;
                END IF;
            END IF;

            IF v_public_id IS NULL THEN
                v_projection_digest := audit._payload_sha256(
                    jsonb_build_object(
                        'present', false,
                        'subject_type', v_subject_type,
                        'subject_id', lower(v_subject_id::text)
                    )
                );
            ELSIF v_subject_type = 'document' THEN
                SELECT jsonb_build_object(
                           'category', document.category::text,
                           'canonical_source_url', document.canonical_source_url,
                           'document_grant_id', lower(document.document_grant_id::text),
                           'fact_status', document.fact_status::text,
                           'id', lower(document.id::text),
                           'revision_no', document.revision_no,
                           'slug', document.slug,
                           'source_name', document.source_name,
                           'source_published_at', CASE
                               WHEN document.source_published_at IS NULL THEN 'null'::jsonb
                               ELSE to_jsonb(to_char(
                                   document.source_published_at AT TIME ZONE 'UTC',
                                   'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                               ))
                           END,
                           'summary', document.summary,
                           'title', document.title
                       )
                  INTO v_manifest
                  FROM public.documents AS document
                 WHERE document.id = v_public_id;
                IF FOUND THEN
                    v_projection_digest := audit._payload_sha256(v_manifest);
                ELSE
                    v_projection_digest := audit._payload_sha256(
                        jsonb_build_object(
                            'present', false,
                            'subject_type', v_subject_type,
                            'subject_id', lower(v_subject_id::text)
                        )
                    );
                END IF;
            ELSE
                SELECT jsonb_build_object(
                           'entity_type', entity.entity_type::text,
                           'country_code', entity.country_code,
                           'description', entity.description,
                           'entity_grant_id', lower(entity.entity_grant_id::text),
                           'id', lower(entity.id::text),
                           'name', entity.name,
                           'revision_no', entity.revision_no,
                           'slug', entity.slug
                       )
                  INTO v_manifest
                  FROM public.entities AS entity
                 WHERE entity.id = v_public_id;
                IF FOUND THEN
                    v_projection_digest := audit._payload_sha256(v_manifest);
                ELSE
                    v_projection_digest := audit._payload_sha256(
                        jsonb_build_object(
                            'present', false,
                            'subject_type', v_subject_type,
                            'subject_id', lower(v_subject_id::text)
                        )
                    );
                END IF;
            END IF;

            IF v_replayed THEN
                IF v_projection_digest IS DISTINCT FROM attempt_row.projection_digest THEN
                    RAISE EXCEPTION 'publication_payload_hash_mismatch'
                        USING ERRCODE = '22023';
                END IF;
                event_id := p_event_id;
                published_at := event_row.published_at;
                projection_digest := attempt_row.projection_digest;
                attempt_no := attempt_row.attempt_no;
                replayed := true;
                RETURN NEXT;
                RETURN;
            END IF;

            PERFORM ops._publication_inject_failure('deferred_constraint');
            SET CONSTRAINTS ALL IMMEDIATE;
            PERFORM ops._publication_inject_failure('ack');
            v_now := clock_timestamp();
            UPDATE audit.publication_delivery_attempts AS delivery_attempt
               SET outcome = 'succeeded'::ops.attempt_outcome,
                   projection_digest = v_projection_digest,
                   finished_at = v_now,
                   sanitized_error_code = NULL,
                   sanitized_error_summary = NULL
             WHERE delivery_attempt.event_id = p_event_id
               AND delivery_attempt.attempt_no = attempt_row.attempt_no;
            UPDATE ops.outbox_events AS outbox
               SET published_at = v_now,
                   lease_owner = NULL,
                   lease_token = NULL,
                   lease_expires_at = NULL,
                   last_error_code = NULL,
                   last_error_summary = NULL
             WHERE outbox.id = p_event_id
               AND outbox.published_at IS NULL
               AND outbox.terminal_at IS NULL
               AND outbox.lease_token = p_lease_token
               AND outbox.lease_expires_at > v_now;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
            END IF;

            event_id := p_event_id;
            published_at := v_now;
            projection_digest := v_projection_digest;
            attempt_no := attempt_row.attempt_no;
            replayed := false;
            RETURN NEXT;
        END
        $apply_publication_event$;

        CREATE FUNCTION ops.fail_publication_event(
            p_event_id uuid,
            p_lease_token uuid,
            p_error_code text,
            p_error_summary text,
            p_retry_delay_seconds integer DEFAULT 5,
            p_terminal boolean DEFAULT false
        ) RETURNS TABLE (
            event_id uuid,
            attempt_no integer,
            outcome text,
            available_at timestamptz,
            terminal_at timestamptz,
            replayed boolean
        )
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, audit, pg_catalog
        AS $fail_publication_event$
        DECLARE
            event_row ops.outbox_events%ROWTYPE;
            attempt_row audit.publication_delivery_attempts%ROWTYPE;
            v_lease_hash text;
            v_operation_hash text;
            v_summary text;
            v_effective_code text;
            v_outcome ops.attempt_outcome;
            v_replayable boolean := false;
            v_now timestamptz;
            v_available_at timestamptz;
            v_terminal_at timestamptz;
            v_retry_seconds integer;
        BEGIN
            IF session_user <> 'uap_publisher' THEN
                RAISE EXCEPTION 'only publisher may fail publication events'
                    USING ERRCODE = '42501';
            END IF;
            -- Resolve the event and lease before inspecting any caller-owned
            -- error fields.  A forged, expired, or otherwise stale token
            -- must always produce the lease error, even with bad input.
            IF p_event_id IS NULL OR p_lease_token IS NULL THEN
                RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
            END IF;
            v_lease_hash := audit._payload_sha256(
                jsonb_build_object('lease_token', lower(p_lease_token::text))
            );
            SELECT * INTO event_row
              FROM ops.outbox_events
             WHERE id = p_event_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
            END IF;
            SELECT * INTO attempt_row
              FROM audit.publication_delivery_attempts AS attempt
             WHERE attempt.event_id = p_event_id
               AND attempt.lease_token_hash = v_lease_hash
             ORDER BY attempt.attempt_no DESC
            LIMIT 1;
            IF FOUND THEN
                IF event_row.publish_attempts > attempt_row.attempt_no THEN
                    RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
                END IF;
                IF attempt_row.operation_payload_hash IS NOT NULL
                   AND attempt_row.outcome IN (
                        'retryable_failure'::ops.attempt_outcome,
                        'terminal_failure'::ops.attempt_outcome,
                        'succeeded'::ops.attempt_outcome
                   )
                THEN
                    v_replayable := true;
                ELSIF attempt_row.outcome <> 'running'::ops.attempt_outcome THEN
                    RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
                END IF;
            END IF;

            v_now := clock_timestamp();
            IF NOT v_replayable THEN
                IF event_row.published_at IS NOT NULL
                   OR event_row.terminal_at IS NOT NULL
                   OR event_row.lease_token IS DISTINCT FROM p_lease_token
                   OR event_row.lease_expires_at IS NULL
                   OR event_row.lease_expires_at <= v_now
                THEN
                    RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
                END IF;
                SELECT * INTO attempt_row
                  FROM audit.publication_delivery_attempts AS attempt
                 WHERE attempt.event_id = p_event_id
                   AND attempt.attempt_no = event_row.publish_attempts
                 FOR UPDATE;
                IF NOT FOUND OR attempt_row.lease_token_hash IS DISTINCT FROM v_lease_hash
                   OR attempt_row.outcome <> 'running'::ops.attempt_outcome
                THEN
                    RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
                END IF;
            END IF;

            -- The fixed summary is checked only after the lease has been
            -- authenticated.  char_length counts PostgreSQL characters/code
            -- points, not bytes.
            IF p_error_code IS NULL
               OR char_length(btrim(p_error_code)) = 0
               OR p_retry_delay_seconds IS NULL
               OR p_retry_delay_seconds NOT BETWEEN 0 AND 3600
               OR p_terminal IS NULL
            THEN
                RAISE EXCEPTION 'publication_failure_parameters_invalid'
                    USING ERRCODE = '22023';
            END IF;
            v_summary := ops._publication_sanitized_summary(btrim(p_error_code));
            IF v_summary IS NULL
               OR p_error_summary IS NULL
               OR char_length(p_error_summary) > 300
               OR p_error_summary IS DISTINCT FROM v_summary
            THEN
                RAISE EXCEPTION 'publication_failure_parameters_invalid'
                    USING ERRCODE = '22023';
            END IF;
            v_operation_hash := audit._payload_sha256(
                jsonb_build_object(
                    'error_code', btrim(p_error_code),
                    'error_summary', v_summary,
                    'event_id', lower(p_event_id::text),
                    'op', 'fail',
                    'retry_delay_seconds', p_retry_delay_seconds,
                    'terminal', p_terminal
                )
            );
            IF v_replayable THEN
                IF attempt_row.operation_payload_hash IS DISTINCT FROM v_operation_hash THEN
                    RAISE EXCEPTION 'publication_delivery_attempt_conflict'
                        USING ERRCODE = '40001';
                END IF;
                IF attempt_row.outcome IN (
                    'retryable_failure'::ops.attempt_outcome,
                    'terminal_failure'::ops.attempt_outcome
                ) THEN
                    event_id := p_event_id;
                    attempt_no := attempt_row.attempt_no;
                    outcome := CASE attempt_row.outcome
                        WHEN 'retryable_failure'::ops.attempt_outcome THEN 'retry_wait'
                        ELSE 'terminal'
                    END;
                    available_at := attempt_row.available_at;
                    terminal_at := attempt_row.terminal_at;
                    replayed := true;
                    RETURN NEXT;
                    RETURN;
                END IF;
                RAISE EXCEPTION 'publication_delivery_attempt_conflict'
                    USING ERRCODE = '40001';
            END IF;
            UPDATE audit.publication_delivery_attempts AS delivery_attempt
               SET operation_payload_hash = v_operation_hash
             WHERE delivery_attempt.event_id = p_event_id
               AND delivery_attempt.attempt_no = attempt_row.attempt_no;

            v_effective_code := btrim(p_error_code);
            -- The caller may request a longer bounded delay, but transient
            -- failures always retain the policy's exponential backoff floor.
            v_retry_seconds := greatest(
                least(3600, p_retry_delay_seconds),
                least(3600, power(2::numeric, attempt_row.attempt_no - 1)::integer)
            );
            IF p_terminal OR v_effective_code IN (
                'publication_payload_hash_mismatch',
                'publication_event_schema_unsupported',
                'publication_event_aggregate_mismatch',
                'publication_grant_missing',
                'publication_manifest_invalid'
            ) THEN
                v_outcome := 'terminal_failure'::ops.attempt_outcome;
                v_terminal_at := v_now;
                v_available_at := NULL;
                IF attempt_row.attempt_no >= 12
                   AND v_effective_code NOT IN (
                       'publication_payload_hash_mismatch',
                       'publication_event_schema_unsupported',
                       'publication_event_aggregate_mismatch',
                       'publication_grant_missing',
                       'publication_manifest_invalid'
                   )
                THEN
                    v_effective_code := 'publication_retry_exhausted';
                    v_summary := ops._publication_sanitized_summary(v_effective_code);
                END IF;
            ELSIF attempt_row.attempt_no >= 12 THEN
                v_outcome := 'terminal_failure'::ops.attempt_outcome;
                v_effective_code := 'publication_retry_exhausted';
                v_summary := ops._publication_sanitized_summary(v_effective_code);
                v_terminal_at := v_now;
                v_available_at := NULL;
            ELSE
                v_outcome := 'retryable_failure'::ops.attempt_outcome;
                v_terminal_at := NULL;
                v_available_at := v_now + make_interval(secs => v_retry_seconds);
            END IF;

            UPDATE audit.publication_delivery_attempts AS delivery_attempt
               SET outcome = v_outcome,
                   sanitized_error_code = v_effective_code,
                   sanitized_error_summary = v_summary,
                   finished_at = v_now,
                   available_at = v_available_at,
                   terminal_at = v_terminal_at
             WHERE delivery_attempt.event_id = p_event_id
               AND delivery_attempt.attempt_no = attempt_row.attempt_no;
            UPDATE ops.outbox_events AS outbox
               SET lease_owner = NULL,
                   lease_token = NULL,
                   lease_expires_at = NULL,
                   available_at = coalesce(v_available_at, outbox.available_at),
                   last_error_code = v_effective_code,
                   last_error_summary = v_summary,
                   terminal_at = v_terminal_at,
                   terminal_error_code = CASE
                       WHEN v_terminal_at IS NULL THEN NULL ELSE v_effective_code END
             WHERE outbox.id = p_event_id
               AND outbox.published_at IS NULL
               AND outbox.terminal_at IS NULL
               AND outbox.lease_token = p_lease_token
               AND outbox.lease_expires_at > v_now;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
            END IF;

            event_id := p_event_id;
            attempt_no := attempt_row.attempt_no;
            outcome := CASE v_outcome
                WHEN 'retryable_failure'::ops.attempt_outcome THEN 'retry_wait'
                ELSE 'terminal'
            END;
            available_at := v_available_at;
            terminal_at := v_terminal_at;
            replayed := false;
            RETURN NEXT;
        END
        $fail_publication_event$;

        REVOKE ALL ON TABLE audit.publication_delivery_attempts FROM PUBLIC;
        REVOKE ALL ON TABLE audit.publication_delivery_attempts
            FROM uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        GRANT SELECT ON audit.publication_delivery_attempts TO uap_audit_reader;
        REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM uap_publisher;
        REVOKE ALL ON ALL FUNCTIONS IN SCHEMA core, audit FROM uap_publisher;
        REVOKE ALL ON FUNCTION ops.ack_outbox(uuid, uuid) FROM uap_publisher;
        REVOKE ALL ON FUNCTION ops.claim_outbox(text, integer, integer) FROM uap_publisher;
        REVOKE ALL ON FUNCTION ops.publish_outbox_failure(uuid, uuid, text, text, integer)
            FROM uap_publisher;
        REVOKE ALL ON FUNCTION ops.release_outbox(uuid, uuid, text, text) FROM uap_publisher;
        REVOKE ALL ON FUNCTION ops._publication_inject_failure(text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION ops._publication_sanitized_summary(text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit.guard_publication_delivery_attempt_mutation() FROM PUBLIC;
        REVOKE ALL ON FUNCTION ops.claim_publication_outbox(text, integer, integer)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_model_governance,
                 uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops.apply_publication_event(uuid, uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_model_governance,
                 uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops.fail_publication_event(uuid, uuid, text, text, integer, boolean)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_model_governance,
                 uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ops FROM uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.claim_publication_outbox(text, integer, integer)
            TO uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.apply_publication_event(uuid, uuid)
            TO uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.fail_publication_event(
            uuid, uuid, text, text, integer, boolean
        ) TO uap_publisher;
        ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner IN SCHEMA audit
            REVOKE INSERT, UPDATE, DELETE ON TABLES FROM uap_publisher;
        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        DO $wp10_2_downgrade_guard$
        BEGIN
            -- Only publication events are WP10 state here.  Ordinary 0020
            -- outbox history and untouched pending publication events must
            -- survive a downgrade; terminal/retry publication state must not.
            IF EXISTS (SELECT 1 FROM public.documents)
               OR EXISTS (SELECT 1 FROM public.entities)
               OR EXISTS (SELECT 1 FROM audit.document_public_identities)
               OR EXISTS (SELECT 1 FROM audit.entity_public_identities)
               OR EXISTS (SELECT 1 FROM audit.publication_delivery_attempts)
               OR EXISTS (
                    SELECT 1
                      FROM ops.outbox_events AS event
                     WHERE event.event_type LIKE 'publication.%'
                       AND (
                            event.published_at IS NOT NULL
                            OR event.terminal_at IS NOT NULL
                            OR event.publish_attempts > 0
                            OR event.last_error_code IS NOT NULL
                            OR event.lease_token IS NOT NULL
                       )
               )
            THEN
                RAISE EXCEPTION 'publication_contract_state_blocks_downgrade'
                    USING ERRCODE = '22023';
            END IF;
        END
        $wp10_2_downgrade_guard$;

        REVOKE EXECUTE ON FUNCTION ops.claim_publication_outbox(text, integer, integer)
            FROM uap_publisher;
        REVOKE EXECUTE ON FUNCTION ops.apply_publication_event(uuid, uuid)
            FROM uap_publisher;
        REVOKE EXECUTE ON FUNCTION ops.fail_publication_event(
            uuid, uuid, text, text, integer, boolean
        ) FROM uap_publisher;
        DROP FUNCTION ops.fail_publication_event(uuid, uuid, text, text, integer, boolean);
        DROP FUNCTION ops.apply_publication_event(uuid, uuid);
        DROP FUNCTION ops.claim_publication_outbox(text, integer, integer);
        DROP FUNCTION ops._publication_sanitized_summary(text);
        DROP FUNCTION ops._publication_inject_failure(text);
        DROP TRIGGER publication_delivery_attempts_immutable
            ON audit.publication_delivery_attempts;
        DROP FUNCTION audit.guard_publication_delivery_attempt_mutation();
        DROP INDEX audit.ix_publication_delivery_attempts_event;
        DROP TABLE audit.publication_delivery_attempts;
        RESET ROLE;
        """
    )
