ALTER TABLE news ADD COLUMN title_translation_status TEXT NOT NULL DEFAULT 'not_started'
    CHECK (title_translation_status IN ('not_started', 'processing', 'completed', 'failed'));
ALTER TABLE news ADD COLUMN title_translation_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE news ADD COLUMN title_translation_error TEXT;
ALTER TABLE news ADD COLUMN title_translation_model TEXT;
ALTER TABLE news ADD COLUMN title_translation_response_id TEXT;
ALTER TABLE news ADD COLUMN title_translation_last_attempt_at TEXT;

CREATE INDEX idx_news_title_translation_queue
    ON news(title_translation_status, publish_date, id);
