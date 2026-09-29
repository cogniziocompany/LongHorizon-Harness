# Delivery record store: `sdlc` design

Task 288 · 2026-09-29

**Authoritative source:** [`docs/handoffs/PLAN-delivery-record-store-2026-09-29.md`](../handoffs/PLAN-delivery-record-store-2026-09-29.md)
(sha256 `b922fd7884a681baa3a82f8f069b99b533da15c71e07e87d9bb7364ef39059cb`). This document expands the plan's
**Design**, **API** and **Redaction rules** sections. Where anything below and the plan differ, the plan wins.

**What this is:** the store that captures the overseer queue's lifecycle history (~265 tasks since 2026-09-05,
audited 2026-09-29: 56 064 files / 24 GB under `C:\tmp`, of which `queue` is 1 604 files / 2.1 GB) and keeps
tracking every future task the same way automatically. Store decision (Paxton, 2026-09-29): **Git + Postgres +
Hivemind**, with fleet.easybutt0n.ai as the UI. Scope: everything, including per-tick scratch, redacted first.

---

## 1. Which layer holds what

### Who holds what (plan, "Design / Who holds what")

| Layer | Holds | Existing piece reused |
|---|---|---|
| Git | Whole documents, with version history | Apparatus layout in LongHorizon-Harness: `tasks/`, `queue/`, `docs/LEDGER.md`, `docs/handoffs/` |
| Postgres (`agent-db` on **CT103**) | Task index, links between records, writable ledger and open asks, file versions | sits next to the `hivemind_sessions` and `fleet` schemas |
| Hivemind | Semantic recall of sessions and decisions | `memory-mcp` and `upsert_session_memory` (pgvector) |
| qdrant | Semantic search over whole documents, chunked | qdrant on CT103 `:6333`, running but unused today — this design fills collection **`sdlc_documents`** |
| UI | Per-task lifecycle view | fleet-admin Overseer and Ship Plane pages |

### Where the data sits (plan, "Where the data sits")

| Data | Primary | Second copy |
|---|---|---|
| Task index, links, ledger, open asks (`sdlc`) | `agent-db` on CT103 | Nightly dump restored to the secondary Postgres (the gateway's `postgressecondary` instance — **host not yet identified**; see `docs/design/sdlc-component-survey.md`, and the plan's open point: task 1 must confirm host and free space before task 9 chooses it) |
| Queue events | `lh_harness` on CT103, unchanged (schema `harness`, migrations 001–003) | Read by `sdlc` through links, **not copied** |
| Document bodies | Git, plus `sdlc.blob` | Git remote on GitHub |
| Document vectors | qdrant collection `sdlc_documents` | Rebuildable from `sdlc.blob`, so no backup needed |
| Session memory vectors | Hivemind pgvector, unchanged | – |

**Why vectors are split:** Hivemind stores trimmed fragments per session. Whole specs and handoffs need chunking
and a payload filter by task, stage and kind, which qdrant does well and which keeps Hivemind's table small.

**Reads vs. writes:** writes happen in exactly one place — the LongHorizon-Harness write API on CT110. The six
overseer archive tools on CT110 are read-only today; this design adds the write path beside them.

---

## 2. Lifecycle stage ladder

The stages reuse the ledger's own ladder, so no new vocabulary:

**`QUEUED → RUNNING → GATED → BRANCHED → PUSHED → PR → REVIEWED → MERGED → LANE → LIVE → VERIFIED`**,
with **`HELD`**, **`BLOCKED`**, **`FAILED`** and **`SUPERSEDED`** off the main path.

The ladder is carried in this design as: (a) the Postgres enum `sdlc.lifecycle_stage`, (b) the
`LifecycleStage` enum in `docs/api/sdlc-openapi.yaml`, and (c) the `StageLadder` UI component (new — see the
component survey). `task.stage` is a cached projection of the task's furthest stage event and is refreshed when
a stage event is written; `sdlc.stage_event` is the source of truth.

---

## 3. Postgres schema `sdlc` (in `agent-db`)

