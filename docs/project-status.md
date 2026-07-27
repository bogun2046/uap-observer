# Project status

Last updated: 2026-07-27

## Phase 1 — automated news system

### Completed

- [x] Initialize Python project structure
- [x] Add SQLite connection management
- [x] Add forward-only schema migration runner
- [x] Create `news` table
- [x] Create `events` table
- [x] Create `persons` table
- [x] Create typed `relationships` table
- [x] Add domain models and controlled status values
- [x] Add initial repository operations
- [x] Test initialization, idempotency, persistence, and constraints
- [x] Document architecture and local commands

### Next

- [ ] Add the `sources` table and source configuration import
- [ ] Implement RSS collection with conditional HTTP requests
- [ ] Add URL normalization and content-hash deduplication
- [ ] Add article text extraction
- [ ] Add structured AI analysis and output validation
- [ ] Generate Markdown pages
- [ ] Add a scheduled GitHub Actions workflow

## Verification

The current baseline is verified with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m uap_observer --database /tmp/uap.db init-db
PYTHONPATH=src python3 -m uap_observer --database /tmp/uap.db db-status
```

Expected database status after initialization:

```text
Schema version: 001_initial.sql
news: 0
events: 0
persons: 0
relationships: 0
```
