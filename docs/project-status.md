# Project status

Last updated: 2026-07-28

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
- [x] Add `sources` table and version-controlled source definitions
- [x] Add NASA official RSS source
- [x] Implement RSS and Atom parsing
- [x] Add ETag and Last-Modified conditional requests
- [x] Add URL normalization and database-level duplicate prevention
- [x] Add UAP inclusion and machine-learning exclusion filters
- [x] Add bounded RSS collection CLI
- [x] Verify NASA live collection and immediate `not modified` repeat
- [x] Add article extraction migration and durable queue
- [x] Extract article text, title, author, publication date, and language
- [x] Add exact-content SHA-256 duplicate detection
- [x] Add failed-item retry and stale-processing recovery
- [x] Pin Python 3.9-compatible Trafilatura and urllib3 versions
- [x] Verify extraction against a live NASA article

### Next

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
Schema version: 003_article_extraction.sql
sources: 0
news: 0
events: 0
persons: 0
relationships: 0
```

After `sync-sources`, the current source count is `1`.
