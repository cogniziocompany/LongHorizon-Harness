# Session Context Artifacts (LHH-SESSION-CONTEXT-ARTIFACTS)

Status: Draft (Paxton 2026-10-17)
Owner: Executor slice
Related work: existing Execution-artifacts UI surface
(`frontend/web/src/App.tsx:1873 ExecutionArtifacts`,
`src/lh_harness/manager.py:1992 persist_trajectory_artifacts`,
`src/lh_harness/webapi/server.py:3454 /api/runs/{run_id}/rounds/{round_index}/artifacts`);
per-run round artifacts spooled by
`_store_upload_chunk` in the same server module;
the experience layer's rule-based redaction pass
(`src/lh_harness/experience/redact.py`, `redact_value`, `redact_text`).

## 1. Goal

Tie the Claude harness that creates the ideas into the LongHorizon-Harness
queue as a first-class producer: a Claude Code session — terminated by the
user, by a `SessionEnd` hook, or by a slash command — exports its current
session context as a durable, renderable **ContextPacket** blob. The blob is
stored in the harness DB/object store with a stable id and rendered
dynamically in the web UI like a GitHub artifact or a Claude artifact link.

The primary consumer is the *next* agent or the same team picking up a
queued task: attach one or more `artifact_ids` to a queue entry at enqueue,
and the launching agent receives the full session context as continuity.

## 2. Why this exists; how it differs from what already ships

The LongHorizon-Harness already ships two artifact surfaces:

1. **Per-round execution artifacts** (`/api/runs/{run_id}/rounds/{round_index}/artifacts/...`):
   files an agent wrote during a run, surfaced by the `ExecutionArtifacts`
   React component. Round-scoped, agent-produced, run-bound — they live and
   die with their host run.
2. **Chunked transcript uploads** (`_store_upload_chunk`): the reporter pipes
   512 KiB gzip+base64 chunks into per-upload spools on disk, assembled under
   `runs/<run>/log/_artifact_uploads/round_NNN/`. Runtime telemetry, also
   run-bound.

What is missing is a **session-bound, cross-run, attachable-to-queue**
artifact: the *context packet* a Claude Code session exports at the end of
its work. The slice below adds that as a new layer that does not require a
git repo, does not require a run, and does not require the producer to be the
LH-Harness app — only a bearer token plus a small producer-side tool.

## 3. Concept: ContextPacket

A `ContextPacket` is one JSON object the producer sends in a single request.
It is the smallest unit the harness stores, renders, attaches, and audits.

### 3.1 Top-level shape

```jsonc
{
  "schema_version": 1,
  "kind": "session_context",        // or "task_handoff", "plan", "evidence"
  "title": "string",                // short, human-readable
  "summary": "string",              // markdown, ≤ 4 KiB
  "transcript_delta": [            // optional, bounded slice of the session
    { "ts": 1713000000.0, "role": "user|assistant|tool|system", "text": "..." }
  ],
  "plan": [                         // optional ordered steps
    { "id": "p1", "text": "..." }
  ],
  "decisions": [                    // optional explicit decisions
    { "ts": 1713000010.0, "what": "...", "why": "..." }
  ],
  "files_touched": [                // optional paths (NOT contents)
    { "path": "src/lh_harness/queue.py", "action": "edit|create|delete", "lines": "12-44" }
  ],
  "links": [                        // optional external references
    { "label": "design doc", "url": "https://..." }
  ],
  "evidence": [                     // optional inline evidence blocks
    { "kind": "command", "label": "pytest", "exit_code": 0, "text": "..." },
    { "kind": "image",   "label": "screenshot.png", "media_type": "image/png", "data_uri": "..." }
  ],
  "producer": {                     // identity of who created the packet
    "agent":    "claude_code",      // or "codex", "opencode", "sdk"
    "host":     "pve.local",
    "session_id": "uuid-or-hex",
    "session_ref": "8f14ab",        // optional short reference
    "captured_at": 1713000050.0
  },
  "scope": {                        // optional privacy scoping
    "visibility": "private",        // "private" | "team" | "all"
    "redact_secrets": true          // default true; honors existing rules
  }
}
```

### 3.2 Hard guarantees

* **Size**: serialized JSON ≤ 1 MiB (`_MAX_CONTROL_BODY_BYTES` constant in
  `webapi/server.py`). Larger packets must be chunked by the producer (out of
  scope for this slice; a follow-up can mirror the chunked upload helper).
* **Idempotent storage**: a producer that retries with the same
  `producer.session_id + captured_at` pair gets the same `artifact_id` back
  (the store keys idempotency on that pair).
