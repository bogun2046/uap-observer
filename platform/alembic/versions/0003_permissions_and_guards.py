"""Apply least-privilege grants and semantic database guards.

Revision ID: 0003_permissions_and_guards
Revises: 0002_authoritative_schema
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "0003_permissions_and_guards"
down_revision = "0002_authoritative_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE FUNCTION audit.reject_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $guard$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME
                USING ERRCODE = '55000';
        END
        $guard$;

        CREATE TRIGGER artifact_versions_append_only
            BEFORE UPDATE OR DELETE ON ingest.artifact_versions
            FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();
        CREATE TRIGGER analysis_results_append_only
            BEFORE UPDATE OR DELETE ON core.analysis_results
            FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();
        CREATE TRIGGER review_decisions_append_only
            BEFORE UPDATE OR DELETE ON audit.review_decisions
            FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();
        CREATE TRIGGER audit_events_append_only
            BEFORE UPDATE OR DELETE ON audit.audit_events
            FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();

        CREATE FUNCTION audit.validate_publication_grant() RETURNS trigger
        LANGUAGE plpgsql AS $grant$
        DECLARE
            approval audit.review_decision;
            withdrawal audit.review_decision;
        BEGIN
            SELECT decision INTO approval FROM audit.review_decisions
             WHERE id=NEW.decision_id AND review_case_id=NEW.review_case_id;
            IF approval NOT IN ('approve','revise') THEN
                RAISE EXCEPTION 'publication grant requires approve or revise decision'
                    USING ERRCODE='23514';
            END IF;
            IF NEW.withdrawn_by_decision_id IS NOT NULL THEN
                SELECT decision INTO withdrawal FROM audit.review_decisions
                 WHERE id=NEW.withdrawn_by_decision_id AND review_case_id=NEW.review_case_id;
                IF withdrawal <> 'withdraw' OR NEW.withdrawn_at IS NULL OR NEW.grant_status <> 'withdrawn' THEN
                    RAISE EXCEPTION 'withdrawal requires same-case withdraw decision and withdrawn status'
                        USING ERRCODE='23514';
                END IF;
            ELSIF NEW.withdrawn_at IS NOT NULL OR NEW.grant_status='withdrawn' THEN
                RAISE EXCEPTION 'withdrawal fields must be complete' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END
        $grant$;

        CREATE TRIGGER validate_document_grant BEFORE INSERT OR UPDATE ON audit.document_publication_grants
            FOR EACH ROW EXECUTE FUNCTION audit.validate_publication_grant();
        CREATE TRIGGER validate_claim_grant BEFORE INSERT OR UPDATE ON audit.claim_publication_grants
            FOR EACH ROW EXECUTE FUNCTION audit.validate_publication_grant();
        CREATE TRIGGER validate_entity_grant BEFORE INSERT OR UPDATE ON audit.entity_publication_grants
            FOR EACH ROW EXECUTE FUNCTION audit.validate_publication_grant();
        CREATE TRIGGER validate_relation_grant BEFORE INSERT OR UPDATE ON audit.relation_publication_grants
            FOR EACH ROW EXECUTE FUNCTION audit.validate_publication_grant();

        CREATE FUNCTION public.validate_claim_evidence_document() RETURNS trigger
        LANGUAGE plpgsql AS $claim_evidence$
        DECLARE claim_document uuid; evidence_document uuid;
        BEGIN
            SELECT document_id INTO claim_document FROM public.claims WHERE id=NEW.claim_id;
            SELECT document_id INTO evidence_document FROM public.evidence WHERE id=NEW.evidence_id;
            IF claim_document IS DISTINCT FROM evidence_document THEN
                RAISE EXCEPTION 'public claim evidence must belong to the same document'
                    USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END
        $claim_evidence$;
        CREATE TRIGGER validate_claim_evidence_document
            BEFORE INSERT OR UPDATE ON public.claim_evidence
            FOR EACH ROW EXECUTE FUNCTION public.validate_claim_evidence_document();

        CREATE FUNCTION public.require_claim_evidence() RETURNS trigger
        LANGUAGE plpgsql AS $required_evidence$
        DECLARE target_claim uuid;
        BEGIN
            target_claim := CASE WHEN TG_TABLE_NAME='claims' THEN COALESCE(NEW.id, OLD.id)
                                 ELSE COALESCE(NEW.claim_id, OLD.claim_id) END;
            IF EXISTS (SELECT 1 FROM public.claims WHERE id=target_claim)
               AND NOT EXISTS (SELECT 1 FROM public.claim_evidence WHERE claim_id=target_claim) THEN
                RAISE EXCEPTION 'public claim requires at least one evidence row'
                    USING ERRCODE='23514';
            END IF;
            RETURN NULL;
        END
        $required_evidence$;
        CREATE CONSTRAINT TRIGGER public_claim_requires_evidence
            AFTER INSERT OR UPDATE ON public.claims DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.require_claim_evidence();
        CREATE CONSTRAINT TRIGGER public_claim_evidence_required
            AFTER INSERT OR UPDATE OR DELETE ON public.claim_evidence DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.require_claim_evidence();

        DO $database_grants$
        BEGIN
            EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
            EXECUTE format(
                'GRANT CONNECT ON DATABASE %I TO uap_migrator, uap_api, uap_worker, '
                'uap_scheduler, uap_publisher, uap_public_reader, uap_audit_reader, uap_backup',
                current_database()
            );
        END
        $database_grants$;
        REVOKE ALL ON SCHEMA ingest, core, ops, audit, public FROM PUBLIC;
        GRANT USAGE ON SCHEMA ingest, core, ops, audit, public TO uap_migrator;

        GRANT USAGE ON SCHEMA ingest, core, ops, audit, public TO uap_api;
        GRANT SELECT ON ALL TABLES IN SCHEMA ingest, public TO uap_api;
        GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA core, ops TO uap_api;
        GRANT SELECT ON ALL TABLES IN SCHEMA audit TO uap_api;
        GRANT INSERT ON audit.audit_events TO uap_api;

        GRANT USAGE ON SCHEMA ingest, core, ops, audit TO uap_worker;
        GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA ingest, core, ops TO uap_worker;
        GRANT INSERT ON audit.audit_events TO uap_worker;

        GRANT USAGE ON SCHEMA ingest, ops, audit TO uap_scheduler;
        GRANT SELECT ON ALL TABLES IN SCHEMA ingest TO uap_scheduler;
        GRANT SELECT, INSERT, UPDATE ON ops.jobs, ops.outbox_events TO uap_scheduler;
        GRANT INSERT ON audit.audit_events TO uap_scheduler;

        GRANT USAGE ON SCHEMA ingest, core, ops, audit, public TO uap_publisher;
        GRANT SELECT ON ALL TABLES IN SCHEMA ingest, core, audit TO uap_publisher;
        GRANT SELECT, UPDATE ON ops.jobs TO uap_publisher;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO uap_publisher;

        GRANT USAGE ON SCHEMA public TO uap_public_reader;
        GRANT SELECT ON ALL TABLES IN SCHEMA public TO uap_public_reader;

        GRANT USAGE ON SCHEMA ingest, core, ops, audit, public TO uap_audit_reader, uap_backup;
        GRANT SELECT ON ALL TABLES IN SCHEMA ingest, core, ops, audit, public TO uap_audit_reader, uap_backup;

        ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner IN SCHEMA public
            GRANT SELECT ON TABLES TO uap_public_reader;
        ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner IN SCHEMA public
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO uap_publisher;
        ALTER DEFAULT PRIVILEGES FOR ROLE uap_owner IN SCHEMA ingest, core, ops, audit, public
            GRANT SELECT ON TABLES TO uap_audit_reader, uap_backup;
        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        SET ROLE uap_owner;
        DROP FUNCTION IF EXISTS public.require_claim_evidence() CASCADE;
        DROP FUNCTION IF EXISTS public.validate_claim_evidence_document() CASCADE;
        DROP FUNCTION IF EXISTS audit.validate_publication_grant() CASCADE;
        DROP FUNCTION IF EXISTS audit.reject_mutation() CASCADE;
        REVOKE ALL ON SCHEMA ingest, core, ops, audit, public FROM uap_api, uap_worker,
            uap_scheduler, uap_publisher, uap_public_reader, uap_audit_reader, uap_backup;
        REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA ingest, core, ops, audit, public
            FROM uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_public_reader, uap_audit_reader, uap_backup;
        RESET ROLE;
        """
    )
