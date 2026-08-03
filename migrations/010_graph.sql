-- First graph-ready vocabulary. Existing news/entity links remain in the
-- polymorphic relationships table; these tables add tag assignments and
-- reviewable person-to-person candidates without losing source evidence.

CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    tag_type TEXT NOT NULL DEFAULT 'topic' CHECK (
        tag_type IN ('topic', 'role', 'geography', 'event', 'source')
    ),
    description TEXT,
    parent_id INTEGER,
    created_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (parent_id) REFERENCES tags(id) ON DELETE SET NULL
);

CREATE TABLE tag_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_id INTEGER NOT NULL,
    entity_type TEXT NOT NULL CHECK (
        entity_type IN ('news', 'event', 'person', 'organization')
    ),
    entity_id INTEGER NOT NULL,
    source_news_id INTEGER,
    confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0),
    method TEXT NOT NULL DEFAULT 'ai_extracted' CHECK (
        method IN ('ai_extracted', 'human_reviewed', 'co_occurrence')
    ),
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'corroborated', 'verified', 'disputed')
    ),
    created_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE,
    FOREIGN KEY (source_news_id) REFERENCES news(id) ON DELETE SET NULL,
    UNIQUE (tag_id, entity_type, entity_id, source_news_id)
);

CREATE TABLE person_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_person_id INTEGER NOT NULL,
    target_person_id INTEGER NOT NULL,
    relationship_type TEXT NOT NULL,
    confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0),
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'corroborated', 'verified', 'disputed')
    ),
    method TEXT NOT NULL DEFAULT 'ai_extracted' CHECK (
        method IN ('ai_extracted', 'human_reviewed', 'co_occurrence')
    ),
    first_seen_at TEXT,
    last_seen_at TEXT,
    created_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (source_person_id, target_person_id, relationship_type),
    CHECK (source_person_id <> target_person_id)
);

CREATE TABLE person_relationship_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_relationship_id INTEGER NOT NULL,
    news_id INTEGER NOT NULL,
    evidence_text TEXT,
    created_time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (person_relationship_id) REFERENCES person_relationships(id) ON DELETE CASCADE,
    FOREIGN KEY (news_id) REFERENCES news(id) ON DELETE CASCADE,
    UNIQUE (person_relationship_id, news_id)
);

CREATE INDEX idx_tags_type ON tags(tag_type);
CREATE INDEX idx_tag_assignments_entity ON tag_assignments(entity_type, entity_id);
CREATE INDEX idx_person_relationships_source ON person_relationships(source_person_id);
CREATE INDEX idx_person_relationships_target ON person_relationships(target_person_id);
CREATE INDEX idx_person_relationship_evidence_news ON person_relationship_evidence(news_id);
