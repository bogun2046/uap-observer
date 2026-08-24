"""Entity merge/reverse state machine. Default closed; no runtime EXECUTE.

Revision ID: 0013_entity_merge_state_machine
Revises: 0012_entity_materialization
Create Date: 2026-08-24
"""

from __future__ import annotations

from alembic import op

revision = "0013_entity_merge_state_machine"
down_revision = "0012_entity_materialization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        ALTER TABLE core.entity_merge_events
            ADD COLUMN event_kind text NOT NULL DEFAULT 'merge',
            ADD CONSTRAINT ck_entity_merge_event_kind
                CHECK (event_kind IN ('merge', 'reverse'));

        UPDATE core.entity_merge_events
           SET event_kind = 'merge'
         WHERE event_kind IS DISTINCT FROM 'merge';

        CREATE UNIQUE INDEX uq_open_merge_source
            ON core.entity_merge_events (source_entity_id)
            WHERE event_kind = 'merge' AND reversed_at IS NULL;

        CREATE FUNCTION core.canonical_entity_id(p_entity_id uuid) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = core, pg_catalog
        AS $canonical_entity_id$
        DECLARE
            current_id uuid := p_entity_id;
            seen uuid[] := ARRAY[]::uuid[];
            hops integer := 0;
            next_id uuid;
        BEGIN
            IF p_entity_id IS NULL THEN
                RAISE EXCEPTION 'knowledge_merge_missing_entity' USING ERRCODE = '23503';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM core.entities WHERE id = p_entity_id) THEN
                RAISE EXCEPTION 'knowledge_merge_missing_entity' USING ERRCODE = '23503';
            END IF;

            LOOP
                SELECT event.target_entity_id INTO next_id
                  FROM core.entity_merge_events AS event
                 WHERE event.source_entity_id = current_id
                   AND event.event_kind = 'merge'
                   AND event.reversed_at IS NULL;
                EXIT WHEN next_id IS NULL;
                IF current_id = ANY (seen) THEN
                    RAISE EXCEPTION 'knowledge_merge_cycle' USING ERRCODE = '23514';
                END IF;
                hops := hops + 1;
                IF hops >= 64 THEN
                    RAISE EXCEPTION 'knowledge_merge_chain_too_long' USING ERRCODE = '22023';
                END IF;
                seen := seen || current_id;
                current_id := next_id;
            END LOOP;

            RETURN current_id;
        END
        $canonical_entity_id$;

        CREATE FUNCTION core.merge_entities(
            p_source_entity_id uuid,
            p_target_entity_id uuid,
            p_merged_by uuid,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = core, audit, pg_catalog
        AS $merge_entities$
        DECLARE
            v_event_id uuid;
            v_source core.entities%ROWTYPE;
            v_target core.entities%ROWTYPE;
            v_principal_active boolean;
            v_reason_sha text;
            v_hops integer;
            v_walk uuid;
            v_next uuid;
            v_seen uuid[];
        BEGIN
            PERFORM pg_advisory_xact_lock(824, 1);

            IF p_source_entity_id IS NULL OR p_target_entity_id IS NULL THEN
                RAISE EXCEPTION 'knowledge_merge_missing_entity' USING ERRCODE = '23503';
            END IF;
            IF p_source_entity_id = p_target_entity_id THEN
                RAISE EXCEPTION 'knowledge_merge_same_entity' USING ERRCODE = '23514';
            END IF;
            IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
                RAISE EXCEPTION 'knowledge_merge_reason_required' USING ERRCODE = '22023';
            END IF;
            IF p_merged_by IS NULL THEN
                RAISE EXCEPTION 'knowledge_merge_principal_required' USING ERRCODE = '22023';
            END IF;

            SELECT principal.active INTO v_principal_active
              FROM audit.principals AS principal
             WHERE principal.id = p_merged_by;
            IF v_principal_active IS NULL THEN
                RAISE EXCEPTION 'knowledge_merge_principal_required' USING ERRCODE = '23503';
            END IF;
            IF v_principal_active IS NOT TRUE THEN
                RAISE EXCEPTION 'knowledge_merge_principal_inactive' USING ERRCODE = '23514';
            END IF;

            PERFORM 1
              FROM core.entities AS entity
             WHERE entity.id = p_source_entity_id
                OR entity.id = p_target_entity_id
                OR EXISTS (
                    SELECT 1
                      FROM core.entity_merge_events AS event
                     WHERE event.event_kind = 'merge'
                       AND event.reversed_at IS NULL
                       AND (
                            event.source_entity_id = entity.id
                            OR event.target_entity_id = entity.id
                       )
                )
             ORDER BY entity.id
             FOR UPDATE;

            PERFORM 1
              FROM core.entity_merge_events AS event
             WHERE event.event_kind = 'merge'
               AND event.reversed_at IS NULL
             ORDER BY event.id
             FOR UPDATE;

            SELECT * INTO v_source FROM core.entities WHERE id = p_source_entity_id;
            SELECT * INTO v_target FROM core.entities WHERE id = p_target_entity_id;
            IF v_source.id IS NULL OR v_target.id IS NULL THEN
                RAISE EXCEPTION 'knowledge_merge_missing_entity' USING ERRCODE = '23503';
            END IF;
            IF v_source.status IS DISTINCT FROM 'active'
               OR v_target.status IS DISTINCT FROM 'active' THEN
                RAISE EXCEPTION 'knowledge_merge_not_active' USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM core.entity_merge_events AS event
                 WHERE event.source_entity_id IN (p_source_entity_id, p_target_entity_id)
                   AND event.event_kind = 'merge'
                   AND event.reversed_at IS NULL
            ) THEN
                RAISE EXCEPTION 'knowledge_merge_not_canonical' USING ERRCODE = '23514';
            END IF;

            v_walk := p_target_entity_id;
            v_seen := ARRAY[]::uuid[];
            v_hops := 0;
            LOOP
                IF v_walk = p_source_entity_id THEN
                    RAISE EXCEPTION 'knowledge_merge_cycle' USING ERRCODE = '23514';
                END IF;
                IF v_walk = ANY (v_seen) THEN
                    RAISE EXCEPTION 'knowledge_merge_cycle' USING ERRCODE = '23514';
                END IF;
                SELECT event.target_entity_id INTO v_next
                  FROM core.entity_merge_events AS event
                 WHERE event.source_entity_id = v_walk
                   AND event.event_kind = 'merge'
                   AND event.reversed_at IS NULL;
                EXIT WHEN v_next IS NULL;
                v_hops := v_hops + 1;
                IF v_hops >= 64 THEN
                    RAISE EXCEPTION 'knowledge_merge_chain_too_long' USING ERRCODE = '22023';
                END IF;
                v_seen := v_seen || v_walk;
                v_walk := v_next;
            END LOOP;

            v_event_id := gen_random_uuid();
            INSERT INTO core.entity_merge_events (
                id, source_entity_id, target_entity_id, reason, merged_by, merged_at,
                event_kind
            ) VALUES (
                v_event_id, p_source_entity_id, p_target_entity_id, p_reason, p_merged_by,
                clock_timestamp(), 'merge'
            );

            UPDATE core.entities
               SET status = 'merged',
                   updated_at = clock_timestamp()
             WHERE id = p_source_entity_id;

            v_reason_sha := encode(sha256(convert_to(p_reason, 'UTF8')), 'hex');
            INSERT INTO audit.audit_events (
                id, event_key, actor_id, action, target_type, target_id, metadata,
                occurred_at
            ) VALUES (
                gen_random_uuid(),
                'entity.merge:' || v_event_id::text,
                p_merged_by,
                'entity.merge',
                'entity_merge_event',
                v_event_id,
                jsonb_build_object(
                    'source_entity_id', p_source_entity_id,
                    'target_entity_id', p_target_entity_id,
                    'reason_sha256', v_reason_sha
                ),
                clock_timestamp()
            );

            RETURN v_event_id;
        END
        $merge_entities$;

        CREATE FUNCTION core.reverse_entity_merge(
            p_merge_event_id uuid,
            p_reversed_by uuid,
            p_reason text
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = core, audit, pg_catalog
        AS $reverse_entity_merge$
        DECLARE
            v_reverse_id uuid;
            v_source_id uuid;
            v_target_id uuid;
            v_kind text;
            v_reversed_at timestamptz;
            v_principal_active boolean;
            v_reason_sha text;
            v_source core.entities%ROWTYPE;
            v_target core.entities%ROWTYPE;
        BEGIN
            PERFORM pg_advisory_xact_lock(824, 1);

            IF p_merge_event_id IS NULL THEN
                RAISE EXCEPTION 'knowledge_merge_missing_event' USING ERRCODE = '23503';
            END IF;
            IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
                RAISE EXCEPTION 'knowledge_merge_reason_required' USING ERRCODE = '22023';
            END IF;
            IF p_reversed_by IS NULL THEN
                RAISE EXCEPTION 'knowledge_merge_principal_required' USING ERRCODE = '22023';
            END IF;

            SELECT principal.active INTO v_principal_active
              FROM audit.principals AS principal
             WHERE principal.id = p_reversed_by;
            IF v_principal_active IS NULL THEN
                RAISE EXCEPTION 'knowledge_merge_principal_required' USING ERRCODE = '23503';
            END IF;
            IF v_principal_active IS NOT TRUE THEN
                RAISE EXCEPTION 'knowledge_merge_principal_inactive' USING ERRCODE = '23514';
            END IF;

            SELECT event.source_entity_id, event.target_entity_id
              INTO v_source_id, v_target_id
              FROM core.entity_merge_events AS event
             WHERE event.id = p_merge_event_id;
            IF v_source_id IS NULL OR v_target_id IS NULL THEN
                RAISE EXCEPTION 'knowledge_merge_missing_event' USING ERRCODE = '23503';
            END IF;

            PERFORM 1
              FROM core.entities AS entity
             WHERE entity.id = v_source_id
                OR entity.id = v_target_id
                OR EXISTS (
                    SELECT 1
                      FROM core.entity_merge_events AS event
                     WHERE event.event_kind = 'merge'
                       AND event.reversed_at IS NULL
                       AND (
                            event.source_entity_id = entity.id
                            OR event.target_entity_id = entity.id
                       )
                )
             ORDER BY entity.id
             FOR UPDATE;

            PERFORM 1
              FROM core.entity_merge_events AS event
             WHERE event.id = p_merge_event_id
                OR (event.event_kind = 'merge' AND event.reversed_at IS NULL)
             ORDER BY event.id
             FOR UPDATE;

            SELECT event.source_entity_id, event.target_entity_id,
                   event.event_kind, event.reversed_at
              INTO v_source_id, v_target_id, v_kind, v_reversed_at
              FROM core.entity_merge_events AS event
             WHERE event.id = p_merge_event_id;
            IF v_source_id IS NULL THEN
                RAISE EXCEPTION 'knowledge_merge_missing_event' USING ERRCODE = '23503';
            END IF;
            IF v_kind IS DISTINCT FROM 'merge' THEN
                RAISE EXCEPTION 'knowledge_merge_not_merge_event' USING ERRCODE = '23514';
            END IF;
            IF v_reversed_at IS NOT NULL THEN
                RAISE EXCEPTION 'knowledge_merge_already_reversed' USING ERRCODE = '23514';
            END IF;

            SELECT * INTO v_source FROM core.entities WHERE id = v_source_id;
            SELECT * INTO v_target FROM core.entities WHERE id = v_target_id;
            IF v_source.id IS NULL OR v_target.id IS NULL THEN
                RAISE EXCEPTION 'knowledge_merge_missing_entity' USING ERRCODE = '23503';
            END IF;

            v_reverse_id := gen_random_uuid();
            INSERT INTO core.entity_merge_events (
                id, source_entity_id, target_entity_id, reason, merged_by, merged_at,
                event_kind, reversed_by_id, reversed_at
            ) VALUES (
                v_reverse_id, v_source_id, v_target_id, p_reason, p_reversed_by,
                clock_timestamp(), 'reverse', NULL, NULL
            );

            UPDATE core.entity_merge_events
               SET reversed_by_id = v_reverse_id,
                   reversed_at = clock_timestamp()
             WHERE id = p_merge_event_id;

            UPDATE core.entities
               SET status = 'active',
                   updated_at = clock_timestamp()
             WHERE id = v_source_id
               AND NOT EXISTS (
                    SELECT 1
                      FROM core.entity_merge_events AS event
                     WHERE event.source_entity_id = v_source_id
                       AND event.event_kind = 'merge'
                       AND event.reversed_at IS NULL
               );

            v_reason_sha := encode(sha256(convert_to(p_reason, 'UTF8')), 'hex');
            INSERT INTO audit.audit_events (
                id, event_key, actor_id, action, target_type, target_id, metadata,
                occurred_at
            ) VALUES (
                gen_random_uuid(),
                'entity.merge.reverse:' || v_reverse_id::text,
                p_reversed_by,
                'entity.merge.reverse',
                'entity_merge_event',
                v_reverse_id,
                jsonb_build_object(
                    'source_entity_id', v_source_id,
                    'target_entity_id', v_target_id,
                    'reversed_merge_event_id', p_merge_event_id,
                    'reason_sha256', v_reason_sha
                ),
                clock_timestamp()
            );

            RETURN v_reverse_id;
        END
        $reverse_entity_merge$;

        REVOKE ALL ON FUNCTION core.canonical_entity_id(uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION core.canonical_entity_id(uuid)
            TO uap_api, uap_worker, uap_publisher, uap_audit_reader, uap_backup;
        REVOKE ALL ON FUNCTION core.merge_entities(uuid, uuid, uuid, text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION core.reverse_entity_merge(uuid, uuid, text) FROM PUBLIC;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        DO $downgrade_guard$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM core.entity_merge_events
                 WHERE event_kind = 'reverse'
                    OR reversed_at IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'knowledge_merge_downgrade_blocked' USING ERRCODE = '55000';
            END IF;
        END
        $downgrade_guard$;

        DROP FUNCTION IF EXISTS core.reverse_entity_merge(uuid, uuid, text);
        DROP FUNCTION IF EXISTS core.merge_entities(uuid, uuid, uuid, text);
        DROP FUNCTION IF EXISTS core.canonical_entity_id(uuid);
        DROP INDEX IF EXISTS core.uq_open_merge_source;
        ALTER TABLE core.entity_merge_events
            DROP CONSTRAINT IF EXISTS ck_entity_merge_event_kind;
        ALTER TABLE core.entity_merge_events
            DROP COLUMN IF EXISTS event_kind;
        """
    )
