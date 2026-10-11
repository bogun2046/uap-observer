"""Version rebuild digests and bound new-run transition state.

Legacy v1 replay intentionally retains its historical input algorithm. New
full/scoped runs use ordered row hashes and fixed-size v2 fold state. Downgrade
fails closed in the presence of v2 audit history. No projection write, eligibility,
TRUNCATE, authorization, or transaction boundary is changed.
"""

from __future__ import annotations

from alembic import op

revision = "0039_v133_full_rebuild_bounded_digest"
down_revision = "0038_v133_full_rebuild_compaction"
branch_labels = None
depends_on = None

SIGNATURES = (
    "ops.rebuild_public_projection(uuid)",
    "ops.rebuild_public_projection(uuid,uuid)",
    "ops._wp10_3_projection_digest()",
    "audit.guard_publication_rebuild_run_mutation()",
)

BASELINE_SHA256 = (
    "a4a89db2e99a76208c1b2f26d3a89455b43c0b1108dc852a1a6389e9ee5f5d35",
    "d69fa284843df67eb27548fd2705e5d35dcc63015acd611d3739ac6628f00f83",
    "d7bf6a3ef1780d8695054ab19b2534f9f22b649e40ffda43df56b83baeb6f0d0",
    "97fd1395241493ba76fc9f94a7246ad3e45e8d0f84c5ceb8514cfcc454b19843",
)

V2_SHA256 = (
    "11bbf3eaabf5e6eeb42d35bf2d85ac6302648ce1cbd92efc792714458a549786",
    "d3447fc529d6826bc1a996889b36377cd094d202f192d785d8bab2cb9399ca0b",
    "6aa842ab56747d2ceab2291a284340bfa1c87f2d450c3af3f57dc282a8e18d18",
    "8cba20305b2c572eb22da3c25e4e7328c462f531f4343dea5a3eba90614936cd",
)