Implemented by `migrations/004_sdlc.sql` (dry run only — not applied to any database; validation status and the
reproducing command are in that file's header and in §7 below). Nine tables, in dependency order. All objects
are created in schema `sdlc` only; nothing existing is altered.

### 3.1 `task` — one row per piece of work

| Column | Type | Key / constraint | Notes |
|---|---|---|---|
| `task_uid` | `UUID` default `gen_random_uuid()` | **PRIMARY KEY** | Surrogate key |
| `task_number` | `INTEGER` NULL | part of unique index below | Nullable: early ids such as `05h3b` are unmapped strings and stay as-is |
| `slug` | `VARCHAR(256)` NOT NULL | part of unique index below | Attribute, not the key |
| `title` | `TEXT` NOT NULL default `''` | | |
| `repo` | `VARCHAR(256)` NOT NULL default `''` | indexed (partial) | Read-API filter |
| `stage` | `sdlc.lifecycle_stage` NULL | indexed (partial) | Current stage projection |
| `requested_by` | `VARCHAR(256)` NOT NULL default `''` | | |
| `created_at`, `updated_at` | `TIMESTAMPTZ` NOT NULL default `now()` | | |

Indexes: **`sdlc_task_number_slug_idx`** — `UNIQUE (task_number, slug) WHERE task_number IS NOT NULL`;
`sdlc_task_stage_idx` — partial on `stage`; `sdlc_task_repo_idx` — partial on `repo`.

**Why the surrogate key:** the audit found task numbers are **reused** (208, 209, 217, 218, 224 each appear
twice). A natural key on `task_number` would collapse two distinct pieces of work into one. `task_number` and
`slug` are therefore attributes: identity is the generated `task_uid`, and the partial unique index on
`(task_number, slug)` is what the loader uses to split the five reused numbers into separate tasks by slug
(plan, Open points). Numbers alone are never trusted.

### 3.2 `artifact` — one row per logical file

| Column | Type | Key / constraint |
|---|---|---|
| `artifact_id` | `UUID` default `gen_random_uuid()` | **PRIMARY KEY** |
| `task_uid` | `UUID` NULL → `task(task_uid)` | FK, nullable + indexed (plans/runbooks are not owned by one task) |
| `kind` | `sdlc.artifact_kind` NOT NULL | enum, indexed |
| `title` | `TEXT` NOT NULL default `''` | |
| `logical_path` | `VARCHAR(4096)` NOT NULL default `''` | the file's canonical path, copies excluded |
| `created_at` | `TIMESTAMPTZ` NOT NULL default `now()` | |

`artifact_kind` — the plan's eleven kinds: `spec`, `queue_entry`, `handoff`, `plan`, `runbook`, `scratch`,
`patch`, `evidence`, `run_dump`, `log`, `script`.

**Why this split:** the artifact row is the stable identity of "the spec for task 283"; copies and bodies
hang off it. Identity survives dedup — 508 copies of the ledger are all *versions of* one artifact.

### 3.3 `blob` — one row per distinct content

| Column | Type | Key / constraint |
|---|---|---|
| `sha256` | `CHAR(64)` (lowercase hex) | **PRIMARY KEY** — content addressing |
| `body` | `BYTEA` NOT NULL | body stored once, post-redaction |
| `byte_size` | `BIGINT` NOT NULL default `0` | |
| `redacted` | `BOOLEAN` NOT NULL default `false` | set when the exporter's redaction pass ran over this body |
| `created_at` | `TIMESTAMPTZ` NOT NULL default `now()` | |

**Why the hash key:** Paxton's answer to "redact, then save deltas with the core records?" was *"deltas means
content-addressed storage"*: each distinct file body is stored once by hash and every copy becomes a version
row pointing at it. The 508 ledger copies (≈2.4 GB) reduce to the final ledger plus the few passages that were
trimmed out of it. qdrant vectors are rebuilt from this table (plan, Where the data sits), so `blob` plus Git
is the only store of body bytes.

### 3.4 `artifact_version` — each copy or backup of an artifact

| Column | Type | Key / constraint |
|---|---|---|
| `version_id` | `UUID` default `gen_random_uuid()` | **PRIMARY KEY** |
| `artifact_id` | `UUID` NOT NULL → `artifact(artifact_id)` | FK |
| `blob_sha256` | `CHAR(64)` NOT NULL → `blob(sha256)` | FK, indexed (find every copy of a body) |
| `original_path` | `VARCHAR(4096)` NOT NULL | where this copy was found |
| `written_at` | `TIMESTAMPTZ` NULL | the copy's own write time |
| `backup_suffix` | `VARCHAR(64)` NOT NULL default `''` | exporter classification (`.bak`, `.1`, …) |
| `import_batch_id` | `UUID` NULL → `import_batch(batch_id)` | provenance |
| `created_at` | `TIMESTAMPTZ` NOT NULL default `now()` | |

Indexes: **`sdlc_artifact_version_dedup_idx`** — `UNIQUE (artifact_id, blob_sha256, original_path, written_at)`;
`sdlc_artifact_version_blob_idx` on `blob_sha256`.

**Why this dedup key:** a logical file can carry many copies of the same body at different paths (the audit's
508 ledger copies and 64 near-identical run dumps). `(artifact, blob)` alone would collapse legitimately
distinct copies; `(artifact, blob, path, written_at)` is exactly "one observed copy", so re-loading a bundle is
idempotent and the copy census survives.

### 3.5 `ledger_row` — one row per tick row or section

| Column | Type | Key / constraint |
|---|---|---|
| `session_ref` | `VARCHAR(16)` NOT NULL | **PRIMARY KEY part** — e.g. `f14d17` |
| `tick_number` | `INTEGER` NOT NULL | **PRIMARY KEY part** |
| `occurred_at` | `TIMESTAMPTZ` NOT NULL | **PRIMARY KEY part** |
| `section` | `VARCHAR(128)` NOT NULL default `''` | |
| `body` | `TEXT` NOT NULL default `''` | post-redaction row text |
| `import_batch_id` | `UUID` NULL → `import_batch(batch_id)` | |
| `created_at` | `TIMESTAMPTZ` NOT NULL default `now()` | |

Index: `sdlc_ledger_row_occurred_at_idx` on `occurred_at` (time-ordered reads; the ledger is exported by month).

**Why the composite key:** **tick numbers restarted** at 1 after the 2026-09-23 cutover — post-cutover ticks
1..n collide with the pre-cutover range, and the audited ledger (35 320 lines, 1 774 tick rows) has no other
per-row identity. A tick is only unique *inside a session at a point in time*, hence
`(session_ref, tick_number, occurred_at)` — the plan's stated key.

### 3.6 `open_ask` — one row per ask

| Column | Type | Key / constraint |
|---|---|---|
| `ask_id` | `UUID` default `gen_random_uuid()` | **PRIMARY KEY** |
| `task_uid` | `UUID` NULL → `task(task_uid)` | |
| `asked_by` | `VARCHAR(256)` NOT NULL default `''` | |
| `question` | `TEXT` NOT NULL default `''` | |
| `state` | `VARCHAR(32)` NOT NULL default `'open'` | indexed |
| `state_history` | `JSONB` NOT NULL default `'[]'` | ordered `{state, at}` transitions |
| `asked_at`, `answered_at` | `TIMESTAMPTZ` NULL | |
| `answer` | `TEXT` NULL | |
| `import_batch_id` | `UUID` NULL → `import_batch(batch_id)` | |
| `created_at`, `updated_at` | `TIMESTAMPTZ` NOT NULL default `now()` | |

The plan: "the seven existing columns plus state history". The existing open-asks record lives on ptait09,
which no harness run may read (plan rule: nothing from `C:\tmp`); the loader therefore maps the seven source
columns onto this row at import time. What this design adds is the `state_history` list, so the current `state`
column can move (open → answered → stale…) without losing the path it took.

### 3.7 `stage_event` — a task reaching a lifecycle stage

| Column | Type | Key / constraint |
|---|---|---|
| `event_id` | `UUID` default `gen_random_uuid()` | **PRIMARY KEY** |
| `task_uid` | `UUID` NOT NULL → `task(task_uid)` | FK |
| `stage` | `sdlc.lifecycle_stage` NOT NULL | |
| `evidence_ref` | `VARCHAR(1024)` NOT NULL default `''` | run id, PR number, merge commit, report path, … |
| `occurred_at` | `TIMESTAMPTZ` NOT NULL | |
| `import_batch_id` | `UUID` NULL → `import_batch(batch_id)` | |
| `created_at` | `TIMESTAMPTZ` NOT NULL default `now()` | |

Index: `sdlc_stage_event_task_time_idx` on `(task_uid, occurred_at)` — the timeline view.

**Why a text evidence reference, not an FK:** the earliest stage events are reconstructed from loose records
(the harness today stores no PR or evidence field in run records — audit finding), so the proof of a stage can
be a run id, a `report.json` path, a PR number or a commit sha depending on era. `evidence_ref` keeps one
column for all of them; `link` rows carry the typed joins.

### 3.8 `link` — joins between records

| Column | Type | Key / constraint |
|---|---|---|
| `link_id` | `UUID` default `gen_random_uuid()` | **PRIMARY KEY** |
| `task_uid` | `UUID` NOT NULL → `task(task_uid)` | FK |
| `kind` | `sdlc.link_kind` NOT NULL | enum |
| `target` | `VARCHAR(1024)` NOT NULL | run id, queue id `q-<hex>`, PR number, commit sha, gate id, session ref |
| `artifact_id` | `UUID` NULL → `artifact(artifact_id)` | typed reference for `kind = 'artifact'` |
| `import_batch_id` | `UUID` NULL → `import_batch(batch_id)` | |
| `created_at` | `TIMESTAMPTZ` NOT NULL default `now()` | |

`link_kind` — the plan's seven joins: `run`, `queue`, `pr`, `commit`, `gate`, `session`, `artifact`.
Index: **`sdlc_link_task_kind_target_idx`** — `UNIQUE (task_uid, kind, target)`.

**Why:** queue events stay in `lh_harness` unchanged and are "read by `sdlc` through links, not copied" (plan,
Where the data sits) — `kind = 'queue'` rows are those pointers. The PR/commit kinds close the audit gap
"no PR link in run records". The unique triple makes the loader's re-runs idempotent.

### 3.9 `import_batch` — provenance of each load

| Column | Type | Key / constraint |
|---|---|---|
| `batch_id` | `UUID` default `gen_random_uuid()` | **PRIMARY KEY** |
| `source_host` | `VARCHAR(64)` NOT NULL default `''` | indexed with `started_at` |
| `started_at` / `finished_at` | `TIMESTAMPTZ` | |
| `counts` | `JSONB` NOT NULL default `'{}'` | per-kind row counts |
| `redaction_report` | `JSONB` NOT NULL default `'{}'` | file, kind, count — never the value |

Every imported row type (`artifact_version`, `ledger_row`, `open_ask`, `stage_event`, `link`) carries an
`import_batch_id`, so a bad load is traceable and reversible by batch.

---

## 4. Redaction rules

Verbatim from the plan (Redaction rules):

1. **Replace matched secret values with `[REDACTED:<kind>]`; keep the rest of the file.**
2. **Files that are themselves secrets** (`.redis_pw`, `.ops_ingest_key`, `.tok*`, `_tnas_keys.txt` and
   similar) **are recorded by name, size and hash only. No body is stored.**
3. **The exporter writes a redaction report: file, kind, count. Never the value.**
4. **A second independent scan runs over the finished bundle. Any hit fails the export.**

Context from the audit the rules answer: ~470 scratch scripts carry a hard-coded harness token; ~30 more files
are secrets or contain key literals. Ledger, open asks, specs and queue entries are clean. Redaction runs **on
ptait09 before anything leaves the machine** (Paxton's decision): harness runs on CT110 cannot read `C:\tmp`,
and unredacted files must not be copied to reach them.

---

## 5. Export bundle format

Content-addressed blobs plus a JSONL manifest. Produced by the exporter (`scripts/sdlc_export.py`, build
sequence task 2), consumed by the loader (`scripts/sdlc_load.py`, task 3) via `POST /api/sdlc/import`.

**Layout** (proposed here, derived from the plan's "content-addressed storage" decision):

```
<bundle>/
  manifest.jsonl          one JSON object per artifact version (below)
  redaction-report.jsonl  one JSON object per redacted file: {path, kind, count} — never the value
  secrets-by-name.jsonl   name/size/sha256-only records for files that are themselves secrets (rule 2)
  blobs/
    <sha256[:2]>/<sha256> raw body bytes, one file per distinct content
```

**Manifest line** (one per artifact version):

```json
{"artifact": {"kind": "spec", "task_number": 283, "slug": "…", "title": "…"},
 "original_path": "queue/…", "written_at": "2026-09-28T20:29:06Z", "backup_suffix": "",
 "sha256": "<64 hex>", "byte_size": 1234, "redactions": 3}
```

- **Content addressing:** each distinct body lands in `blobs/` exactly once, keyed by sha256; every copy is a
  manifest line pointing at it. The 508 ledger copies collapse to far fewer blobs (verification item 1 of the
  plan). The sharded `<sha256[:2]>/` prefix is a conventional content-addressed layout detail, not a plan
  decision.
- **Redaction first:** all bodies in `blobs/` are post-redaction (rule 1 ran on ptait09); the bundle cannot
  contain a secret value unless a file evaded the patterns — which the second scan (rule 4) exists to catch,
  and any hit fails the export before it is copied to CT110.
- **Provenance:** the loader opens an `sdlc.import_batch` row per load and stamps every imported row with it;
  the batch carries the source host, the counts and the redaction report.

---

## 6. API summary

Full contract: **`docs/api/sdlc-openapi.yaml`** (OpenAPI 3.1). Two services, each extending an API that
already exists; **writes happen in one place only.**

**Write and capture API — LongHorizon-Harness on CT110** (FastAPI, `src/lh_harness/webapi/server.py`,
bearer token). Seven write endpoints:

| Method and path | Purpose |
|---|---|
| `POST /api/sdlc/tasks` | Create or update a task record |
| `POST /api/sdlc/tasks/{task_uid}/stages` | Record a stage reached, with its evidence reference |
| `POST /api/sdlc/artifacts` | Store a document version (body, kind, links) |
| `POST /api/sdlc/links` | Join a task to a run, queue id, PR, commit or session |
| `POST /api/overseer/ledger` | Append a ledger row |
| `POST /api/overseer/open-asks` | Create or update an open ask |
| `POST /api/sdlc/import` | Load an exported bundle (used by the backfill) |

The same operations are exposed as MCP tools beside the six read tools in `overseer_state.py`, so the chat
overseer and the gateway reach them as `hydrafleet-*`. The FastAPI routes publish the spec at `/openapi.json`.

**Read API — fleet-admin** (Express, `src/server.js`, new router `buildSdlcRouter()` in `src/sdlc.js`, mounted
at `/api/sdlc`, GET only, behind the existing read key). Eight read endpoints:

| Path | Returns |
|---|---|
| `/api/sdlc/tasks?stage=&repo=&q=&from=&to=` | Task list with current stage |
| `/api/sdlc/tasks/{task_uid}` | One task: stages, links, documents |
| `/api/sdlc/tasks/{task_uid}/timeline` | Stage events and ledger rows in time order |
| `/api/sdlc/artifacts/{id}` and `/versions` | A document and its version history |
| `/api/sdlc/artifacts/{id}/diff?from=&to=` | Difference between two versions |
| `/api/sdlc/search?q=&mode=keyword\|semantic` | Keyword from Postgres, semantic from qdrant |
| `/api/sdlc/stats` | Counts by stage, repo and month |

**Conventions to follow** (plan): fleet-admin reads the `sdlc` schema directly through `src/db.js` (both
schemas live in `agent-db`); new tables go in `schema.sql` **and** the copy inside `src/test/helpers.js` which
the tests use; handlers use the existing `guarded()` and `relay()` wrappers and the 502/503/404 behaviour of
`src/overseer.js`.

**Automatic capture** (plan, Going forward): task enqueued → spec text, queue entry, `QUEUED` stage event;
run launched/gated/finished → stage events from `harness.queue_events` and the run's `report.json`; PR opened
or merged → PR link and merge commit (which the harness does not record today); overseer tick → ledger row via
the new write endpoint; handoff or decision written → document plus links to the tasks it names.

---

## 7. Migration dry run and validation status

`migrations/004_sdlc.sql` follows the style of migrations 001–003: schema-qualified `CREATE … IF NOT EXISTS`
throughout, enum types inside `DO` blocks that check `pg_type`, credentials never in the file, and a header
recording what the file may touch. It creates objects **only** in schema `sdlc`; it alters no existing schema
(`harness`, `hivemind_sessions`, `fleet` untouched). It is **not applied to any database** by this task.

**Validation command** (throwaway Postgres; from the file header):

```bash
docker run -d --name sdlc-004-check -e POSTGRES_PASSWORD=throwaway postgres:16
until docker exec sdlc-004-check pg_isready -q; do sleep 1; done
docker exec -i sdlc-004-check psql -U postgres -d postgres -v ON_ERROR_STOP=1 -f - < migrations/004_sdlc.sql
docker exec -i sdlc-004-check psql -U postgres -d postgres -v ON_ERROR_STOP=1 -f - < migrations/004_sdlc.sql
docker rm -f sdlc-004-check
```

Applied twice with `ON_ERROR_STOP=1`: the second apply proves re-run safety.

**Validation status on 2026-09-29: NOT RUN — no docker on the authoring node (CT110), and no local PostgreSQL
binaries (`initdb`/`pg_ctl`/`psql`) exist either** (`dpkg -l`, `PATH`, `/usr/lib/postgresql` all checked).
Per the operator decision for this task (1b), the documented command above ships unexecuted rather than
validating on any other host. This is recorded for the PR body; applying the migration in an overseer window
remains gated per the plan's CT103 rule (UAT first, script with dry run by default).
