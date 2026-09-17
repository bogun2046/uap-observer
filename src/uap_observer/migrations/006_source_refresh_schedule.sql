ALTER TABLE sources ADD COLUMN refresh_interval_hours INTEGER NOT NULL DEFAULT 24 CHECK (refresh_interval_hours >= 1);
