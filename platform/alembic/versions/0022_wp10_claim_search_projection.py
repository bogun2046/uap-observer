"""Add WP10.3 claim, evidence, association, search, and rebuild projection.

Revision ID: 0022_wp10_claim_search_projection
Revises: 0021_wp10_publisher_projection
Create Date: 2026-08-30
"""

from __future__ import annotations

from alembic import op

revision = "0022_wp10_claim_search_projection"
down_revision = "0021_wp10_publisher_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        ALTER TABLE alembic_version ALTER COLUMN version_num TYPE varchar(64);
        SET ROLE uap_owner;

        CREATE TABLE audit.publication_rebuild_runs (
            rebuild_id uuid PRIMARY KEY,
            input_digest char(64) NOT NULL CHECK (input_digest ~ '^[0-9a-f]{64}$'),
            result_digest char(64) CHECK (
                result_digest IS NULL OR result_digest ~ '^[0-9a-f]{64}$'
            ),
            counts jsonb NOT NULL DEFAULT '{}'::jsonb,
            status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
            error_code text,
            started_at timestamptz NOT NULL,
            finished_at timestamptz,
            CHECK ((status = 'running') = (finished_at IS NULL)),
            CHECK ((status = 'succeeded') = (result_digest IS NOT NULL)),
            CHECK (error_code IS NULL OR error_code = 'publication_rebuild_mismatch')
        );

        CREATE FUNCTION audit.guard_publication_rebuild_run_mutation() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = audit, pg_catalog
        AS $guard_publication_rebuild_run_mutation$
        BEGIN
            IF TG_OP = 'DELETE'
               OR NEW.rebuild_id IS DISTINCT FROM OLD.rebuild_id
               OR NEW.input_digest IS DISTINCT FROM OLD.input_digest
               OR NEW.started_at IS DISTINCT FROM OLD.started_at
               OR OLD.status <> 'running'
            THEN
                RAISE EXCEPTION 'publication rebuild history is immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END
        $guard_publication_rebuild_run_mutation$;

        CREATE TRIGGER publication_rebuild_runs_immutable
            BEFORE UPDATE OR DELETE ON audit.publication_rebuild_runs
            FOR EACH ROW EXECUTE FUNCTION audit.guard_publication_rebuild_run_mutation();

        CREATE UNIQUE INDEX uq_public_claim_document_ordinal
            ON public.claims(document_id, ordinal);
        CREATE INDEX ix_public_claims_document_ordinal
            ON public.claims(document_id, ordinal, id);
        CREATE INDEX ix_public_claim_evidence_claim
            ON public.claim_evidence(claim_id, evidence_id);
        CREATE INDEX ix_public_document_entities_entity
            ON public.document_entities(entity_id, document_id);

        CREATE FUNCTION ops._wp10_3_public_row_id(p_namespace text, p_parts jsonb)
        RETURNS uuid
        LANGUAGE sql IMMUTABLE STRICT
        SET search_path = audit, pg_catalog
        AS $_wp10_3_public_row_id$
            WITH digest AS (
                SELECT audit._payload_sha256(
                    jsonb_build_object('namespace', p_namespace, 'parts', p_parts)
                ) AS value
            )
            SELECT (
                substr(value, 1, 8) || '-' || substr(value, 9, 4) || '-' ||
                substr(value, 13, 4) || '-' || substr(value, 17, 4) || '-' ||
                substr(value, 21, 12)
            )::uuid
            FROM digest
        $_wp10_3_public_row_id$;

        CREATE FUNCTION ops._wp10_3_document_manifest_payload(p_grant_id uuid)
        RETURNS jsonb
        LANGUAGE sql STABLE STRICT
        SET search_path = audit, pg_catalog
        AS $_wp10_3_document_manifest_payload$
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
        $_wp10_3_document_manifest_payload$;

        CREATE FUNCTION ops._wp10_3_entity_manifest_payload(p_grant_id uuid)
        RETURNS jsonb
        LANGUAGE sql STABLE STRICT
        SET search_path = audit, pg_catalog
        AS $_wp10_3_entity_manifest_payload$
            SELECT jsonb_build_object(
                'schema', 'publication-manifest.v2',
                'subject_type', 'entity',
                'grant_id', lower(manifest.grant_id::text),
                'decision_id', lower(manifest.decision_id::text),
                'subject_id', lower(manifest.entity_id::text),
                'revision_no', grant_row.revision_no,
                'entity_type', manifest.entity_type::text,
                'canonical_name', manifest.canonical_name,
                'description', to_jsonb(manifest.description),
                'country_code', to_jsonb(manifest.country_code)
            )
            FROM audit.entity_publication_manifests AS manifest
            JOIN audit.entity_publication_grants AS grant_row
              ON grant_row.id = manifest.grant_id
            WHERE manifest.grant_id = p_grant_id
        $_wp10_3_entity_manifest_payload$;

        CREATE FUNCTION ops._wp10_3_claim_manifest_payload(p_grant_id uuid)
        RETURNS jsonb
        LANGUAGE sql STABLE STRICT
        SET search_path = audit, pg_catalog
        AS $_wp10_3_claim_manifest_payload$
            SELECT jsonb_build_object(
                'schema', 'publication-manifest.v2',
                'subject_type', 'claim',
                'grant_id', lower(manifest.grant_id::text),
                'decision_id', lower(manifest.decision_id::text),
                'subject_id', lower(manifest.claim_id::text),
                'document_id', lower(manifest.document_id::text),
                'document_version_id', lower(manifest.document_version_id::text),
                'revision_no', grant_row.revision_no,
                'claim_text', manifest.claim_text,
                'claim_type', manifest.claim_type::text,
                'assertion_status', manifest.assertion_status::text,
                'attribution', to_jsonb(manifest.attribution),
                'subject_entity_id', CASE
                    WHEN manifest.subject_entity_id IS NULL THEN 'null'::jsonb
                    ELSE to_jsonb(lower(manifest.subject_entity_id::text))
                END,
                'evidence', coalesce((
                    SELECT jsonb_agg(jsonb_build_object(
                        'evidence_span_id', lower(evidence.evidence_span_id::text),
                        'evidence_ordinal', evidence.evidence_ordinal,
                        'excerpt', evidence.excerpt,
                        'locator_type', evidence.locator_type::text,
                        'char_start', to_jsonb(evidence.char_start),
                        'char_end', to_jsonb(evidence.char_end),
                        'page_start', to_jsonb(evidence.page_start),
                        'page_end', to_jsonb(evidence.page_end),
                        'time_start_ms', to_jsonb(evidence.time_start_ms),
                        'time_end_ms', to_jsonb(evidence.time_end_ms),
                        'public_locator', evidence.public_locator,
                        'locator_sha256', evidence.locator_sha256,
                        'source_url', evidence.source_url
                    ) ORDER BY evidence.evidence_ordinal)
                    FROM audit.claim_publication_manifest_evidence AS evidence
                    WHERE evidence.grant_id = manifest.grant_id
                ), '[]'::jsonb)
            )
            FROM audit.claim_publication_manifests AS manifest
            JOIN audit.claim_publication_grants AS grant_row
              ON grant_row.id = manifest.grant_id
            WHERE manifest.grant_id = p_grant_id
        $_wp10_3_claim_manifest_payload$;

        CREATE FUNCTION ops._wp10_3_refresh_search(p_document_id uuid) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = public, audit, pg_catalog
        AS $_wp10_3_refresh_search$
        DECLARE
            v_claim_text text;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM public.documents WHERE id = p_document_id) THEN
                DELETE FROM public.search_documents WHERE document_id = p_document_id;
                RETURN;
            END IF;
            SELECT string_agg(claim.claim_text, E'\n' ORDER BY claim.ordinal, claim.id)
              INTO v_claim_text
              FROM public.claims AS claim
             WHERE claim.document_id = p_document_id;
            INSERT INTO public.search_documents (
                document_id, search_vector, display_text, facets, indexed_at
            )
            SELECT document.id,
                   to_tsvector(
                       'simple'::regconfig,
                       concat_ws(E'\n', document.title, nullif(document.summary, ''), v_claim_text)
                   ),
                   concat_ws(E'\n', document.title, nullif(document.summary, ''), v_claim_text),
                   jsonb_build_object(
                       'category', document.category::text,
                       'fact_status', document.fact_status::text,
                       'source_name', document.source_name
                   ),
                   clock_timestamp()
              FROM public.documents AS document
             WHERE document.id = p_document_id
            ON CONFLICT (document_id) DO UPDATE SET
                search_vector = EXCLUDED.search_vector,
                display_text = EXCLUDED.display_text,
                facets = EXCLUDED.facets,
                indexed_at = EXCLUDED.indexed_at;
        END
        $_wp10_3_refresh_search$;

        CREATE FUNCTION ops._wp10_3_reconcile_document_entities(p_document_id uuid)
        RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = public, audit, pg_catalog
        AS $_wp10_3_reconcile_document_entities$
        BEGIN
            DELETE FROM public.document_entities WHERE document_id = p_document_id;
            INSERT INTO public.document_entities (
                id, document_id, entity_id, basis_evidence_id, basis_claim_id,
                basis_relation_id
            )
            SELECT DISTINCT ON (claim.document_id, entity.id)
                   ops._wp10_3_public_row_id(
                       'document-entity',
                       jsonb_build_array(lower(claim.document_id::text), lower(entity.id::text))
                   ),
                   claim.document_id,
                   entity.id,
                   basis.evidence_id,
                   claim.id,
                   NULL
              FROM public.claims AS claim
              JOIN audit.claim_public_identities AS claim_identity
                ON claim_identity.public_id = claim.id
              JOIN audit.claim_publication_manifests AS manifest
                ON manifest.grant_id = claim.claim_grant_id
               AND manifest.claim_id = claim_identity.claim_id
              JOIN audit.entity_public_identities AS entity_identity
                ON entity_identity.entity_id = manifest.subject_entity_id
              JOIN public.entities AS entity ON entity.id = entity_identity.public_id
              JOIN LATERAL (
                    SELECT evidence.id AS evidence_id
                      FROM audit.claim_publication_manifest_evidence AS manifest_evidence
                      JOIN audit.evidence_public_identities AS evidence_identity
                        ON evidence_identity.evidence_span_id = manifest_evidence.evidence_span_id
                      JOIN public.evidence AS evidence
                        ON evidence.id = evidence_identity.public_id
                       AND evidence.document_id = claim.document_id
                      JOIN public.claim_evidence AS claim_evidence
                        ON claim_evidence.claim_id = claim.id
                       AND claim_evidence.evidence_id = evidence.id
                     WHERE manifest_evidence.grant_id = manifest.grant_id
                     ORDER BY manifest_evidence.evidence_ordinal
                     LIMIT 1
              ) AS basis ON true
             WHERE claim.document_id = p_document_id
             ORDER BY claim.document_id, entity.id, claim.ordinal, claim.id,
                      basis.evidence_id
            ON CONFLICT (document_id, entity_id) DO UPDATE SET
                basis_evidence_id = EXCLUDED.basis_evidence_id,
                basis_claim_id = EXCLUDED.basis_claim_id,
                basis_relation_id = NULL;
        END
        $_wp10_3_reconcile_document_entities$;

        CREATE FUNCTION ops._wp10_3_reconcile_claim(p_grant_id uuid)
        RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, audit, core, public, pg_catalog
        AS $_wp10_3_reconcile_claim$
        DECLARE
            v_manifest audit.claim_publication_manifests%ROWTYPE;
            v_grant audit.claim_publication_grants%ROWTYPE;
            v_document_public_id uuid;
            v_claim_public_id uuid;
            v_display_ordinal integer;
            v_evidence_public_id uuid;
            evidence_row audit.claim_publication_manifest_evidence%ROWTYPE;
        BEGIN
            SET CONSTRAINTS public_claim_requires_evidence,
                            public_claim_evidence_required DEFERRED;
            SELECT * INTO v_grant FROM audit.claim_publication_grants WHERE id = p_grant_id;
            SELECT * INTO v_manifest FROM audit.claim_publication_manifests
             WHERE grant_id = p_grant_id;
            IF v_grant.id IS NULL OR v_manifest.grant_id IS NULL
               OR v_grant.grant_status <> 'active'::audit.grant_status
            THEN
                RETURN false;
            END IF;
            IF btrim(v_grant.publication_payload_sha256::text) IS DISTINCT FROM
               btrim(v_manifest.manifest_sha256::text)
               OR btrim(v_manifest.manifest_sha256::text) IS DISTINCT FROM
                  audit._publication_manifest_sha(
                      ops._wp10_3_claim_manifest_payload(p_grant_id)
                  )
            THEN
                RAISE EXCEPTION 'publication_payload_hash_mismatch'
                    USING ERRCODE = '22023';
            END IF;
            SELECT identity.public_id
              INTO v_document_public_id
              FROM audit.document_public_identities AS identity
              JOIN public.documents AS document ON document.id = identity.public_id
             WHERE identity.document_id = v_manifest.document_id;
            IF v_document_public_id IS NULL THEN
                RETURN false;
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'publication-claim-ordinal:' || v_manifest.document_id::text, 0
            ));
            SELECT identity.public_id, identity.display_ordinal
              INTO v_claim_public_id, v_display_ordinal
              FROM audit.claim_public_identities AS identity
             WHERE identity.claim_id = v_manifest.claim_id;
            IF v_claim_public_id IS NULL THEN
                SELECT coalesce(max(identity.display_ordinal), -1) + 1
                  INTO v_display_ordinal
                  FROM audit.claim_public_identities AS identity
                 WHERE identity.document_id = v_manifest.document_id;
                v_claim_public_id := gen_random_uuid();
                INSERT INTO audit.claim_public_identities (
                    claim_id, document_id, public_id, display_ordinal
                ) VALUES (
                    v_manifest.claim_id, v_manifest.document_id,
                    v_claim_public_id, v_display_ordinal
                );
            END IF;

            DELETE FROM public.document_entities WHERE document_id = v_document_public_id;
            INSERT INTO public.claims (
                id, document_id, claim_grant_id, ordinal, claim_text, claim_type,
                assertion_status, attribution, revision_no
            ) VALUES (
                v_claim_public_id, v_document_public_id, p_grant_id, v_display_ordinal,
                v_manifest.claim_text, v_manifest.claim_type::text,
                v_manifest.assertion_status::text, v_manifest.attribution,
                v_grant.revision_no
            )
            ON CONFLICT (id) DO UPDATE SET
                document_id = EXCLUDED.document_id,
                claim_grant_id = EXCLUDED.claim_grant_id,
                ordinal = EXCLUDED.ordinal,
                claim_text = EXCLUDED.claim_text,
                claim_type = EXCLUDED.claim_type,
                assertion_status = EXCLUDED.assertion_status,
                attribution = EXCLUDED.attribution,
                revision_no = EXCLUDED.revision_no
            WHERE public.claims.revision_no <= EXCLUDED.revision_no;

            DELETE FROM public.claim_evidence WHERE claim_id = v_claim_public_id;
            FOR evidence_row IN
                SELECT * FROM audit.claim_publication_manifest_evidence
                 WHERE grant_id = p_grant_id
                 ORDER BY evidence_ordinal
            LOOP
                SELECT identity.public_id INTO v_evidence_public_id
                  FROM audit.evidence_public_identities AS identity
                 WHERE identity.evidence_span_id = evidence_row.evidence_span_id;
                IF v_evidence_public_id IS NULL THEN
                    v_evidence_public_id := gen_random_uuid();
                    INSERT INTO audit.evidence_public_identities (
                        evidence_span_id, public_id
                    ) VALUES (evidence_row.evidence_span_id, v_evidence_public_id);
                END IF;
                INSERT INTO public.evidence (
                    id, document_id, excerpt, locator_type, page_start, page_end,
                    time_start_ms, time_end_ms, public_locator, locator_sha256, source_url
                ) VALUES (
                    v_evidence_public_id, v_document_public_id, evidence_row.excerpt,
                    evidence_row.locator_type::text, evidence_row.page_start,
                    evidence_row.page_end, evidence_row.time_start_ms,
                    evidence_row.time_end_ms, evidence_row.public_locator,
                    evidence_row.locator_sha256, evidence_row.source_url
                )
                ON CONFLICT (id) DO UPDATE SET
                    document_id = EXCLUDED.document_id,
                    excerpt = EXCLUDED.excerpt,
                    locator_type = EXCLUDED.locator_type,
                    page_start = EXCLUDED.page_start,
                    page_end = EXCLUDED.page_end,
                    time_start_ms = EXCLUDED.time_start_ms,
                    time_end_ms = EXCLUDED.time_end_ms,
                    public_locator = EXCLUDED.public_locator,
                    locator_sha256 = EXCLUDED.locator_sha256,
                    source_url = EXCLUDED.source_url;
                INSERT INTO public.claim_evidence (id, claim_id, evidence_id)
                VALUES (
                    ops._wp10_3_public_row_id(
                        'claim-evidence',
                        jsonb_build_array(
                            lower(v_claim_public_id::text),
                            lower(v_evidence_public_id::text)
                        )
                    ),
                    v_claim_public_id,
                    v_evidence_public_id
                );
            END LOOP;
            IF NOT EXISTS (
                SELECT 1 FROM public.claim_evidence WHERE claim_id = v_claim_public_id
            ) THEN
                RAISE EXCEPTION 'publication_evidence_required' USING ERRCODE = '23514';
            END IF;
            DELETE FROM public.evidence AS evidence
             WHERE evidence.document_id = v_document_public_id
               AND NOT EXISTS (
                    SELECT 1 FROM public.claim_evidence AS link
                     WHERE link.evidence_id = evidence.id
               );
            PERFORM ops._wp10_3_reconcile_document_entities(v_document_public_id);
            PERFORM ops._wp10_3_refresh_search(v_document_public_id);
            RETURN true;
        END
        $_wp10_3_reconcile_claim$;

        CREATE FUNCTION ops._wp10_3_reconcile_document_claims(p_document_internal_id uuid)
        RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, audit, public, pg_catalog
        AS $_wp10_3_reconcile_document_claims$
        DECLARE
            v_document_public_id uuid;
            v_active_claim record;
        BEGIN
            SELECT identity.public_id INTO v_document_public_id
              FROM audit.document_public_identities AS identity
              JOIN public.documents AS document ON document.id = identity.public_id
             WHERE identity.document_id = p_document_internal_id;
            IF v_document_public_id IS NULL THEN
                RETURN;
            END IF;
            DELETE FROM public.document_entities WHERE document_id = v_document_public_id;
            DELETE FROM public.claim_evidence AS link
             USING public.claims AS claim
             WHERE link.claim_id = claim.id
               AND claim.document_id = v_document_public_id
               AND NOT EXISTS (
                    SELECT 1
                      FROM audit.claim_public_identities AS identity
                      JOIN audit.claim_publication_grants AS grant_row
                        ON grant_row.claim_id = identity.claim_id
                       AND grant_row.grant_status = 'active'::audit.grant_status
                      JOIN audit.claim_publication_manifests AS manifest
                        ON manifest.grant_id = grant_row.id
                     WHERE identity.public_id = claim.id
                       AND manifest.document_id = p_document_internal_id
               );
            DELETE FROM public.claims AS claim
             WHERE claim.document_id = v_document_public_id
               AND NOT EXISTS (
                    SELECT 1
                      FROM audit.claim_public_identities AS identity
                      JOIN audit.claim_publication_grants AS grant_row
                        ON grant_row.claim_id = identity.claim_id
                       AND grant_row.grant_status = 'active'::audit.grant_status
                      JOIN audit.claim_publication_manifests AS manifest
                        ON manifest.grant_id = grant_row.id
                     WHERE identity.public_id = claim.id
                       AND manifest.document_id = p_document_internal_id
               );
            DELETE FROM public.evidence AS evidence
             WHERE evidence.document_id = v_document_public_id
               AND NOT EXISTS (
                    SELECT 1 FROM public.claim_evidence AS link
                     WHERE link.evidence_id = evidence.id
               );
            FOR v_active_claim IN
                SELECT grant_row.id
                  FROM audit.claim_publication_grants AS grant_row
                  JOIN audit.claim_publication_manifests AS manifest
                    ON manifest.grant_id = grant_row.id
                 WHERE grant_row.grant_status = 'active'::audit.grant_status
                   AND manifest.document_id = p_document_internal_id
                 ORDER BY manifest.claim_id
            LOOP
                PERFORM ops._wp10_3_reconcile_claim(v_active_claim.id);
            END LOOP;
            PERFORM ops._wp10_3_reconcile_document_entities(v_document_public_id);
            PERFORM ops._wp10_3_refresh_search(v_document_public_id);
        END
        $_wp10_3_reconcile_document_claims$;

        CREATE FUNCTION ops._wp10_3_reconcile_entity_links(p_entity_internal_id uuid)
        RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, audit, public, pg_catalog
        AS $_wp10_3_reconcile_entity_links$
        DECLARE
            document_row record;
        BEGIN
            FOR document_row IN
                SELECT DISTINCT claim.document_id
                  FROM public.claims AS claim
                  JOIN audit.claim_publication_manifests AS manifest
                    ON manifest.grant_id = claim.claim_grant_id
                 WHERE manifest.subject_entity_id = p_entity_internal_id
            LOOP
                PERFORM ops._wp10_3_reconcile_document_entities(document_row.document_id);
            END LOOP;
        END
        $_wp10_3_reconcile_entity_links$;

        CREATE FUNCTION ops._wp10_3_prepare_parent_withdraw(p_event_id uuid) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, audit, public, pg_catalog
        AS $_wp10_3_prepare_parent_withdraw$
        DECLARE
            event_row ops.outbox_events%ROWTYPE;
            v_grant_id uuid;
            v_decision_id uuid;
            v_revision integer;
            v_subject_type text;
            v_public_id uuid;
        BEGIN
            SELECT * INTO event_row FROM ops.outbox_events WHERE id = p_event_id;
            IF event_row.event_type <> 'publication.withdrawn' THEN RETURN; END IF;
            BEGIN
                v_grant_id := (event_row.payload ->> 'grant_id')::uuid;
                v_decision_id := (event_row.payload ->> 'decision_id')::uuid;
                v_revision := (event_row.payload ->> 'revision_no')::integer;
                v_subject_type := event_row.payload ->> 'subject_type';
            EXCEPTION WHEN OTHERS THEN
                RETURN;
            END;
            IF v_subject_type = 'document' THEN
                SELECT identity.public_id INTO v_public_id
                  FROM audit.document_public_identities AS identity
                  JOIN public.documents AS document
                    ON document.id = identity.public_id
                   AND document.document_grant_id = v_grant_id
                   AND document.revision_no = v_revision
                  JOIN audit.document_publication_grants AS grant_row
                    ON grant_row.id = v_grant_id
                   AND grant_row.grant_status = 'withdrawn'::audit.grant_status
                   AND grant_row.withdrawn_by_decision_id = v_decision_id
                 WHERE identity.document_id = (
                    SELECT manifest.document_id
                      FROM audit.document_publication_manifests AS manifest
                     WHERE manifest.grant_id = v_grant_id
                 );
                IF v_public_id IS NOT NULL THEN
                    DELETE FROM public.document_entities WHERE document_id = v_public_id;
                    DELETE FROM public.claim_evidence AS link
                     USING public.claims AS claim
                     WHERE link.claim_id = claim.id AND claim.document_id = v_public_id;
                    DELETE FROM public.claims WHERE document_id = v_public_id;
                    DELETE FROM public.evidence WHERE document_id = v_public_id;
                    DELETE FROM public.search_documents WHERE document_id = v_public_id;
                END IF;
            ELSIF v_subject_type = 'entity' THEN
                SELECT identity.public_id INTO v_public_id
                  FROM audit.entity_public_identities AS identity
                  JOIN public.entities AS entity
                    ON entity.id = identity.public_id
                   AND entity.entity_grant_id = v_grant_id
                   AND entity.revision_no = v_revision
                  JOIN audit.entity_publication_grants AS grant_row
                    ON grant_row.id = v_grant_id
                   AND grant_row.grant_status = 'withdrawn'::audit.grant_status
                   AND grant_row.withdrawn_by_decision_id = v_decision_id
                 WHERE identity.entity_id = (
                    SELECT manifest.entity_id
                      FROM audit.entity_publication_manifests AS manifest
                     WHERE manifest.grant_id = v_grant_id
                 );
                IF v_public_id IS NOT NULL THEN
                    DELETE FROM public.document_entities WHERE entity_id = v_public_id;
                END IF;
            END IF;
        END
        $_wp10_3_prepare_parent_withdraw$;

        CREATE FUNCTION ops._wp10_3_claim_projection_digest(p_claim_internal_id uuid)
        RETURNS text
        LANGUAGE sql STABLE STRICT
        SET search_path = ops, audit, public, pg_catalog
        AS $_wp10_3_claim_projection_digest$
            SELECT audit._payload_sha256(CASE
                WHEN claim.id IS NULL THEN jsonb_build_object(
                    'present', false, 'subject_type', 'claim',
                    'subject_id', lower(p_claim_internal_id::text)
                )
                ELSE jsonb_build_object(
                    'id', lower(claim.id::text),
                    'document_id', lower(claim.document_id::text),
                    'claim_grant_id', lower(claim.claim_grant_id::text),
                    'ordinal', claim.ordinal,
                    'claim_text', claim.claim_text,
                    'claim_type', claim.claim_type,
                    'assertion_status', claim.assertion_status,
                    'attribution', to_jsonb(claim.attribution),
                    'revision_no', claim.revision_no,
                    'evidence', coalesce((
                        SELECT jsonb_agg(lower(link.evidence_id::text) ORDER BY link.evidence_id)
                          FROM public.claim_evidence AS link
                         WHERE link.claim_id = claim.id
                    ), '[]'::jsonb)
                ) END)
              FROM (SELECT 1) AS anchor
              LEFT JOIN audit.claim_public_identities AS identity
                ON identity.claim_id = p_claim_internal_id
              LEFT JOIN public.claims AS claim ON claim.id = identity.public_id
        $_wp10_3_claim_projection_digest$;

        ALTER FUNCTION ops.claim_publication_outbox(text, integer, integer)
            RENAME TO _claim_publication_outbox_wp10_2;
        ALTER FUNCTION ops.apply_publication_event(uuid, uuid)
            RENAME TO _apply_publication_event_wp10_2;

        CREATE FUNCTION ops.claim_publication_outbox(
            p_dispatcher_id text,
            p_lease_seconds integer,
            p_limit integer DEFAULT 10
        ) RETURNS TABLE (
            event_id uuid, causation_job_id uuid, aggregate_type text,
            aggregate_id uuid, event_type text, event_key text, payload jsonb,
            attempt_no integer, lease_token uuid, lease_expires_at timestamptz
        )
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, audit, pg_catalog
        AS $claim_publication_outbox$
        DECLARE
            claimed_row record;
            event_row ops.outbox_events%ROWTYPE;
            v_count integer := 0;
            v_now timestamptz;
            v_token uuid;
        BEGIN
            FOR claimed_row IN
                SELECT * FROM ops._claim_publication_outbox_wp10_2(
                    p_dispatcher_id, p_lease_seconds, p_limit
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
                v_count := v_count + 1;
                RETURN NEXT;
            END LOOP;
            IF v_count >= p_limit THEN RETURN; END IF;
            v_now := clock_timestamp();
            FOR event_row IN
                SELECT candidate.*
                  FROM ops.outbox_events AS candidate
                 WHERE candidate.published_at IS NULL
                   AND candidate.terminal_at IS NULL
                   AND candidate.available_at <= v_now
                   AND (candidate.lease_expires_at IS NULL OR candidate.lease_expires_at <= v_now)
                   AND candidate.event_type IN (
                        'publication.granted', 'publication.superseded',
                        'publication.withdrawn'
                   )
                   AND candidate.aggregate_type = 'claim_publication_grants'
                   AND jsonb_typeof(candidate.payload) = 'object'
                   AND candidate.payload ->> 'schema' = 'publication-outbox.v2'
                   AND candidate.payload ->> 'subject_type' = 'claim'
                   AND (candidate.payload - ARRAY[
                        'schema', 'grant_id', 'decision_id', 'subject_type',
                        'subject_id', 'revision_no', 'payload_sha256', 'old_grant_id'
                   ]) = '{}'::jsonb
                   AND candidate.payload ?& ARRAY[
                        'grant_id', 'decision_id', 'subject_id',
                        'revision_no', 'payload_sha256'
                   ]
                   AND ((candidate.event_type = 'publication.superseded') =
                        (candidate.payload ? 'old_grant_id'))
                 ORDER BY candidate.occurred_at, candidate.id
                 FOR UPDATE SKIP LOCKED
                 LIMIT p_limit - v_count
            LOOP
                IF event_row.publish_attempts > 0 THEN
                    UPDATE audit.publication_delivery_attempts AS attempt
                       SET outcome = 'retryable_failure'::ops.attempt_outcome,
                           sanitized_error_code = 'publication_lease_lost',
                           sanitized_error_summary = ops._publication_sanitized_summary(
                               'publication_lease_lost'
                           ), finished_at = v_now, available_at = v_now
                     WHERE attempt.event_id = event_row.id
                       AND attempt.attempt_no = event_row.publish_attempts
                       AND attempt.outcome = 'running'::ops.attempt_outcome;
                END IF;
                v_token := gen_random_uuid();
                UPDATE ops.outbox_events
                   SET lease_owner = btrim(p_dispatcher_id), lease_token = v_token,
                       lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
                       publish_attempts = event_row.publish_attempts + 1
                 WHERE id = event_row.id;
                INSERT INTO audit.publication_delivery_attempts (
                    event_id, attempt_no, dispatcher, lease_token_hash, outcome, started_at
                ) VALUES (
                    event_row.id, event_row.publish_attempts + 1, btrim(p_dispatcher_id),
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
                attempt_no := event_row.publish_attempts + 1;
                lease_token := v_token;
                lease_expires_at := v_now + make_interval(secs => p_lease_seconds);
                RETURN NEXT;
            END LOOP;
        END
        $claim_publication_outbox$;

        CREATE FUNCTION ops._apply_claim_publication_event(
            p_event_id uuid, p_lease_token uuid
        ) RETURNS TABLE (
            event_id uuid, published_at timestamptz, projection_digest char(64),
            attempt_no integer, replayed boolean
        )
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, audit, public, pg_catalog
        SET timezone = 'UTC'
        AS $_apply_claim_publication_event$
        DECLARE
            event_row ops.outbox_events%ROWTYPE;
            attempt_row audit.publication_delivery_attempts%ROWTYPE;
            grant_row audit.claim_publication_grants%ROWTYPE;
            manifest_row audit.claim_publication_manifests%ROWTYPE;
            v_lease_hash text;
            v_operation_hash text;
            v_payload jsonb;
            v_grant_id uuid;
            v_old_grant_id uuid;
            v_decision_id uuid;
            v_claim_id uuid;
            v_revision integer;
            v_payload_sha text;
            v_now timestamptz;
            v_digest text;
            v_replayed boolean := false;
            v_document_public_id uuid;
            v_claim_public_id uuid;
        BEGIN
            IF session_user <> 'uap_publisher' OR p_event_id IS NULL OR p_lease_token IS NULL THEN
                RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
            END IF;
            v_operation_hash := audit._payload_sha256(jsonb_build_object(
                'event_id', lower(p_event_id::text), 'op', 'apply'
            ));
            v_lease_hash := audit._payload_sha256(jsonb_build_object(
                'lease_token', lower(p_lease_token::text)
            ));
            SELECT * INTO event_row FROM ops.outbox_events WHERE id = p_event_id FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
            END IF;
            SELECT * INTO attempt_row
              FROM audit.publication_delivery_attempts AS attempt
             WHERE attempt.event_id = p_event_id
               AND attempt.lease_token_hash = v_lease_hash
             ORDER BY attempt.attempt_no DESC LIMIT 1;
            IF FOUND AND event_row.publish_attempts > attempt_row.attempt_no THEN
                RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
            END IF;
            IF FOUND AND attempt_row.operation_payload_hash IS NOT NULL THEN
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
            END IF;
            v_now := clock_timestamp();
            IF NOT v_replayed THEN
                IF NOT FOUND OR event_row.published_at IS NOT NULL
                   OR event_row.terminal_at IS NOT NULL
                   OR event_row.lease_token IS DISTINCT FROM p_lease_token
                   OR event_row.lease_expires_at IS NULL
                   OR event_row.lease_expires_at <= v_now
                   OR attempt_row.outcome <> 'running'::ops.attempt_outcome
                THEN
                    RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
                END IF;
                PERFORM ops._publication_inject_failure('attempt');
                UPDATE audit.publication_delivery_attempts AS attempt
                   SET operation_payload_hash = v_operation_hash
                 WHERE attempt.event_id = p_event_id
                   AND attempt.attempt_no = attempt_row.attempt_no;
            END IF;

            v_payload := event_row.payload;
            IF jsonb_typeof(v_payload) IS DISTINCT FROM 'object'
               OR v_payload ->> 'schema' IS DISTINCT FROM 'publication-outbox.v2'
               OR v_payload ->> 'subject_type' IS DISTINCT FROM 'claim'
               OR event_row.aggregate_type IS DISTINCT FROM 'claim_publication_grants'
               OR event_row.event_type NOT IN (
                    'publication.granted', 'publication.superseded', 'publication.withdrawn'
               )
               OR (v_payload - ARRAY[
                    'schema', 'grant_id', 'decision_id', 'subject_type',
                    'subject_id', 'revision_no', 'payload_sha256', 'old_grant_id'
               ]) <> '{}'::jsonb
               OR ((event_row.event_type = 'publication.superseded') <>
                   (v_payload ? 'old_grant_id'))
            THEN
                RAISE EXCEPTION 'publication_event_schema_unsupported'
                    USING ERRCODE = '22023';
            END IF;
            BEGIN
                v_grant_id := (v_payload ->> 'grant_id')::uuid;
                v_decision_id := (v_payload ->> 'decision_id')::uuid;
                v_claim_id := (v_payload ->> 'subject_id')::uuid;
                v_revision := (v_payload ->> 'revision_no')::integer;
                v_payload_sha := v_payload ->> 'payload_sha256';
                IF event_row.event_type = 'publication.superseded' THEN
                    v_old_grant_id := (v_payload ->> 'old_grant_id')::uuid;
                END IF;
            EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                RAISE EXCEPTION 'publication_event_schema_unsupported'
                    USING ERRCODE = '22023';
            END;
            IF v_grant_id IS NULL OR v_decision_id IS NULL OR v_claim_id IS NULL
               OR v_revision < 1 OR v_payload_sha !~ '^[0-9a-f]{64}$'
            THEN
                RAISE EXCEPTION 'publication_event_schema_unsupported'
                    USING ERRCODE = '22023';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'publication-claim:' || v_claim_id::text, 0
            ));
            IF (event_row.event_type IN ('publication.granted', 'publication.withdrawn')
                AND event_row.aggregate_id IS DISTINCT FROM v_grant_id)
               OR (event_row.event_type = 'publication.superseded'
                   AND event_row.aggregate_id IS DISTINCT FROM v_old_grant_id)
            THEN
                RAISE EXCEPTION 'publication_event_aggregate_mismatch'
                    USING ERRCODE = '22023';
            END IF;
            SELECT * INTO grant_row FROM audit.claim_publication_grants
             WHERE id = v_grant_id;
            SELECT * INTO manifest_row FROM audit.claim_publication_manifests
             WHERE grant_id = v_grant_id;
            IF grant_row.id IS NULL OR manifest_row.grant_id IS NULL THEN
                RAISE EXCEPTION 'publication_grant_missing' USING ERRCODE = '22023';
            END IF;
            IF grant_row.claim_id IS DISTINCT FROM v_claim_id
               OR manifest_row.claim_id IS DISTINCT FROM v_claim_id
               OR grant_row.revision_no IS DISTINCT FROM v_revision
               OR (event_row.event_type <> 'publication.withdrawn'
                   AND manifest_row.decision_id IS DISTINCT FROM v_decision_id)
               OR v_payload_sha IS DISTINCT FROM btrim(grant_row.publication_payload_sha256::text)
               OR v_payload_sha IS DISTINCT FROM btrim(manifest_row.manifest_sha256::text)
               OR v_payload_sha IS DISTINCT FROM audit._publication_manifest_sha(
                    ops._wp10_3_claim_manifest_payload(v_grant_id)
               )
            THEN
                RAISE EXCEPTION 'publication_payload_hash_mismatch'
                    USING ERRCODE = '22023';
            END IF;
            IF event_row.event_type = 'publication.superseded' AND NOT EXISTS (
                SELECT 1 FROM audit.claim_publication_grants AS old_grant
                 WHERE old_grant.id = v_old_grant_id
                   AND old_grant.claim_id = v_claim_id
                   AND old_grant.grant_status = 'superseded'::audit.grant_status
                   AND old_grant.revision_no < v_revision
            ) THEN
                RAISE EXCEPTION 'publication_event_aggregate_mismatch'
                    USING ERRCODE = '22023';
            END IF;

            IF NOT v_replayed THEN
                IF event_row.event_type = 'publication.withdrawn' THEN
                    SELECT identity.public_id INTO v_claim_public_id
                      FROM audit.claim_public_identities AS identity
                     WHERE identity.claim_id = v_claim_id;
                    SELECT identity.public_id INTO v_document_public_id
                      FROM audit.document_public_identities AS identity
                     WHERE identity.document_id = manifest_row.document_id;
                    IF v_claim_public_id IS NOT NULL
                       AND EXISTS (
                            SELECT 1 FROM public.claims AS claim
                             WHERE claim.id = v_claim_public_id
                               AND claim.claim_grant_id = v_grant_id
                               AND claim.revision_no = v_revision
                       )
                       AND grant_row.grant_status = 'withdrawn'::audit.grant_status
                       AND grant_row.withdrawn_by_decision_id = v_decision_id
                    THEN
                        DELETE FROM public.document_entities
                         WHERE document_id = v_document_public_id;
                        DELETE FROM public.claim_evidence WHERE claim_id = v_claim_public_id;
                        DELETE FROM public.claims WHERE id = v_claim_public_id;
                        DELETE FROM public.evidence AS evidence
                         WHERE evidence.document_id = v_document_public_id
                           AND NOT EXISTS (
                                SELECT 1 FROM public.claim_evidence AS link
                                 WHERE link.evidence_id = evidence.id
                           );
                        PERFORM ops._wp10_3_reconcile_document_entities(v_document_public_id);
                        PERFORM ops._wp10_3_refresh_search(v_document_public_id);
                    END IF;
                ELSIF grant_row.grant_status = 'active'::audit.grant_status THEN
                    IF NOT ops._wp10_3_reconcile_claim(v_grant_id) THEN
                        RAISE EXCEPTION 'publication_dependency_not_ready'
                            USING ERRCODE = '40001';
                    END IF;
                END IF;
            END IF;
            v_digest := ops._wp10_3_claim_projection_digest(v_claim_id);
            IF v_replayed THEN
                IF v_digest IS DISTINCT FROM attempt_row.projection_digest THEN
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
            UPDATE audit.publication_delivery_attempts AS attempt
               SET outcome = 'succeeded'::ops.attempt_outcome,
                   projection_digest = v_digest, finished_at = v_now,
                   sanitized_error_code = NULL, sanitized_error_summary = NULL
             WHERE attempt.event_id = p_event_id
               AND attempt.attempt_no = attempt_row.attempt_no;
            UPDATE ops.outbox_events AS outbox
               SET published_at = v_now, lease_owner = NULL, lease_token = NULL,
                   lease_expires_at = NULL, last_error_code = NULL,
                   last_error_summary = NULL
             WHERE outbox.id = p_event_id AND outbox.published_at IS NULL
               AND outbox.terminal_at IS NULL AND outbox.lease_token = p_lease_token
               AND outbox.lease_expires_at > v_now;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'publication_lease_lost' USING ERRCODE = '40001';
            END IF;
            event_id := p_event_id;
            published_at := v_now;
            projection_digest := v_digest;
            attempt_no := attempt_row.attempt_no;
            replayed := false;
            RETURN NEXT;
        END
        $_apply_claim_publication_event$;

        CREATE FUNCTION ops.apply_publication_event(
            p_event_id uuid, p_lease_token uuid
        ) RETURNS TABLE (
            event_id uuid, published_at timestamptz, projection_digest char(64),
            attempt_no integer, replayed boolean
        )
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, audit, public, pg_catalog
        AS $apply_publication_event$
        DECLARE
            v_subject_type text;
            v_event_type text;
            v_grant_id uuid;
            v_document_internal_id uuid;
            v_entity_internal_id uuid;
            applied_row record;
        BEGIN
            IF session_user <> 'uap_publisher' THEN
                RAISE EXCEPTION 'only publisher may apply publication events'
                    USING ERRCODE = '42501';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended('publication-rebuild', 0));
            SELECT payload ->> 'subject_type', event_type
              INTO v_subject_type, v_event_type
              FROM ops.outbox_events WHERE id = p_event_id;
            IF v_subject_type = 'claim' THEN
                RETURN QUERY SELECT * FROM ops._apply_claim_publication_event(
                    p_event_id, p_lease_token
                );
                RETURN;
            END IF;
            IF v_event_type = 'publication.withdrawn' THEN
                PERFORM ops._wp10_3_prepare_parent_withdraw(p_event_id);
            END IF;
            FOR applied_row IN
                SELECT * FROM ops._apply_publication_event_wp10_2(
                    p_event_id, p_lease_token
                )
            LOOP
                IF NOT applied_row.replayed THEN
                    BEGIN
                        v_grant_id := (
                            SELECT (payload ->> 'grant_id')::uuid
                              FROM ops.outbox_events WHERE id = p_event_id
                        );
                    EXCEPTION WHEN OTHERS THEN
                        v_grant_id := NULL;
                    END;
                    IF v_subject_type = 'document' AND v_grant_id IS NOT NULL THEN
                        SELECT manifest.document_id INTO v_document_internal_id
                          FROM audit.document_publication_manifests AS manifest
                         WHERE manifest.grant_id = v_grant_id;
                        IF v_document_internal_id IS NOT NULL THEN
                            PERFORM ops._wp10_3_reconcile_document_claims(
                                v_document_internal_id
                            );
                        END IF;
                    ELSIF v_subject_type = 'entity' AND v_grant_id IS NOT NULL THEN
                        SELECT manifest.entity_id INTO v_entity_internal_id
                          FROM audit.entity_publication_manifests AS manifest
                         WHERE manifest.grant_id = v_grant_id;
                        IF v_entity_internal_id IS NOT NULL THEN
                            PERFORM ops._wp10_3_reconcile_entity_links(v_entity_internal_id);
                        END IF;
                    END IF;
                END IF;
                event_id := applied_row.event_id;
                published_at := applied_row.published_at;
                projection_digest := applied_row.projection_digest;
                attempt_no := applied_row.attempt_no;
                replayed := applied_row.replayed;
                RETURN NEXT;
            END LOOP;
        END
        $apply_publication_event$;

        CREATE FUNCTION ops._wp10_3_projection_digest() RETURNS text
        LANGUAGE sql STABLE
        SET search_path = audit, public, pg_catalog
        AS $_wp10_3_projection_digest$
            SELECT audit._payload_sha256(jsonb_build_object(
                'documents', coalesce((SELECT jsonb_agg(to_jsonb(row_data) ORDER BY id)
                    FROM (SELECT id, document_grant_id, slug, title, summary,
                                 category::text, fact_status::text, source_name,
                                 canonical_source_url, source_published_at, revision_no
                            FROM public.documents) AS row_data), '[]'::jsonb),
                'entities', coalesce((SELECT jsonb_agg(to_jsonb(row_data) ORDER BY id)
                    FROM (SELECT id, entity_grant_id, slug, entity_type, name,
                                 description, country_code, revision_no
                            FROM public.entities) AS row_data), '[]'::jsonb),
                'claims', coalesce((SELECT jsonb_agg(to_jsonb(row_data) ORDER BY id)
                    FROM (SELECT id, document_id, claim_grant_id, ordinal, claim_text,
                                 claim_type, assertion_status, attribution, revision_no
                            FROM public.claims) AS row_data), '[]'::jsonb),
                'evidence', coalesce((SELECT jsonb_agg(to_jsonb(row_data) ORDER BY id)
                    FROM (SELECT id, document_id, excerpt, locator_type, page_start,
                                 page_end, time_start_ms, time_end_ms, public_locator,
                                 locator_sha256, source_url FROM public.evidence) AS row_data
                ), '[]'::jsonb),
                'claim_evidence', coalesce((SELECT jsonb_agg(to_jsonb(row_data) ORDER BY id)
                    FROM (SELECT id, claim_id, evidence_id FROM public.claim_evidence) AS row_data
                ), '[]'::jsonb),
                'document_entities', coalesce((SELECT jsonb_agg(to_jsonb(row_data) ORDER BY id)
                    FROM (SELECT id, document_id, entity_id, basis_evidence_id,
                                 basis_claim_id FROM public.document_entities) AS row_data
                ), '[]'::jsonb),
                'search', coalesce((SELECT jsonb_agg(to_jsonb(row_data) ORDER BY document_id)
                    FROM (SELECT document_id, search_vector::text, display_text, facets
                            FROM public.search_documents) AS row_data), '[]'::jsonb)
            ))
        $_wp10_3_projection_digest$;

        CREATE FUNCTION ops._wp10_3_grant_has_unresolved_quarantine(
            p_grant_table text,
            p_grant_id uuid
        ) RETURNS boolean
        LANGUAGE sql STABLE STRICT SECURITY DEFINER
        SET search_path = ops, audit, pg_catalog
        AS $_wp10_3_grant_has_unresolved_quarantine$
            SELECT EXISTS (
                SELECT 1
                  FROM audit.publication_quarantine AS quarantine
                 WHERE quarantine.grant_table = p_grant_table
                   AND quarantine.grant_id = p_grant_id
                   AND quarantine.resolves_quarantine_id IS NULL
                   AND NOT EXISTS (
                        SELECT 1
                          FROM audit.publication_quarantine AS resolution
                         WHERE resolution.resolves_quarantine_id = quarantine.id
                   )
            )
        $_wp10_3_grant_has_unresolved_quarantine$;

        CREATE FUNCTION ops._wp10_3_rebuild_inputs_valid() RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, audit, core, public, pg_catalog
        AS $_wp10_3_rebuild_inputs_valid$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM audit.document_publication_grants AS grant_row
                  LEFT JOIN audit.document_publication_manifests AS manifest
                    ON manifest.grant_id = grant_row.id
                 WHERE grant_row.grant_status = 'active'::audit.grant_status
                   AND manifest.grant_id IS NULL
                   AND NOT ops._wp10_3_grant_has_unresolved_quarantine(
                       'document_publication_grants', grant_row.id
                   )
            ) OR EXISTS (
                SELECT 1
                  FROM audit.entity_publication_grants AS grant_row
                  LEFT JOIN audit.entity_publication_manifests AS manifest
                    ON manifest.grant_id = grant_row.id
                 WHERE grant_row.grant_status = 'active'::audit.grant_status
                   AND manifest.grant_id IS NULL
                   AND NOT ops._wp10_3_grant_has_unresolved_quarantine(
                       'entity_publication_grants', grant_row.id
                   )
            ) OR EXISTS (
                SELECT 1
                  FROM audit.claim_publication_grants AS grant_row
                  LEFT JOIN audit.claim_publication_manifests AS manifest
                    ON manifest.grant_id = grant_row.id
                 WHERE grant_row.grant_status = 'active'::audit.grant_status
                   AND manifest.grant_id IS NULL
                   AND NOT ops._wp10_3_grant_has_unresolved_quarantine(
                       'claim_publication_grants', grant_row.id
                   )
            ) OR EXISTS (
                SELECT 1
                  FROM audit.document_publication_grants AS grant_row
                  JOIN audit.document_publication_manifests AS manifest
                    ON manifest.grant_id = grant_row.id
                 WHERE grant_row.grant_status = 'active'::audit.grant_status
                   AND NOT ops._wp10_3_grant_has_unresolved_quarantine(
                       'document_publication_grants', grant_row.id
                   )
                   AND (btrim(grant_row.publication_payload_sha256::text) IS DISTINCT FROM
                        btrim(manifest.manifest_sha256::text)
                        OR btrim(manifest.manifest_sha256::text) IS DISTINCT FROM
                           audit._publication_manifest_sha(
                               ops._wp10_3_document_manifest_payload(grant_row.id)
                           ))
            ) OR EXISTS (
                SELECT 1
                  FROM audit.entity_publication_grants AS grant_row
                  JOIN audit.entity_publication_manifests AS manifest
                    ON manifest.grant_id = grant_row.id
                 WHERE grant_row.grant_status = 'active'::audit.grant_status
                   AND NOT ops._wp10_3_grant_has_unresolved_quarantine(
                       'entity_publication_grants', grant_row.id
                   )
                   AND (btrim(grant_row.publication_payload_sha256::text) IS DISTINCT FROM
                        btrim(manifest.manifest_sha256::text)
                        OR btrim(manifest.manifest_sha256::text) IS DISTINCT FROM
                           audit._publication_manifest_sha(
                               ops._wp10_3_entity_manifest_payload(grant_row.id)
                           ))
            ) OR EXISTS (
                SELECT 1
                  FROM audit.claim_publication_grants AS grant_row
                  JOIN audit.claim_publication_manifests AS manifest
                    ON manifest.grant_id = grant_row.id
                 WHERE grant_row.grant_status = 'active'::audit.grant_status
                   AND NOT ops._wp10_3_grant_has_unresolved_quarantine(
                       'claim_publication_grants', grant_row.id
                   )
                   AND (btrim(grant_row.publication_payload_sha256::text) IS DISTINCT FROM
                        btrim(manifest.manifest_sha256::text)
                        OR btrim(manifest.manifest_sha256::text) IS DISTINCT FROM
                           audit._publication_manifest_sha(
                               ops._wp10_3_claim_manifest_payload(grant_row.id)
                           ))
            ) THEN
                RETURN false;
            END IF;
            RETURN true;
        END
        $_wp10_3_rebuild_inputs_valid$;

        CREATE FUNCTION ops.rebuild_public_projection(p_rebuild_id uuid)
        RETURNS TABLE (
            rebuild_id uuid, input_digest char(64), result_digest char(64),
            counts jsonb, status text, replayed boolean
        )
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, audit, core, public, pg_catalog
        SET timezone = 'UTC'
        AS $rebuild_public_projection$
        DECLARE
            run_row audit.publication_rebuild_runs%ROWTYPE;
            v_input text;
            v_result text;
            v_counts jsonb;
            v_now timestamptz := clock_timestamp();
            row_data record;
            v_public_id uuid;
            v_slug text;
            v_inputs_valid boolean;
        BEGIN
            IF session_user NOT IN ('uap_migrator', 'uap_owner') THEN
                RAISE EXCEPTION 'only migrator may rebuild publication projection'
                    USING ERRCODE = '42501';
            END IF;
            IF p_rebuild_id IS NULL THEN
                RAISE EXCEPTION 'publication_rebuild_id_conflict' USING ERRCODE = '22023';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended('publication-rebuild', 0));
            SELECT audit._payload_sha256(jsonb_build_object(
                'document', coalesce((SELECT jsonb_agg(jsonb_build_array(
                    lower(grant_row.id::text),
                    btrim(grant_row.publication_payload_sha256::text),
                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE
                        btrim(manifest.manifest_sha256::text) END,
                    manifest.grant_id IS NOT NULL,
                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE
                        btrim(audit._publication_manifest_sha(
                            ops._wp10_3_document_manifest_payload(grant_row.id)
                        )) END
                ) ORDER BY grant_row.id)
                FROM audit.document_publication_grants AS grant_row
                 LEFT JOIN audit.document_publication_manifests AS manifest
                   ON manifest.grant_id = grant_row.id
                WHERE grant_row.grant_status = 'active'::audit.grant_status
                  AND manifest.grant_id IS NOT NULL
                  AND NOT ops._wp10_3_grant_has_unresolved_quarantine(
                      'document_publication_grants', grant_row.id
                  )), '[]'::jsonb),
                'entity', coalesce((SELECT jsonb_agg(jsonb_build_array(
                    lower(grant_row.id::text),
                    btrim(grant_row.publication_payload_sha256::text),
                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE
                        btrim(manifest.manifest_sha256::text) END,
                    manifest.grant_id IS NOT NULL,
                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE
                        btrim(audit._publication_manifest_sha(
                            ops._wp10_3_entity_manifest_payload(grant_row.id)
                        )) END
                ) ORDER BY grant_row.id)
                FROM audit.entity_publication_grants AS grant_row
                 LEFT JOIN audit.entity_publication_manifests AS manifest
                   ON manifest.grant_id = grant_row.id
                WHERE grant_row.grant_status = 'active'::audit.grant_status
                  AND manifest.grant_id IS NOT NULL
                  AND NOT ops._wp10_3_grant_has_unresolved_quarantine(
                      'entity_publication_grants', grant_row.id
                  )), '[]'::jsonb),
                'claim', coalesce((SELECT jsonb_agg(jsonb_build_array(
                    lower(grant_row.id::text),
                    btrim(grant_row.publication_payload_sha256::text),
                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE
                        btrim(manifest.manifest_sha256::text) END,
                    manifest.grant_id IS NOT NULL,
                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE
                        btrim(audit._publication_manifest_sha(
                            ops._wp10_3_claim_manifest_payload(grant_row.id)
                        )) END
                ) ORDER BY grant_row.id)
                FROM audit.claim_publication_grants AS grant_row
                 LEFT JOIN audit.claim_publication_manifests AS manifest
                   ON manifest.grant_id = grant_row.id
                WHERE grant_row.grant_status = 'active'::audit.grant_status
                  AND manifest.grant_id IS NOT NULL
                  AND NOT ops._wp10_3_grant_has_unresolved_quarantine(
                      'claim_publication_grants', grant_row.id
                  )), '[]'::jsonb)
            )) INTO v_input;
            SELECT ops._wp10_3_rebuild_inputs_valid() INTO v_inputs_valid;
            SELECT * INTO run_row FROM audit.publication_rebuild_runs
             WHERE publication_rebuild_runs.rebuild_id = p_rebuild_id FOR UPDATE;
            IF FOUND THEN
                IF NOT v_inputs_valid THEN
                    RAISE EXCEPTION 'publication_rebuild_mismatch' USING ERRCODE = '22023';
                END IF;
                IF btrim(run_row.input_digest::text) IS DISTINCT FROM v_input THEN
                    RAISE EXCEPTION 'publication_rebuild_id_conflict'
                        USING ERRCODE = '40001';
                END IF;
                rebuild_id := run_row.rebuild_id;
                input_digest := run_row.input_digest;
                result_digest := run_row.result_digest;
                counts := run_row.counts;
                status := run_row.status;
                replayed := true;
                RETURN NEXT;
                RETURN;
            END IF;
            INSERT INTO audit.publication_rebuild_runs (
                rebuild_id, input_digest, status, started_at
            ) VALUES (p_rebuild_id, v_input, 'running', v_now);
            BEGIN
                IF NOT v_inputs_valid THEN
                    RAISE EXCEPTION 'publication_rebuild_mismatch' USING ERRCODE = '22023';
                END IF;

                DELETE FROM public.document_entities;
                DELETE FROM public.claim_evidence;
                DELETE FROM public.claims;
                DELETE FROM public.evidence;
                DELETE FROM public.search_documents;
                DELETE FROM public.documents;
                DELETE FROM public.entities;

                FOR row_data IN
                    SELECT DISTINCT ON (manifest.document_id)
                           grant_row.id AS grant_id, grant_row.revision_no, manifest.*
                      FROM audit.document_publication_grants AS grant_row
                     JOIN audit.document_publication_manifests AS manifest
                        ON manifest.grant_id = grant_row.id
                     WHERE grant_row.grant_status = 'active'::audit.grant_status
                       AND NOT ops._wp10_3_grant_has_unresolved_quarantine(
                           'document_publication_grants', grant_row.id
                       )
                     ORDER BY manifest.document_id, grant_row.revision_no DESC, grant_row.id
                LOOP
                    SELECT identity.public_id, identity.slug INTO v_public_id, v_slug
                      FROM audit.document_public_identities AS identity
                     WHERE identity.document_id = row_data.document_id;
                    IF v_public_id IS NULL THEN
                        v_public_id := gen_random_uuid();
                        v_slug := 'd-' || replace(v_public_id::text, '-', '');
                        INSERT INTO audit.document_public_identities (
                            document_id, public_id, slug
                        ) VALUES (row_data.document_id, v_public_id, v_slug);
                    END IF;
                    INSERT INTO public.documents (
                        id, document_grant_id, slug, title, summary, category,
                        fact_status, source_name, canonical_source_url,
                        source_published_at, published_at, revised_at, revision_no
                    ) VALUES (
                        v_public_id, row_data.grant_id, v_slug, row_data.title,
                        row_data.summary, row_data.category, row_data.fact_status,
                        row_data.source_name, row_data.canonical_source_url,
                        row_data.source_published_at, v_now, NULL, row_data.revision_no
                    );
                END LOOP;
                FOR row_data IN
                    SELECT grant_row.id AS grant_id, grant_row.revision_no, manifest.*
                      FROM audit.entity_publication_grants AS grant_row
                      JOIN audit.entity_publication_manifests AS manifest
                        ON manifest.grant_id = grant_row.id
                      JOIN core.entities AS entity ON entity.id = manifest.entity_id
                     WHERE grant_row.grant_status = 'active'::audit.grant_status
                       AND NOT ops._wp10_3_grant_has_unresolved_quarantine(
                           'entity_publication_grants', grant_row.id
                       )
                       AND entity.status = 'active'::core.entity_status
                       AND core.canonical_entity_id(entity.id) = entity.id
                     ORDER BY manifest.entity_id, grant_row.revision_no DESC
                LOOP
                    IF EXISTS (
                        SELECT 1 FROM public.entities AS entity
                         JOIN audit.entity_public_identities AS identity
                           ON identity.public_id = entity.id
                        WHERE identity.entity_id = row_data.entity_id
                    ) THEN
                        CONTINUE;
                    END IF;
                    SELECT identity.public_id, identity.slug INTO v_public_id, v_slug
                      FROM audit.entity_public_identities AS identity
                     WHERE identity.entity_id = row_data.entity_id;
                    IF v_public_id IS NULL THEN
                        v_public_id := gen_random_uuid();
                        v_slug := 'e-' || replace(v_public_id::text, '-', '');
                        INSERT INTO audit.entity_public_identities (
                            entity_id, public_id, slug
                        ) VALUES (row_data.entity_id, v_public_id, v_slug);
                    END IF;
                    INSERT INTO public.entities (
                        id, entity_grant_id, slug, entity_type, name, description,
                        country_code, published_at, revision_no
                    ) VALUES (
                        v_public_id, row_data.grant_id, v_slug, row_data.entity_type::text,
                        row_data.canonical_name, row_data.description,
                        row_data.country_code, v_now, row_data.revision_no
                    );
                END LOOP;
                FOR row_data IN
                    SELECT grant_row.id
                      FROM audit.claim_publication_grants AS grant_row
                     JOIN audit.claim_publication_manifests AS manifest
                        ON manifest.grant_id = grant_row.id
                     WHERE grant_row.grant_status = 'active'::audit.grant_status
                       AND NOT ops._wp10_3_grant_has_unresolved_quarantine(
                           'claim_publication_grants', grant_row.id
                       )
                     ORDER BY manifest.document_id, manifest.claim_id
                LOOP
                    PERFORM ops._wp10_3_reconcile_claim(row_data.id);
                END LOOP;
                FOR row_data IN SELECT id FROM public.documents LOOP
                    PERFORM ops._wp10_3_reconcile_document_entities(row_data.id);
                    PERFORM ops._wp10_3_refresh_search(row_data.id);
                END LOOP;
                v_result := ops._wp10_3_projection_digest();
                SELECT jsonb_build_object(
                    'documents', (SELECT count(*) FROM public.documents),
                    'entities', (SELECT count(*) FROM public.entities),
                    'claims', (SELECT count(*) FROM public.claims),
                    'evidence', (SELECT count(*) FROM public.evidence),
                    'claim_evidence', (SELECT count(*) FROM public.claim_evidence),
                    'document_entities', (SELECT count(*) FROM public.document_entities),
                    'search_documents', (SELECT count(*) FROM public.search_documents)
                ) INTO v_counts;
            EXCEPTION WHEN OTHERS THEN
                IF SQLERRM NOT IN (
                    'publication_rebuild_mismatch',
                    'publication_payload_hash_mismatch',
                    'publication_evidence_required'
                ) THEN
                    RAISE;
                END IF;
                UPDATE audit.publication_rebuild_runs AS run
                   SET status = 'failed', error_code = 'publication_rebuild_mismatch',
                       counts = jsonb_build_object(
                           'blocked', true,
                           'error_code', 'publication_rebuild_mismatch'
                       ), finished_at = clock_timestamp()
                 WHERE run.rebuild_id = p_rebuild_id;
                SELECT * INTO run_row FROM audit.publication_rebuild_runs
                 WHERE publication_rebuild_runs.rebuild_id = p_rebuild_id;
                rebuild_id := run_row.rebuild_id;
                input_digest := run_row.input_digest;
                result_digest := run_row.result_digest;
                counts := run_row.counts;
                status := run_row.status;
                replayed := false;
                RETURN NEXT;
                RETURN;
            END;
            UPDATE audit.publication_rebuild_runs AS run
               SET result_digest = v_result, counts = v_counts, status = 'succeeded',
                   finished_at = clock_timestamp()
             WHERE run.rebuild_id = p_rebuild_id;
            rebuild_id := p_rebuild_id;
            input_digest := v_input;
            result_digest := v_result;
            counts := v_counts;
            status := 'succeeded';
            replayed := false;
            RETURN NEXT;
        END
        $rebuild_public_projection$;

        REVOKE ALL ON TABLE audit.publication_rebuild_runs FROM PUBLIC;
        REVOKE ALL ON TABLE audit.publication_rebuild_runs
            FROM uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        GRANT SELECT ON audit.publication_rebuild_runs TO uap_audit_reader;
        REVOKE ALL ON FUNCTION audit.guard_publication_rebuild_run_mutation()
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._wp10_3_public_row_id(text, jsonb)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._wp10_3_document_manifest_payload(uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._wp10_3_entity_manifest_payload(uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._wp10_3_claim_manifest_payload(uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._wp10_3_refresh_search(uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._wp10_3_reconcile_document_entities(uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._wp10_3_reconcile_claim(uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._wp10_3_reconcile_document_claims(uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._wp10_3_reconcile_entity_links(uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._wp10_3_prepare_parent_withdraw(uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._wp10_3_claim_projection_digest(uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._apply_claim_publication_event(uuid, uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._wp10_3_projection_digest()
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._wp10_3_rebuild_inputs_valid()
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._wp10_3_grant_has_unresolved_quarantine(text, uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._claim_publication_outbox_wp10_2(
            text, integer, integer
        ) FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
               uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops._apply_publication_event_wp10_2(uuid, uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops.claim_publication_outbox(text, integer, integer)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_model_governance,
                 uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION ops.apply_publication_event(uuid, uuid)
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
        REVOKE ALL ON FUNCTION ops.rebuild_public_projection(uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        GRANT EXECUTE ON FUNCTION ops.rebuild_public_projection(uuid) TO uap_migrator;
        REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM uap_publisher;
        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        DO $wp10_3_downgrade_guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.claims)
               OR EXISTS (SELECT 1 FROM public.evidence)
               OR EXISTS (SELECT 1 FROM public.claim_evidence)
               OR EXISTS (SELECT 1 FROM public.document_entities)
               OR EXISTS (SELECT 1 FROM public.search_documents)
               OR EXISTS (SELECT 1 FROM audit.claim_public_identities)
               OR EXISTS (SELECT 1 FROM audit.evidence_public_identities)
               OR EXISTS (SELECT 1 FROM audit.publication_rebuild_runs)
            THEN
                RAISE EXCEPTION 'publication_contract_state_blocks_downgrade'
                    USING ERRCODE = '22023';
            END IF;
        END
        $wp10_3_downgrade_guard$;

        REVOKE EXECUTE ON FUNCTION ops.rebuild_public_projection(uuid) FROM uap_migrator;
        REVOKE EXECUTE ON FUNCTION ops.claim_publication_outbox(text, integer, integer)
            FROM uap_publisher;
        REVOKE EXECUTE ON FUNCTION ops.apply_publication_event(uuid, uuid)
            FROM uap_publisher;
        DROP FUNCTION ops.rebuild_public_projection(uuid);
        DROP FUNCTION ops._wp10_3_projection_digest();
        DROP FUNCTION ops._wp10_3_rebuild_inputs_valid();
        DROP FUNCTION ops._wp10_3_grant_has_unresolved_quarantine(text, uuid);
        DROP FUNCTION ops.apply_publication_event(uuid, uuid);
        DROP FUNCTION ops._apply_claim_publication_event(uuid, uuid);
        DROP FUNCTION ops.claim_publication_outbox(text, integer, integer);
        ALTER FUNCTION ops._apply_publication_event_wp10_2(uuid, uuid)
            RENAME TO apply_publication_event;
        ALTER FUNCTION ops._claim_publication_outbox_wp10_2(text, integer, integer)
            RENAME TO claim_publication_outbox;
        DROP FUNCTION ops._wp10_3_claim_projection_digest(uuid);
        DROP FUNCTION ops._wp10_3_prepare_parent_withdraw(uuid);
        DROP FUNCTION ops._wp10_3_reconcile_entity_links(uuid);
        DROP FUNCTION ops._wp10_3_reconcile_document_claims(uuid);
        DROP FUNCTION ops._wp10_3_reconcile_claim(uuid);
        DROP FUNCTION ops._wp10_3_reconcile_document_entities(uuid);
        DROP FUNCTION ops._wp10_3_refresh_search(uuid);
        DROP FUNCTION ops._wp10_3_claim_manifest_payload(uuid);
        DROP FUNCTION ops._wp10_3_entity_manifest_payload(uuid);
        DROP FUNCTION ops._wp10_3_document_manifest_payload(uuid);
        DROP FUNCTION ops._wp10_3_public_row_id(text, jsonb);
        DROP INDEX public.ix_public_document_entities_entity;
        DROP INDEX public.ix_public_claim_evidence_claim;
        DROP INDEX public.ix_public_claims_document_ordinal;
        DROP INDEX public.uq_public_claim_document_ordinal;
        DROP TRIGGER publication_rebuild_runs_immutable ON audit.publication_rebuild_runs;
        DROP FUNCTION audit.guard_publication_rebuild_run_mutation();
        DROP TABLE audit.publication_rebuild_runs;
        REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ops FROM uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.claim_publication_outbox(text, integer, integer)
            TO uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.apply_publication_event(uuid, uuid)
            TO uap_publisher;
        GRANT EXECUTE ON FUNCTION ops.fail_publication_event(
            uuid, uuid, text, text, integer, boolean
        ) TO uap_publisher;
        RESET ROLE;
        """
    )
