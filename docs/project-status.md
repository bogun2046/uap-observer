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
- [x] Add strict structured AI analysis schema
- [x] Add non-streaming OpenAI Responses API adapter
- [x] Ground summaries and fact states only in supplied source text
- [x] Add AI queue claims, bounded failures, explicit retry, and stale recovery
- [x] Persist model, response ID, schema version, confidence, risk flags, and JSON
- [x] Keep source credibility separate from AI analysis confidence
- [x] Add `analyze-articles` CLI and environment-based model configuration
- [x] Test successful, failed, retried, stale, and mocked provider paths
- [x] Generate deterministic Markdown homepage and news detail pages
- [x] Generate historical events index and timeline page
- [x] Ensure published pages link sources without exposing article bodies
- [x] Add `publish-markdown` CLI and empty-state output
- [x] Test Markdown links, metadata, timeline, and no-body publishing
- [x] Add daily GitHub Actions workflow with manual dispatch
- [x] Persist SQLite database between scheduled runners
- [x] Upload and deploy generated Markdown to GitHub Pages
- [x] Add The Debrief RSS source with keyword filtering
- [x] Register AARO official press products for the future web-page collector
- [x] Verify The Debrief RSS returns successfully through the HTTP fallback
- [x] Fix curl fallback parsing when status markers are written to stdout
- [x] Add AARO HTML table parser with relative-link normalization
- [x] Add AARO web-page collection CLI with conditional requests and deduplication
- [x] Add AARO web-page collection to the daily GitHub Actions workflow
- [x] Test AARO parsing, insertion, deduplication, and 304 handling
- [x] Record AARO runner HTTP 403 as a visible non-blocking limitation
- [x] Add AARO case-resolution page source
- [x] Create historical `events` rows from official case-resolution records
- [x] Preserve official case assessment descriptions as news summaries
- [x] Add idempotent entity-linking service for analyzed persons and events
- [x] Create evidence-backed `news` to `persons/events` relationships
- [x] Add first-class organizations and `news` to `organizations` relationships
- [x] Add `link-entities` CLI and daily workflow step
- [x] Test relationship creation, confidence, deduplication, and invalid JSON
- [x] Display related persons and events on news detail pages
- [x] Generate persons index and relationship evidence table
- [x] Generate organization index and render organization graph entities
- [x] Group published news by controlled category
- [x] Generate metadata-only search index and static search page
- [x] Test graph pages and detail-page entity rendering
- [x] Add Supabase PostgreSQL schema baseline
- [x] Add deterministic SQLite JSON snapshot export
- [x] Test snapshot table coverage and row preservation
- [x] Add reviewable snapshot-to-PostgreSQL SQL generation
- [x] Ensure generated import SQL contains no destructive reset statements

### Next

- [ ] Add more official and reputable RSS/API sources
- [x] Add client-side full-text search for published news
- [ ] Replace committed SQLite persistence with Supabase PostgreSQL

## Verification

The current baseline is verified with:

```bash
.venv/bin/python -m pytest
.venv/bin/uap-observer --database /tmp/uap-observer-test.db init-db
.venv/bin/uap-observer --database /tmp/uap-observer-test.db db-status
.venv/bin/uap-observer analyze-articles --limit 1
```

Expected database status after initialization:

```text
Schema version: 005_organizations.sql
sources: 0
news: 0
events: 0
persons: 0
organizations: 0
relationships: 0
```

After `sync-sources`, the current source count is `4`.

Current regression result: `34 passed`. The real local database is migrated to
`005_organizations.sql`; it currently contains four configured sources and
zero news rows. The empty-queue AI and Markdown CLI smoke tests complete without
requiring a key or making an API request. Markdown smoke output contains four
empty-state pages under `site/generated/`.
