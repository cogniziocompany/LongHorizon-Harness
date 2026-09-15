-- 002_harness_queue_events.sql
-- Append-only audit log of queue-entry lifecycle events (PgQueueStore).
--
-- Schema/database are fixed: schema "harness", database "lh_harness".
-- Credentials are supplied by the caller via database_url (URL) and the
-- LH_HARNESS_DB_PASSWORD environment variable -- never baked into this file.

CREATE TABLE IF NOT EXISTS harness.queue_events (
    host        VARCHAR(255) NOT NULL DEFAULT '',
    queue_id    VARCHAR(128) NOT NULL,
    ts          DOUBLE PRECISION NOT NULL DEFAULT 0,
    event       VARCHAR(64) NOT NULL,
    actor       VARCHAR(255) NOT NULL DEFAULT '',
    rationale   VARCHAR(4096),
    payload     JSONB
);

-- One row per state change: mark_launched / mark_done / mark_failed /
-- record_skip / record_block / record_unblock, plus enqueue and delete.
-- Insertion-ordered by (ts, host, queue_id) so a reader can replay an entry's
-- history without an extra index on a moving column.
CREATE INDEX IF NOT EXISTS harness_queue_events_order_idx
    ON harness.queue_events (ts, host, queue_id);

-- queue_id references harness.queue; a deleted entry leaves its events in place
-- so the audit trail survives.
ALTER TABLE harness.queue_events
    ADD CONSTRAINT harness_queue_events_queue_id_fkey
        FOREIGN KEY (queue_id) REFERENCES harness.queue (queue_id)
        ON DELETE CASCADE;
