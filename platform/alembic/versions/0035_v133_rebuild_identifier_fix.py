"""Qualify the frozen V1-3.3 rebuild-run lookup columns.

0034 is part of the migration history and cannot be edited.  Its
``ops.rebuild_public_projection(uuid, uuid)`` body has two lookups whose
``rebuild_id`` column is ambiguous with the function's ``RETURNS TABLE``
output variable.  This forward migration replaces only those two identifier
references and preserves the existing function signature, body semantics,
ACL, locks, eligibility checks, and projection behavior.
"""

from __future__ import annotations

from alembic import op

revision = "0035_v133_rebuild_identifier_fix"
down_revision = "0034_v133_postpublication_withdraw_rebuild"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        DO $v133_rebuild_identifier_fix$
        DECLARE
            v_definition text;
            v_fixed_definition text;
            v_needle text := 'WHERE rebuild_id = p_rebuild_id';
        BEGIN
            SELECT pg_get_functiondef(
                'ops.rebuild_public_projection(uuid, uuid)'::regprocedure
            ) INTO v_definition;

            IF v_definition IS NULL
               OR length(v_definition) - length(replace(v_definition, v_needle, ''))
                    <> 2 * length(v_needle)
            THEN
                RAISE EXCEPTION 'v133_rebuild_identifier_inventory_mismatch';
            END IF;

            v_fixed_definition := replace(
                replace(
                    v_definition,
                    'FROM audit.publication_rebuild_runs',
                    'FROM audit.publication_rebuild_runs AS prr'
                ),
                v_needle,
                'WHERE prr.rebuild_id = p_rebuild_id'
            );

            IF v_fixed_definition = v_definition
               OR v_fixed_definition LIKE '%WHERE rebuild_id = p_rebuild_id%'
            THEN
                RAISE EXCEPTION 'v133_rebuild_identifier_fix_not_applied';
            END IF;

            EXECUTE v_fixed_definition;
        END;
        $v133_rebuild_identifier_fix$;

        ALTER FUNCTION ops.rebuild_public_projection(uuid, uuid)
            OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION ops.rebuild_public_projection(uuid, uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        GRANT EXECUTE ON FUNCTION ops.rebuild_public_projection(uuid, uuid)
            TO uap_migrator;

        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        DO $v133_rebuild_identifier_fix_downgrade$
        DECLARE
            v_definition text;
            v_restored_definition text;
        BEGIN
            SELECT pg_get_functiondef(
                'ops.rebuild_public_projection(uuid, uuid)'::regprocedure
            ) INTO v_definition;

            v_restored_definition := replace(
                replace(
                    v_definition,
                    'FROM audit.publication_rebuild_runs AS prr',
                    'FROM audit.publication_rebuild_runs'
                ),
                'WHERE prr.rebuild_id = p_rebuild_id',
                'WHERE rebuild_id = p_rebuild_id'
            );
            EXECUTE v_restored_definition;
        END;
        $v133_rebuild_identifier_fix_downgrade$;

        ALTER FUNCTION ops.rebuild_public_projection(uuid, uuid)
            OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION ops.rebuild_public_projection(uuid, uuid)
            FROM PUBLIC, uap_api, uap_worker, uap_scheduler, uap_publisher,
                 uap_model_governance, uap_public_reader, uap_audit_reader, uap_backup;
        GRANT EXECUTE ON FUNCTION ops.rebuild_public_projection(uuid, uuid)
            TO uap_migrator;

        RESET ROLE;
        """
    )
