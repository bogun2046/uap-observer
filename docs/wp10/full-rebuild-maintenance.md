# WP10.6 Full Projection Rebuild Maintenance Contract

`ops.rebuild_public_projection(rebuild_id uuid)` is an administrative full-recovery operation, not an online publication path. Run it only during an approved, quiesced maintenance window:

1. Stop new public API and public-reader requests.
2. Drain existing reader requests and transactions.
3. Run the rebuild transaction and wait for `COMMIT` to succeed.
4. Restore public traffic only after commit.

Concurrent public reads are unsupported during this rebuild. The reset uses `TRUNCATE`, which takes `ACCESS EXCLUSIVE` locks and is not safe for concurrent readers or old snapshots. The database function cannot prove that external HTTP ingress is quiesced; the operator owns this precondition.

The rebuild locks `public.relations` and `public.relation_evidence`, then fails closed if either table contains rows. It uses one explicit `TRUNCATE TABLE ONLY` statement for the reviewed nine-table projection set, with `RESTRICT`, without `CASCADE`, and with `CONTINUE IDENTITY`. It does not acknowledge pending publication events; events remain idempotently applicable after the transaction.

`uap_migrator` may execute the approved `SECURITY DEFINER` rebuild function but receives no direct public projection table privileges, including `TRUNCATE`. The function owner, ACL, `SECURITY DEFINER`, and fixed `search_path` remain unchanged by migration 0038.


## Digest version 2 (0039)

The failed 20261008 final 100K acceptance stopped during full rebuild with a PostgreSQL backend SIGKILL/OOM; capacity and freshness were not run. Independent read-only profiling subsequently proved that the legacy global projection digest alone could exhaust a 1 GiB diagnostic instance. Legacy input digest measured approximately 505 MB RSS and 923 seconds. This remediation does not rerun final acceptance and does not modify PR #47.

New full and document-scoped rebuild runs explicitly write `digest_version=2`. Both input and result use v2; v2 is a new algorithm and is not numerically equal to v1. Each logical row is canonicalized and SHA256-hashed separately. The custom ordered aggregate sorts only section, stable key and fixed-size hash inputs, then folds a 64-character SHA256 state using `publication-rebuild-digest-fold.v2`, previous hash, section/domain and item hash. PostgreSQL text value size is 68 bytes; payload processing is bounded by one row, and sort may spill under the existing work_mem.

Projection sections are documents, entities, claims, evidence, claim_evidence, document_entities and search. Full input sections are document, entity and claim. Scoped input folds the original document header and published claims. Fixed schema/section seeds and begin/end markers distinguish empty sections. Stable keys and section order are explicit in the aggregate call. The covered fields and publication eligibility predicates remain those of 0038; no published_at, revised_at or basis_relation_id fields are added to the projection digest.

Existing audit rows remain version1. Same-ID replay acquires the existing advisory/grant/run locks, selects the stored version, computes its corresponding current input, compares for conflicts and returns the stored historical result digest. The clearly named legacy-v1 input helpers are used only for existing version1 replay. **New runs are bounded v2; legacy v1 replay retains legacy digest computation and its memory/runtime characteristics.** Historical input/result digests are not rewritten. digest_version is immutable alongside rebuild_id, input_digest and started_at.

0039 downgrade is permitted only when no version2 audit row exists. Any version2 history fails closed with `publication_rebuild_v2_state_blocks_downgrade`; no rows are deleted, digest values rewritten or versions automatically converted. Four formal function definitions, owner/ACL/security/configuration and maintenance comments are checked before and after transformations.

The 0038 TRUNCATE scope, relation fail-close, advisory lock ordering, authorization, deferred integrity and atomic transaction boundary remain unchanged. Full rebuild still requires an approved quiesced maintenance window through COMMIT. The frozen 23-step historical runtime remains on its original 0019–0024 topology. Current-head validation independently targets 0039 and checks digest metadata/private-helper ACLs as well as hardened membership and complete runtime.

Pre-migration database-only 100K prototypes completed under the unchanged 1 GiB diagnostic cap: full input v2 peak RSS 173,953,024 bytes, 906.33 seconds; projection v2 peak RSS 115,265,536 bytes, 8.76 seconds. Neither was cancelled or caused a new OOM. Input runtime remains dominated by unchanged eligibility/manifest work, so no substantial latency improvement is claimed. The cap is a diagnostic regression gate, not a formal product memory specification. Full diagnostic rebuild and the CI result are recorded separately from these prototype measurements.
