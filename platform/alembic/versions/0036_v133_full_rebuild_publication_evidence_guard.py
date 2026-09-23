"""Require successful publication evidence for full projection recovery.

The legacy one-argument rebuild is a recovery operation, not a publication
path.  This forward migration filters its digest and projection inputs through
the same grant/manifest/event eligibility predicate while preserving the
function signature, lock order, and atomic projection transaction.  It does not
replace or alter the two-argument scoped rebuild contract.
"""

from __future__ import annotations

from alembic import op

revision = "0036_v133_full_rebuild_publication_evidence_guard"
down_revision = "0035_v133_rebuild_identifier_fix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION ops._v133_full_rebuild_grant_eligible(
            p_grant_table text,
            p_grant_id uuid
        ) RETURNS boolean
        LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path = ops, audit, core, public, pg_catalog
        AS $v133_full_rebuild_grant_eligible$
        DECLARE
            v_review_case_id uuid;
            v_decision_id uuid;
            v_subject_id uuid;
            v_revision_no integer;
            v_payload_sha text;
            v_editorial_revision_id uuid;
            v_editorial_revision_no integer;
            v_manifest_sha text;
        BEGIN
            IF p_grant_table IS NULL OR p_grant_table NOT IN (
                'document_publication_grants',
                'claim_publication_grants',
                'entity_publication_grants'
            ) OR p_grant_id IS NULL THEN
                RETURN false;
            END IF;

            IF p_grant_table = 'document_publication_grants' THEN
                SELECT grant_row.review_case_id, grant_row.decision_id,
                       grant_row.document_version_id, grant_row.revision_no,
                       grant_row.publication_payload_sha256::text,
                       grant_row.editorial_revision_id,
                       grant_row.editorial_revision_no,
                       manifest.manifest_sha256::text
                  INTO v_review_case_id, v_decision_id, v_subject_id,
                       v_revision_no, v_payload_sha, v_editorial_revision_id,
                       v_editorial_revision_no, v_manifest_sha
                  FROM audit.document_publication_grants AS grant_row
                  JOIN audit.document_publication_manifests AS manifest
                    ON manifest.grant_id = grant_row.id
                  JOIN audit.review_cases AS review_case
                    ON review_case.id = grant_row.review_case_id
                   AND review_case.document_version_id = grant_row.document_version_id
                   AND review_case.editorial_revision_id
                        IS NOT DISTINCT FROM grant_row.editorial_revision_id
                  LEFT JOIN core.editorial_revisions AS editorial_revision
                    ON editorial_revision.id = grant_row.editorial_revision_id
                   AND editorial_revision.document_version_id
                        = grant_row.document_version_id
                   AND editorial_revision.revision_no
                        = grant_row.editorial_revision_no
                  JOIN core.document_versions AS version
                    ON version.id = manifest.document_version_id
                   AND version.document_id = manifest.document_id
                 WHERE grant_row.id = p_grant_id
                   AND grant_row.grant_status = 'active'::audit.grant_status
                   AND manifest.review_case_id = grant_row.review_case_id
                   AND manifest.decision_id = grant_row.decision_id
                   AND manifest.document_version_id = grant_row.document_version_id
                   AND manifest.editorial_revision_id
                        IS NOT DISTINCT FROM grant_row.editorial_revision_id
                   AND manifest.editorial_revision_no
                        IS NOT DISTINCT FROM grant_row.editorial_revision_no;
            ELSIF p_grant_table = 'claim_publication_grants' THEN
                SELECT grant_row.review_case_id, grant_row.decision_id,
                       grant_row.claim_id, grant_row.revision_no,
                       grant_row.publication_payload_sha256::text,
                       manifest.manifest_sha256::text
                  INTO v_review_case_id, v_decision_id, v_subject_id,
                       v_revision_no, v_payload_sha, v_manifest_sha
                  FROM audit.claim_publication_grants AS grant_row
                  JOIN audit.claim_publication_manifests AS manifest
                    ON manifest.grant_id = grant_row.id
                  JOIN audit.review_cases AS review_case
                    ON review_case.id = grant_row.review_case_id
                   AND review_case.claim_id = grant_row.claim_id
                  JOIN core.claims AS claim
                    ON claim.id = manifest.claim_id
                   AND claim.id = grant_row.claim_id
                  JOIN core.document_versions AS version
                    ON version.id = manifest.document_version_id
                   AND version.document_id = manifest.document_id
                   AND claim.document_version_id = version.id
                 WHERE grant_row.id = p_grant_id
                   AND grant_row.grant_status = 'active'::audit.grant_status
                   AND manifest.review_case_id = grant_row.review_case_id
                   AND manifest.decision_id = grant_row.decision_id
                   AND manifest.claim_id = grant_row.claim_id;
            ELSE
                SELECT grant_row.review_case_id, grant_row.decision_id,
                       grant_row.entity_id, grant_row.revision_no,
                       grant_row.publication_payload_sha256::text,
                       manifest.manifest_sha256::text
                  INTO v_review_case_id, v_decision_id, v_subject_id,
                       v_revision_no, v_payload_sha, v_manifest_sha
                  FROM audit.entity_publication_grants AS grant_row
                  JOIN audit.entity_publication_manifests AS manifest
                    ON manifest.grant_id = grant_row.id
                  JOIN audit.review_cases AS review_case
                    ON review_case.id = grant_row.review_case_id
                   AND review_case.entity_id = grant_row.entity_id
                  JOIN core.entities AS entity
                    ON entity.id = manifest.entity_id
                   AND entity.id = grant_row.entity_id
                 WHERE grant_row.id = p_grant_id
                   AND grant_row.grant_status = 'active'::audit.grant_status
                   AND manifest.review_case_id = grant_row.review_case_id
                   AND manifest.decision_id = grant_row.decision_id
                   AND manifest.entity_id = grant_row.entity_id;
            END IF;

            IF NOT FOUND
               OR v_review_case_id IS NULL
               OR v_decision_id IS NULL
               OR v_subject_id IS NULL
               OR v_revision_no IS NULL
               OR v_payload_sha IS NULL
               OR v_manifest_sha IS NULL
               OR btrim(v_payload_sha) IS DISTINCT FROM btrim(v_manifest_sha)
               OR ops._wp10_3_grant_has_unresolved_quarantine(
                    p_grant_table, p_grant_id
               ) THEN
                RETURN false;
            END IF;

            IF p_grant_table = 'document_publication_grants' THEN
                IF (v_editorial_revision_id IS NULL)
                   <> (v_editorial_revision_no IS NULL)
                   OR (v_editorial_revision_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1
                         FROM core.editorial_revisions AS editorial_revision
                        WHERE editorial_revision.id = v_editorial_revision_id
                          AND editorial_revision.revision_no = v_editorial_revision_no
                   ))
                   OR btrim(v_manifest_sha) IS DISTINCT FROM
                      btrim(audit._publication_manifest_sha(
                          ops._wp10_3_document_manifest_payload(p_grant_id)
                      )) THEN
                    RETURN false;
                END IF;
            ELSIF p_grant_table = 'claim_publication_grants' THEN
                IF btrim(v_manifest_sha) IS DISTINCT FROM
                   btrim(audit._publication_manifest_sha(
                       ops._wp10_3_claim_manifest_payload(p_grant_id)
                   )) THEN
                    RETURN false;
                END IF;
            ELSE
                IF btrim(v_manifest_sha) IS DISTINCT FROM
                   btrim(audit._publication_manifest_sha(
                       ops._wp10_3_entity_manifest_payload(p_grant_id)
                   )) THEN
                    RETURN false;
                END IF;
            END IF;

            RETURN EXISTS (
                SELECT 1
                  FROM ops.outbox_events AS event
                 WHERE event.aggregate_type = p_grant_table
                   AND event.aggregate_id = p_grant_id
                   AND event.event_type = 'publication.granted'
                   AND event.published_at IS NOT NULL
                   AND event.terminal_at IS NULL
                   AND event.payload ->> 'grant_id' = lower(p_grant_id::text)
                   AND event.payload ->> 'decision_id' = lower(v_decision_id::text)
                   AND event.payload ->> 'subject_type' = CASE p_grant_table
                       WHEN 'document_publication_grants' THEN 'document'
                       WHEN 'claim_publication_grants' THEN 'claim'
                       ELSE 'entity'
                   END
                   AND event.payload ->> 'subject_id' = lower(v_subject_id::text)
                   AND event.payload ->> 'revision_no' = v_revision_no::text
                   AND event.payload ->> 'payload_sha256' = lower(btrim(v_payload_sha))
                   AND (
                       p_grant_table <> 'document_publication_grants'
                       OR (
                           v_editorial_revision_id IS NULL
                           AND v_editorial_revision_no IS NULL
                           AND event.payload ->> 'editorial_revision_id' IS NULL
                           AND event.payload ->> 'editorial_revision_no' IS NULL
                       )
                       OR (
                           v_editorial_revision_id IS NOT NULL
                           AND v_editorial_revision_no IS NOT NULL
                           AND event.payload ->> 'editorial_revision_id'
                               = lower(v_editorial_revision_id::text)
                           AND event.payload ->> 'editorial_revision_no'
                               = v_editorial_revision_no::text
                       )
                   )
            );
        END
        $v133_full_rebuild_grant_eligible$;

        ALTER FUNCTION ops._v133_full_rebuild_grant_eligible(text, uuid)
            OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION ops._v133_full_rebuild_grant_eligible(text, uuid)
            FROM PUBLIC, uap_migrator, uap_api, uap_worker, uap_scheduler,
                 uap_publisher, uap_model_governance, uap_public_reader,
                 uap_audit_reader, uap_backup;

        DO $v133_full_rebuild_evidence_guard$
        DECLARE
            v_definition text;
            v_fixed_definition text;
            v_table text;
            v_pattern text;
            v_replacement text;
            v_marker text;
        BEGIN
            SELECT pg_get_functiondef(
                'ops.rebuild_public_projection(uuid)'::regprocedure
            ) INTO v_definition;

            IF v_definition IS NULL THEN
                RAISE EXCEPTION 'v133_full_rebuild_function_missing';
            END IF;

            v_fixed_definition := v_definition;
            FOREACH v_table IN ARRAY ARRAY[
                'document_publication_grants',
                'entity_publication_grants',
                'claim_publication_grants'
            ] LOOP
                v_pattern := '(AND NOT ops\._wp10_3_grant_has_unresolved_quarantine\('
                    || '[[:space:]]+''' || v_table
                    || ''', grant_row\.id[[:space:]]+\))';
                v_marker := '''' || v_table || ''', grant_row.id';
                IF (
                    length(v_definition) - length(replace(v_definition, v_marker, ''))
                ) / length(v_marker) <> 2 THEN
                    RAISE EXCEPTION 'v133_full_rebuild_evidence_inventory_mismatch: %',
                        v_table;
                END IF;

                v_replacement := '\1' || chr(10)
                    || '                  AND ops._v133_full_rebuild_grant_eligible('
                    || quote_literal(v_table) || ', grant_row.id)';
                v_fixed_definition := regexp_replace(
                    v_fixed_definition, v_pattern, v_replacement, 'g'
                );
                IF (
                    length(v_fixed_definition) - length(replace(
                        v_fixed_definition,
                        'ops._v133_full_rebuild_grant_eligible(''' || v_table
                            || ''', grant_row.id)',
                        ''
                    ))
                ) / length('ops._v133_full_rebuild_grant_eligible(''' || v_table
                    || ''', grant_row.id)') <> 2 THEN
                    RAISE EXCEPTION 'v133_full_rebuild_evidence_patch_mismatch: %',
                        v_table;
                END IF;
            END LOOP;

            IF v_fixed_definition = v_definition THEN
                RAISE EXCEPTION 'v133_full_rebuild_evidence_patch_not_applied';
            END IF;
            EXECUTE v_fixed_definition;
        END;
        $v133_full_rebuild_evidence_guard$;

        ALTER FUNCTION ops.rebuild_public_projection(uuid) OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION ops.rebuild_public_projection(uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        GRANT EXECUTE ON FUNCTION ops.rebuild_public_projection(uuid) TO uap_migrator;

        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        DO $v133_full_rebuild_evidence_guard_downgrade$
        DECLARE
            v_definition text;
            v_restored_definition text;
            v_table text;
            v_pattern text;
            v_marker text;
        BEGIN
            SELECT pg_get_functiondef(
                'ops.rebuild_public_projection(uuid)'::regprocedure
            ) INTO v_definition;
            IF v_definition IS NULL THEN
                RAISE EXCEPTION 'v133_full_rebuild_function_missing';
            END IF;

            v_restored_definition := v_definition;
            FOREACH v_table IN ARRAY ARRAY[
                'document_publication_grants',
                'entity_publication_grants',
                'claim_publication_grants'
            ] LOOP
                v_pattern := '[[:space:]]+AND ops\._v133_full_rebuild_grant_eligible\('
                    || '''' || v_table || ''', grant_row\.id\)';
                v_marker := 'AND ops._v133_full_rebuild_grant_eligible('''
                    || v_table || ''', grant_row.id)';
                IF (
                    length(v_definition) - length(replace(v_definition, v_marker, ''))
                ) / length(v_marker) <> 2 THEN
                    RAISE EXCEPTION 'v133_full_rebuild_evidence_downgrade_mismatch: %',
                        v_table;
                END IF;
                v_restored_definition := regexp_replace(
                    v_restored_definition, v_pattern, '', 'g'
                );
            END LOOP;

            IF v_restored_definition LIKE '%_v133_full_rebuild_grant_eligible(%' THEN
                RAISE EXCEPTION 'v133_full_rebuild_evidence_downgrade_incomplete';
            END IF;
            EXECUTE v_restored_definition;
        END;
        $v133_full_rebuild_evidence_guard_downgrade$;

        ALTER FUNCTION ops.rebuild_public_projection(uuid) OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION ops.rebuild_public_projection(uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        GRANT EXECUTE ON FUNCTION ops.rebuild_public_projection(uuid) TO uap_migrator;

        DROP FUNCTION ops._v133_full_rebuild_grant_eligible(text, uuid);
        RESET ROLE;
        """
    )
