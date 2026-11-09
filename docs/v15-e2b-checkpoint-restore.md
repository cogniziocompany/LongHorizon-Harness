# V1.5 readiness matrix, row E2b — checkpoint-restore case design (CT111)

**Status:** design + execution constraints for the remaining E2b case: a run
that is checkpointed mid-round and restored on a **second worker**. Test
target: **CT111**, targeted tests **timeout 300**. This round delivers the
case design only; the test implementation and the CT111 execution are later
rounds of the same task (`easybutt0n-17`).

## Where this sits on main

Main already carries three pieces of the same task family, and this row is
the piece they still do not cover:

* `tests/test_checkpoint_restore_recovery.py` (674 lines, merged by 4b9108021710cf5cad5996c412e6d0dece3e05d6
  through PR #189 on branch `test/e2b-checkpoint-restore-recovery`) covers the
  E2b scenario **offline** with real production code. Assertions 1, 2 and 4
  (resume at the checkpointed round, one record per round, close-out records
  the interruption + the `role_harness_resumed` event with
  `restored_rounds` / `resume_from_round`, `src/lh_harness/manager.py:319-322`)
  pass on origin/main. The mid-round part of assertion 3 is pinned as a
  **strict xfail at `:620-634`** of that suite.
* `tests/webapi/test_restart_run_recovery.py` (also task easybutt0n-17) pins
  the queue-level restart recovery: one `-r1` successor
  (`src/lh_harness/restart_recovery.py:92` `RETRY_NAME_SUFFIX`), lineage on
  both ends (`retry_of`, `interrupted_run`), and the single-successor fence
  invoked from the launcher (`src/lh_harness/launcher.py:1087`, `:1226`, `:1421`).
* `src/lh_harness/restart_recovery.py` carries the 2026-11-04 CT111
  qualification measurement under lease `ovf-v15-ct111-qual-20261104` — the
  precedent for how an on-node V1.5 execution is authorised and recorded.

What is **missing on main** — and the only thing this row adds: a checkpoint
written **while a round is still in flight**, and a restore path that resumes
the interrupted round on a second worker instead of replaying it. The merged
xfail's reason names exactly this (`tests/test_checkpoint_restore_recovery.py:620-634`,
backed by `manager.py:2385` `_recorded_rounds` restoring only COMPLETED
rounds, and `manager.py:2472` `_record_round` writing the checkpoint only
when the round completes).

## Verified code facts this design is grounded in

| Fact | Location |
|---|---|
| Per-round checkpoint path `role_dir/rounds/round_NNN/checkpoint.json` | `src/lh_harness/manager.py:2451` `_round_checkpoint_path` |
| Checkpoint payload: canonical round record + `schema_version`, `checkpoint_kind: "managed_round"` (:2466), `recorded_at`, SHA-256 `fingerprint` | `src/lh_harness/manager.py:2456-2469` |
| Checkpoint written only at round completion (appends `rounds.jsonl`, writes the checkpoint, emits `managed_round_recorded`) | `src/lh_harness/manager.py:2472-2491` |
| Restore used by `--resume` reads the ledger of completed rounds | `src/lh_harness/manager.py:2385` `_recorded_rounds` |
| Fail-closed fingerprint validation before an in-place continue | `src/lh_harness/supervisor/service.py:3357`, invoked at `:3187` |
| Restart-interrupted queue entry → exactly one successor, lineage on both ends | `src/lh_harness/restart_recovery.py:89` (`INTERRUPTED_BY_RESTART`), `:92` (`-r1`), `:227` (`begin_service_epoch`), `:919` |
| Queue entries are atomic JSON files `queue/<queue_id>.json` under the runs root (file store; `PgQueueStore` optional) so they survive restarts | `src/lh_harness/queue.py:4`, `:35`; `src/lh_harness/pg_queue.py:326` |
| Strict xfail naming the missing mid-round behaviour | `tests/test_checkpoint_restore_recovery.py:620-634` |
| Offline second-worker convention: a fresh `run(..., resume=True)` call with no shared in-memory state | `tests/test_checkpoint_restore_recovery.py:242-257`, `:261` |
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
   same stand-in the merged suite uses, `tests/test_checkpoint_restore_recovery.py:74-81`).
   The durable state after the loss: ledger has `1..N-1`, completed-round
   checkpoints for `1..N-1`, **plus the mid-round checkpoint for N** (the
   capability this row demands; the merged suite proves this state's sibling
   without the in-flight part).
