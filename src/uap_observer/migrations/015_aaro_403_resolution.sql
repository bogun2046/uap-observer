UPDATE news
SET extraction_status = 'pending',
    extraction_started_at = NULL,
    extraction_error = 'Requeued by migration 015 for AARO HTTP 403 resolution',
    updated_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE source IN (
        'AARO Congressional and Press Products',
        'AARO Official UAP Imagery'
    )
  AND extraction_status = 'failed'
  AND processing_status = 'pending'
  AND extraction_error LIKE '%403%';