REPLACEMENTS = (
    (
        (
            "            SELECT audit._payload_sha256(jsonb_build_object(\n                'document', coalesce((SELECT jsonb_agg(jsonb_build_array(\n                    lower(grant_row.id::text),\n                    btrim(grant_row.publication_payload_sha256::text),\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(manifest.manifest_sha256::text) END,\n                    manifest.grant_id IS NOT NULL,\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(audit._publication_manifest_sha(\n                            ops._wp10_3_document_manifest_payload(grant_row.id)\n                        )) END\n                ) ORDER BY grant_row.id)\n                FROM audit.document_publication_grants AS grant_row\n                 LEFT JOIN audit.document_publication_manifests AS manifest\n                   ON manifest.grant_id = grant_row.id\n                WHERE grant_row.grant_status = 'active'::audit.grant_status\n                  AND manifest.grant_id IS NOT NULL\n                  AND NOT ops._wp10_3_grant_has_unresolved_quarantine(\n                      'document_publication_grants', grant_row.id\n                  )\n                  AND ops._v133_full_rebuild_grant_eligible('document_publication_grants', grant_row.id)), '[]'::jsonb),\n                'entity', coalesce((SELECT jsonb_agg(jsonb_build_array(\n                    lower(grant_row.id::text),\n                    btrim(grant_row.publication_payload_sha256::text),\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(manifest.manifest_sha256::text) END,\n                    manifest.grant_id IS NOT NULL,\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(audit._publication_manifest_sha(\n                            ops._wp10_3_entity_manifest_payload(grant_row.id)\n                        )) END\n                ) ORDER BY grant_row.id)\n                FROM audit.entity_publication_grants AS grant_row\n                 LEFT JOIN audit.entity_publication_manifests AS manifest\n                   ON manifest.grant_id = grant_row.id\n                WHERE grant_row.grant_status = 'active'::audit.grant_status\n                  AND manifest.grant_id IS NOT NULL\n                  AND NOT ops._wp10_3_grant_has_unresolved_quarantine(\n                      'entity_publication_grants', grant_row.id\n                  )\n                  AND ops._v133_full_rebuild_grant_eligible('entity_publication_grants', grant_row.id)), '[]'::jsonb),\n                'claim', coalesce((SELECT jsonb_agg(jsonb_build_array(\n                    lower(grant_row.id::text),\n                    btrim(grant_row.publication_payload_sha256::text),\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(manifest.manifest_sha256::text) END,\n                    manifest.grant_id IS NOT NULL,\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(audit._publication_manifest_sha(\n                            ops._wp10_3_claim_manifest_payload(grant_row.id)\n                        )) END\n                ) ORDER BY grant_row.id)\n                FROM audit.claim_publication_grants AS grant_row\n                 LEFT JOIN audit.claim_publication_manifests AS manifest\n                   ON manifest.grant_id = grant_row.id\n                WHERE grant_row.grant_status = 'active'::audit.grant_status\n                  AND manifest.grant_id IS NOT NULL\n                  AND NOT ops._wp10_3_grant_has_unresolved_quarantine(\n                      'claim_publication_grants', grant_row.id\n                  )\n                  AND ops._v133_full_rebuild_grant_eligible('claim_publication_grants', grant_row.id)), '[]'::jsonb)\n            )) INTO v_input;\n            SELECT ops._wp10_3_rebuild_inputs_valid() INTO v_inputs_valid;\n            SELECT * INTO run_row FROM audit.publication_rebuild_runs\n             WHERE publication_rebuild_runs.rebuild_id = p_rebuild_id FOR UPDATE;\n            IF FOUND THEN",
            "            SELECT * INTO run_row FROM audit.publication_rebuild_runs\n             WHERE publication_rebuild_runs.rebuild_id = p_rebuild_id FOR UPDATE;\n            IF run_row.rebuild_id IS NULL THEN\n                SELECT ops._v133_full_input_digest_v2() INTO v_input;\n            ELSIF run_row.digest_version = 1 THEN\n                SELECT ops._v133_full_input_digest_legacy_v1() INTO v_input;\n            ELSIF run_row.digest_version = 2 THEN\n                SELECT ops._v133_full_input_digest_v2() INTO v_input;\n            ELSE\n                RAISE EXCEPTION 'publication_rebuild_unknown_digest_version'\n                    USING ERRCODE = '22023';\n            END IF;\n            SELECT ops._wp10_3_rebuild_inputs_valid() INTO v_inputs_valid;\n            IF run_row.rebuild_id IS NOT NULL THEN",
        ),
        (
            "rebuild_id, input_digest, status, started_at",
            "rebuild_id, input_digest, status, started_at, digest_version",
        ),
        (
            "VALUES (p_rebuild_id, v_input, 'running', v_now)",
            "VALUES (p_rebuild_id, v_input, 'running', v_now, 2)",
        ),
    ),
    (
        (
            "            SELECT audit._payload_sha256(jsonb_build_object(\n                'scope', 'document',\n                'document_id', lower(p_document_id::text),\n                'document_version_id', lower(v_document_grant.document_version_id::text),\n                'grant_id', lower(v_document_grant.id::text),\n                'revision_no', v_document_grant.revision_no,\n                'editorial_revision_id', lower(v_document_grant.editorial_revision_id::text),\n                'editorial_revision_no', v_document_grant.editorial_revision_no,\n                'manifest_sha256', btrim(manifest_row.manifest_sha256::text),\n                'published_event_id', lower(event_row.id::text),\n                'claims', coalesce((\n                    SELECT jsonb_agg(jsonb_build_array(\n                        lower(claim_grant.id::text),\n                        claim_grant.revision_no,\n                        btrim(claim_manifest.manifest_sha256::text)\n                    ) ORDER BY claim_grant.id)\n                      FROM audit.claim_publication_grants AS claim_grant\n                      JOIN audit.claim_publication_manifests AS claim_manifest\n                        ON claim_manifest.grant_id = claim_grant.id\n                     WHERE claim_grant.grant_status = 'active'::audit.grant_status\n                       AND claim_manifest.document_id = p_document_id\n                       AND EXISTS (\n                           SELECT 1 FROM ops.outbox_events AS claim_event\n                            WHERE claim_event.aggregate_type = 'claim_publication_grants'\n                              AND claim_event.aggregate_id = claim_grant.id\n                              AND claim_event.event_type = 'publication.granted'\n                              AND claim_event.published_at IS NOT NULL\n                              AND claim_event.terminal_at IS NULL\n                       )\n                ), '[]'::jsonb)\n            )) INTO v_input;\n\n            SELECT * INTO run_row FROM audit.publication_rebuild_runs AS prr\n             WHERE prr.rebuild_id = p_rebuild_id FOR UPDATE;\n            IF FOUND THEN",
            "            SELECT * INTO run_row FROM audit.publication_rebuild_runs AS prr\n             WHERE prr.rebuild_id = p_rebuild_id FOR UPDATE;\n            IF run_row.rebuild_id IS NULL THEN\n                SELECT ops._v133_scoped_input_digest_v2(p_document_id, jsonb_build_object(\n                'scope', 'document',\n                'document_id', lower(p_document_id::text),\n                'document_version_id', lower(v_document_grant.document_version_id::text),\n                'grant_id', lower(v_document_grant.id::text),\n                'revision_no', v_document_grant.revision_no,\n                'editorial_revision_id', lower(v_document_grant.editorial_revision_id::text),\n                'editorial_revision_no', v_document_grant.editorial_revision_no,\n                'manifest_sha256', btrim(manifest_row.manifest_sha256::text),\n                'published_event_id', lower(event_row.id::text))) INTO v_input;\n            ELSIF run_row.digest_version = 1 THEN\n                SELECT ops._v133_scoped_input_digest_legacy_v1(p_document_id, jsonb_build_object(\n                'scope', 'document',\n                'document_id', lower(p_document_id::text),\n                'document_version_id', lower(v_document_grant.document_version_id::text),\n                'grant_id', lower(v_document_grant.id::text),\n                'revision_no', v_document_grant.revision_no,\n                'editorial_revision_id', lower(v_document_grant.editorial_revision_id::text),\n                'editorial_revision_no', v_document_grant.editorial_revision_no,\n                'manifest_sha256', btrim(manifest_row.manifest_sha256::text),\n                'published_event_id', lower(event_row.id::text))) INTO v_input;\n            ELSIF run_row.digest_version = 2 THEN\n                SELECT ops._v133_scoped_input_digest_v2(p_document_id, jsonb_build_object(\n                'scope', 'document',\n                'document_id', lower(p_document_id::text),\n                'document_version_id', lower(v_document_grant.document_version_id::text),\n                'grant_id', lower(v_document_grant.id::text),\n                'revision_no', v_document_grant.revision_no,\n                'editorial_revision_id', lower(v_document_grant.editorial_revision_id::text),\n                'editorial_revision_no', v_document_grant.editorial_revision_no,\n                'manifest_sha256', btrim(manifest_row.manifest_sha256::text),\n                'published_event_id', lower(event_row.id::text))) INTO v_input;\n            ELSE\n                RAISE EXCEPTION 'publication_rebuild_unknown_digest_version'\n                    USING ERRCODE = '22023';\n            END IF;\n            IF run_row.rebuild_id IS NOT NULL THEN",
        ),
        (
            "rebuild_id, input_digest, status, started_at",
            "rebuild_id, input_digest, status, started_at, digest_version",
        ),
        (
            "VALUES (p_rebuild_id, v_input, 'running', v_now)",
            "VALUES (p_rebuild_id, v_input, 'running', v_now, 2)",
        ),
    ),
    (
        (
            "            SELECT audit._payload_sha256(jsonb_build_object(\n                'documents', coalesce((SELECT jsonb_agg(to_jsonb(row_data) ORDER BY id)\n                    FROM (SELECT id, document_grant_id, slug, title, summary,\n                                 category::text, fact_status::text, source_name,\n                                 canonical_source_url, source_published_at, revision_no\n                            FROM public.documents) AS row_data), '[]'::jsonb),\n                'entities', coalesce((SELECT jsonb_agg(to_jsonb(row_data) ORDER BY id)\n                    FROM (SELECT id, entity_grant_id, slug, entity_type, name,\n                                 description, country_code, revision_no\n                            FROM public.entities) AS row_data), '[]'::jsonb),\n                'claims', coalesce((SELECT jsonb_agg(to_jsonb(row_data) ORDER BY id)\n                    FROM (SELECT id, document_id, claim_grant_id, ordinal, claim_text,\n                                 claim_type, assertion_status, attribution, revision_no\n                            FROM public.claims) AS row_data), '[]'::jsonb),\n                'evidence', coalesce((SELECT jsonb_agg(to_jsonb(row_data) ORDER BY id)\n                    FROM (SELECT id, document_id, excerpt, locator_type, page_start,\n                                 page_end, time_start_ms, time_end_ms, public_locator,\n                                 locator_sha256, source_url FROM public.evidence) AS row_data\n                ), '[]'::jsonb),\n                'claim_evidence', coalesce((SELECT jsonb_agg(to_jsonb(row_data) ORDER BY id)\n                    FROM (SELECT id, claim_id, evidence_id FROM public.claim_evidence) AS row_data\n                ), '[]'::jsonb),\n                'document_entities', coalesce((SELECT jsonb_agg(to_jsonb(row_data) ORDER BY id)\n                    FROM (SELECT id, document_id, entity_id, basis_evidence_id,\n                                 basis_claim_id FROM public.document_entities) AS row_data\n                ), '[]'::jsonb),\n                'search', coalesce((SELECT jsonb_agg(to_jsonb(row_data) ORDER BY document_id)\n                    FROM (SELECT document_id, search_vector::text, display_text, facets\n                            FROM public.search_documents) AS row_data), '[]'::jsonb)\n            ))\n        ",
            '            SELECT ops._v133_rebuild_digest_v2(domain,item_sha256 ORDER BY section,kind,stable_key) FROM (\nSELECT 0 AS section,0 AS kind,\'00000000-0000-0000-0000-000000000000\'::uuid AS stable_key,\'seed\'::text AS domain,audit._payload_sha256(\'{"schema":"publication-projection-digest.v2","sections":["documents","entities","claims","evidence","claim_evidence","document_entities","search"]}\'::jsonb) AS item_sha256\nUNION ALL\nSELECT 1,0,\'00000000-0000-0000-0000-000000000000\'::uuid,\'documents\',audit._payload_sha256(\'{"schema":"publication-projection-digest.v2","section":"documents","marker":"begin"}\'::jsonb)\nUNION ALL\nSELECT 1,2,\'00000000-0000-0000-0000-000000000000\'::uuid,\'documents\',audit._payload_sha256(\'{"schema":"publication-projection-digest.v2","section":"documents","marker":"end"}\'::jsonb)\nUNION ALL\nSELECT 1,1,id,\'documents\',audit._payload_sha256(to_jsonb(row_data)) FROM (SELECT id, document_grant_id, slug, title, summary,\n                                 category::text, fact_status::text, source_name,\n                                 canonical_source_url, source_published_at, revision_no\n                            FROM public.documents) AS row_data\nUNION ALL\nSELECT 2,0,\'00000000-0000-0000-0000-000000000000\'::uuid,\'entities\',audit._payload_sha256(\'{"schema":"publication-projection-digest.v2","section":"entities","marker":"begin"}\'::jsonb)\nUNION ALL\nSELECT 2,2,\'00000000-0000-0000-0000-000000000000\'::uuid,\'entities\',audit._payload_sha256(\'{"schema":"publication-projection-digest.v2","section":"entities","marker":"end"}\'::jsonb)\nUNION ALL\nSELECT 2,1,id,\'entities\',audit._payload_sha256(to_jsonb(row_data)) FROM (SELECT id, entity_grant_id, slug, entity_type, name,\n                                 description, country_code, revision_no\n                            FROM public.entities) AS row_data\nUNION ALL\nSELECT 3,0,\'00000000-0000-0000-0000-000000000000\'::uuid,\'claims\',audit._payload_sha256(\'{"schema":"publication-projection-digest.v2","section":"claims","marker":"begin"}\'::jsonb)\nUNION ALL\nSELECT 3,2,\'00000000-0000-0000-0000-000000000000\'::uuid,\'claims\',audit._payload_sha256(\'{"schema":"publication-projection-digest.v2","section":"claims","marker":"end"}\'::jsonb)\nUNION ALL\nSELECT 3,1,id,\'claims\',audit._payload_sha256(to_jsonb(row_data)) FROM (SELECT id, document_id, claim_grant_id, ordinal, claim_text,\n                                 claim_type, assertion_status, attribution, revision_no\n                            FROM public.claims) AS row_data\nUNION ALL\nSELECT 4,0,\'00000000-0000-0000-0000-000000000000\'::uuid,\'evidence\',audit._payload_sha256(\'{"schema":"publication-projection-digest.v2","section":"evidence","marker":"begin"}\'::jsonb)\nUNION ALL\nSELECT 4,2,\'00000000-0000-0000-0000-000000000000\'::uuid,\'evidence\',audit._payload_sha256(\'{"schema":"publication-projection-digest.v2","section":"evidence","marker":"end"}\'::jsonb)\nUNION ALL\nSELECT 4,1,id,\'evidence\',audit._payload_sha256(to_jsonb(row_data)) FROM (SELECT id, document_id, excerpt, locator_type, page_start,\n                                 page_end, time_start_ms, time_end_ms, public_locator,\n                                 locator_sha256, source_url FROM public.evidence) AS row_data\nUNION ALL\nSELECT 5,0,\'00000000-0000-0000-0000-000000000000\'::uuid,\'claim_evidence\',audit._payload_sha256(\'{"schema":"publication-projection-digest.v2","section":"claim_evidence","marker":"begin"}\'::jsonb)\nUNION ALL\nSELECT 5,2,\'00000000-0000-0000-0000-000000000000\'::uuid,\'claim_evidence\',audit._payload_sha256(\'{"schema":"publication-projection-digest.v2","section":"claim_evidence","marker":"end"}\'::jsonb)\nUNION ALL\nSELECT 5,1,id,\'claim_evidence\',audit._payload_sha256(to_jsonb(row_data)) FROM (SELECT id, claim_id, evidence_id FROM public.claim_evidence) AS row_data\nUNION ALL\nSELECT 6,0,\'00000000-0000-0000-0000-000000000000\'::uuid,\'document_entities\',audit._payload_sha256(\'{"schema":"publication-projection-digest.v2","section":"document_entities","marker":"begin"}\'::jsonb)\nUNION ALL\nSELECT 6,2,\'00000000-0000-0000-0000-000000000000\'::uuid,\'document_entities\',audit._payload_sha256(\'{"schema":"publication-projection-digest.v2","section":"document_entities","marker":"end"}\'::jsonb)\nUNION ALL\nSELECT 6,1,id,\'document_entities\',audit._payload_sha256(to_jsonb(row_data)) FROM (SELECT id, document_id, entity_id, basis_evidence_id,\n                                 basis_claim_id FROM public.document_entities) AS row_data\nUNION ALL\nSELECT 7,0,\'00000000-0000-0000-0000-000000000000\'::uuid,\'search\',audit._payload_sha256(\'{"schema":"publication-projection-digest.v2","section":"search","marker":"begin"}\'::jsonb)\nUNION ALL\nSELECT 7,2,\'00000000-0000-0000-0000-000000000000\'::uuid,\'search\',audit._payload_sha256(\'{"schema":"publication-projection-digest.v2","section":"search","marker":"end"}\'::jsonb)\nUNION ALL\nSELECT 7,1,document_id,\'search\',audit._payload_sha256(to_jsonb(row_data)) FROM (SELECT document_id, search_vector::text, display_text, facets\n                            FROM public.search_documents) AS row_data\n) AS digest_items;\n        ',
        ),
    ),
    (
        (
            "               OR NEW.input_digest IS DISTINCT FROM OLD.input_digest",
            "               OR NEW.input_digest IS DISTINCT FROM OLD.input_digest\n               OR NEW.digest_version IS DISTINCT FROM OLD.digest_version",
        ),
    ),
)