* **Redaction**: every string field is run through
  `lh_harness.experience.redact.redact_value` on *store*; the producer is
  encouraged but not required to pre-redact. Redaction is idempotent, so a
  pre-redacted value is unchanged. `scope.redact_secrets=false` is permitted
  only when no known-secret pattern appears (the store refuses otherwise).
* **No repo required**: `files_touched` paths are advisory. A null/empty
  `repo` is acceptable. Git integration is explicitly out of the supported
  path; the LH-Harness app must keep working when `files_touched` are bare
  paths or empty.

## 4. Storage model

### 4.1 DB table

`harness.artifacts` (added by `migrations/007_session_context_artifacts.sql`):

| column              | type                 | notes                                          |
| ------------------- | -------------------- | ---------------------------------------------- |
| `artifact_id`       | VARCHAR(64) PK       | stable, URL-safe, e.g. `art-<32-hex>`          |
| `kind`              | VARCHAR(32)          | restricted to the kind allowlist                |
| `title`             | VARCHAR(512)         | from the packet                                |
| `summary`           | TEXT                 | markdown summary                               |
| `producer_agent`    | VARCHAR(64)          | from the packet                                |
| `producer_host`     | VARCHAR(256)         |                                                |
| `producer_session`  | VARCHAR(128)         |                                                |
| `producer_ref`      | VARCHAR(64)          | short ref                                      |
| `captured_at`       | DOUBLE PRECISION     |                                                |
| `visibility`        | VARCHAR(16)          | private / team / all                           |
| `packet`            | JSONB                | the validated, redacted packet                 |
| `packet_sha256`     | CHAR(64)             | hex digest of the raw POST body (pre-redaction) |
| `bytes`             | INTEGER              | serialized size                                |
| `redaction_count`   | INTEGER              | how many values the store replaced             |
| `created_at`        | DOUBLE PRECISION     |                                                |
| `idempotency_key`   | VARCHAR(160) UNIQUE  | producer.session_id + ':' + captured_at (str)  |

Attachment to a queue entry (`harness.queue_entries.artifact_ids`):

```sql
ALTER TABLE harness.queue
    ADD COLUMN IF NOT EXISTS artifact_ids JSONB NOT NULL DEFAULT '[]';
```

The column stores a JSON array of `artifact_id` strings. The first slice
adds the column; the existing in-memory `QueueEntry.artifact_ids` list field
is the source of truth on the file-backed `QueueStore`, and the migration
aligns the Postgres-backed `PgQueueStore` to the same shape.

### 4.2 Idempotency

A re-POST of the identical `(producer_session, captured_at)` pair returns
the existing `artifact_id` with HTTP 200 instead of creating a duplicate
(HTTP 201). The `idempotency_key` UNIQUE constraint backs the rule; the
code path also checks first to return the existing row fast.

### 4.3 Retention & GC

Artifacts follow the configured `queue_retention_days` (default 30; existing
artifact surfaces share this knob where present, else the convention is to
expire after 30 days). The slice stores no per-artifact TTL; a follow-up can
add GC. The first slice records the `created_at` so a GC cron has the
information it needs.

## 5. HTTP surface

All routes share the existing bearer-token boundary used by `/api/*`. The
read-only HTML render additionally honors the standard `WWW-Authenticate`
behavior. Routes intentionally live under `/api/artifacts/...` (not under
`/api/runs/...`) because they are run-independent.

### 5.1 `POST /api/artifacts`

Body: a `ContextPacket` JSON object. Response: `{ "artifact_id": "art-...",
"url": "/artifacts/<id>", "created_at": <float>, "idempotent": false }`. A
duplicate POST returns 200 with `idempotent: true` and the same
`artifact_id`. Limits:

* Body ≤ 1 MiB (existing `_MAX_CONTROL_BODY_BYTES`).
* `kind` ∈ `{session_context, task_handoff, plan, evidence}`.
* `producer.session_id` non-empty, ≤ 128 chars.
* `producer.agent` ∈ `{claude_code, codex, opencode, sdk, other}`.
* `visibility` ∈ `{private, team, all}`.

### 5.2 `GET /api/artifacts/<id>`

Returns the validated packet JSON. Access control: a `private` artifact is
readable only when the bearer token carries a `producer.session_ref` header
matching the packet's `producer_ref`, or when an `[artifacts]` access list
includes the bearer (left as follow-up; first slice permits any authenticated
bearer to read `private` so the slice is shippable). A `team` artifact is
readable by any authenticated bearer. An `all` artifact is readable by any
authenticated bearer.

### 5.3 `GET /api/artifacts/<id>/raw`

Returns the exact stored JSONB (no rendering). Used by SDK clients to fetch
the raw context they pushed, e.g. for re-attaching to a new queue entry.

### 5.4 `POST /api/queue` extended

