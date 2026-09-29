-- 004_sdlc.sql
-- Delivery record store: schema "sdlc" in database "agent-db" on CT103.
--
-- Implements the nine tables of docs/design/sdlc-record-store.md (task 288),
-- which expands the plan in docs/handoffs/PLAN-delivery-record-store-2026-09-29.md:
-- task, artifact, blob, artifact_version, ledger_row, open_ask, stage_event,
-- link, import_batch.
--
-- The schema holds the task index, links between records, the writable ledger
-- and open asks, and file versions.  Document bodies also live in Git; vectors
-- live in qdrant collection "sdlc_documents" and in Hivemind.  Queue events
-- stay in the "harness" schema of database "lh_harness" (001-003) and are
-- reached through sdlc.link, never copied.
--
-- Credentials are supplied by the caller via database_url (URL) and a password
-- environment variable -- never baked into this file.
--
-- The whole file is safe to re-run: CREATE SCHEMA/TABLE/INDEX IF NOT EXISTS
-- are no-ops once their object exists, the enum types are created inside DO
-- blocks that check pg_type (Postgres has no "CREATE TYPE IF NOT EXISTS"),
-- and re-running the file on a schema that is already migrated changes
-- nothing observable.  The file creates objects in schema "sdlc" only; it
-- does not alter any existing schema.
--
-- Dry-run validation (throwaway Postgres, docker postgres:16):
--
--   docker run -d --name sdlc-004-check -e POSTGRES_PASSWORD=throwaway postgres:16
--   until docker exec sdlc-004-check pg_isready -q; do sleep 1; done
--   docker exec -i sdlc-004-check psql -U postgres -d postgres -v ON_ERROR_STOP=1 -f - < migrations/004_sdlc.sql
--   docker exec -i sdlc-004-check psql -U postgres -d postgres -v ON_ERROR_STOP=1 -f - < migrations/004_sdlc.sql
--   docker rm -f sdlc-004-check
--
-- The file is applied twice with ON_ERROR_STOP=1 to prove re-run safety; the
-- container is then deleted.  Validation status for this revision:
-- NOT RUN -- no docker on the authoring node (CT110), 2026-09-29.

CREATE SCHEMA IF NOT EXISTS sdlc;

-- Lifecycle stages reuse the ledger's own ladder, so no new vocabulary:
-- QUEUED, RUNNING, GATED, BRANCHED, PUSHED, PR, REVIEWED, MERGED, LANE,
-- LIVE, VERIFIED on the main path, with HELD, BLOCKED, FAILED and
-- SUPERSEDED off the main path.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type t
                   JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = 'lifecycle_stage'
                     AND n.nspname = 'sdlc') THEN
        CREATE TYPE sdlc.lifecycle_stage
            AS ENUM ('QUEUED', 'RUNNING', 'GATED', 'BRANCHED', 'PUSHED', 'PR',
                     'REVIEWED', 'MERGED', 'LANE', 'LIVE', 'VERIFIED',
                     'HELD', 'BLOCKED', 'FAILED', 'SUPERSEDED');
    END IF;
END
$$;

-- The eleven artifact kinds of the plan: spec, queue_entry, handoff, plan,
-- runbook, scratch, patch, evidence, run_dump, log, script.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type t
                   JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = 'artifact_kind'
                     AND n.nspname = 'sdlc') THEN
        CREATE TYPE sdlc.artifact_kind
            AS ENUM ('spec', 'queue_entry', 'handoff', 'plan', 'runbook',
                     'scratch', 'patch', 'evidence', 'run_dump', 'log',
                     'script');
    END IF;
END
$$;

-- Link kinds of the plan: a task joined to a run id, queue id, PR, commit,
-- gate id, session or artifact.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type t
                   JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = 'link_kind'
                     AND n.nspname = 'sdlc') THEN
        CREATE TYPE sdlc.link_kind
            AS ENUM ('run', 'queue', 'pr', 'commit', 'gate', 'session',
                     'artifact');
    END IF;
END
$$;

