# V1.5 readiness matrix, row E2b — checkpoint-restore executed/unexecuted matrix (CT111)

**Status:** executed/unexecuted matrix for the E2b case — a run checkpointed
mid-round and restored on a **second worker**. Test target: **CT111**,
targeted tests **timeout 300**. The capability is implemented and its offline
rows execute green on this branch; the on-node CT111 rows are unexecuted. The
matrix is section 4b; every row states its exact test id and evidence.

## Where this sits on main

Main already carries three pieces of the same task family, and this row is
the piece they still do not cover:

* `tests/test_checkpoint_restore_recovery.py` (674 lines, merged by 4b9108021710cf5cad5996c412e6d0dece3e05d6
  through PR #189 on branch `test/e2b-checkpoint-restore-recovery`) covers the
  E2b scenario **offline** with real production code. Assertions 1, 2 and 4
  (resume at the checkpointed round, one record per round, close-out records
  the interruption + the `role_harness_resumed` event with
  `restored_rounds` / `resume_from_round`, `src/lh_harness/manager.py:346-352`)
  pass. The mid-round part of assertion 3 was pinned as a strict xfail on
  origin/main and now executes, its marker removed with the capability.
* `tests/webapi/test_restart_run_recovery.py` (also task easybutt0n-17) pins
  the queue-level restart recovery: one `-r1` successor
  (`src/lh_harness/restart_recovery.py:92` `RETRY_NAME_SUFFIX`), lineage on
  both ends (`retry_of`, `interrupted_run`), and the single-successor fence
  invoked from the launcher (`src/lh_harness/launcher.py:1087`, `:1226`, `:1421`).
* `src/lh_harness/restart_recovery.py` carries the 2026-11-04 CT111
  qualification measurement under lease `ovf-v15-ct111-qual-20261104` — the
  precedent for how an on-node V1.5 execution is authorised and recorded.

What this row adds on top of main — the two behaviours main lacks: a checkpoint
written **while a round is still in flight**
(`manager.py:2752` `_write_mid_round_checkpoint`, invoked at `:448`), and a
restore path that resumes the interrupted round on a second worker instead of
replaying it (`manager.py:2813` `_resumable_mid_round`, wired at `:304-320`,
`:2776` `_load_mid_round_checkpoint`). The merged xfail on main named exactly
this gap; that marker was removed in the same change that landed the
capability.

## Verified code facts this design is grounded in

| Fact | Location |
|---|---|
| Per-round checkpoint path `role_dir/rounds/round_NNN/checkpoint.json` | `src/lh_harness/manager.py:2627` `_round_checkpoint_path` |
| Checkpoint payload: canonical round record + `schema_version`, `checkpoint_kind` (`"managed_round"` at `:2632`, `"managed_round_mid_round"` at `:2637`), `recorded_at`, SHA-256 `fingerprint` | `src/lh_harness/manager.py:2640-2655` `_checkpoint_envelope` |
| Mid-round checkpoint kind, written while the round is still in flight | `src/lh_harness/manager.py:2637` `_CHECKPOINT_KIND_MID_ROUND`, written at `:2752`, invoked at `:448` |
| Checkpoint written at round completion (appends `rounds.jsonl`, writes the checkpoint, emits `managed_round_recorded`) | `src/lh_harness/manager.py:2867` `_record_round` |
| Mid-round restore lookup: a mid-round checkpoint whose round has no ledger record | `src/lh_harness/manager.py:2813` `_resumable_mid_round`, wired at `:304-320` |
| Fail-closed handling of an unusable mid-round checkpoint | `src/lh_harness/manager.py:315` `abort_reason="mid_round_restore_failed"`; rule mirrored from `supervisor/service.py:3379` |
| Restore used by `--resume` reads the ledger of completed rounds | `src/lh_harness/manager.py:2561` `_recorded_rounds` |
| Fail-closed fingerprint validation before an in-place continue | `src/lh_harness/supervisor/service.py:3379` `_validate_latest_round_checkpoint`, invoked at `:3209` |
| Restart-interrupted queue entry → exactly one successor, lineage on both ends | `src/lh_harness/restart_recovery.py:89` (`INTERRUPTED_BY_RESTART`), `:92` (`-r1`), `:227` (`begin_service_epoch`), `:963-965` (the `find_restart_successor` idempotency fence that returns `None` when a successor exists) |
| Queue entries are atomic JSON files `queue/<queue_id>.json` under the runs root (file store; `PgQueueStore` optional) so they survive restarts | `src/lh_harness/queue.py:4`, `:35`; `src/lh_harness/pg_queue.py:326` |
| Offline second-worker convention: a fresh `run(..., resume=True)` call with no shared in-memory state | `tests/test_e2b_mid_round_restore.py:293-298` and `:413-418`; `tests/test_checkpoint_restore_recovery.py:241` `_run_manager` |
| Targeted-test invocation conventions | `.github/workflows/pr-gate.yml:33` (job timeout), `:44` (build step), `:60` (`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m pytest -p no:cacheprovider -q -rf --junitxml=...`); `tests/conftest.py` pins the checkout's own `src` tree ahead of any installed copy |
| CT111 runbook and its provisioning boundary | `docs/nodes/ct111-cfo1.md` ("DRAFT PR. No live provisioning has happened."), `scripts/deploy/node/VERIFICATION-ct111.md`, `scripts/deploy/nodes.json` (ct111 entry), `scripts/deploy/node/templates/config.ct111.toml` |

## 1. Scenario

Row E2b exercises a **restart-restore of an in-flight round onto a second
worker**, end to end:

1. **Run is launched and reaches round N.** The queue entry that launched it
   is in `launched` (the entry's durable `queue/<queue_id>.json` under the
   runs root). The run's ledger (`rounds.jsonl`) holds rounds `1..N-1`, each
   with a completed-round checkpoint. Round N is **in flight**: the manager
   has planned, the executor has produced (or is producing) the round's
   effect, and `_record_round` has **not** been called for N.
2. **The first worker is lost mid-round** (a real or simulated `SIGKILL`, the
   same stand-in the merged suite uses, its `_Killed` exception).
   The durable state after the loss: ledger has `1..N-1`, completed-round
   checkpoints for `1..N-1`, **plus the mid-round checkpoint for N** — the
   capability this row delivers, written by `_write_mid_round_checkpoint`
   (`manager.py:2752`) as the round's phases advance (`manager.py:448`).
3. **A second worker starts and performs the restore.** "Second worker" means
   a fresh worker process with no shared in-memory state: offline, that is a
   separate `run(..., resume=True)` invocation against the same durable
   run/queue state (`tests/test_e2b_mid_round_restore.py:293-298`); on CT111
   it is the worker launched after the service restart, with the restart
   proven the way the merged suite proves it (`supervisor_pid` mismatch via
   `begin_service_epoch`, `restart_recovery.py:227`, as asserted by
   `tests/webapi/test_restart_run_recovery.py`).
4. **The restored run resumes round N from the mid-round checkpoint**
   (continues the round from the recorded phase) instead of replaying it from
   scratch, finishes round N, and completes. The lookup is `_resumable_mid_round`
   (`manager.py:2813`), which accepts a mid-round checkpoint **only** for a
   round that has no ledger record.
5. **Queue and run state survive:** the queue entry chain remains exactly one
   entry continuing (or at most one `-r1` successor with `retry_of` +
   `interrupted_run` lineage); no queue state is lost or duplicated; the run's
   ledger, per-round checkpoints, events and final report are complete and
   consistent with the post-restore execution, and the close-out records the
   interruption.

## 2. Restore trigger

Two triggers, layered — the completed-round one, and the mid-round one this
row added:

* **Completed rounds.** On `resume`/`continue`, the worker restores rounds
  from the ledger (`manager.py:2561` `_recorded_rounds`) and, for in-place
  continue, the supervisor validates the latest completed-round checkpoint
  fingerprint and **fails closed** if it is missing, malformed or tampered
  (`supervisor/service.py:3379` `_validate_latest_round_checkpoint`, called at
  `:3209`).
* **Mid-round (the trigger this row added).** At worker start-up, before the
  ledger restore is assumed complete, the worker looks for a **mid-round
  checkpoint**: a checkpoint for round N (whose `checkpoint_kind` distinguishes
  the in-flight record) whose round **has no `rounds.jsonl` ledger record** —
  i.e. the round never completed. If the file exists and its fingerprint
  validates, the second worker RESUMES round N from it (`manager.py:2813`
  `_resumable_mid_round`, wired at `:304-320`). If the round has neither a
  ledger record nor a mid-round checkpoint (pre-capability state, or a torn
  write), the existing behaviour stands: the round is replayed and the
  close-out records the replay. The fail-closed rule from `service.py:3379`
  extends unchanged: an invalid mid-round checkpoint must never be silently
  discarded or silently trusted — restore aborts with
  `abort_reason="mid_round_restore_failed"` (`manager.py:315`), the run ends
  failed, and the human-visible close-out names the round and the reason.

For the queue side the trigger is the existing restart path: a `launched`
entry whose run died is reconciled by `reconcile_interrupted_run` from the
launcher (`launcher.py:1421`), creating at most the one `-r1` successor with
both lineage fields set — or, for the continue-in-place design this row
targets, the original entry stays bound to the same run that the second
worker restores. Exactly one of those two outcomes may exist for a given
interruption; the successor idempotency fence (`restart_recovery.py:963-965`) is what enforces
"never two".

## 3. Invariant

The restore on the second worker must preserve, durably, all of:

* **No round duplicated.** One record per round index in `rounds.jsonl`
  (the merged suite's assertion 2,
  `tests/test_checkpoint_restore_recovery.py::test_no_round_is_duplicated_across_the_manager_loss`);
  the interrupted round N appears exactly once after the restore, attributed
  to the post-restore execution — a mid-round checkpoint is resumed, not
  re-executed-and-recorded-twice.
* **No effect issued twice.** The interrupted round's effect (agent
  execution, push, PR, gate resolve) is observed once across both workers
  (assertion 3 at large, now including the executed mid-round part,
  `tests/test_checkpoint_restore_recovery.py::test_a_mid_round_checkpoint_lets_the_restart_resume_without_replaying_the_effect`);
  on the queue side, reconciling twice and even two launchers racing still
  yield exactly one successor (`restart_recovery.py:24-26, 44-45`).
