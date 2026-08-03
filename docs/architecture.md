# Architecture

## Phase 1 flow

```text
RSS / official APIs / monitored pages
                 |
                 v
        collection and cleaning
                 |
                 v
       deduplication and storage
                 |
                 v
       structured AI processing
                 |
                 v
              SQLite
                 |
                 v
       Markdown static publishing
```

The SQLite foundation, incremental RSS collector, article extraction queue, and
structured AI analysis queue are implemented. The Markdown publisher is also
implemented and emits source-linked static pages.

## Design decisions

### Standard-library data layer

The MVP uses `sqlite3` and SQL migration files. This keeps local setup small,
makes the schema explicit, and avoids binding the domain model to an ORM.
Forward-only migrations provide a controlled path for later schema changes.
The migration SQL and default source registry are packaged under
`uap_observer` so installed CLI runs resolve resources from the package rather
than from a repository-relative path.

### Raw and derived content

The `news` table stores source metadata and optional raw content separately from
translated titles, summaries, key facts, viewpoints, and AI processing
metadata. A future processor can re-run a newer model without re-collecting an
article.

### Source credibility versus claim state

The integer `credibility` records a source-level assessment. It does not claim
that every statement in the source is true. `fact_status` provides a separate,
controlled description of the information state:

- `official_record`
- `corroborated`
- `source_reported`
- `unverified`
- `disputed`
- `opinion`

### Typed relationships

Relationships use `source_type/source_id` and `target_type/target_id` so an
event can connect to a person, news item, or organization. Since these are
polymorphic references, SQLite cannot enforce a direct foreign key for every
pair. Application validation will enforce entity existence when the knowledge
graph module is expanded.

`evidence_news_id` is a conventional foreign key and records the public item
supporting a relationship.

### SQLite operation

Every connection enables foreign-key checks and WAL mode. Database files and
WAL sidecar files are ignored by Git. Schema migrations and tests are committed,
so a clean database is reproducible.

### Source registry and incremental fetching

`config/sources.json` is the reviewable source of truth. `sync-sources` upserts
these definitions into the `sources` table without erasing fetch state.

Each source stores:

- feed and homepage URLs
- default category, credibility, and fact status
- inclusion and exclusion keywords
- ETag and Last-Modified validators
- latest fetch, success, and error state

RSS collection uses conditional requests. Entries are normalized before insert,
and unique database indexes protect both canonical URLs and
`(source_id, feed_entry_id)` pairs.

The HTTP adapter first uses Python's standard client and falls back to curl for
servers that terminate chunked responses. The fallback accepts curl's status
marker on either stderr or stdout and removes it before XML parsing.

NASA's general Feed is not treated as UAP-only. Items must match configured UAP
phrases, while machine-learning uses of “UAP” are explicitly excluded. A
successful fetch containing no matching item is therefore a valid daily result.

### Article extraction

Only news items accepted by source filtering enter the extraction queue.
Trafilatura converts source HTML into cleaned article text and metadata without
changing the RSS excerpt.

The queue records:

- pending, processing, completed, failed, and skipped states
- attempt count, start time, completion time, and bounded errors
- extracted title, author, date, language, and extractor version
- SHA-256 content hash for exact-content duplicate detection

A compare-and-update claim prevents two workers from handling the same pending
item. Processing claims older than one hour are returned to the pending queue so
an interrupted scheduled job cannot leave articles permanently stuck.

Trafilatura is isolated behind an adapter. Version 1.12.2 is pinned for Python
3.9 compatibility; the adapter can move to the current 2.x API after the
project runtime is upgraded to Python 3.10+.

### Structured AI analysis

Only rows with completed article extraction enter the AI queue. The queue
reuses `processing_status` and adds attempt, start, error, schema version,
response ID, model, confidence, risk flags, and canonical JSON audit fields.
Claims are compare-and-update claimed, failed items require explicit retry, and
stale processing claims are recovered after one hour.

`ArticleAnalysis` is a strict Pydantic schema. Unknown fields, unsupported
controlled values, out-of-range confidence, empty list entries, and duplicate
list entries are rejected before persistence. The model produces:

- Simplified Chinese title and summary
- controlled category and fact status
- directly supported key facts and attributed viewpoints
- named persons, organizations, and related events
- analysis confidence and controlled risk flags

The prompt permits only supplied source text, preserves attribution and
uncertainty, and distinguishes the existence of an official record from the
truth of claims inside it. Source credibility remains deterministic metadata
and is never overwritten by model output.

The OpenAI adapter uses the non-streaming Responses API Structured Outputs
helper, disables response storage, and reads credentials only from
`OPENAI_API_KEY`. The cost-oriented daily default is `gpt-5.6-luna` with low
reasoning effort, while environment and CLI overrides support controlled model
migrations.