-- One row per piece of work.  The key is the surrogate task_uid, because
-- task numbers are reused (208, 209, 217, 218, 224 appear twice in the
-- audit) and early ids such as '05h3b' are unmapped strings, so
-- task_number + slug are attributes, not the key.  task_number is nullable
-- for the unmapped early ids; where a number exists the pair
-- (task_number, slug) is unique, which lets the loader split the five
-- reused numbers into separate tasks by slug.
CREATE TABLE IF NOT EXISTS sdlc.task (
    task_uid       UUID NOT NULL DEFAULT gen_random_uuid(),
    task_number    INTEGER,
    slug           VARCHAR(256) NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    repo           VARCHAR(256) NOT NULL DEFAULT '',
    stage          sdlc.lifecycle_stage,
    requested_by   VARCHAR(256) NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task_uid)
);

CREATE UNIQUE INDEX IF NOT EXISTS sdlc_task_number_slug_idx
    ON sdlc.task (task_number, slug)
    WHERE task_number IS NOT NULL;

-- "Tasks by current stage" and "tasks by repo" are the read API's filters.
CREATE INDEX IF NOT EXISTS sdlc_task_stage_idx
    ON sdlc.task (stage)
    WHERE stage IS NOT NULL;

CREATE INDEX IF NOT EXISTS sdlc_task_repo_idx
    ON sdlc.task (repo)
    WHERE repo <> '';

-- One row per logical file (a spec, a queue entry, a handoff, ...).
-- task_uid is nullable: plans and runbooks are delivery records that are
-- not owned by a single task.  The row identifies the file, not a body;
-- bodies and copies live in blob / artifact_version.
CREATE TABLE IF NOT EXISTS sdlc.artifact (
    artifact_id    UUID NOT NULL DEFAULT gen_random_uuid(),
    task_uid       UUID REFERENCES sdlc.task (task_uid),
    kind           sdlc.artifact_kind NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    logical_path   VARCHAR(4096) NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (artifact_id)
);

CREATE INDEX IF NOT EXISTS sdlc_artifact_task_uid_idx
    ON sdlc.artifact (task_uid)
    WHERE task_uid IS NOT NULL;

CREATE INDEX IF NOT EXISTS sdlc_artifact_kind_idx
    ON sdlc.artifact (kind);

