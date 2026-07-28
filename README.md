# UAP Observer / UAP观察

UAP Observer is a source-first database and publishing pipeline for public
information about unidentified anomalous phenomena (UAP).

The project is not an “alien disclosure” platform. It records where information
came from, distinguishes records from claims and opinions, and preserves enough
provenance for later review.

## Current scope

Phase 1 establishes the local data foundation:

- Python 3.9+ project and command-line interface
- SQLite database with forward-only SQL migrations
- `sources`, `news`, `events`, `persons`, and `relationships` tables
- typed domain models and a small persistence layer
- version-controlled source definitions
- incremental RSS/Atom collection with conditional HTTP requests
- URL normalization, keyword filtering, and duplicate prevention
- article-body and metadata extraction with a durable processing queue
- automated database tests

Structured AI analysis, Markdown publishing, and the website are the next
modules. No frontend is included yet.

## Project structure

```text
.
├── data/                       # Local SQLite file (ignored by Git)
├── config/
│   └── sources.json            # Auditable source and keyword definitions
├── docs/
│   ├── architecture.md         # Architecture and design decisions
│   └── project-status.md       # Phase checklist and current status
├── migrations/
│   └── 001_initial.sql         # Initial schema
├── src/uap_observer/
│   ├── cli.py                  # init-db and db-status commands
│   ├── collectors/rss.py       # RSS/Atom incremental collector
│   ├── article_extraction.py   # Article extraction queue and adapter
│   ├── config.py               # Environment-based configuration
│   ├── database.py             # SQLite connection and migrations
│   ├── models.py               # Domain models and controlled values
│   ├── repositories.py         # Persistence operations
│   ├── source_config.py        # Source configuration validation
│   └── url_utils.py            # Canonical URL normalization
├── tests/
│   └── test_database.py
├── .env.example
├── .gitignore
└── pyproject.toml
```

## Run locally

The project pins Trafilatura 1.12.2 because it supports the current Python 3.9
runtime. The current Trafilatura 2.x line requires Python 3.10 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
uap-observer init-db
uap-observer db-status
uap-observer sync-sources
uap-observer collect-rss --source nasa-recent
uap-observer extract-articles --limit 20
```

Without installing the package, commands can be run from the repository root:

```bash
PYTHONPATH=src python3 -m uap_observer init-db
PYTHONPATH=src python3 -m uap_observer db-status
PYTHONPATH=src python3 -m uap_observer sync-sources
PYTHONPATH=src python3 -m uap_observer collect-rss --source nasa-recent
```

Article extraction requires the installed project environment:

```bash
.venv/bin/uap-observer extract-articles --limit 20
.venv/bin/uap-observer extract-articles --limit 20 --retry-failed
```

Accepted RSS items begin with `extraction_status=pending`. The extractor claims
one item at a time, stores cleaned text and metadata, calculates a SHA-256
content hash, skips exact-content duplicates, and records bounded failure
messages. Processing tasks left incomplete for more than one hour are recovered
automatically.

`collect-rss` synchronizes `config/sources.json` before collection. Use
`--limit 30` for a bounded smoke run. A second run sends the stored ETag and
Last-Modified values; a source that has not changed reports `not modified`.

The collector uses Python's standard HTTP client first. If a server repeatedly
terminates HTTP/1.1 chunked transfer early, it can safely fall back to the
system `curl` executable using an argument list without a shell. This handles
NASA's current Feed behavior on macOS while keeping Python runtime dependencies
at zero.

The default database is `data/uap.db`. Override it with either:

```bash
UAP_DB_PATH=/absolute/path/uap.db uap-observer init-db
uap-observer --database /absolute/path/uap.db init-db
```

## Run tests

Run the full suite in the installed environment:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Optional developer tools can be installed with:

```bash
python -m pip install -e ".[dev]"
pytest
ruff check .
```

## Data rules

- Original source fields and AI-generated fields remain distinguishable.
- `credibility` is a 1–5 source assessment, not proof of a claim.
- `fact_status` identifies whether content is an official record, corroborated,
  source-reported, unverified, disputed, or opinion.
- Relationships may cite `evidence_news_id` and a confidence score.
- Feed entries are deduplicated by canonical URL and source entry ID.
- Extracted articles are deduplicated by SHA-256 content hash.
- Feed excerpts remain separate from extracted article text.
- Tracking parameters such as `utm_*`, `fbclid`, and `gclid` are removed before
  URL comparison.
- Short keywords such as `UAP` and `UFO` match whole tokens, avoiding words such
  as `startup`.
- Dates and timestamps use ISO 8601 text; generated timestamps are stored in UTC.
- Published pages should link to the original source and should not republish
  copyrighted article bodies. Extracted bodies are internal analysis material.

See [architecture](docs/architecture.md) for the schema rationale and
[project status](docs/project-status.md) for the next work.
