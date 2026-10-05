"""Secure the deferred public integrity checks without changing their bodies or ACLs.

Privileged recovery DML queues these checks until after its definer context ends.
Each check therefore needs its own owner identity and fixed object lookup path.
Frozen 0004 remains unchanged. Downgrade restores its invoker/config contract.
"""

from __future__ import annotations

from alembic import op

revision = "0037_v133_deferred_integrity_trigger_security"
down_revision = "0036_v133_full_rebuild_publication_evidence_guard"
branch_labels = None
depends_on = None

EXPECTED_BODIES = {
    "require_claim_has_evidence": r"""
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM public.claim_evidence WHERE claim_id=NEW.id) THEN
                RAISE EXCEPTION 'public claim requires at least one evidence row'
                    USING ERRCODE='23514';
            END IF;
            RETURN NULL;
        END
        """,
    "prevent_last_claim_evidence_removal": r"""
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
        """,
    "require_document_entity_revision_match": r"""
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
        """,
    "require_linked_document_entity_revision_match": r"""
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
        """,
}


def _sql(*, upgrade: bool) -> str:
    expected_security = "false" if upgrade else "true"
    target_security = "DEFINER" if upgrade else "INVOKER"
    expected_config = "NULL::text[]" if upgrade else "ARRAY['search_path=public, pg_catalog']"
    target_config = "SET search_path = public, pg_catalog" if upgrade else ""
    values = ",\n".join(
        f"('{name}', $expected_body${body}$expected_body$)"
        for name, body in EXPECTED_BODIES.items()
    )
    return f"""
        SET ROLE uap_owner;
        DO $deferred_integrity_security$
        DECLARE
            item record;
            original pg_proc%ROWTYPE;
            changed pg_proc%ROWTYPE;
            original_triggers jsonb;
            changed_triggers jsonb;
            definition text;
        BEGIN
            FOR item IN SELECT * FROM (VALUES {values}) AS targets(name, body)
            LOOP
                SELECT p.* INTO original FROM pg_proc p
                 WHERE p.oid = to_regprocedure('public.' || item.name || '()');
                IF NOT FOUND OR original.proowner <> 'uap_owner'::regrole
                   OR original.prorettype <> 'trigger'::regtype
                   OR original.pronargs <> 0 OR original.prokind <> 'f'
                   OR original.prolang <> (SELECT oid FROM pg_language WHERE lanname='plpgsql')
                   OR original.prosrc IS DISTINCT FROM item.body
                   OR original.prosecdef IS DISTINCT FROM {expected_security}
                   OR original.proconfig IS DISTINCT FROM {expected_config}
                   OR original.provolatile <> 'v' OR original.proisstrict
                   OR original.proleakproof OR original.proparallel <> 'u'
                   OR original.procost <> 100 OR original.proretset
                THEN
                    RAISE EXCEPTION 'v133_deferred_integrity_definition_mismatch: %', item.name;
                END IF;
                definition := pg_get_functiondef(original.oid);
                IF definition IS NULL THEN
                    RAISE EXCEPTION 'v133_deferred_integrity_definition_missing: %', item.name;
                END IF;
                SELECT jsonb_agg(jsonb_build_array(t.oid, pg_get_triggerdef(t.oid),
                                  t.tgdeferrable, t.tginitdeferred) ORDER BY t.oid)
                  INTO original_triggers FROM pg_trigger t WHERE t.tgfoid=original.oid;
                IF original_triggers IS NULL OR EXISTS (
                    SELECT 1 FROM pg_trigger t WHERE t.tgfoid=original.oid
                     AND (t.tgisinternal OR NOT t.tgdeferrable OR NOT t.tginitdeferred)
                ) THEN
                    RAISE EXCEPTION 'v133_deferred_integrity_trigger_mismatch: %', item.name;
                END IF;
                EXECUTE format(
                    'CREATE OR REPLACE FUNCTION public.%I() RETURNS trigger '
                    'LANGUAGE plpgsql SECURITY {target_security} {target_config} AS %L',
                    item.name, original.prosrc
                );
                SELECT p.* INTO changed FROM pg_proc p WHERE p.oid=original.oid;
                SELECT jsonb_agg(jsonb_build_array(t.oid, pg_get_triggerdef(t.oid),
                                  t.tgdeferrable, t.tginitdeferred) ORDER BY t.oid)
                  INTO changed_triggers FROM pg_trigger t WHERE t.tgfoid=original.oid;
                IF changed.proowner IS DISTINCT FROM original.proowner
                   OR changed.prosrc IS DISTINCT FROM original.prosrc
                   OR changed.proacl IS DISTINCT FROM original.proacl
                   OR changed.prosecdef IS DISTINCT FROM {str(upgrade).lower()}
                   OR changed.proconfig IS DISTINCT FROM
                      {"ARRAY['search_path=public, pg_catalog']" if upgrade else "NULL::text[]"}
                   OR changed_triggers IS DISTINCT FROM original_triggers
                THEN
                    RAISE EXCEPTION 'v133_deferred_integrity_postcondition_mismatch: %', item.name;
                END IF;
            END LOOP;
        END;
        $deferred_integrity_security$;
        RESET ROLE;
    """


def upgrade() -> None:
    op.execute(_sql(upgrade=True))


def downgrade() -> None:
    op.execute(_sql(upgrade=False))
