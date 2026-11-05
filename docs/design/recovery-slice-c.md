# Recovery, slice C: five recovery gaps around restart-interrupted runs

Task `easybutt0n-17` · 2026-11-05 · base commit `c0991926ccb235b85affbf1d7a225ebb49f49603`
("Merge pull request #182 from cogniziocompany/fix/restart-run-recovery")

This is a **design document only**. It adds no code, no migration and no test. Every claim
below is read off the code at the base commit; the symbol and line references are the
evidence, not a restatement of the commit subjects.

---

## 0. Scope: what #182 already owns, and what this slice is about

PR #182 ("restart-interrupted run recovery") closed one specific hole: when the *service
itself* stopped, the workers it owned died, and their queue entries went to `failed` with no
successor. #182 classifies that death as `interrupted_by_restart` and gives the entry **one**
fenced second chance — a single `-r1` successor created through the ordinary `requeue` path.
Its whole implementation lives in one module, `src/lh_harness/restart_recovery.py`, called
from the launcher's existing mark-failed site (`Launcher._update_launched_entries`,
`src/lh_harness/launcher.py:1226`; the restart branch is at `:1294`–`:1326`).

**This document does not redesign #182 and does not restate it.** It starts from the five
properties #182 established, treats them as invariants that any further recovery work must
keep, and designs the five *adjacent* recovery areas that #182 deliberately left open:

| # | Slice-C area | Status at base commit |
|---|---|---|
| 1 | Durable checkpoint restore of an in-flight round | partial (machinery exists, not wired to the restart successor) |
| 2 | Whole-host / `runs_root` loss | no-handling |
| 3 | Manager-layer restart in non-web launcher shapes | partial, with a demonstrated defect |
| 4 | Reconcile idempotency across concurrent reconciler processes | partial (sequential only) |
| 5 | Worker loss without a service stop | partial (deliberate negative policy, no recovery) |

### 0.1 Invariants inherited from #182 (must hold for every case below)

Each design case in §1–§5 is checked against these five, and any implementation that breaks
one is out of bounds. The code location named with each is the piece that enforces it today.

- **G1 — One fenced successor, never two.** A restart retry exists only if no successor for
  that `retry_of` exists already, and only if the entry has not already had its restart
  second chance. `restart_recovery.find_restart_successor` (`:447`),
  `restart_recovery.already_restart_retried` (`:463`), applied in
  `restart_recovery.reconcile_interrupted_run` (`:908`).
- **G2 — Effects are detected before any successor exists.** A pushed branch, an open PR,
  local commits and a dirty tree are probed *before* the successor is created; an
  undecidable effect (the strict `gh` probe returns `PROBE_UNKNOWN`, `:133`) marks the entry
  `needs_review` and creates **no** successor.
  `restart_recovery.probe_open_pr_gh_strict` (`:699`),
  `restart_recovery.detect_run_effects` (`:760`), and the `effects.decided` gate in
  `reconcile_interrupted_run`.
- **G3 — Lineage is recorded on both ends.** The successor carries `retry_of` and
  `interrupted_run`; the original records the successor's queue id; the stash name and the
  detected effects land in the interrupted run's durable report.
  `queue.QueueStore.requeue` (`src/lh_harness/queue.py:1767`),
  `restart_recovery._record_successor` (`:1038`), `restart_recovery.log_lineage_in_report`
  (`:891`).
- **G4 — Stash-only cleanup.** `git stash push -u -m "lh-harness interrupted <run id>"`,
  the stash kept and named in the report. Never `git clean`, never `git reset --hard`.
  `restart_recovery.stash_interrupted_workspace` (`:533`),
  `restart_recovery.log_stash_in_report` (`:569`).
- **G5 — No two workers for one task.** A restart successor does not launch while the
  interrupted run's own worker is still alive; it waits.
  `restart_recovery.workspace_worker_is_alive` (`:859`),
  `Launcher._interrupted_worker_alive_reason` (`src/lh_harness/launcher.py:1462`).

The boundary in one sentence: **#182 decides whether a *run* gets a successor queue entry;
it never replays a round, never moves state between hosts, and never arbitrates between two
processes.** Slice C is exactly about those three things.

