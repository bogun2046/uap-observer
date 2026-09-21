"""Fail closed when a V1-3.1 revoke reaches the publication boundary.

V1-3.1 permits revoking an authorization only before the corresponding
``publication.granted`` event has been leased for apply or acknowledged as
published.  Public projection withdrawal remains a V1-3.3 capability.

The granted outbox row is the canonical apply/ack state.  The public document
row is checked as a defensive projection invariant.  Both checks run inside
the database authority boundary and share the publisher's document advisory
lock, so an API-side visibility check cannot race the publisher.
"""

from __future__ import annotations

from alembic import op

revision = "0031_v131_prepublication_revoke_guard"
down_revision = "0030_v131_publication_editorial_revision_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        ALTER FUNCTION audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) RENAME TO _apply_publication_grant_v131_unfenced;

        CREATE FUNCTION audit._apply_publication_grant(
            p_case_type audit.review_case_type,
            p_subject_id uuid,
            p_case_id uuid,
            p_decision_id uuid,
            p_decision audit.review_decision
        ) RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = audit, core, ops, public, pg_catalog
        AS $apply_publication_grant_v131_guard$
        DECLARE
            v_grant_id uuid;
            v_grant_status audit.grant_status;
            v_event_id uuid;
            v_event_published_at timestamptz;
            v_event_lease_token uuid;
            v_event_lease_expires_at timestamptz;
        BEGIN
            IF p_case_type = 'document'::audit.review_case_type
               AND p_decision = 'withdraw'::audit.review_decision
            THEN
                -- Resolve the active grant without taking the grant lock yet.
                -- Publisher lock order is outbox row -> subject advisory lock;
                -- retaining that order avoids a revoke/apply deadlock.
                SELECT grant_row.id
                  INTO v_grant_id
                  FROM audit.document_publication_grants AS grant_row
                 WHERE grant_row.document_version_id = p_subject_id
                   AND grant_row.grant_status = 'active'::audit.grant_status;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'review_grant_not_active' USING ERRCODE = '22023';
                END IF;

                -- published_at is the canonical successful apply/ack signal.
                -- A live lease means Publisher already owns the apply right.
                SELECT event.id, event.published_at, event.lease_token,
                       event.lease_expires_at
                  INTO v_event_id, v_event_published_at, v_event_lease_token,
                       v_event_lease_expires_at
                  FROM ops.outbox_events AS event
                 WHERE event.aggregate_type = 'document_publication_grants'
                   AND event.aggregate_id = v_grant_id
                   AND event.event_type = 'publication.granted'
                 ORDER BY event.occurred_at ASC, event.id ASC
                 LIMIT 1
                 FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'publication_event_not_terminal' USING ERRCODE = '23505';
                END IF;
                IF v_event_published_at IS NOT NULL
                   OR (
                        v_event_lease_token IS NOT NULL
                        AND v_event_lease_expires_at > clock_timestamp()
                   )
                THEN
                    RAISE EXCEPTION 'publication_already_projected' USING ERRCODE = '23505';
                END IF;

                -- apply_publication_event uses this exact lock identity before
                -- reading grant state or writing the public projection.
                PERFORM pg_advisory_xact_lock(
                    hashtextextended(
                        'publication-document:' || p_subject_id::text,
                        0
                    )
                );

                SELECT grant_row.grant_status
                  INTO v_grant_status
                  FROM audit.document_publication_grants AS grant_row
                 WHERE grant_row.id = v_grant_id
                 FOR UPDATE;
                IF NOT FOUND OR v_grant_status <> 'active'::audit.grant_status THEN
                    RAISE EXCEPTION 'review_grant_not_active' USING ERRCODE = '22023';
                END IF;

                -- Recheck the canonical ack while its row remains locked and
                -- defend against projection drift with the public row itself.
                IF v_event_published_at IS NOT NULL
                   OR EXISTS (
                        SELECT 1
                          FROM public.documents AS document
                         WHERE document.document_grant_id = v_grant_id
                   )
                THEN
                    RAISE EXCEPTION 'publication_already_projected' USING ERRCODE = '23505';
                END IF;
            END IF;

            PERFORM audit._apply_publication_grant_v131_unfenced(
                p_case_type, p_subject_id, p_case_id, p_decision_id, p_decision
            );
        END
        $apply_publication_grant_v131_guard$;

        ALTER FUNCTION audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) OWNER TO uap_owner;
        ALTER FUNCTION audit._apply_publication_grant_v131_unfenced(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) FROM PUBLIC;
        REVOKE ALL ON FUNCTION audit._apply_publication_grant_v131_unfenced(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) FROM PUBLIC;

        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        DROP FUNCTION audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        );
        ALTER FUNCTION audit._apply_publication_grant_v131_unfenced(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) RENAME TO _apply_publication_grant;
        ALTER FUNCTION audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) OWNER TO uap_owner;
        REVOKE ALL ON FUNCTION audit._apply_publication_grant(
            audit.review_case_type, uuid, uuid, uuid, audit.review_decision
        ) FROM PUBLIC;

        RESET ROLE;
        """
    )
