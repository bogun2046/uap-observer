"""Repair G3 publication and locator semantic constraints.

Revision ID: 0004_g3_semantic_repairs
Revises: 0003_permissions_and_guards
Create Date: 2026-08-13
"""

from __future__ import annotations

from alembic import op

revision = "0004_g3_semantic_repairs"
down_revision = "0003_permissions_and_guards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        DROP TRIGGER public_claim_requires_evidence ON public.claims;
        DROP TRIGGER public_claim_evidence_required ON public.claim_evidence;
        DROP FUNCTION public.require_claim_evidence();

        CREATE FUNCTION public.require_claim_has_evidence() RETURNS trigger
        LANGUAGE plpgsql AS $required_evidence$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM public.claim_evidence WHERE claim_id=NEW.id) THEN
                RAISE EXCEPTION 'public claim requires at least one evidence row'
                    USING ERRCODE='23514';
            END IF;
            RETURN NULL;
        END
        $required_evidence$;

        CREATE FUNCTION public.prevent_last_claim_evidence_removal() RETURNS trigger
        LANGUAGE plpgsql AS $required_evidence_mutation$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.claims WHERE id=OLD.claim_id)
               AND NOT EXISTS (
                   SELECT 1 FROM public.claim_evidence WHERE claim_id=OLD.claim_id
               ) THEN
                RAISE EXCEPTION 'public claim requires at least one evidence row'
                    USING ERRCODE='23514';
            END IF;
            RETURN NULL;
        END
        $required_evidence_mutation$;

        CREATE CONSTRAINT TRIGGER public_claim_requires_evidence
            AFTER INSERT OR UPDATE ON public.claims DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.require_claim_has_evidence();
        CREATE CONSTRAINT TRIGGER public_claim_evidence_required
            AFTER UPDATE OR DELETE ON public.claim_evidence DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.prevent_last_claim_evidence_removal();

        ALTER TABLE core.evidence_spans
            DROP CONSTRAINT evidence_spans_check3;
        ALTER TABLE core.evidence_spans
            ADD CONSTRAINT ck_evidence_locator_fields CHECK (
                (locator_type IN ('text','html')
                    AND char_start IS NOT NULL AND char_end IS NOT NULL
                    AND page_start IS NULL AND page_end IS NULL
                    AND time_start_ms IS NULL AND time_end_ms IS NULL)
                OR (locator_type='pdf'
                    AND page_start IS NOT NULL AND page_end IS NOT NULL
                    AND char_start IS NULL AND char_end IS NULL
                    AND time_start_ms IS NULL AND time_end_ms IS NULL)
                OR (locator_type IN ('video','audio')
                    AND time_start_ms IS NOT NULL AND time_end_ms IS NOT NULL
                    AND char_start IS NULL AND char_end IS NULL
                    AND page_start IS NULL AND page_end IS NULL)
            );

        CREATE FUNCTION public.require_document_entity_revision_match() RETURNS trigger
        LANGUAGE plpgsql AS $document_entity_revision$
        DECLARE
            revisions_mismatch boolean;
        BEGIN
            SELECT EXISTS (
                SELECT 1
                  FROM public.document_entities de
                  JOIN public.claims claim
                    ON claim.id=de.basis_claim_id AND claim.document_id=de.document_id
                  JOIN public.relations relation ON relation.id=de.basis_relation_id
                 WHERE de.id=NEW.id
                   AND claim.revision_no IS DISTINCT FROM relation.revision_no
            ) INTO revisions_mismatch;
            IF revisions_mismatch THEN
                RAISE EXCEPTION 'document entity claim and relation revisions must match'
                    USING ERRCODE='23514';
            END IF;
            RETURN NULL;
        END
        $document_entity_revision$;
        CREATE CONSTRAINT TRIGGER public_document_entity_revision
            AFTER INSERT OR UPDATE ON public.document_entities
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.require_document_entity_revision_match();

        CREATE FUNCTION public.require_linked_document_entity_revision_match() RETURNS trigger
        LANGUAGE plpgsql AS $linked_document_entity_revision$
        BEGIN
            IF TG_TABLE_NAME='claims' AND EXISTS (
                SELECT 1
                  FROM public.document_entities de
                  JOIN public.claims claim ON claim.id=de.basis_claim_id
                  JOIN public.relations relation ON relation.id=de.basis_relation_id
                 WHERE de.basis_claim_id=NEW.id
                   AND claim.revision_no IS DISTINCT FROM relation.revision_no
            ) THEN
                RAISE EXCEPTION 'document entity claim and relation revisions must match'
                    USING ERRCODE='23514';
            ELSIF TG_TABLE_NAME='relations' AND EXISTS (
                SELECT 1
                  FROM public.document_entities de
                  JOIN public.claims claim ON claim.id=de.basis_claim_id
                  JOIN public.relations relation ON relation.id=de.basis_relation_id
                 WHERE de.basis_relation_id=NEW.id
                   AND claim.revision_no IS DISTINCT FROM relation.revision_no
            ) THEN
                RAISE EXCEPTION 'document entity claim and relation revisions must match'
                    USING ERRCODE='23514';
            END IF;
            RETURN NULL;
        END
        $linked_document_entity_revision$;
        CREATE CONSTRAINT TRIGGER public_claim_document_entity_revision
            AFTER UPDATE ON public.claims DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.require_linked_document_entity_revision_match();
        CREATE CONSTRAINT TRIGGER public_relation_document_entity_revision
            AFTER UPDATE ON public.relations DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.require_linked_document_entity_revision_match();

        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        DROP TRIGGER public_relation_document_entity_revision ON public.relations;
        DROP TRIGGER public_claim_document_entity_revision ON public.claims;
        DROP TRIGGER public_document_entity_revision ON public.document_entities;
        DROP FUNCTION public.require_linked_document_entity_revision_match();
        DROP FUNCTION public.require_document_entity_revision_match();

        ALTER TABLE core.evidence_spans
            DROP CONSTRAINT ck_evidence_locator_fields;
        ALTER TABLE core.evidence_spans
            ADD CONSTRAINT evidence_spans_check3 CHECK (
                (locator_type IN ('text','html')
                    AND char_start IS NOT NULL AND char_end IS NOT NULL
                    AND page_start IS NULL AND time_start_ms IS NULL)
                OR (locator_type='pdf'
                    AND page_start IS NOT NULL AND page_end IS NOT NULL
                    AND time_start_ms IS NULL)
                OR (locator_type IN ('video','audio')
                    AND time_start_ms IS NOT NULL AND time_end_ms IS NOT NULL
                    AND page_start IS NULL)
            );

        DROP TRIGGER public_claim_requires_evidence ON public.claims;
        DROP TRIGGER public_claim_evidence_required ON public.claim_evidence;
        DROP FUNCTION public.prevent_last_claim_evidence_removal();
        DROP FUNCTION public.require_claim_has_evidence();

        CREATE FUNCTION public.require_claim_evidence() RETURNS trigger
        LANGUAGE plpgsql AS $required_evidence$
        DECLARE target_claim uuid;
        BEGIN
            target_claim := CASE WHEN TG_TABLE_NAME='claims' THEN COALESCE(NEW.id, OLD.id)
                                 ELSE COALESCE(NEW.claim_id, OLD.claim_id) END;
            IF EXISTS (SELECT 1 FROM public.claims WHERE id=target_claim)
               AND NOT EXISTS (
                   SELECT 1 FROM public.claim_evidence WHERE claim_id=target_claim
               ) THEN
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
            AFTER INSERT OR UPDATE OR DELETE ON public.claim_evidence
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.require_claim_evidence();

        RESET ROLE;
        """
    )
