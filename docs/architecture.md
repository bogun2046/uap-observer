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

`MarkdownPublisher` reads only completed AI records and dated historical events.
It creates a deterministic homepage, news index, individual news detail pages,
historical events index, and timeline. Detail pages include the original source
link, but never include `extracted_content` or `raw_content`. Empty queues still
produce valid pages with an explicit empty-state message. Generated files are
written to `site/generated/`, which remains ignored until a deployment workflow
is defined.

### Scheduled execution

`.github/workflows/daily-uap.yml` runs at 09:15 Asia/Shanghai and supports a
manual dispatch. It initializes or migrates SQLite, syncs sources, collects RSS,
extracts articles, conditionally runs AI analysis when `OPENAI_API_KEY` exists,
publishes Markdown, checkpoints the WAL, commits the database, and deploys the
generated directory to GitHub Pages. The committed SQLite file is a deliberate
Phase 1 persistence bridge; Supabase PostgreSQL should replace it before the
database becomes large or receives concurrent writers.

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
