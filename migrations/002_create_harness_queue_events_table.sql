-- Create harness.queue_events table for queue event logging
-- This table stores events related to queue entries

CREATE TABLE IF NOT EXISTS harness.queue_events (
    host VARCHAR(256) NOT NULL,
    queue_id VARCHAR(128) NOT NULL,
    ts DOUBLE PRECISION NOT NULL,
    event VARCHAR(100) NOT NULL,
    actor VARCHAR(100) NOT NULL,
    rationale TEXT,
    payload JSONB,
    PRIMARY KEY (host, queue_id, ts)
);

-- Create index for common query patterns (though primary key already covers host, queue_id, ts)
CREATE INDEX IF NOT EXISTS idx_harness_queue_events_host_queue ON harness.queue_events(host, queue_id);