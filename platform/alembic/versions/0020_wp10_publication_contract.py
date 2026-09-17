"""Freeze WP10.1 publication manifests, legacy quarantine, and write authority.

Revision ID: 0020_wp10_publication_contract
Revises: 0019_manual_claims_binding
Create Date: 2026-08-28
"""

from __future__ import annotations

from alembic import op

revision = "0020_wp10_publication_contract"
down_revision = "0019_manual_claims_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        -- Do not silently adopt an existing public projection.  Before this
        -- revision there is no typed manifest that can prove its provenance.
        DO $wp10_public_preflight$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.documents)
               OR EXISTS (SELECT 1 FROM public.claims)
               OR EXISTS (SELECT 1 FROM public.evidence)
               OR EXISTS (SELECT 1 FROM public.claim_evidence)
               OR EXISTS (SELECT 1 FROM public.entities)
               OR EXISTS (SELECT 1 FROM public.document_entities)
               OR EXISTS (SELECT 1 FROM public.relations)
               OR EXISTS (SELECT 1 FROM public.relation_evidence)
               OR EXISTS (SELECT 1 FROM public.search_documents)
            THEN
                RAISE EXCEPTION 'publication_manifest_invalid'
                    USING ERRCODE = '23514';
            END IF;
        END
        $wp10_public_preflight$;

        CREATE TYPE public.document_category AS ENUM (
            'official_report', 'government_document', 'military',
            'scientific_research', 'historical_event', 'sighting',
            'disputed_event', 'other'
        );
        CREATE TYPE public.fact_status AS ENUM (
            'official_record', 'corroborated', 'source_reported',
            'unverified', 'disputed', 'opinion'
        );

        DO $wp10_enum_preflight$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.documents
                 WHERE category NOT IN (
                    'official_report', 'government_document', 'military',
                    'scientific_research', 'historical_event', 'sighting',
                    'disputed_event', 'other'
                 )
            ) OR EXISTS (
                SELECT 1 FROM public.documents
                 WHERE fact_status NOT IN (
                    'official_record', 'corroborated', 'source_reported',
                    'unverified', 'disputed', 'opinion'
                 )
            ) THEN
                RAISE EXCEPTION 'publication_manifest_invalid'
                    USING ERRCODE = '23514';
            END IF;
        END
        $wp10_enum_preflight$;

        ALTER TABLE public.documents
            ALTER COLUMN category TYPE public.document_category
                USING category::public.document_category,
            ALTER COLUMN fact_status TYPE public.fact_status
                USING fact_status::public.fact_status;

        CREATE TABLE audit.document_publication_manifests (
            grant_id uuid PRIMARY KEY
                REFERENCES audit.document_publication_grants(id),
            review_case_id uuid NOT NULL,
            decision_id uuid NOT NULL,
            document_id uuid NOT NULL REFERENCES core.documents(id),
            document_version_id uuid NOT NULL REFERENCES core.document_versions(id),
            title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 500),
            summary text CHECK (summary IS NULL OR char_length(summary) <= 20000),
            category public.document_category NOT NULL,
            fact_status public.fact_status NOT NULL,
            source_name text NOT NULL CHECK (char_length(source_name) > 0),
            canonical_source_url text NOT NULL,
            source_published_at timestamptz,
            summary_analysis_result_id uuid,
            manifest_sha256 char(64) NOT NULL
                CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (grant_id, document_version_id),
            FOREIGN KEY (review_case_id, document_version_id)
                REFERENCES audit.review_cases(id, document_version_id),
            FOREIGN KEY (decision_id, review_case_id)
                REFERENCES audit.review_decisions(id, review_case_id)
        );

        CREATE TABLE audit.claim_publication_manifests (
            grant_id uuid PRIMARY KEY
                REFERENCES audit.claim_publication_grants(id),
            review_case_id uuid NOT NULL,
            decision_id uuid NOT NULL,
            claim_id uuid NOT NULL REFERENCES core.claims(id),
            document_id uuid NOT NULL REFERENCES core.documents(id),
            document_version_id uuid NOT NULL REFERENCES core.document_versions(id),
            claim_text text NOT NULL CHECK (char_length(claim_text) > 0),
            claim_type core.claim_type NOT NULL,
            assertion_status core.assertion_status NOT NULL,
            attribution text,
            subject_entity_id uuid,
            manifest_sha256 char(64) NOT NULL
                CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (grant_id, claim_id),
            FOREIGN KEY (review_case_id, claim_id)
                REFERENCES audit.review_cases(id, claim_id),
            FOREIGN KEY (decision_id, review_case_id)
                REFERENCES audit.review_decisions(id, review_case_id),
            FOREIGN KEY (claim_id, document_version_id)
                REFERENCES core.claims(id, document_version_id),
            FOREIGN KEY (subject_entity_id) REFERENCES core.entities(id)
        );

        CREATE TABLE audit.claim_publication_manifest_evidence (
            grant_id uuid NOT NULL
                REFERENCES audit.claim_publication_manifests(grant_id),
            evidence_ordinal integer NOT NULL CHECK (evidence_ordinal BETWEEN 0 AND 19),
            evidence_span_id uuid NOT NULL,
            document_version_id uuid NOT NULL REFERENCES core.document_versions(id),
            excerpt text NOT NULL CHECK (char_length(excerpt) BETWEEN 1 AND 2000),
            locator_type core.locator_type NOT NULL,
            char_start integer,
            char_end integer,
            page_start integer,
            page_end integer,
            time_start_ms bigint,
            time_end_ms bigint,
            public_locator jsonb NOT NULL,
            locator_sha256 char(64) NOT NULL
                CHECK (locator_sha256 ~ '^[0-9a-f]{64}$'),
            source_url text NOT NULL,
            PRIMARY KEY (grant_id, evidence_ordinal),
            UNIQUE (grant_id, evidence_span_id),
            FOREIGN KEY (evidence_span_id, document_version_id)
                REFERENCES core.evidence_spans(id, document_version_id),
            CHECK (char_start IS NULL OR (char_start >= 0 AND char_end >= char_start)),
            CHECK (page_start IS NULL OR (page_start > 0 AND page_end >= page_start)),
            CHECK (time_start_ms IS NULL OR (time_start_ms >= 0 AND time_end_ms >= time_start_ms))
        );

        CREATE TABLE audit.entity_publication_manifests (
            grant_id uuid PRIMARY KEY
                REFERENCES audit.entity_publication_grants(id),
            review_case_id uuid NOT NULL,
            decision_id uuid NOT NULL,
            entity_id uuid NOT NULL REFERENCES core.entities(id),
            entity_type core.entity_type NOT NULL,
            canonical_name text NOT NULL CHECK (char_length(canonical_name) > 0),
            description text,
            country_code char(2),
            manifest_sha256 char(64) NOT NULL
                CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (grant_id, entity_id),
            FOREIGN KEY (review_case_id, entity_id)
                REFERENCES audit.review_cases(id, entity_id),
            FOREIGN KEY (decision_id, review_case_id)
                REFERENCES audit.review_decisions(id, review_case_id)
        );

        CREATE TABLE audit.document_public_identities (
            document_id uuid PRIMARY KEY REFERENCES core.documents(id),
            public_id uuid NOT NULL UNIQUE,
            slug text NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (slug = 'd-' || replace(public_id::text, '-', ''))
        );
        CREATE TABLE audit.claim_public_identities (
            claim_id uuid PRIMARY KEY REFERENCES core.claims(id),
            document_id uuid NOT NULL REFERENCES core.documents(id),
            public_id uuid NOT NULL UNIQUE,
            display_ordinal integer NOT NULL CHECK (display_ordinal >= 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (document_id, display_ordinal),
            UNIQUE (claim_id, document_id)
        );
        -- claim_id/document_id is a logical-to-version relation.  Capture
        -- validates that the logical document owns the claim's version; the
        -- identity table keeps the stable public document anchor.

        CREATE TABLE audit.evidence_public_identities (
            evidence_span_id uuid PRIMARY KEY REFERENCES core.evidence_spans(id),
            public_id uuid NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE audit.entity_public_identities (
            entity_id uuid PRIMARY KEY REFERENCES core.entities(id),
            public_id uuid NOT NULL UNIQUE,
            slug text NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (slug = 'e-' || replace(public_id::text, '-', ''))
        );

        CREATE TABLE audit.publication_quarantine (
            id uuid PRIMARY KEY,
            grant_table text,
            grant_id uuid,
            event_id uuid,
            reason_code text NOT NULL CHECK (reason_code = 'publication_manifest_required'),
            resolves_quarantine_id uuid
                REFERENCES audit.publication_quarantine(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            resolved_at timestamptz,
            resolution text,
            CHECK (
                (resolves_quarantine_id IS NULL AND resolved_at IS NULL AND resolution IS NULL)
                OR (resolves_quarantine_id IS NOT NULL AND resolved_at IS NOT NULL AND resolution IS NOT NULL)
            ),
            CHECK (grant_id IS NOT NULL OR event_id IS NOT NULL),
            UNIQUE NULLS NOT DISTINCT (grant_table, grant_id, event_id, resolves_quarantine_id)
        );

        ALTER TABLE ops.outbox_events
            ADD COLUMN terminal_at timestamptz,
            ADD COLUMN terminal_error_code text;
        ALTER TABLE ops.outbox_events
            ADD CONSTRAINT ck_outbox_published_terminal CHECK (
                terminal_at IS NULL OR (published_at IS NULL AND terminal_error_code IS NOT NULL)
            );

        CREATE FUNCTION audit.reject_publication_contract_mutation() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = audit, pg_catalog
        AS $reject_publication_contract_mutation$
        BEGIN
            RAISE EXCEPTION 'publication contract history is immutable'
                USING ERRCODE = '55000';
        END
        $reject_publication_contract_mutation$;

        CREATE TRIGGER document_publication_manifests_append_only
            BEFORE UPDATE OR DELETE ON audit.document_publication_manifests
            FOR EACH ROW EXECUTE FUNCTION audit.reject_publication_contract_mutation();
        CREATE TRIGGER claim_publication_manifests_append_only
            BEFORE UPDATE OR DELETE ON audit.claim_publication_manifests
            FOR EACH ROW EXECUTE FUNCTION audit.reject_publication_contract_mutation();
        CREATE TRIGGER claim_manifest_evidence_append_only
            BEFORE UPDATE OR DELETE ON audit.claim_publication_manifest_evidence
            FOR EACH ROW EXECUTE FUNCTION audit.reject_publication_contract_mutation();
        CREATE TRIGGER entity_publication_manifests_append_only
            BEFORE UPDATE OR DELETE ON audit.entity_publication_manifests
            FOR EACH ROW EXECUTE FUNCTION audit.reject_publication_contract_mutation();
        CREATE TRIGGER document_public_identities_append_only
            BEFORE UPDATE OR DELETE ON audit.document_public_identities
            FOR EACH ROW EXECUTE FUNCTION audit.reject_publication_contract_mutation();
        CREATE TRIGGER claim_public_identities_append_only
            BEFORE UPDATE OR DELETE ON audit.claim_public_identities
            FOR EACH ROW EXECUTE FUNCTION audit.reject_publication_contract_mutation();
        CREATE TRIGGER evidence_public_identities_append_only
            BEFORE UPDATE OR DELETE ON audit.evidence_public_identities
            FOR EACH ROW EXECUTE FUNCTION audit.reject_publication_contract_mutation();
        CREATE TRIGGER entity_public_identities_append_only
            BEFORE UPDATE OR DELETE ON audit.entity_public_identities
            FOR EACH ROW EXECUTE FUNCTION audit.reject_publication_contract_mutation();
        CREATE TRIGGER publication_quarantine_append_only
            BEFORE UPDATE OR DELETE ON audit.publication_quarantine
            FOR EACH ROW EXECUTE FUNCTION audit.reject_publication_contract_mutation();

        CREATE FUNCTION audit.require_claim_publication_manifest_evidence() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, pg_catalog
        AS $require_claim_publication_manifest_evidence$
        DECLARE
            v_grant_id uuid;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                v_grant_id := OLD.grant_id;
            ELSE
                v_grant_id := NEW.grant_id;
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM audit.claim_publication_manifests AS manifest
                 WHERE manifest.grant_id = v_grant_id
            ) AND NOT EXISTS (
                SELECT 1
                  FROM audit.claim_publication_manifest_evidence AS evidence
                 WHERE evidence.grant_id = v_grant_id
            ) THEN
                RAISE EXCEPTION 'publication_evidence_required'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END
        $require_claim_publication_manifest_evidence$;

        CREATE CONSTRAINT TRIGGER claim_publication_manifest_evidence_required
            AFTER INSERT ON audit.claim_publication_manifests
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION audit.require_claim_publication_manifest_evidence();
        CREATE CONSTRAINT TRIGGER claim_publication_manifest_evidence_present
            AFTER INSERT OR UPDATE OR DELETE ON audit.claim_publication_manifest_evidence
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION audit.require_claim_publication_manifest_evidence();

        CREATE FUNCTION audit.publication_source_url_valid(p_url text) RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT
        SET search_path = pg_catalog
        AS $publication_source_url_valid$
            SELECT p_url ~* '^https?://[^/?#]+(?:[/?][^#]*)?$'
               AND position('@' IN substring(p_url FROM '^https?://([^/?#]+)')) = 0
               AND position('#' IN p_url) = 0;
        $publication_source_url_valid$;

        CREATE FUNCTION audit._publication_manifest_sha(p_manifest jsonb) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT
        SET search_path = audit, pg_catalog
        AS $_publication_manifest_sha$
            SELECT audit._payload_sha256(p_manifest)
        $_publication_manifest_sha$;

        CREATE FUNCTION audit._document_publication_payload(
            p_grant_id uuid,
            p_decision_id uuid,
            p_document_version_id uuid,
            p_revision integer,
            p_changes jsonb
        ) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = audit, core, ingest, pg_catalog
        SET timezone = 'UTC'
        AS $_document_publication_payload$
        DECLARE
            v_publication jsonb;
            v_document_id uuid;
            v_source_name text;
            v_source_url text;
            v_source_published_at timestamptz;
            v_title text;
            v_summary text;
            v_category text;
            v_fact_status text;
            v_summary_result uuid;
            v_result_summary text;
        BEGIN
            IF p_changes IS NULL OR jsonb_typeof(p_changes) IS DISTINCT FROM 'object'
               OR (SELECT count(*) FROM jsonb_object_keys(p_changes)) <> 1
               OR NOT p_changes ? 'publication'
               OR jsonb_typeof(p_changes -> 'publication') IS DISTINCT FROM 'object'
            THEN
                RAISE EXCEPTION 'publication_manifest_invalid' USING ERRCODE = '22023';
            END IF;
            v_publication := p_changes -> 'publication';
            IF EXISTS (
                SELECT 1 FROM jsonb_object_keys(v_publication) AS key
                 WHERE key NOT IN (
                    'title', 'summary', 'category', 'fact_status',
                    'summary_analysis_result_id'
                 )
            ) OR NOT (
                v_publication ? 'title'
                AND v_publication ? 'summary'
                AND v_publication ? 'category'
                AND v_publication ? 'fact_status'
                AND v_publication ? 'summary_analysis_result_id'
            ) OR (SELECT count(*) FROM jsonb_object_keys(v_publication)) <> 5 THEN
                RAISE EXCEPTION 'publication_manifest_invalid' USING ERRCODE = '22023';
            END IF;

            v_title := v_publication ->> 'title';
            v_summary := CASE WHEN v_publication -> 'summary' = 'null'::jsonb
                              THEN NULL ELSE v_publication ->> 'summary' END;
            v_category := v_publication ->> 'category';
            v_fact_status := v_publication ->> 'fact_status';
            IF v_title IS NULL OR char_length(btrim(v_title)) = 0
               OR char_length(v_title) > 500
               OR (v_summary IS NOT NULL AND char_length(v_summary) > 20000)
               OR v_category NOT IN (
                    'official_report', 'government_document', 'military',
                    'scientific_research', 'historical_event', 'sighting',
                    'disputed_event', 'other'
               )
               OR v_fact_status NOT IN (
                    'official_record', 'corroborated', 'source_reported',
                    'unverified', 'disputed', 'opinion'
               )
            THEN
                RAISE EXCEPTION 'publication_manifest_invalid' USING ERRCODE = '22023';
            END IF;

            BEGIN
                v_summary_result := NULLIF(v_publication ->> 'summary_analysis_result_id', '')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'publication_manifest_invalid' USING ERRCODE = '22023';
            END;
            SELECT document.id, source.name, document.canonical_url,
                   version.source_published_at
              INTO v_document_id, v_source_name, v_source_url, v_source_published_at
              FROM core.document_versions AS version
              JOIN core.documents AS document ON document.id = version.document_id
              JOIN ingest.sources AS source ON source.id = document.source_id
             WHERE version.id = p_document_version_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'publication_manifest_invalid' USING ERRCODE = '23503';
            END IF;
            IF v_source_url IS NULL OR NOT audit.publication_source_url_valid(v_source_url) THEN
                RAISE EXCEPTION 'publication_source_url_missing' USING ERRCODE = '22023';
            END IF;
            IF v_summary_result IS NOT NULL THEN
                SELECT result ->> 'summary'
                  INTO v_result_summary
                  FROM core.analysis_results AS result_row
                 WHERE result_row.id = v_summary_result
                   AND result_row.document_version_id = p_document_version_id
                   AND result_row.result_type = 'summary'::ops.model_task_type
                   AND result_row.validation_status = 'valid'::core.validation_status;
                IF NOT FOUND OR v_result_summary IS DISTINCT FROM v_summary THEN
                    RAISE EXCEPTION 'publication_manifest_invalid' USING ERRCODE = '22023';
                END IF;
            END IF;
            RETURN jsonb_build_object(
                'schema', 'publication-manifest.v2',
                'subject_type', 'document',
                'grant_id', lower(p_grant_id::text),
                'decision_id', lower(p_decision_id::text),
                'subject_id', lower(p_document_version_id::text),
                'document_id', lower(v_document_id::text),
                'document_version_id', lower(p_document_version_id::text),
                'revision_no', p_revision,
                'title', v_title,
                'summary', to_jsonb(v_summary),
                'category', v_category,
                'fact_status', v_fact_status,
                'source_name', v_source_name,
                'canonical_source_url', v_source_url,
                -- Do not let a caller's session TimeZone affect the signed
                -- manifest.  The stored value remains timestamptz; the
                -- canonical payload uses an explicit UTC representation.
                'source_published_at', CASE
                    WHEN v_source_published_at IS NULL THEN 'null'::jsonb
                    ELSE to_jsonb(to_char(
                        v_source_published_at AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                    ))
                END,
                'summary_analysis_result_id', to_jsonb(v_summary_result)
            );
        END
        $_document_publication_payload$;

        CREATE FUNCTION audit._claim_publication_payload(
            p_grant_id uuid,
            p_decision_id uuid,
            p_claim_id uuid,
            p_revision integer
        ) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = audit, core, ingest, pg_catalog
        AS $_claim_publication_payload$
        DECLARE
            v_claim core.claims%ROWTYPE;
            v_document_id uuid;
            v_document_grant_id uuid;
            v_source_url text;
            v_evidence jsonb;
            v_subject text;
        BEGIN
            SELECT claim.* INTO v_claim FROM core.claims AS claim
             WHERE claim.id = p_claim_id FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'publication_manifest_invalid' USING ERRCODE = '23503';
            END IF;
            SELECT document.id, document.canonical_url
              INTO v_document_id, v_source_url
              FROM core.document_versions AS version
              JOIN core.documents AS document ON document.id = version.document_id
             WHERE version.id = v_claim.document_version_id;
            IF NOT FOUND OR v_source_url IS NULL
               OR NOT audit.publication_source_url_valid(v_source_url) THEN
                RAISE EXCEPTION 'publication_source_url_missing' USING ERRCODE = '22023';
            END IF;
            -- Claim publication and document withdrawal/revision serialize on
            -- the same active document grant row.  The lock is part of the
            -- check: a concurrent withdrawal cannot pass this check after
            -- the grant has ceased to be active.
            SELECT grant_row.id
              INTO v_document_grant_id
              FROM audit.document_publication_grants AS grant_row
              JOIN audit.document_publication_manifests AS manifest
                ON manifest.grant_id = grant_row.id
             WHERE grant_row.document_version_id = v_claim.document_version_id
               AND grant_row.grant_status = 'active'::audit.grant_status
             FOR UPDATE OF grant_row;
            IF v_document_grant_id IS NULL THEN
                RAISE EXCEPTION 'publication_document_grant_required' USING ERRCODE = '23514';
            END IF;
            SELECT coalesce(jsonb_agg(jsonb_build_object(
                       'evidence_span_id', lower(span.id::text),
                       'evidence_ordinal', row_data.evidence_ordinal,
                       'excerpt', span.evidence_text,
                       'locator_type', span.locator_type::text,
                       'char_start', to_jsonb(span.char_start),
                       'char_end', to_jsonb(span.char_end),
                       'page_start', to_jsonb(span.page_start),
                       'page_end', to_jsonb(span.page_end),
                       'time_start_ms', to_jsonb(span.time_start_ms),
                       'time_end_ms', to_jsonb(span.time_end_ms),
                       'public_locator', span.locator,
                       'locator_sha256', span.locator_sha256,
                       'source_url', v_source_url
                   ) ORDER BY row_data.evidence_ordinal), '[]'::jsonb)
              INTO v_evidence
              FROM (
                  SELECT evidence_span_id,
                         row_number() OVER (ORDER BY id) - 1 AS evidence_ordinal
                    FROM core.claim_evidence
                   WHERE claim_id = p_claim_id
                     AND support_type = 'supports'::core.support_type
              ) AS row_data
              JOIN core.evidence_spans AS span ON span.id = row_data.evidence_span_id;
            IF jsonb_array_length(v_evidence) < 1 OR jsonb_array_length(v_evidence) > 20 THEN
                RAISE EXCEPTION 'publication_evidence_required' USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1 FROM jsonb_array_elements(v_evidence) AS item
                 WHERE char_length(item.value ->> 'excerpt') > 2000
                    OR char_length(item.value ->> 'excerpt') < 1
            ) THEN
                RAISE EXCEPTION 'publication_evidence_excerpt_too_long' USING ERRCODE = '22023';
            END IF;
            v_subject := CASE WHEN v_claim.subject_entity_id IS NULL
                              THEN NULL ELSE lower(v_claim.subject_entity_id::text) END;
            RETURN jsonb_build_object(
                'schema', 'publication-manifest.v2',
                'subject_type', 'claim',
                'grant_id', lower(p_grant_id::text),
                'decision_id', lower(p_decision_id::text),
                'subject_id', lower(p_claim_id::text),
                'document_id', lower(v_document_id::text),
                'document_version_id', lower(v_claim.document_version_id::text),
                'revision_no', p_revision,
                'claim_text', v_claim.claim_text,
                'claim_type', v_claim.claim_type::text,
                'assertion_status', v_claim.assertion_status::text,
                'attribution', to_jsonb(v_claim.attribution),
                'subject_entity_id', to_jsonb(v_subject),
                'evidence', v_evidence
            );
        END
        $_claim_publication_payload$;

        CREATE FUNCTION audit._entity_publication_payload(
            p_grant_id uuid,
            p_decision_id uuid,
            p_entity_id uuid,
            p_revision integer
        ) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $_entity_publication_payload$
        DECLARE
            v_entity core.entities%ROWTYPE;
            v_canonical uuid;
        BEGIN
            SELECT entity.* INTO v_entity FROM core.entities AS entity
             WHERE entity.id = p_entity_id FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'publication_manifest_invalid' USING ERRCODE = '23503';
            END IF;
            v_canonical := core.canonical_entity_id(p_entity_id);
            IF v_canonical IS DISTINCT FROM p_entity_id
               OR v_entity.status IS DISTINCT FROM 'active'::core.entity_status THEN
                RAISE EXCEPTION 'publication_manifest_invalid' USING ERRCODE = '22023';
            END IF;
            RETURN jsonb_build_object(
                'schema', 'publication-manifest.v2',
                'subject_type', 'entity',
                'grant_id', lower(p_grant_id::text),
                'decision_id', lower(p_decision_id::text),
                'subject_id', lower(p_entity_id::text),
                'revision_no', p_revision,
                'entity_type', v_entity.entity_type::text,
                'canonical_name', v_entity.canonical_name,
                'description', to_jsonb(v_entity.description),
                'country_code', to_jsonb(v_entity.country_code)
            );
        END
        $_entity_publication_payload$;

        CREATE FUNCTION audit._resolve_publication_quarantine(
            p_grant_table text,
            p_grant_id uuid
        ) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = audit, ops, pg_catalog
        AS $_resolve_publication_quarantine$
        BEGIN
            IF p_grant_table IS NULL OR p_grant_id IS NULL THEN
                RETURN;
            END IF;

            -- Quarantine is append-only: a v2 revise records one resolution
            -- row for the legacy grant row and for every matching v1 event.
            INSERT INTO audit.publication_quarantine (
                id, grant_table, grant_id, reason_code,
                resolves_quarantine_id, resolved_at, resolution
            )
            SELECT gen_random_uuid(), quarantine.grant_table, quarantine.grant_id,
                   quarantine.reason_code, quarantine.id, clock_timestamp(),
                   'resolved_by_v2_revision'
              FROM audit.publication_quarantine AS quarantine
             WHERE quarantine.grant_table = p_grant_table
               AND quarantine.grant_id = p_grant_id
               AND quarantine.resolves_quarantine_id IS NULL
               AND NOT EXISTS (
                    SELECT 1
                      FROM audit.publication_quarantine AS resolution
                     WHERE resolution.resolves_quarantine_id = quarantine.id
               );

            INSERT INTO audit.publication_quarantine (
                id, grant_table, grant_id, event_id, reason_code,
                resolves_quarantine_id, resolved_at, resolution
            )
            SELECT gen_random_uuid(), quarantine.grant_table, quarantine.grant_id,
                   quarantine.event_id, quarantine.reason_code, quarantine.id,
                   clock_timestamp(), 'resolved_by_v2_revision'
              FROM audit.publication_quarantine AS quarantine
              JOIN ops.outbox_events AS event_row
                ON event_row.id = quarantine.event_id
             WHERE quarantine.grant_table IS NULL
               AND quarantine.grant_id IS NULL
               AND quarantine.event_id IS NOT NULL
               AND quarantine.resolves_quarantine_id IS NULL
               AND event_row.aggregate_type = p_grant_table
               AND event_row.aggregate_id = p_grant_id
               AND event_row.payload ->> 'schema' = 'publication-outbox.v1'
               AND NOT EXISTS (
                    SELECT 1
                      FROM audit.publication_quarantine AS resolution
                     WHERE resolution.resolves_quarantine_id = quarantine.id
               );
        END
        $_resolve_publication_quarantine$;

        CREATE OR REPLACE FUNCTION audit._apply_publication_grant(
            p_case_type audit.review_case_type,
            p_subject_id uuid,
            p_case_id uuid,
            p_decision_id uuid,
            p_decision audit.review_decision
        ) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
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
            v_is_v2 boolean;
        BEGIN
            SELECT structured_changes INTO v_changes
              FROM audit.review_decisions WHERE id = p_decision_id;
            IF p_case_type = 'document'::audit.review_case_type THEN
                v_table := 'document_publication_grants';
                SELECT id INTO v_old FROM audit.document_publication_grants
                 WHERE document_version_id = p_subject_id
                   AND grant_status = 'active'::audit.grant_status
                 FOR UPDATE;
                SELECT coalesce(max(revision_no), 0) INTO v_old_rev
                  FROM audit.document_publication_grants WHERE document_version_id = p_subject_id;
            ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                v_table := 'claim_publication_grants';
                SELECT id INTO v_old FROM audit.claim_publication_grants
                 WHERE claim_id = p_subject_id
                   AND grant_status = 'active'::audit.grant_status
                 FOR UPDATE;
                SELECT coalesce(max(revision_no), 0) INTO v_old_rev
                  FROM audit.claim_publication_grants WHERE claim_id = p_subject_id;
            ELSIF p_case_type = 'entity'::audit.review_case_type THEN
                v_table := 'entity_publication_grants';
                SELECT id INTO v_old FROM audit.entity_publication_grants
                 WHERE entity_id = p_subject_id
                   AND grant_status = 'active'::audit.grant_status
                 FOR UPDATE;
                SELECT coalesce(max(revision_no), 0) INTO v_old_rev
                  FROM audit.entity_publication_grants WHERE entity_id = p_subject_id;
            ELSE
                RAISE EXCEPTION 'knowledge_relation_review_not_in_wp9' USING ERRCODE = '22023';
            END IF;

            IF p_decision IN ('reject'::audit.review_decision, 'dispute'::audit.review_decision) THEN
                RETURN;
            END IF;
            IF p_decision = 'withdraw'::audit.review_decision THEN
                IF v_old IS NULL THEN
                    RAISE EXCEPTION 'review_grant_not_active' USING ERRCODE = '22023';
                END IF;
                IF p_case_type = 'document'::audit.review_case_type THEN
                    UPDATE audit.document_publication_grants SET grant_status = 'withdrawn',
                        withdrawn_by_decision_id = p_decision_id, withdrawn_at = clock_timestamp()
                     WHERE id = v_old;
                    v_is_v2 := EXISTS (SELECT 1 FROM audit.document_publication_manifests WHERE grant_id = v_old);
                ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                    UPDATE audit.claim_publication_grants SET grant_status = 'withdrawn',
                        withdrawn_by_decision_id = p_decision_id, withdrawn_at = clock_timestamp()
                     WHERE id = v_old;
                    v_is_v2 := EXISTS (SELECT 1 FROM audit.claim_publication_manifests WHERE grant_id = v_old);
                ELSE
                    UPDATE audit.entity_publication_grants SET grant_status = 'withdrawn',
                        withdrawn_by_decision_id = p_decision_id, withdrawn_at = clock_timestamp()
                     WHERE id = v_old;
                    v_is_v2 := EXISTS (SELECT 1 FROM audit.entity_publication_manifests WHERE grant_id = v_old);
                END IF;
                SELECT publication_payload_sha256, revision_no INTO v_sha, v_rev
                  FROM audit.document_publication_grants WHERE id = v_old;
                IF p_case_type = 'claim'::audit.review_case_type THEN
                    SELECT publication_payload_sha256, revision_no INTO v_sha, v_rev
                      FROM audit.claim_publication_grants WHERE id = v_old;
                ELSIF p_case_type = 'entity'::audit.review_case_type THEN
                    SELECT publication_payload_sha256, revision_no INTO v_sha, v_rev
                      FROM audit.entity_publication_grants WHERE id = v_old;
                END IF;
                v_payload := jsonb_build_object(
                    'schema', CASE WHEN v_is_v2 THEN 'publication-outbox.v2' ELSE 'publication-outbox.v1' END,
                    'grant_id', lower(v_old::text), 'decision_id', lower(p_decision_id::text),
                    'subject_type', p_case_type::text, 'subject_id', lower(p_subject_id::text),
                    'revision_no', v_rev, 'payload_sha256', v_sha
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
            v_new := gen_random_uuid();
            v_rev := v_old_rev + 1;
            IF p_case_type = 'document'::audit.review_case_type THEN
                v_manifest := audit._document_publication_payload(
                    v_new, p_decision_id, p_subject_id, v_rev, v_changes
                );
            ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                v_manifest := audit._claim_publication_payload(
                    v_new, p_decision_id, p_subject_id, v_rev
                );
            ELSE
                v_manifest := audit._entity_publication_payload(
                    v_new, p_decision_id, p_subject_id, v_rev
                );
            END IF;
            v_sha := audit._publication_manifest_sha(v_manifest);

            IF p_decision = 'revise'::audit.review_decision THEN
                IF p_case_type = 'document'::audit.review_case_type THEN
                    UPDATE audit.document_publication_grants SET grant_status = 'superseded' WHERE id = v_old;
                ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                    UPDATE audit.claim_publication_grants SET grant_status = 'superseded' WHERE id = v_old;
                ELSE
                    UPDATE audit.entity_publication_grants SET grant_status = 'superseded' WHERE id = v_old;
                END IF;
            END IF;

            IF p_case_type = 'document'::audit.review_case_type THEN
                INSERT INTO audit.document_publication_grants
                    (id, review_case_id, document_version_id, decision_id, revision_no,
                     grant_status, granted_at, publication_payload_sha256)
                VALUES (v_new, p_case_id, p_subject_id, p_decision_id, v_rev,
                        'active', clock_timestamp(), v_sha);
                INSERT INTO audit.document_publication_manifests
                    (grant_id, review_case_id, decision_id, document_id, document_version_id,
                     title, summary, category, fact_status, source_name, canonical_source_url,
                     source_published_at, summary_analysis_result_id, manifest_sha256)
                SELECT v_new, p_case_id, p_decision_id,
                       (v_manifest ->> 'document_id')::uuid, p_subject_id,
                       v_manifest ->> 'title',
                       CASE WHEN v_manifest -> 'summary' = 'null'::jsonb THEN NULL ELSE v_manifest ->> 'summary' END,
                       (v_manifest ->> 'category')::public.document_category,
                       (v_manifest ->> 'fact_status')::public.fact_status,
                       v_manifest ->> 'source_name', v_manifest ->> 'canonical_source_url',
                       (v_manifest ->> 'source_published_at')::timestamptz,
                       NULLIF(v_manifest ->> 'summary_analysis_result_id', '')::uuid, v_sha;
            ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                INSERT INTO audit.claim_publication_grants
                    (id, review_case_id, claim_id, decision_id, revision_no,
                     grant_status, granted_at, publication_payload_sha256)
                VALUES (v_new, p_case_id, p_subject_id, p_decision_id, v_rev,
                        'active', clock_timestamp(), v_sha);
                INSERT INTO audit.claim_publication_manifests
                    (grant_id, review_case_id, decision_id, claim_id, document_id, document_version_id,
                     claim_text, claim_type, assertion_status, attribution, subject_entity_id, manifest_sha256)
                VALUES (v_new, p_case_id, p_decision_id, p_subject_id,
                        (v_manifest ->> 'document_id')::uuid,
                        (v_manifest ->> 'document_version_id')::uuid,
                        v_manifest ->> 'claim_text',
                        (v_manifest ->> 'claim_type')::core.claim_type,
                        (v_manifest ->> 'assertion_status')::core.assertion_status,
                        CASE WHEN v_manifest -> 'attribution' = 'null'::jsonb THEN NULL ELSE v_manifest ->> 'attribution' END,
                        NULLIF(v_manifest ->> 'subject_entity_id', '')::uuid, v_sha);
                INSERT INTO audit.claim_publication_manifest_evidence
                    (grant_id, evidence_ordinal, evidence_span_id, document_version_id, excerpt,
                     locator_type, char_start, char_end, page_start, page_end, time_start_ms,
                     time_end_ms, public_locator, locator_sha256, source_url)
                SELECT v_new, (item ->> 'evidence_ordinal')::integer,
                       (item ->> 'evidence_span_id')::uuid,
                       (v_manifest ->> 'document_version_id')::uuid,
                       item ->> 'excerpt', (item ->> 'locator_type')::core.locator_type,
                       (item ->> 'char_start')::integer, (item ->> 'char_end')::integer,
                       (item ->> 'page_start')::integer, (item ->> 'page_end')::integer,
                       (item ->> 'time_start_ms')::bigint, (item ->> 'time_end_ms')::bigint,
                       item -> 'public_locator', item ->> 'locator_sha256', item ->> 'source_url'
                  FROM jsonb_array_elements(v_manifest -> 'evidence') AS item;
            ELSE
                INSERT INTO audit.entity_publication_grants
                    (id, review_case_id, entity_id, decision_id, revision_no,
                     grant_status, granted_at, publication_payload_sha256)
                VALUES (v_new, p_case_id, p_subject_id, p_decision_id, v_rev,
                        'active', clock_timestamp(), v_sha);
                INSERT INTO audit.entity_publication_manifests
                    (grant_id, review_case_id, decision_id, entity_id, entity_type,
                     canonical_name, description, country_code, manifest_sha256)
                VALUES (v_new, p_case_id, p_decision_id, p_subject_id,
                        (v_manifest ->> 'entity_type')::core.entity_type,
                        v_manifest ->> 'canonical_name',
                        CASE WHEN v_manifest -> 'description' = 'null'::jsonb THEN NULL ELSE v_manifest ->> 'description' END,
                        NULLIF(v_manifest ->> 'country_code', '')::char(2), v_sha);
            END IF;

            IF p_decision = 'revise'::audit.review_decision THEN
                PERFORM audit._resolve_publication_quarantine(v_table, v_old);
            END IF;

            IF p_decision = 'revise'::audit.review_decision THEN
                PERFORM ops.enqueue_publication_outbox(
                    'publication.superseded',
                    'publication-superseded:' || v_table || ':' || v_old::text || ':' || v_new::text,
                    v_table, v_old,
                    jsonb_build_object(
                        'schema', 'publication-outbox.v2', 'grant_id', lower(v_new::text),
                        'old_grant_id', lower(v_old::text), 'decision_id', lower(p_decision_id::text),
                        'subject_type', p_case_type::text, 'subject_id', lower(p_subject_id::text),
                        'revision_no', v_rev, 'payload_sha256', v_sha
                    )
                );
            END IF;
            PERFORM ops.enqueue_publication_outbox(
                'publication.granted',
                'publication-granted:' || v_table || ':' || v_new::text,
                v_table, v_new,
                jsonb_build_object(
                    'schema', 'publication-outbox.v2', 'grant_id', lower(v_new::text),
                    'decision_id', lower(p_decision_id::text), 'subject_type', p_case_type::text,
                    'subject_id', lower(p_subject_id::text), 'revision_no', v_rev,
                    'payload_sha256', v_sha
                )
            );
        END
        $_apply_publication_grant$;

        CREATE OR REPLACE FUNCTION audit.record_review_decision(
            p_case_id uuid,
            p_decision audit.review_decision,
            p_reason text,
            p_structured_changes jsonb
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = audit, core, ops, pg_catalog
        AS $record_review_decision$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_status audit.review_status;
            v_closed timestamptz;
            v_assigned uuid;
            v_type audit.review_case_type;
            v_subject uuid;
            v_created_by uuid;
            v_seq integer;
            v_decision uuid;
            v_prev uuid;
            v_new_status audit.review_status;
            v_change_key text;
            v_bind uuid;
            v_spans uuid[];
        BEGIN
            IF p_decision = 'withdraw'::audit.review_decision THEN
                v_actor := audit.require_active_role('senior_reviewer'::audit.application_role);
            ELSE
                v_actor := audit.require_active_role('reviewer'::audit.application_role);
            END IF;
            v_request := audit._review_request_id();
            IF p_reason IS NULL OR char_length(btrim(p_reason)) < 10 THEN
                RAISE EXCEPTION 'review_reason_too_short' USING ERRCODE = '22023';
            END IF;
            IF p_case_id IS NULL OR p_decision IS NULL THEN
                RAISE EXCEPTION 'review_case_missing' USING ERRCODE = '23503';
            END IF;
            v_payload := jsonb_build_object(
                'case_id', lower(p_case_id::text), 'decision', p_decision::text,
                'op', 'review.decision', 'reason', p_reason,
                'structured_changes', coalesce(p_structured_changes, 'null'::jsonb)
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.decision:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN RETURN v_existing; END IF;
            IF p_structured_changes IS NULL OR jsonb_typeof(p_structured_changes) IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'review_structured_changes_unsupported' USING ERRCODE = '22023';
            END IF;

            SELECT status, closed_at, assigned_to, case_type,
                   coalesce(document_version_id, claim_id, entity_id, relation_id)
              INTO v_status, v_closed, v_assigned, v_type, v_subject
              FROM audit.review_cases WHERE id = p_case_id FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'review_case_missing' USING ERRCODE = '23503'; END IF;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN RETURN v_existing; END IF;
            IF v_closed IS NOT NULL THEN RAISE EXCEPTION 'review_case_already_closed' USING ERRCODE = '22023'; END IF;
            IF v_type = 'relation'::audit.review_case_type THEN
                RAISE EXCEPTION 'knowledge_relation_review_not_in_wp9' USING ERRCODE = '22023';
            END IF;
            IF v_type = 'claim'::audit.review_case_type THEN
                SELECT created_by INTO v_created_by FROM core.claims WHERE id = v_subject;
                IF v_created_by IS NOT NULL AND v_created_by = v_actor THEN
                    RAISE EXCEPTION 'review_self_review_denied' USING ERRCODE = '42501';
                END IF;
            END IF;
            IF p_decision <> 'withdraw'::audit.review_decision
               AND v_assigned IS NOT NULL AND v_assigned IS DISTINCT FROM v_actor THEN
                RAISE EXCEPTION 'review_assignee_mismatch' USING ERRCODE = '42501';
            END IF;
            IF p_decision IN ('approve'::audit.review_decision, 'reject'::audit.review_decision,
                              'dispute'::audit.review_decision) THEN
                IF v_status NOT IN ('open'::audit.review_status, 'assigned'::audit.review_status) THEN
                    RAISE EXCEPTION 'review_decision_not_allowed' USING ERRCODE = '22023';
                END IF;
            ELSIF p_decision IN ('revise'::audit.review_decision, 'withdraw'::audit.review_decision) THEN
                IF v_status IS DISTINCT FROM 'approved'::audit.review_status THEN
                    RAISE EXCEPTION 'review_decision_not_allowed' USING ERRCODE = '22023';
                END IF;
            ELSE
                RAISE EXCEPTION 'review_decision_not_allowed' USING ERRCODE = '22023';
            END IF;

            IF v_type = 'document'::audit.review_case_type
               AND p_decision IN ('approve'::audit.review_decision, 'revise'::audit.review_decision)
               AND ((SELECT count(*) FROM jsonb_object_keys(p_structured_changes)) <> 1
                    OR NOT p_structured_changes ? 'publication') THEN
                RAISE EXCEPTION 'publication_manifest_invalid' USING ERRCODE = '22023';
            ELSIF v_type = 'document'::audit.review_case_type
               AND p_decision IN ('reject'::audit.review_decision, 'dispute'::audit.review_decision,
                                  'withdraw'::audit.review_decision)
               AND p_structured_changes <> '{}'::jsonb THEN
                RAISE EXCEPTION 'review_structured_changes_unsupported' USING ERRCODE = '22023';
            ELSIF v_type = 'entity'::audit.review_case_type AND p_structured_changes <> '{}'::jsonb THEN
                RAISE EXCEPTION 'review_structured_changes_unsupported' USING ERRCODE = '22023';
            END IF;
            FOR v_change_key IN SELECT jsonb_object_keys(p_structured_changes) LOOP
                IF v_type IS DISTINCT FROM 'claim'::audit.review_case_type THEN
                    IF v_type IS DISTINCT FROM 'document'::audit.review_case_type OR v_change_key <> 'publication' THEN
                        RAISE EXCEPTION 'review_structured_changes_unsupported' USING ERRCODE = '22023';
                    END IF;
                ELSIF v_change_key = 'bind_subject_entity_id' THEN
                    IF p_decision NOT IN ('approve'::audit.review_decision, 'revise'::audit.review_decision) THEN
                        RAISE EXCEPTION 'review_structured_changes_unsupported' USING ERRCODE = '22023';
                    END IF;
                ELSIF v_change_key = 'replace_supporting_span_ids' THEN
                    IF p_decision IS DISTINCT FROM 'revise'::audit.review_decision
                       OR jsonb_typeof(p_structured_changes -> v_change_key) IS DISTINCT FROM 'array'
                       OR jsonb_array_length(p_structured_changes -> v_change_key) NOT BETWEEN 1 AND 20 THEN
                        RAISE EXCEPTION 'review_structured_changes_unsupported' USING ERRCODE = '22023';
                    END IF;
                ELSIF v_change_key = 'retire_supporting_evidence' THEN
                    IF p_decision NOT IN ('reject'::audit.review_decision, 'withdraw'::audit.review_decision)
                       OR (p_structured_changes -> v_change_key) IS DISTINCT FROM 'true'::jsonb THEN
                        RAISE EXCEPTION 'review_structured_changes_unsupported' USING ERRCODE = '22023';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'review_structured_changes_unsupported' USING ERRCODE = '22023';
                END IF;
            END LOOP;

            SELECT coalesce(max(sequence_no), 0) + 1 INTO v_seq
              FROM audit.review_decisions WHERE review_case_id = p_case_id;
            IF p_decision = 'revise'::audit.review_decision THEN
                SELECT id INTO v_prev FROM audit.review_decisions
                 WHERE review_case_id = p_case_id
                   AND decision IN ('approve'::audit.review_decision, 'revise'::audit.review_decision)
                 ORDER BY sequence_no DESC LIMIT 1;
            END IF;
            v_decision := gen_random_uuid();
            INSERT INTO audit.review_decisions
                (id, review_case_id, sequence_no, decision, reason, structured_changes,
                 decided_by, supersedes_decision_id, decided_at)
            VALUES (v_decision, p_case_id, v_seq, p_decision, p_reason, p_structured_changes,
                    v_actor, v_prev, clock_timestamp());

            -- Structured claim changes must be applied before the immutable
            -- claim manifest is captured.
            IF p_structured_changes ? 'bind_subject_entity_id' THEN
                v_bind := (p_structured_changes ->> 'bind_subject_entity_id')::uuid;
                PERFORM audit._apply_claim_subject_bind(p_case_id, v_decision, v_bind);
            END IF;
            IF p_structured_changes ? 'replace_supporting_span_ids' THEN
                SELECT array_agg(span_id::uuid) INTO v_spans
                  FROM jsonb_array_elements_text(p_structured_changes -> 'replace_supporting_span_ids') AS span_id;
                PERFORM audit._replace_claim_evidence(p_case_id, v_decision, v_spans);
            END IF;
            IF p_structured_changes ? 'retire_supporting_evidence' THEN
                PERFORM audit._retire_manual_claim_supports(p_case_id, v_decision);
            END IF;
            PERFORM audit._apply_publication_grant(v_type, v_subject, p_case_id, v_decision, p_decision);

            v_new_status := CASE p_decision
                WHEN 'approve'::audit.review_decision THEN 'approved'::audit.review_status
                WHEN 'revise'::audit.review_decision THEN 'approved'::audit.review_status
                WHEN 'reject'::audit.review_decision THEN 'rejected'::audit.review_status
                WHEN 'dispute'::audit.review_decision THEN 'disputed'::audit.review_status
                WHEN 'withdraw'::audit.review_decision THEN 'withdrawn'::audit.review_status
            END;
            UPDATE audit.review_cases SET status = v_new_status WHERE id = p_case_id;
            BEGIN
                PERFORM audit.append_audit_event(
                    v_key, 'review.decision', 'review_decision', v_decision, v_request,
                    jsonb_build_object('payload_sha256', v_sha)
                );
            EXCEPTION WHEN unique_violation THEN
                RAISE EXCEPTION 'review_idempotency_payload_conflict' USING ERRCODE = '23505';
            END;
            RETURN v_decision;
        END
        $record_review_decision$;

        -- Legacy grants/events are retained for auditability but made
        -- permanently ineligible for the future v2 Publisher.
        INSERT INTO audit.publication_quarantine (id, grant_table, grant_id, reason_code)
        SELECT gen_random_uuid(), 'document_publication_grants', grant_row.id,
               'publication_manifest_required'
          FROM audit.document_publication_grants AS grant_row
         WHERE grant_row.grant_status = 'active'
           AND NOT EXISTS (SELECT 1 FROM audit.document_publication_manifests AS manifest
                            WHERE manifest.grant_id = grant_row.id);
        INSERT INTO audit.publication_quarantine (id, grant_table, grant_id, reason_code)
        SELECT gen_random_uuid(), 'claim_publication_grants', grant_row.id,
               'publication_manifest_required'
          FROM audit.claim_publication_grants AS grant_row
         WHERE grant_row.grant_status = 'active'
           AND NOT EXISTS (SELECT 1 FROM audit.claim_publication_manifests AS manifest
                            WHERE manifest.grant_id = grant_row.id);
        INSERT INTO audit.publication_quarantine (id, grant_table, grant_id, reason_code)
        SELECT gen_random_uuid(), 'entity_publication_grants', grant_row.id,
               'publication_manifest_required'
          FROM audit.entity_publication_grants AS grant_row
         WHERE grant_row.grant_status = 'active'
           AND NOT EXISTS (SELECT 1 FROM audit.entity_publication_manifests AS manifest
                            WHERE manifest.grant_id = grant_row.id);
        INSERT INTO audit.publication_quarantine (id, event_id, reason_code)
        SELECT gen_random_uuid(), event.id, 'publication_manifest_required'
          FROM ops.outbox_events AS event
         WHERE event.published_at IS NULL
           AND event.event_type LIKE 'publication.%'
           AND (event.payload ->> 'schema') = 'publication-outbox.v1';
        UPDATE ops.outbox_events
           SET terminal_at = clock_timestamp(), terminal_error_code = 'publication_manifest_required'
         WHERE published_at IS NULL
           AND event_type LIKE 'publication.%'
           AND (payload ->> 'schema') = 'publication-outbox.v1';

        REVOKE ALL ON FUNCTION audit.reject_publication_contract_mutation() FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit.require_claim_publication_manifest_evidence() FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit.publication_source_url_valid(text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._publication_manifest_sha(jsonb) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._document_publication_payload(uuid,uuid,uuid,integer,jsonb) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._claim_publication_payload(uuid,uuid,uuid,integer) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._entity_publication_payload(uuid,uuid,uuid,integer) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._resolve_publication_quarantine(text, uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) FROM PUBLIC;

        GRANT SELECT ON
            audit.document_publication_manifests,
            audit.claim_publication_manifests,
            audit.claim_publication_manifest_evidence,
            audit.entity_publication_manifests,
            audit.document_public_identities,
            audit.claim_public_identities,
            audit.evidence_public_identities,
            audit.entity_public_identities,
            audit.publication_quarantine
            TO uap_api, uap_publisher;

        -- uap_api can read internal state for the later Admin API, but all
        -- direct writes and all private/core/ops function paths are closed.
        REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA core, ops, audit FROM uap_api;
        REVOKE INSERT ON audit.audit_events FROM uap_api;
        REVOKE ALL ON ALL FUNCTIONS IN SCHEMA core, ops FROM uap_api;
        REVOKE ALL ON ALL FUNCTIONS IN SCHEMA audit FROM uap_api;
        GRANT EXECUTE ON FUNCTION audit.require_active_role(audit.application_role) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.open_review_case(audit.review_case_type, uuid, smallint, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.assign_review_case(uuid, uuid) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.close_review_case(uuid, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.record_review_decision(uuid, audit.review_decision, text, jsonb) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.select_analysis_result(uuid, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.accept_entity_candidate(uuid, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.bind_entity_candidate(uuid, uuid, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.apply_entity_merge(uuid, uuid, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.apply_entity_merge_reverse(uuid, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.create_manual_claim(
            uuid, text, core.claim_type, core.assertion_status, text, uuid[]
        ) TO uap_api;

        REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM uap_publisher;
        REVOKE ALL ON ALL FUNCTIONS IN SCHEMA core, audit FROM uap_publisher;
        REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ops FROM uap_publisher;
        REVOKE ALL ON FUNCTION ops.ack_outbox(uuid, uuid) FROM uap_publisher;
        REVOKE ALL ON FUNCTION ops.release_outbox(uuid, uuid, text, text) FROM uap_publisher;
        REVOKE ALL ON FUNCTION ops.publish_outbox_failure(uuid, uuid, text, text, integer) FROM uap_publisher;
        REVOKE ALL ON FUNCTION ops.claim_outbox(text, integer, integer) FROM uap_publisher;
        REVOKE ALL ON FUNCTION audit.apply_entity_merge(uuid, uuid, text) FROM uap_publisher;
        REVOKE ALL ON FUNCTION audit.apply_entity_merge_reverse(uuid, text) FROM uap_publisher;
        REVOKE ALL ON FUNCTION audit.record_review_decision(uuid, audit.review_decision, text, jsonb)
            FROM uap_publisher;
        REVOKE ALL ON FUNCTION core.merge_entities(uuid, uuid, uuid, text) FROM uap_api, uap_publisher;
        REVOKE ALL ON FUNCTION core.reverse_entity_merge(uuid, uuid, text) FROM uap_api, uap_publisher;

        ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner IN SCHEMA core, ops, audit
            REVOKE INSERT, UPDATE, DELETE ON TABLES FROM uap_api;
        ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner IN SCHEMA public
            REVOKE INSERT, UPDATE, DELETE ON TABLES FROM uap_publisher;
        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;
        CREATE OR REPLACE FUNCTION audit._apply_publication_grant(
            p_case_type audit.review_case_type,
            p_subject_id uuid,
            p_case_id uuid,
            p_decision_id uuid,
            p_decision audit.review_decision
        ) RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, ops, pg_catalog
        AS $_apply_publication_grant$
        DECLARE
            v_table text;
            v_old uuid;
            v_old_rev integer;
            v_new uuid;
            v_rev integer;
            v_sha text;
            v_payload jsonb;
        BEGIN
            IF p_case_type = 'document'::audit.review_case_type THEN
                v_table := 'document_publication_grants';
                SELECT grant_row.id INTO v_old
                  FROM audit.document_publication_grants AS grant_row
                 WHERE grant_row.document_version_id = p_subject_id
                   AND grant_row.grant_status = 'active'::audit.grant_status;
                SELECT coalesce(max(grant_row.revision_no), 0) INTO v_old_rev
                  FROM audit.document_publication_grants AS grant_row
                 WHERE grant_row.document_version_id = p_subject_id;
            ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                v_table := 'claim_publication_grants';
                SELECT grant_row.id INTO v_old
                  FROM audit.claim_publication_grants AS grant_row
                 WHERE grant_row.claim_id = p_subject_id
                   AND grant_row.grant_status = 'active'::audit.grant_status;
                SELECT coalesce(max(grant_row.revision_no), 0) INTO v_old_rev
                  FROM audit.claim_publication_grants AS grant_row
                 WHERE grant_row.claim_id = p_subject_id;
            ELSIF p_case_type = 'entity'::audit.review_case_type THEN
                v_table := 'entity_publication_grants';
                SELECT grant_row.id INTO v_old
                  FROM audit.entity_publication_grants AS grant_row
                 WHERE grant_row.entity_id = p_subject_id
                   AND grant_row.grant_status = 'active'::audit.grant_status;
                SELECT coalesce(max(grant_row.revision_no), 0) INTO v_old_rev
                  FROM audit.entity_publication_grants AS grant_row
                 WHERE grant_row.entity_id = p_subject_id;
            ELSE
                RAISE EXCEPTION 'knowledge_relation_review_not_in_wp9'
                    USING ERRCODE = '22023';
            END IF;

            IF p_decision IN (
                'reject'::audit.review_decision,
                'dispute'::audit.review_decision
            ) THEN
                RETURN;
            END IF;

            IF p_decision = 'withdraw'::audit.review_decision THEN
                IF v_old IS NULL THEN
                    RAISE EXCEPTION 'review_grant_not_active' USING ERRCODE = '22023';
                END IF;
                IF p_case_type = 'document'::audit.review_case_type THEN
                    UPDATE audit.document_publication_grants
                       SET grant_status = 'withdrawn'::audit.grant_status,
                           withdrawn_by_decision_id = p_decision_id,
                           withdrawn_at = clock_timestamp()
                     WHERE id = v_old;
                ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                    UPDATE audit.claim_publication_grants
                       SET grant_status = 'withdrawn'::audit.grant_status,
                           withdrawn_by_decision_id = p_decision_id,
                           withdrawn_at = clock_timestamp()
                     WHERE id = v_old;
                ELSE
                    UPDATE audit.entity_publication_grants
                       SET grant_status = 'withdrawn'::audit.grant_status,
                           withdrawn_by_decision_id = p_decision_id,
                           withdrawn_at = clock_timestamp()
                     WHERE id = v_old;
                END IF;
                v_payload := jsonb_build_object(
                    'decision_id', lower(p_decision_id::text),
                    'grant_id', lower(v_old::text),
                    'payload_sha256', '',
                    'revision_no', v_old_rev,
                    'schema', 'publication-outbox.v1',
                    'subject_id', lower(p_subject_id::text),
                    'subject_type', p_case_type::text
                );
                IF p_case_type = 'document'::audit.review_case_type THEN
                    SELECT grant_row.publication_payload_sha256, grant_row.revision_no
                      INTO v_sha, v_rev
                      FROM audit.document_publication_grants AS grant_row
                     WHERE grant_row.id = v_old;
                ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                    SELECT grant_row.publication_payload_sha256, grant_row.revision_no
                      INTO v_sha, v_rev
                      FROM audit.claim_publication_grants AS grant_row
                     WHERE grant_row.id = v_old;
                ELSE
                    SELECT grant_row.publication_payload_sha256, grant_row.revision_no
                      INTO v_sha, v_rev
                      FROM audit.entity_publication_grants AS grant_row
                     WHERE grant_row.id = v_old;
                END IF;
                v_payload := jsonb_set(v_payload, '{payload_sha256}', to_jsonb(v_sha));
                v_payload := jsonb_set(v_payload, '{revision_no}', to_jsonb(v_rev));
                PERFORM ops.enqueue_publication_outbox(
                    'publication.withdrawn',
                    'publication-withdrawn:' || v_table || ':' || v_old::text
                        || ':' || p_decision_id::text,
                    v_table,
                    v_old,
                    v_payload
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

            v_new := gen_random_uuid();
            v_rev := v_old_rev + 1;
            v_sha := audit._publication_grant_sha(
                v_table, v_new, p_case_type::text, p_subject_id, v_rev
            );

            IF p_decision = 'revise'::audit.review_decision THEN
                IF p_case_type = 'document'::audit.review_case_type THEN
                    UPDATE audit.document_publication_grants
                       SET grant_status = 'superseded'::audit.grant_status
                     WHERE id = v_old;
                ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                    UPDATE audit.claim_publication_grants
                       SET grant_status = 'superseded'::audit.grant_status
                     WHERE id = v_old;
                ELSE
                    UPDATE audit.entity_publication_grants
                       SET grant_status = 'superseded'::audit.grant_status
                     WHERE id = v_old;
                END IF;
            END IF;

            IF p_case_type = 'document'::audit.review_case_type THEN
                INSERT INTO audit.document_publication_grants (
                    id, review_case_id, document_version_id, decision_id,
                    revision_no, grant_status, granted_at, publication_payload_sha256
                ) VALUES (
                    v_new, p_case_id, p_subject_id, p_decision_id,
                    v_rev, 'active'::audit.grant_status, clock_timestamp(), v_sha
                );
            ELSIF p_case_type = 'claim'::audit.review_case_type THEN
                INSERT INTO audit.claim_publication_grants (
                    id, review_case_id, claim_id, decision_id,
                    revision_no, grant_status, granted_at, publication_payload_sha256
                ) VALUES (
                    v_new, p_case_id, p_subject_id, p_decision_id,
                    v_rev, 'active'::audit.grant_status, clock_timestamp(), v_sha
                );
            ELSE
                INSERT INTO audit.entity_publication_grants (
                    id, review_case_id, entity_id, decision_id,
                    revision_no, grant_status, granted_at, publication_payload_sha256
                ) VALUES (
                    v_new, p_case_id, p_subject_id, p_decision_id,
                    v_rev, 'active'::audit.grant_status, clock_timestamp(), v_sha
                );
            END IF;

            IF p_decision = 'revise'::audit.review_decision THEN
                PERFORM ops.enqueue_publication_outbox(
                    'publication.superseded',
                    'publication-superseded:' || v_table || ':' || v_old::text
                        || ':' || v_new::text,
                    v_table,
                    v_old,
                    jsonb_build_object(
                        'decision_id', lower(p_decision_id::text),
                        'grant_id', lower(v_new::text),
                        'old_grant_id', lower(v_old::text),
                        'payload_sha256', v_sha,
                        'revision_no', v_rev,
                        'schema', 'publication-outbox.v1',
                        'subject_id', lower(p_subject_id::text),
                        'subject_type', p_case_type::text
                    )
                );
            END IF;

            PERFORM ops.enqueue_publication_outbox(
                'publication.granted',
                'publication-granted:' || v_table || ':' || v_new::text,
                v_table,
                v_new,
                jsonb_build_object(
                    'decision_id', lower(p_decision_id::text),
                    'grant_id', lower(v_new::text),
                    'payload_sha256', v_sha,
                    'revision_no', v_rev,
                    'schema', 'publication-outbox.v1',
                    'subject_id', lower(p_subject_id::text),
                    'subject_type', p_case_type::text
                )
            );
        END
        $_apply_publication_grant$;

        CREATE OR REPLACE FUNCTION audit.record_review_decision(
            p_case_id uuid,
            p_decision audit.review_decision,
            p_reason text,
            p_structured_changes jsonb
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, ops, pg_catalog
        AS $record_review_decision$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_status audit.review_status;
            v_closed timestamptz;
            v_assigned uuid;
            v_type audit.review_case_type;
            v_subject uuid;
            v_created_by uuid;
            v_seq integer;
            v_decision uuid;
            v_prev uuid;
            v_new_status audit.review_status;
            v_change_key text;
            v_bind uuid;
            v_spans uuid[];
        BEGIN
            IF p_decision = 'withdraw'::audit.review_decision THEN
                v_actor := audit.require_active_role(
                    'senior_reviewer'::audit.application_role
                );
            ELSE
                v_actor := audit.require_active_role(
                    'reviewer'::audit.application_role
                );
            END IF;
            v_request := audit._review_request_id();
            IF p_reason IS NULL OR char_length(btrim(p_reason)) < 10 THEN
                RAISE EXCEPTION 'review_reason_too_short' USING ERRCODE = '22023';
            END IF;
            IF p_case_id IS NULL OR p_decision IS NULL THEN
                RAISE EXCEPTION 'review_case_missing' USING ERRCODE = '23503';
            END IF;

            v_payload := jsonb_build_object(
                'case_id', lower(p_case_id::text),
                'decision', p_decision::text,
                'op', 'review.decision',
                'reason', p_reason,
                'structured_changes', coalesce(p_structured_changes, 'null'::jsonb)
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'review.decision:' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            IF p_structured_changes IS NULL
               OR jsonb_typeof(p_structured_changes) IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'review_structured_changes_unsupported'
                    USING ERRCODE = '22023';
            END IF;

            SELECT review_case.status, review_case.closed_at, review_case.assigned_to,
                   review_case.case_type,
                   coalesce(
                       review_case.document_version_id,
                       review_case.claim_id,
                       review_case.entity_id,
                       review_case.relation_id
                   )
              INTO v_status, v_closed, v_assigned, v_type, v_subject
              FROM audit.review_cases AS review_case
             WHERE review_case.id = p_case_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'review_case_missing' USING ERRCODE = '23503';
            END IF;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;
            IF v_closed IS NOT NULL THEN
                RAISE EXCEPTION 'review_case_already_closed' USING ERRCODE = '22023';
            END IF;
            IF v_type = 'relation'::audit.review_case_type THEN
                RAISE EXCEPTION 'knowledge_relation_review_not_in_wp9'
                    USING ERRCODE = '22023';
            END IF;
            IF v_type = 'claim'::audit.review_case_type THEN
                SELECT claim.created_by INTO v_created_by
                  FROM core.claims AS claim
                 WHERE claim.id = v_subject;
                IF v_created_by IS NOT NULL AND v_created_by = v_actor THEN
                    RAISE EXCEPTION 'review_self_review_denied' USING ERRCODE = '42501';
                END IF;
            END IF;
            IF p_decision <> 'withdraw'::audit.review_decision
               AND v_assigned IS NOT NULL
               AND v_assigned IS DISTINCT FROM v_actor THEN
                RAISE EXCEPTION 'review_assignee_mismatch' USING ERRCODE = '42501';
            END IF;

            IF p_decision IN (
                'approve'::audit.review_decision,
                'reject'::audit.review_decision,
                'dispute'::audit.review_decision
            ) THEN
                IF v_status NOT IN (
                    'open'::audit.review_status,
                    'assigned'::audit.review_status
                ) THEN
                    RAISE EXCEPTION 'review_decision_not_allowed'
                        USING ERRCODE = '22023';
                END IF;
            ELSIF p_decision IN (
                'revise'::audit.review_decision,
                'withdraw'::audit.review_decision
            ) THEN
                IF v_status IS DISTINCT FROM 'approved'::audit.review_status THEN
                    RAISE EXCEPTION 'review_decision_not_allowed'
                        USING ERRCODE = '22023';
                END IF;
            ELSE
                RAISE EXCEPTION 'review_decision_not_allowed' USING ERRCODE = '22023';
            END IF;

            FOR v_change_key IN
                SELECT jsonb_object_keys(p_structured_changes)
            LOOP
                IF v_type IS DISTINCT FROM 'claim'::audit.review_case_type THEN
                    RAISE EXCEPTION 'review_structured_changes_unsupported'
                        USING ERRCODE = '22023';
                END IF;
                IF v_change_key = 'bind_subject_entity_id' THEN
                    IF p_decision NOT IN (
                        'approve'::audit.review_decision,
                        'revise'::audit.review_decision
                    ) THEN
                        RAISE EXCEPTION 'review_structured_changes_unsupported'
                            USING ERRCODE = '22023';
                    END IF;
                ELSIF v_change_key = 'replace_supporting_span_ids' THEN
                    IF p_decision IS DISTINCT FROM 'revise'::audit.review_decision
                       OR jsonb_typeof(
                            p_structured_changes -> 'replace_supporting_span_ids'
                       ) IS DISTINCT FROM 'array'
                       OR jsonb_array_length(
                            p_structured_changes -> 'replace_supporting_span_ids'
                       ) < 1 THEN
                        RAISE EXCEPTION 'review_structured_changes_unsupported'
                            USING ERRCODE = '22023';
                    END IF;
                ELSIF v_change_key = 'retire_supporting_evidence' THEN
                    IF p_decision NOT IN (
                        'reject'::audit.review_decision,
                        'withdraw'::audit.review_decision
                    ) OR (p_structured_changes -> 'retire_supporting_evidence')
                       IS DISTINCT FROM 'true'::jsonb THEN
                        RAISE EXCEPTION 'review_structured_changes_unsupported'
                            USING ERRCODE = '22023';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'review_structured_changes_unsupported'
                        USING ERRCODE = '22023';
                END IF;
            END LOOP;

            SELECT coalesce(max(decision.sequence_no), 0) + 1
              INTO v_seq
              FROM audit.review_decisions AS decision
             WHERE decision.review_case_id = p_case_id;

            IF p_decision = 'revise'::audit.review_decision THEN
                SELECT decision.id INTO v_prev
                  FROM audit.review_decisions AS decision
                 WHERE decision.review_case_id = p_case_id
                   AND decision.decision IN (
                        'approve'::audit.review_decision,
                        'revise'::audit.review_decision
                   )
                 ORDER BY decision.sequence_no DESC
                 LIMIT 1;
            END IF;

            v_decision := gen_random_uuid();
            INSERT INTO audit.review_decisions (
                id, review_case_id, sequence_no, decision, reason,
                structured_changes, decided_by, supersedes_decision_id, decided_at
            ) VALUES (
                v_decision, p_case_id, v_seq, p_decision, p_reason,
                p_structured_changes, v_actor, v_prev, clock_timestamp()
            );

            PERFORM audit._apply_publication_grant(
                v_type, v_subject, p_case_id, v_decision, p_decision
            );

            v_new_status := CASE p_decision
                WHEN 'approve'::audit.review_decision THEN 'approved'::audit.review_status
                WHEN 'revise'::audit.review_decision THEN 'approved'::audit.review_status
                WHEN 'reject'::audit.review_decision THEN 'rejected'::audit.review_status
                WHEN 'dispute'::audit.review_decision THEN 'disputed'::audit.review_status
                WHEN 'withdraw'::audit.review_decision THEN 'withdrawn'::audit.review_status
            END;
            UPDATE audit.review_cases
               SET status = v_new_status
             WHERE id = p_case_id;

            IF p_structured_changes ? 'bind_subject_entity_id' THEN
                v_bind := (p_structured_changes ->> 'bind_subject_entity_id')::uuid;
                PERFORM audit._apply_claim_subject_bind(p_case_id, v_decision, v_bind);
            END IF;
            IF p_structured_changes ? 'replace_supporting_span_ids' THEN
                SELECT array_agg(span_id::uuid)
                  INTO v_spans
                  FROM jsonb_array_elements_text(
                      p_structured_changes -> 'replace_supporting_span_ids'
                  ) AS span_id;
                PERFORM audit._replace_claim_evidence(p_case_id, v_decision, v_spans);
            END IF;
            IF p_structured_changes ? 'retire_supporting_evidence' THEN
                PERFORM audit._retire_manual_claim_supports(p_case_id, v_decision);
            END IF;

            BEGIN
                PERFORM audit.append_audit_event(
                    v_key,
                    'review.decision',
                    'review_decision',
                    v_decision,
                    v_request,
                    jsonb_build_object('payload_sha256', v_sha)
                );
            EXCEPTION
                WHEN unique_violation THEN
                    RAISE EXCEPTION 'review_idempotency_payload_conflict'
                        USING ERRCODE = '23505';
            END;
            RETURN v_decision;
        END
        $record_review_decision$;

        DO $wp10_downgrade_guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM audit.document_publication_manifests)
               OR EXISTS (SELECT 1 FROM audit.claim_publication_manifests)
               OR EXISTS (SELECT 1 FROM audit.claim_publication_manifest_evidence)
               OR EXISTS (SELECT 1 FROM audit.entity_publication_manifests)
               OR EXISTS (SELECT 1 FROM audit.document_public_identities)
               OR EXISTS (SELECT 1 FROM audit.claim_public_identities)
               OR EXISTS (SELECT 1 FROM audit.evidence_public_identities)
               OR EXISTS (SELECT 1 FROM audit.entity_public_identities)
               OR EXISTS (SELECT 1 FROM audit.publication_quarantine)
               OR EXISTS (SELECT 1 FROM ops.outbox_events WHERE terminal_at IS NOT NULL)
            THEN
                RAISE EXCEPTION 'publication_contract_state_blocks_downgrade'
                    USING ERRCODE = '22023';
            END IF;
        END
        $wp10_downgrade_guard$;

        -- A non-empty public projection is also a WP10 state marker.  This
        -- guard is intentionally before every DROP/ALTER operation.
        DO $wp10_public_downgrade_guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.documents)
               OR EXISTS (SELECT 1 FROM public.claims)
               OR EXISTS (SELECT 1 FROM public.evidence)
               OR EXISTS (SELECT 1 FROM public.claim_evidence)
               OR EXISTS (SELECT 1 FROM public.entities)
               OR EXISTS (SELECT 1 FROM public.document_entities)
               OR EXISTS (SELECT 1 FROM public.relations)
               OR EXISTS (SELECT 1 FROM public.relation_evidence)
               OR EXISTS (SELECT 1 FROM public.search_documents)
            THEN
                RAISE EXCEPTION 'publication_contract_state_blocks_downgrade'
                    USING ERRCODE = '22023';
            END IF;
        END
        $wp10_public_downgrade_guard$;

        DROP FUNCTION audit._entity_publication_payload(uuid,uuid,uuid,integer);
        DROP FUNCTION audit._claim_publication_payload(uuid,uuid,uuid,integer);
        DROP FUNCTION audit._document_publication_payload(uuid,uuid,uuid,integer,jsonb);
        DROP FUNCTION audit._resolve_publication_quarantine(text, uuid);
        DROP FUNCTION audit._publication_manifest_sha(jsonb);
        DROP FUNCTION audit.publication_source_url_valid(text);
        DROP TRIGGER IF EXISTS document_publication_manifests_append_only
            ON audit.document_publication_manifests;
        DROP TRIGGER IF EXISTS claim_publication_manifests_append_only
            ON audit.claim_publication_manifests;
        DROP TRIGGER IF EXISTS claim_manifest_evidence_append_only
            ON audit.claim_publication_manifest_evidence;
        DROP TRIGGER IF EXISTS entity_publication_manifests_append_only
            ON audit.entity_publication_manifests;
        DROP TRIGGER IF EXISTS document_public_identities_append_only
            ON audit.document_public_identities;
        DROP TRIGGER IF EXISTS claim_public_identities_append_only
            ON audit.claim_public_identities;
        DROP TRIGGER IF EXISTS evidence_public_identities_append_only
            ON audit.evidence_public_identities;
        DROP TRIGGER IF EXISTS entity_public_identities_append_only
            ON audit.entity_public_identities;
        DROP TRIGGER IF EXISTS publication_quarantine_append_only
            ON audit.publication_quarantine;
        DROP TRIGGER IF EXISTS claim_publication_manifest_evidence_required
            ON audit.claim_publication_manifests;
        DROP TRIGGER IF EXISTS claim_publication_manifest_evidence_present
            ON audit.claim_publication_manifest_evidence;
        DROP FUNCTION IF EXISTS audit.require_claim_publication_manifest_evidence();
        DROP FUNCTION audit.reject_publication_contract_mutation();
        ALTER TABLE ops.outbox_events DROP CONSTRAINT ck_outbox_published_terminal;
        ALTER TABLE ops.outbox_events DROP COLUMN terminal_error_code, DROP COLUMN terminal_at;
        DROP TABLE audit.publication_quarantine;
        DROP TABLE audit.entity_publication_manifests;
        DROP TABLE audit.claim_publication_manifest_evidence;
        DROP TABLE audit.claim_publication_manifests;
        DROP TABLE audit.document_publication_manifests;
        DROP TABLE audit.entity_public_identities;
        DROP TABLE audit.evidence_public_identities;
        DROP TABLE audit.claim_public_identities;
        DROP TABLE audit.document_public_identities;
        ALTER TABLE public.documents
            ALTER COLUMN category TYPE text USING category::text,
            ALTER COLUMN fact_status TYPE text USING fact_status::text;
        DROP TYPE public.fact_status;
        DROP TYPE public.document_category;

        -- Restore the exact cumulative 0019 role boundary.  Earlier migrations
        -- had already removed uap_api writes from orchestration, model,
        -- knowledge, and selection tables.
        GRANT INSERT, UPDATE ON
            core.stored_objects, core.documents, core.document_versions, core.extractions
            TO uap_api;
        GRANT INSERT ON audit.audit_events TO uap_api;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO uap_publisher;
        ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner IN SCHEMA public
            GRANT INSERT, UPDATE, DELETE ON TABLES TO uap_publisher;
        GRANT EXECUTE ON FUNCTION core.canonical_entity_id(uuid)
            TO uap_api, uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.claim_job(text, text, text[], integer)
            TO uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.classify_failure(smallint, text)
            TO uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.finish_job(
            uuid, uuid, uuid, ops.attempt_outcome, smallint, text, text, integer
        ) TO uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.claim_outbox(text, integer, integer)
            TO uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.ack_outbox(uuid, uuid) TO uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.release_outbox(uuid, uuid, text, text)
            TO uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.publish_outbox_failure(
            uuid, uuid, text, text, integer
        ) TO uap_publisher;
        GRANT EXECUTE ON FUNCTION audit.record_review_decision(
            uuid, audit.review_decision, text, jsonb
        ) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.require_active_role(audit.application_role) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.open_review_case(
            audit.review_case_type, uuid, smallint, text
        ) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.assign_review_case(uuid, uuid) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.close_review_case(uuid, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.select_analysis_result(uuid, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.accept_entity_candidate(uuid, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.bind_entity_candidate(uuid, uuid, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.apply_entity_merge(uuid, uuid, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.apply_entity_merge_reverse(uuid, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.create_manual_claim(
            uuid, text, core.claim_type, core.assertion_status, text, uuid[]
        ) TO uap_api;
        RESET ROLE;
        """
    )