### 0.2 Method note

`graphify-query_graph` was not available in this run (no such executable on `PATH`, and no
graphify resource advertised by any configured MCP server). The coverage map in §6 and every
reference in this document therefore come from direct reads of the checkout at the base
commit, not from a graph query.

---

## 1. Durable checkpoint restore of an in-flight round

**Failure mode.** A run is killed mid-round (service restart, host reboot, OOM). The run
directory holds a complete ledger of finished rounds and a per-round checkpoint, but the
recovery path that #182 adds starts the successor as a **fresh run**: the successor's worker
is spawned without `--resume`, so the manager replays from round 1 and re-does work that is
already on disk. The operator sees a `-r1` entry that looks like a retry and is in fact a
restart.

**Detection.** At the point a successor is created, the original run's directory contains
`role_orchestration/rounds/round_NNN/checkpoint.json` for at least one round, and the
`rounds.jsonl` ledger is non-empty, while the successor run directory does not exist yet.
Both facts are readable from `runs_root` before any worker is spawned
(`manager._recorded_rounds`, `src/lh_harness/manager.py:2385`; `_round_checkpoint_path`,
`:2451`).

**Recovery.** Resume the successor's manager in `resume=True` shape against the original
round state rather than creating a new run directory:

1. Validate the latest checkpoint with the existing fail-closed check
   (`RunSupervisor._validate_latest_round_checkpoint`, `:3357`) — missing, malformed or
   tampered checksum aborts the restore and falls back to the current fresh-start
   behaviour.
2. Rebuild the round list from the ledger (`_recorded_rounds`) and continue at
   `round_index + 1`, exactly as `manager.run()` already does when `resume` is true
   (`src/lh_harness/manager.py:280`–`:290`, and the `role_harness_resumed` event at `:316`).
3. Keep G2/G4 ahead of this step: effects are still detected first and the workspace is
   still stashed first; a restore never overwrites uncommitted work.
4. Keep G3: the successor keeps `retry_of` / `interrupted_run`, and the report records
   `resume_from_round`.

The single missing link today is the wiring: `Launcher._launch`
(`src/lh_harness/launcher.py:1981`) calls `RunSupervisor.create_run` (`:2106`), which reaches
`_create_run_once` (`src/lh_harness/supervisor/service.py:2095`) — a function with **no
`resume` parameter**. `_worker_command` (`:1859`) only appends `--resume` when told to
(`resume: bool = False` at `:1874`, append at `:1901`), and the only caller that passes
`resume=True` is `_continue_run_in_place` (`:3236`, argument at `:3319`). So the durable
round state is written correctly and read correctly, and simply never reaches the successor.

**Proof test.** Build a run directory with two recorded rounds and a valid checkpoint, drive
the restart branch to a successor, then assert: (a) the successor worker command contains
`--resume`; (b) the manager's first action is `role_harness_resumed` with
`resume_from_round == 2`; (c) round 3 executes before round 1 is ever mentioned; (d) a
tampered checkpoint makes the restore refuse and the run fall back to a fresh start with an
explicit reason, not a silent replay. The checkpoint validator has no test today — nothing
under `tests/` opens `checkpoint.json` — so this test is new coverage either way.

**Invariant check.** G1 unaffected (successor identity is unchanged). G2/G4 must be
re-asserted in the test because the restore touches the same workspace the stash cleanup
runs on. G3 gains one report field. G5 unaffected.

---

## 2. Whole-host / `runs_root` loss

**Failure mode.** The host (or the volume holding `runs_root`) is lost. Nothing about the
in-flight work survives: the round ledger, the per-round checkpoints, the queue entries, the
service epoch, the stash — all of it is on the lost disk. Today the only copy of round
content that ever leaves the host is outbound telemetry, and there is no path back.

**Detection.** Not a runtime detection problem — the recovery signal is "the runs root is
new" (no queue directory, no epoch file, no prior run directories) while the fleet side has
records for run ids this host does not know. That comparison is only possible where the
fleet data is read back, which does not exist (§ evidence below), so in practice this is an
operator-declared event.

