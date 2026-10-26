-- 008_harness_queue_migration_tombstone.sql
-- The migration tombstone link columns for the Postgres queue backend
-- (LHH-QUEUE-MIGRATION-TOMBSTONE, slice A).
--
--   migrated_to      JSONB: on the SOURCE entry, the destination it migrated
--                    to -- {"node": ..., "queue_id": ..., "at": ...}.  NULL on
--                    every entry that was never migrated.
--   migrated_from    JSONB: on the DESTINATION entry, the source it migrated
--                    from -- {"node": ..., "queue_id": ..., "at": ...}.  NULL on
--                    every entry that did not arrive through a migration.
--   migration_epoch  INTEGER: the monotonic migration counter stamped on the
--                    source tombstone when it was marked migrated.  NULL on
--                    every entry that was never migrated.
--
-- The status column is the enum harness.queue_status, created by migration
-- 001 with the values pending, launched, done, failed and blocked.  The
-- tombstone state 'migrated' is a SIXTH value, so the enum has to grow --
-- without this a migrated row is refused by Postgres before the store ever
-- sees it.  Postgres has no "ALTER TYPE ... ADD VALUE IF NOT EXISTS", so the
-- addition goes through a DO block that checks pg_enum for the value first;
-- the whole statement is a no-op once the value exists.
--
-- 'migrated' is a TERMINAL value, exactly like 'done' and 'failed': it is
-- the state of an entry whose work moved to another node.  It never means
-- "waiting to be launched".
--
-- Safe to re-run: ADD COLUMN IF NOT EXISTS is a no-op once the columns exist,
-- the enum addition is a no-op once the value exists, and existing rows read
-- back NULL -- exactly how the file store loads an entry written before the
-- fields existed.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_enum e
                   JOIN pg_type t ON t.oid = e.enumtypid
                   JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = 'queue_status'
                     AND n.nspname = 'harness'
                     AND e.enumlabel = 'migrated') THEN
        ALTER TYPE harness.queue_status ADD VALUE 'migrated';
    END IF;
END
$$;

ALTER TABLE harness.queue
    ADD COLUMN IF NOT EXISTS migrated_to JSONB;

ALTER TABLE harness.queue
    ADD COLUMN IF NOT EXISTS migrated_from JSONB;

ALTER TABLE harness.queue
    ADD COLUMN IF NOT EXISTS migration_epoch INTEGER;
