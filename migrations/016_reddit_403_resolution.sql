UPDATE news
SET extraction_status = 'pending',
    extraction_started_at = NULL,
    extraction_error = 'Requeued by migration 016 for Reddit HTTP 403 resolution',
    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE source IN (
        'Reddit r/UFOs',
        'Reddit r/aliens',
        'Reddit r/HighStrangeness'
    )
  AND extraction_status = 'failed'
  AND processing_status = 'pending'
  AND extraction_error LIKE '%403%';