* **Queue survives.** Entries live as atomic JSON files under the runs root
  (`queue.py:4, 35`); after the cross-worker restore, the entry chain for the
  task is exactly one of: (a) the original entry still `launched`, bound to
  the restored run, or (b) the original terminal entry plus **one** `-r1`
  successor with `retry_of` and `interrupted_run` set. No entry is created
  for a round or a run that was not interrupted; terminal states are not
  rewritten.
* **Run state survives.** The ledger, the per-round checkpoints (completed
  rounds keep their fingerprints; the mid-round checkpoint either graduates
  into the round's completed record or is recorded as superseded — never
  silently deleted), `events.jsonl` (including `role_harness_resumed` with
  `restored_rounds` / `resume_from_round`, `manager.py:349`), and the
  final `report.json` are all readable and consistent with the post-restore
  history. A torn or tampered file in any of these is a **failed restore**,
  never a best-effort skip (`service.py:3379`).
* **Interruption is recorded.** The close-out of the lost worker names the
  interruption (`abort_reason: "worker_exception"`), and the second worker's
  close-out carries the `role_harness_resumed` event plus the
  `restored_from_mid_round_checkpoint` round metadata (`manager.py:2730`,
  `:2740`), so a later reader can tell a restored in-flight round from a
  clean round.

