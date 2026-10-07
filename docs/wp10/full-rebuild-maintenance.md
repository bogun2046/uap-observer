# WP10.6 Full Projection Rebuild Maintenance Contract

`ops.rebuild_public_projection(rebuild_id uuid)` is an administrative full-recovery operation, not an online publication path. Run it only during an approved, quiesced maintenance window:

1. Stop new public API and public-reader requests.
2. Drain existing reader requests and transactions.
3. Run the rebuild transaction and wait for `COMMIT` to succeed.
4. Restore public traffic only after commit.

Concurrent public reads are unsupported during this rebuild. The reset uses `TRUNCATE`, which takes `ACCESS EXCLUSIVE` locks and is not safe for concurrent readers or old snapshots. The database function cannot prove that external HTTP ingress is quiesced; the operator owns this precondition.

The rebuild locks `public.relations` and `public.relation_evidence`, then fails closed if either table contains rows. It uses one explicit `TRUNCATE TABLE ONLY` statement for the reviewed nine-table projection set, with `RESTRICT`, without `CASCADE`, and with `CONTINUE IDENTITY`. It does not acknowledge pending publication events; events remain idempotently applicable after the transaction.

`uap_migrator` may execute the approved `SECURITY DEFINER` rebuild function but receives no direct public projection table privileges, including `TRUNCATE`. The function owner, ACL, `SECURITY DEFINER`, and fixed `search_path` remain unchanged by migration 0038.
