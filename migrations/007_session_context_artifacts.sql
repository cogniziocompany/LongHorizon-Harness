-- 007_session_context_artifacts.sql
-- Durable store for session context artifacts (LHH-SESSION-CONTEXT-ARTIFACTS).
--
-- A "context packet" is a JSON blob a Claude Code session (or any
-- SDK/CLI producer) sends to /api/artifacts.  The packet is redacted on
-- store, persisted in JSONB, and rendered dynamically at /artifacts/<id>.
-- Producer identity (agent/host/session) is denormalized for indexing.
--
-- Idempotency: a (producer_session, captured_at) tuple collides on
-- ``idempotency_key`` (UNIQUE) so a re-POST returns the same artifact_id.
--
-- Attachment to a queue entry: harness.queue.artifact_ids (JSONB list of
-- artifact_id strings) is the join surface; existing queue.py validates the
-- shape and stores the same value on the file-backed store.

CREATE TABLE IF NOT EXISTS harness.artifacts (
    artifact_id        VARCHAR(64)  NOT NULL,
    kind               VARCHAR(32)  NOT NULL,
    title              VARCHAR(512) NOT NULL DEFAULT '',
    summary            TEXT         NOT NULL DEFAULT '',
    producer_agent     VARCHAR(64)  NOT NULL DEFAULT '',
    producer_host      VARCHAR(256) NOT NULL DEFAULT '',
    producer_session   VARCHAR(128) NOT NULL DEFAULT '',
    producer_ref       VARCHAR(64)  NOT NULL DEFAULT '',
    captured_at        DOUBLE PRECISION NOT NULL DEFAULT 0,
    visibility         VARCHAR(16)  NOT NULL DEFAULT 'private',
    packet             JSONB        NOT NULL,
    packet_sha256      CHAR(64)     NOT NULL DEFAULT '',
    bytes              INTEGER      NOT NULL DEFAULT 0,
    redaction_count    INTEGER      NOT NULL DEFAULT 0,
    created_at         DOUBLE PRECISION NOT NULL DEFAULT 0,
    idempotency_key    VARCHAR(160) NOT NULL,
    PRIMARY KEY (artifact_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS harness_artifacts_idem_idx
    ON harness.artifacts (idempotency_key);

CREATE INDEX IF NOT EXISTS harness_artifacts_kind_created_idx
    ON harness.artifacts (kind, created_at);

-- Attachment to queue entries (added by this slice; old JSON reads as '[]').
ALTER TABLE harness.queue
    ADD COLUMN IF NOT EXISTS artifact_ids JSONB NOT NULL DEFAULT '[]';