## 4. Pass criteria

Mid-round checkpoint writing and restore **exist on this branch**
(`manager.py:2752`, `:2776`, `:2813`, wired at `:304-320` and `:448`) and do
**not** exist on origin/main, where the merged suite pinned their absence with
`xfail(strict=True)`. That marker was removed in the same change that landed
the capability, so the mid-round case now **executes and passes**. What that
does and does not establish is the matrix in section 4b:

* **Offline pass criteria (CI-grade, pytest, no network), mirroring the
  merged suite's conventions:**
  1. The completed-round restore on a second worker keeps passing, with the
     queue and run-state invariants above asserted (rows 1, 4, 6, 10, 13 in
     the matrix — the already-passing set the new test reuses rather than
     duplicates).
  2. The mid-round case is written against the real production interfaces and
     **passes**, because the mid-round checkpoint writer + restore path landed
     and the marker was removed in the same change (rows 2, 3, 9). A soft
     xfail, a skip, or a test that "passes" by not executing the restore path
     does not satisfy this row.
  3. Both workers in the offline test are fresh invocations with no shared
     in-memory state; the only bridge between them is durable state under the
     test's own temp `runs`/queue root, in the formats production code writes
     (`queue.py` entries, `rounds.jsonl`, `round_NNN/checkpoint.json`,
     `report.json`, `events.jsonl`).
