"""Freeze durable job claiming, retry recovery, dead letters, and Outbox leases.

Revision ID: 0005_durable_jobs
Revises: 0004_g3_semantic_repairs
Create Date: 2026-08-14
"""

from __future__ import annotations

from alembic import op

revision = "0005_durable_jobs"
down_revision = "0004_g3_semantic_repairs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        ALTER TABLE ops.jobs
            ADD COLUMN lease_token uuid,
            ADD COLUMN last_error_code text,
            ADD COLUMN last_error_summary text,
            ADD COLUMN recovery_reason text,
            ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
        ALTER TABLE ops.job_attempts
            ADD COLUMN lease_token uuid;
        ALTER TABLE ops.outbox_events
            ADD COLUMN lease_owner text,
            ADD COLUMN lease_token uuid,
            ADD COLUMN lease_expires_at timestamptz,
            ADD COLUMN last_error_summary text,
            ADD COLUMN available_at timestamptz NOT NULL DEFAULT now();

        ALTER TABLE ops.jobs
            ADD CONSTRAINT ck_jobs_lease_token CHECK (
                (lease_owner IS NULL AND lease_expires_at IS NULL AND lease_token IS NULL)
                OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND lease_token IS NOT NULL)
            ),
            ADD CONSTRAINT ck_jobs_terminal_fields CHECK (
                (status IN ('succeeded','dead','cancelled') AND completed_at IS NOT NULL)
                OR (status NOT IN ('succeeded','dead','cancelled'))
            );
        ALTER TABLE ops.outbox_events
            ADD CONSTRAINT ck_outbox_lease_token CHECK (
                (lease_owner IS NULL AND lease_expires_at IS NULL AND lease_token IS NULL)
                OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND lease_token IS NOT NULL)
            );

        CREATE INDEX ix_jobs_ready_claim
            ON ops.jobs (priority DESC, available_at, created_at)
            WHERE status IN ('queued','retry_wait');
        CREATE INDEX ix_jobs_expired_leases
            ON ops.jobs (lease_expires_at)
            WHERE status IN ('leased','running');
        CREATE INDEX ix_outbox_ready_claim
            ON ops.outbox_events (occurred_at)
            WHERE published_at IS NULL;

        CREATE FUNCTION ops.classify_failure(
            p_http_status smallint,
            p_error_code text
        ) RETURNS ops.attempt_outcome
        LANGUAGE plpgsql IMMUTABLE SECURITY DEFINER SET search_path = ops, pg_catalog
        AS $classify_failure$
        BEGIN
            IF p_http_status = 408 OR p_http_status = 429 OR p_http_status >= 500
               OR lower(coalesce(p_error_code, '')) IN ('timeout', 'deadline_exceeded', 'connection_reset') THEN
                RETURN 'retryable_failure';
            END IF;
            RETURN 'terminal_failure';
        END
        $classify_failure$;

        CREATE FUNCTION ops.enqueue_job(
            p_job_type text, p_payload jsonb, p_payload_schema_version text,
            p_idempotency_key text, p_priority smallint, p_available_at timestamptz,
            p_max_attempts integer, p_timeout_seconds integer
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = ops, pg_catalog
        AS $enqueue_job$
        DECLARE result_id uuid;
        BEGIN
            IF btrim(coalesce(p_job_type, '')) = '' OR btrim(coalesce(p_payload_schema_version, '')) = ''
               OR btrim(coalesce(p_idempotency_key, '')) = '' THEN
                RAISE EXCEPTION 'job type, payload schema version, and idempotency key are required'
                    USING ERRCODE='22023';
            END IF;
            IF session_user = 'uap_worker'
               AND p_job_type NOT IN (
                   'fetch_source','extract_document','translate_document',
                   'analyze_document','resolve_entities','resolve_claims','resolve_relations'
               ) THEN
                RAISE EXCEPTION 'ordinary worker cannot enqueue a publisher job'
                    USING ERRCODE='42501';
            ELSIF session_user = 'uap_publisher'
               AND p_job_type NOT IN ('publish_document','withdraw_document','invalidate_public_cache') THEN
                RAISE EXCEPTION 'publisher cannot enqueue an ordinary worker job'
                    USING ERRCODE='42501';
            ELSIF session_user NOT IN ('uap_worker', 'uap_scheduler', 'uap_publisher') THEN
                RAISE EXCEPTION 'database role cannot enqueue jobs' USING ERRCODE='42501';
            END IF;
            INSERT INTO ops.jobs (
                id, job_type, payload, payload_schema_version, idempotency_key,
                priority, available_at, max_attempts, timeout_seconds
            ) VALUES (
                md5(random()::text || clock_timestamp()::text)::uuid,
                p_job_type, coalesce(p_payload, '{}'::jsonb), p_payload_schema_version,
                p_idempotency_key, coalesce(p_priority, 0), coalesce(p_available_at, now()),
                p_max_attempts, p_timeout_seconds
            )
            ON CONFLICT (idempotency_key) DO UPDATE SET updated_at = now()
            RETURNING id INTO result_id;
            RETURN result_id;
        END
        $enqueue_job$;

        CREATE FUNCTION ops.emit_outbox(
            p_causation_job_id uuid, p_aggregate_type text, p_aggregate_id uuid,
            p_event_type text, p_event_key text, p_payload jsonb
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = ops, pg_catalog
        AS $emit_outbox$
        DECLARE result_id uuid;
        BEGIN
            IF btrim(coalesce(p_aggregate_type, '')) = '' OR p_aggregate_id IS NULL
               OR btrim(coalesce(p_event_type, '')) = '' OR btrim(coalesce(p_event_key, '')) = '' THEN
                RAISE EXCEPTION 'Outbox aggregate, event type, and event key are required'
                    USING ERRCODE='22023';
            END IF;
            INSERT INTO ops.outbox_events (
                id, causation_job_id, aggregate_type, aggregate_id, event_type, event_key, payload, occurred_at
            ) VALUES (
                md5(random()::text || clock_timestamp()::text)::uuid,
                p_causation_job_id, p_aggregate_type, p_aggregate_id, p_event_type,
                p_event_key, coalesce(p_payload, '{}'::jsonb), now()
            )
            ON CONFLICT (event_key) DO UPDATE SET event_key = EXCLUDED.event_key
            RETURNING id INTO result_id;
            RETURN result_id;
        END
        $emit_outbox$;

        CREATE FUNCTION ops.claim_job(
            p_executor_role text,
            p_worker_id text,
            p_job_types text[],
            p_lease_seconds integer
        ) RETURNS TABLE (
            job_id uuid,
            attempt_id uuid,
            job_type text,
            payload jsonb,
            payload_schema_version text,
            idempotency_key text,
            attempt_no integer,
            lease_token uuid,
            lease_expires_at timestamptz
        )
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, pg_catalog
        AS $claim_job$
        DECLARE
            candidate ops.jobs%ROWTYPE;
            previous_attempt ops.job_attempts%ROWTYPE;
            new_token uuid;
            new_attempt_no integer;
            new_attempt_id uuid;
            deadline timestamptz;
        BEGIN
            IF p_worker_id IS NULL OR btrim(p_worker_id) = '' THEN
                RAISE EXCEPTION 'worker identity is required' USING ERRCODE='22023';
            END IF;
            IF p_lease_seconds NOT BETWEEN 1 AND 86400 THEN
                RAISE EXCEPTION 'lease must be between 1 and 86400 seconds' USING ERRCODE='22023';
            END IF;
            IF coalesce(array_length(p_job_types, 1), 0) = 0 THEN
                RAISE EXCEPTION 'at least one job type is required' USING ERRCODE='22023';
            END IF;
            IF p_executor_role = 'worker' THEN
                IF session_user NOT IN ('uap_worker', 'uap_scheduler') THEN
                    RAISE EXCEPTION 'database role is not an ordinary worker' USING ERRCODE='42501';
                END IF;
                IF EXISTS (
                    SELECT 1 FROM unnest(p_job_types) AS requested(job_type)
                    WHERE requested.job_type NOT IN (
                        'fetch_source','extract_document','translate_document',
                        'analyze_document','resolve_entities','resolve_claims','resolve_relations'
                    )
                ) THEN
                    RAISE EXCEPTION 'ordinary worker requested a publisher job type' USING ERRCODE='42501';
                END IF;
            ELSIF p_executor_role = 'publisher' THEN
                IF session_user <> 'uap_publisher' THEN
                    RAISE EXCEPTION 'database role is not the publisher' USING ERRCODE='42501';
                END IF;
                IF EXISTS (
                    SELECT 1 FROM unnest(p_job_types) AS requested(job_type)
                    WHERE requested.job_type NOT IN ('publish_document','withdraw_document','invalidate_public_cache')
                ) THEN
                    RAISE EXCEPTION 'publisher requested a non-publisher job type' USING ERRCODE='42501';
                END IF;
            ELSE
                RAISE EXCEPTION 'unknown executor role' USING ERRCODE='22023';
            END IF;

            FOR candidate IN
                SELECT j.*
                  FROM ops.jobs AS j
                 WHERE (
                       (j.status IN ('queued','retry_wait') AND j.available_at <= now())
                    OR (j.status IN ('leased','running') AND j.lease_expires_at <= now())
                 )
                   AND j.job_type = ANY (p_job_types)
                 ORDER BY j.priority DESC, j.available_at, j.created_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
            LOOP
                IF candidate.status IN ('leased','running') THEN
                    SELECT ja.* INTO previous_attempt
                      FROM ops.job_attempts AS ja
                     WHERE ja.job_id = candidate.id AND ja.attempt_no = candidate.attempt_count
                     FOR UPDATE;
                    IF previous_attempt.id IS NOT NULL AND previous_attempt.finished_at IS NULL THEN
                        UPDATE ops.job_attempts
                           SET finished_at = clock_timestamp(),
                               duration_ms = greatest(0, (extract(epoch FROM (clock_timestamp() - started_at)) * 1000)::bigint),
                               outcome = 'retryable_failure',
                               error_code = 'lease_expired',
                               error_summary = 'lease expired before completion',
                               retry_at = now()
                         WHERE id = previous_attempt.id;
                    END IF;
                    IF candidate.attempt_count >= candidate.max_attempts THEN
                        INSERT INTO ops.dead_letters (
                            id, job_id, last_attempt_id, reason_code, payload_snapshot, dead_at
                        ) VALUES (
                            md5(random()::text || clock_timestamp()::text)::uuid,
                            candidate.id, previous_attempt.id, 'lease_expired', candidate.payload, now()
                        ) ON CONFLICT ON CONSTRAINT dead_letters_job_id_key DO NOTHING;
                        UPDATE ops.jobs
                           SET status = 'dead', completed_at = now(), updated_at = now(),
                               lease_owner = NULL, lease_expires_at = NULL, lease_token = NULL,
                               last_error_code = 'lease_expired',
                               last_error_summary = 'maximum attempts exhausted after lease expiry',
                               recovery_reason = 'lease_expired'
                         WHERE id = candidate.id;
                        CONTINUE;
                    END IF;
                END IF;

                new_attempt_no := candidate.attempt_count + 1;
                new_attempt_id := md5(random()::text || clock_timestamp()::text)::uuid;
                new_token := md5(random()::text || clock_timestamp()::text)::uuid;
                deadline := now() + make_interval(secs => p_lease_seconds);
                UPDATE ops.jobs
                   SET status = 'running', attempt_count = new_attempt_no,
                       lease_owner = p_worker_id, lease_expires_at = deadline, lease_token = new_token,
                       updated_at = now(),
                       recovery_reason = CASE
                           WHEN candidate.status IN ('leased','running') THEN 'lease_expired'
                           ELSE candidate.recovery_reason
                       END
                 WHERE id = candidate.id;
                INSERT INTO ops.job_attempts (
                    id, job_id, attempt_no, worker_id, lease_token, started_at, outcome
                ) VALUES (
                    new_attempt_id, candidate.id, new_attempt_no, p_worker_id, new_token, now(), 'running'
                );
                job_id := candidate.id;
                attempt_id := new_attempt_id;
                job_type := candidate.job_type;
                payload := candidate.payload;
                payload_schema_version := candidate.payload_schema_version;
                idempotency_key := candidate.idempotency_key;
                attempt_no := new_attempt_no;
                lease_token := new_token;
                lease_expires_at := deadline;
                RETURN NEXT;
                RETURN;
            END LOOP;
        END
        $claim_job$;

        CREATE FUNCTION ops.finish_job(
            p_job_id uuid,
            p_attempt_id uuid,
            p_lease_token uuid,
            p_outcome ops.attempt_outcome,
            p_http_status smallint DEFAULT NULL,
            p_error_code text DEFAULT NULL,
            p_error_summary text DEFAULT NULL,
            p_retry_delay_seconds integer DEFAULT NULL
        ) RETURNS ops.job_status
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, pg_catalog
        AS $finish_job$
        DECLARE
            current_job ops.jobs%ROWTYPE;
            next_status ops.job_status;
            retry_at timestamptz;
            dead_reason text;
        BEGIN
            IF p_outcome = 'running' THEN
                RAISE EXCEPTION 'running is not a terminal attempt outcome' USING ERRCODE='22023';
            END IF;
            IF p_outcome NOT IN ('succeeded', 'cancelled')
               AND p_outcome <> ops.classify_failure(p_http_status, p_error_code) THEN
                RAISE EXCEPTION 'failure outcome does not match the frozen retry policy'
                    USING ERRCODE='22023';
            END IF;
            SELECT * INTO current_job FROM ops.jobs WHERE id = p_job_id FOR UPDATE;
            IF current_job.id IS NULL OR current_job.status <> 'running'
               OR current_job.lease_token IS DISTINCT FROM p_lease_token
               OR current_job.lease_expires_at <= now() THEN
                RAISE EXCEPTION 'job lease is missing, expired, or owned by another worker' USING ERRCODE='40001';
            END IF;
            IF session_user IN ('uap_worker', 'uap_scheduler')
               AND current_job.job_type IN ('publish_document','withdraw_document','invalidate_public_cache') THEN
                RAISE EXCEPTION 'ordinary worker cannot finish a publisher job' USING ERRCODE='42501';
            ELSIF session_user = 'uap_publisher'
               AND current_job.job_type NOT IN ('publish_document','withdraw_document','invalidate_public_cache') THEN
                RAISE EXCEPTION 'publisher cannot finish an ordinary worker job' USING ERRCODE='42501';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM ops.job_attempts
                 WHERE id = p_attempt_id AND job_id = p_job_id
                   AND lease_token = p_lease_token AND finished_at IS NULL
            ) THEN
                RAISE EXCEPTION 'attempt is missing or already finished' USING ERRCODE='40001';
            END IF;
            UPDATE ops.job_attempts
               SET finished_at = clock_timestamp(),
                   duration_ms = greatest(0, (extract(epoch FROM (clock_timestamp() - started_at)) * 1000)::bigint),
                   outcome = p_outcome, http_status = p_http_status,
                   error_code = p_error_code, error_summary = p_error_summary,
                   retry_at = CASE WHEN p_outcome = 'retryable_failure'
                                   THEN now() + make_interval(secs => greatest(0, coalesce(p_retry_delay_seconds, 0)))
                                   ELSE NULL END
             WHERE id = p_attempt_id;

            IF p_outcome = 'succeeded' THEN
                next_status := 'succeeded';
            ELSIF p_outcome = 'cancelled' THEN
                next_status := 'cancelled';
            ELSIF p_outcome = 'terminal_failure' OR current_job.attempt_count >= current_job.max_attempts THEN
                next_status := 'dead';
                dead_reason := coalesce(p_error_code, 'terminal_failure');
            ELSE
                next_status := 'retry_wait';
            END IF;

            IF next_status = 'dead' THEN
                INSERT INTO ops.dead_letters (
                    id, job_id, last_attempt_id, reason_code, payload_snapshot, dead_at
                ) VALUES (
                    md5(random()::text || clock_timestamp()::text)::uuid,
                    p_job_id, p_attempt_id, dead_reason, current_job.payload, now()
                ) ON CONFLICT (job_id) DO UPDATE
                    SET last_attempt_id = EXCLUDED.last_attempt_id,
                        reason_code = EXCLUDED.reason_code,
                        payload_snapshot = EXCLUDED.payload_snapshot,
                        dead_at = EXCLUDED.dead_at,
                        resolved_at = NULL, resolution = NULL;
            END IF;
            retry_at := CASE WHEN next_status = 'retry_wait'
                             THEN now() + make_interval(secs => least(3600, greatest(0, coalesce(
                                  p_retry_delay_seconds,
                                  (power(2::numeric, least(greatest(current_job.attempt_count - 1, 0), 9)))::integer
                              ))))
                             ELSE NULL END;
            UPDATE ops.jobs
               SET status = next_status,
                   available_at = coalesce(retry_at, available_at),
                   completed_at = CASE WHEN next_status IN ('succeeded','dead','cancelled') THEN now() ELSE NULL END,
                   lease_owner = NULL, lease_expires_at = NULL, lease_token = NULL,
                   last_error_code = CASE WHEN p_outcome = 'succeeded' THEN NULL ELSE p_error_code END,
                   last_error_summary = CASE WHEN p_outcome = 'succeeded' THEN NULL ELSE p_error_summary END,
                   updated_at = now()
             WHERE id = p_job_id;
            RETURN next_status;
        END
        $finish_job$;

        CREATE FUNCTION ops.requeue_dead_letter(
            p_job_id uuid,
            p_resolution text
        ) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, pg_catalog
        AS $requeue_dead_letter$
        DECLARE dead_job_type text;
        BEGIN
            IF session_user NOT IN ('uap_scheduler', 'uap_worker') THEN
                RAISE EXCEPTION 'only scheduler or worker may requeue a dead letter' USING ERRCODE='42501';
            END IF;
            IF btrim(coalesce(p_resolution, '')) = '' THEN
                RAISE EXCEPTION 'dead-letter resolution is required' USING ERRCODE='22023';
            END IF;
            SELECT job_type INTO dead_job_type
              FROM ops.jobs
             WHERE id = p_job_id AND status = 'dead'
             FOR UPDATE;
            IF dead_job_type IS NULL THEN
                RAISE EXCEPTION 'dead-letter job is not in dead state' USING ERRCODE='22023';
            END IF;
            IF session_user = 'uap_worker'
               AND dead_job_type IN ('publish_document','withdraw_document','invalidate_public_cache') THEN
                RAISE EXCEPTION 'ordinary worker cannot requeue a publisher job'
                    USING ERRCODE='42501';
            END IF;
            UPDATE ops.dead_letters
               SET resolved_at = now(), resolution = p_resolution
             WHERE job_id = p_job_id AND resolved_at IS NULL;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'unresolved dead letter was not found' USING ERRCODE='22023';
            END IF;
            UPDATE ops.jobs
               SET status = 'queued', available_at = now(), completed_at = NULL,
                   lease_owner = NULL, lease_expires_at = NULL, lease_token = NULL,
                   recovery_reason = 'dead_letter_requeued', updated_at = now()
             WHERE id = p_job_id AND status = 'dead';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'dead-letter job is not in dead state' USING ERRCODE='22023';
            END IF;
        END
        $requeue_dead_letter$;

        CREATE FUNCTION ops.publish_outbox_failure(
            p_event_id uuid,
            p_lease_token uuid,
            p_error_code text,
            p_error_summary text,
            p_retry_delay_seconds integer DEFAULT 5
        ) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, pg_catalog
        AS $publish_outbox_failure$
        BEGIN
            IF session_user <> 'uap_publisher' THEN
                RAISE EXCEPTION 'only publisher may record Outbox failures' USING ERRCODE='42501';
            END IF;
            IF p_retry_delay_seconds NOT BETWEEN 0 AND 3600 THEN
                RAISE EXCEPTION 'Outbox retry delay is outside the allowed range' USING ERRCODE='22023';
            END IF;
            UPDATE ops.outbox_events
               SET lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                   available_at = now() + make_interval(secs => p_retry_delay_seconds),
                   last_error_code = p_error_code, last_error_summary = p_error_summary
             WHERE id = p_event_id AND published_at IS NULL
               AND lease_token = p_lease_token AND lease_expires_at > now();
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Outbox lease is missing or already acknowledged' USING ERRCODE='40001';
            END IF;
        END
        $publish_outbox_failure$;

        CREATE FUNCTION ops.claim_outbox(
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
            lease_token uuid,
            lease_expires_at timestamptz
        )
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, pg_catalog
        AS $claim_outbox$
        DECLARE
            event_row ops.outbox_events%ROWTYPE;
            token uuid;
            deadline timestamptz;
        BEGIN
            IF session_user <> 'uap_publisher' THEN
                RAISE EXCEPTION 'only publisher may claim Outbox events' USING ERRCODE='42501';
            END IF;
            IF p_dispatcher_id IS NULL OR btrim(p_dispatcher_id) = '' OR p_lease_seconds NOT BETWEEN 1 AND 86400
               OR p_limit NOT BETWEEN 1 AND 100 THEN
                RAISE EXCEPTION 'invalid Outbox claim parameters' USING ERRCODE='22023';
            END IF;
            FOR event_row IN
                SELECT e.* FROM ops.outbox_events AS e
                 WHERE e.published_at IS NULL
                   AND e.available_at <= now()
                   AND (e.lease_expires_at IS NULL OR e.lease_expires_at <= now())
                 ORDER BY e.occurred_at, e.id
                 FOR UPDATE SKIP LOCKED
                 LIMIT p_limit
            LOOP
                token := md5(random()::text || clock_timestamp()::text)::uuid;
                deadline := now() + make_interval(secs => p_lease_seconds);
                UPDATE ops.outbox_events
                   SET lease_owner = p_dispatcher_id, lease_token = token,
                       lease_expires_at = deadline, publish_attempts = publish_attempts + 1
                 WHERE id = event_row.id;
                event_id := event_row.id;
                causation_job_id := event_row.causation_job_id;
                aggregate_type := event_row.aggregate_type;
                aggregate_id := event_row.aggregate_id;
                event_type := event_row.event_type;
                event_key := event_row.event_key;
                payload := event_row.payload;
                lease_token := token;
                lease_expires_at := deadline;
                RETURN NEXT;
            END LOOP;
        END
        $claim_outbox$;

        CREATE FUNCTION ops.ack_outbox(p_event_id uuid, p_lease_token uuid) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, pg_catalog
        AS $ack_outbox$
        BEGIN
            IF session_user <> 'uap_publisher' THEN
                RAISE EXCEPTION 'only publisher may acknowledge Outbox events' USING ERRCODE='42501';
            END IF;
            UPDATE ops.outbox_events
               SET published_at = now(), lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
             WHERE id = p_event_id AND published_at IS NULL
               AND lease_token = p_lease_token AND lease_expires_at > now();
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Outbox lease is missing or already acknowledged' USING ERRCODE='40001';
            END IF;
        END
        $ack_outbox$;

        CREATE FUNCTION ops.release_outbox(
            p_event_id uuid, p_lease_token uuid, p_error_code text, p_error_summary text
        ) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, pg_catalog
        AS $release_outbox$
        BEGIN
            IF session_user <> 'uap_publisher' THEN
                RAISE EXCEPTION 'only publisher may release Outbox events' USING ERRCODE='42501';
            END IF;
            UPDATE ops.outbox_events
               SET lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                   last_error_code = p_error_code, last_error_summary = p_error_summary
             WHERE id = p_event_id AND published_at IS NULL
               AND lease_token = p_lease_token AND lease_expires_at > now();
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Outbox lease is missing or already acknowledged' USING ERRCODE='40001';
            END IF;
        END
        $release_outbox$;

        GRANT SELECT ON ops.jobs, ops.job_attempts, ops.dead_letters TO uap_worker, uap_scheduler, uap_publisher;
        GRANT SELECT ON ops.outbox_events TO uap_publisher;
        REVOKE INSERT, UPDATE ON ops.jobs, ops.job_attempts, ops.dead_letters, ops.outbox_events
            FROM uap_worker, uap_scheduler, uap_publisher;
        REVOKE INSERT, UPDATE ON ops.jobs, ops.job_attempts, ops.dead_letters, ops.outbox_events
            FROM uap_api;
        REVOKE ALL ON FUNCTION ops.classify_failure(smallint, text),
            ops.enqueue_job(text, jsonb, text, text, smallint, timestamptz, integer, integer),
            ops.emit_outbox(uuid, text, uuid, text, text, jsonb),
            ops.claim_job(text, text, text[], integer),
            ops.finish_job(uuid, uuid, uuid, ops.attempt_outcome, smallint, text, text, integer),
            ops.requeue_dead_letter(uuid, text),
            ops.claim_outbox(text, integer, integer),
            ops.ack_outbox(uuid, uuid),
            ops.release_outbox(uuid, uuid, text, text),
            ops.publish_outbox_failure(uuid, uuid, text, text, integer)
            FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION ops.claim_job(text, text, text[], integer) TO uap_worker, uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.classify_failure(smallint, text) TO uap_worker, uap_publisher, uap_scheduler;
        GRANT EXECUTE ON FUNCTION ops.enqueue_job(text, jsonb, text, text, smallint, timestamptz, integer, integer)
            TO uap_worker, uap_scheduler;
        GRANT EXECUTE ON FUNCTION ops.emit_outbox(uuid, text, uuid, text, text, jsonb)
            TO uap_worker, uap_scheduler;
        GRANT EXECUTE ON FUNCTION ops.finish_job(uuid, uuid, uuid, ops.attempt_outcome, smallint, text, text, integer)
            TO uap_worker, uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.requeue_dead_letter(uuid, text) TO uap_worker, uap_scheduler;
        GRANT EXECUTE ON FUNCTION ops.claim_outbox(text, integer, integer) TO uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.ack_outbox(uuid, uuid), ops.release_outbox(uuid, uuid, text, text)
            TO uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.publish_outbox_failure(uuid, uuid, text, text, integer) TO uap_publisher;

        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;
        REVOKE EXECUTE ON FUNCTION ops.claim_job(text, text, text[], integer) FROM uap_worker, uap_publisher;
        REVOKE EXECUTE ON FUNCTION ops.classify_failure(smallint, text) FROM uap_worker, uap_publisher, uap_scheduler;
        REVOKE EXECUTE ON FUNCTION ops.enqueue_job(text, jsonb, text, text, smallint, timestamptz, integer, integer)
            FROM uap_worker, uap_scheduler;
        REVOKE EXECUTE ON FUNCTION ops.emit_outbox(uuid, text, uuid, text, text, jsonb)
            FROM uap_worker, uap_scheduler;
        REVOKE EXECUTE ON FUNCTION ops.finish_job(uuid, uuid, uuid, ops.attempt_outcome, smallint, text, text, integer)
            FROM uap_worker, uap_publisher;
        REVOKE EXECUTE ON FUNCTION ops.requeue_dead_letter(uuid, text) FROM uap_worker, uap_scheduler;
        REVOKE EXECUTE ON FUNCTION ops.claim_outbox(text, integer, integer) FROM uap_publisher;
        REVOKE EXECUTE ON FUNCTION ops.ack_outbox(uuid, uuid), ops.release_outbox(uuid, uuid, text, text)
            FROM uap_publisher;
        REVOKE EXECUTE ON FUNCTION ops.publish_outbox_failure(uuid, uuid, text, text, integer) FROM uap_publisher;
        DROP FUNCTION ops.release_outbox(uuid, uuid, text, text);
        DROP FUNCTION ops.publish_outbox_failure(uuid, uuid, text, text, integer);
        DROP FUNCTION ops.ack_outbox(uuid, uuid);
        DROP FUNCTION ops.claim_outbox(text, integer, integer);
        DROP FUNCTION ops.finish_job(uuid, uuid, uuid, ops.attempt_outcome, smallint, text, text, integer);
        DROP FUNCTION ops.requeue_dead_letter(uuid, text);
        DROP FUNCTION ops.claim_job(text, text, text[], integer);
        DROP FUNCTION ops.emit_outbox(uuid, text, uuid, text, text, jsonb);
        DROP FUNCTION ops.enqueue_job(text, jsonb, text, text, smallint, timestamptz, integer, integer);
        DROP FUNCTION ops.classify_failure(smallint, text);
        DROP INDEX ops.ix_outbox_ready_claim;
        DROP INDEX ops.ix_jobs_expired_leases;
        DROP INDEX ops.ix_jobs_ready_claim;
        ALTER TABLE ops.outbox_events DROP CONSTRAINT ck_outbox_lease_token;
        ALTER TABLE ops.jobs DROP CONSTRAINT ck_jobs_terminal_fields, DROP CONSTRAINT ck_jobs_lease_token;
        ALTER TABLE ops.outbox_events
            DROP COLUMN last_error_summary, DROP COLUMN lease_expires_at,
            DROP COLUMN lease_token, DROP COLUMN lease_owner, DROP COLUMN available_at;
        ALTER TABLE ops.job_attempts DROP COLUMN lease_token;
        ALTER TABLE ops.jobs
            DROP COLUMN updated_at, DROP COLUMN recovery_reason, DROP COLUMN last_error_summary,
            DROP COLUMN last_error_code, DROP COLUMN lease_token;
        RESET ROLE;
        """
    )
