CREATE TABLE source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'success', 'not_modified', 'empty', 'failed')
    ),
    http_status INTEGER,
    fetched_count INTEGER NOT NULL DEFAULT 0,
    parsed_count INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    filtered_count INTEGER NOT NULL DEFAULT 0,
    invalid_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_time TEXT,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX idx_source_runs_source_time
    ON source_runs(source_id, started_time DESC, id DESC);

CREATE INDEX idx_source_runs_status_time
    ON source_runs(status, started_time DESC, id DESC);
