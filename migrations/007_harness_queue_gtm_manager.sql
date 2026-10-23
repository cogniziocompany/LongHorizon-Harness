-- 007_harness_queue_gtm_manager.sql
-- The go-to-market manager for the Postgres queue backend (GTM-MANAGER-G1-a).
--
--   gtm_manager  the overseer that owns the task: 'overseer1' or
--                'overseer1-op'.  NOT NULL with the default 'overseer1', so
--                a task created by any other agent (codex, continuity1-op,
--                Paxton's own sessions, CFO1 ...) belongs to 'overseer1' until
--                it is reassigned -- never to nobody.
--
-- Safe to re-run: ADD COLUMN IF NOT EXISTS is a no-op once the column exists.
-- Existing rows read back as 'overseer1', exactly how the file store reads an
-- entry written before the field existed.

ALTER TABLE harness.queue
    ADD COLUMN IF NOT EXISTS gtm_manager VARCHAR(32) NOT NULL DEFAULT 'overseer1';
