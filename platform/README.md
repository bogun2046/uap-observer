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

## V1-1 local chain

V1-1 is an additive local profile in `compose.v1.yaml`; the frozen base
`compose.yaml` remains unchanged. Supply a valid DeepSeek key only through the
process environment, then start the single-source r/UFOs chain:

```bash
read -s DEEPSEEK_API_KEY
export DEEPSEEK_API_KEY
make v1-dev
```

The command uses the independent `uap-platform-v1-1` Compose project, records
the approved CNY 20 monthly hard limit, and exposes the authenticated internal
library only at `http://127.0.0.1:8091/`. Read the random local bearer token
from ignored `platform/.env`; never copy it or the DeepSeek key into tracked
files. `make v1-schedule` adds one idempotent controlled schedule window and
`make v1-down` stops V1 containers without deleting volumes.

Only r/UFOs is scheduled in V1-1. r/UAP and the two WAR.GOV Web Sources are
registered for later authorized stages. V1-1 never creates a publication grant
or writes the public projection.
