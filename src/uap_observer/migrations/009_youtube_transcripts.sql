ALTER TABLE news ADD COLUMN transcript_status TEXT NOT NULL DEFAULT 'not_requested'
    CHECK (transcript_status IN ('not_requested', 'pending', 'completed', 'skipped', 'failed'));
ALTER TABLE news ADD COLUMN transcript_tokens INTEGER;
CREATE INDEX idx_news_transcript_priority ON news(transcript_status, processing_status);