3. **A second worker starts and performs the restore.** "Second worker" means
   a fresh worker process with no shared in-memory state: offline, that is a
   separate `run(..., resume=True)` invocation against the same durable
   run/queue state (the merged suite's convention, `:242-257`); on CT111 it is
   the worker launched after the service restart, with the restart proven the
   way the merged suite proves it (`supervisor_pid` mismatch via
   `begin_service_epoch`, `restart_recovery.py:227`, as asserted by
   `tests/webapi/test_restart_run_recovery.py`).
4. **The restored run resumes round N from the mid-round checkpoint**
   (continues or re-executes the round from the recorded state) instead of
   replaying it from scratch, finishes round N, and completes.
5. **Queue and run state survive:** the queue entry chain remains exactly one
   entry continuing (or at most one `-r1` successor with `retry_of` +
   `interrupted_run` lineage); no queue state is lost or duplicated; the run's
   ledger, per-round checkpoints, events and final report are complete and
   consistent with the post-restore execution, and the close-out records the
   interruption.

## 2. Restore trigger

Two triggers, layered — the existing one and the one this row demands:

* **Existing (completed rounds only).** On `resume`/`continue`, the worker
  restores rounds from the ledger (`manager.py:2385` `_recorded_rounds`) and,
  for in-place continue, the supervisor validates the latest completed-round
  checkpoint fingerprint and **fails closed** if it is missing, malformed or
  tampered (`supervisor/service.py:3187`, `:3357`).
* **Required (mid-round).** At worker start-up, before the ledger restore is
  assumed complete, the worker looks for a **mid-round checkpoint**: a
  checkpoint for round N (`checkpoint_kind` distinguishing the in-flight
  record) whose round **has no `rounds.jsonl` ledger record** — i.e. the round
  never completed. If the file exists and its fingerprint validates, the
  second worker RESUMES round N from it. If the round has neither a ledger
  record nor a mid-round checkpoint (pre-capability state, or a torn write),
  the existing behaviour stands: the round is replayed and the close-out
  records the replay (this is the state the merged `:620` xfail documents).
  The fail-closed rule from `service.py:3357` extends unchanged: an invalid
  mid-round checkpoint must never be silently discarded or silently trusted —
  restore aborts, the run ends failed, and the human-visible close-out names
  the round and the reason.

For the queue side the trigger is the existing restart path: a `launched`
entry whose run died is reconciled by `reconcile_interrupted_run` from the
launcher (`launcher.py:1421`), creating at most the one `-r1` successor with
both lineage fields set — or, for the continue-in-place design this row
targets, the original entry stays bound to the same run that the second
worker restores. Exactly one of those two outcomes may exist for a given
interruption; the successor fence (`restart_recovery.py:919`) is what enforces
"never two".

## 3. Invariant

The restore on the second worker must preserve, durably, all of:

* **No round duplicated.** One record per round index in `rounds.jsonl`
  (the merged suite's assertion 2, `tests/test_checkpoint_restore_recovery.py:309-335`);
  the interrupted round N appears exactly once after the restore, attributed
  to the post-restore execution — a mid-round checkpoint is resumed, not
  re-executed-and-recorded-twice.
* **No effect issued twice.** The interrupted round's effect (agent
  execution, push, PR, gate resolve) is observed once across both workers
  (assertion 3 at large, `:414-611` today plus the `:620` xfail pinning the
  missing piece); on the queue side, reconciling twice and even two launchers
  racing still yield exactly one successor (`restart_recovery.py:24-26, 44-45`).
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
  `restored_rounds` / `resume_from_round`, `manager.py:319-330`), and the
  final `report.json` are all readable and consistent with the post-restore
  history. A torn or tampered file in any of these is a **failed restore**,
  never a best-effort skip (`service.py:3357`).
* **Interruption is recorded.** The close-out of the second worker's run
  names the interruption (`abort_reason`/`worker_exception` today, or the
  mid-round restore event added by the capability), so a later reader can
  tell a restored in-flight round from a clean round.

## 4. Pass criteria

**The capability gap decides how a pass is even possible, so it is stated
first.** Mid-round checkpoint writing and restore do **not** exist on
origin/main (merged suite `:620-634`, strict xfail; `manager.py:2451`,
`_record_round` at `:2472-2491` writing only at completion; `_recorded_rounds`
at `:2385` restoring only completed rounds). Consequences for this row:

* The **full E2b case cannot pass on main today**, in CI or on CT111. Honesty
  about test status is part of this row's deliverable: the test round must
  report the strict-xfail status as the case's status rather than presenting
  an XPASS, a skip, or the completed-round-only test as the E2b pass.
* **Offline pass criteria (CI-grade, pytest, no network), mirroring the
  merged suite's conventions:**
  1. The completed-round restore on a second worker keeps passing, with the
     queue and run-state invariants above asserted (assertions 1, 2, 4 and
     assertion 3's fence part — the already-passing set the new test reuses
     rather than duplicates).
  2. The mid-round case is written against the real production interfaces so
     that it passes **exactly when the capability lands**: while the
     capability is absent the test carries `xfail(strict=True)` with the exact
     missing behaviour named (the `:620-634` convention); when the mid-round
     checkpoint writer + restore path land, the test XPASSes, and the marker
     is then removed in the same change that lands the behaviour. A soft
     xfail, a skip, or a test that "passes" by not executing the restore path
     does not satisfy this row.
  3. Both workers in the offline test are fresh invocations with no shared
     in-memory state; the only bridge between them is durable state under the
     test's own temp `runs`/queue root, in the formats production code writes
     (`queue.py` entries, `rounds.jsonl`, `round_NNN/checkpoint.json`,
     `report.json`, `events.jsonl`).
* **On-node pass criteria (CT111, the "test executed on CT111" clause):**
  1. The targeted test file runs on CT111 with the project's invocation
     conventions and a hard **timeout 300** for the targeted run, e.g.
     `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src timeout 300 python -m pytest
     tests/test_<e2b-mid-round-restore>.py -p no:cacheprovider -q` executed in
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

The mid-round restore gap is an approved waiver under ticket `easybutt0n-48`,
enforced by the strict xfail (`xfail(strict=True)` at
`tests/test_e2b_mid_round_restore.py:352`). The condition it covers is
documented at lines 176-177 of this document.

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
* **CT111 appears as written documentation in the early rounds** (this doc,
  plus the test's CT111-execution plan) and as a real target only once an
  authorised channel exists.
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
* The new test belongs beside `tests/test_checkpoint_restore_recovery.py`,
  reuses its offline helpers (`_run_manager`, `_ScriptedAgent`, `_Killed`,
  `_seed_ledger`, `_write_checkpoint_for`, the local-origin git fixtures) and
  extends rather than duplicates the merged coverage; the queue side is
  asserted with the real `QueueEntry`/`QueueStore` as
  `tests/webapi/test_restart_run_recovery.py` does against a throwaway store
  — never a live workspace.
