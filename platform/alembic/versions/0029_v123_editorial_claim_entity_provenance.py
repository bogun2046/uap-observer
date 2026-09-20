"""V1-2.3 editorial claim/entity evidence ownership and materialisation.

Editorial content stores only references to the canonical ``core.evidence_spans``
table.  This migration adds the narrow API-role helper used by editorial
adoption; it deliberately does not create a second provenance store or alter
the frozen model schemas.
"""

from __future__ import annotations

from alembic import op

revision = "0029_v123_editorial_claim_entity_provenance"
down_revision = "0028_v12_editorial_revision_restore"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION core.validate_editorial_evidence_ownership()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = core, pg_catalog
        AS $validate_editorial_evidence_ownership$
        DECLARE
            item jsonb;
            v_span_id uuid;
            span_version uuid;
            span_extraction uuid;
        BEGIN
            IF NEW.content IS NULL THEN
                RETURN NEW;
            END IF;
            FOR item IN SELECT value FROM jsonb_array_elements(NEW.content -> 'claims') LOOP
                FOR v_span_id IN
                    SELECT (value #>> '{}')::uuid
                      FROM jsonb_array_elements(item -> 'evidence_span_ids')
                LOOP
                    SELECT id, document_version_id, extraction_id
                      INTO v_span_id, span_version, span_extraction
                      FROM core.evidence_spans
                     WHERE core.evidence_spans.id = v_span_id;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'editorial_evidence_not_found' USING ERRCODE = '22023';
                    END IF;
                    IF span_version IS DISTINCT FROM NEW.document_version_id THEN
                        RAISE EXCEPTION 'editorial_evidence_version_mismatch' USING ERRCODE = '22023';
                    END IF;
                    IF span_extraction IS NULL THEN
                        RAISE EXCEPTION 'editorial_evidence_extraction_mismatch' USING ERRCODE = '22023';
                    END IF;
                END LOOP;
            END LOOP;
            FOR item IN SELECT value FROM jsonb_array_elements(NEW.content -> 'entities') LOOP
                FOR v_span_id IN
                    SELECT (value #>> '{}')::uuid
                      FROM jsonb_array_elements(item -> 'evidence_span_ids')
                LOOP
                    SELECT id, document_version_id, extraction_id
                      INTO v_span_id, span_version, span_extraction
                      FROM core.evidence_spans
                     WHERE core.evidence_spans.id = v_span_id;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'editorial_evidence_not_found' USING ERRCODE = '22023';
                    END IF;
                    IF span_version IS DISTINCT FROM NEW.document_version_id THEN
                        RAISE EXCEPTION 'editorial_evidence_version_mismatch' USING ERRCODE = '22023';
                    END IF;
                    IF span_extraction IS NULL THEN
                        RAISE EXCEPTION 'editorial_evidence_extraction_mismatch' USING ERRCODE = '22023';
                    END IF;
                END LOOP;
            END LOOP;
            RETURN NEW;
        EXCEPTION
            WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'editorial_evidence_malformed_uuid' USING ERRCODE = '22023';
        END
        $validate_editorial_evidence_ownership$;

        CREATE TRIGGER editorial_revisions_evidence_ownership
            BEFORE INSERT ON core.editorial_revisions
            FOR EACH ROW EXECUTE FUNCTION core.validate_editorial_evidence_ownership();

        CREATE FUNCTION audit.materialize_editorial_evidence(
            p_document_version_id uuid,
            p_extraction_id uuid,
            p_input_sha256 text,
            p_spans jsonb
        ) RETURNS jsonb
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $materialize_editorial_evidence$
        DECLARE
            item jsonb;
            envelope jsonb;
            locator_sha text;
            existing core.evidence_spans%ROWTYPE;
            result jsonb := '[]'::jsonb;
            span_id uuid;
            expected_output text;
        BEGIN
            IF session_user IS DISTINCT FROM 'uap_api' THEN
                RAISE EXCEPTION 'editorial_session_role_denied' USING ERRCODE = '42501';
            END IF;
            PERFORM audit.require_active_role('editorial_admin'::audit.application_role);
            IF p_document_version_id IS NULL OR p_extraction_id IS NULL
               OR p_input_sha256 IS NULL OR jsonb_typeof(p_spans) <> 'array' THEN
                RAISE EXCEPTION 'editorial_evidence_request_invalid' USING ERRCODE = '22023';
            END IF;
            SELECT output_sha256 INTO expected_output
              FROM core.extractions
             WHERE id = p_extraction_id
               AND document_version_id = p_document_version_id
               AND outcome = 'succeeded';
            IF NOT FOUND OR expected_output IS DISTINCT FROM p_input_sha256 THEN
                RAISE EXCEPTION 'editorial_evidence_extraction_mismatch' USING ERRCODE = '22023';
            END IF;
            FOR item IN SELECT value FROM jsonb_array_elements(p_spans) LOOP
                IF jsonb_typeof(item) <> 'object'
                   OR jsonb_typeof(item -> 'locator') <> 'object'
                   OR item ->> 'locator_type' IS NULL
                   OR item ->> 'evidence_text' IS NULL THEN
                    RAISE EXCEPTION 'editorial_evidence_request_invalid' USING ERRCODE = '22023';
                END IF;
                envelope := item -> 'locator';
                IF envelope ->> 'document_version_id' IS DISTINCT FROM p_document_version_id::text
                   OR envelope ->> 'extraction_id' IS DISTINCT FROM p_extraction_id::text
                   OR envelope ->> 'input_sha256' IS DISTINCT FROM p_input_sha256 THEN
                    RAISE EXCEPTION 'editorial_evidence_provenance_mismatch' USING ERRCODE = '22023';
                END IF;
                locator_sha := core.compute_evidence_locator_sha256(envelope);
                SELECT * INTO existing
                  FROM core.evidence_spans
                 WHERE document_version_id = p_document_version_id
                   AND locator_sha256 = locator_sha
                 FOR UPDATE;
                IF FOUND THEN
                    IF existing.locator IS DISTINCT FROM envelope
                       OR existing.extraction_id IS DISTINCT FROM p_extraction_id
                       OR existing.evidence_text IS DISTINCT FROM item ->> 'evidence_text' THEN
                        RAISE EXCEPTION 'knowledge_locator_hash_conflict' USING ERRCODE = '22023';
                    END IF;
                    span_id := existing.id;
                ELSE
                    span_id := gen_random_uuid();
                    INSERT INTO core.evidence_spans (
                        id, document_version_id, extraction_id, evidence_text,
                        locator_type, char_start, char_end, page_start, page_end,
                        time_start_ms, time_end_ms, locator, locator_sha256
                    ) VALUES (
                        span_id, p_document_version_id, p_extraction_id,
                        item ->> 'evidence_text', (item ->> 'locator_type')::core.locator_type,
                        NULLIF(item ->> 'char_start','')::integer,
                        NULLIF(item ->> 'char_end','')::integer,
                        NULLIF(item ->> 'page_start','')::integer,
                        NULLIF(item ->> 'page_end','')::integer,
                        NULLIF(item ->> 'time_start_ms','')::bigint,
                        NULLIF(item ->> 'time_end_ms','')::bigint,
                        envelope, locator_sha
                    );
                END IF;
                result := result || jsonb_build_array(span_id::text);
            END LOOP;
            RETURN result;
        END
        $materialize_editorial_evidence$;

        GRANT EXECUTE ON FUNCTION audit.materialize_editorial_evidence(uuid, uuid, text, jsonb)
            TO uap_api;
        REVOKE ALL ON FUNCTION audit.materialize_editorial_evidence(uuid, uuid, text, jsonb)
            FROM PUBLIC, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;
        REVOKE ALL ON FUNCTION audit.materialize_editorial_evidence(uuid, uuid, text, jsonb)
            FROM PUBLIC, uap_api;
        DROP FUNCTION IF EXISTS audit.materialize_editorial_evidence(uuid, uuid, text, jsonb);
        DROP TRIGGER IF EXISTS editorial_revisions_evidence_ownership ON core.editorial_revisions;
        DROP FUNCTION IF EXISTS core.validate_editorial_evidence_ownership();
        RESET ROLE;
        """
    )