**What survives today** (all verified by reading the code):

- `runs_root`-local: `role_orchestration/rounds.jsonl` and
  `rounds/round_NNN/checkpoint.json` (`manager.py:266`, `:2385`, `:2451`), the queue store
  under `runs_root/queue/` (`queue._QUEUE_DIR` at `src/lh_harness/queue.py:35`; per-entry
  `q-*.json` via `_write`, `:1487`), and the service epoch at
  `runs_root/queue/service_epoch.json` (`restart_recovery.epoch_path`, `:167`). Everything
  that #182 relies on for classification is therefore in the same blast radius as the run
  data itself: losing the host loses the proof *and* the subject.
- Postgres queue backend: the queue *rows* survive (they live in `harness.queue`, migrated
  by `_MIGRATION_RESTART_RECOVERY_SQL`, `src/lh_harness/pg_queue.py:143`), but the round
  state does not — `runs_root` is still local-only, and the PG store has no `runs_root`
  attribute at all (`Launcher._acquire_lease`, `src/lh_harness/launcher.py:1040`, returns
  early for exactly that reason).
- Outbound only: `fleet.reporter.post_round_content`
  (`src/lh_harness/fleet/reporter.py:1022`) ships round artifacts to fleet-admin. There is
  **no** counterpart anywhere in `src/lh_harness/fleet/` that fetches, downloads or imports
  run state back.
- Retention: run directories older than `DEFAULT_RUN_MAX_AGE_DAYS = 14`
  (`src/lh_harness/disk_retention/config.py:58`) are pruned by `prune_runs`
  (`src/lh_harness/disk_retention/runs.py:240`), which preserves only the failed-report
  files under `runs/_failed-reports/<id>/`. So even on a *live* host, round state older
  than 14 days is gone by design, and `LIVE_STATES` (`:62`) is the only protection for an
  in-flight run.

**Recovery.** The design is a two-part restore, and it must be built in this order:

1. **A durable second copy of the round ledger and checkpoints**, content-addressed by run
   id and round index, shipped the same way round content already is
   (`post_round_content`), with the checkpoint fingerprint as the integrity check. The
   ledger is append-only and the checkpoint is self-verifying, so the pair restores
   cleanly without a transaction log.
2. **An explicit import path**: `lh-harness` gains a command that materialises
   `runs/<id>/role_orchestration/…` for a known run id from that copy, validates the
   checkpoints with the existing fail-closed validator, and only then lets the queue entry
   be launched with the §1 restore shape. Import must never fabricate a round: a missing or
   mismatched checkpoint aborts the import.

Until part 1 exists, part 2 has nothing to read, and any "cross-host restore" claimed today
would be a fresh start wearing a restored run id — which is worse than an honest failure
because it breaks G2 (the effects of the lost run were never detected) without saying so.

**Proof test.** (a) With the second copy enabled, kill a host after round 2, restore into an
empty `runs_root`, and assert the run continues at round 3 with the original queue id and
`retry_of` lineage. (b) Corrupt one checkpoint blob in the copy and assert the import
refuses and the entry stays `needs_review` rather than starting fresh. (c) With the copy
disabled, assert the tool reports "no restore source" instead of silently creating a new
run.

**Invariant check.** G2 is the hard one: an imported successor must detect the lost run's
effects (branch pushed / PR open) *before* it is allowed to start, exactly as a local one
does. G4: an import overwrites a workspace only if it is clean; a dirty tree keeps the
stash path. G3: the imported run records the same lineage plus `restored_from`.

---

## 3. Manager-layer restart in non-web launcher shapes

**Failure mode.** The service epoch is the proof #182 uses for "the service stopped". It is
written by the launcher's constructor. But a launcher is constructed in shapes other than
the systemd `lh-harness web` unit — most importantly by the **embedded dashboard**, which
`lh-harness run` starts by default (`--dashboard`, `default=run_default("dashboard", True)`,
`src/lh_harness/cli.py:779`) over an `attached_only` supervisor
(`src/lh_harness/cli.py:2021`–`:2024`). When that dashboard shares the service's runs root,
its constructor **overwrites the epoch**, naming the still-running service as the
"previous" instance. From then on a worker that died while the service was up matches the
previous-epoch test and is misclassified as restart-interrupted.

