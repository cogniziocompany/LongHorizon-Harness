-- 005_harness_queue_retry_backoff.sql
-- Retry and provider-quota backoff columns for the Postgres queue backend.
--
-- The file store already round-trips these QueueEntry fields; harness.queue
-- lacked them, so a requeued entry read back from Postgres lost its lineage
-- (retry_of / attempt / failure_cause: the max_retries cap could never trip
-- past attempt 2, and the overseer could not see a pending successor) and its
-- backoff (not_before / wait_reason: the launcher launched a quota retry at
-- once instead of after the window).
--
--   retry_of       queue_id of the failed entry this one retries (NULL = original)
--   attempt        1 for an original, +1 per retry
--   failure_cause  the cause text that triggered the retry
--   not_before     epoch seconds; the launcher skips the entry until then
--   wait_reason    why it waits (provider_quota / provider_rate_limit), <=64 chars
--
-- Credentials are supplied by the caller via database_url (URL) and the
-- LH_HARNESS_DB_PASSWORD environment variable -- never baked into this file.
--
-- The whole file is safe to re-run: ADD COLUMN IF NOT EXISTS and CREATE INDEX
-- IF NOT EXISTS are no-ops once the objects exist.  Existing rows get the
-- defaults (attempt 1, everything else NULL), which is exactly how the file
-- store reads an entry written before these fields existed.

ALTER TABLE harness.queue
    ADD COLUMN IF NOT EXISTS retry_of VARCHAR(128);

ALTER TABLE harness.queue
    ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 1;

ALTER TABLE harness.queue
    ADD COLUMN IF NOT EXISTS failure_cause TEXT;

ALTER TABLE harness.queue
    ADD COLUMN IF NOT EXISTS not_before DOUBLE PRECISION;

ALTER TABLE harness.queue
    ADD COLUMN IF NOT EXISTS wait_reason VARCHAR(64);

-- "Is there already a pending successor of entry X?" (the launcher's and the
-- overseer's duplicate check) reads by retry_of.
CREATE INDEX IF NOT EXISTS harness_queue_retry_of_idx
    ON harness.queue (retry_of)
    WHERE retry_of IS NOT NULL;