-- One row per distinct content, keyed by sha256, body stored once.  Every
-- copy of a file becomes an artifact_version row pointing here, so the 508
-- audited ledger copies reduce to one ledger blob plus the few passages
-- trimmed out of it.  Content addressing is the dedup ("deltas means
-- content-addressed storage", plan Decisions).  qdrant vectors are rebuilt
-- from this table, so blob is the only store of body bytes besides Git.
CREATE TABLE IF NOT EXISTS sdlc.blob (
    sha256         CHAR(64) NOT NULL,
    body           BYTEA NOT NULL,
    byte_size      BIGINT NOT NULL DEFAULT 0,
    redacted       BOOLEAN NOT NULL DEFAULT false,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (sha256)
);

-- Provenance of each load: which host the bundle was exported from, the
-- per-kind counts, and the redaction report (file, kind, count -- never
-- the secret value).  Every imported row carries its batch so a bad load
-- is traceable and reversible.
CREATE TABLE IF NOT EXISTS sdlc.import_batch (
    batch_id       UUID NOT NULL DEFAULT gen_random_uuid(),
    source_host    VARCHAR(64) NOT NULL DEFAULT '',
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    counts         JSONB NOT NULL DEFAULT '{}',
    redaction_report JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (batch_id)
);

CREATE INDEX IF NOT EXISTS sdlc_import_batch_source_time_idx
    ON sdlc.import_batch (source_host, started_at);

-- Each copy or backup of an artifact.  Carries where the copy was found
-- (original_path), when that copy was written and the backup suffix the
-- exporter classified it by.  A logical file can have many copies of the
-- same body at different paths, so the dedup key is
-- (artifact, blob, path, written_at) and written_at is nullable.
CREATE TABLE IF NOT EXISTS sdlc.artifact_version (
    version_id     UUID NOT NULL DEFAULT gen_random_uuid(),
    artifact_id    UUID NOT NULL REFERENCES sdlc.artifact (artifact_id),
    blob_sha256    CHAR(64) NOT NULL REFERENCES sdlc.blob (sha256),
    original_path  VARCHAR(4096) NOT NULL,
    written_at     TIMESTAMPTZ,
    backup_suffix  VARCHAR(64) NOT NULL DEFAULT '',
    import_batch_id UUID REFERENCES sdlc.import_batch (batch_id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (version_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS sdlc_artifact_version_dedup_idx
    ON sdlc.artifact_version (artifact_id, blob_sha256, original_path, written_at);

CREATE INDEX IF NOT EXISTS sdlc_artifact_version_blob_idx
    ON sdlc.artifact_version (blob_sha256);

-- One row per tick row or section of the overseer ledger.  The key is
-- (session_ref, tick_number, occurred_at), because tick numbers restarted
-- at 1 after the 2026-09-23 cutover: neither tick_number alone nor
-- (tick_number, occurred_at) alone is stable, but a tick inside a session
-- at a time is.  body holds the row text with secret values already
-- replaced by [REDACTED:<kind>].
CREATE TABLE IF NOT EXISTS sdlc.ledger_row (
    session_ref    VARCHAR(16) NOT NULL,
    tick_number    INTEGER NOT NULL,
    occurred_at    TIMESTAMPTZ NOT NULL,
    section        VARCHAR(128) NOT NULL DEFAULT '',
    body           TEXT NOT NULL DEFAULT '',
    import_batch_id UUID REFERENCES sdlc.import_batch (batch_id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_ref, tick_number, occurred_at)
);

-- The ledger is read in time order ("exported by month", timeline view).
CREATE INDEX IF NOT EXISTS sdlc_ledger_row_occurred_at_idx
    ON sdlc.ledger_row (occurred_at);

-- One row per open ask: the existing open-asks record carried over, plus
-- state history.  state is the current state; state_history is the ordered
-- JSON list of {state, at} transitions so the current column can change
-- without losing the path it took.  The loader maps the seven source
-- columns of the existing open-asks file onto this row at import time.
CREATE TABLE IF NOT EXISTS sdlc.open_ask (
    ask_id         UUID NOT NULL DEFAULT gen_random_uuid(),
    task_uid       UUID REFERENCES sdlc.task (task_uid),
    asked_by       VARCHAR(256) NOT NULL DEFAULT '',
    question       TEXT NOT NULL DEFAULT '',
    state          VARCHAR(32) NOT NULL DEFAULT 'open',
    state_history  JSONB NOT NULL DEFAULT '[]',
    asked_at       TIMESTAMPTZ,
    answered_at    TIMESTAMPTZ,
    answer         TEXT,
    import_batch_id UUID REFERENCES sdlc.import_batch (batch_id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ask_id)
);

CREATE INDEX IF NOT EXISTS sdlc_open_ask_state_idx
    ON sdlc.open_ask (state);

-- A task reaching a lifecycle stage.  evidence_ref points at the proof of
-- the stage (run id, PR number, merge commit, report.json path, qdrant or
-- artifact reference); it is a text reference rather than a foreign key
-- because the earliest stage events are reconstructed from loose records.
CREATE TABLE IF NOT EXISTS sdlc.stage_event (
    event_id       UUID NOT NULL DEFAULT gen_random_uuid(),
    task_uid       UUID NOT NULL REFERENCES sdlc.task (task_uid),
    stage          sdlc.lifecycle_stage NOT NULL,
    evidence_ref   VARCHAR(1024) NOT NULL DEFAULT '',
    occurred_at    TIMESTAMPTZ NOT NULL,
    import_batch_id UUID REFERENCES sdlc.import_batch (batch_id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id)
);

-- The timeline view is one task's events in time order.
CREATE INDEX IF NOT EXISTS sdlc_stage_event_task_time_idx
    ON sdlc.stage_event (task_uid, occurred_at);

-- Joins between records: a task to a run id, queue id, PR, commit, gate
-- id, session or artifact.  target holds the joined identifier in its own
-- vocabulary (run id, q-<hex> queue id, PR number, commit sha, session
-- reference); for artifact links artifact_id carries the typed reference.
-- (task_uid, kind, target) is unique so re-loading a bundle is idempotent.
CREATE TABLE IF NOT EXISTS sdlc.link (
    link_id        UUID NOT NULL DEFAULT gen_random_uuid(),
    task_uid       UUID NOT NULL REFERENCES sdlc.task (task_uid),
    kind           sdlc.link_kind NOT NULL,
    target         VARCHAR(1024) NOT NULL,
    artifact_id    UUID REFERENCES sdlc.artifact (artifact_id),
    import_batch_id UUID REFERENCES sdlc.import_batch (batch_id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (link_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS sdlc_link_task_kind_target_idx
    ON sdlc.link (task_uid, kind, target);
