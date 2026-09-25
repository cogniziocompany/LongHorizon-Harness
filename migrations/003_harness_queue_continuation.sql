-- 003_harness_queue_continuation.sql
-- Continuation opt-in columns for the Postgres queue backend (task 233).
--
-- Adds the two caller-facing fields the file store already round-trips but
-- ``harness.queue`` lacked: ``branch`` (the continuation branch name, empty
-- string when unset) and ``continue_branch`` (the "use whatever branch the
-- workspace has checked out" flag, false when unset).  Together with the
-- pre-existing ``priority`` and ``dedup_key`` columns this makes the Postgres
-- store persist exactly the caller-supplied fields the file store persists.
--
-- Credentials are supplied by the caller via database_url (URL) and the
-- LH_HARNESS_DB_PASSWORD environment variable -- never baked into this file.
--
-- The whole file is safe to re-run: ADD COLUMN IF NOT EXISTS is a no-op once
-- the column exists, and re-running it on a schema that is already migrated
-- changes nothing observable.

ALTER TABLE harness.queue
    ADD COLUMN IF NOT EXISTS branch VARCHAR(256) NOT NULL DEFAULT '';

ALTER TABLE harness.queue
    ADD COLUMN IF NOT EXISTS continue_branch BOOLEAN NOT NULL DEFAULT false;