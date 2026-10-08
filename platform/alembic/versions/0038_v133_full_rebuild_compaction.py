"""Use explicit TRUNCATE for quiesced administrative full projection recovery.

This migration transforms only the exact DELETE reset block in the live
0037 function definition. Every other function clause and body segment is
compared byte-for-byte with PostgreSQL's formal definition.
"""

from __future__ import annotations

from alembic import op

revision = "0038_v133_full_rebuild_compaction"
down_revision = "0037_v133_deferred_integrity_trigger_security"
branch_labels = None
depends_on = None

MAINTENANCE_COMMENT = (
    "Administrative full projection recovery only. Requires an approved "
    "maintenance window with public read traffic quiesced before the transaction "
    "starts and until commit. Uses TRUNCATE and is not safe for concurrent public reads."
)
DELETE_RESET = (
    "                DELETE FROM public.document_entities;\n"
    "                DELETE FROM public.claim_evidence;\n"
    "                DELETE FROM public.claims;\n"
    "                DELETE FROM public.evidence;\n"
    "                DELETE FROM public.search_documents;\n"
    "                DELETE FROM public.documents;\n"
    "                DELETE FROM public.entities;\n\n"
)
TRUNCATE_RESET = (
    "                LOCK TABLE public.relations, public.relation_evidence\n"
    "                    IN SHARE ROW EXCLUSIVE MODE;\n"
    "                IF EXISTS (SELECT 1 FROM public.relations)\n"
    "                   OR EXISTS (SELECT 1 FROM public.relation_evidence) THEN\n"
    "                    RAISE EXCEPTION 'publication_rebuild_mismatch'\n"
    "                        USING ERRCODE = '22023';\n"
    "                END IF;\n"
    "                TRUNCATE TABLE ONLY public.document_entities,\n"
    "                    ONLY public.claim_evidence,\n"
    "                    ONLY public.relation_evidence,\n"
    "                    ONLY public.relations,\n"
    "                    ONLY public.claims,\n"
    "                    ONLY public.evidence,\n"
    "                    ONLY public.search_documents,\n"
    "                    ONLY public.documents,\n"
    "                    ONLY public.entities CONTINUE IDENTITY RESTRICT;\n\n"
)


def _dollar_quote(value: str, tag: str) -> str:
    if tag in value:
        raise ValueError("dollar quote delimiter occurs in migration marker")
    return f"${tag}${value}${tag}$"


def _sql_literal(value: str | None, tag: str) -> str:
    if value is None:
        return "NULL"
    return _dollar_quote(value, tag)


