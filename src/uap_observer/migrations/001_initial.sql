CREATE TABLE news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    original_title TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL UNIQUE,
    canonical_url TEXT,
    publish_date TEXT,
    country TEXT,
    category TEXT NOT NULL CHECK (
        category IN (
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
    summary TEXT,
    credibility INTEGER NOT NULL CHECK (credibility BETWEEN 1 AND 5),
    fact_status TEXT NOT NULL CHECK (
        fact_status IN (
            'official_record',
            'corroborated',
            'source_reported',
            'unverified',
            'disputed',
            'opinion'
        )
    ),
    key_facts TEXT NOT NULL DEFAULT '[]',
    viewpoints TEXT NOT NULL DEFAULT '[]',
    raw_content TEXT,
    content_hash TEXT UNIQUE,
    ai_model TEXT,
    ai_processed_at TEXT,
    processing_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        processing_status IN ('pending', 'processing', 'completed', 'failed', 'skipped')
    ),
    created_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_name TEXT NOT NULL,
    date_start TEXT,
    date_end TEXT,
    location TEXT,
    country TEXT,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'unverified' CHECK (
        status IN ('official_record', 'corroborated', 'unverified', 'disputed')
    ),
    credibility INTEGER NOT NULL CHECK (credibility BETWEEN 1 AND 5),
    created_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE persons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    country TEXT,
    organization TEXT,
    description TEXT,
    created_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL CHECK (
        source_type IN ('news', 'event', 'person', 'organization')
    ),
    source_id INTEGER NOT NULL,
    target_type TEXT NOT NULL CHECK (
        target_type IN ('news', 'event', 'person', 'organization')
    ),
    target_id INTEGER NOT NULL,
    relationship_type TEXT NOT NULL,
    evidence_news_id INTEGER,
    confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0),
    created_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (evidence_news_id) REFERENCES news(id) ON DELETE SET NULL,
    UNIQUE (source_type, source_id, target_type, target_id, relationship_type)
);

CREATE INDEX idx_news_publish_date ON news(publish_date);
CREATE INDEX idx_news_category ON news(category);
CREATE INDEX idx_news_fact_status ON news(fact_status);
CREATE INDEX idx_news_processing_status ON news(processing_status);
CREATE INDEX idx_events_date_start ON events(date_start);
CREATE INDEX idx_events_country ON events(country);
CREATE INDEX idx_persons_name ON persons(name);
CREATE INDEX idx_relationships_source ON relationships(source_type, source_id);
CREATE INDEX idx_relationships_target ON relationships(target_type, target_id);
