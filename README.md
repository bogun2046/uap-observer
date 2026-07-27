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
- `news`, `events`, `persons`, and `relationships` tables
- typed domain models and a small persistence layer
- automated database tests

RSS collection, AI analysis, Markdown publishing, and the website are the next
modules. No frontend is included yet.

## Project structure

```text
.
├── data/                       # Local SQLite file (ignored by Git)
├── docs/
│   ├── architecture.md         # Architecture and design decisions
│   └── project-status.md       # Phase checklist and current status
├── migrations/
│   └── 001_initial.sql         # Initial schema
├── src/uap_observer/
│   ├── cli.py                  # init-db and db-status commands
│   ├── config.py               # Environment-based configuration
│   ├── database.py             # SQLite connection and migrations
│   ├── models.py               # Domain models and controlled values
│   └── repositories.py         # Initial persistence operations
├── tests/
│   └── test_database.py
├── .env.example
├── .gitignore
└── pyproject.toml
```

## Run locally

No third-party runtime dependency is required for the current phase.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
uap-observer init-db
uap-observer db-status
```

Without installing the package, commands can be run from the repository root:

```bash
PYTHONPATH=src python3 -m uap_observer init-db
PYTHONPATH=src python3 -m uap_observer db-status
```

The default database is `data/uap.db`. Override it with either:

```bash
UAP_DB_PATH=/absolute/path/uap.db uap-observer init-db
uap-observer --database /absolute/path/uap.db init-db
```

## Run tests

The test suite uses Python's standard library:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
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
- Dates and timestamps use ISO 8601 text; generated timestamps are stored in UTC.
- Published pages should link to the original source and should not republish
  copyrighted article bodies.

See [architecture](docs/architecture.md) for the schema rationale and
[project status](docs/project-status.md) for the next work.
