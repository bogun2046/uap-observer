ALTER TABLE news ADD COLUMN analysis_version TEXT;
ALTER TABLE news ADD COLUMN analysis_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE news ADD COLUMN analysis_started_at TEXT;
ALTER TABLE news ADD COLUMN analysis_error TEXT;
ALTER TABLE news ADD COLUMN analysis_json TEXT;
ALTER TABLE news ADD COLUMN analysis_response_id TEXT;
ALTER TABLE news ADD COLUMN analysis_confidence REAL CHECK (
    analysis_confidence IS NULL OR analysis_confidence BETWEEN 0.0 AND 1.0
);
ALTER TABLE news ADD COLUMN risk_flags TEXT NOT NULL DEFAULT '[]';

CREATE INDEX idx_news_analysis_queue
    ON news(processing_status, extraction_status, publish_date, id);
