# UAP Platform

This directory is the Python 3.12 engineering root for the target platform.
It is intentionally isolated from the legacy `src/uap_observer` SQLite
application. WP2 provides infrastructure and CI only; WP3 will add the first
authoritative PostgreSQL migration under `alembic/versions/`.

From the repository root:

```bash
make dev
```

The command creates an ignored `platform/.env` containing random local-only
credentials, then starts hardened PostgreSQL 16.14, SeaweedFS 4.41 as the S3-
compatible object store, idempotent bucket initialization, migrations, and the
readiness service. Open `http://localhost:8080/healthz` after startup.

Use `make dev-down` to stop containers without deleting data. `make dev-reset`
deletes only the named WP2 development volumes and requires an explicit
confirmation variable.
