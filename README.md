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
- NASA, The Debrief, Reddit r/UFOs, and filtered Reddit r/aliens RSS sources
- AARO official press-products and case-resolution pages
- incremental RSS/Atom collection with conditional HTTP requests
- URL normalization, keyword filtering, and duplicate prevention
- article-body and metadata extraction with a durable processing queue
- source-grounded structured AI analysis through OpenAI or DeepSeek
- strict validation for Chinese summaries, fact state, entities, confidence,
  and risk flags
- deterministic Markdown generation for homepage, news, events, and timeline
- data-driven observatory-style homepage with source status, event map coverage,
  category distribution, and an explicit media-evidence empty state
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
uap-observer collect-web --source aaro-official-imagery --limit 30
uap-observer collect-web --source aaro-efoia --limit 30
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
Before a batch, verify the credential, API connection, and configured model with:

```bash
.venv/bin/uap-observer deepseek-health-check
```

The health check sends one `/models` request and never prints the key or its
suffix. The DeepSeek adapter uses non-streaming Chat Completions JSON Output;
the OpenAI adapter uses non-streaming Structured Outputs through the Responses
API and sets `store=False`.

Each result must pass a strict Pydantic schema before it is committed. The
processor writes the Chinese title and summary, category, fact status, key
facts, viewpoints, named entities, analysis confidence, risk flags, model,
response ID, prompt/schema version, and canonical JSON. Source credibility is
not changed by the model. Failed items remain auditable and are retried only
with `--retry-failed`; stale claims are recovered after one hour. Independent
title translation records status, attempts, sanitized errors, model, response
ID, and last-attempt time. A 401 or 403 stops the remaining batch and returns a
nonzero exit code.

Generate static pages after analysis:

```bash
.venv/bin/uap-observer publish-markdown --output site/generated
.venv/bin/uap-observer publish-markdown --output site/generated --date 2026-07-28
```

The output contains `index.md`, a category-grouped `news/index.md`, one detail page per source-filtered
news item, `events/index.md`, `persons/index.md`, `organizations.md`,
`relationships.md`, `graph.md`, `graph.json`, `timeline.md`, a metadata-only `search.json`, and a
client-side `search.md` page. The publisher also writes the shared dark
observatory stylesheet, reduced-motion interaction script, and social preview
asset under `site/generated/assets/`. Homepage counts and source health are
derived from the current database; missing coordinates or media attachments
are shown as explicit data-quality states rather than inferred or fabricated.
`site/generated/` is ignored
by default so local previews do not dirty Git; a later deployment workflow can
publish this directory as a build artifact.

After AI analysis, `link-entities` creates idempotent `news → persons`,
`news → organizations`, and `news → events` relationships from validated
`analysis_json`. It creates minimal entity records when needed, preserves the
AI confidence on each relationship, and marks newly inferred events and
person-to-person relations as pending human review. It also creates
source-backed topic tags. `graph.json` combines explicit relationship
candidates with person pairs that co-occur in at least two separate news
items; co-occurrence is labelled as a statistical association and is never
presented as a confirmed fact. The static `graph.md` page uses a pinned
Cytoscape.js CDN asset for tag filtering and evidence drill-down.

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

`.github/workflows/daily-uap.yml` starts daily at 06:30 Asia/Shanghai time so
the collection, analysis, and Pages deployment can finish in the 07:30
publication window. It can also be started with `workflow_dispatch`. Before
enabling it:

1. Add either the repository secret `DEEPSEEK_API_KEY` or `OPENAI_API_KEY`.
   When `AI_PROVIDER` is not explicitly set, the workflow selects DeepSeek if
   its key is present; set `AI_PROVIDER=openai` or `AI_PROVIDER=deepseek` to
   override that choice.
2. Enable GitHub Pages with **GitHub Actions** as the build source.
3. Optionally set repository variables `AI_PROVIDER`, `OPENAI_MODEL`,
   `DEEPSEEK_MODEL`, and `OPENAI_REASONING_EFFORT`.
   When DeepSeek is selected, the workflow runs `deepseek-health-check` before
   title translation or article analysis and stops if authentication, API
   connectivity, or model availability fails.
4. To enable YouTube metadata collection, add the `YOUTUBE_API_KEY` secret and
   set `YOUTUBE_CHANNEL_IDS` to a comma-separated list of channel IDs. The
   collector stores titles, descriptions, links, publish times, and daily
   view/like/comment snapshots. Videos below `YOUTUBE_HOT_VIEW_THRESHOLD`
   (default `100000`) are marked skipped so they do not enter extraction or AI
   analysis; it does not download or redistribute videos.
5. Optional captions require `YOUTUBE_OAUTH_TOKEN`. Only priority videos are
   requested, capped by `YOUTUBE_TRANSCRIPT_LIMIT` (default 5) and
   `YOUTUBE_TRANSCRIPT_MAX_TOKENS` (default 12000). Article extraction first
   uses the API-provided public video description as an explicitly labelled
   fallback; a successfully downloaded caption transcript then replaces that
   fallback. Unavailable captions are recorded as skipped and do not stop the
   workflow.

See [`docs/deployment.md`](docs/deployment.md) for the manual trigger and
Pages acceptance checklist.

The workflow installs the development extras and runs the full regression
suite before touching the database or collecting remote sources. A failing
test stops the scheduled publish. Individual source or AI-analysis failures
are reported in the Actions summary while the site continues to publish
available source records with pending-analysis labels.

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

The active RSS registry currently includes NASA and U.S. National Archives
updates, France's official GEIPAN feed, The Debrief, Metabunk's UFO
investigation forum, Reddit r/UFOs, Reddit r/aliens, and Reddit
r/HighStrangeness. The broad National Archives feed is restricted to explicit
UAP/UFO records phrases, while the GEIPAN and Metabunk feeds are already
topic-specific. The r/aliens and r/HighStrangeness sources
use strict UAP/UFO and evidence-oriented phrase filters rather than broad
paranormal or `NHI` matches, and retain source credibility 1 with
`source_reported` status. AARO's official Congressional/Press Products page is
registered as an official web-page source and is collected by `collect-web`.
If AARO's Akamai edge returns HTTP 403 to a scheduled runner, the workflow
keeps the warning visible and continues with RSS sources; it does not fabricate
records from an inaccessible page.
The case-resolution collector also creates an `events` row for each new case,
using the official assessment description and any date found in that text. The
event is created only after the source-linked news item is deduplicated.
AARO's Official UAP Imagery and EFOIA Reading Room are collected as separate
official document streams. Their credibility value identifies the provenance
of the publication; it does not turn an unresolved observation or quoted claim
into an officially verified explanation.
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

Article extraction supports normal HTML pages, official PDFs, and a fallback
for official release pages whose markup is not recognized by Trafilatura. AI
analysis is only run after readable source text has been extracted; failed
extractions remain visible with an explicit reason instead of being presented
as an empty AI summary.

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
