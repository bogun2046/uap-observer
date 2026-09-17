"""Add the WP10.4 public API read indexes.

Revision ID: 0023_wp10_api_read_indexes
Revises: 0022_wp10_claim_search_projection
Create Date: 2026-08-30
"""

from __future__ import annotations

from alembic import op

revision = "0023_wp10_api_read_indexes"
down_revision = "0022_wp10_claim_search_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;

        CREATE INDEX ix_public_documents_published_id
            ON public.documents (published_at DESC, id DESC);
        CREATE INDEX ix_public_documents_category_published_id
            ON public.documents (category, published_at DESC, id DESC);
        CREATE INDEX ix_public_documents_fact_status_published_id
            ON public.documents (fact_status, published_at DESC, id DESC);
        CREATE INDEX ix_public_documents_category_fact_published_id
            ON public.documents (
                category, fact_status, published_at DESC, id DESC
            );

        CREATE INDEX ix_public_entities_published_id
            ON public.entities (published_at DESC, id DESC);
        CREATE INDEX ix_public_entities_type_published_id
            ON public.entities (entity_type, published_at DESC, id DESC);

        -- Signed WP10.3 already supplies the minimal remaining read indexes:
        --   ix_public_claims_document_ordinal(document_id, ordinal, id)
        --   document_entities UNIQUE(document_id, entity_id) backing index
        --   ix_public_document_entities_entity(entity_id, document_id)
        --   ix_search_documents_vector GIN(search_vector)
        --   ix_search_documents_facets GIN(facets)

        RESET ROLE;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        SET ROLE uap_owner;
        DROP INDEX public.ix_public_entities_type_published_id;
        DROP INDEX public.ix_public_entities_published_id;
        DROP INDEX public.ix_public_documents_category_fact_published_id;
        DROP INDEX public.ix_public_documents_fact_status_published_id;
        DROP INDEX public.ix_public_documents_category_published_id;
        DROP INDEX public.ix_public_documents_published_id;
        RESET ROLE;
        """
    )
