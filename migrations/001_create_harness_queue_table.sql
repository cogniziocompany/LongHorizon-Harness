-- Create harness.queue table matching QueueEntry.to_dict() exactly
-- This table stores queue entries for the LongHorizon-Harness task queue

CREATE SCHEMA IF NOT EXISTS harness;

CREATE TABLE IF NOT EXISTS harness.queue (
    queue_id VARCHAR(128) PRIMARY KEY,
    name VARCHAR(256) NOT NULL,
    task TEXT NOT NULL,
    workspace VARCHAR(4096) NOT NULL,
    max_rounds INTEGER NOT NULL,
    trio VARCHAR(10) NOT NULL,
    priority INTEGER NOT NULL,
    requested_by VARCHAR(256) NOT NULL,
    base_check VARCHAR(4000) DEFAULT '',
    status VARCHAR(20) NOT NULL,
    run_id VARCHAR(128),
    reason VARCHAR(4000),
    skip_reasons TEXT[] DEFAULT '{}',
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    launched_at DOUBLE PRECISION,
    last_checked_at DOUBLE PRECISION,
    dedup_key VARCHAR(256),
    CONSTRAINT valid_status CHECK (status IN ('pending', 'launched', 'done', 'failed', 'blocked'))
);

-- Create indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_harness_queue_status ON harness.queue(status);
CREATE INDEX IF NOT EXISTS idx_harness_queue_priority_created ON harness.queue(priority DESC, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_harness_queue_dedup_key ON harness.queue(dedup_key) WHERE dedup_key IS NOT NULL;