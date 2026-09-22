"""Accept the V1-3.1 Editorial revision identity in publication delivery.

0030 extended the canonical document publication event with the immutable
Editorial revision identity.  The 0022 Publisher consumer still used the
pre-0030 allow-list and therefore left valid document events unclaimable.
This forward fix changes only the publication payload consumers and keeps
unknown-field rejection and pre-0030 events intact.
"""

from __future__ import annotations

from alembic import op

revision = "0033_v132_publication_payload_revision_compat"
down_revision = "0032_v131_publication_review_submission_audit_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION ops._validate_publication_editorial_identity(
            p_payload jsonb,
            p_grant_id uuid
        ) RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = ops, audit, core, pg_catalog
        AS $validate_publication_editorial_identity$
        DECLARE
            v_revision_id uuid;
            v_revision_no integer;
            v_document_version_id uuid;
            v_grant_revision_id uuid;
            v_grant_revision_no integer;
            v_manifest_revision_id uuid;
            v_manifest_revision_no integer;
        BEGIN
            -- No Editorial identity means a legacy pre-0030 event.  Its
            -- frozen payload/hash contract remains unchanged.
            IF NOT (p_payload ? 'editorial_revision_id')
               AND NOT (p_payload ? 'editorial_revision_no') THEN
                RETURN;
            END IF;
            IF NOT (p_payload ?& ARRAY['editorial_revision_id', 'editorial_revision_no'])
               OR jsonb_typeof(p_payload -> 'editorial_revision_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_payload -> 'editorial_revision_no') IS DISTINCT FROM 'number'
            THEN
                RAISE EXCEPTION 'publication_event_schema_unsupported'
                    USING ERRCODE = '22023';
            END IF;
            BEGIN
                v_revision_id := (p_payload ->> 'editorial_revision_id')::uuid;
                v_revision_no := (p_payload ->> 'editorial_revision_no')::integer;
            EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                RAISE EXCEPTION 'publication_event_schema_unsupported'
                    USING ERRCODE = '22023';
            END;
            IF v_revision_id IS NULL OR v_revision_no IS NULL OR v_revision_no < 1 THEN
                RAISE EXCEPTION 'publication_event_schema_unsupported'
                    USING ERRCODE = '22023';
            END IF;

            SELECT grant_row.document_version_id,
                   grant_row.editorial_revision_id,
                   grant_row.editorial_revision_no,
                   manifest.editorial_revision_id,
                   manifest.editorial_revision_no
              INTO v_document_version_id, v_grant_revision_id,
                   v_grant_revision_no, v_manifest_revision_id,
                   v_manifest_revision_no
              FROM audit.document_publication_grants AS grant_row
              JOIN audit.document_publication_manifests AS manifest
                ON manifest.grant_id = grant_row.id
             WHERE grant_row.id = p_grant_id;
            IF NOT FOUND
               OR v_grant_revision_id IS NULL
               OR v_grant_revision_no IS NULL
               OR v_manifest_revision_id IS NULL
               OR v_manifest_revision_no IS NULL
            THEN
                RAISE EXCEPTION 'publication_payload_hash_mismatch'
                    USING ERRCODE = '22023';
            END IF;
            IF p_payload ->> 'subject_id' IS DISTINCT FROM lower(v_document_version_id::text)
               OR v_revision_id IS DISTINCT FROM v_grant_revision_id
               OR v_revision_no IS DISTINCT FROM v_grant_revision_no
               OR v_revision_id IS DISTINCT FROM v_manifest_revision_id
               OR v_revision_no IS DISTINCT FROM v_manifest_revision_no
               OR v_grant_revision_id IS DISTINCT FROM v_manifest_revision_id
               OR v_grant_revision_no IS DISTINCT FROM v_manifest_revision_no
               OR NOT EXISTS (
                   SELECT 1
                     FROM core.editorial_revisions AS revision
                    WHERE revision.id = v_revision_id
                      AND revision.document_version_id = v_document_version_id
                      AND revision.revision_no = v_revision_no
               )
            THEN
                RAISE EXCEPTION 'publication_payload_hash_mismatch'
                    USING ERRCODE = '22023';
            END IF;
        END
        $validate_publication_editorial_identity$;

        ALTER FUNCTION ops._validate_publication_editorial_identity(jsonb, uuid)
            OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION ops._validate_publication_editorial_identity(jsonb, uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_model_governance,
                 uap_public_reader, uap_audit_reader, uap_backup;
        GRANT EXECUTE ON FUNCTION ops._validate_publication_editorial_identity(jsonb, uuid)
            TO uap_publisher;

        -- The manifest/hash consumer must use the same immutable snapshot
        -- contract as 0030.  Legacy rows keep the exact old JSON shape.
        CREATE OR REPLACE FUNCTION ops._wp10_3_document_manifest_payload(p_grant_id uuid)
        RETURNS jsonb
        LANGUAGE sql STABLE STRICT
        SET search_path = audit, pg_catalog
        AS $wp10_3_document_manifest_payload$
            SELECT jsonb_build_object(
                'schema', 'publication-manifest.v2',
                'subject_type', 'document',
                'grant_id', lower(manifest.grant_id::text),
                'decision_id', lower(manifest.decision_id::text),
                'subject_id', lower(manifest.document_version_id::text),
                'document_id', lower(manifest.document_id::text),
                'document_version_id', lower(manifest.document_version_id::text),
                'revision_no', grant_row.revision_no,
                'title', manifest.title,
                'summary', to_jsonb(manifest.summary),
                'category', manifest.category::text,
                'fact_status', manifest.fact_status::text,
                'source_name', manifest.source_name,
                'canonical_source_url', manifest.canonical_source_url,
                'source_published_at', CASE
                    WHEN manifest.source_published_at IS NULL THEN 'null'::jsonb
                    ELSE to_jsonb(to_char(
                        manifest.source_published_at AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                    ))
                END,
                'summary_analysis_result_id', to_jsonb(manifest.summary_analysis_result_id)
            ) || CASE
                WHEN manifest.editorial_revision_id IS NULL THEN '{}'::jsonb
                ELSE jsonb_build_object(
                    'editorial_revision_id', lower(manifest.editorial_revision_id::text),
                    'editorial_revision_no', manifest.editorial_revision_no
                )
            END
            FROM audit.document_publication_manifests AS manifest
            JOIN audit.document_publication_grants AS grant_row
              ON grant_row.id = manifest.grant_id
            WHERE manifest.grant_id = p_grant_id
        $wp10_3_document_manifest_payload$;

        -- Patch the frozen document/entity apply helper in place.  The
        -- function body is read from the 0032 database definition so the
        -- legacy projection algorithm remains byte-for-byte intact except
        -- for the two 0030 identity fields and their validation.
        DO $patch_document_apply$
        DECLARE
            v_sql text;
            v_old text;
            v_new text;
        BEGIN
            SELECT pg_get_functiondef(
                'ops._apply_publication_event_wp10_2(uuid,uuid)'::regprocedure
            ) INTO v_sql;

            v_old := $old$
            v_manifest_document_version_id uuid;
$old$;
            v_new := $new$
            v_manifest_document_version_id uuid;
            v_manifest_editorial_revision_id uuid;
            v_manifest_editorial_revision_no integer;
$new$;
            IF strpos(v_sql, v_old) = 0 THEN
                RAISE EXCEPTION '0033 apply declaration marker missing';
            END IF;
            v_sql := replace(v_sql, v_old, v_new);

            v_old := $old$
               OR (v_payload - ARRAY[
                    'schema', 'grant_id', 'decision_id', 'subject_type',
                    'subject_id', 'revision_no', 'payload_sha256', 'old_grant_id'
                  ]) <> '{}'::jsonb
$old$;
            v_new := $new$
               OR (
                    (v_subject_type = 'document' AND
                     (v_payload - ARRAY[
                        'schema', 'grant_id', 'decision_id', 'subject_type',
                        'subject_id', 'revision_no', 'payload_sha256', 'old_grant_id',
                        'editorial_revision_id', 'editorial_revision_no'
                     ]) <> '{}'::jsonb)
                    OR (v_subject_type <> 'document' AND
                     (v_payload - ARRAY[
                        'schema', 'grant_id', 'decision_id', 'subject_type',
                        'subject_id', 'revision_no', 'payload_sha256', 'old_grant_id'
                     ]) <> '{}'::jsonb)
                  )
               OR (
                    v_subject_type = 'document'
                    AND (v_payload ? 'editorial_revision_id')
                        IS DISTINCT FROM (v_payload ? 'editorial_revision_no')
                  )
$new$;
            IF strpos(v_sql, v_old) = 0 THEN
                RAISE EXCEPTION '0033 apply payload allow-list marker missing';
            END IF;
            v_sql := replace(v_sql, v_old, v_new);

            v_old := $old$
                       manifest.summary_analysis_result_id, manifest.manifest_sha256
$old$;
            v_new := $new$
                       manifest.summary_analysis_result_id, manifest.manifest_sha256,
                       manifest.editorial_revision_id, manifest.editorial_revision_no
$new$;
            IF strpos(v_sql, v_old) = 0 THEN
                RAISE EXCEPTION '0033 apply manifest select marker missing';
            END IF;
            v_sql := replace(v_sql, v_old, v_new);

            v_old := $old$
                       v_source_published_at, v_summary_result, v_recomputed_sha
$old$;
            v_new := $new$
                       v_source_published_at, v_summary_result, v_recomputed_sha,
                       v_manifest_editorial_revision_id, v_manifest_editorial_revision_no
$new$;
            IF strpos(v_sql, v_old) = 0 THEN
                RAISE EXCEPTION '0033 apply manifest into marker missing';
            END IF;
            v_sql := replace(v_sql, v_old, v_new);

            v_old := $old$
            IF NOT FOUND THEN
                RAISE EXCEPTION 'publication_grant_missing' USING ERRCODE = '22023';
            END IF;
            IF v_subject_type = 'document' THEN
                v_manifest := jsonb_build_object(
$old$;
            v_new := $new$
            IF NOT FOUND THEN
                RAISE EXCEPTION 'publication_grant_missing' USING ERRCODE = '22023';
            END IF;
            PERFORM ops._validate_publication_editorial_identity(v_payload, v_grant_id);
            IF v_subject_type = 'document' THEN
                v_manifest := jsonb_build_object(
$new$;
            IF strpos(v_sql, v_old) = 0 THEN
                RAISE EXCEPTION '0033 apply identity validation marker missing';
            END IF;
            v_sql := replace(v_sql, v_old, v_new);

            v_old := $old$
                    'summary_analysis_result_id', to_jsonb(v_summary_result)
                );
            ELSE
$old$;
            v_new := $new$
                    'summary_analysis_result_id', to_jsonb(v_summary_result)
                );
                IF v_manifest_editorial_revision_id IS NOT NULL THEN
                    v_manifest := v_manifest || jsonb_build_object(
                        'editorial_revision_id', lower(v_manifest_editorial_revision_id::text),
                        'editorial_revision_no', v_manifest_editorial_revision_no
                    );
                END IF;
            ELSE
$new$;
            IF strpos(v_sql, v_old) = 0 THEN
                RAISE EXCEPTION '0033 apply manifest hash marker missing';
            END IF;
            v_sql := replace(v_sql, v_old, v_new);

            EXECUTE v_sql;
        END
        $patch_document_apply$;

        CREATE FUNCTION ops._claim_publication_v132_documents(
            p_dispatcher_id text,
            p_lease_seconds integer,
            p_limit integer
        ) RETURNS TABLE (
            event_id uuid, causation_job_id uuid, aggregate_type text,
            aggregate_id uuid, event_type text, event_key text, payload jsonb,
            attempt_no integer, lease_token uuid, lease_expires_at timestamptz
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = ops, audit, pg_catalog
        AS $claim_publication_v132_documents$
        DECLARE
            event_row ops.outbox_events%ROWTYPE;
            v_now timestamptz := clock_timestamp();
            v_token uuid;
            v_attempt_no integer;
        BEGIN
            IF session_user <> 'uap_publisher' THEN
                RAISE EXCEPTION 'only publisher may claim publication events'
                    USING ERRCODE = '42501';
            END IF;
            FOR event_row IN
                SELECT candidate.*
                  FROM ops.outbox_events AS candidate
                 WHERE candidate.published_at IS NULL
                   AND candidate.terminal_at IS NULL
                   AND candidate.available_at <= v_now
                   AND (candidate.lease_expires_at IS NULL
                        OR candidate.lease_expires_at <= v_now)
                   AND candidate.event_type IN (
                        'publication.granted', 'publication.superseded',
                        'publication.withdrawn'
                   )
                   AND candidate.aggregate_type = 'document_publication_grants'
                   AND jsonb_typeof(candidate.payload) = 'object'
                   AND candidate.payload ->> 'schema' = 'publication-outbox.v2'
                   AND candidate.payload ->> 'subject_type' = 'document'
                   AND (candidate.payload - ARRAY[
                        'schema', 'grant_id', 'decision_id', 'subject_type',
                        'subject_id', 'revision_no', 'payload_sha256', 'old_grant_id',
                        'editorial_revision_id', 'editorial_revision_no'
                   ]) = '{}'::jsonb
                   AND candidate.payload ?& ARRAY[
                        'grant_id', 'decision_id', 'subject_id', 'revision_no',
                        'payload_sha256', 'editorial_revision_id',
                        'editorial_revision_no'
                   ]
                   AND jsonb_typeof(candidate.payload -> 'editorial_revision_id') = 'string'
                   AND jsonb_typeof(candidate.payload -> 'editorial_revision_no') = 'number'
                   AND ((candidate.event_type = 'publication.superseded') =
                        (candidate.payload ? 'old_grant_id'))
                 ORDER BY candidate.occurred_at, candidate.id
                 FOR UPDATE SKIP LOCKED
                 LIMIT p_limit
            LOOP
                v_attempt_no := event_row.publish_attempts + 1;
                IF event_row.publish_attempts > 0 THEN
                    UPDATE audit.publication_delivery_attempts AS attempt
                       SET outcome = 'retryable_failure'::ops.attempt_outcome,
                           sanitized_error_code = 'publication_lease_lost',
                           sanitized_error_summary = ops._publication_sanitized_summary(
                               'publication_lease_lost'
                           ),
                           finished_at = v_now, available_at = v_now
                     WHERE attempt.event_id = event_row.id
                       AND attempt.attempt_no = event_row.publish_attempts
                       AND attempt.outcome = 'running'::ops.attempt_outcome;
                END IF;
                v_token := gen_random_uuid();
                UPDATE ops.outbox_events
                   SET lease_owner = btrim(p_dispatcher_id),
                       lease_token = v_token,
                       lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
                       publish_attempts = v_attempt_no
                 WHERE id = event_row.id;
                INSERT INTO audit.publication_delivery_attempts (
                    event_id, attempt_no, dispatcher, lease_token_hash, outcome, started_at
                ) VALUES (
                    event_row.id, v_attempt_no, btrim(p_dispatcher_id),
                    audit._payload_sha256(jsonb_build_object(
                        'lease_token', lower(v_token::text)
                    )), 'running'::ops.attempt_outcome, v_now
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
                lease_expires_at := v_now + make_interval(secs => p_lease_seconds);
                RETURN NEXT;
            END LOOP;
        END
        $claim_publication_v132_documents$;

        ALTER FUNCTION ops._claim_publication_v132_documents(text, integer, integer)
            OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION ops._claim_publication_v132_documents(text, integer, integer)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_model_governance,
                 uap_public_reader, uap_audit_reader, uap_backup;
        GRANT EXECUTE ON FUNCTION ops._claim_publication_v132_documents(text, integer, integer)
            TO uap_publisher;

        -- Keep the frozen legacy helper and claim-event path; append only the
        -- V1-3.1 document identity path to the existing bounded claim loop.
        DO $patch_claim$
        DECLARE
            v_sql text;
            v_old text;
            v_new text;
        BEGIN
            SELECT pg_get_functiondef(
                'ops.claim_publication_outbox(text,integer,integer)'::regprocedure
            ) INTO v_sql;
            v_old := $old$
            END LOOP;
        END
        $function$
$old$;
            v_new := $new$
            END LOOP;
            IF v_count >= p_limit THEN RETURN; END IF;
            FOR claimed_row IN
                SELECT * FROM ops._claim_publication_v132_documents(
                    p_dispatcher_id, p_lease_seconds, p_limit - v_count
                )
            LOOP
                event_id := claimed_row.event_id;
                causation_job_id := claimed_row.causation_job_id;
                aggregate_type := claimed_row.aggregate_type;
                aggregate_id := claimed_row.aggregate_id;
                event_type := claimed_row.event_type;
                event_key := claimed_row.event_key;
                payload := claimed_row.payload;
                attempt_no := claimed_row.attempt_no;
                lease_token := claimed_row.lease_token;
                lease_expires_at := claimed_row.lease_expires_at;
                RETURN NEXT;
            END LOOP;
        END
        $function$
$new$;
            IF strpos(v_sql, v_old) = 0 THEN
                RAISE EXCEPTION '0033 claim append marker missing';
            END IF;
            EXECUTE replace(v_sql, v_old, v_new);
        END
        $patch_claim$;

        ALTER FUNCTION ops.claim_publication_outbox(text, integer, integer)
            OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION ops.claim_publication_outbox(text, integer, integer)
            FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION ops.claim_publication_outbox(text, integer, integer)
            TO uap_publisher;

        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        -- Reverse only the textual compatibility additions made above.  The
        -- 0032 function bodies remain the source of truth; no data is touched.
        DO $restore_apply$
        DECLARE
            v_sql text;
            v_old text;
            v_new text;
        BEGIN
            SELECT pg_get_functiondef(
                'ops._apply_publication_event_wp10_2(uuid,uuid)'::regprocedure
            ) INTO v_sql;

            v_old := $old$
            v_manifest_document_version_id uuid;
            v_manifest_editorial_revision_id uuid;
            v_manifest_editorial_revision_no integer;
$old$;
            v_new := $new$
            v_manifest_document_version_id uuid;
$new$;
            IF strpos(v_sql, v_old) = 0 THEN
                RAISE EXCEPTION '0033 downgrade apply declaration marker missing';
            END IF;
            v_sql := replace(v_sql, v_old, v_new);

            v_old := $old$
               OR (
                    (v_subject_type = 'document' AND
                     (v_payload - ARRAY[
                        'schema', 'grant_id', 'decision_id', 'subject_type',
                        'subject_id', 'revision_no', 'payload_sha256', 'old_grant_id',
                        'editorial_revision_id', 'editorial_revision_no'
                     ]) <> '{}'::jsonb)
                    OR (v_subject_type <> 'document' AND
                     (v_payload - ARRAY[
                        'schema', 'grant_id', 'decision_id', 'subject_type',
                        'subject_id', 'revision_no', 'payload_sha256', 'old_grant_id'
                     ]) <> '{}'::jsonb)
                  )
               OR (
                    v_subject_type = 'document'
                    AND (v_payload ? 'editorial_revision_id')
                        IS DISTINCT FROM (v_payload ? 'editorial_revision_no')
                  )
$old$;
            v_new := $new$
               OR (v_payload - ARRAY[
                    'schema', 'grant_id', 'decision_id', 'subject_type',
                    'subject_id', 'revision_no', 'payload_sha256', 'old_grant_id'
                  ]) <> '{}'::jsonb
$new$;
            IF strpos(v_sql, v_old) = 0 THEN
                RAISE EXCEPTION '0033 downgrade apply allow-list marker missing';
            END IF;
            v_sql := replace(v_sql, v_old, v_new);

            v_old := $old$
                       manifest.summary_analysis_result_id, manifest.manifest_sha256,
                       manifest.editorial_revision_id, manifest.editorial_revision_no
$old$;
            v_new := $new$
                       manifest.summary_analysis_result_id, manifest.manifest_sha256
$new$;
            IF strpos(v_sql, v_old) = 0 THEN
                RAISE EXCEPTION '0033 downgrade apply select marker missing';
            END IF;
            v_sql := replace(v_sql, v_old, v_new);

            v_old := $old$
                       v_source_published_at, v_summary_result, v_recomputed_sha,
                       v_manifest_editorial_revision_id, v_manifest_editorial_revision_no
$old$;
            v_new := $new$
                       v_source_published_at, v_summary_result, v_recomputed_sha
$new$;
            IF strpos(v_sql, v_old) = 0 THEN
                RAISE EXCEPTION '0033 downgrade apply into marker missing';
            END IF;
            v_sql := replace(v_sql, v_old, v_new);

            v_old := $old$
            PERFORM ops._validate_publication_editorial_identity(v_payload, v_grant_id);
$old$;
            v_new := $new$
$new$;
            IF strpos(v_sql, v_old) = 0 THEN
                RAISE EXCEPTION '0033 downgrade apply validator marker missing';
            END IF;
            v_sql := replace(v_sql, v_old, v_new);

            v_old := $old$
                IF v_manifest_editorial_revision_id IS NOT NULL THEN
                    v_manifest := v_manifest || jsonb_build_object(
                        'editorial_revision_id', lower(v_manifest_editorial_revision_id::text),
                        'editorial_revision_no', v_manifest_editorial_revision_no
                    );
                END IF;
$old$;
            v_new := $new$
$new$;
            IF strpos(v_sql, v_old) = 0 THEN
                RAISE EXCEPTION '0033 downgrade apply hash marker missing';
            END IF;
            v_sql := replace(v_sql, v_old, v_new);

            EXECUTE v_sql;
        END
        $restore_apply$;

        DO $restore_claim$
        DECLARE
            v_sql text;
            v_old text;
            v_new text;
        BEGIN
            SELECT pg_get_functiondef(
                'ops.claim_publication_outbox(text,integer,integer)'::regprocedure
            ) INTO v_sql;
            v_old := $old$
            IF v_count >= p_limit THEN RETURN; END IF;
            FOR claimed_row IN
                SELECT * FROM ops._claim_publication_v132_documents(
                    p_dispatcher_id, p_lease_seconds, p_limit - v_count
                )
            LOOP
                event_id := claimed_row.event_id;
                causation_job_id := claimed_row.causation_job_id;
                aggregate_type := claimed_row.aggregate_type;
                aggregate_id := claimed_row.aggregate_id;
                event_type := claimed_row.event_type;
                event_key := claimed_row.event_key;
                payload := claimed_row.payload;
                attempt_no := claimed_row.attempt_no;
                lease_token := claimed_row.lease_token;
                lease_expires_at := claimed_row.lease_expires_at;
                RETURN NEXT;
            END LOOP;
$old$;
            v_new := $new$
$new$;
            IF strpos(v_sql, v_old) = 0 THEN
                RAISE EXCEPTION '0033 downgrade claim marker missing';
            END IF;
            EXECUTE replace(v_sql, v_old, v_new);
        END
        $restore_claim$;

        CREATE OR REPLACE FUNCTION ops._wp10_3_document_manifest_payload(p_grant_id uuid)
        RETURNS jsonb
        LANGUAGE sql STABLE STRICT
        SET search_path = audit, pg_catalog
        AS $wp10_3_document_manifest_payload$
            SELECT jsonb_build_object(
                'schema', 'publication-manifest.v2',
                'subject_type', 'document',
                'grant_id', lower(manifest.grant_id::text),
                'decision_id', lower(manifest.decision_id::text),
                'subject_id', lower(manifest.document_version_id::text),
                'document_id', lower(manifest.document_id::text),
                'document_version_id', lower(manifest.document_version_id::text),
                'revision_no', grant_row.revision_no,
                'title', manifest.title,
                'summary', to_jsonb(manifest.summary),
                'category', manifest.category::text,
                'fact_status', manifest.fact_status::text,
                'source_name', manifest.source_name,
                'canonical_source_url', manifest.canonical_source_url,
                'source_published_at', CASE
                    WHEN manifest.source_published_at IS NULL THEN 'null'::jsonb
                    ELSE to_jsonb(to_char(
                        manifest.source_published_at AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                    ))
                END,
                'summary_analysis_result_id', to_jsonb(manifest.summary_analysis_result_id)
            )
            FROM audit.document_publication_manifests AS manifest
            JOIN audit.document_publication_grants AS grant_row
              ON grant_row.id = manifest.grant_id
            WHERE manifest.grant_id = p_grant_id
        $wp10_3_document_manifest_payload$;

        DROP FUNCTION IF EXISTS ops._claim_publication_v132_documents(text, integer, integer);
        DROP FUNCTION IF EXISTS ops._validate_publication_editorial_identity(jsonb, uuid);
        RESET ROLE;
        """
    )
