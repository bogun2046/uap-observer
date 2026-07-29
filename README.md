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
- NASA official RSS and The Debrief RSS sources
- AARO official press-products and case-resolution pages
- incremental RSS/Atom collection with conditional HTTP requests
- URL normalization, keyword filtering, and duplicate prevention
- article-body and metadata extraction with a durable processing queue
- source-grounded structured AI analysis through OpenAI or DeepSeek
- strict validation for Chinese summaries, fact state, entities, confidence,
  and risk flags
- deterministic Markdown generation for homepage, news, events, and timeline
- relationship graph pages for persons, events, and news evidence links
- PostgreSQL/Supabase schema baseline and reviewed SQLite JSON export
- automated database tests

The generated Markdown is ready for a GitHub Pages workflow. No frontend
framework or native App is included yet.

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
│   ├── 001_initial.sql         # Initial schema
│   ├── 002_sources.sql         # Managed source registry
│   ├── 003_article_extraction.sql
│   ├── 004_ai_analysis.sql     # AI queue audit and validation fields
│   └── 005_organizations.sql   # First-class organization entities
├── src/uap_observer/
│   ├── cli.py                  # Pipeline commands
│   ├── collectors/rss.py       # RSS/Atom incremental collector
│   ├── article_extraction.py   # Article extraction queue and adapter
│   ├── ai_analysis.py          # Structured analysis schema and adapter
│   ├── publishing.py           # Source-linked Markdown generator
│   ├── config.py               # Environment-based configuration
│   ├── database.py             # SQLite connection and migrations
│   ├── models.py               # Domain models and controlled values
│   ├── repositories.py         # Persistence operations
│   ├── source_config.py        # Source configuration validation
│   └── url_utils.py            # Canonical URL normalization
├── tests/
│   ├── test_ai_analysis.py
│   ├── test_article_extraction.py
│   ├── test_database.py
│   └── test_rss_collector.py
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
uap-observer source-status
uap-observer analysis-status
uap-observer collect-rss --source nasa-recent
uap-observer collect-web --source aaro-press-products --limit 20
uap-observer collect-web --source aaro-case-resolutions --limit 20
uap-observer extract-articles --limit 20
uap-observer analyze-articles --limit 10
uap-observer link-entities --limit 1000
uap-observer publish-markdown --output site/generated
uap-observer export-json --output /tmp/uap-snapshot.json
uap-observer snapshot-to-sql --input /tmp/uap-snapshot.json --output /tmp/uap-import.sql
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

AI analysis requires a provider API key only when the queue contains extracted
articles. Export it in the shell or a secret manager; do not put a real key in
Git:

```bash
export OPENAI_API_KEY="your-key"
export OPENAI_MODEL="gpt-5.6-luna"
.venv/bin/uap-observer analyze-articles --limit 10
.venv/bin/uap-observer analyze-articles --limit 10 --retry-failed
```

The daily-processing default is `gpt-5.6-luna` with low reasoning effort to
control cost. `OPENAI_MODEL` or `--model` can select another available model.
To use DeepSeek's OpenAI-compatible JSON API, set `AI_PROVIDER=deepseek`,
`DEEPSEEK_API_KEY`, and optionally `DEEPSEEK_MODEL=deepseek-v4-flash`.
The adapter uses non-streaming Structured Outputs through the Responses API and
sets `store=False`.

Each result must pass a strict Pydantic schema before it is committed. The
processor writes the Chinese title and summary, category, fact status, key
facts, viewpoints, named entities, analysis confidence, risk flags, model,
response ID, prompt/schema version, and canonical JSON. Source credibility is
not changed by the model. Failed items remain auditable and are retried only
with `--retry-failed`; stale claims are recovered after one hour.

Generate static pages after analysis:

```bash
.venv/bin/uap-observer publish-markdown --output site/generated
.venv/bin/uap-observer publish-markdown --output site/generated --date 2026-07-28
```

The output contains `index.md`, a category-grouped `news/index.md`, one detail page per source-filtered
news item, `events/index.md`, `persons/index.md`, `organizations.md`,
`relationships.md`, `timeline.md`, a metadata-only `search.json`, and a
client-side `search.md` page. `site/generated/` is ignored
by default so local previews do not dirty Git; a later deployment workflow can
publish this directory as a build artifact.