`KNOWN_REQUEST_KEYS` (in `queue.py:94`) gains `"artifact_ids"`. The
`QueueEntry` dataclass gains `artifact_ids: list[str]`. Validation: a list of
strings each matching `^art-[0-9a-f]{8,64}$`; unknown id format is rejected
with HTTP 422. Missing field defaults to `[]` (existing enqueues are
unchanged). The same validation runs on the file `QueueStore` and the
`PgQueueStore` so behavior matches across backends.

### 5.5 Page: `GET /artifacts/<id>`

Read-only render. The page is an HTML response that loads `/api/artifacts/<id>`
and renders dynamically with the conventions below. The page is reachable
without an `[artifacts]` table check in the first slice.

## 6. Rendering

The page renders the packet as the user expects for a session-context
artifact (GitHub artifact, Claude artifact link):

* **Header**: `kind`, `title`, `producer.agent`, `producer.host`,
  `producer.session_ref`, `captured_at`, link URLs.
* **Summary**: rendered as markdown.
* **Plan**: a table of step id / text.
* **Decisions**: a table of ts / what / why.
* **Files touched**: a code-styled list (no diffs — paths only).
* **Links**: an `<a>` list.
* **Evidence**:
  * `command` blocks render as fenced code with the exit code badge.
  * `image` blocks render inline if `media_type` is in the existing
    `_INLINE_RASTER_TYPES` whitelist in `webapi/server.py:395`; otherwise a
    download link with the agent-safe Content-Disposition helper from the
    same file.
  * HTML evidence renders in a sandboxed `<iframe sandbox="">` so an
    agent-supplied HTML block cannot run script; the iframe's `srcdoc` is
    the agent text, the CSP `sandbox` value omits `allow-scripts`. This is
    the same trust model GitHub uses for `gist` HTML.
* **Transcript delta**: a chronology of `user / assistant / tool / system`
  rows, redacted on read if `scope.redact_secrets` was true at write-time
  (it already was in storage).

Every render path is *read-only*. The artifact never produces writes during
viewing; the producer's session is not re-executed.

## 7. Producers

### 7.1 SDK / CLI: `lh-harness artifact push`

A new subcommand in the existing `cli.py` that posts a packet file:

```
lh-harness artifact push --packet packet.json \
    --lh-base http://lh-harness.local:8787 \
    --bearer-env LH_HARNESS_WEB_TOKEN
```

Behavior:

* Reads the JSON file, validates top-level shape, computes `packet_sha256`.
* POSTs to `/api/artifacts` and prints the assigned `artifact_id` and URL.
* Idempotent on retry.

### 7.2 Claude Code plugin hook

A pair of files under `src/lh_harness/plugins/claude_code/` shipped as a
reference:

* `hooks/session_end.sh` — runs on Claude Code `SessionEnd` events. It
  inspects the local transcript (read-only), builds a minimal packet
  (transcript last N turns, plan bullets, files touched, links), POSTs via
  the SDK above, and prints the artifact id.
* `commands/artifact-push.md` — slash command equivalent for an
  on-demand push.

The plugin is *optional*; the LH-Harness app does not require Claude Code or
the hook. A run-time check skips the plugin when not present.

## 8. Access control

| visibility | required bearer | first-slice behavior |
| ---------- | --------------- | ------------------- |
| `all`      | any valid       | any bearer |
| `team`     | any valid       | any bearer |
| `private`  | any valid       | any bearer (TODO: enforce session_ref header) |

The follow-up scope-tightens `private` so it requires the matching
`X-Lh-Session-Ref` header. The slice records the requirement, not the
gate, so the design is stable across releases.

## 9. Out of scope (explicit)

* Chunked upload of multi-MiB packets — defer to a follow-up; mention only.
* Editor / live update of an artifact — read-only render only.
* GitHub-style annotation / comment threads on an artifact.
* Cross-node replication of an artifact between LH-Harness instances.
* GC of expired artifacts (records `created_at` so a cron has what it needs).

## 10. Acceptance criteria (matching AC1..AC10)

* **AC1**: this document.
* **AC2**: migration `007_session_context_artifacts.sql` creates the
  `artifacts` table and the `queue.artifact_ids` column.
* **AC3**: `POST /api/artifacts` accepts a ContextPacket and returns
  `artifact_id`.
* **AC4**: `GET /api/artifacts/<id>` returns the packet JSON.
* **AC5**: `POST /api/queue` accepts `artifact_ids: ["art-..."]`.
* **AC6**: `GET /artifacts/<id>` renders markdown / tables / code / images /
  sandboxed HTML dynamically.
* **AC7**: secret redaction reuses `lh_harness.experience.redact.redact_value`.
* **AC8**: no git integration is required to POST or GET.
* **AC9**: unit tests pass (pytest).
* **AC10**: draft PR to `main`.