This is not hypothetical. Constructing a launcher over an existing epoch
`{pid: 1111, previous_pid: null}` rewrote the file to
`{pid: <new>, previous_pid: 1111, previous_started_at: …}` on the real code, i.e. it
recorded the live service as the previous instance. `classify_worker_death`
(`restart_recovery.py:387`) then returns `INTERRUPTED_BY_RESTART` for a same-service death,
and `Launcher._update_launched_entries` creates a real `-r1` successor for an ordinary
crash. A second, independent defect in the same shape: an `attached_only` supervisor refuses
`create_run` (`src/lh_harness/supervisor/service.py:2124`, "this API is attached to an
existing worker and cannot create runs"), and the launcher's `_launch` turns that refusal
into `mark_failed(..., "launch failed: …")` — so the embedded dashboard can consume a real
pending queue entry and fail it. Both were reproduced directly against the filesystem.

**Detection.** Three structural facts identify a launcher shape that must **not** own the
epoch:

- the supervisor is `attached_only` (it cannot create or resume runs by design);
- the launcher is embedded in a run's own dashboard rather than the standalone web server;
- another live process already owns this runs root (the `.lease` holder, or an epoch whose
  pid is alive and is not this process).

`create_app` currently builds a `Launcher` for **any** non-`None` supervisor and queue
store (`src/lh_harness/webapi/server.py:2039`) with no `attached_only` check, and
`Launcher.__init__` unconditionally calls `_begin_service_epoch` (`launcher.py:806` →
`:830` → `restart_recovery.begin_service_epoch`, `:227`).

**Recovery.** Classify launcher shapes and give each an explicit role:

| Shape | How it is started | Epoch owner? | May launch queue entries? |
|---|---|---|---|
| standalone web service | `lh-harness web` (systemd `packaging/lh-harness.service`; node template `scripts/deploy/node/templates/lh-harness-node.service`) | **yes** — exactly one per runs root | yes |
| `run` without dashboard | `lh-harness run --no-dashboard` | no | no |
| `run` with embedded dashboard | `lh-harness run` (default on) | **no** | no |
| out-of-tree helpers | `tools/spin_watchdog.py`, `tools/optimistic_deploy_harness.py` (edit queue JSON directly) | no — and must not be used | no |

The fix follows directly: epoch bootstrap and launcher construction must be gated on "this
process is the standalone web service for this runs root" — `attached_only` false and no
other live epoch holder — and an embedded launcher must be constructed **without** a
launcher at all, or with launching disabled. `attached_only` already communicates the
intent everywhere else (`create_app` uses it for `/api/maintenance/resume` at
`server.py:2991`); it is simply not consulted at the launcher construction site.

**Proof test.** (a) Construct the embedded-dashboard shape over an existing epoch and assert
the epoch file is byte-identical afterwards and `classify_worker_death` still returns `None`
for a same-service death. (b) Assert an `attached_only` supervisor never causes a pending
entry to be marked failed: the queue row is unchanged after a tick. (c) Assert the
standalone web shape still writes exactly one epoch with `previous_pid` set. (d) A
regression test that the dashboard never creates a successor for an ordinary worker death.

**Invariant check.** G1 is what the defect breaks: an ordinary death gets a successor it
must not have. Fixing the shape restores G1 without touching the reconcile itself. G2–G5 are
unaffected, but the proof test for (a) should also assert no stash and no report lineage was
written for the misclassified run.

---

## 4. Reconcile idempotency across concurrent reconciler processes

**Failure mode.** `reconcile_interrupted_run` is check-then-act: it lists entries, finds no
successor, then calls `requeue`, which mints a new `q-<hex>` id. Two reconciler processes
that overlap in time both see "no successor" and both create one. The result violates G1

— two `-r1` successors for one original — and, with them, two potential workers.

This is real on the code at the base commit: two concurrent callers against one original
entry produced **two** successors (`successors for q-orig: 2`), while the same two calls in
sequence produce one (the existing tests
`test_reconcile_twice_still_yields_one_successor` and
`test_reconcile_is_idempotent_when_called_directly_twice`,
`tests/webapi/test_restart_run_recovery.py:346`, `:376`, cover only the sequential case).

**Detection.** At reconcile time: the entry has `failure_cause == interrupted_by_restart`,
no successor keyed by `retry_of` is visible in the caller's snapshot, and the caller holds no
mutual-exclusion token over that original. The durable, after-the-fact signal is a second
row with the same `retry_of` — which nothing in the schema currently prevents:
`migrations/005_harness_queue_retry_backoff.sql:42` creates `harness_queue_retry_of_idx` as
a **plain partial index**, not a unique constraint, and both stores insert without
`ON CONFLICT` (`queue.QueueStore.requeue` `:1767`; `PgQueueStore.requeue`
`src/lh_harness/pg_queue.py:763`, insert at `:872`).

**Recovery.** Make the successor's identity deterministic and let the store arbitrate:

1. **Derive the successor id** from the original — e.g. `q-<hash(original_queue_id)>` with
   the `-r1` name suffix — instead of minting a random id. Two racers then write the same
   primary key, and the loser's insert fails rather than creating a second entry.
2. **Back it with a unique constraint** on `(retry_of)` where `retry_of IS NOT NULL` (a new
   migration; 005's index becomes redundant), and `INSERT … ON CONFLICT DO NOTHING` in the
   Postgres store, with the file store's atomic create (O_EXCL semantics, already used for
   the lease) doing the same job.
3. **Treat "lost the race" as success**, not as an error: the reconcile returns the existing
   successor (or `None`, per G1's "already exists ⇒ no-op") and never raises into the
   launcher tick.

For completeness, the *adjacent* reconciler is already idempotent in this sense:
`queue_reconcile._apply_reconciliation` (`src/lh_harness/queue_reconcile.py:364`) re-reads
each entry and `continue`s when `status != "launched"`; two overlapping sweeps over one
zombie produced "A applied 1, B applied 0". It has no lock either — its protection is the
status re-read, which leaves a stale-snapshot residual but no duplicate-write path. It is
the model to copy, not a second thing to fix.

**Known residuals to state in the design, not to hide.** The file-store launcher lease
(`queue.py` `.lease`, `_create_lease_excl` `:1384`, `acquire_lease` `:1405`) is O_EXCL and
stale after 3 intervals, but it is a *launcher* lease, not a reconcile lock, and its
documented residual — "there is no fencing token on `mark_launched`" (comment at `:1320`)
— remains true. It is also **skipped entirely for the Postgres backend**:
`Launcher._acquire_lease` returns `True` when the store has no `runs_root`
(`launcher.py:1040`), so a PG deployment has no cross-process exclusion at the file layer at
all. The unique constraint in step 2 is what closes that gap for successors; it does not
close the lease residual, which is out of scope here.

**Proof test.** (a) Two concurrent reconciles over one interrupted entry yield exactly one
successor, whichever order they interleave in (this is the missing test; the existing ones
are sequential). (b) The same test against the PG store asserts `ON CONFLICT` produces one
row. (c) A third reconcile after the fact remains a no-op and does not raise. (d) Two
overlapping `queue_reconcile` sweeps stay at one applied row (already true; pin it).

**Invariant check.** This is G1 made durable: the invariant stops depending on the caller's
snapshot and starts depending on the store. G2/G3/G4 order is unchanged — the loser of the
race must not stash the workspace a second time, so the loser returns before step 4. G5
unchanged.

---

## 5. Worker loss without a service stop

**Failure mode.** A single worker dies while the service keeps running — OOM kill, an
operator's `kill`, a crashed agent process. #182 deliberately does **not** recover this: the
classification requires evidence that the service stopped, and a bare `SIGKILL`/`SIGTERM` is
not that evidence. The entry ends `failed` with an ordinary cause and, for most causes, no
successor. That is the correct conservative choice (a blanket successor for every death
would resurrect task-poisoning failures forever), but it is currently an *implicit* policy:
nothing states what an operator should do, and there is no supported action for the case
where the work was fine and the worker was not.

**Detection.** `restart_recovery.classify_worker_death` (`:387`) returns `None` unless
`worker_belonged_to_previous_epoch` (`:285`) holds, so a same-epoch death falls through to
the ordinary path in `_update_launched_entries`. There the cause is only retried when
`Launcher._is_retryable_cause` (`launcher.py:2360` → `_classify_failure_cause`, `:350`)
matches a provider fault — rate limit / 429, quota, episode timeout, provider stall. Every
other cause, including `"worker disappeared without a final report"` (written by
`src/lh_harness/supervisor/service.py:1645`) and the memory-kill reason produced by
`worker_isolation.classify_memory_death` (prefix `"memory limit exceeded ("`, persisted as a
crash report at `service.py:1678`), stays terminal with no successor. `needs_review` is set
only for undecided effects, never for this.

**Recovery — the explicit policy this slice adds (design, not code).**

- **Ordinary same-epoch death, retryable cause:** unchanged — the existing retry path
  applies. This policy adds nothing.
- **Ordinary same-epoch death, non-retryable cause (including memory kill):** stays
  `failed` with no automatic successor. It is **not** a restart interruption and must not be
  labelled as one (that would break G1 by spending the one restart second chance on the
  wrong event).
- **Operator-initiated recovery:** a documented, explicit action — a new `requeue` with a
  distinct cause (`operator_recovered_after_worker_loss`), a fresh attempt, and the same G2
  preconditions as a restart successor: effects detected first, undecidable ⇒
  `needs_review`, workspace stashed not cleaned, stash named in the report. It is *not*
  automatic and it does not reuse the `-r1` key.
- **Escalation:** a repeated worker loss on the same entry (N≥2 in a window) is surfaced on
  the queue row as `needs_review` with the loss count, so the pattern is visible instead of
  being retried silently.

**Proof test.** (a) A same-epoch `SIGKILL` with a non-retryable cause produces no successor
and no `interrupted_by_restart` label (partly covered today:
`test_same_epoch_worker_sigkill_gets_no_successor`,
`tests/webapi/test_restart_run_recovery.py:270`). (b) A memory-kill death produces no
successor and keeps its `memory limit exceeded` reason. (c) The operator action creates
exactly one successor, with `retry_of` set, cause `operator_recovered_after_worker_loss`,
and does not consume the restart second chance. (d) An operator recovery whose effects are
undecidable marks `needs_review` and creates nothing. (e) The Nth repeated loss sets
`needs_review`.

**Invariant check.** G1: the operator path is a separate cause and a separate key, so it
cannot collide with the `-r1` successor. G2/G4: same preconditions as the automatic path,
asserted by the same tests. G3: lineage recorded both ways. G5: the live-worker gate applies
to the operator successor too.

---

## 6. Coverage map (evidence read from the code at the base commit)

Kept here rather than in the commit message so the design's claims are checkable.

### 6.1 What implements #182's five guarantees

| Guarantee | Files | Symbols |
|---|---|---|
| G1 one fenced successor | `src/lh_harness/restart_recovery.py`, `src/lh_harness/queue.py`, `src/lh_harness/pg_queue.py` | `find_restart_successor:447`, `already_restart_retried:463`, `reconcile_interrupted_run:908`, `QueueStore.requeue:1767`, `PgQueueStore.requeue:763` |
| G2 effects detected first | `restart_recovery.py`, `launcher.py` | `PROBE_UNKNOWN:133`, `probe_open_pr_gh_strict:699`, `detect_run_effects:760`, `Launcher._restart_probe_pr:808`, `_mark_needs_review:1021` |
| G3 lineage both ways | `restart_recovery.py`, `queue.py`, `pg_queue.py` | `_record_successor:1038`, `log_lineage_in_report:891`, `requeue` (`interrupted_run`, `name_suffix`), `_MIGRATION_RESTART_RECOVERY_SQL:143` |
| G4 stash-only cleanup | `restart_recovery.py` | `workspace_is_dirty:524`, `stash_interrupted_workspace:533`, `log_stash_in_report:569` |
| G5 no two workers | `restart_recovery.py`, `launcher.py` | `workspace_worker_is_alive:859`, `Launcher._interrupted_worker_alive_reason:1462` |
| classification / epoch | `restart_recovery.py`, `launcher.py` | `ServiceEpoch`, `epoch_path:167`, `read_epoch:173`, `write_epoch:207`, `begin_service_epoch:227`, `worker_belonged_to_previous_epoch:285`, `death_took_a_shutdown_signal:360`, `classify_worker_death:387`, `Launcher._begin_service_epoch:830`, `Launcher._classify_interrupted_by_restart:1349` |
| integration point | `launcher.py` | `_update_launched_entries:1226` (restart branch `:1294`–`:1326`), `_recover_interrupted_run:1395`, `_launch:1981` |

Migration state: `migrations/` holds files 001–007 only; **migration 009 is applied in-code**
as `_MIGRATION_RESTART_RECOVERY_SQL` (`src/lh_harness/pg_queue.py:143`, executed at `:366`),
adding `interrupted_run`, `successor`, `needs_review`. 008 is the queue-migration tombstone
(PR #173). A slice-C unique constraint would be migration 010.

### 6.2 The five slice-C areas

| Area | Grade | Evidence | Gap |
|---|---|---|---|
| 1. Durable checkpoint restore | **partial** | write: `manager._record_round:2472` → `_round_checkpoint:2456` → `_round_checkpoint_path:2451`; read/validate: `service._validate_latest_round_checkpoint:3357` (fail-closed); restore: `manager.run:280`, `_recorded_rounds:2385` | validator reachable only from `_resume_once:3139` (line `:3187`, `mode="continue"`, via `POST /api/maintenance/resume`, `server.py:2967`). Successors go `launcher._launch:1981` → `create_run:1932` → `_create_run_once:2095`, which has no `resume`; `_worker_command:1859` appends `--resume` only when passed (`:1874`, `:1901`). No test covers `checkpoint.json` at all. |
| 2. Whole-host / `runs_root` loss | **no-handling** | state: `manager.py:266`, `queue.py:35`/`:1487`, `restart_recovery.epoch_path:167`; outbound only: `fleet/reporter.post_round_content:1022`; retention: `disk_retention/config.py:58`, `runs.py:240`, `LIVE_STATES:62`; PG rows only: `pg_queue.py:143` | no inbound fetch/import/restore anywhere in `src/lh_harness/fleet/`; epoch and round state share the lost disk; PG keeps queue rows but not round state |
| 3. Non-web launcher shapes | **partial, defect reproduced** | `Launcher.__init__:806` → `_begin_service_epoch:830` → `begin_service_epoch:227` (unconditional); `server.create_app:2039` builds a Launcher for any supervisor; `cli.py:779` (`--dashboard` default on) + `:2021`–`:2024` (`attached_only`) + `:2039` (`start_web_server`); `service.py:2124` refuses `create_run` | constructing the embedded launcher rewrote `runs_root/queue/service_epoch.json` to `{pid:new, previous_pid:<live service>}`; an `attached_only` launcher marked an unrelated pending entry `failed` ("launch failed: this API is attached to an existing worker and cannot create runs") |
| 4. Reconciler idempotency | **partial (sequential only)** | `find_restart_successor:447`, `already_restart_retried:463`; tests `test_restart_run_recovery.py:346`, `:376`; `migrations/005:42` plain partial index on `retry_of`; `queue.requeue:1767`, `pg_queue.requeue:763` (insert `:872`, random `q-uuid4`); lease `queue.py:1384`/`:1405`; `launcher._acquire_lease:1040` | two concurrent reconciles produced **2** successors for one original; no unique constraint; PG backend has no file lease. Adjacent `queue_reconcile._apply_reconciliation:364` *is* idempotent (verified: A applied 1, B applied 0) |
| 5. Worker loss without a service stop | **partial (explicit negative policy)** | `classify_worker_death:387`, `worker_belonged_to_previous_epoch:285`; `launcher._is_retryable_cause:2360` → `_classify_failure_cause:350`; `service.py:1645` (reason), `:1676` (memory-kill persistence); `worker_isolation.is_memory_kill_reason` | same-epoch death and bare signal are ordinary failures with no successor; memory kill likewise; no operator action; `needs_review` never set for this case |

### 6.3 Test baseline (measured, not added)

- `tests/webapi/test_restart_run_recovery.py` — 42 passed
- `tests/test_resume.py` — 74 passed
- combined — 116 passed in 1.32s
- `tests/test_queue_reconcile_precheck.py` — 19 passed
- checkpoint coverage: **none** (`checkpoint.json` appears in no test)

---

## 7. Research (Sonar Pro)

Five questions were prepared, one per slice-C area. **No answer was obtained and none is
reproduced here.** The LiteLLM router at `https://litellm.easybutt0n.ai/v1/chat/completions`
is reachable, but every credential present in the run environment is refused for the
Perplexity models.

**Questions intended** (recorded so the gap is auditable):

1. What is the state of the art for durable mid-round checkpoint/resume in long-running
   agent harnesses — what is checkpointed, how is integrity proven, and how is a partial
   round detected?
2. How do systems that lose a whole host restore in-flight job state — what is replicated,
   what is accepted as lost, and how is a stale replica prevented from being mistaken for
   live state?
3. How should a process classify "am I the service of record for this state directory"
   when several processes of the same product can be started in different shapes?
4. What patterns make a retry/successor creation idempotent across concurrent reconcilers
   when the underlying store has no transactional uniqueness?
5. What is a defensible automatic-versus-operator policy for retrying work lost to a
   single worker death, and how do mature systems bound retry storms?

**Refusal, recorded exactly** (no key was printed; only environment variable *names* were
read):

- No `LITELLM_API_KEY` / `LITELLM_KEY` is present in the run environment.
- `LH_HARNESS_MCP_GATEWAY_KEY` → HTTP 403, `key_model_access_denied`:
  `key not allowed to access model. This key can only access models=['qwen3.8']. Tried to access sonar-pro:perplexity`
- `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY` → HTTP 403, same error type, key limited to
  an Anthropic/OpenRouter/Cloud model allowlist that contains no Perplexity model.
- Also refused: `sonar-reasoning-pro:perplexity`, `sonar:perplexity`, `sonar-pro`.
- `GET /v1/models`: 51 models for `ANTHROPIC_AUTH_TOKEN`, 1 model for the gateway key,
  **zero** `sonar`/`perplexity` models under either.

There are therefore no cited URLs to record. Nothing in §1–§5 is derived from an external
source; the designs are grounded in the code coverage map in §6 and in #182's stated
invariants.

---

## 8. Remaining work (numbered, in build order)

1. **Restore wiring (§1).** Pass the interrupted run's round state into the successor's
   worker: give the launch path a resume shape, validate the latest checkpoint with
   `_validate_latest_round_checkpoint`, rebuild rounds with `_recorded_rounds`, and assert
   `role_harness_resumed{resume_from_round}` in the successor. Includes the first
   `checkpoint.json` tests.
2. **Cross-host restore (§2).** Ship round ledger + checkpoints as a durable second copy
   content-addressed by run id and round index, and add the explicit import command that
   refuses on a missing or mismatched fingerprint. No cross-host restore may be claimed
   before this exists.
3. **Launcher-shape recovery (§3).** Gate epoch bootstrap and launcher construction on the
   standalone-web shape; never construct a launching launcher over an `attached_only`
   supervisor; regression-test that the embedded dashboard cannot rewrite the epoch or fail
   a pending queue entry.
4. **Reconciler idempotency (§4).** Deterministic successor id derived from the original,
   unique constraint on `retry_of` (migration 010), `ON CONFLICT DO NOTHING` in the PG
   store, and a concurrent-reconcile test for both backends. Keep the adjacent
   `queue_reconcile` sweep behaviour as the model.
5. **Worker-loss policy (§5).** Make the negative policy explicit on the queue row
   (`needs_review` on repeated loss), add the operator-initiated `operator_recovered_after_worker_loss`
   requeue with the G2/G4 preconditions, and test that it neither consumes the restart
   second chance nor bypasses effect detection.