HELPER_SIGNATURES = (
    "ops._v133_rebuild_digest_fold_v2(text,text,text)",
    "ops._v133_rebuild_digest_v2(text,text)",
    "ops._v133_full_input_digest_v2()",
    "ops._v133_full_input_digest_legacy_v1()",
    "ops._v133_scoped_input_digest_v2(uuid,jsonb)",
    "ops._v133_scoped_input_digest_legacy_v1(uuid,jsonb)",
)

HELPER_DDL = (
    "CREATE FUNCTION ops._v133_rebuild_digest_fold_v2(state text, domain text, item_sha256 text) RETURNS text\nLANGUAGE plpgsql IMMUTABLE STRICT SET search_path=pg_catalog AS $fold$\nBEGIN\n IF state !~ '^[0-9a-f]{64}$' OR item_sha256 !~ '^[0-9a-f]{64}$' OR domain='' THEN\n  RAISE EXCEPTION 'publication_rebuild_digest_v2_invalid_item' USING ERRCODE='22023';\n END IF;\n RETURN audit._payload_sha256(jsonb_build_object('schema','publication-rebuild-digest-fold.v2','previous',state,'domain',domain,'item_sha256',item_sha256));\nEND $fold$;",
    "CREATE AGGREGATE ops._v133_rebuild_digest_v2(text,text) (SFUNC=ops._v133_rebuild_digest_fold_v2,STYPE=text,INITCOND='0000000000000000000000000000000000000000000000000000000000000000');",
    'CREATE FUNCTION ops._v133_full_input_digest_v2() RETURNS text LANGUAGE sql STABLE SET search_path=ops,audit,core,public,pg_catalog SET timezone=\'UTC\' AS $v133_digest$\nSELECT ops._v133_rebuild_digest_v2(domain,item_sha256 ORDER BY section,kind,stable_key) FROM (\nSELECT 0 AS section,0 AS kind,\'00000000-0000-0000-0000-000000000000\'::uuid AS stable_key,\'seed\'::text AS domain,audit._payload_sha256(\'{"schema":"publication-full-input-digest.v2","sections":["document","entity","claim"]}\'::jsonb) AS item_sha256\nUNION ALL\nSELECT 1,0,\'00000000-0000-0000-0000-000000000000\'::uuid,\'document\',audit._payload_sha256(\'{"schema":"publication-full-input-digest.v2","section":"document","marker":"begin"}\'::jsonb)\nUNION ALL\nSELECT 1,2,\'00000000-0000-0000-0000-000000000000\'::uuid,\'document\',audit._payload_sha256(\'{"schema":"publication-full-input-digest.v2","section":"document","marker":"end"}\'::jsonb)\nUNION ALL\nSELECT 1,1,grant_row.id,\'document\',audit._payload_sha256(jsonb_build_array(\n                    lower(grant_row.id::text),\n                    btrim(grant_row.publication_payload_sha256::text),\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(manifest.manifest_sha256::text) END,\n                    manifest.grant_id IS NOT NULL,\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(audit._publication_manifest_sha(\n                            ops._wp10_3_document_manifest_payload(grant_row.id)\n                        )) END\n                )) FROM audit.document_publication_grants AS grant_row\n                 LEFT JOIN audit.document_publication_manifests AS manifest\n                   ON manifest.grant_id = grant_row.id\n                WHERE grant_row.grant_status = \'active\'::audit.grant_status\n                  AND manifest.grant_id IS NOT NULL\n                  AND NOT ops._wp10_3_grant_has_unresolved_quarantine(\n                      \'document_publication_grants\', grant_row.id\n                  )\n                  AND ops._v133_full_rebuild_grant_eligible(\'document_publication_grants\', grant_row.id)\nUNION ALL\nSELECT 2,0,\'00000000-0000-0000-0000-000000000000\'::uuid,\'entity\',audit._payload_sha256(\'{"schema":"publication-full-input-digest.v2","section":"entity","marker":"begin"}\'::jsonb)\nUNION ALL\nSELECT 2,2,\'00000000-0000-0000-0000-000000000000\'::uuid,\'entity\',audit._payload_sha256(\'{"schema":"publication-full-input-digest.v2","section":"entity","marker":"end"}\'::jsonb)\nUNION ALL\nSELECT 2,1,grant_row.id,\'entity\',audit._payload_sha256(jsonb_build_array(\n                    lower(grant_row.id::text),\n                    btrim(grant_row.publication_payload_sha256::text),\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(manifest.manifest_sha256::text) END,\n                    manifest.grant_id IS NOT NULL,\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(audit._publication_manifest_sha(\n                            ops._wp10_3_entity_manifest_payload(grant_row.id)\n                        )) END\n                )) FROM audit.entity_publication_grants AS grant_row\n                 LEFT JOIN audit.entity_publication_manifests AS manifest\n                   ON manifest.grant_id = grant_row.id\n                WHERE grant_row.grant_status = \'active\'::audit.grant_status\n                  AND manifest.grant_id IS NOT NULL\n                  AND NOT ops._wp10_3_grant_has_unresolved_quarantine(\n                      \'entity_publication_grants\', grant_row.id\n                  )\n                  AND ops._v133_full_rebuild_grant_eligible(\'entity_publication_grants\', grant_row.id)\nUNION ALL\nSELECT 3,0,\'00000000-0000-0000-0000-000000000000\'::uuid,\'claim\',audit._payload_sha256(\'{"schema":"publication-full-input-digest.v2","section":"claim","marker":"begin"}\'::jsonb)\nUNION ALL\nSELECT 3,2,\'00000000-0000-0000-0000-000000000000\'::uuid,\'claim\',audit._payload_sha256(\'{"schema":"publication-full-input-digest.v2","section":"claim","marker":"end"}\'::jsonb)\nUNION ALL\nSELECT 3,1,grant_row.id,\'claim\',audit._payload_sha256(jsonb_build_array(\n                    lower(grant_row.id::text),\n                    btrim(grant_row.publication_payload_sha256::text),\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(manifest.manifest_sha256::text) END,\n                    manifest.grant_id IS NOT NULL,\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(audit._publication_manifest_sha(\n                            ops._wp10_3_claim_manifest_payload(grant_row.id)\n                        )) END\n                )) FROM audit.claim_publication_grants AS grant_row\n                 LEFT JOIN audit.claim_publication_manifests AS manifest\n                   ON manifest.grant_id = grant_row.id\n                WHERE grant_row.grant_status = \'active\'::audit.grant_status\n                  AND manifest.grant_id IS NOT NULL\n                  AND NOT ops._wp10_3_grant_has_unresolved_quarantine(\n                      \'claim_publication_grants\', grant_row.id\n                  )\n                  AND ops._v133_full_rebuild_grant_eligible(\'claim_publication_grants\', grant_row.id)\n) AS digest_items;\n\n$v133_digest$;',
    "CREATE FUNCTION ops._v133_full_input_digest_legacy_v1() RETURNS text LANGUAGE sql STABLE SET search_path=ops,audit,core,public,pg_catalog SET timezone='UTC' AS $v133_digest$\nSELECT audit._payload_sha256(jsonb_build_object(\n                'document', coalesce((SELECT jsonb_agg(jsonb_build_array(\n                    lower(grant_row.id::text),\n                    btrim(grant_row.publication_payload_sha256::text),\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(manifest.manifest_sha256::text) END,\n                    manifest.grant_id IS NOT NULL,\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(audit._publication_manifest_sha(\n                            ops._wp10_3_document_manifest_payload(grant_row.id)\n                        )) END\n                ) ORDER BY grant_row.id)\n                FROM audit.document_publication_grants AS grant_row\n                 LEFT JOIN audit.document_publication_manifests AS manifest\n                   ON manifest.grant_id = grant_row.id\n                WHERE grant_row.grant_status = 'active'::audit.grant_status\n                  AND manifest.grant_id IS NOT NULL\n                  AND NOT ops._wp10_3_grant_has_unresolved_quarantine(\n                      'document_publication_grants', grant_row.id\n                  )\n                  AND ops._v133_full_rebuild_grant_eligible('document_publication_grants', grant_row.id)), '[]'::jsonb),\n                'entity', coalesce((SELECT jsonb_agg(jsonb_build_array(\n                    lower(grant_row.id::text),\n                    btrim(grant_row.publication_payload_sha256::text),\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(manifest.manifest_sha256::text) END,\n                    manifest.grant_id IS NOT NULL,\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(audit._publication_manifest_sha(\n                            ops._wp10_3_entity_manifest_payload(grant_row.id)\n                        )) END\n                ) ORDER BY grant_row.id)\n                FROM audit.entity_publication_grants AS grant_row\n                 LEFT JOIN audit.entity_publication_manifests AS manifest\n                   ON manifest.grant_id = grant_row.id\n                WHERE grant_row.grant_status = 'active'::audit.grant_status\n                  AND manifest.grant_id IS NOT NULL\n                  AND NOT ops._wp10_3_grant_has_unresolved_quarantine(\n                      'entity_publication_grants', grant_row.id\n                  )\n                  AND ops._v133_full_rebuild_grant_eligible('entity_publication_grants', grant_row.id)), '[]'::jsonb),\n                'claim', coalesce((SELECT jsonb_agg(jsonb_build_array(\n                    lower(grant_row.id::text),\n                    btrim(grant_row.publication_payload_sha256::text),\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(manifest.manifest_sha256::text) END,\n                    manifest.grant_id IS NOT NULL,\n                    CASE WHEN manifest.grant_id IS NULL THEN NULL ELSE\n                        btrim(audit._publication_manifest_sha(\n                            ops._wp10_3_claim_manifest_payload(grant_row.id)\n                        )) END\n                ) ORDER BY grant_row.id)\n                FROM audit.claim_publication_grants AS grant_row\n                 LEFT JOIN audit.claim_publication_manifests AS manifest\n                   ON manifest.grant_id = grant_row.id\n                WHERE grant_row.grant_status = 'active'::audit.grant_status\n                  AND manifest.grant_id IS NOT NULL\n                  AND NOT ops._wp10_3_grant_has_unresolved_quarantine(\n                      'claim_publication_grants', grant_row.id\n                  )\n                  AND ops._v133_full_rebuild_grant_eligible('claim_publication_grants', grant_row.id)), '[]'::jsonb)\n            ));\n$v133_digest$;",
    'CREATE FUNCTION ops._v133_scoped_input_digest_v2(p_document_id uuid,p_header jsonb) RETURNS text LANGUAGE sql STABLE SET search_path=ops,audit,core,public,pg_catalog SET timezone=\'UTC\' AS $v133_digest$\nSELECT ops._v133_rebuild_digest_v2(domain,item_sha256 ORDER BY section,kind,stable_key) FROM (\nSELECT 0 AS section,0 AS kind,\'00000000-0000-0000-0000-000000000000\'::uuid AS stable_key,\'seed\'::text AS domain,audit._payload_sha256(\'{"schema":"publication-scoped-input-digest.v2","sections":["header","claims"]}\'::jsonb) AS item_sha256\nUNION ALL\nSELECT 1,0,\'00000000-0000-0000-0000-000000000000\'::uuid,\'header\',audit._payload_sha256(\'{"schema":"publication-scoped-input-digest.v2","section":"header","marker":"begin"}\'::jsonb)\nUNION ALL\nSELECT 1,2,\'00000000-0000-0000-0000-000000000000\'::uuid,\'header\',audit._payload_sha256(\'{"schema":"publication-scoped-input-digest.v2","section":"header","marker":"end"}\'::jsonb)\nUNION ALL\nSELECT 1,1,p_document_id,\'header\',audit._payload_sha256(p_header) FROM (SELECT 1) AS scoped_header\nUNION ALL\nSELECT 2,0,\'00000000-0000-0000-0000-000000000000\'::uuid,\'claims\',audit._payload_sha256(\'{"schema":"publication-scoped-input-digest.v2","section":"claims","marker":"begin"}\'::jsonb)\nUNION ALL\nSELECT 2,2,\'00000000-0000-0000-0000-000000000000\'::uuid,\'claims\',audit._payload_sha256(\'{"schema":"publication-scoped-input-digest.v2","section":"claims","marker":"end"}\'::jsonb)\nUNION ALL\nSELECT 2,1,claim_grant.id,\'claims\',audit._payload_sha256(jsonb_build_array(\n                        lower(claim_grant.id::text),\n                        claim_grant.revision_no,\n                        btrim(claim_manifest.manifest_sha256::text)\n                    )) FROM audit.claim_publication_grants AS claim_grant\n                      JOIN audit.claim_publication_manifests AS claim_manifest\n                        ON claim_manifest.grant_id = claim_grant.id\n                     WHERE claim_grant.grant_status = \'active\'::audit.grant_status\n                       AND claim_manifest.document_id = p_document_id\n                       AND EXISTS (\n                           SELECT 1 FROM ops.outbox_events AS claim_event\n                            WHERE claim_event.aggregate_type = \'claim_publication_grants\'\n                              AND claim_event.aggregate_id = claim_grant.id\n                              AND claim_event.event_type = \'publication.granted\'\n                              AND claim_event.published_at IS NOT NULL\n                              AND claim_event.terminal_at IS NULL\n                       )\n) AS digest_items;\n$v133_digest$;',
    "CREATE FUNCTION ops._v133_scoped_input_digest_legacy_v1(p_document_id uuid,p_header jsonb) RETURNS text LANGUAGE sql STABLE SET search_path=ops,audit,core,public,pg_catalog SET timezone='UTC' AS $v133_digest$\nSELECT audit._payload_sha256(p_header || jsonb_build_object('claims',coalesce((SELECT jsonb_agg(jsonb_build_array(\n                        lower(claim_grant.id::text),\n                        claim_grant.revision_no,\n                        btrim(claim_manifest.manifest_sha256::text)\n                    ) ORDER BY claim_grant.id) FROM audit.claim_publication_grants AS claim_grant\n                      JOIN audit.claim_publication_manifests AS claim_manifest\n                        ON claim_manifest.grant_id = claim_grant.id\n                     WHERE claim_grant.grant_status = 'active'::audit.grant_status\n                       AND claim_manifest.document_id = p_document_id\n                       AND EXISTS (\n                           SELECT 1 FROM ops.outbox_events AS claim_event\n                            WHERE claim_event.aggregate_type = 'claim_publication_grants'\n                              AND claim_event.aggregate_id = claim_grant.id\n                              AND claim_event.event_type = 'publication.granted'\n                              AND claim_event.published_at IS NOT NULL\n                              AND claim_event.terminal_at IS NULL\n                       )), '[]'::jsonb)));\n$v133_digest$;",
)