* **On-node pass criteria (CT111, the "test executed on CT111" clause)** —
  rows 17–19, **unexecuted**:
  1. The targeted test file runs on CT111 with the project's invocation
     conventions and a hard **timeout 300** for the targeted run, e.g.
     `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src timeout 300 python -m pytest
     tests/test_e2b_mid_round_restore.py -p no:cacheprovider -q` executed in
     the node's checkout. The 300-second timeout applies to the targeted test
     execution only (the pr-gate job timeout, `pr-gate.yml:33`, is 40 minutes
     for the full suite and is not this row's budget).
  2. The run is recorded the way the CT111 qualification precedent is
     recorded: an effect-lease entry (`restart_recovery.py` header documents
     lease `ovf-v15-ct111-qual-20261104`), the test log and exit code kept as
     execution evidence, and the outcome written into the run's report
     (including a failed or xfailed targeted test — evidence beats green).
  3. No merge, no deploy, no prod restarts: the row's execution may stop or
     restart the harness **test** process only; it must not restart
     `lh-harness.service` on ct110, ct111 or any other node, must not deploy a
     wheel, and must not merge anything.
* **Escalation when CT111 is unreachable through an authorised path:** see
  the execution-target section below.

## 4a. Full-suite status (ACCEPTANCE 2): UNCLOSED, not PASS

`/tmp/venv137/bin/python -m pytest tests -q` completes with 7 or 8 failures
depending on one timing-sensitive test. Captures at this tree, each
labelled by the head it was taken at:

* `easybutt0n-137-fullsuite-at-0c62540-raw.txt` — `8 failed, 2553 passed, 44 skipped`
* `easybutt0n-137-fullsuite-final2-raw.txt` — `7 failed, 2554 passed, 44 skipped`
* `easybutt0n-137-fullsuite-final3-raw.txt` — `8 failed, 2553 passed, 44 skipped`
* `easybutt0n-137-fullsuite-final4-raw.txt` — `8 failed, 2553 passed, 44 skipped`
* `easybutt0n-137-fullsuite-final5-raw.txt` — `7 failed, 2554 passed, 44 skipped`

The variable one is exactly `tests/webapi/test_restart_run_recovery.py::test_live_stale_worker_makes_the_successor_wait`,
which fails in the first and third captures and passes in the second (and
passes in the targeted per-node capture, `PASSED [ 74%]`). Its presence is
the whole difference in both totals. None of the failures is on the E2b
capability; the stable seven are the *same ids* the clean detached worktree at
origin/main `9a01b5e` reports
(`easybutt0n-137-baseline-main-final-raw.txt`: **8 failed, 2550 passed, 44
skipped, 1 xfailed**). They are environmental and are reported **UNCLOSED**,
never waived and never PASS:

* `tests/test_deepseek_harness_adapter.py::test_deepseek_adapter_runs_end_to_end_with_fake_dsh`
* `tests/test_worker_rss_bound.py::test_v8_shaped_reservation_survives_an_rss_bound`
* `tests/test_worker_rss_bound.py::test_resident_blowup_is_killed_alone_inside_its_own_cgroup`
* `tests/test_workspace_identity.py::test_identity_resolves_main_branch`
* `tests/test_workspace_identity.py::test_repo_id_strips_credentials`
* `tests/test_workspace_identity.py::test_repo_id_for_scp_remote`
* `tests/test_workspace_identity.py::test_worktree_common_dir_is_absolute_and_same_as_primary`

Eighth id, flaky and likewise **UNCLOSED**, never PASS:

* `tests/webapi/test_restart_run_recovery.py::test_live_stale_worker_makes_the_successor_wait`

The baseline also reports `1 xfailed` and this branch reports none: that is the
target capability's strict xfail, present on main and removed on this branch.

The 44 skips are condition-gated. The `-q` ACCEPTANCE-2 capture does not print
`SKIPPED` lines, so they are enumerated in the dedicated `-rs` capture
`easybutt0n-137-final2-skipids-raw.txt` (the ten skipping files run with `-rs`
at head `80463e6`; `244 passed, 44 skipped in 6.15s`). The same twelve skip
groups and the same 44-skip total were reported at `1408d92`
(`easybutt0n-137-final-skipids-raw.txt`). Verbatim:

```
SKIPPED [1] tests/webapi/test_pg_migrations.py:31: could not import 'psycopg': No module named 'psycopg'
SKIPPED [1] tests/supervisor/test_hardening.py:208: host has no macOS /var alias
SKIPPED [1] tests/test_ask_store.py:350: needs setfacl
SKIPPED [2] tests/webapi/test_enqueue_contract.py:43: LH_HARNESS_DB_URL not set; Postgres backend unavailable
SKIPPED [30] tests/webapi/test_pg_queue.py:65: LH_HARNESS_DB_URL not set; Postgres backend unavailable
SKIPPED [1] tests/webapi/test_pg_queue.py:385: LH_HARNESS_DB_URL not set; Postgres backend unavailable
SKIPPED [1] tests/webapi/test_pg_queue.py:417: LH_HARNESS_DB_URL not set; Postgres backend unavailable
SKIPPED [3] tests/webapi/test_pg_queue_retry_columns.py:261: LH_HARNESS_DB_URL not set; Postgres backend unavailable
SKIPPED [1] tests/webapi/test_pg_queue_task_overrides.py:457: LH_HARNESS_DB_URL not set; Postgres backend unavailable
SKIPPED [1] tests/webapi/test_queue_gtm_manager.py:417: LH_HARNESS_DB_URL not set; Postgres backend unavailable
SKIPPED [1] tests/webapi/test_settings_admin.py:172: web UI not built in this checkout
SKIPPED [1] tests/webapi/test_webapi.py:300: web bundle not built; run `npm run build --prefix frontend/web`
```

That is 44 in total. Every one is **UNCLOSED** and routed to overseer1 with
that status; none is on the E2b capability.

## 4b. Executed / unexecuted matrix

Legend — **Executed**: the row's exact pytest node id ran and PASSED in a
capture named in the Evidence column. **Unexecuted**: the row states its exact
test id or artifact and the reason it did not run. **Waived**: the single
precise `easybutt0n-48` gap (row 16).

Evidence captures (workspace root, all produced by running this branch's own
tree). The `tests` tree hash is `3992485d6b95c681ddc48fe9c190b01f563f4c7b` and
the `src` tree hash is `9b119598b1405b085a8a8a49f70f9b0e0d8129aa` at every head
from `1408d92` through `24056a6` (`git rev-parse <head>:tests`, `<head>:src`),
and this branch's commits since `1408d92` touch only this document. Every
capture below therefore exercises the same `tests/` and `src/` code, which is
what makes a per-node capture taken at `1408d92` evidence for a row at the
pushed head. Each capture is labelled by the head it was taken at:

| Capture | Command | Content |
|---|---|---|
| `easybutt0n-137-matrix-nodes-raw.txt` | `pytest tests/test_e2b_mid_round_restore.py tests/test_checkpoint_restore_recovery.py tests/webapi/test_restart_run_recovery.py -p no:cacheprovider -v` | Per-node-id PASSED lines for every executed offline row (`51 passed in 4.08s`), at `1408d92` |
| `easybutt0n-137-acc1-at-0c62540-raw.txt` | `pytest tests/test_e2b_mid_round_restore.py -q` | ACCEPTANCE 1 at `0c62540` (`2 passed in 0.90s`) |
| `easybutt0n-137-fullsuite-at-0c62540-raw.txt` | `pytest tests -q` | ACCEPTANCE 2 at `0c62540` (`8 failed, 2553 passed, 44 skipped`) |
| `easybutt0n-137-acc1-final2-raw.txt` | `pytest tests/test_e2b_mid_round_restore.py -q` | ACCEPTANCE 1 at head `80463e6` (`2 passed in 0.89s`) |
| `easybutt0n-137-fullsuite-final2-raw.txt` | `pytest tests -q` | ACCEPTANCE 2 at head `80463e6` (`7 failed, 2554 passed, 44 skipped`) |
| `easybutt0n-137-acc1-final3-raw.txt` | `pytest tests/test_e2b_mid_round_restore.py -q` | ACCEPTANCE 1 at head `8120231` (`2 passed in 0.90s`) |
| `easybutt0n-137-fullsuite-final3-raw.txt` | `pytest tests -q` | ACCEPTANCE 2 at head `8120231` (`8 failed, 2553 passed, 44 skipped`) |
| `easybutt0n-137-acc1-final4-raw.txt` | `pytest tests/test_e2b_mid_round_restore.py -q` | ACCEPTANCE 1 at head `24056a6` (`2 passed in 0.92s`) |
| `easybutt0n-137-fullsuite-final4-raw.txt` | `pytest tests -q` | ACCEPTANCE 2 at head `24056a6` (`8 failed, 2553 passed, 44 skipped`) |
| `easybutt0n-137-acc1-final5-raw.txt` | `pytest tests/test_e2b_mid_round_restore.py -q` | ACCEPTANCE 1 at head `7dc5918` (`2 passed in 0.92s`) |
| `easybutt0n-137-fullsuite-final5-raw.txt` | `pytest tests -q` | ACCEPTANCE 2 at head `7dc5918` (`7 failed, 2554 passed, 44 skipped`) |
| `easybutt0n-137-acceptance1-final-raw.txt` | `pytest tests/test_e2b_mid_round_restore.py -q` | ACCEPTANCE 1 at `1408d92` (`2 passed in 1.52s`) |
| `easybutt0n-137-fullsuite-final-raw.txt` | `pytest tests -q` | ACCEPTANCE 2 at `1408d92` (`7 failed, 2554 passed, 44 skipped`) |
| `easybutt0n-137-baseline-main-final-raw.txt` | same command, detached worktree at origin/main `9a01b5e` | Clean-main baseline for failure classification (`8 failed, 2550 passed, 44 skipped, 1 xfailed`) |

| # | E2b requirement | Status | Exact test id | Evidence |
|---|---|---|---|---|
| 1 | Completed-round restore on a second worker reaches `complete` after the resume | Executed | `tests/test_e2b_mid_round_restore.py::test_the_completed_round_restore_on_a_second_worker_keeps_queue_and_run_state_intact` | matrix-nodes `PASSED [  1%]` |
| 2 | Mid-round checkpoint **written** while round N is in flight (file exists for a round with no ledger record, fingerprinted) | Executed | `tests/test_e2b_mid_round_restore.py::test_a_mid_round_checkpoint_restores_the_in_flight_round_on_a_second_worker` | matrix-nodes `PASSED [  3%]` |
| 3 | Mid-round **restore**: the second worker resumes round N instead of replaying it | Executed | `tests/test_e2b_mid_round_restore.py::test_a_mid_round_checkpoint_restores_the_in_flight_round_on_a_second_worker` | matrix-nodes `PASSED [  3%]` |
| 4 | No round duplicated across the manager loss | Executed | `tests/test_checkpoint_restore_recovery.py::test_no_round_is_duplicated_across_the_manager_loss`; also asserted in rows 1 and 3 | matrix-nodes `PASSED [  7%]` |
| 5 | Restore resumes at the checkpointed round | Executed | `tests/test_checkpoint_restore_recovery.py::test_the_run_resumes_at_the_checkpointed_round_after_manager_loss` | matrix-nodes `PASSED [  5%]` |
| 6 | Restored run records that it resumed (`role_harness_resumed`, `restored_rounds`, `resume_from_round`) | Executed | `tests/test_checkpoint_restore_recovery.py::test_the_resumed_run_records_that_it_resumed_from_the_checkpoint`; also rows 1 and 3 | matrix-nodes `PASSED [ 11%]` |
| 7 | Close-out records the manager interruption | Executed | `tests/test_checkpoint_restore_recovery.py::test_the_close_out_records_the_manager_interruption` | matrix-nodes `PASSED [  9%]` |
| 8 | Interrupted run's effects detected once and reconciled once | Executed | `tests/test_checkpoint_restore_recovery.py::test_the_interrupted_runs_effects_are_detected_once_and_reconciled_once` | matrix-nodes `PASSED [ 13%]` |
| 9 | Mid-round checkpoint lets the restart resume **without replaying the effect** (assertion 3, part 3; effect count `== 2`, `tests/test_e2b_mid_round_restore.py:424`) | Executed | `tests/test_checkpoint_restore_recovery.py::test_a_mid_round_checkpoint_lets_the_restart_resume_without_replaying_the_effect` | matrix-nodes `PASSED [ 17%]` |
| 10 | **Queue fence** — interrupted entry gets exactly one `-r1` successor, lineage on both ends (`retry_of`, `interrupted_run`, `successor`); store holds exactly 2 entries | Executed | `tests/webapi/test_restart_run_recovery.py::test_interrupted_run_gets_exactly_one_r1_successor`; `tests/test_e2b_mid_round_restore.py::test_the_completed_round_restore_on_a_second_worker_keeps_queue_and_run_state_intact` (`:253` `retry_of`, `:255` `interrupted_run`, `:259` `successor`, `:262` and `:275` `len(store.list()) == 2`) | matrix-nodes `PASSED [ 19%]`, `[  1%]` |
| 11 | **One-successor fence** holds on a second reconcile (`second is None`, `len(store.successors) == 1`) | Executed | `tests/webapi/test_restart_run_recovery.py::test_reconcile_twice_still_yields_one_successor`; `::test_reconcile_is_idempotent_when_called_directly_twice`; `tests/test_checkpoint_restore_recovery.py::test_reconciling_an_interrupted_run_twice_creates_only_one_successor` (`:610` `second is None`, `:611` `len(store.successors) == 1`) | matrix-nodes `PASSED [ 27%]`, `[ 29%]`, `[ 15%]` |
| 12 | **Run fence** — lineage and stash name written into the run report | Executed | `tests/webapi/test_restart_run_recovery.py::test_the_lineage_is_written_into_the_run_report` | matrix-nodes `PASSED [ 80%]` |
| 13 | **Checkpoint fence** — restored checkpoints re-validate under the supervisor's fail-closed fingerprint rule | Executed | `tests/test_e2b_mid_round_restore.py::test_the_completed_round_restore_on_a_second_worker_keeps_queue_and_run_state_intact` (helper `_checkpoint_fingerprint_validates`, called at `:318`); `...::test_a_mid_round_checkpoint_restores_the_in_flight_round_on_a_second_worker` (same helper) | matrix-nodes `PASSED [  1%]`, `[  3%]` |
| 14 | **Retry-lineage fence** — a decided effect makes the successor continue that branch; an undecided one creates no successor | Executed | `tests/webapi/test_restart_run_recovery.py::test_pushed_branch_makes_the_successor_continue_that_branch`; `::test_open_pr_makes_the_successor_continue_that_branch`; `::test_uncertain_effects_mark_the_entry_needs_review_and_create_no_successor` | matrix-nodes `PASSED [ 66%]`, `[ 68%]`, `[ 70%]` |
| 15 | The offline E2b suites as a whole run green | Executed | all 51 node ids in `easybutt0n-137-matrix-nodes-raw.txt` | matrix-nodes `51 passed in 4.08s` |
| 16 | **Waived:** the absent-mid-round-capability gap named by `easybutt0n-48` (the interrupted round was replayed, its executor/effect could run twice) | **Waived** | — | the waiver paragraph at the end of this section (text verbatim); the capability that closes it is executed in rows 2, 3 and 9 |
| 17 | On-node execution of the targeted test on **CT111**, 300-second bound, retained evidence | **Unexecuted** | `tests/test_e2b_mid_round_restore.py` (whole file), under the CT111 invocation | reason: CT111 is unprovisioned and no authorised channel exists (section 5); nothing was run on any node |
| 18 | On-node recording against the CT111 effect lease (as `ovf-v15-ct111-qual-20261104`) | **Unexecuted** | — (no lease entry exists) | reason: no lease was granted and no on-node run happened |
| 19 | Full E2b PASS on CT111 including the mid-round case | **Unexecuted** | — | reason: rows 17–18 unexecuted; the offline rows establish behaviour, not on-node qualification |

**Row coverage against the previous version of this document** — no previous
row is dropped. The earlier version had no numeric matrix; its rows were the
five scenario steps (§1), the two restore triggers (§2), the five invariants
(§3), and the offline/on-node pass criteria (§4). Each maps as follows:

| Previous row | Now |
|---|---|
| §1 step 1 — run reaches round N, ledger `1..N-1`, round N in flight | row 2 (in-flight checkpoint written) |
| §1 step 2 — first worker lost mid-round; durable state keeps the mid-round checkpoint for N | rows 2, 3 |
| §1 step 3 — second worker starts, fresh process, no shared memory | rows 1, 3, 5 |
| §1 step 4 — resumes round N from the mid-round checkpoint, completes | rows 3, 9 |
| §1 step 5 — queue and run state survive, close-out records interruption | rows 4, 6, 7, 10-14 |
| §2 trigger 1 — completed rounds restore from ledger, fail-closed fingerprint | rows 5, 13 |
| §2 trigger 2 — mid-round checkpoint restored; fail-closed on an invalid one | rows 2, 3 |
| §3 invariant "no round duplicated" | row 4 |
| §3 invariant "no effect issued twice" | rows 8, 9 |
| §3 invariant "queue survives" | rows 10, 11, 14 |
| §3 invariant "run state survives" | rows 6, 12, 13 |
| §3 invariant "interruption is recorded" | row 7 |
| §4 offline criterion 1 — completed-round restore keeps passing with queue/run invariants | rows 1, 4, 6, 10, 13 |
| §4 offline criterion 2 — mid-round case passes when the capability lands | rows 2, 3, 9 |
| §4 offline criterion 3 — both workers fresh; durable state is the only bridge | rows 1, 3, 5 |
| §4 on-node criterion 1 — targeted test on CT111, timeout 300 | row 17 (unexecuted) |
| §4 on-node criterion 2 — recorded against the CT111 effect lease | row 18 (unexecuted) |
| §4 on-node criterion 3 — no merge/deploy/prod restart | row 19 (unexecuted); boundary restated in §5 |
| §4 waiver paragraph — `easybutt0n-48` gap | row 16 (Waived, text verbatim) |

### 4c. Evidence provenance

Every capture cited above is a workspace file produced by this branch's own
tree, with the `git head` and tracked-tree cleanliness it was captured at
recorded inside the file itself:

| Capture | How produced | Provenance to the current tree |
|---|---|---|
| `easybutt0n-137-matrix-nodes-raw.txt` | `/tmp/venv137/bin/python -m pytest <the three E2b suites> -p no:cacheprovider -v` run in this workspace | header line records `git head 1408d92`; `git diff 1408d92 0c62540 -- tests/ src/` is empty, so it exercises the same `src/` and `tests/` blobs as the `0c62540` captures |
| `easybutt0n-137-acc1-at-0c62540-raw.txt` | `/tmp/venv137/bin/python -m pytest tests/test_e2b_mid_round_restore.py -q` | header line records `captured at pushed head 0c6254046104f7ea86b316df07a7101e25c4c5b4` and `exit=0` |
| `easybutt0n-137-fullsuite-at-0c62540-raw.txt` | `/tmp/venv137/bin/python -m pytest tests -q` | same header, same head (`8 failed, 2553 passed, 44 skipped`) |
| `easybutt0n-137-acc1-final2-raw.txt` | `/tmp/venv137/bin/python -m pytest tests/test_e2b_mid_round_restore.py -q` | header line records `captured at pushed head 80463e6d8dc1e38f3875e5679eb68bbd3601507d` and `exit=0` |
| `easybutt0n-137-fullsuite-final2-raw.txt` | `/tmp/venv137/bin/python -m pytest tests -q` | same header, same head, plus the tracked-tree status; `7 failed, 2554 passed, 44 skipped`, `# exit=1` |
| `easybutt0n-137-final2-skipids-raw.txt` | `pytest <the ten skipping files> -q -rs` | header records `captured at pushed head 80463e6…`; `244 passed, 44 skipped in 6.15s` |
| `easybutt0n-137-acc1-final3-raw.txt` | `/tmp/venv137/bin/python -m pytest tests/test_e2b_mid_round_restore.py -q` | header records `captured at pushed head 8120231…` and `exit=0` |
| `easybutt0n-137-fullsuite-final3-raw.txt` | `/tmp/venv137/bin/python -m pytest tests -q` | header records the same head plus the tracked-tree status; `8 failed, 2553 passed, 44 skipped`, `# exit=1` |
| `easybutt0n-137-final3-failids-raw.txt` | `grep '^FAILED'` over the final3 capture | the eight exact ids, each verified present in the clean-main baseline |
| `easybutt0n-137-lsremote-final3-raw.txt` | `git ls-remote` + `git rev-parse` + `git log` at head `8120231` | remote head equals local head, zero `forced-update` |
| `easybutt0n-137-acc1-final4-raw.txt` | `/tmp/venv137/bin/python -m pytest tests/test_e2b_mid_round_restore.py -q` | header records `captured at pushed head 24056a6…`, the `tests`/`src` tree hashes, and `exit=0` |
| `easybutt0n-137-fullsuite-final4-raw.txt` | `/tmp/venv137/bin/python -m pytest tests -q` | same head and tree hashes plus the tracked-tree status; `8 failed, 2553 passed, 44 skipped`, `# exit=1` |
| `easybutt0n-137-final4-failids-raw.txt` | `grep '^FAILED'` over the final4 capture | the eight exact ids, each verified present in the clean-main baseline |
| `easybutt0n-137-lsremote-final4-raw.txt` | `git ls-remote` + `git rev-parse` + `git log` at head `24056a6` | remote head equals local head `24056a6`, zero `forced-update`, `9a01b5e` is the merge-base with origin/main |
| `easybutt0n-137-acc1-final5-raw.txt` | `/tmp/venv137/bin/python -m pytest tests/test_e2b_mid_round_restore.py -q` | header records the head and both tree hashes; `2 passed`, `exit=0` |
| `easybutt0n-137-fullsuite-final5-raw.txt` | `/tmp/venv137/bin/python -m pytest tests -q` | same head and tree hashes plus the tracked-tree status; `7 failed, 2554 passed, 44 skipped`, `# exit=1` |
| `easybutt0n-137-final5-failids-raw.txt` | `grep '^FAILED'` over the final5 capture | the seven exact ids, each verified present in the clean-main baseline |
| `easybutt0n-137-lsremote-final5-raw.txt` | `git ls-remote` + `git rev-parse` + `git log` at head `7dc5918` | remote head equals local head, zero `forced-update`, `9a01b5e` is the merge-base with origin/main |
| `easybutt0n-137-acceptance1-final-raw.txt` | `/tmp/venv137/bin/python -m pytest tests/test_e2b_mid_round_restore.py -q` | header line records `git head 1408d92` and an empty tracked-tree status |
| `easybutt0n-137-fullsuite-final-raw.txt` | `/tmp/venv137/bin/python -m pytest tests -q` | same header, same head |
| `easybutt0n-137-baseline-main-final-raw.txt` | same command in a detached worktree at `origin/main` `9a01b5e` (`/tmp/e2b-main-baseline`, created read-only from the same object store) | header line names the detached worktree and `9a01b5e` |
| `easybutt0n-137-final-skipids-raw.txt` | `pytest tests -q -rs` summary extraction | header line records `git head 1408d92` |
| `easybutt0n-137-existing-surface-r3.txt` | `python3 -m graphify query '<the six affected files>' --budget 4000` then `python3 -m graphify explain 'src/lh_harness/manager.py'`, run **before** this round's edit, written outside `graphify-out/` | verbatim tool output; names `src/lh_harness/manager.py` 23 times, including the `explain` node whose `Source:` line is `src/lh_harness/manager.py L1`. Both the query header and the `explain` block are the tool's own stdout |
| `easybutt0n-137-existing-surface-r2.txt` | the same two commands, run before the previous round's edit | superseded by `-r3` only in that it predates the current edit; it likewise names `src/lh_harness/manager.py` (23 times) |

**Round_004 leftovers — unverified, not cited.** The nine files
`easybutt0n-137-acceptance1-literal-raw.txt`,
`-acceptance1-raw-r2.txt`, `-acceptance1-systempython-raw.txt`,
`-fence-nodes-raw.txt`, `-fullsuite-literal-raw.txt`,
`-fullsuite-main-raw-r2.txt`, `-fullsuite-raw-r2.txt`,
`-fullsuite-skipids-raw.txt` and `-fullsuite-skipnodeids-raw.txt` carry no
executor report and no audit verdict, so their provenance is unestablished.
They are **not** cited for any matrix row. Their contents are consistent with
this tree (same failure sets; `-fence-nodes-raw.txt` shows the same 51 node
ids passing) but consistency is not provenance, and they are retained
untouched rather than deleted.

The mid-round restore gap is an approved waiver under ticket `easybutt0n-48`,
enforced by the strict xfail (`xfail(strict=True)` at
`tests/test_e2b_mid_round_restore.py:352`). The condition it covers is
documented at lines 176-177 of this document.

*Reconciliation of the enforcement clause (this round).* The enforcement
mechanism above is now stale and is recorded here, not silently dropped: the
strict xfail named in that sentence no longer exists, because the same change
that landed `_write_mid_round_checkpoint` / `_resumable_mid_round`
(`src/lh_harness/manager.py`) removed it. The waived condition — an in-flight
round replayed with its executor/effect run twice — is therefore closed by
rows 2, 3 and 9 of the matrix above, and the waiver now covers only its
historical scope, not any executed row. Its wording, and the scope stated
verbatim in `CHANGELOG.md`, are unchanged; the waiver is neither widened nor
narrowed, and rows 17-19 remain unexecuted with no on-node or full-E2b PASS
claimed.

The waiver paragraph above is reproduced byte-for-byte as approved, including
its positional cross-reference "lines 176-177". That reference was written
against the pre-matrix version of this file, where those two lines carried the
pass criterion naming the `xfail(strict=True)` convention. After this round's
rewrite the same line numbers fall inside section 4's offline pass criteria, so
the reference is preserved as approved text rather than retargeted; the
condition it names is now recorded in criterion 2 of that list and in the
paragraph you are reading.

## 5. Execution target and authorisation boundary

CT111 is the execution target for this row, and it is currently
**unprovisioned**: `docs/nodes/ct111-cfo1.md` is a DRAFT PR and says so
("Status: DRAFT PR. No live provisioning has happened."), with the runbook and
acceptance checklist in `scripts/deploy/node/VERIFICATION-ct111.md` — which
itself states that nothing there "can be automated from a harness run:
Proxmox provisioning and Hydra registration are host-side steps for overseer1
(gateway ssh/proxmox tools) or a Paxton session".

Design decisions that follow, binding for the implementing rounds:

* **Never prohibited access.** The executor for this task may only touch its
  workspace and the git transport to the origin remote. Reaching CT111 by
  ssh, `pct`, remote-exec, gateway ssh, e2b provisioning or a deploy route is
  out of bounds for this run — this row's on-node stage may therefore proceed
  **only** through an authorised channel (for example a lease the operator
  grants and an overseer-run execution, as in the recorded
  `ovf-v15-ct111-qual-20261104` qualification), or it is escalated back to
  the overseer/user (`Next: ask` / `Next: blocked`) rather than satisfied by
  prohibited access. An unavailable CT111 never converts a targeted test into
  a locally-run substitute labelled "executed on CT111".
* **CT111 appears as written documentation until an authorised channel
  exists** (this doc, plus the test's CT111-execution plan). This matrix's
  rows 17-19 are therefore marked unexecuted, not passed; an unavailable CT111
  never converts a targeted test into a locally-run substitute labelled
  "executed on CT111".
* **No merge, no deploy, no prod restarts** applies on every round of this
  row, including on-node ones.

## 6. Test conventions the implementation must follow

* pytest invocation with `PYTHONPATH=src` and
  `PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider -q` (`.github/workflows/pr-gate.yml:60`);
  `pip install -e '.[test]'` plus `pyyaml` is the build step (`:44`); the job
  timeout is 40 minutes for the whole suite (`:33`) — the 300-second budget in
  this row scopes the targeted test execution.
* `tests/conftest.py` (TASK 211) pins the checkout's own `src` tree ahead of
  any installed copy and re-primes already-imported modules, so both the
  offline and the CT111 runs test the tree being run.
* The tests belong beside `tests/test_checkpoint_restore_recovery.py`, reuse
  its offline helpers (`_run_manager`, `_ScriptedAgent`, `_Killed`,
  `_make_workspace_repo`, `_checkpointed_rounds`, the ledger / checkpoint /
  report readers) and extend rather than duplicate the merged coverage — as
  the two tests in `tests/test_e2b_mid_round_restore.py` do; the queue side is
  asserted with the real `QueueEntry`/`QueueStore` as
  `tests/webapi/test_restart_run_recovery.py` does against a throwaway store
  — never a live workspace.
