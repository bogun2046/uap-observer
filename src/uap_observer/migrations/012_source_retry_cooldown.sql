ALTER TABLE sources ADD COLUMN next_retry_at TEXT;
ALTER TABLE sources ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0;
