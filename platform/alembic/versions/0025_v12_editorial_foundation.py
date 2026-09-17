"""V1-2.1A editorial revisions, lifecycle and database write contract.

The migration deliberately keeps RAW, model results, evidence and public
projection tables out of the editorial model.  Editorial writes are exposed
only through SECURITY DEFINER functions for the dedicated editorial_admin
principal role; HTTP/API wiring is a later V1-2.1B step.

Revision ID: 0025_v12_editorial_foundation
Revises: 0024_wp10_admin_replay
Create Date: 2026-09-17
"""

from __future__ import annotations

from alembic import op

revision = "0025_v12_editorial_foundation"
down_revision = "0024_wp10_admin_replay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL makes an enum value visible to later statements only after
    # the ALTER TYPE transaction commits. Keep this additive change in its
    # own Alembic autocommit block before creating functions that cast to it.
    with op.get_context().autocommit_block():
        op.execute("SET ROLE uap_owner")
        op.execute("ALTER TYPE audit.application_role ADD VALUE IF NOT EXISTS 'editorial_admin'")
        op.execute("RESET ROLE")
    op.execute(
        r"""
        SET ROLE uap_owner;

        ALTER TABLE core.documents
            ADD COLUMN IF NOT EXISTS deleted_at timestamptz,
            ADD COLUMN IF NOT EXISTS deleted_by uuid REFERENCES audit.principals(id),
            ADD COLUMN IF NOT EXISTS delete_reason text;
        ALTER TABLE core.documents
            DROP CONSTRAINT IF EXISTS ck_documents_delete_fields;
        ALTER TABLE core.documents
            ADD CONSTRAINT ck_documents_delete_fields CHECK (
                (deleted_at IS NULL AND deleted_by IS NULL)
                OR (deleted_at IS NOT NULL AND deleted_by IS NOT NULL)
            );
        CREATE INDEX IF NOT EXISTS ix_documents_active_lifecycle
            ON core.documents (last_seen_at DESC, id DESC)
            WHERE deleted_at IS NULL;
        CREATE INDEX IF NOT EXISTS ix_documents_trash_lifecycle
            ON core.documents (deleted_at DESC, id DESC)
            WHERE deleted_at IS NOT NULL;

        CREATE TABLE core.editorial_revisions (
            id uuid PRIMARY KEY,
            document_version_id uuid NOT NULL REFERENCES core.document_versions(id),
            revision_no integer NOT NULL CHECK (revision_no > 0),
            parent_revision_id uuid,
            base_revision_no integer NOT NULL CHECK (base_revision_no >= 0),
            operation text NOT NULL CHECK (
                operation IN ('save', 'adopt', 'trash', 'restore')
            ),
            content jsonb,
            source_map jsonb NOT NULL DEFAULT '{}'::jsonb,
            adopted_from jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_by uuid NOT NULL REFERENCES audit.principals(id),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (document_version_id, revision_no),
            UNIQUE (id, document_version_id),
            FOREIGN KEY (parent_revision_id, document_version_id)
                REFERENCES core.editorial_revisions(id, document_version_id),
            CHECK ((operation IN ('save', 'adopt')) = (content IS NOT NULL)),
            CHECK (jsonb_typeof(source_map) = 'object'),
            CHECK (jsonb_typeof(adopted_from) = 'object')
        );
        CREATE INDEX ix_editorial_revisions_latest
            ON core.editorial_revisions (document_version_id, revision_no DESC);
        CREATE INDEX ix_editorial_revisions_created
            ON core.editorial_revisions (created_at DESC, id DESC);

        CREATE FUNCTION core.validate_editorial_content() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = core, pg_catalog
        AS $validate_editorial_content$
        DECLARE
            v_key text;
            v_item jsonb;
            v_evidence jsonb;
            v_required text[] := ARRAY[
                'title', 'summary', 'bullets', 'category', 'labels', 'claims', 'entities'
            ];
            v_top_level text[] := ARRAY[
                'title', 'summary', 'bullets', 'category', 'labels', 'claims', 'entities'
            ];
        BEGIN
            IF NEW.content IS NULL THEN
                RETURN NEW;
            END IF;
            IF jsonb_typeof(NEW.content) <> 'object' THEN
                RAISE EXCEPTION 'editorial_content_object_required' USING ERRCODE = '22023';
            END IF;
            FOREACH v_key IN ARRAY v_required LOOP
                IF NOT (NEW.content ? v_key) THEN
                    RAISE EXCEPTION 'editorial_content_field_missing:%', v_key
                        USING ERRCODE = '22023';
                END IF;
            END LOOP;
            FOR v_key IN SELECT jsonb_object_keys(NEW.content) LOOP
                IF v_key <> ALL (v_top_level) THEN
                    RAISE EXCEPTION 'editorial_content_field_unknown:%', v_key
                        USING ERRCODE = '22023';
                END IF;
            END LOOP;

            IF jsonb_typeof(NEW.content -> 'title') <> 'string'
               OR char_length(NEW.content ->> 'title') NOT BETWEEN 1 AND 500 THEN
                RAISE EXCEPTION 'editorial_title_invalid' USING ERRCODE = '22023';
            END IF;
            IF jsonb_typeof(NEW.content -> 'summary') NOT IN ('string', 'null')
               OR (jsonb_typeof(NEW.content -> 'summary') = 'string'
                   AND char_length(NEW.content ->> 'summary') > 20000) THEN
                RAISE EXCEPTION 'editorial_summary_invalid' USING ERRCODE = '22023';
            END IF;
            IF jsonb_typeof(NEW.content -> 'category') <> 'string'
               OR NEW.content ->> 'category' NOT IN (
                    'official_report', 'government_document', 'military',
                    'scientific_research', 'historical_event', 'sighting',
                    'disputed_event', 'other'
               ) THEN
                RAISE EXCEPTION 'editorial_category_invalid' USING ERRCODE = '22023';
            END IF;

            IF jsonb_typeof(NEW.content -> 'bullets') <> 'array'
               OR jsonb_array_length(NEW.content -> 'bullets') > 20 THEN
                RAISE EXCEPTION 'editorial_bullets_invalid' USING ERRCODE = '22023';
            END IF;
            FOR v_item IN SELECT value FROM jsonb_array_elements(NEW.content -> 'bullets') LOOP
                IF jsonb_typeof(v_item) <> 'string'
                   OR char_length(v_item #>> '{}') > 2000 THEN
                    RAISE EXCEPTION 'editorial_bullet_invalid' USING ERRCODE = '22023';
                END IF;
            END LOOP;

            IF jsonb_typeof(NEW.content -> 'labels') <> 'array'
               OR jsonb_array_length(NEW.content -> 'labels') > 50 THEN
                RAISE EXCEPTION 'editorial_labels_invalid' USING ERRCODE = '22023';
            END IF;
            FOR v_item IN SELECT value FROM jsonb_array_elements(NEW.content -> 'labels') LOOP
                IF jsonb_typeof(v_item) <> 'string'
                   OR char_length(v_item #>> '{}') NOT BETWEEN 1 AND 200 THEN
                    RAISE EXCEPTION 'editorial_label_invalid' USING ERRCODE = '22023';
                END IF;
            END LOOP;

            IF jsonb_typeof(NEW.content -> 'claims') <> 'array'
               OR jsonb_array_length(NEW.content -> 'claims') > 200 THEN
                RAISE EXCEPTION 'editorial_claims_invalid' USING ERRCODE = '22023';
            END IF;
            FOR v_item IN SELECT value FROM jsonb_array_elements(NEW.content -> 'claims') LOOP
                IF jsonb_typeof(v_item) <> 'object' THEN
                    RAISE EXCEPTION 'editorial_claim_object_required' USING ERRCODE = '22023';
                END IF;
                FOR v_key IN SELECT jsonb_object_keys(v_item) LOOP
                    IF v_key <> ALL (ARRAY[
                        'claim_id', 'claim', 'source_statement', 'speaker',
                        'claim_type', 'assertion_status', 'evidence_span_ids', 'state'
                    ]) THEN
                        RAISE EXCEPTION 'editorial_claim_field_unknown:%', v_key
                            USING ERRCODE = '22023';
                    END IF;
                END LOOP;
                IF NOT (v_item ? 'claim') OR NOT (v_item ? 'source_statement')
                   OR NOT (v_item ? 'claim_type') OR NOT (v_item ? 'assertion_status')
                   OR NOT (v_item ? 'evidence_span_ids') OR NOT (v_item ? 'state')
                   OR char_length(v_item ->> 'claim') NOT BETWEEN 1 AND 10000
                   OR char_length(v_item ->> 'source_statement') NOT BETWEEN 1 AND 20000
                   OR v_item ->> 'claim_type' NOT IN (
                        'observation', 'attribution', 'event', 'assessment', 'other'
                   )
                   OR v_item ->> 'assertion_status' NOT IN (
                        'reported', 'corroborated', 'disputed', 'unverified', 'false'
                   )
                   OR v_item ->> 'state' NOT IN ('active', 'removed')
                   OR jsonb_typeof(v_item -> 'evidence_span_ids') <> 'array'
                   OR jsonb_array_length(v_item -> 'evidence_span_ids') > 20 THEN
                    RAISE EXCEPTION 'editorial_claim_invalid' USING ERRCODE = '22023';
                END IF;
                IF v_item ? 'speaker' AND jsonb_typeof(v_item -> 'speaker') NOT IN ('string', 'null') THEN
                    RAISE EXCEPTION 'editorial_claim_speaker_invalid' USING ERRCODE = '22023';
                END IF;
                IF v_item ? 'speaker' AND jsonb_typeof(v_item -> 'speaker') = 'string'
                   AND char_length(v_item ->> 'speaker') > 500 THEN
                    RAISE EXCEPTION 'editorial_claim_speaker_invalid' USING ERRCODE = '22023';
                END IF;
                FOR v_evidence IN SELECT value FROM jsonb_array_elements(v_item -> 'evidence_span_ids') LOOP
                    IF jsonb_typeof(v_evidence) <> 'string'
                       OR v_evidence #>> '{}' !~ '^[0-9a-fA-F-]{36}$' THEN
                        RAISE EXCEPTION 'editorial_claim_evidence_invalid' USING ERRCODE = '22023';
                    END IF;
                END LOOP;
            END LOOP;

            IF jsonb_typeof(NEW.content -> 'entities') <> 'array'
               OR jsonb_array_length(NEW.content -> 'entities') > 200 THEN
                RAISE EXCEPTION 'editorial_entities_invalid' USING ERRCODE = '22023';
            END IF;
            FOR v_item IN SELECT value FROM jsonb_array_elements(NEW.content -> 'entities') LOOP
                IF jsonb_typeof(v_item) <> 'object' THEN
                    RAISE EXCEPTION 'editorial_entity_object_required' USING ERRCODE = '22023';
                END IF;
                FOR v_key IN SELECT jsonb_object_keys(v_item) LOOP
                    IF v_key <> ALL (ARRAY[
                        'entity_id', 'name', 'entity_type', 'aliases',
                        'evidence_span_ids', 'state'
                    ]) THEN
                        RAISE EXCEPTION 'editorial_entity_field_unknown:%', v_key
                            USING ERRCODE = '22023';
                    END IF;
                END LOOP;
                IF NOT (v_item ? 'name') OR NOT (v_item ? 'entity_type')
                   OR NOT (v_item ? 'aliases') OR NOT (v_item ? 'evidence_span_ids')
                   OR NOT (v_item ? 'state')
                   OR char_length(v_item ->> 'name') NOT BETWEEN 1 AND 500
                   OR v_item ->> 'entity_type' NOT IN (
                        'person', 'organization', 'location', 'event', 'object', 'concept'
                   )
                   OR v_item ->> 'state' NOT IN ('active', 'removed')
                   OR jsonb_typeof(v_item -> 'aliases') <> 'array'
                   OR jsonb_array_length(v_item -> 'aliases') > 20
                   OR jsonb_typeof(v_item -> 'evidence_span_ids') <> 'array'
                   OR jsonb_array_length(v_item -> 'evidence_span_ids') > 20 THEN
                    RAISE EXCEPTION 'editorial_entity_invalid' USING ERRCODE = '22023';
                END IF;
                FOR v_key IN
                    SELECT item #>> '{}'
                      FROM jsonb_array_elements(v_item -> 'aliases') AS alias(item)
                LOOP
                    IF char_length(v_key) NOT BETWEEN 1 AND 500 THEN
                        RAISE EXCEPTION 'editorial_entity_alias_invalid' USING ERRCODE = '22023';
                    END IF;
                END LOOP;
                FOR v_evidence IN SELECT value FROM jsonb_array_elements(v_item -> 'evidence_span_ids') LOOP
                    IF jsonb_typeof(v_evidence) <> 'string'
                       OR v_evidence #>> '{}' !~ '^[0-9a-fA-F-]{36}$' THEN
                        RAISE EXCEPTION 'editorial_entity_evidence_invalid' USING ERRCODE = '22023';
                    END IF;
                END LOOP;
            END LOOP;
            RETURN NEW;
        END
        $validate_editorial_content$;

        CREATE TRIGGER editorial_revisions_validate
            BEFORE INSERT OR UPDATE ON core.editorial_revisions
            FOR EACH ROW EXECUTE FUNCTION core.validate_editorial_content();
        CREATE TRIGGER editorial_revisions_append_only
            BEFORE UPDATE OR DELETE ON core.editorial_revisions
            FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();

        CREATE FUNCTION audit._append_editorial_audit(
            p_event_key text,
            p_action text,
            p_target_id uuid,
            p_request_id uuid,
            p_before_digest text,
            p_after_digest text,
            p_metadata jsonb
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, pg_catalog
        AS $append_editorial_audit$
        DECLARE
            v_actor uuid;
            v_id uuid;
        BEGIN
            IF session_user IS DISTINCT FROM 'uap_api' THEN
                RAISE EXCEPTION 'editorial_session_role_denied' USING ERRCODE = '42501';
            END IF;
            v_actor := audit.require_active_role('editorial_admin'::audit.application_role);
            INSERT INTO audit.audit_events (
                id, event_key, actor_id, action, target_type, target_id,
                request_id, before_digest, after_digest, metadata, occurred_at
            ) VALUES (
                gen_random_uuid(), p_event_key, v_actor, p_action, 'document', p_target_id,
                p_request_id, p_before_digest, p_after_digest,
                coalesce(p_metadata, '{}'::jsonb), clock_timestamp()
            ) RETURNING id INTO v_id;
            RETURN v_id;
        END
        $append_editorial_audit$;

        CREATE FUNCTION audit._write_editorial_revision(
            p_operation text,
            p_document_version_id uuid,
            p_expected_revision integer,
            p_content jsonb,
            p_source_map jsonb,
            p_adopted_from jsonb
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $write_editorial_revision$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_document uuid;
            v_deleted_at timestamptz;
            v_current core.editorial_revisions%ROWTYPE;
            v_revision integer;
            v_id uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
        BEGIN
            IF session_user IS DISTINCT FROM 'uap_api' THEN
                RAISE EXCEPTION 'editorial_session_role_denied' USING ERRCODE = '42501';
            END IF;
            v_actor := audit.require_active_role('editorial_admin'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_operation NOT IN ('save', 'adopt') THEN
                RAISE EXCEPTION 'editorial_operation_invalid' USING ERRCODE = '22023';
            END IF;
            IF p_document_version_id IS NULL OR p_expected_revision IS NULL
               OR p_expected_revision < 0 OR p_content IS NULL THEN
                RAISE EXCEPTION 'editorial_request_invalid' USING ERRCODE = '22023';
            END IF;
            IF jsonb_typeof(coalesce(p_source_map, '{}'::jsonb)) <> 'object'
               OR jsonb_typeof(coalesce(p_adopted_from, '{}'::jsonb)) <> 'object' THEN
                RAISE EXCEPTION 'editorial_provenance_invalid' USING ERRCODE = '22023';
            END IF;
            IF p_operation = 'adopt' AND coalesce(p_adopted_from, '{}'::jsonb) = '{}'::jsonb THEN
                RAISE EXCEPTION 'editorial_adoption_source_missing' USING ERRCODE = '22023';
            END IF;

            v_payload := jsonb_build_object(
                'operation', p_operation,
                'document_version_id', lower(p_document_version_id::text),
                'expected_revision', p_expected_revision,
                'content', p_content,
                'source_map', coalesce(p_source_map, '{}'::jsonb),
                'adopted_from', coalesce(p_adopted_from, '{}'::jsonb)
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'editorial.' || p_operation || ':' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;
            PERFORM pg_advisory_xact_lock(9176, hashtext(p_document_version_id::text));
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            SELECT document.id, document.deleted_at
              INTO v_document, v_deleted_at
              FROM core.document_versions AS version
              JOIN core.documents AS document ON document.id = version.document_id
             WHERE version.id = p_document_version_id
             FOR UPDATE OF document;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'editorial_document_version_missing' USING ERRCODE = '02000';
            END IF;
            IF v_deleted_at IS NOT NULL THEN
                RAISE EXCEPTION 'editorial_document_trashed' USING ERRCODE = '22023';
            END IF;

            SELECT revision.*
              INTO v_current
              FROM core.editorial_revisions AS revision
             WHERE revision.document_version_id = p_document_version_id
             ORDER BY revision.revision_no DESC
             LIMIT 1;
            v_revision := coalesce(v_current.revision_no, 0);
            IF v_revision <> p_expected_revision THEN
                RAISE EXCEPTION 'editorial_revision_conflict' USING ERRCODE = '23505';
            END IF;

            v_id := gen_random_uuid();
            INSERT INTO core.editorial_revisions (
                id, document_version_id, revision_no, parent_revision_id,
                base_revision_no, operation, content, source_map, adopted_from,
                created_by, created_at
            ) VALUES (
                v_id, p_document_version_id, v_revision + 1,
                v_current.id, p_expected_revision, p_operation, p_content,
                coalesce(p_source_map, '{}'::jsonb), coalesce(p_adopted_from, '{}'::jsonb),
                v_actor, clock_timestamp()
            );

            PERFORM audit._append_editorial_audit(
                v_key,
                'editorial.' || p_operation,
                v_document,
                v_request,
                CASE WHEN v_current.content IS NULL THEN NULL
                     ELSE audit._payload_sha256(v_current.content) END,
                audit._payload_sha256(p_content),
                jsonb_build_object(
                    'payload_sha256', v_sha,
                    'document_version_id', p_document_version_id,
                    'revision_no', v_revision + 1,
                    'base_revision_no', p_expected_revision,
                    'source_map', coalesce(p_source_map, '{}'::jsonb),
                    'adopted_from', coalesce(p_adopted_from, '{}'::jsonb)
                )
            );
            RETURN v_id;
        END
        $write_editorial_revision$;

        CREATE FUNCTION audit.save_editorial_revision(
            p_document_version_id uuid,
            p_expected_revision integer,
            p_content jsonb,
            p_source_map jsonb,
            p_adopted_from jsonb
        ) RETURNS uuid
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $save_editorial_revision$
            SELECT audit._write_editorial_revision(
                'save', p_document_version_id, p_expected_revision,
                p_content, p_source_map, p_adopted_from
            )
        $save_editorial_revision$;

        CREATE FUNCTION audit.adopt_editorial_suggestion(
            p_document_version_id uuid,
            p_expected_revision integer,
            p_content jsonb,
            p_source_map jsonb,
            p_adopted_from jsonb
        ) RETURNS uuid
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $adopt_editorial_suggestion$
            SELECT audit._write_editorial_revision(
                'adopt', p_document_version_id, p_expected_revision,
                p_content, p_source_map, p_adopted_from
            )
        $adopt_editorial_suggestion$;

        CREATE FUNCTION audit._write_document_lifecycle(
            p_operation text,
            p_document_id uuid,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $write_document_lifecycle$
        DECLARE
            v_actor uuid;
            v_request uuid;
            v_document core.documents%ROWTYPE;
            v_version uuid;
            v_current core.editorial_revisions%ROWTYPE;
            v_revision integer;
            v_id uuid;
            v_payload jsonb;
            v_sha text;
            v_key text;
            v_existing uuid;
            v_before jsonb;
            v_after jsonb;
            v_reason text := nullif(btrim(coalesce(p_reason, '')), '');
        BEGIN
            IF session_user IS DISTINCT FROM 'uap_api' THEN
                RAISE EXCEPTION 'editorial_session_role_denied' USING ERRCODE = '42501';
            END IF;
            v_actor := audit.require_active_role('editorial_admin'::audit.application_role);
            v_request := audit._review_request_id();
            IF p_operation NOT IN ('trash', 'restore') OR p_document_id IS NULL THEN
                RAISE EXCEPTION 'editorial_lifecycle_request_invalid' USING ERRCODE = '22023';
            END IF;
            IF v_reason IS NOT NULL AND char_length(v_reason) > 5000 THEN
                RAISE EXCEPTION 'editorial_delete_reason_too_long' USING ERRCODE = '22023';
            END IF;

            v_payload := jsonb_build_object(
                'operation', p_operation,
                'document_id', lower(p_document_id::text),
                'reason', v_reason
            );
            v_sha := audit._payload_sha256(v_payload);
            v_key := 'editorial.' || p_operation || ':' || v_request::text;
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;
            PERFORM pg_advisory_xact_lock(9176, hashtext(p_document_id::text));
            v_existing := audit._existing_write_target(v_key, v_sha);
            IF v_existing IS NOT NULL THEN
                RETURN v_existing;
            END IF;

            SELECT document.* INTO v_document
              FROM core.documents AS document
             WHERE document.id = p_document_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'editorial_document_missing' USING ERRCODE = '02000';
            END IF;
            IF p_operation = 'trash' AND v_document.deleted_at IS NOT NULL THEN
                RAISE EXCEPTION 'editorial_document_already_trashed' USING ERRCODE = '22023';
            END IF;
            IF p_operation = 'restore' AND v_document.deleted_at IS NULL THEN
                RAISE EXCEPTION 'editorial_document_not_trashed' USING ERRCODE = '22023';
            END IF;

            SELECT version.id INTO v_version
              FROM core.document_versions AS version
             WHERE version.document_id = p_document_id
             ORDER BY version.version_no DESC, version.id DESC
             LIMIT 1;
            IF v_version IS NULL THEN
                RAISE EXCEPTION 'editorial_document_version_missing' USING ERRCODE = '02000';
            END IF;
            SELECT revision.* INTO v_current
              FROM core.editorial_revisions AS revision
             WHERE revision.document_version_id = v_version
             ORDER BY revision.revision_no DESC
             LIMIT 1;
            v_revision := coalesce(v_current.revision_no, 0);
            v_before := jsonb_build_object(
                'deleted_at', v_document.deleted_at,
                'deleted_by', v_document.deleted_by,
                'delete_reason', v_document.delete_reason
            );

            IF p_operation = 'trash' THEN
                UPDATE core.documents
                   SET deleted_at = clock_timestamp(), deleted_by = v_actor, delete_reason = v_reason
                 WHERE id = p_document_id;
            ELSE
                UPDATE core.documents
                   SET deleted_at = NULL, deleted_by = NULL, delete_reason = NULL
                 WHERE id = p_document_id;
            END IF;
            v_after := jsonb_build_object(
                'deleted_at', CASE WHEN p_operation = 'trash' THEN true ELSE false END,
                'delete_reason', CASE WHEN p_operation = 'trash' THEN v_reason ELSE NULL END
            );

            v_id := gen_random_uuid();
            INSERT INTO core.editorial_revisions (
                id, document_version_id, revision_no, parent_revision_id,
                base_revision_no, operation, content, source_map, adopted_from,
                created_by, created_at
            ) VALUES (
                v_id, v_version, v_revision + 1, v_current.id, v_revision,
                p_operation, NULL, '{}'::jsonb, '{}'::jsonb, v_actor, clock_timestamp()
            );
            PERFORM audit._append_editorial_audit(
                v_key,
                'editorial.' || p_operation,
                p_document_id,
                v_request,
                audit._payload_sha256(v_before),
                audit._payload_sha256(v_after),
                jsonb_build_object(
                    'payload_sha256', v_sha,
                    'document_version_id', v_version,
                    'revision_no', v_revision + 1,
                    'reason', v_reason
                )
            );
            RETURN v_id;
        END
        $write_document_lifecycle$;

        CREATE FUNCTION audit.trash_document(p_document_id uuid, p_reason text)
        RETURNS uuid
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $trash_document$
            SELECT audit._write_document_lifecycle('trash', p_document_id, p_reason)
        $trash_document$;

        CREATE FUNCTION audit.restore_document(p_document_id uuid, p_reason text)
        RETURNS uuid
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = audit, core, pg_catalog
        AS $restore_document$
            SELECT audit._write_document_lifecycle('restore', p_document_id, p_reason)
        $restore_document$;

        REVOKE ALL ON core.editorial_revisions
            FROM PUBLIC, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader;
        REVOKE INSERT, UPDATE, DELETE ON core.editorial_revisions FROM uap_api;
        GRANT SELECT ON core.editorial_revisions TO uap_api, uap_audit_reader, uap_backup;
        GRANT SELECT ON core.documents TO uap_audit_reader, uap_backup;
        REVOKE INSERT, UPDATE, DELETE ON core.documents FROM uap_api;

        REVOKE ALL ON FUNCTION audit._append_editorial_audit(
            text, text, uuid, uuid, text, text, jsonb
        ) FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
            uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION audit._write_editorial_revision(
            text, uuid, integer, jsonb, jsonb, jsonb
        ) FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
            uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION audit._write_document_lifecycle(text, uuid, text)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        GRANT EXECUTE ON FUNCTION audit.save_editorial_revision(
            uuid, integer, jsonb, jsonb, jsonb
        ) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.adopt_editorial_suggestion(
            uuid, integer, jsonb, jsonb, jsonb
        ) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.trash_document(uuid, text) TO uap_api;
        GRANT EXECUTE ON FUNCTION audit.restore_document(uuid, text) TO uap_api;

        REVOKE ALL ON FUNCTION audit.save_editorial_revision(
            uuid, integer, jsonb, jsonb, jsonb
        ) FROM PUBLIC, uap_worker, uap_scheduler, uap_publisher,
            uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION audit.adopt_editorial_suggestion(
            uuid, integer, jsonb, jsonb, jsonb
        ) FROM PUBLIC, uap_worker, uap_scheduler, uap_publisher,
            uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION audit.trash_document(uuid, text)
            FROM PUBLIC, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION audit.restore_document(uuid, text)
            FROM PUBLIC, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;
        DO $editorial_binding_guard$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM audit.role_bindings
                 WHERE role = 'editorial_admin'::audit.application_role
            ) THEN
                RAISE EXCEPTION 'editorial_admin_bindings_must_be_revoked_before_downgrade'
                    USING ERRCODE = '55000';
            END IF;
        END
        $editorial_binding_guard$;
        REVOKE ALL ON FUNCTION audit.save_editorial_revision(
            uuid, integer, jsonb, jsonb, jsonb
        ) FROM PUBLIC, uap_api;
        REVOKE ALL ON FUNCTION audit.adopt_editorial_suggestion(
            uuid, integer, jsonb, jsonb, jsonb
        ) FROM PUBLIC, uap_api;
        REVOKE ALL ON FUNCTION audit.trash_document(uuid, text) FROM PUBLIC, uap_api;
        REVOKE ALL ON FUNCTION audit.restore_document(uuid, text) FROM PUBLIC, uap_api;
        DROP FUNCTION IF EXISTS audit.trash_document(uuid, text);
        DROP FUNCTION IF EXISTS audit.restore_document(uuid, text);
        DROP FUNCTION IF EXISTS audit.adopt_editorial_suggestion(
            uuid, integer, jsonb, jsonb, jsonb
        );
        DROP FUNCTION IF EXISTS audit.save_editorial_revision(
            uuid, integer, jsonb, jsonb, jsonb
        );
        DROP FUNCTION IF EXISTS audit._write_document_lifecycle(text, uuid, text);
        DROP FUNCTION IF EXISTS audit._write_editorial_revision(
            text, uuid, integer, jsonb, jsonb, jsonb
        );
        DROP FUNCTION IF EXISTS audit._append_editorial_audit(
            text, text, uuid, uuid, text, text, jsonb
        );
        DROP TRIGGER IF EXISTS editorial_revisions_append_only ON core.editorial_revisions;
        DROP TRIGGER IF EXISTS editorial_revisions_validate ON core.editorial_revisions;
        DROP FUNCTION IF EXISTS core.validate_editorial_content();
        DROP TABLE IF EXISTS core.editorial_revisions;
        DROP INDEX IF EXISTS ix_documents_active_lifecycle;
        DROP INDEX IF EXISTS ix_documents_trash_lifecycle;
        ALTER TABLE core.documents
            DROP CONSTRAINT IF EXISTS ck_documents_delete_fields,
            DROP COLUMN IF EXISTS delete_reason,
            DROP COLUMN IF EXISTS deleted_by,
            DROP COLUMN IF EXISTS deleted_at;
        -- PostgreSQL cannot safely remove an enum label while dependent
        -- function signatures and historical role bindings may exist.  The
        -- additive label remains inert after all V1-2 objects are removed.
        RESET ROLE;
        """
    )