def _sql(*, upgrade: bool) -> str:
    expected_reset = DELETE_RESET if upgrade else TRUNCATE_RESET
    replacement_reset = TRUNCATE_RESET if upgrade else DELETE_RESET
    expected_comment = None if upgrade else MAINTENANCE_COMMENT
    replacement_comment = MAINTENANCE_COMMENT if upgrade else None
    action = "upgrade" if upgrade else "downgrade"
    expected_reset_sql = _dollar_quote(expected_reset, "v133_expected_reset")
    replacement_reset_sql = _dollar_quote(replacement_reset, "v133_replacement_reset")
    expected_comment_sql = _sql_literal(expected_comment, "v133_expected_comment")
    replacement_comment_sql = _sql_literal(replacement_comment, "v133_replacement_comment")
    return f"""
        SET ROLE uap_owner;
        DO $v133_full_rebuild_compaction$
        DECLARE
            target_tables CONSTANT regclass[] := ARRAY[
                'public.document_entities'::regclass,
                'public.claim_evidence'::regclass,
                'public.relation_evidence'::regclass,
                'public.relations'::regclass,
                'public.claims'::regclass,
                'public.evidence'::regclass,
                'public.search_documents'::regclass,
                'public.documents'::regclass,
                'public.entities'::regclass
            ];
            original pg_proc%ROWTYPE;
            changed pg_proc%ROWTYPE;
            definition text;
            rewritten text;
            expected_comment text := {expected_comment_sql};
            replacement_comment text := {replacement_comment_sql};
            delete_trigger_count integer;
        BEGIN
            SELECT p.* INTO original
              FROM pg_proc AS p
             WHERE p.oid = to_regprocedure('ops.rebuild_public_projection(uuid)');
            IF NOT FOUND
               OR original.proowner <> 'uap_owner'::regrole
               OR original.prosecdef IS DISTINCT FROM true
               OR original.proconfig IS DISTINCT FROM ARRAY[
                    'search_path=ops, audit, core, public, pg_catalog',
                    'TimeZone=UTC'
               ]::text[]
               OR original.prokind <> 'f'
               OR original.prorettype <> 'record'::regtype
               OR original.pronargs <> 1
               OR original.prolang <> (SELECT oid FROM pg_language WHERE lanname='plpgsql')
               OR original.provolatile <> 'v'
               OR original.proisstrict
               OR original.proleakproof
               OR original.proparallel <> 'u'
               OR original.procost <> 100
               OR NOT original.proretset
               OR obj_description(original.oid, 'pg_proc') IS DISTINCT FROM expected_comment
            THEN
                RAISE EXCEPTION 'v133_full_rebuild_definition_mismatch: {action}';
            END IF;

            IF cardinality(target_tables) <> 9
               OR EXISTS (
                    SELECT 1 FROM unnest(target_tables) AS selected(table_oid)
                    JOIN pg_class AS relation ON relation.oid=selected.table_oid
                    WHERE relation.relkind <> 'r'
               )
               OR EXISTS (
                    SELECT 1 FROM pg_inherits AS inheritance
                    WHERE inheritance.inhparent = ANY(target_tables)
               )
               OR EXISTS (
                    SELECT 1 FROM pg_constraint AS foreign_key
                    WHERE foreign_key.contype='f'
                      AND foreign_key.confrelid = ANY(target_tables)
                      AND NOT foreign_key.conrelid = ANY(target_tables)
               )
            THEN
                RAISE EXCEPTION 'v133_full_rebuild_table_scope_mismatch';
            END IF;

            SELECT count(*) INTO delete_trigger_count
              FROM pg_trigger AS trigger_row
             WHERE trigger_row.tgrelid = ANY(target_tables)
               AND NOT trigger_row.tgisinternal
               AND (trigger_row.tgtype & 8) <> 0;
            IF delete_trigger_count <> 1
               OR EXISTS (
                    SELECT 1 FROM pg_trigger AS trigger_row
                    WHERE trigger_row.tgrelid = ANY(target_tables)
                      AND NOT trigger_row.tgisinternal
                      AND (trigger_row.tgtype & 32) <> 0
               )
               OR NOT EXISTS (
                    SELECT 1 FROM pg_trigger AS trigger_row
                    WHERE trigger_row.tgrelid = 'public.claim_evidence'::regclass
                      AND trigger_row.tgname = 'public_claim_evidence_required'
                      AND trigger_row.tgfoid =
                          'public.prevent_last_claim_evidence_removal()'::regprocedure
                      AND trigger_row.tgconstraint <> 0
                      AND trigger_row.tgdeferrable
                      AND trigger_row.tginitdeferred
                      AND (trigger_row.tgtype & 8) <> 0
               )
            THEN
                RAISE EXCEPTION 'v133_full_rebuild_trigger_inventory_mismatch';
            END IF;

            definition := pg_get_functiondef(original.oid);
            IF length(definition) - length(replace(definition, {expected_reset_sql}, ''))
                   <> length({expected_reset_sql}) THEN
                RAISE EXCEPTION 'v133_full_rebuild_reset_marker_mismatch: {action}';
            END IF;
            rewritten := replace(
                definition, {expected_reset_sql}, {replacement_reset_sql}
            );
            EXECUTE rewritten;
            EXECUTE pg_catalog.format(
                'COMMENT ON FUNCTION ops.rebuild_public_projection(uuid) IS %L',
                replacement_comment
            );

            SELECT p.* INTO changed
              FROM pg_proc AS p
             WHERE p.oid = to_regprocedure('ops.rebuild_public_projection(uuid)');
            IF changed.proowner IS DISTINCT FROM original.proowner
               OR changed.proacl IS DISTINCT FROM original.proacl
               OR changed.prosecdef IS DISTINCT FROM original.prosecdef
               OR changed.proconfig IS DISTINCT FROM original.proconfig
               OR changed.prorettype IS DISTINCT FROM original.prorettype
               OR changed.proargtypes IS DISTINCT FROM original.proargtypes
               OR changed.prokind IS DISTINCT FROM original.prokind
               OR changed.prolang IS DISTINCT FROM original.prolang
               OR changed.provolatile IS DISTINCT FROM original.provolatile
               OR changed.proisstrict IS DISTINCT FROM original.proisstrict
               OR changed.proleakproof IS DISTINCT FROM original.proleakproof
               OR changed.proparallel IS DISTINCT FROM original.proparallel
               OR changed.procost IS DISTINCT FROM original.procost
               OR changed.prorows IS DISTINCT FROM original.prorows
               OR changed.proretset IS DISTINCT FROM original.proretset
               OR pg_get_functiondef(changed.oid) IS DISTINCT FROM rewritten
               OR obj_description(changed.oid, 'pg_proc') IS DISTINCT FROM replacement_comment
            THEN
                RAISE EXCEPTION 'v133_full_rebuild_postcondition_mismatch: {action}';
            END IF;
        END;
        $v133_full_rebuild_compaction$;
        RESET ROLE;
    """


def upgrade() -> None:
    op.execute(_sql(upgrade=True))


def downgrade() -> None:
    op.execute(_sql(upgrade=False))
