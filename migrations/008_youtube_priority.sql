ALTER TABLE youtube_metrics ADD COLUMN view_growth_24h INTEGER NOT NULL DEFAULT 0;
ALTER TABLE youtube_metrics ADD COLUMN priority INTEGER NOT NULL DEFAULT 0 CHECK (priority IN (0, 1));
