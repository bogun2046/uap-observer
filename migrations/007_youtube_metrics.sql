CREATE TABLE youtube_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id INTEGER NOT NULL REFERENCES news(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL,
    captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    view_count INTEGER NOT NULL DEFAULT 0,
    like_count INTEGER NOT NULL DEFAULT 0,
    comment_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (video_id, captured_at)
);

CREATE INDEX idx_youtube_metrics_video_time
    ON youtube_metrics(video_id, captured_at DESC);
