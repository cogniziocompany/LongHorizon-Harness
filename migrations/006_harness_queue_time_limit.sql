-- 006_harness_queue_time_limit.sql
-- Per-task wall-clock limit for the Postgres queue backend (task fc-H2).
--
--   time_limit_minutes  minutes from launched_at after which the launcher
--                       stops the run (1..1440); NULL = no per-entry limit
--                       ([queue] default_time_limit_minutes may still apply).
--
-- Safe to re-run: ADD COLUMN IF NOT EXISTS is a no-op once the column exists.
-- Existing rows read back as NULL, exactly how the file store reads an entry
-- written before the field existed.

ALTER TABLE harness.queue
    ADD COLUMN IF NOT EXISTS time_limit_minutes INTEGER;