### Markdown publishing

`MarkdownPublisher` reads source-filtered news, including queued records that
have not completed AI analysis, and dated historical events.
It creates a deterministic homepage, news index, individual news detail pages,
historical events index, and timeline. Detail pages include the original source
link, but never include `extracted_content` or `raw_content`. Empty queues still
produce valid pages with an explicit empty-state message. Generated files are
written to `site/generated/`; the daily GitHub Actions workflow uploads that
directory as the GitHub Pages artifact.

### Scheduled execution

`.github/workflows/daily-uap.yml` runs at 09:15 Asia/Shanghai and supports a
manual dispatch. It initializes or migrates SQLite, syncs sources, collects RSS,
extracts articles, conditionally runs AI analysis when `OPENAI_API_KEY` exists,
publishes Markdown, checkpoints the WAL, commits the database, and deploys the
generated directory to GitHub Pages. The committed SQLite file is a deliberate
Phase 1 persistence bridge; Supabase PostgreSQL should replace it before the
database becomes large or receives concurrent writers.

### Official HTML page collection

Some official sources publish release tables without RSS. `AaroCollector`
fetches the AARO Congressional/Press Products page, extracts linked table rows,
normalizes relative links, preserves year or full publication dates, and inserts
each linked report as a source-reported queue item for later extraction and AI
analysis. It uses ETag/Last-Modified validators, a stable URL hash as the feed
entry ID, and the same canonical URL deduplication as RSS. It intentionally
stores the source link and table description only; it does not download PDF
content during collection.

The scheduled web-page step is non-blocking because AARO may return an edge
HTTP 403 to automated runners. Such a failure remains visible in the Actions
log and does not create a synthetic record; RSS collection and publishing
continue independently.

The case-resolution variant uses the first link in each row as the official
resolution document, keeps the remaining text as the source description, and
creates a matching `events` record keyed by case name and extracted start date.
Video links in the same row remain in the source page but are not downloaded or
treated as independent claims.

### Entity linking

`link-entities` consumes only completed, schema-validated `analysis_json`. It
creates or reuses case-insensitive `persons`, `organizations`, and `events`,
then adds unique relationships from each news item with the analysis
confidence and `evidence_news_id`. AI-inferred events start as `unverified`
and include a human-review note. The operation is idempotent, so each daily
run can safely reprocess completed analyses.

The Markdown publisher emits `persons/index.md`, `organizations.md`, and
`relationships.md`, `graph.md`, and `graph.json`, and adds related people,
organizations, and events to each news detail page. Relationship rows include
the evidence news title, relationship type, and confidence; empty graphs still
produce valid empty-state pages.

### Tags and person graph

The graph migration adds independent `tags` and `tag_assignments` tables, plus
reviewable `person_relationships` and `person_relationship_evidence` tables.
AI analysis may propose short topic tags and explicit person-to-person
relations, but the linker stores both as candidates with the source news ID and
evidence quote. A deterministic graph builder then adds statistical
co-occurrence edges for person pairs appearing together in at least two news
items. Explicit and co-occurrence edges have different `kind` values so the
static page can render them with different visual treatment. Tag membership is
a filter over people; it never silently becomes a person-to-person fact.

The graph page is static and loads `graph.json` in the browser. It uses
Cytoscape.js from a pinned public CDN version, so no graph server or graph
database is required for the current SQLite/GitHub Pages deployment. The
database can move to Supabase/PostgreSQL later because the graph tables are
included in `supabase/schema.sql` and the snapshot export order.

## Module layout

```text
src/uap_observer/
├── collectors/       # RSS, official API, and monitored-page adapters
├── processing/       # normalization, relevance, deduplication
├── ai_analysis.py    # schema-validated AI output and Responses API adapter
├── publishing.py     # Markdown generation
├── database.py
├── models.py
└── repositories.py
```

The next module should schedule collection, extraction, analysis, and Markdown
publishing through GitHub Actions.

### PostgreSQL migration bridge

`supabase/schema.sql` is a reviewed PostgreSQL baseline, not an automatic
production migration. It preserves numeric IDs for simpler SQLite import,
converts list/object fields to `jsonb`, keeps source/news evidence foreign keys,
and intentionally enables no anonymous/public policies. `export-json` writes a
deterministic snapshot of every current table so an operator can validate row
counts and sensitive fields before importing into Supabase.

`snapshot-to-sql` converts that snapshot into an explicit transaction of
`INSERT ... OVERRIDING SYSTEM VALUE` statements and sequence updates. It emits
review warnings and no destructive reset commands; the operator must review
the generated SQL and run it only against an isolated target database.
