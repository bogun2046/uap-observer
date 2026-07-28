ALTER TABLE news ADD COLUMN extraction_status TEXT NOT NULL DEFAULT 'pending' CHECK (
    extraction_status IN ('pending', 'processing', 'completed', 'failed', 'skipped')
);
ALTER TABLE news ADD COLUMN extracted_content TEXT;
ALTER TABLE news ADD COLUMN extracted_title TEXT;
ALTER TABLE news ADD COLUMN extracted_author TEXT;
ALTER TABLE news ADD COLUMN extracted_publish_date TEXT;
ALTER TABLE news ADD COLUMN extracted_language TEXT;
ALTER TABLE news ADD COLUMN extracted_by TEXT;
ALTER TABLE news ADD COLUMN extraction_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE news ADD COLUMN extraction_started_at TEXT;
ALTER TABLE news ADD COLUMN content_extracted_at TEXT;
ALTER TABLE news ADD COLUMN extraction_error TEXT;

CREATE INDEX idx_news_extraction_queue
    ON news(extraction_status, publish_date, id);
