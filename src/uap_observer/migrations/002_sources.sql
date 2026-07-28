CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('rss', 'api', 'web_page')),
    homepage_url TEXT NOT NULL,
    feed_url TEXT UNIQUE,
    country TEXT,
    language TEXT,
    default_category TEXT NOT NULL CHECK (
        default_category IN (
            'official_report',
            'government_document',
            'military',
            'scientific_research',
            'historical_event',
            'sighting',
            'disputed_event',
            'other'
        )
    ),
    default_credibility INTEGER NOT NULL CHECK (default_credibility BETWEEN 1 AND 5),
    default_fact_status TEXT NOT NULL CHECK (
        default_fact_status IN (
            'official_record',
            'corroborated',
            'source_reported',
            'unverified',
            'disputed',
            'opinion'
        )
    ),
    include_keywords TEXT NOT NULL DEFAULT '[]',
    exclude_keywords TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    etag TEXT,
    last_modified TEXT,
    last_fetched_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    created_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

ALTER TABLE news ADD COLUMN source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL;
ALTER TABLE news ADD COLUMN feed_entry_id TEXT;

CREATE INDEX idx_sources_enabled_type ON sources(enabled, source_type);
CREATE INDEX idx_news_source_id ON news(source_id);
CREATE UNIQUE INDEX idx_news_canonical_url_unique
    ON news(canonical_url)
    WHERE canonical_url IS NOT NULL;
CREATE UNIQUE INDEX idx_news_source_entry_unique
    ON news(source_id, feed_entry_id)
    WHERE source_id IS NOT NULL AND feed_entry_id IS NOT NULL;
