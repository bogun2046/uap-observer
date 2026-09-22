"""Freeze post-publication withdraw and document-scoped projection recovery.

This migration keeps the V1-3.1 pre-publication revoke guard, while allowing a
senior reviewer to withdraw a document publication that has a successful
``publication.granted`` apply.  It also adds an operator-only, document-scoped
rebuild that uses the immutable manifest and successful publication evidence.
No business data is changed during upgrade or downgrade.

The existing unfenced grant function continues to create the frozen
``publication.withdrawn`` outbox event after this gate accepts the decision.
"""

from __future__ import annotations

from alembic import op

revision = "0034_v133_postpublication_withdraw_rebuild"
down_revision = "0033_v132_publication_payload_revision_compat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION ops._v133_document_grant_was_published(
            p_grant_id uuid,
            p_document_version_id uuid
        ) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, audit, core, public, pg_catalog
        AS $v133_document_grant_was_published$
        DECLARE
            v_grant audit.document_publication_grants%ROWTYPE;
            v_manifest audit.document_publication_manifests%ROWTYPE;
            v_event ops.outbox_events%ROWTYPE;
            v_public_id uuid;
        BEGIN
            SELECT * INTO v_grant
              FROM audit.document_publication_grants
             WHERE id = p_grant_id
               AND document_version_id = p_document_version_id
               AND grant_status = 'active'::audit.grant_status;
            IF NOT FOUND THEN RETURN false; END IF;

            SELECT * INTO v_manifest
              FROM audit.document_publication_manifests
             WHERE grant_id = p_grant_id;
            IF NOT FOUND
               OR v_manifest.document_version_id IS DISTINCT FROM v_grant.document_version_id
               OR v_manifest.editorial_revision_id IS DISTINCT FROM v_grant.editorial_revision_id
               OR v_manifest.editorial_revision_no IS DISTINCT FROM v_grant.editorial_revision_no
               OR btrim(v_grant.publication_payload_sha256::text)
                    IS DISTINCT FROM btrim(v_manifest.manifest_sha256::text)
               OR btrim(v_manifest.manifest_sha256::text)
                    IS DISTINCT FROM btrim(audit._publication_manifest_sha(
                        ops._wp10_3_document_manifest_payload(p_grant_id)
                    ))
            THEN
                RETURN false;
            END IF;

            SELECT * INTO v_event
              FROM ops.outbox_events
             WHERE aggregate_type = 'document_publication_grants'
               AND aggregate_id = p_grant_id
               AND event_type = 'publication.granted'
               AND published_at IS NOT NULL
               AND terminal_at IS NULL
             ORDER BY occurred_at ASC, id ASC
             LIMIT 1;
            IF NOT FOUND THEN RETURN false; END IF;

            SELECT identity.public_id INTO v_public_id
              FROM audit.document_public_identities AS identity
             WHERE identity.document_id = v_manifest.document_id;
            IF v_public_id IS NULL THEN RETURN false; END IF;

            -- A present row must still be the row belonging to this exact
            -- publication.  A missing row is a recoverable projection gap.
            IF EXISTS (
                SELECT 1
                  FROM public.documents AS document
                 WHERE document.id = v_public_id
                   AND (
                       document.document_grant_id IS DISTINCT FROM v_grant.id
                       OR document.revision_no IS DISTINCT FROM v_grant.revision_no
                   )
            ) THEN
                RETURN false;
            END IF;
            RETURN true;
        END
        $v133_document_grant_was_published$;

        ALTER FUNCTION ops._v133_document_grant_was_published(uuid, uuid)
            OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION ops._v133_document_grant_was_published(uuid, uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;

        ALTER FUNCTION audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) RENAME TO _apply_publication_grant_v131_prepublication_guard;

        CREATE FUNCTION audit._apply_publication_grant(
            p_case_type audit.review_case_type,
            p_subject_id uuid,
            p_case_id uuid,
            p_decision_id uuid,
            p_decision audit.review_decision
        ) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = audit, core, ops, public, pg_catalog
        AS $apply_publication_grant_v133_withdraw$
        DECLARE
            v_grant_id uuid;
            v_event ops.outbox_events%ROWTYPE;
            v_grant audit.document_publication_grants%ROWTYPE;
            v_manifest audit.document_publication_manifests%ROWTYPE;
        BEGIN
            IF p_case_type = 'document'::audit.review_case_type
               AND p_decision = 'withdraw'::audit.review_decision
            THEN
                -- Match Publisher's global rebuild lock -> event row lock ->
                -- subject lock order before inspecting publication state.
                PERFORM pg_advisory_xact_lock(hashtextextended('publication-rebuild', 0));

                SELECT grant_row.id INTO v_grant_id
                  FROM audit.document_publication_grants AS grant_row
                 WHERE grant_row.document_version_id = p_subject_id
                   AND grant_row.grant_status = 'active'::audit.grant_status;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'review_grant_not_active' USING ERRCODE = '22023';
                END IF;

                SELECT event.* INTO v_event
                  FROM ops.outbox_events AS event
                 WHERE event.aggregate_type = 'document_publication_grants'
                   AND event.aggregate_id = v_grant_id
                   AND event.event_type = 'publication.granted'
                 ORDER BY event.occurred_at ASC, event.id ASC
                 LIMIT 1
                 FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'publication_event_not_terminal' USING ERRCODE = '23505';
                END IF;

                PERFORM pg_advisory_xact_lock(
                    hashtextextended('publication-document:' || p_subject_id::text, 0)
                );

                SELECT * INTO v_grant
                  FROM audit.document_publication_grants AS grant_row
                 WHERE grant_row.id = v_grant_id
                 FOR UPDATE;
                SELECT * INTO v_manifest
                  FROM audit.document_publication_manifests AS manifest
                 WHERE manifest.grant_id = v_grant_id;

                IF v_event.published_at IS NOT NULL THEN
                    IF NOT ops._v133_document_grant_was_published(
                        v_grant_id, p_subject_id
                    ) THEN
                        RAISE EXCEPTION 'publication_already_projected'
                            USING ERRCODE = '23505';
                    END IF;
                ELSE
                    -- Preserve 0031 pre-publication revoke semantics.  An
                    -- active lease or any current projection blocks revoke.
                    IF (
                        v_event.lease_token IS NOT NULL
                        AND v_event.lease_expires_at > clock_timestamp()
                    ) OR EXISTS (
                        SELECT 1
                          FROM public.documents AS document
                         WHERE document.document_grant_id = v_grant_id
                    ) THEN
                        RAISE EXCEPTION 'publication_already_projected'
                            USING ERRCODE = '23505';
                    END IF;
                END IF;

                IF v_manifest.grant_id IS NULL
                   OR v_manifest.document_version_id IS DISTINCT FROM v_grant.document_version_id
                   OR v_manifest.editorial_revision_id IS DISTINCT FROM v_grant.editorial_revision_id
                   OR v_manifest.editorial_revision_no IS DISTINCT FROM v_grant.editorial_revision_no
                THEN
                    RAISE EXCEPTION 'publication_payload_hash_mismatch'
                        USING ERRCODE = '22023';
                END IF;
            END IF;

            -- Call the pre-0031 implementation after this migration has made
            -- the explicit pre/post-publication decision.  The old guarded
            -- wrapper remains available for downgrade only.
            PERFORM audit._apply_publication_grant_v131_unfenced(
                p_case_type, p_subject_id, p_case_id, p_decision_id, p_decision
            );
        END
        $apply_publication_grant_v133_withdraw$;

        ALTER FUNCTION audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._apply_publication_grant_v131_prepublication_guard(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) FROM PUBLIC;

        CREATE FUNCTION ops.rebuild_public_projection(
            p_rebuild_id uuid,
            p_document_id uuid
        ) RETURNS TABLE (
            rebuild_id uuid, input_digest char(64), result_digest char(64),
            counts jsonb, status text, replayed boolean
        )
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = ops, audit, core, public, pg_catalog
        SET timezone = 'UTC'
        AS $rebuild_public_document_projection$
        DECLARE
            run_row audit.publication_rebuild_runs%ROWTYPE;
            v_document_grant audit.document_publication_grants%ROWTYPE;
            manifest_row audit.document_publication_manifests%ROWTYPE;
            event_row ops.outbox_events%ROWTYPE;
            v_input text;
            v_result text;
            v_counts jsonb;
            v_document_public_id uuid;
            v_public_id uuid;
            v_slug text;
            v_now timestamptz := clock_timestamp();
            v_claim record;
            v_entity record;
        BEGIN
            IF session_user NOT IN ('uap_migrator', 'uap_owner') THEN
                RAISE EXCEPTION 'only migrator may rebuild publication projection'
                    USING ERRCODE = '42501';
            END IF;
            IF p_rebuild_id IS NULL OR p_document_id IS NULL THEN
                RAISE EXCEPTION 'publication_rebuild_id_conflict' USING ERRCODE = '22023';
            END IF;

            PERFORM pg_advisory_xact_lock(hashtextextended('publication-rebuild', 0));
            PERFORM pg_advisory_xact_lock(
                hashtextextended('publication-document:' || p_document_id::text, 0)
            );

            SELECT candidate.* INTO v_document_grant
              FROM audit.document_publication_grants AS candidate
              JOIN audit.document_publication_manifests AS manifest
                ON manifest.grant_id = candidate.id
              JOIN core.document_versions AS version
                ON version.id = candidate.document_version_id
             WHERE manifest.document_id = p_document_id
               AND candidate.grant_status = 'active'::audit.grant_status
             ORDER BY version.version_no DESC, candidate.revision_no DESC, candidate.id DESC
             LIMIT 1
             FOR UPDATE OF candidate;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'publication_rebuild_mismatch' USING ERRCODE = '22023';
            END IF;

            SELECT * INTO manifest_row
              FROM audit.document_publication_manifests
             WHERE grant_id = v_document_grant.id;
            SELECT event.* INTO event_row
              FROM ops.outbox_events AS event
             WHERE event.aggregate_type = 'document_publication_grants'
               AND event.aggregate_id = v_document_grant.id
               AND event.event_type = 'publication.granted'
               AND event.published_at IS NOT NULL
               AND event.terminal_at IS NULL
             ORDER BY event.occurred_at ASC, event.id ASC
             LIMIT 1;

            IF NOT FOUND
               OR NOT ops._v133_document_grant_was_published(
                   v_document_grant.id, v_document_grant.document_version_id
               )
               OR EXISTS (
                   SELECT 1
                     FROM audit.publication_quarantine AS quarantine
                    WHERE quarantine.grant_table = 'document_publication_grants'
                      AND quarantine.grant_id = v_document_grant.id
                      AND quarantine.resolves_quarantine_id IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM audit.publication_quarantine AS resolution
                           WHERE resolution.resolves_quarantine_id = quarantine.id
                      )
               )
            THEN
                RAISE EXCEPTION 'publication_rebuild_mismatch' USING ERRCODE = '22023';
            END IF;

            SELECT audit._payload_sha256(jsonb_build_object(
                'scope', 'document',
                'document_id', lower(p_document_id::text),
                'document_version_id', lower(v_document_grant.document_version_id::text),
                'grant_id', lower(v_document_grant.id::text),
                'revision_no', v_document_grant.revision_no,
                'editorial_revision_id', lower(v_document_grant.editorial_revision_id::text),
                'editorial_revision_no', v_document_grant.editorial_revision_no,
                'manifest_sha256', btrim(manifest_row.manifest_sha256::text),
                'published_event_id', lower(event_row.id::text),
                'claims', coalesce((
                    SELECT jsonb_agg(jsonb_build_array(
                        lower(claim_grant.id::text),
                        claim_grant.revision_no,
                        btrim(claim_manifest.manifest_sha256::text)
                    ) ORDER BY claim_grant.id)
                      FROM audit.claim_publication_grants AS claim_grant
                      JOIN audit.claim_publication_manifests AS claim_manifest
                        ON claim_manifest.grant_id = claim_grant.id
                     WHERE claim_grant.grant_status = 'active'::audit.grant_status
                       AND claim_manifest.document_id = p_document_id
                       AND EXISTS (
                           SELECT 1 FROM ops.outbox_events AS claim_event
                            WHERE claim_event.aggregate_type = 'claim_publication_grants'
                              AND claim_event.aggregate_id = claim_grant.id
                              AND claim_event.event_type = 'publication.granted'
                              AND claim_event.published_at IS NOT NULL
                              AND claim_event.terminal_at IS NULL
                       )
                ), '[]'::jsonb)
            )) INTO v_input;

            SELECT * INTO run_row FROM audit.publication_rebuild_runs
             WHERE rebuild_id = p_rebuild_id FOR UPDATE;
            IF FOUND THEN
                IF run_row.input_digest IS DISTINCT FROM v_input THEN
                    RAISE EXCEPTION 'publication_rebuild_id_conflict' USING ERRCODE = '40001';
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
                SELECT identity.public_id, identity.slug
                  INTO v_document_public_id, v_slug
                  FROM audit.document_public_identities AS identity
                 WHERE identity.document_id = p_document_id;
                IF v_document_public_id IS NULL THEN
                    v_document_public_id := gen_random_uuid();
                    v_slug := 'd-' || replace(v_document_public_id::text, '-', '');
                    INSERT INTO audit.document_public_identities (
                        document_id, public_id, slug
                    ) VALUES (p_document_id, v_document_public_id, v_slug);
                END IF;

                -- Clear only this document's projection.  Stable identities,
                -- grants, manifests and history remain untouched.
                DELETE FROM public.document_entities WHERE document_id = v_document_public_id;
                DELETE FROM public.claim_evidence AS link
                 USING public.claims AS claim
                 WHERE link.claim_id = claim.id AND claim.document_id = v_document_public_id;
                DELETE FROM public.claims WHERE document_id = v_document_public_id;
                DELETE FROM public.evidence WHERE document_id = v_document_public_id;
                DELETE FROM public.search_documents WHERE document_id = v_document_public_id;
                DELETE FROM public.documents WHERE id = v_document_public_id;

                INSERT INTO public.documents (
                    id, document_grant_id, slug, title, summary, category,
                    fact_status, source_name, canonical_source_url,
                    source_published_at, published_at, revised_at, revision_no
                ) VALUES (
                    v_document_public_id, v_document_grant.id, v_slug, manifest_row.title,
                    manifest_row.summary, manifest_row.category,
                    manifest_row.fact_status, manifest_row.source_name,
                    manifest_row.canonical_source_url,
                    manifest_row.source_published_at, event_row.published_at,
                    NULL, v_document_grant.revision_no
                );

                -- Restore only already-published canonical entities referenced
                -- by this document's already-published claim manifests.
                FOR v_entity IN
                    SELECT DISTINCT ON (entity_manifest.entity_id)
                           entity_grant.id AS grant_id, entity_grant.revision_no,
                           entity_event.published_at AS entity_published_at,
                           entity_manifest.*
                      FROM audit.entity_publication_grants AS entity_grant
                      JOIN audit.entity_publication_manifests AS entity_manifest
                        ON entity_manifest.grant_id = entity_grant.id
                      JOIN audit.claim_publication_manifests AS claim_manifest
                        ON claim_manifest.subject_entity_id = entity_manifest.entity_id
                       AND claim_manifest.document_id = p_document_id
                      JOIN core.entities AS entity
                        ON entity.id = entity_manifest.entity_id
                      JOIN ops.outbox_events AS entity_event
                        ON entity_event.aggregate_type = 'entity_publication_grants'
                       AND entity_event.aggregate_id = entity_grant.id
                       AND entity_event.event_type = 'publication.granted'
                       AND entity_event.published_at IS NOT NULL
                       AND entity_event.terminal_at IS NULL
                     WHERE entity_grant.grant_status = 'active'::audit.grant_status
                       AND entity.status = 'active'::core.entity_status
                       AND core.canonical_entity_id(entity.id) = entity.id
                     ORDER BY entity_manifest.entity_id, entity_grant.revision_no DESC,
                              entity_grant.id DESC
                LOOP
                    SELECT identity.public_id, identity.slug
                      INTO v_public_id, v_slug
                      FROM audit.entity_public_identities AS identity
                     WHERE identity.entity_id = v_entity.entity_id;
                    IF v_public_id IS NULL THEN
                        v_public_id := gen_random_uuid();
                        v_slug := 'e-' || replace(v_public_id::text, '-', '');
                        INSERT INTO audit.entity_public_identities (
                            entity_id, public_id, slug
                        ) VALUES (v_entity.entity_id, v_public_id, v_slug);
                    END IF;
                    INSERT INTO public.entities (
                        id, entity_grant_id, slug, entity_type, name, description,
                        country_code, published_at, revision_no
                    ) VALUES (
                        v_public_id, v_entity.grant_id, v_slug, v_entity.entity_type::text,
                        v_entity.canonical_name, v_entity.description,
                        v_entity.country_code, v_entity.entity_published_at, v_entity.revision_no
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        entity_grant_id = EXCLUDED.entity_grant_id,
                        slug = EXCLUDED.slug,
                        entity_type = EXCLUDED.entity_type,
                        name = EXCLUDED.name,
                        description = EXCLUDED.description,
                        country_code = EXCLUDED.country_code,
                        revision_no = EXCLUDED.revision_no;
                END LOOP;

                FOR v_claim IN
                    SELECT claim_grant.id AS grant_id
                      FROM audit.claim_publication_grants AS claim_grant
                      JOIN audit.claim_publication_manifests AS claim_manifest
                        ON claim_manifest.grant_id = claim_grant.id
                      JOIN ops.outbox_events AS claim_event
                        ON claim_event.aggregate_type = 'claim_publication_grants'
                       AND claim_event.aggregate_id = claim_grant.id
                       AND claim_event.event_type = 'publication.granted'
                       AND claim_event.published_at IS NOT NULL
                       AND claim_event.terminal_at IS NULL
                     WHERE claim_grant.grant_status = 'active'::audit.grant_status
                       AND claim_manifest.document_id = p_document_id
                     ORDER BY claim_manifest.claim_id
                LOOP
                    IF NOT ops._wp10_3_reconcile_claim(v_claim.grant_id) THEN
                        RAISE EXCEPTION 'publication_rebuild_mismatch' USING ERRCODE = '22023';
                    END IF;
                END LOOP;

                PERFORM ops._wp10_3_reconcile_document_entities(v_document_public_id);
                PERFORM ops._wp10_3_refresh_search(v_document_public_id);
                v_result := ops._wp10_3_projection_digest();
                SELECT jsonb_build_object(
                    'documents', (SELECT count(*) FROM public.documents WHERE id = v_document_public_id),
                    'entities', (SELECT count(*) FROM public.entities AS entity
                                  WHERE EXISTS (SELECT 1 FROM public.document_entities AS link
                                                 WHERE link.document_id = v_document_public_id
                                                   AND link.entity_id = entity.id)),
                    'claims', (SELECT count(*) FROM public.claims WHERE document_id = v_document_public_id),
                    'evidence', (SELECT count(*) FROM public.evidence WHERE document_id = v_document_public_id),
                    'claim_evidence', (SELECT count(*) FROM public.claim_evidence AS link
                                        JOIN public.claims AS claim ON claim.id = link.claim_id
                                       WHERE claim.document_id = v_document_public_id),
                    'document_entities', (SELECT count(*) FROM public.document_entities
                                           WHERE document_id = v_document_public_id),
                    'search_documents', (SELECT count(*) FROM public.search_documents
                                          WHERE document_id = v_document_public_id)
                ) INTO v_counts;
            EXCEPTION WHEN OTHERS THEN
                -- Keep a sanitized durable run outcome while the projection
                -- writes in this block remain rolled back atomically.
                UPDATE audit.publication_rebuild_runs AS run
                   SET status = 'failed', error_code = 'publication_rebuild_mismatch',
                       counts = jsonb_build_object(
                           'blocked', true, 'error_code', 'publication_rebuild_mismatch'
                       ), finished_at = clock_timestamp()
                 WHERE run.rebuild_id = p_rebuild_id;
                SELECT * INTO run_row FROM audit.publication_rebuild_runs
                 WHERE rebuild_id = p_rebuild_id;
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
        $rebuild_public_document_projection$;

        ALTER FUNCTION ops.rebuild_public_projection(uuid, uuid) OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION ops.rebuild_public_projection(uuid, uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        GRANT EXECUTE ON FUNCTION ops.rebuild_public_projection(uuid, uuid) TO uap_migrator;

        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        REVOKE EXECUTE ON FUNCTION ops.rebuild_public_projection(uuid, uuid)
            FROM uap_migrator;
        DROP FUNCTION ops.rebuild_public_projection(uuid, uuid);
        DROP FUNCTION ops._v133_document_grant_was_published(uuid, uuid);

        DROP FUNCTION audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        );
        ALTER FUNCTION audit._apply_publication_grant_v131_prepublication_guard(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) RENAME TO _apply_publication_grant;
        ALTER FUNCTION audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) FROM PUBLIC;

        RESET ROLE;
        """
    )
