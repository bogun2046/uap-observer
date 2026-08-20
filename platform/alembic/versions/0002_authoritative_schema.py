"""Create the authoritative 49-table PostgreSQL model.

Revision ID: 0002_authoritative_schema
Revises: 0001_roles_and_schemas
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "0002_authoritative_schema"
down_revision = "0001_roles_and_schemas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE TYPE ingest.source_type AS ENUM ('rss','web','x','youtube','api');
        CREATE TYPE ingest.source_run_outcome AS ENUM ('succeeded','not_modified','empty','failed');
        CREATE TYPE ingest.artifact_kind AS ENUM ('html','pdf','rss_item','video','subtitle','json','binary');
        CREATE TYPE core.storage_domain AS ENUM ('raw','derived','model_io','public_assets','backup');
        CREATE TYPE core.document_kind AS ENUM ('article','report','video','post','record');
        CREATE TYPE core.extraction_outcome AS ENUM ('succeeded','failed');
        CREATE TYPE ops.model_task_type AS ENUM ('translation','summary','classification','entity_extraction','claim_extraction');
        CREATE TYPE ops.model_run_status AS ENUM ('started','succeeded','failed','invalid');
        CREATE TYPE core.validation_status AS ENUM ('valid','invalid','pending');
        CREATE TYPE core.entity_type AS ENUM ('person','organization','location','event','object','concept');
        CREATE TYPE core.entity_status AS ENUM ('active','merged','disputed','retired');
        CREATE TYPE core.candidate_status AS ENUM ('pending','resolved','rejected');
        CREATE TYPE core.claim_type AS ENUM ('observation','attribution','event','assessment','other');
        CREATE TYPE core.assertion_status AS ENUM ('reported','corroborated','disputed','unverified','false');
        CREATE TYPE core.locator_type AS ENUM ('text','html','pdf','video','audio');
        CREATE TYPE core.support_type AS ENUM ('supports','contradicts','context');
        CREATE TYPE core.relation_status AS ENUM ('reported','corroborated','disputed','retracted');
        CREATE TYPE core.tag_type AS ENUM ('topic','category','status','region');
        CREATE TYPE core.assignment_method AS ENUM ('manual','rule','model');
        CREATE TYPE ops.job_status AS ENUM ('queued','leased','running','succeeded','retry_wait','dead','cancelled');
        CREATE TYPE ops.attempt_outcome AS ENUM ('running','succeeded','retryable_failure','terminal_failure','cancelled');
        CREATE TYPE audit.principal_type AS ENUM ('person','service');
        CREATE TYPE audit.application_role AS ENUM ('viewer','reviewer','senior_reviewer','data_operator','model_manager','security_admin','platform_admin','audit_reader');
        CREATE TYPE audit.review_case_type AS ENUM ('document','claim','entity','relation');
        CREATE TYPE audit.review_status AS ENUM ('open','assigned','approved','rejected','disputed','withdrawn','closed');
        CREATE TYPE audit.review_decision AS ENUM ('approve','reject','dispute','withdraw','revise');
        CREATE TYPE audit.grant_status AS ENUM ('active','withdrawn','superseded');

        CREATE TABLE audit.principals (
            id uuid PRIMARY KEY,
            principal_type audit.principal_type NOT NULL,
            issuer text,
            subject text,
            service_name text,
            display_name text NOT NULL,
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz,
            CONSTRAINT ck_principal_identity CHECK (
                (principal_type='person' AND issuer IS NOT NULL AND subject IS NOT NULL AND service_name IS NULL)
                OR (principal_type='service' AND service_name IS NOT NULL AND issuer IS NULL AND subject IS NULL)
            ),
            CONSTRAINT uq_principal_person UNIQUE (issuer, subject),
            CONSTRAINT uq_principal_service UNIQUE (service_name)
        );

        CREATE TABLE ops.jobs (
            id uuid PRIMARY KEY,
            job_type text NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            payload_schema_version text NOT NULL,
            idempotency_key text NOT NULL,
            status ops.job_status NOT NULL DEFAULT 'queued',
            priority smallint NOT NULL DEFAULT 0,
            available_at timestamptz NOT NULL DEFAULT now(),
            lease_owner text,
            lease_expires_at timestamptz,
            attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            max_attempts integer NOT NULL CHECK (max_attempts > 0),
            timeout_seconds integer NOT NULL CHECK (timeout_seconds > 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz,
            CONSTRAINT uq_jobs_idempotency UNIQUE (idempotency_key),
            CONSTRAINT ck_job_lease CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
        );
        CREATE INDEX ix_jobs_claim ON ops.jobs (status, available_at, priority DESC);

        CREATE TABLE ingest.sources (
            id uuid PRIMARY KEY,
            slug text NOT NULL UNIQUE,
            name text NOT NULL,
            source_type ingest.source_type NOT NULL,
            homepage_url text NOT NULL,
            feed_url text,
            country_code char(2),
            language_code text,
            enabled boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX uq_sources_feed_url ON ingest.sources (feed_url) WHERE feed_url IS NOT NULL;

        CREATE TABLE ingest.source_config_versions (
            id uuid PRIMARY KEY,
            source_id uuid NOT NULL REFERENCES ingest.sources(id),
            version_no integer NOT NULL CHECK (version_no > 0),
            configuration jsonb NOT NULL,
            configuration_sha256 char(64) NOT NULL CHECK (configuration_sha256 ~ '^[0-9a-f]{64}$'),
            effective_from timestamptz NOT NULL,
            effective_to timestamptz,
            changed_by uuid NOT NULL REFERENCES audit.principals(id),
            change_reason text NOT NULL,
            UNIQUE (source_id, version_no),
            CHECK (effective_to IS NULL OR effective_to > effective_from)
        );
        CREATE UNIQUE INDEX uq_source_config_current ON ingest.source_config_versions(source_id) WHERE effective_to IS NULL;

        CREATE TABLE ingest.source_runs (
            id uuid PRIMARY KEY,
            source_id uuid NOT NULL REFERENCES ingest.sources(id),
            job_id uuid NOT NULL UNIQUE REFERENCES ops.jobs(id),
            run_key text NOT NULL UNIQUE,
            outcome ingest.source_run_outcome NOT NULL,
            http_status smallint CHECK (http_status BETWEEN 100 AND 599),
            fetched_count integer NOT NULL DEFAULT 0 CHECK (fetched_count >= 0),
            parsed_count integer NOT NULL DEFAULT 0 CHECK (parsed_count >= 0),
            persisted_count integer NOT NULL DEFAULT 0 CHECK (persisted_count >= 0),
            duplicate_count integer NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0),
            filtered_count integer NOT NULL DEFAULT 0 CHECK (filtered_count >= 0),
            invalid_count integer NOT NULL DEFAULT 0 CHECK (invalid_count >= 0),
            etag text,
            last_modified text,
            error_code text,
            error_summary text,
            started_at timestamptz NOT NULL,
            finished_at timestamptz,
            CHECK (finished_at IS NULL OR finished_at >= started_at)
        );

        CREATE TABLE ingest.artifacts (
            id uuid PRIMARY KEY,
            source_id uuid NOT NULL REFERENCES ingest.sources(id),
            canonical_locator text NOT NULL,
            artifact_kind ingest.artifact_kind NOT NULL,
            first_seen_at timestamptz NOT NULL,
            last_seen_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (source_id, canonical_locator),
            CHECK (last_seen_at >= first_seen_at)
        );

        CREATE TABLE core.stored_objects (
            id uuid PRIMARY KEY,
            storage_domain core.storage_domain NOT NULL,
            bucket_name text NOT NULL,
            object_key text NOT NULL UNIQUE,
            content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
            byte_length bigint NOT NULL CHECK (byte_length >= 0),
            media_type text NOT NULL,
            encryption_key_ref text,
            verified_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (storage_domain, content_sha256),
            UNIQUE (id, storage_domain),
            UNIQUE (id, storage_domain, content_sha256)
        );

        CREATE TABLE ingest.artifact_versions (
            id uuid PRIMARY KEY,
            artifact_id uuid NOT NULL REFERENCES ingest.artifacts(id),
            source_run_id uuid NOT NULL REFERENCES ingest.source_runs(id),
            stored_object_id uuid NOT NULL,
            storage_domain core.storage_domain NOT NULL DEFAULT 'raw' CHECK (storage_domain='raw'),
            http_status smallint CHECK (http_status BETWEEN 100 AND 599),
            response_headers jsonb NOT NULL DEFAULT '{}'::jsonb,
            retrieved_at timestamptz NOT NULL,
            source_published_at timestamptz,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (artifact_id, stored_object_id),
            FOREIGN KEY (stored_object_id, storage_domain) REFERENCES core.stored_objects(id, storage_domain)
        );

        CREATE TABLE ingest.artifact_metrics (
            id uuid PRIMARY KEY,
            artifact_id uuid NOT NULL REFERENCES ingest.artifacts(id),
            source_run_id uuid NOT NULL REFERENCES ingest.source_runs(id),
            metric_name text NOT NULL,
            metric_value bigint NOT NULL,
            captured_at timestamptz NOT NULL,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (artifact_id, captured_at, metric_name)
        );

        CREATE TABLE core.documents (
            id uuid PRIMARY KEY,
            source_id uuid NOT NULL REFERENCES ingest.sources(id),
            source_item_key text,
            canonical_url text,
            document_kind core.document_kind NOT NULL,
            first_seen_at timestamptz NOT NULL,
            last_seen_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (canonical_url IS NOT NULL OR source_item_key IS NOT NULL),
            CHECK (last_seen_at >= first_seen_at)
        );
        CREATE UNIQUE INDEX uq_documents_url ON core.documents(canonical_url) WHERE canonical_url IS NOT NULL;
        CREATE UNIQUE INDEX uq_documents_source_item ON core.documents(source_id, source_item_key) WHERE source_item_key IS NOT NULL;

        CREATE TABLE core.document_versions (
            id uuid PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES core.documents(id),
            artifact_version_id uuid NOT NULL REFERENCES ingest.artifact_versions(id),
            version_no integer NOT NULL CHECK (version_no > 0),
            original_title text,
            source_published_at timestamptz,
            language_code text,
            normalized_content_sha256 char(64) NOT NULL CHECK (normalized_content_sha256 ~ '^[0-9a-f]{64}$'),
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (document_id, version_no),
            UNIQUE (document_id, normalized_content_sha256)
        );

        CREATE TABLE ops.job_attempts (
            id uuid PRIMARY KEY,
            job_id uuid NOT NULL REFERENCES ops.jobs(id),
            attempt_no integer NOT NULL CHECK (attempt_no > 0),
            worker_id text NOT NULL,
            started_at timestamptz NOT NULL,
            finished_at timestamptz,
            duration_ms bigint CHECK (duration_ms >= 0),
            outcome ops.attempt_outcome NOT NULL,
            http_status smallint CHECK (http_status BETWEEN 100 AND 599),
            error_class text,
            error_code text,
            error_summary text,
            retry_at timestamptz,
            metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (job_id, attempt_no),
            CHECK (finished_at IS NULL OR finished_at >= started_at)
        );

        CREATE TABLE core.extractions (
            id uuid PRIMARY KEY,
            document_version_id uuid NOT NULL REFERENCES core.document_versions(id),
            job_attempt_id uuid NOT NULL REFERENCES ops.job_attempts(id),
            extractor_name text NOT NULL,
            extractor_version text NOT NULL,
            outcome core.extraction_outcome NOT NULL,
            text_object_id uuid,
            storage_domain core.storage_domain NOT NULL DEFAULT 'derived' CHECK (storage_domain='derived'),
            output_sha256 char(64) CHECK (output_sha256 ~ '^[0-9a-f]{64}$'),
            title text,
            author text,
            language_code text,
            source_date timestamptz,
            location_map jsonb NOT NULL DEFAULT '{}'::jsonb,
            error_code text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (document_version_id, extractor_name, extractor_version, output_sha256),
            UNIQUE (id, document_version_id),
            FOREIGN KEY (text_object_id, storage_domain, output_sha256) REFERENCES core.stored_objects(id, storage_domain, content_sha256),
            CHECK ((outcome='succeeded' AND text_object_id IS NOT NULL AND output_sha256 IS NOT NULL AND error_code IS NULL)
                OR (outcome='failed' AND text_object_id IS NULL AND output_sha256 IS NULL AND error_code IS NOT NULL))
        );

        CREATE TABLE ops.prompt_versions (
            id uuid PRIMARY KEY,
            task_type ops.model_task_type NOT NULL,
            version text NOT NULL,
            system_template text NOT NULL,
            user_template text NOT NULL,
            output_schema jsonb NOT NULL,
            content_sha256 char(64) NOT NULL UNIQUE CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
            active boolean NOT NULL DEFAULT false,
            created_by uuid NOT NULL REFERENCES audit.principals(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (task_type, version)
        );

        CREATE TABLE ops.model_runs (
            id uuid PRIMARY KEY,
            job_attempt_id uuid NOT NULL REFERENCES ops.job_attempts(id),
            prompt_version_id uuid NOT NULL REFERENCES ops.prompt_versions(id),
            document_version_id uuid NOT NULL REFERENCES core.document_versions(id),
            task_type ops.model_task_type NOT NULL,
            provider text NOT NULL,
            model text NOT NULL,
            input_sha256 char(64) NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
            idempotency_key text NOT NULL UNIQUE,
            request_object_id uuid,
            response_object_id uuid,
            storage_domain core.storage_domain NOT NULL DEFAULT 'model_io' CHECK (storage_domain='model_io'),
            provider_response_id text,
            status ops.model_run_status NOT NULL,
            input_tokens integer CHECK (input_tokens >= 0),
            output_tokens integer CHECK (output_tokens >= 0),
            cost_minor_units bigint CHECK (cost_minor_units >= 0),
            currency char(3),
            error_code text,
            started_at timestamptz NOT NULL,
            finished_at timestamptz,
            UNIQUE (id, document_version_id, task_type),
            FOREIGN KEY (request_object_id, storage_domain) REFERENCES core.stored_objects(id, storage_domain),
            FOREIGN KEY (response_object_id, storage_domain) REFERENCES core.stored_objects(id, storage_domain),
            CHECK (finished_at IS NULL OR finished_at >= started_at)
        );

        CREATE TABLE core.analysis_results (
            id uuid PRIMARY KEY,
            model_run_id uuid NOT NULL,
            document_version_id uuid NOT NULL REFERENCES core.document_versions(id),
            result_type ops.model_task_type NOT NULL,
            schema_version text NOT NULL,
            result jsonb NOT NULL,
            result_sha256 char(64) NOT NULL CHECK (result_sha256 ~ '^[0-9a-f]{64}$'),
            validation_status core.validation_status NOT NULL,
            validation_errors jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (model_run_id, result_type),
            UNIQUE (id, document_version_id, result_type),
            UNIQUE (id, document_version_id),
            FOREIGN KEY (model_run_id, document_version_id, result_type) REFERENCES ops.model_runs(id, document_version_id, task_type)
        );

        CREATE TABLE core.analysis_selections (
            id uuid PRIMARY KEY,
            document_version_id uuid NOT NULL REFERENCES core.document_versions(id),
            analysis_result_id uuid NOT NULL,
            result_type ops.model_task_type NOT NULL,
            selected_by uuid NOT NULL REFERENCES audit.principals(id),
            selection_reason text NOT NULL,
            selected_at timestamptz NOT NULL DEFAULT now(),
            superseded_at timestamptz,
            FOREIGN KEY (analysis_result_id, document_version_id, result_type) REFERENCES core.analysis_results(id, document_version_id, result_type),
            CHECK (superseded_at IS NULL OR superseded_at >= selected_at)
        );
        CREATE UNIQUE INDEX uq_analysis_selection_current ON core.analysis_selections(document_version_id, result_type) WHERE superseded_at IS NULL;

        CREATE TABLE core.entities (
            id uuid PRIMARY KEY,
            entity_type core.entity_type NOT NULL,
            canonical_name text NOT NULL,
            description text,
            country_code char(2),
            identifier_namespace text,
            identifier_value text,
            status core.entity_status NOT NULL DEFAULT 'active',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK ((identifier_namespace IS NULL) = (identifier_value IS NULL))
        );
        CREATE UNIQUE INDEX uq_entity_identifier ON core.entities(identifier_namespace, identifier_value) WHERE identifier_namespace IS NOT NULL;

        CREATE TABLE core.evidence_spans (
            id uuid PRIMARY KEY,
            document_version_id uuid NOT NULL REFERENCES core.document_versions(id),
            extraction_id uuid,
            evidence_text text NOT NULL,
            locator_type core.locator_type NOT NULL,
            char_start integer,
            char_end integer,
            page_start integer,
            page_end integer,
            time_start_ms bigint,
            time_end_ms bigint,
            locator jsonb NOT NULL,
            locator_sha256 char(64) NOT NULL CHECK (locator_sha256 ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (document_version_id, locator_sha256),
            UNIQUE (id, document_version_id),
            FOREIGN KEY (extraction_id, document_version_id)
                REFERENCES core.extractions(id, document_version_id),
            CHECK (char_start IS NULL OR (char_start >= 0 AND char_end >= char_start)),
            CHECK (page_start IS NULL OR (page_start > 0 AND page_end >= page_start)),
            CHECK (time_start_ms IS NULL OR (time_start_ms >= 0 AND time_end_ms >= time_start_ms)),
            CHECK ((locator_type IN ('text','html') AND char_start IS NOT NULL AND char_end IS NOT NULL AND page_start IS NULL AND time_start_ms IS NULL)
                OR (locator_type='pdf' AND page_start IS NOT NULL AND page_end IS NOT NULL AND time_start_ms IS NULL)
                OR (locator_type IN ('video','audio') AND time_start_ms IS NOT NULL AND time_end_ms IS NOT NULL AND page_start IS NULL))
        );

        CREATE TABLE core.entity_candidates (
            id uuid PRIMARY KEY,
            analysis_result_id uuid NOT NULL,
            document_version_id uuid NOT NULL REFERENCES core.document_versions(id),
            evidence_span_id uuid,
            resolved_entity_id uuid REFERENCES core.entities(id),
            ordinal integer NOT NULL CHECK (ordinal >= 0),
            result_type ops.model_task_type NOT NULL DEFAULT 'entity_extraction' CHECK (result_type='entity_extraction'),
            proposed_entity_type core.entity_type NOT NULL,
            proposed_name text NOT NULL,
            proposed_aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
            candidate_payload jsonb NOT NULL,
            status core.candidate_status NOT NULL DEFAULT 'pending',
            resolved_at timestamptz,
            resolved_by uuid REFERENCES audit.principals(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (analysis_result_id, ordinal),
            FOREIGN KEY (analysis_result_id, document_version_id, result_type) REFERENCES core.analysis_results(id, document_version_id, result_type),
            FOREIGN KEY (evidence_span_id, document_version_id) REFERENCES core.evidence_spans(id, document_version_id),
            CHECK ((status='resolved' AND resolved_entity_id IS NOT NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)
                OR (status<>'resolved' AND resolved_entity_id IS NULL AND resolved_by IS NULL AND resolved_at IS NULL))
        );

        CREATE TABLE core.entity_aliases (
            id uuid PRIMARY KEY,
            entity_id uuid NOT NULL REFERENCES core.entities(id),
            alias text NOT NULL,
            normalized_alias text NOT NULL,
            locale text NOT NULL,
            source_document_version_id uuid REFERENCES core.document_versions(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (entity_id, normalized_alias, locale)
        );

        CREATE TABLE core.entity_merge_events (
            id uuid PRIMARY KEY,
            source_entity_id uuid NOT NULL REFERENCES core.entities(id),
            target_entity_id uuid NOT NULL REFERENCES core.entities(id),
            reason text NOT NULL,
            merged_by uuid NOT NULL REFERENCES audit.principals(id),
            merged_at timestamptz NOT NULL,
            reversed_by_id uuid UNIQUE REFERENCES core.entity_merge_events(id),
            reversed_at timestamptz,
            CHECK (source_entity_id <> target_entity_id),
            CHECK ((reversed_by_id IS NULL) = (reversed_at IS NULL))
        );

        CREATE TABLE core.claims (
            id uuid PRIMARY KEY,
            origin_analysis_result_id uuid REFERENCES core.analysis_results(id),
            subject_entity_id uuid REFERENCES core.entities(id),
            ordinal integer,
            claim_text text NOT NULL,
            claim_fingerprint char(64) NOT NULL CHECK (claim_fingerprint ~ '^[0-9a-f]{64}$'),
            claim_type core.claim_type NOT NULL,
            assertion_status core.assertion_status NOT NULL,
            attribution text,
            created_by uuid REFERENCES audit.principals(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK ((origin_analysis_result_id IS NULL) = (ordinal IS NULL))
        );
        CREATE UNIQUE INDEX uq_claim_analysis_ordinal ON core.claims(origin_analysis_result_id, ordinal) WHERE origin_analysis_result_id IS NOT NULL;

        CREATE TABLE core.claim_evidence (
            id uuid PRIMARY KEY,
            claim_id uuid NOT NULL REFERENCES core.claims(id),
            evidence_span_id uuid NOT NULL REFERENCES core.evidence_spans(id),
            support_type core.support_type NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (claim_id, evidence_span_id)
        );

        CREATE TABLE core.relations (
            id uuid PRIMARY KEY,
            subject_entity_id uuid NOT NULL REFERENCES core.entities(id),
            object_entity_id uuid NOT NULL REFERENCES core.entities(id),
            origin_analysis_result_id uuid REFERENCES core.analysis_results(id),
            predicate text NOT NULL,
            relation_status core.relation_status NOT NULL,
            confidence numeric(4,3) CHECK (confidence BETWEEN 0 AND 1),
            created_by uuid REFERENCES audit.principals(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE NULLS NOT DISTINCT (subject_entity_id, predicate, object_entity_id, origin_analysis_result_id),
            CHECK (subject_entity_id <> object_entity_id)
        );

        CREATE TABLE core.relation_evidence (
            id uuid PRIMARY KEY,
            relation_id uuid NOT NULL REFERENCES core.relations(id),
            evidence_span_id uuid NOT NULL REFERENCES core.evidence_spans(id),
            UNIQUE (relation_id, evidence_span_id)
        );

        CREATE TABLE core.tags (
            id uuid PRIMARY KEY,
            parent_id uuid REFERENCES core.tags(id),
            name text NOT NULL,
            slug text NOT NULL UNIQUE,
            tag_type core.tag_type NOT NULL,
            description text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK (parent_id IS NULL OR parent_id <> id)
        );

        CREATE TABLE core.document_tags (
            id uuid PRIMARY KEY,
            tag_id uuid NOT NULL REFERENCES core.tags(id),
            document_version_id uuid NOT NULL REFERENCES core.document_versions(id),
            method core.assignment_method NOT NULL,
            confidence numeric(4,3) CHECK (confidence BETWEEN 0 AND 1),
            origin_analysis_result_id uuid REFERENCES core.analysis_results(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tag_id, document_version_id)
        );
        CREATE TABLE core.entity_tags (
            id uuid PRIMARY KEY,
            tag_id uuid NOT NULL REFERENCES core.tags(id),
            entity_id uuid NOT NULL REFERENCES core.entities(id),
            method core.assignment_method NOT NULL,
            confidence numeric(4,3) CHECK (confidence BETWEEN 0 AND 1),
            origin_analysis_result_id uuid REFERENCES core.analysis_results(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tag_id, entity_id)
        );
        CREATE TABLE core.claim_tags (
            id uuid PRIMARY KEY,
            tag_id uuid NOT NULL REFERENCES core.tags(id),
            claim_id uuid NOT NULL REFERENCES core.claims(id),
            method core.assignment_method NOT NULL,
            confidence numeric(4,3) CHECK (confidence BETWEEN 0 AND 1),
            origin_analysis_result_id uuid REFERENCES core.analysis_results(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tag_id, claim_id)
        );

        CREATE TABLE ops.dead_letters (
            id uuid PRIMARY KEY,
            job_id uuid NOT NULL UNIQUE REFERENCES ops.jobs(id),
            last_attempt_id uuid NOT NULL REFERENCES ops.job_attempts(id),
            reason_code text NOT NULL,
            payload_snapshot jsonb NOT NULL,
            dead_at timestamptz NOT NULL,
            resolved_at timestamptz,
            resolution text,
            CHECK ((resolved_at IS NULL) = (resolution IS NULL))
        );

        CREATE TABLE ops.outbox_events (
            id uuid PRIMARY KEY,
            causation_job_id uuid REFERENCES ops.jobs(id),
            aggregate_type text NOT NULL,
            aggregate_id uuid NOT NULL,
            event_type text NOT NULL,
            event_key text NOT NULL UNIQUE,
            payload jsonb NOT NULL,
            occurred_at timestamptz NOT NULL,
            published_at timestamptz,
            publish_attempts integer NOT NULL DEFAULT 0 CHECK (publish_attempts >= 0),
            last_error_code text
        );
        CREATE INDEX ix_outbox_unpublished ON ops.outbox_events(occurred_at) WHERE published_at IS NULL;

        CREATE TABLE audit.role_bindings (
            id uuid PRIMARY KEY,
            principal_id uuid NOT NULL REFERENCES audit.principals(id),
            role audit.application_role NOT NULL,
            scope_type text NOT NULL,
            scope_id uuid,
            reason text NOT NULL,
            granted_by uuid NOT NULL REFERENCES audit.principals(id),
            granted_at timestamptz NOT NULL,
            revoked_by uuid REFERENCES audit.principals(id),
            revoked_at timestamptz,
            CHECK ((revoked_by IS NULL) = (revoked_at IS NULL))
        );
        CREATE UNIQUE INDEX uq_role_binding_active ON audit.role_bindings(principal_id, role, scope_type, scope_id) NULLS NOT DISTINCT WHERE revoked_at IS NULL;

        CREATE TABLE audit.review_cases (
            id uuid PRIMARY KEY,
            document_version_id uuid REFERENCES core.document_versions(id),
            claim_id uuid REFERENCES core.claims(id),
            entity_id uuid REFERENCES core.entities(id),
            relation_id uuid REFERENCES core.relations(id),
            case_type audit.review_case_type NOT NULL,
            status audit.review_status NOT NULL DEFAULT 'open',
            priority smallint NOT NULL DEFAULT 0,
            assigned_to uuid REFERENCES audit.principals(id),
            opened_by uuid NOT NULL REFERENCES audit.principals(id),
            opened_at timestamptz NOT NULL,
            closed_at timestamptz,
            UNIQUE (id, document_version_id),
            UNIQUE (id, claim_id),
            UNIQUE (id, entity_id),
            UNIQUE (id, relation_id),
            CHECK (num_nonnulls(document_version_id,claim_id,entity_id,relation_id)=1),
            CHECK ((case_type='document')=(document_version_id IS NOT NULL)),
            CHECK ((case_type='claim')=(claim_id IS NOT NULL)),
            CHECK ((case_type='entity')=(entity_id IS NOT NULL)),
            CHECK ((case_type='relation')=(relation_id IS NOT NULL)),
            CHECK (closed_at IS NULL OR closed_at >= opened_at)
        );
        CREATE UNIQUE INDEX uq_open_document_case ON audit.review_cases(document_version_id) WHERE document_version_id IS NOT NULL AND closed_at IS NULL;
        CREATE UNIQUE INDEX uq_open_claim_case ON audit.review_cases(claim_id) WHERE claim_id IS NOT NULL AND closed_at IS NULL;
        CREATE UNIQUE INDEX uq_open_entity_case ON audit.review_cases(entity_id) WHERE entity_id IS NOT NULL AND closed_at IS NULL;
        CREATE UNIQUE INDEX uq_open_relation_case ON audit.review_cases(relation_id) WHERE relation_id IS NOT NULL AND closed_at IS NULL;

        CREATE TABLE audit.review_decisions (
            id uuid PRIMARY KEY,
            review_case_id uuid NOT NULL REFERENCES audit.review_cases(id),
            sequence_no integer NOT NULL CHECK (sequence_no > 0),
            decision audit.review_decision NOT NULL,
            reason text NOT NULL,
            structured_changes jsonb NOT NULL DEFAULT '{}'::jsonb,
            decided_by uuid NOT NULL REFERENCES audit.principals(id),
            supersedes_decision_id uuid REFERENCES audit.review_decisions(id),
            decided_at timestamptz NOT NULL,
            UNIQUE (review_case_id, sequence_no),
            UNIQUE (id, review_case_id)
        );

        CREATE TABLE audit.document_publication_grants (
            id uuid PRIMARY KEY, review_case_id uuid NOT NULL, document_version_id uuid NOT NULL,
            decision_id uuid NOT NULL UNIQUE, revision_no integer NOT NULL CHECK (revision_no > 0),
            grant_status audit.grant_status NOT NULL, granted_at timestamptz NOT NULL,
            withdrawn_by_decision_id uuid, withdrawn_at timestamptz,
            publication_payload_sha256 char(64) NOT NULL CHECK (publication_payload_sha256 ~ '^[0-9a-f]{64}$'),
            FOREIGN KEY (review_case_id, document_version_id) REFERENCES audit.review_cases(id, document_version_id),
            FOREIGN KEY (decision_id, review_case_id) REFERENCES audit.review_decisions(id, review_case_id),
            FOREIGN KEY (withdrawn_by_decision_id, review_case_id) REFERENCES audit.review_decisions(id, review_case_id),
            CHECK ((withdrawn_by_decision_id IS NULL) = (withdrawn_at IS NULL))
        );
        CREATE UNIQUE INDEX uq_document_grant_active ON audit.document_publication_grants(document_version_id) WHERE withdrawn_at IS NULL;
        CREATE TABLE audit.claim_publication_grants (
            id uuid PRIMARY KEY, review_case_id uuid NOT NULL, claim_id uuid NOT NULL,
            decision_id uuid NOT NULL UNIQUE, revision_no integer NOT NULL CHECK (revision_no > 0),
            grant_status audit.grant_status NOT NULL, granted_at timestamptz NOT NULL,
            withdrawn_by_decision_id uuid, withdrawn_at timestamptz,
            publication_payload_sha256 char(64) NOT NULL CHECK (publication_payload_sha256 ~ '^[0-9a-f]{64}$'),
            FOREIGN KEY (review_case_id, claim_id) REFERENCES audit.review_cases(id, claim_id),
            FOREIGN KEY (decision_id, review_case_id) REFERENCES audit.review_decisions(id, review_case_id),
            FOREIGN KEY (withdrawn_by_decision_id, review_case_id) REFERENCES audit.review_decisions(id, review_case_id),
            CHECK ((withdrawn_by_decision_id IS NULL) = (withdrawn_at IS NULL))
        );
        CREATE UNIQUE INDEX uq_claim_grant_active ON audit.claim_publication_grants(claim_id) WHERE withdrawn_at IS NULL;
        CREATE TABLE audit.entity_publication_grants (
            id uuid PRIMARY KEY, review_case_id uuid NOT NULL, entity_id uuid NOT NULL,
            decision_id uuid NOT NULL UNIQUE, revision_no integer NOT NULL CHECK (revision_no > 0),
            grant_status audit.grant_status NOT NULL, granted_at timestamptz NOT NULL,
            withdrawn_by_decision_id uuid, withdrawn_at timestamptz,
            publication_payload_sha256 char(64) NOT NULL CHECK (publication_payload_sha256 ~ '^[0-9a-f]{64}$'),
            FOREIGN KEY (review_case_id, entity_id) REFERENCES audit.review_cases(id, entity_id),
            FOREIGN KEY (decision_id, review_case_id) REFERENCES audit.review_decisions(id, review_case_id),
            FOREIGN KEY (withdrawn_by_decision_id, review_case_id) REFERENCES audit.review_decisions(id, review_case_id),
            CHECK ((withdrawn_by_decision_id IS NULL) = (withdrawn_at IS NULL))
        );
        CREATE UNIQUE INDEX uq_entity_grant_active ON audit.entity_publication_grants(entity_id) WHERE withdrawn_at IS NULL;
        CREATE TABLE audit.relation_publication_grants (
            id uuid PRIMARY KEY, review_case_id uuid NOT NULL, relation_id uuid NOT NULL,
            decision_id uuid NOT NULL UNIQUE, revision_no integer NOT NULL CHECK (revision_no > 0),
            grant_status audit.grant_status NOT NULL, granted_at timestamptz NOT NULL,
            withdrawn_by_decision_id uuid, withdrawn_at timestamptz,
            publication_payload_sha256 char(64) NOT NULL CHECK (publication_payload_sha256 ~ '^[0-9a-f]{64}$'),
            FOREIGN KEY (review_case_id, relation_id) REFERENCES audit.review_cases(id, relation_id),
            FOREIGN KEY (decision_id, review_case_id) REFERENCES audit.review_decisions(id, review_case_id),
            FOREIGN KEY (withdrawn_by_decision_id, review_case_id) REFERENCES audit.review_decisions(id, review_case_id),
            CHECK ((withdrawn_by_decision_id IS NULL) = (withdrawn_at IS NULL))
        );
        CREATE UNIQUE INDEX uq_relation_grant_active ON audit.relation_publication_grants(relation_id) WHERE withdrawn_at IS NULL;

        CREATE TABLE audit.audit_events (
            id uuid PRIMARY KEY,
            event_key text NOT NULL UNIQUE,
            actor_id uuid NOT NULL REFERENCES audit.principals(id),
            action text NOT NULL,
            target_type text NOT NULL,
            target_id uuid,
            request_id uuid,
            before_digest char(64) CHECK (before_digest ~ '^[0-9a-f]{64}$'),
            after_digest char(64) CHECK (after_digest ~ '^[0-9a-f]{64}$'),
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            occurred_at timestamptz NOT NULL
        );

        CREATE TABLE public.documents (
            id uuid PRIMARY KEY,
            document_grant_id uuid NOT NULL UNIQUE REFERENCES audit.document_publication_grants(id),
            slug text NOT NULL UNIQUE,
            title text NOT NULL,
            summary text,
            category text NOT NULL,
            fact_status text NOT NULL,
            source_name text NOT NULL,
            canonical_source_url text NOT NULL UNIQUE,
            source_published_at timestamptz,
            published_at timestamptz NOT NULL,
            revised_at timestamptz,
            revision_no integer NOT NULL CHECK (revision_no > 0)
        );
        CREATE TABLE public.claims (
            id uuid PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES public.documents(id),
            claim_grant_id uuid NOT NULL UNIQUE REFERENCES audit.claim_publication_grants(id),
            ordinal integer NOT NULL CHECK (ordinal >= 0),
            claim_text text NOT NULL,
            claim_type text NOT NULL,
            assertion_status text NOT NULL,
            attribution text,
            revision_no integer NOT NULL CHECK (revision_no > 0),
            UNIQUE (document_id, ordinal, revision_no),
            UNIQUE (id, document_id)
        );
        CREATE TABLE public.evidence (
            id uuid PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES public.documents(id),
            excerpt text NOT NULL,
            locator_type text NOT NULL,
            page_start integer,
            page_end integer,
            time_start_ms bigint,
            time_end_ms bigint,
            public_locator jsonb NOT NULL,
            locator_sha256 char(64) NOT NULL CHECK (locator_sha256 ~ '^[0-9a-f]{64}$'),
            source_url text NOT NULL,
            UNIQUE (document_id, locator_sha256),
            UNIQUE (id, document_id)
        );
        CREATE TABLE public.claim_evidence (
            id uuid PRIMARY KEY,
            claim_id uuid NOT NULL REFERENCES public.claims(id),
            evidence_id uuid NOT NULL REFERENCES public.evidence(id),
            UNIQUE (claim_id, evidence_id)
        );
        CREATE TABLE public.entities (
            id uuid PRIMARY KEY,
            entity_grant_id uuid NOT NULL UNIQUE REFERENCES audit.entity_publication_grants(id),
            slug text NOT NULL UNIQUE,
            entity_type text NOT NULL,
            name text NOT NULL,
            description text,
            country_code char(2),
            published_at timestamptz NOT NULL,
            revision_no integer NOT NULL CHECK (revision_no > 0)
        );
        CREATE TABLE public.relations (
            id uuid PRIMARY KEY,
            subject_entity_id uuid NOT NULL REFERENCES public.entities(id),
            object_entity_id uuid NOT NULL REFERENCES public.entities(id),
            relation_grant_id uuid NOT NULL UNIQUE REFERENCES audit.relation_publication_grants(id),
            predicate text NOT NULL,
            relation_status text NOT NULL,
            published_at timestamptz NOT NULL,
            revision_no integer NOT NULL CHECK (revision_no > 0),
            UNIQUE (subject_entity_id, predicate, object_entity_id, revision_no),
            CHECK (subject_entity_id <> object_entity_id)
        );
        CREATE TABLE public.relation_evidence (
            id uuid PRIMARY KEY,
            relation_id uuid NOT NULL REFERENCES public.relations(id),
            evidence_id uuid NOT NULL REFERENCES public.evidence(id),
            UNIQUE (relation_id, evidence_id)
        );
        CREATE TABLE public.document_entities (
            id uuid PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES public.documents(id),
            entity_id uuid NOT NULL REFERENCES public.entities(id),
            basis_evidence_id uuid NOT NULL,
            basis_claim_id uuid,
            basis_relation_id uuid REFERENCES public.relations(id),
            UNIQUE (document_id, entity_id),
            FOREIGN KEY (basis_evidence_id, document_id) REFERENCES public.evidence(id, document_id),
            FOREIGN KEY (basis_claim_id, document_id) REFERENCES public.claims(id, document_id),
            CHECK (basis_claim_id IS NOT NULL OR basis_relation_id IS NOT NULL)
        );
        CREATE TABLE public.search_documents (
            document_id uuid PRIMARY KEY REFERENCES public.documents(id) ON DELETE CASCADE,
            search_vector tsvector NOT NULL,
            display_text text NOT NULL,
            facets jsonb NOT NULL DEFAULT '{}'::jsonb,
            indexed_at timestamptz NOT NULL
        );
        CREATE INDEX ix_search_documents_vector ON public.search_documents USING gin(search_vector);
        CREATE INDEX ix_search_documents_facets ON public.search_documents USING gin(facets);

        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        SET ROLE uap_owner;
        DROP TABLE IF EXISTS public.search_documents, public.document_entities,
            public.relation_evidence, public.relations, public.entities,
            public.claim_evidence, public.evidence, public.claims, public.documents CASCADE;
        DROP SCHEMA audit CASCADE;
        DROP SCHEMA ops CASCADE;
        DROP SCHEMA core CASCADE;
        DROP SCHEMA ingest CASCADE;
        CREATE SCHEMA ingest AUTHORIZATION uap_owner;
        CREATE SCHEMA core AUTHORIZATION uap_owner;
        CREATE SCHEMA ops AUTHORIZATION uap_owner;
        CREATE SCHEMA audit AUTHORIZATION uap_owner;
        RESET ROLE;
        """
    )
