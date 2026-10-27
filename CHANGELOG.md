# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Node-wide atomic run admission and a memory headroom gate in the launcher**
  (LHH-CT110-MEMORY-ADMISSION, after the OOM incident in which lane caps
  summing past the node maximum let far more runs run at once than the node
  could hold: the OOM killer took `lh-harness.service` and every launched
  run ended "worker disappeared without a final report").  Before any
  launch, the launcher now makes ONE admission decision per launch cycle
  under a lock: it counts running + starting runs across ALL lanes from the
  authoritative run store and refuses once the count reaches
  `LH_HARNESS_NODE_MAX_RUNS` (new setting, default 5, stored in the settings
  DB like the lane caps), regardless of lane caps; then it applies a memory
  headroom gate that refuses when `MemAvailable` minus the startup allowance
  `LH_HARNESS_RUN_STARTUP_MB` (default 600) is below the floor
  `LH_HARNESS_MEM_FLOOR_MB` (default 1024), recording `memory_headroom` as
  the entry's latest skip reason -- the new per-entry
  `latest_skip_reason` field, which is `None` on an entry that was never
  skipped and is returned by the queue API alongside the full
  `skip_reasons` history.  The same cap is enforced a second time
  at the shared launch boundary (`RunSupervisor.create_run`), where every
  launch path -- including a direct `POST /api/runs` -- takes a PENDING
  STARTUP reservation under a lock before any worker is spawned; the
  reservation is released when the worker is published, when the start
  fails, and when the worker exits or crashes.  At service start the
  launcher reconciles from live processes and run dirs and marks orphaned
  launched entries failed with the existing crash-naming reason, never
  requeueing them beyond `retry_of`.  A node already over the cap at start
  kills nothing and admits nothing until the count drops below it.
  `/api/meta` gains `admission: {node_max, running, starting,
  mem_available_mb, blocked_reason}`, and a startup check warns (and the
  node gate clamps) when the lane caps sum past the node maximum -- with a
  regression test pinning that incident shape.  See
  docs/operations/admission.md for how the three limits compose and how to
  raise caps safely.  A refusal from the shared supervisor boundary is now
  treated as a skip, not a launch failure: the queue entry stays PENDING
  with the refusal recorded as its latest skip reason (so it keeps the
  `memory_headroom` marker and does not burn a retry), and `POST /api/runs`
  reports it as 429 rather than an unhandled 500.  This change is source
  only -- it is not deployed here, and the live CT110 node picks the cap and
  the memory gate up only on its next deploy (Paxton's go).
- **The launcher enforces a node-level active-run ceiling.** `[queue] node_max_runs`
  is the maximum number of runs the launcher will let be active on THIS node at
  once — the value the fleet registry advertises as `maxConcurrentRuns`. Until
  now the launcher enforced only the per-trio caps in `[queue.capacity]`, and
  because those sum past the node maximum a node could run well over its own
  advertised limit (ct110 on 2026-10-22: 11 active runs against an advertised
  maximum of 5, with per-trio caps summing to 13).  `_run_pass` now counts the
  active runs from the same `_active_runs()` set it already counts per-trio
  occupancy from, and once the node maximum is reached it skips the entry
  through the existing pending path with
  `node at capacity: <n> active runs, node max <m>`.  The per-trio caps keep
  working unchanged and the tighter of the two wins.  The key is a NEW,
  documented config key (the harness had no node-level value before; the
  advertised number lived only in the Hydra node registry): it defaults to
  `5`, matching what ct110 advertises today, and a node that advertises
  something else (ct111: `2`) sets it explicitly.  No existing per-trio cap,
  node maximum value or default was changed.

- **Queue entries carry a go-to-market manager (`gtm_manager`).** A new
  optional enqueue field names the overseer that owns the task: `overseer1` or
  `overseer1-op` (GTM-MANAGER-G1-a, 2026-10-22). It is separate from the
  creator fields (`requested_by`, `requester`): other agents create tasks, so
  the creator is not the manager. Absent means the default manager
  (`overseer1`), any other value is refused by the enqueue path's own
  validation error (422 on REST, `code: 422` on MCP), and the value is stored
  on the entry, returned by `GET /api/queue` / `GET /api/queue/{id}`, and
  carried onto a requeued successor. Migration
  `007_harness_queue_gtm_manager.sql` adds the Postgres column
  (`VARCHAR(32) NOT NULL DEFAULT 'overseer1'`); an entry written before the
  field existed reads back as the default manager.

- **Postgres queue: retry and backoff columns.** Migration
  `005_harness_queue_retry_backoff.sql` adds `retry_of`, `attempt`,
  `failure_cause`, `not_before` and `wait_reason` to `harness.queue`, and
  `PgQueueStore` writes and reads them like the file store: a requeued entry
  keeps its lineage (the `max_retries` cap now trips through re-reads) and its
  provider-quota wait (the launcher's `waiting:` skip works on Postgres). Also
  fixed so the backend runs on a real psycopg 3 server at all: the store's
  `with self._txn()` blocks now run on a cursor with commit/rollback (a
  psycopg 3 connection closes at the end of `with conn:` and has no
  `fetchall`), JSONB `skip_reasons` decoded by the driver are kept, a dedup hit
  returns the full row, and a requeue carries `branch`/`continue_branch`.

- **Run list failure fields.** `GET /api/runs?fields=summary` rows of terminal
  runs now carry `outcome` (the terminal lifecycle status, else report.json
  `status`), `abort_reason` and `failure_reason` (`<abort_reason> |
  <failure_reason>` from report.json plus the supervisor's `failure_reason`,
  else a supervisor-generated report's `error`, else the worker's `Stopped:`
  line). Both reasons are ASCII-only and at most 200 characters. Live rows are
  unchanged (the keys are absent), so existing readers see the same shape.
  report.json is parsed once per finished run (memoized on mtime/size).
  Hydra's `list_fleet_runs` can now show why a run created outside the queue
  failed. New module `src/lh_harness/run_outcome.py`.

- **Experience layer (MSCE Phase 1).** Optional valued L1 trace persistence:
  one redacted JSONL record per managed round written to
  `role_orchestration/experience.jsonl` in the run dir at finalization,
  carrying a deterministic terminal reward split into goal/process/satisfaction
  terms (R = 0.45·goal + 0.30·process + 0.25·satisfaction), a per-round
  reflection weight from the independent audit, and Eq. 2 value backfill
  (γ = 0.9).  Off by default (`LH_HARNESS_EXPERIENCE=1` or
  `[run] experience = true`); when off, runs are byte-identical to previous
  builds.  New package `src/lh_harness/experience/`; documentation in
  [docs/experience-layer.md](docs/experience-layer.md).
- **Experience read-only API.** The three MSCE levels are viewable by the
  fleet surfaces through `GET /api/experience/environment` (L3 — the seeded
  fleet knowledge: hosts, standing constraints, routing backends, with
  `origin`/`seeded_at`/`source`/`superseded_*` per item),
  `GET /api/experience/policies` (L2 — empty but addressable, paginated,
  with the final item schema declared), and
  `GET /api/experience/runs/<run_id>/traces` (L1 — paginated, redacted
  again at serve time, read from the run-dir ledger through the same
  run-boundary checks as every run-scoped route).  Same bearer middleware as
  the rest of `/api/*`; read-only, no write endpoints.
- **Fleet reporting.** Nodes can now stream live telemetry to
  `fleet.easybutt0n.ai` over a single outbound HTTPS path, so NAT'd hosts are
  visible without any inbound firewall rule.  Enabled only when
  `LH_HARNESS_FLEET_URL` is set; when unset the web server and CLI behave
  exactly as before.
  - New reporter at `src/lh_harness/fleet/reporter.py`: one daemon thread,
    bounded queue, 2 s event batching, gzip, HMAC-SHA256 over the raw body with
    `X-Fleet-Host` + `X-Fleet-Signature`, bounded retry then drop, and WARN-once
    persistent-failure behavior.
  - Event hooks feed a public `EventEnvelope` (no transcripts or thinking).
  - Heartbeat every 30 s carries node identity, version, supervised run
    summaries, and capacity.
  - Round content push uploads every round artifact and role trajectory
    (thinking included as stored) capped at 8 MB per round; larger files are
    represented by `{truncated: true, bytes: N}`.
  - `POST /api/runs` accepts an optional `youtrack_issue_id` that is stored in
    control metadata and included in summaries and heartbeats.
  - Docker node deployment passes through `LH_HARNESS_FLEET_URL`,
    `LH_HARNESS_FLEET_NODE`, `LH_HARNESS_FLEET_KEY`, and `LH_HARNESS_FLEET_LABELS`.
  - Documentation: [docs/fleet-reporting.md](docs/fleet-reporting.md) and README
    env table.
- Service-side task queue with durable atomic JSON persistence under the runs root.
  - `POST /api/queue`, `GET /api/queue`, `DELETE /api/queue/{id}`, and priority endpoints.
  - Capacity-gated launcher with trio limits, workspace collision avoidance, and idempotent restarts.
  - Fleet MCP tools exposed under the LiteLLM alias `lhharness` (access group `fleet-runners`).
  - ASCII-only `harness_resolve_gate` validation and `/api/runs/{id}/approvals/{id}/resolve` wiring.
- End-to-end happy-path test for the local queue API.
  - `e2e/happy_path.sh` and `e2e/happy_path.py` start a local service, use a fake `codex` agent, and assert the run reaches `completed`.
  - Covers `queue.launched`, the inter-tick double-launch race, and `base_check` wiring.
- `Makefile` target `e2e-happy` and pytest marker `e2e` so heavy stack tests run separately from the main suite.
- Server-side caching for `/api/runs/{id}/snapshot`, cutting full snapshot p95 latency from ~37 ms to ~5 ms and summary snapshot p95 latency from ~37 ms to ~4 ms on a 16-round synthetic run.
- Documentation: `docs/queue.md`, `docs/ux-performance.md`, and `docs/release-checklist-ct110.md`.

### Changed

- Web API now embeds a service-side supervisor and launcher when run with `lh-harness web`.

### Fixed

- Queue launches no longer burn an attempt on an ineligible auditor MCP
  profile (task 234).  An auditor role that would resolve to a non-read-only
  profile is refused on the pre-burn path: the exact supervisor reason
  (`roles.auditor.mcp_profile '<name>' is not read-only; auditor roles
  require a read-only MCP profile`) is appended to the queue entry's
  `skip_reasons`, the entry stays pending with its attempt count unchanged,
  and `mark_failed` is never reached.  The check honours the worker's
  precedence — an explicit `[run.roles.auditor] mcp_profile` in the project
  config wins over the trio's run-wide profile — so a deployment binds the
  auditor to a read-only profile (e.g. built-in `audit`) while the
  manager/executor keep a richer one.
- Race where two queue entries for the same workspace could be launched across concurrent ticks is now prevented by a launch lock and active-run re-check.
- `migrations/001_harness_queue.sql` now applies cleanly to a fresh database as
  a non-superuser role owner. It previously failed under `ON_ERROR_STOP=1` with
  `ERROR: default for column "status" cannot be cast automatically to type
  queue_status` because it created `status` as `VARCHAR NOT NULL DEFAULT
  'pending'` and only converted the column afterwards. The `queue_status` enum
  is now created first (guarded by a `pg_type` check so re-running is a no-op)
  and `status` is declared as that enum with the `'pending'` default directly
  in `CREATE TABLE`. `002_harness_queue_events.sql`'s foreign key is likewise
  guarded, so re-running either file on an already-migrated schema is a clean
  no-op. Covered by `tests/webapi/test_pg_migrations.py` (fresh apply +
  double-apply) and documented in `docs/queue.md` (Postgres backend and
  migration order).