DENIED_ROLES = (
    "uap_migrator",
    "uap_api",
    "uap_worker",
    "uap_scheduler",
    "uap_publisher",
    "uap_public_reader",
    "uap_model_governance",
    "uap_audit_reader",
    "uap_backup",
)


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql(*, upgrade: bool) -> str:
    expected = BASELINE_SHA256 if upgrade else V2_SHA256
    resulting = V2_SHA256 if upgrade else BASELINE_SHA256
    preflight = []
    changes = []
    for index, signature in enumerate(SIGNATURES):
        sig = _literal(signature)
        preflight.append(f"""
            SELECT p.* INTO original FROM pg_proc AS p
             WHERE p.oid=to_regprocedure({sig});
            IF NOT FOUND OR original.proowner <> 'uap_owner'::regrole
               OR encode(sha256(convert_to(pg_get_functiondef(original.oid), 'UTF8')),
                         'hex') IS DISTINCT FROM {_literal(expected[index])}
            THEN
                RAISE EXCEPTION 'v133_bounded_digest_definition_mismatch: %', {sig};
            END IF;
            original_states := original_states || jsonb_build_object({sig},to_jsonb(original));
            original_comments := original_comments || jsonb_build_object(
                {sig},obj_description(original.oid,'pg_proc'));
        """)
        patch = []
        for old, new in REPLACEMENTS[index]:
            before, after = (old, new) if upgrade else (new, old)
            patch.append(f"rewritten := replace(rewritten, {_literal(before)}, {_literal(after)});")
        changes.append(f"""
            definition := pg_get_functiondef(to_regprocedure({sig}));
            rewritten := definition;
            {"".join(patch)}
            EXECUTE rewritten;
            SELECT p.* INTO changed FROM pg_proc AS p WHERE p.oid=to_regprocedure({sig});
            IF encode(sha256(convert_to(pg_get_functiondef(changed.oid),'UTF8')),'hex')
                    IS DISTINCT FROM {_literal(resulting[index])}
               OR (to_jsonb(changed)-'prosrc') IS DISTINCT FROM
                    ((original_states -> {sig})-'prosrc')
               OR obj_description(changed.oid,'pg_proc') IS DISTINCT FROM
                    (original_comments ->> {sig})
            THEN
                RAISE EXCEPTION 'v133_bounded_digest_postcondition_mismatch: %', {sig};
            END IF;
        """)
    if upgrade:
        ddl = [
            "ALTER TABLE audit.publication_rebuild_runs ADD COLUMN digest_version smallint NOT NULL DEFAULT 1 CONSTRAINT publication_rebuild_digest_version CHECK (digest_version IN (1,2))",
            *HELPER_DDL,
        ]
        for signature in HELPER_SIGNATURES:
            ddl.append(
                f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, " + ", ".join(DENIED_ROLES)
            )
        before_changes = "\n".join("EXECUTE " + _literal(statement) + ";" for statement in ddl)
        after_changes = ""
    else:
        before_changes = """
            IF EXISTS (SELECT 1 FROM audit.publication_rebuild_runs WHERE digest_version=2) THEN
                RAISE EXCEPTION 'publication_rebuild_v2_state_blocks_downgrade'
                    USING ERRCODE='55000';
            END IF;
        """
        drops = [
            "DROP FUNCTION " + signature
            for signature in reversed(HELPER_SIGNATURES)
            if signature != "ops._v133_rebuild_digest_v2(text,text)"
        ]
        # Fold must remain until the aggregate has been removed.
        drops.remove("DROP FUNCTION ops._v133_rebuild_digest_fold_v2(text,text,text)")
        drops += [
            "DROP AGGREGATE ops._v133_rebuild_digest_v2(text,text)",
            "DROP FUNCTION ops._v133_rebuild_digest_fold_v2(text,text,text)",
            "ALTER TABLE audit.publication_rebuild_runs DROP COLUMN digest_version",
        ]
        after_changes = "\n".join("EXECUTE " + _literal(statement) + ";" for statement in drops)
    return f"""
        SET ROLE uap_owner;
        DO $v133_bounded_digest$
        DECLARE
            original pg_proc%ROWTYPE;
            changed pg_proc%ROWTYPE;
            original_states jsonb := '{{}}'::jsonb;
            original_comments jsonb := '{{}}'::jsonb;
            definition text;
            rewritten text;
        BEGIN
            {"".join(preflight)}
            {before_changes}
            {"".join(changes)}
            {after_changes}
        END;
        $v133_bounded_digest$;
        RESET ROLE;
    """


def upgrade() -> None:
    op.execute(_sql(upgrade=True))


def downgrade() -> None:
    op.execute(_sql(upgrade=False))