After AI analysis, `link-entities` creates idempotent `news → persons`,
`news → organizations`, and `news → events` relationships from validated
`analysis_json`. It creates minimal entity records when needed, preserves the
AI confidence on each relationship, and marks newly inferred events as
unverified pending human review.

Source-filtered news is published before AI analysis completes. These entries
show their source, fact status, and an explicit pending-analysis label; the
site never invents a summary or conclusion for them.

For Phase 3 migration preparation, `supabase/schema.sql` mirrors the current
SQLite model using PostgreSQL `jsonb` and identity keys. Export a reviewed
snapshot with `export-json`; importing it into Supabase remains an operator
step. `snapshot-to-sql` generates reviewable `INSERT` statements and never
generates `DROP` or destructive reset statements. No public RLS policy is
enabled by default.

## Daily GitHub Actions workflow

`.github/workflows/daily-uap.yml` runs daily at 09:15 Asia/Shanghai time and
can also be started with `workflow_dispatch`. Before enabling it:

1. Add either the repository secret `OPENAI_API_KEY` (with `AI_PROVIDER=openai`)
   or `DEEPSEEK_API_KEY` (with `AI_PROVIDER=deepseek`).
2. Enable GitHub Pages with **GitHub Actions** as the build source.
3. Optionally set repository variables `AI_PROVIDER`, `OPENAI_MODEL`,
   `DEEPSEEK_MODEL`, and `OPENAI_REASONING_EFFORT`.

See [`docs/deployment.md`](docs/deployment.md) for the manual trigger and
Pages acceptance checklist.

The workflow installs the development extras and runs the full regression
suite before touching the database or collecting remote sources. A failing
test stops the scheduled publish.

After Markdown generation, the workflow builds a Jekyll HTML site and uploads
the generated `_site` directory, so GitHub Pages receives `index.html` rather
than raw Markdown-only output.

The workflow persists `data/uap.db` back to the repository after a successful
run, then uploads the generated Markdown to GitHub Pages. The database remains
ignored for normal local development, but the workflow uses `git add -f` so
history survives between scheduled runners. Do not place API keys in the
database, source files, or committed workflow YAML.

`collect-rss` synchronizes `config/sources.json` before collection. Use
`--limit 30` for a bounded smoke run. A second run sends the stored ETag and
Last-Modified values; a source that has not changed reports `not modified`.

The active RSS registry currently includes NASA's recently published feed and
The Debrief's feed. AARO's official Congressional/Press Products page is
registered as an official web-page source and is collected by `collect-web`.
If AARO's Akamai edge returns HTTP 403 to a scheduled runner, the workflow
keeps the warning visible and continues with RSS sources; it does not fabricate
records from an inaccessible page.
The case-resolution collector also creates an `events` row for each new case,
using the official assessment description and any date found in that text. The
event is created only after the source-linked news item is deduplicated.
NASA's feed is filtered for UAP keywords because the feed also contains
unrelated space news. The Debrief is
assigned source credibility 4 and `source_reported`; those values describe the
source, not the truth of individual claims.

The collector uses Python's standard HTTP client first. If a server repeatedly
terminates HTTP/1.1 chunked transfer early, it can safely fall back to the
system `curl` executable using an argument list without a shell. This handles
NASA's current Feed behavior on macOS while keeping Python runtime dependencies
to zero.

Use `source-status` to inspect whether each configured source is enabled and
when it was last fetched successfully. Fetch errors are retained in SQLite for
scheduled-run diagnostics.

The default database is `data/uap.db`. Override it with either:

```bash
UAP_DB_PATH=/absolute/path/uap.db uap-observer init-db
uap-observer --database /absolute/path/uap.db init-db
```

The installed wheel includes the SQL migrations and default source registry as
package data, so `init-db` and `sync-sources` do not depend on the repository
working directory. The database remains an operator-selected runtime path.

## Run tests

Run the full suite in the installed environment:

```bash
.venv/bin/python -m pytest
```

Optional developer tools can be installed with:

```bash
python -m pip install -e ".[dev]"
pytest
ruff check .
```

## Data rules

- Original source fields and AI-generated fields remain distinguishable.
- AI output is grounded only in supplied source text and validated before save.
- `credibility` is a 1–5 source assessment, not proof of a claim.
- `analysis_confidence` measures extraction quality, not truth of extraordinary
  claims.
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
