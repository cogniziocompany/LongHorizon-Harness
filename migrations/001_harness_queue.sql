-- 001_harness_queue.sql
-- Durable Postgres backend for the fleet task queue (PgQueueStore).
--
-- Columns mirror QueueEntry.to_dict() field-for-field (queue.py:58-79, the
-- field table in docs/queue.md:128-146) so the two stores serialise identically.
--
-- Schema/database are fixed: schema "harness", database "lh_harness".
-- Credentials are supplied by the caller via database_url (URL) and the
-- LH_HARNESS_DB_PASSWORD environment variable -- never baked into this file.

CREATE TABLE IF NOT EXISTS harness.queue (
    queue_id       VARCHAR(128) NOT NULL,
    name           VARCHAR(256) NOT NULL,
    task           TEXT NOT NULL,
    workspace      VARCHAR(4096) NOT NULL,
    max_rounds     INTEGER NOT NULL DEFAULT 25,
    trio           VARCHAR(16) NOT NULL,
    priority       INTEGER NOT NULL DEFAULT 0,
    requested_by   VARCHAR(256) NOT NULL,
    base_check     VARCHAR(256) NOT NULL DEFAULT '',
    status         VARCHAR(16) NOT NULL DEFAULT 'pending',
    run_id         VARCHAR(128),
    reason         TEXT,
    skip_reasons   JSONB NOT NULL DEFAULT '[]',
    created_at     DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at     DOUBLE PRECISION NOT NULL DEFAULT 0,
    launched_at    DOUBLE PRECISION,
    last_checked_at DOUBLE PRECISION,
    dedup_key      VARCHAR(256),
    PRIMARY KEY (queue_id)
);

-- status is a queue-entry state.  Allowed values are exactly the canonical
-- _VALID_STATUS set (queue.py:39): pending, launched, done, failed, blocked.
-- "blocked" is the PC queue's parked (non-terminal) state.
CREATE TYPE harness.queue_status AS ENUM ('pending', 'launched', 'done', 'failed', 'blocked');

ALTER TABLE harness.queue
    ALTER COLUMN status TYPE harness.queue_status USING status::text::harness.queue_status;

-- Keep the dedup key's "in use" window consistent with _NON_TERMINAL_STATUS
-- (queue.py:44): a key is considered held only while its entry is pending,
-- launched, or blocked.
CREATE INDEX IF NOT EXISTS harness_queue_dedup_key_idx
    ON harness.queue (dedup_key)
    WHERE dedup_key IS NOT NULL;
