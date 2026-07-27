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

Only the SQLite foundation is implemented in the initial module.

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

## Planned modules

```text
src/uap_observer/
├── collectors/       # RSS, official API, and monitored-page adapters
├── processing/       # normalization, relevance, deduplication
├── ai/               # schema-validated AI output
├── publishing/       # Markdown generation
├── database.py
├── models.py
└── repositories.py
```

The next database extension should introduce a `sources` table to manage feed
URLs, source types, default credibility, fetch timestamps, and enabled status.
