"""V1-3.1 bind publication authorization to an immutable Editorial revision.

The publication sequence already stored in ``revision_no`` is intentionally
left untouched.  This migration adds a separate UUID identity for the
Editorial revision and a narrow SECURITY DEFINER entry point that lets an
``editorial_admin`` submit a document publication review without granting
that role decision authority.

Historical rows remain NULL.  No historical provenance is guessed or
backfilled.
"""

from __future__ import annotations

from alembic import op

revision = "0030_v131_publication_editorial_revision_binding"
down_revision = "0029_v123_editorial_claim_entity_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        ALTER TABLE audit.review_cases
            ADD COLUMN editorial_revision_id uuid
                REFERENCES core.editorial_revisions(id);
        CREATE INDEX ix_review_cases_editorial_revision
            ON audit.review_cases (editorial_revision_id)
            WHERE editorial_revision_id IS NOT NULL;

        ALTER TABLE audit.document_publication_grants
            ADD COLUMN editorial_revision_id uuid
                REFERENCES core.editorial_revisions(id),
            ADD COLUMN editorial_revision_no integer
                CHECK (editorial_revision_no IS NULL OR editorial_revision_no > 0);
        CREATE INDEX ix_document_grants_editorial_revision
            ON audit.document_publication_grants (editorial_revision_id)
            WHERE editorial_revision_id IS NOT NULL;

        ALTER TABLE audit.document_publication_manifests
            ADD COLUMN editorial_revision_id uuid
                REFERENCES core.editorial_revisions(id),
            ADD COLUMN editorial_revision_no integer
                CHECK (editorial_revision_no IS NULL OR editorial_revision_no > 0);
        CREATE INDEX ix_document_manifests_editorial_revision
            ON audit.document_publication_manifests (editorial_revision_id)
            WHERE editorial_revision_id IS NOT NULL;

        -- Preserve the frozen WP10 implementation for legacy cases.  The new
        -- function delegates claim/entity and NULL-bound document cases to it.
        ALTER FUNCTION audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) RENAME TO _apply_publication_grant_legacy;

        CREATE FUNCTION audit._document_publication_payload_bound(
            p_grant_id uuid,
            p_decision_id uuid,
            p_document_version_id uuid,
            p_publication_revision integer,
            p_editorial_revision_id uuid,
            p_changes jsonb
        ) RETURNS jsonb
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, ingest, pg_catalog
        SET timezone = 'UTC'
        AS $document_publication_payload_bound$
        DECLARE
            v_latest_id uuid;
            v_editorial_revision_no integer;
            v_content jsonb;
            v_payload jsonb;
        BEGIN
            IF p_editorial_revision_id IS NULL THEN
                RAISE EXCEPTION 'publication_manifest_invalid' USING ERRCODE = '23503';
            END IF;
            SELECT revision.id, revision.revision_no, revision.content
              INTO v_latest_id, v_editorial_revision_no, v_content
              FROM core.editorial_revisions AS revision
              JOIN core.document_versions AS version
                ON version.id = revision.document_version_id
              JOIN core.documents AS document
                ON document.id = version.document_id
             WHERE revision.id = p_editorial_revision_id
               AND revision.document_version_id = p_document_version_id
               AND revision.content IS NOT NULL
               AND document.deleted_at IS NULL;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'publication_manifest_invalid' USING ERRCODE = '23503';
            END IF;

            SELECT revision.id
              INTO v_latest_id
              FROM core.editorial_revisions AS revision
             WHERE revision.document_version_id = p_document_version_id
               AND revision.content IS NOT NULL
             ORDER BY revision.revision_no DESC, revision.id DESC
             LIMIT 1;
            IF v_latest_id IS DISTINCT FROM p_editorial_revision_id THEN
                RAISE EXCEPTION 'editorial_revision_conflict' USING ERRCODE = '23505';
            END IF;

            -- Keep the frozen publication contract for fact_status and the
            -- summary basis, but require title/summary/category to be the
            -- exact values of the selected immutable Editorial snapshot.
            v_payload := audit._document_publication_payload(
                p_grant_id, p_decision_id, p_document_version_id,
                p_publication_revision, p_changes
            );
            IF (v_content -> 'title') IS DISTINCT FROM (v_payload -> 'title')
               OR (v_content -> 'summary') IS DISTINCT FROM (v_payload -> 'summary')
               OR (v_content -> 'category') IS DISTINCT FROM (v_payload -> 'category')
            THEN
                RAISE EXCEPTION 'publication_manifest_invalid' USING ERRCODE = '22023';
            END IF;
            RETURN v_payload || jsonb_build_object(
                'editorial_revision_id', lower(p_editorial_revision_id::text),
                'editorial_revision_no', v_editorial_revision_no
            );
        END
        $document_publication_payload_bound$;

        CREATE OR REPLACE FUNCTION audit._apply_publication_grant(
            p_case_type audit.review_case_type,
            p_subject_id uuid,
            p_case_id uuid,
            p_decision_id uuid,
            p_decision audit.review_decision
        ) RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, ops, pg_catalog
        AS $_apply_publication_grant$
        DECLARE
            v_table text;
            v_old uuid;
            v_old_rev integer;
            v_new uuid;
            v_rev integer;
            v_sha text;
            v_payload jsonb;
            v_manifest jsonb;
            v_changes jsonb;
            v_editorial_revision_id uuid;
            v_editorial_revision_no integer;
            v_old_editorial_revision_id uuid;
            v_old_editorial_revision_no integer;
        BEGIN
            IF p_case_type <> 'document'::audit.review_case_type THEN
                PERFORM audit._apply_publication_grant_legacy(
                    p_case_type, p_subject_id, p_case_id, p_decision_id, p_decision
                );
                RETURN;
            END IF;

            SELECT review_case.editorial_revision_id
              INTO v_editorial_revision_id
              FROM audit.review_cases AS review_case
             WHERE review_case.id = p_case_id
               AND review_case.document_version_id = p_subject_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'review_case_missing' USING ERRCODE = '23503';
            END IF;

            -- Existing WP9/WP10 cases have no Editorial provenance and retain
            -- their frozen behavior.  Only the new wrapper can create a
            -- document case with a non-NULL binding.
            IF v_editorial_revision_id IS NULL THEN
                PERFORM audit._apply_publication_grant_legacy(
                    p_case_type, p_subject_id, p_case_id, p_decision_id, p_decision
                );
                RETURN;
            END IF;

            SELECT revision.revision_no
              INTO v_editorial_revision_no
              FROM core.editorial_revisions AS revision
              JOIN core.document_versions AS version
                ON version.id = revision.document_version_id
              JOIN core.documents AS document
                ON document.id = version.document_id
             WHERE revision.id = v_editorial_revision_id
               AND revision.document_version_id = p_subject_id
               AND revision.content IS NOT NULL
               AND document.deleted_at IS NULL
               AND revision.id = (
                   SELECT current_revision.id
                     FROM core.editorial_revisions AS current_revision
                    WHERE current_revision.document_version_id = p_subject_id
                      AND current_revision.content IS NOT NULL
                    ORDER BY current_revision.revision_no DESC, current_revision.id DESC
                    LIMIT 1
               );
            IF NOT FOUND THEN
                RAISE EXCEPTION 'editorial_revision_conflict' USING ERRCODE = '23505';
            END IF;

            v_table := 'document_publication_grants';
            SELECT grant_row.id, grant_row.editorial_revision_id,
                   grant_row.editorial_revision_no
              INTO v_old, v_old_editorial_revision_id, v_old_editorial_revision_no
              FROM audit.document_publication_grants AS grant_row
             WHERE grant_row.document_version_id = p_subject_id
               AND grant_row.grant_status = 'active'::audit.grant_status
             FOR UPDATE;
            SELECT coalesce(max(grant_row.revision_no), 0)
              INTO v_old_rev
              FROM audit.document_publication_grants AS grant_row
             WHERE grant_row.document_version_id = p_subject_id;

            IF p_decision IN ('reject'::audit.review_decision,
                              'dispute'::audit.review_decision) THEN
                RETURN;
            END IF;
            IF p_decision = 'withdraw'::audit.review_decision THEN
                IF v_old IS NULL THEN
                    RAISE EXCEPTION 'review_grant_not_active' USING ERRCODE = '22023';
                END IF;
                UPDATE audit.document_publication_grants
                   SET grant_status = 'withdrawn'::audit.grant_status,
                       withdrawn_by_decision_id = p_decision_id,
                       withdrawn_at = clock_timestamp()
                 WHERE id = v_old;
                SELECT publication_payload_sha256, revision_no
                  INTO v_sha, v_rev
                  FROM audit.document_publication_grants
                 WHERE id = v_old;
                v_payload := jsonb_build_object(
                    'schema', 'publication-outbox.v2',
                    'grant_id', lower(v_old::text),
                    'decision_id', lower(p_decision_id::text),
                    'subject_type', 'document',
                    'subject_id', lower(p_subject_id::text),
                    'revision_no', v_rev,
                    'editorial_revision_id', lower(v_old_editorial_revision_id::text),
                    'editorial_revision_no', v_old_editorial_revision_no,
                    'payload_sha256', v_sha
                );
                PERFORM ops.enqueue_publication_outbox(
                    'publication.withdrawn',
                    'publication-withdrawn:' || v_table || ':' || v_old::text || ':' || p_decision_id::text,
                    v_table, v_old, v_payload
                );
                RETURN;
            END IF;

            IF p_decision = 'approve'::audit.review_decision THEN
                IF v_old IS NOT NULL THEN
                    RAISE EXCEPTION 'review_grant_already_active' USING ERRCODE = '22023';
                END IF;
            ELSIF p_decision = 'revise'::audit.review_decision THEN
                IF v_old IS NULL THEN
                    RAISE EXCEPTION 'review_grant_not_active' USING ERRCODE = '22023';
                END IF;
            ELSE
                RAISE EXCEPTION 'review_decision_not_allowed' USING ERRCODE = '22023';
            END IF;

            SELECT structured_changes
              INTO v_changes
              FROM audit.review_decisions
             WHERE id = p_decision_id;
            v_new := gen_random_uuid();
            v_rev := v_old_rev + 1;
            v_manifest := audit._document_publication_payload_bound(
                v_new, p_decision_id, p_subject_id, v_rev,
                v_editorial_revision_id, v_changes
            );
            v_sha := audit._publication_manifest_sha(v_manifest);

            IF p_decision = 'revise'::audit.review_decision THEN
                UPDATE audit.document_publication_grants
                   SET grant_status = 'superseded'::audit.grant_status
                 WHERE id = v_old;
                PERFORM audit._resolve_publication_quarantine(v_table, v_old);
                PERFORM ops.enqueue_publication_outbox(
                    'publication.superseded',
                    'publication-superseded:' || v_table || ':' || v_old::text || ':' || v_new::text,
                    v_table, v_old,
                    jsonb_build_object(
                        'schema', 'publication-outbox.v2',
                        'grant_id', lower(v_new::text),
                        'old_grant_id', lower(v_old::text),
                        'decision_id', lower(p_decision_id::text),
                        'subject_type', 'document',
                        'subject_id', lower(p_subject_id::text),
                        'revision_no', v_rev,
                        'editorial_revision_id', lower(v_editorial_revision_id::text),
                        'editorial_revision_no', v_editorial_revision_no,
                        'payload_sha256', v_sha
                    )
                );
            END IF;

            INSERT INTO audit.document_publication_grants (
                id, review_case_id, document_version_id, decision_id, revision_no,
                grant_status, granted_at, publication_payload_sha256,
                editorial_revision_id, editorial_revision_no
            ) VALUES (
                v_new, p_case_id, p_subject_id, p_decision_id, v_rev,
                'active'::audit.grant_status, clock_timestamp(), v_sha,
                v_editorial_revision_id, v_editorial_revision_no
            );
            INSERT INTO audit.document_publication_manifests (
                grant_id, review_case_id, decision_id, document_id, document_version_id,
                title, summary, category, fact_status, source_name, canonical_source_url,
                source_published_at, summary_analysis_result_id, manifest_sha256,
                editorial_revision_id, editorial_revision_no
            )
            SELECT v_new, p_case_id, p_decision_id,
                   (v_manifest ->> 'document_id')::uuid, p_subject_id,
                   v_manifest ->> 'title',
                   CASE WHEN v_manifest -> 'summary' = 'null'::jsonb
                        THEN NULL ELSE v_manifest ->> 'summary' END,
                   (v_manifest ->> 'category')::public.document_category,
                   (v_manifest ->> 'fact_status')::public.fact_status,
                   v_manifest ->> 'source_name', v_manifest ->> 'canonical_source_url',
                   (v_manifest ->> 'source_published_at')::timestamptz,
                   NULLIF(v_manifest ->> 'summary_analysis_result_id', '')::uuid,
                   v_sha, v_editorial_revision_id, v_editorial_revision_no;

            PERFORM ops.enqueue_publication_outbox(
                'publication.granted',
                'publication-granted:' || v_table || ':' || v_new::text,
                v_table, v_new,
                jsonb_build_object(
                    'schema', 'publication-outbox.v2',
                    'grant_id', lower(v_new::text),
                    'decision_id', lower(p_decision_id::text),
                    'subject_type', 'document',
                    'subject_id', lower(p_subject_id::text),
                    'revision_no', v_rev,
                    'editorial_revision_id', lower(v_editorial_revision_id::text),
                    'editorial_revision_no', v_editorial_revision_no,
                    'payload_sha256', v_sha
                )
            );
        END
        $_apply_publication_grant$;

        CREATE FUNCTION audit.open_document_publication_review_case(
            p_document_version_id uuid,
            p_editorial_revision_id uuid,
            p_priority smallint,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $open_document_publication_review_case$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_case uuid;
            v_revision_no integer;
        BEGIN
            v_actor := audit.require_active_role('editorial_admin'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_reason IS NULL OR char_length(btrim(p_reason)) < 10
               OR p_document_version_id IS NULL OR p_editorial_revision_id IS NULL THEN
                RAISE EXCEPTION 'review_subject_missing' USING ERRCODE = '23503';
            END IF;
            SELECT revision.revision_no
              INTO v_revision_no
              FROM core.editorial_revisions AS revision
              JOIN core.document_versions AS version
                ON version.id = revision.document_version_id
              JOIN core.documents AS document
                ON document.id = version.document_id
             WHERE revision.id = p_editorial_revision_id
               AND revision.document_version_id = p_document_version_id
               AND revision.content IS NOT NULL
               AND document.deleted_at IS NULL
               AND revision.id = (
                   SELECT current_revision.id
                     FROM core.editorial_revisions AS current_revision
                    WHERE current_revision.document_version_id = p_document_version_id
                      AND current_revision.content IS NOT NULL
                    ORDER BY current_revision.revision_no DESC, current_revision.id DESC
                    LIMIT 1
               );
            IF NOT FOUND THEN
                RAISE EXCEPTION 'editorial_revision_conflict' USING ERRCODE = '23505';
            END IF;
            v_payload := jsonb_build_object(
                'case_type', 'document',
                'op', 'review.case.open.publication',
                'priority', coalesce(p_priority, 0),
                'reason', p_reason,
                'subject_id', lower(p_document_version_id::text),
                'editorial_revision_id', lower(p_editorial_revision_id::text),
                'editorial_revision_no', v_revision_no
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.case.open.publication:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;
            v_case := gen_random_uuid();
            BEGIN
                INSERT INTO audit.review_cases (
                    id, document_version_id, editorial_revision_id,
                    case_type, status, priority, opened_by, opened_at
                ) VALUES (
                    v_case, p_document_version_id, p_editorial_revision_id,
                    'document'::audit.review_case_type, 'open'::audit.review_status,
                    coalesce(p_priority, 0), v_actor, clock_timestamp()
                );
            EXCEPTION
                WHEN unique_violation THEN
                    RAISE EXCEPTION 'review_case_already_open' USING ERRCODE = '23505';
            END;
            PERFORM audit.append_audit_event(
                v_key, 'review.case.open.publication', 'review_case', v_case,
                v_request, jsonb_build_object(
                    'payload_sha256', v_sha,
                    'editorial_revision_id', lower(p_editorial_revision_id::text),
                    'editorial_revision_no', v_revision_no
                )
            );
            RETURN v_case;
        END
        $open_document_publication_review_case$;

        REVOKE ALL ON FUNCTION audit._document_publication_payload_bound(
            uuid, uuid, uuid, integer, uuid, jsonb
        ) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit.open_document_publication_review_case(
            uuid, uuid, smallint, text
        ) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION audit.open_document_publication_review_case(
            uuid, uuid, smallint, text
        ) TO uap_api;
        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;
        REVOKE ALL ON FUNCTION audit.open_document_publication_review_case(
            uuid, uuid, smallint, text
        ) FROM PUBLIC, uap_api;
        DROP FUNCTION IF EXISTS audit.open_document_publication_review_case(
            uuid, uuid, smallint, text
        );
        REVOKE ALL ON FUNCTION audit._document_publication_payload_bound(
            uuid, uuid, uuid, integer, uuid, jsonb
        ) FROM PUBLIC;
        DROP FUNCTION IF EXISTS audit._document_publication_payload_bound(
            uuid, uuid, uuid, integer, uuid, jsonb
        );
        DROP FUNCTION IF EXISTS audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        );
        ALTER FUNCTION audit._apply_publication_grant_legacy(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) RENAME TO _apply_publication_grant;
        DROP INDEX IF EXISTS audit.ix_document_manifests_editorial_revision;
        DROP INDEX IF EXISTS audit.ix_document_grants_editorial_revision;
        DROP INDEX IF EXISTS audit.ix_review_cases_editorial_revision;
        ALTER TABLE audit.document_publication_manifests
            DROP COLUMN IF EXISTS editorial_revision_no,
            DROP COLUMN IF EXISTS editorial_revision_id;
        ALTER TABLE audit.document_publication_grants
            DROP COLUMN IF EXISTS editorial_revision_no,
            DROP COLUMN IF EXISTS editorial_revision_id;
        ALTER TABLE audit.review_cases
            DROP COLUMN IF EXISTS editorial_revision_id;
        RESET ROLE;
        """
    )
