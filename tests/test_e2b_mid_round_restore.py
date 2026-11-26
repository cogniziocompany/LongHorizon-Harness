"""E2b: mid-round checkpoint-restore onto a second worker (task easybutt0n-17).

Design doc: ``docs/v15-e2b-checkpoint-restore.md`` — row E2b of the V1.5
readiness matrix.  That doc is this module's specification; what follows
implements its ``4. Pass criteria`` under the offline (CI-grade) branch, ready
to run on CT111 with the row's targeted-test invocation.

Two tests, extending (never duplicating) the merged E2b suite
``tests/test_checkpoint_restore_recovery.py`` (4b91080 / PR #189), whose
offline helpers (``_run_manager``, the scripted agent, the ``_Killed``
``SIGKILL`` stand-in, ``_make_workspace_repo`` and the ledger / checkpoint /
report readers) are imported and reused here:

* the **completed-round restore on a second worker** keeps passing, with the
  queue and run-state survival invariants asserted — the queue side with the
  REAL ``QueueEntry``/``QueueStore`` (atomic ``queue/<queue_id>.json`` files
  under a throwaway runs root, ``src/lh_harness/queue.py:4``), the run side
  with a fingerprint re-validation of the restored checkpoints exactly the way
  the supervisor validates the latest one before an in-place continue
  (``src/lh_harness/supervisor/service.py:3357``);
* the **mid-round** case — checkpoint the run mid-round, restore it on a
  second worker — asserted against the real production interfaces, per the
  doc's pass criterion 2.  Mid-round checkpoint WRITING and the restore path
  that resumes the interrupted round now exist
  (``src/lh_harness/manager.py`` ``_write_mid_round_checkpoint`` /
  ``_resumable_mid_round``), so the case runs for real and no longer carries
  the ``xfail(strict=True)`` marker that named the missing behaviour.

Both workers in every test are fresh ``lh_harness.manager.run`` invocations
with no shared in-memory state; the only bridge between them is durable state
under the test's own temp runs/queue root, in the formats production code
writes (``rounds.jsonl``, ``round_NNN/checkpoint.json``, ``report.json``,
``events.jsonl``, ``queue/<queue_id>.json``).

The queue side is asserted against a throwaway store — never a live workspace;
no live workspace, no network, no service process is touched.  The on-node
(CT111) stage of this row is a later round gated on an authorised provisioning
channel; see ``5. Execution target and authorisation boundary`` in the design
doc.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, List, Tuple

import pytest

from lh_harness.environment.local import LocalEnvironment
from lh_harness.manager import run
from lh_harness.queue import QueueEntry, QueueStore
from lh_harness.restart_recovery import (
    INTERRUPTED_BY_RESTART,
    WORKER_DISAPPEARED_REASON,
    begin_service_epoch,
    reconcile_interrupted_run,
)

from .test_checkpoint_restore_recovery import (
    _CLEAN_AUDIT,
    _DONE,
    _EXECUTED,
    _Killed,
    _PLAN,
    _ScriptedAgent,
    _checkpointed_rounds,
    _config,
    _ledger_rounds,
    _make_workspace_repo,
    _report,
    _role_dir,
    _run_manager,
)

# --------------------------------------------------------------------------
# Offline helpers local to this module
# --------------------------------------------------------------------------


@pytest.fixture()
def clean_git_env(monkeypatch) -> None:
    """Give git a clean environment, the way the merged suite's fixture does.

    An inherited ``GIT_CONFIG_COUNT`` whose ``GIT_CONFIG_KEY_n`` entries are
    missing makes every ``git`` invocation fail for reasons unrelated to the
    code under test; the fixture strips them for the duration of the test.
    """

    for name in list(os.environ):
        if name == "GIT_CONFIG_COUNT" or name.startswith("GIT_CONFIG_KEY_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def local_run(tmp_path: Path) -> Callable[..., Tuple[Path, QueueStore]]:
    """A throwaway pair of (temp runs root, real QueueStore over it).

    The store is the real production file store: every entry lives as an
    atomic JSON file at ``<runs_root>/queue/<queue_id>.json``.  The fixture's
    root is inside pytest's ``tmp_path``, so nothing outside the test's own
    directory can be touched.
    """

    runs_root = tmp_path / "runs"
    runs_root.mkdir(parents=True)
    return runs_root, QueueStore(runs_root)


def _enqueue_interrupted_run(store: QueueStore, workspace: Path, run_id: str) -> QueueEntry:
    """The queue entry of a run that was launched and then lost mid-round."""

    entry = store.create(
        {
            "name": "e2b mid-round restore",
            "task": "finish the refactor",
            "workspace": str(workspace),
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    launched = store.mark_launched(entry.queue_id, run_id)
    assert launched is not None
    assert launched.status == "launched", "the entry was launched, not just created"
    assert launched.run_id == run_id
    # The worker behind the run is lost mid-round: the entry stops being
    # launched and ends failed (the supervisor's own terminal record).
    lost = store.mark_failed(entry.queue_id, reason=WORKER_DISAPPEARED_REASON)
    assert lost is not None
    assert lost.status == "failed", "a lost worker leaves the entry failed"
    return lost


def _events(role: Path) -> List[dict[str, Any]]:
    path = role / "events.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _checkpoint_fingerprint_validates(checkpoint_path: Path, index: int) -> None:
    """Re-validate a round checkpoint exactly as service.py:3357 does.

    The fingerprint is the SHA-256 over the checkpoint's canonical round-record
    fields (everything except the envelope keys).  A mismatch means the file is
    torn or tampered — a failed restore, never a best-effort skip.
    """

    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    stored_fingerprint = payload.get("fingerprint") if isinstance(payload, dict) else None
    assert stored_fingerprint, f"round {index} checkpoint has no fingerprint"
    canonical_record = {
        key: value
        for key, value in payload.items()
        if key not in {"schema_version", "checkpoint_kind", "recorded_at", "fingerprint"}
    }
    canonical = json.dumps(
        canonical_record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    computed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert computed == stored_fingerprint, (
        f"round {index} checkpoint fingerprint does not validate after restore"
    )


# --------------------------------------------------------------------------
# Pass criterion 1: the completed-round restore on a second worker keeps
# passing, with the queue and run-state invariants of the design doc asserted.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_completed_round_restore_on_a_second_worker_keeps_queue_and_run_state_intact(
    tmp_path: Path,
    local_run,
    clean_git_env,
) -> None:
    """Completed-round restore on a second worker: the durable state survives.

    The scenario is the merged E2b suite's already-passing one (round 1
    completes and is checkpointed, the manager is lost while round 2 is in
    flight, a second worker restores from the checkpointed history), extended
    here with the parts that NO suite on main covers yet:

    * the interrupted run's queue entry is a REAL entry in a REAL
      ``QueueStore``, its chain after the restore is the design doc's outcome
      (b): the terminal original plus one ``-r1`` successor with the lineage
      on both ends, created through the production reconcile (its fence
      holds on a second call), and nothing else appears in the store;
    * the restored run state survives: the ledger gained one record per
      round, the completed rounds' checkpoints are still fingerprint-valid
      (the supervisor's fail-closed rule), and the events log carries the
      second worker's ``role_harness_resumed`` with the round it resumed
      from.
    """

    # ---- First worker: round 1 completes, round 2 is in flight, lost. -----
    report1, _agent1 = await _run_manager(
        tmp_path,
        resume=False,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _PLAN, _EXECUTED, _Killed("manager killed")],
    )

    assert report1["status"] == "failed"
    assert report1["abort_reason"] == "worker_exception", (
        "the close-out of the lost worker records the interruption"
    )
    assert [item["round_index"] for item in _ledger_rounds(tmp_path)] == [1]
    assert _checkpointed_rounds(tmp_path) == ["round_001", "round_002"], (
        "the completed round is checkpointed, and the in-flight round 2 left "
        "the mid-round checkpoint the restored worker resumes from"
    )

    # ---- The queue side: a real entry for the interrupted run. ------------
    runs_root, store = local_run
    # Offline there is no supervisor-assigned run id; a stand-in id carries
    # the lineage through the production reconcile the way a launched run
    # would.  The durable bridge between the workers stays the runs root.
    run_id = "offline-e2b-run"
    # The workspace the entry names is a throwaway LOCAL-ORIGIN git repo (no
    # network), so the production effect probes decide cleanly.
    workspace = _make_workspace_repo(tmp_path / "queue-ws")
    original = _enqueue_interrupted_run(store, workspace, run_id)

    # Two service epochs: the one that ran the first worker, and the one that
    # starts the second.  The epochs are durable under the runs root, and the
    # second's recorded previous_pid is the durable proof that the service
    # itself stopped between the two workers.
    first_service = begin_service_epoch(runs_root, pid=111, started_at=1000.0)
    second_service = begin_service_epoch(runs_root)
    assert second_service.pid != first_service.pid
    assert second_service.previous_pid == first_service.pid

    successor = reconcile_interrupted_run(
        store,
        original,
        runs_root=runs_root,
        epoch=second_service,
        status={"status": "failed", "failure_reason": WORKER_DISAPPEARED_REASON},
        owner={"supervisor_pid": first_service.pid, "started_at": first_service.started_at},
        probe_pr=None,  # effect detection must stay offline
    )

    assert successor is not None, "the interrupted entry gets its one successor"
    assert isinstance(successor, QueueEntry)
    assert successor.retry_of == original.queue_id
    assert successor.name == f"{original.name}-r1"
    assert successor.interrupted_run == run_id, "the successor names what it continues"
    assert successor.failure_cause == INTERRUPTED_BY_RESTART
    assert successor.status == "pending", "the successor waits to be launched"
    # The lineage is readable from BOTH ends (design doc, invariant bullet 3).
    assert store.get(original.queue_id).successor == successor.queue_id
    # Exactly one successor, and no entry for any round: the store holds the
    # original and the successor, nothing else.
    assert len(store.list()) == 2
    # The fence holds on the real store: reconciling a second time creates
    # nothing (never two successors for one interruption).
    second = reconcile_interrupted_run(
        store,
        original,
        runs_root=runs_root,
        epoch=second_service,
        status={"status": "failed", "failure_reason": WORKER_DISAPPEARED_REASON},
        owner={"supervisor_pid": first_service.pid, "started_at": first_service.started_at},
        probe_pr=None,
    )
    assert second is None
    assert len(store.list()) == 2, "no second successor, and no entry for a round"

    # The entries are durable atomic JSON files under the queue dir, readable
    # in the formats production code writes (queue.py:4), and their on-disk
    # lineage matches what the store reports.
    queue_dir = runs_root / "queue"
    assert queue_dir.is_dir()
    on_disk_original = json.loads(
        (queue_dir / f"{original.queue_id}.json").read_text(encoding="utf-8")
    )
    on_disk_successor = json.loads(
        (queue_dir / f"{successor.queue_id}.json").read_text(encoding="utf-8")
    )
    assert on_disk_original["status"] == "failed", "terminal states are not rewritten"
    assert on_disk_original["successor"] == successor.queue_id
    assert on_disk_successor["retry_of"] == original.queue_id
    assert on_disk_successor["interrupted_run"] == run_id

    # ---- Second worker: fresh invocation, no shared in-memory state. ------
    # The lost worker had planned round 2 and run its executor already, so the
    # resumed round 2 skips both and continues at its auditor; round 3's
    # manager then claims completion.
    report2, agent2 = await _run_manager(
        tmp_path,
        resume=True,
        replies=[_CLEAN_AUDIT, _DONE],
    )

    assert report2["status"] == "complete"
    assert report2["completion_satisfied"] is True
    assert [item["round_index"] for item in report2["rounds"]] == [1, 2, 3], (
        "the restore continues the numbering at the interrupted round"
    )
    # One record per round index, no duplicated round (the merged suite's
    # assertion 2, asserted here across the restore).
    ledger_indices = [item["round_index"] for item in _ledger_rounds(tmp_path)]
    assert ledger_indices == [1, 2, 3]
    assert len(ledger_indices) == len(set(ledger_indices))

    # The restored run state survives: every completed round has a checkpoint
    # whose fingerprint still validates (the supervisor's fail-closed rule,
    # service.py:3357).
    assert _checkpointed_rounds(tmp_path) == ["round_001", "round_002", "round_003"]
    for index in (1, 2, 3):
        checkpoint_path = _role_dir(tmp_path) / "rounds" / f"round_{index:03d}" / "checkpoint.json"
        assert checkpoint_path.is_file()
        _checkpoint_fingerprint_validates(checkpoint_path, index)

    # The events log survived and carries the second worker's resume record.
    events = _events(_role_dir(tmp_path))
    event_names = [item["event"] for item in events]
    assert "role_harness_start" in event_names
    assert "role_harness_resumed" in event_names
    assert "managed_round_recorded" in event_names
    assert "role_harness_done" in event_names, "the restored run reaches its close-out"
    resume_event = next(item for item in events if item["event"] == "role_harness_resumed")
    assert resume_event["restored_rounds"] == 1
    assert resume_event["resume_from_round"] == 1, (
        "the second worker records which checkpoint it resumed from"
    )
    # The restored task state actually reached the second worker's manager.
    assert any("still working" in prompt for prompt in agent2.prompts), (
        "the restored ledger must reach the manager prompt on the resume path"
    )
    # The durable report on disk is the second worker's close-out.
    on_disk = _report(tmp_path)
    assert on_disk["status"] == "complete"
    assert on_disk["completion_satisfied"] is True


# --------------------------------------------------------------------------
# Pass criterion 2: the mid-round case.  Written against the real production
# interfaces; it passes because the capability has landed, and carries no
# xfail marker naming a missing behaviour.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_mid_round_checkpoint_restores_the_in_flight_round_on_a_second_worker(
    tmp_path: Path,
) -> None:
    """The E2b row's demand: the interrupted round is RESUMED, not replayed.

    Written against the real production code paths of the design doc's
    ``2. Restore trigger`` section (the "Required (mid-round)" trigger):

    * after the mid-round loss, the in-flight round leaves a mid-round
      checkpoint (a checkpoint for a round whose ledger record does not
      exist — the in-flight record), fingerprint-validated the way the
      supervisor validates one;
    * the second worker — a fresh ``run(..., resume=True)`` with no shared
      in-memory state — resumes the interrupted round from that checkpoint
      instead of replaying it, so its effect is observed exactly once across
      both workers (the design doc's "no effect issued twice" invariant);
    * the restored run state survives: the interrupted round appears exactly
      once in the ledger, its checkpoint graduates into a fingerprint-valid
      record (never silently lost), and the close-out records the resume.

    All three hold now that the capability has landed: the writer and the
    resumable lookup live in ``src/lh_harness/manager.py`` and this test
    exercises them rather than pinning their absence.
    """

    role = _role_dir(tmp_path)
    effects: list[int] = []

    def _on_execute(call_number: int) -> None:
        effects.append(call_number)

    # ---- First worker: round 1 completes; round 2 in flight when lost. ----
    await _run_manager(
        tmp_path,
        resume=False,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _PLAN, _EXECUTED, _Killed("manager killed")],
        on_execute=_on_execute,
    )

    # The in-flight round must have left a MID-ROUND checkpoint: a checkpoint
    # for a round that has no ledger record (it was never completed) — the
    # exact behaviour missing on origin/main.
    mid_checkpoint = role / "rounds" / "round_002" / "checkpoint.json"
    assert mid_checkpoint.is_file(), (
        "the interrupted in-flight round must leave a mid-round checkpoint "
        "for the second worker to restore"
    )
    payload = json.loads(mid_checkpoint.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert payload.get("fingerprint"), "the mid-round checkpoint must be fingerprinted"
    ledger_indices = [item["round_index"] for item in _ledger_rounds(tmp_path)]
    assert 2 not in ledger_indices, (
        "the round the checkpoint is for must have no ledger record: it was "
        "in flight, not completed"
    )

    # ---- Second worker: fresh invocation, no shared in-memory state. ------
    # The restored round 2 does NOT re-run its manager or executor, so the
    # scripted agent never receives a reply for either phase: the list is
    # auditor, manager.  Leaving the executor's ``_EXECUTED`` in the list would
    # feed it to the auditor, and ``_ScriptedAgent`` counts every ``_EXECUTED``
    # reply as an effect regardless of role, which would inflate the count with
    # an effect that never ran.
    resumed, agent2 = await _run_manager(
        tmp_path,
        resume=True,
        replies=[_CLEAN_AUDIT, _DONE],
        on_execute=_on_execute,
    )
    # The resumed round's only manager episode belongs to round 3: the restored
    # round 2 was planned by the lost worker and is not planned again.
    assert sum("LongHorizon-Harness manager agent" in prompt for prompt in agent2.prompts) == 1, (
        "the restored round must not run a second manager episode"
    )

    # The interrupted round was resumed, not replayed: across BOTH workers
    # its effect (the scripted executor) was observed exactly once.  Two
    # effects total — round 1's executor once, the interrupted round's
    # executor once.  A replayed round makes it three.
    assert len(effects) == 2, (
        "the interrupted round's effect must not run again on the second "
        f"worker, but it ran {len(effects)} times in total"
    )

    # The restored run state survives: one record per round, no duplication.
    ledger_indices = [item["round_index"] for item in _ledger_rounds(tmp_path)]
    assert ledger_indices == [1, 2, 3]
    assert len(ledger_indices) == len(set(ledger_indices))

    # The interrupted round's checkpoint graduated into the round's completed
    # record and still validates — never silently deleted.
    graduated = role / "rounds" / "round_002" / "checkpoint.json"
    assert graduated.is_file()
    _checkpoint_fingerprint_validates(graduated, 2)

    # The close-out of the second worker records the resume.
    assert resumed["status"] == "complete"
    assert resumed["completion_satisfied"] is True
    events = _events(role)
    resume_event = next(item for item in events if item["event"] == "role_harness_resumed")
    assert resume_event["restored_rounds"] == 1
    assert resume_event["resume_from_round"] == 1


# --------------------------------------------------------------------------
# Pass criterion 2b: the restored round skips EVERY phase the checkpoint
# proves finished, starting with the manager's own plan.
#
# The phases are named by the reply the lost worker was producing when it
# died, because the scripted agent raises the ``_Killed`` stand-in in place of
# that reply -- so the phase that was in progress never finishes, and the
# checkpoint names the phase before it:
#
#   lost while producing the EXECUTOR reply  -> checkpoint ``manager_done``
#   lost while producing the AUDITOR reply   -> checkpoint ``executor_done``
# --------------------------------------------------------------------------

_MANAGER_PROMPT = "You are the LongHorizon-Harness manager agent"
_CLI_EXECUTOR_PROMPT = "You are the LongHorizon-Harness CLI executor"
_GUI_EXECUTOR_PROMPT = "You are the LongHorizon-Harness GUI executor"
_CLI_AUDITOR_PROMPT = "You are the read-only LongHorizon-Harness CLI auditor"
_GUI_AUDITOR_PROMPT = "You are the read-only LongHorizon-Harness GUI auditor"

# A plan that routes the next subtask to the GUI executor: the restore must
# carry this route through, not assume every restored plan is ``cli``.
_GUI_PLAN = "Next: gui\n\nCurrent Task State:\nstill working\n\nPlan:\nopen the dialog"


def _mid_round_checkpoint(tmp_path: Path, round_index: int = 2) -> dict[str, Any]:
    payload = json.loads(
        (_role_dir(tmp_path) / "rounds" / f"round_{round_index:03d}" / "checkpoint.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload.get("checkpoint_kind") == "managed_round_mid_round", (
        "the in-flight round's checkpoint is the mid-round kind"
    )
    return payload


def _ledger_round(tmp_path: Path, round_index: int) -> dict[str, Any]:
    matches = [item for item in _ledger_rounds(tmp_path) if item["round_index"] == round_index]
    assert len(matches) == 1, f"round {round_index} must have exactly one ledger record"
    return matches[0]


def _count(prompts: list[str], header: str) -> int:
    return sum(prompt.startswith(header) for prompt in prompts)


@pytest.mark.asyncio
async def test_a_resume_at_manager_done_reuses_the_checkpointed_plan_without_replanning(
    tmp_path: Path,
) -> None:
    """Resuming at ``manager_done``: the manager is NOT called again.

    The lost worker planned round 2 (``Next: cli``) and died while its executor
    was producing the reply, so the checkpoint names ``manager_done``.  The
    resumed worker must continue with the checkpointed plan and route -- the
    executor and auditor act on THAT plan -- and the round record must say
    ``cli`` with the checkpointed plan text, never a re-planned route.
    """

    # Round 1 completes; round 2's manager plans, then the worker is lost
    # while the executor reply is being produced.
    await _run_manager(
        tmp_path,
        resume=False,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _PLAN, _Killed("manager killed")],
    )
    checkpoint = _mid_round_checkpoint(tmp_path)
    assert checkpoint["phase"] == "manager_done", "the plan finished; the executor did not"
    assert checkpoint["next_step"] == "cli"
    assert checkpoint["plan_text"] == _PLAN

    # The resumed round 2 runs executor -> auditor on the checkpointed plan;
    # round 3's manager then claims completion.
    report, agent = await _run_manager(
        tmp_path,
        resume=True,
        replies=[_EXECUTED, _CLEAN_AUDIT, _DONE],
    )

    assert report["status"] == "complete"
    assert report["completion_satisfied"] is True
    # No second manager episode for the restored round: the only manager
    # prompt the resumed worker sent belongs to round 3.
    assert _count(agent.prompts, _MANAGER_PROMPT) == 1, (
        "the restored round must not run a manager episode; only the round "
        "after it is planned"
    )
    assert _count(agent.prompts, _CLI_EXECUTOR_PROMPT) == 1, (
        "the restored round's executor runs exactly once, on the checkpointed cli route"
    )
    assert _count(agent.prompts, _GUI_EXECUTOR_PROMPT) == 0
    # The executor and auditor saw the checkpointed plan, not a new one.
    executor_prompt = next(p for p in agent.prompts if p.startswith(_CLI_EXECUTOR_PROMPT))
    assert _PLAN in executor_prompt, "the executor acts on the checkpointed plan text"

    # The round record reports truthfully: the route and plan are the ones
    # the lost worker checkpointed.
    record = _ledger_round(tmp_path, 2)
    assert record["next_step"] == "cli"
    assert record["plan_text"] == _PLAN
    assert record["executor_output"] == _EXECUTED
    assert record["auditor_report"]
    # The resumed worker's own event names what it really skipped.
    resumed_events = [
        item for item in _events(_role_dir(tmp_path)) if item["event"] == "managed_round_resumed"
    ]
    assert [item["skipped_phases"] for item in resumed_events] == [["manager_done", "round_start"]]


@pytest.mark.asyncio
async def test_a_resume_at_executor_done_skips_the_manager_and_executor_and_audits_once(
    tmp_path: Path,
) -> None:
    """Resuming at ``executor_done``: manager and executor are NOT called.

    The lost worker planned round 2 and its executor already issued the
    round's effect; the worker is lost while the auditor reply is produced, so
    the checkpoint names ``executor_done``.  The resumed worker must continue
    at the auditor -- the effect is never issued a second time -- and the
    auditor runs exactly once for the restored round.
    """

    effects: list[int] = []

    def _on_execute(call_number: int) -> None:
        effects.append(call_number)

    # Round 1 completes; round 2's manager plans, its executor runs, and the
    # worker is lost while the auditor reply is being produced.
    await _run_manager(
        tmp_path,
        resume=False,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _PLAN, _EXECUTED, _Killed("manager killed")],
        on_execute=_on_execute,
    )
    checkpoint = _mid_round_checkpoint(tmp_path)
    assert checkpoint["phase"] == "executor_done", "the effect was issued before the loss"
    assert checkpoint["executor_output"] == _EXECUTED
    assert len(effects) == 2, "round 1's effect and the interrupted round 2's effect"

    # The resumed round 2 runs only its auditor; round 3's manager then
    # claims completion.
    report, agent = await _run_manager(
        tmp_path,
        resume=True,
        replies=[_CLEAN_AUDIT, _DONE],
        on_execute=_on_execute,
    )

    assert report["status"] == "complete"
    assert report["completion_satisfied"] is True
    assert _count(agent.prompts, _MANAGER_PROMPT) == 1, (
        "the restored round must not run a manager episode; only the round after it is planned"
    )
    assert _count(agent.prompts, _CLI_EXECUTOR_PROMPT) == 0, (
        "the restored round's executor must not run: its effect was already issued"
    )
    assert _count(agent.prompts, _GUI_EXECUTOR_PROMPT) == 0
    assert _count(agent.prompts, _CLI_AUDITOR_PROMPT) == 1, (
        "the restored round's auditor runs exactly once"
    )
    assert len(effects) == 2, (
        "the interrupted round's effect must not run again on the second worker"
    )
    # The auditor audited the checkpointed plan and the checkpointed executor
    # output, not a re-planned round.
    auditor_prompt = agent.prompts[0]
    assert auditor_prompt.startswith(_CLI_AUDITOR_PROMPT)
    assert _EXECUTED in auditor_prompt

    record = _ledger_round(tmp_path, 2)
    assert record["next_step"] == "cli"
    assert record["plan_text"] == _PLAN
    assert record["executor_output"] == _EXECUTED
    assert record["auditor_report"]
    resumed_events = [
        item for item in _events(_role_dir(tmp_path)) if item["event"] == "managed_round_resumed"
    ]
    assert [item["skipped_phases"] for item in resumed_events] == [
        ["executor_done", "manager_done", "round_start"]
    ]


@pytest.mark.asyncio
async def test_a_checkpointed_gui_plan_is_restored_as_a_gui_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkpointed ``Next: gui`` plan is restored as a GUI round.

    Every scripted plan above routes to ``cli``; this is the blind spot.  The
    route is the lost worker's decision, recorded in the checkpoint, and the
    resumed worker must carry it through: the restored round binds the GUI
    executor and GUI auditor and records ``next_step: gui``.
    """

    # The GUI route must not be suppressed by the environment's opt-out.
    monkeypatch.delenv("LH_HARNESS_DISABLE_GUI_ROUTE", raising=False)

    await _run_manager(
        tmp_path,
        resume=False,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _GUI_PLAN, _Killed("manager killed")],
    )
    checkpoint = _mid_round_checkpoint(tmp_path)
    assert checkpoint["phase"] == "manager_done"
    assert checkpoint["next_step"] == "gui", "the lost worker's plan routes to the GUI executor"

    report, agent = await _run_manager(
        tmp_path,
        resume=True,
        replies=[_EXECUTED, _CLEAN_AUDIT, _DONE],
    )

    assert report["status"] == "complete"
    assert _count(agent.prompts, _MANAGER_PROMPT) == 1, "the restored round is not re-planned"
    assert _count(agent.prompts, _GUI_EXECUTOR_PROMPT) == 1, (
        "the restored round runs the GUI executor the checkpointed plan asked for"
    )
    assert _count(agent.prompts, _GUI_AUDITOR_PROMPT) == 1
    assert _count(agent.prompts, _CLI_EXECUTOR_PROMPT) == 0
    assert _count(agent.prompts, _CLI_AUDITOR_PROMPT) == 0
    gui_executor_prompt = next(p for p in agent.prompts if p.startswith(_GUI_EXECUTOR_PROMPT))
    assert _GUI_PLAN in gui_executor_prompt, "the GUI executor acts on the checkpointed plan"

    record = _ledger_round(tmp_path, 2)
    assert record["next_step"] == "gui"
    assert record["plan_text"] == _GUI_PLAN


@pytest.mark.asyncio
async def test_the_auditor_result_is_saved_exactly_once_per_restored_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each round's auditor result is saved exactly once, resumed or not.

    The auditor's saved result (raw trajectory plus the final screenshot its
    episode produced) is one artifact per round.  Saving it twice rewrites the
    round's trajectory a second time and -- for a GUI round -- takes a second
    screenshot after the auditor has finished, so the saved ``final`` screenshot
    is not the one the auditor ended on.  The save is counted at the production
    writer, for every round across both workers.
    """

    import lh_harness.manager as manager_module

    real_save = manager_module._save_role_result
    auditor_saves: dict[str, int] = {}
    screenshots = 0
    real_capture = manager_module._capture_environment_screenshot

    def _counting_save(round_dir, role_name, result, **kwargs):
        if role_name == "auditor":
            auditor_saves[round_dir.name] = auditor_saves.get(round_dir.name, 0) + 1
        return real_save(round_dir, role_name, result, **kwargs)

    async def _counting_capture(env):
        nonlocal screenshots
        screenshots += 1
        return await real_capture(env)

    monkeypatch.setattr(manager_module, "_save_role_result", _counting_save)
    monkeypatch.setattr(manager_module, "_capture_environment_screenshot", _counting_capture)

    # Both kinds of round are covered: round 1 is a fresh round (its auditor
    # runs on the first worker), round 2 is lost while its auditor runs and is
    # restored on the second worker (its auditor runs there), round 3 is a
    # fresh round on the second worker.
    await _run_manager(
        tmp_path,
        resume=False,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _PLAN, _EXECUTED, _Killed("manager killed")],
    )
    assert _mid_round_checkpoint(tmp_path)["phase"] == "executor_done"
    report, _agent = await _run_manager(
        tmp_path,
        resume=True,
        replies=[_CLEAN_AUDIT, _DONE],
    )
    assert report["status"] == "complete"
    assert [item["round_index"] for item in _ledger_rounds(tmp_path)] == [1, 2, 3]

    assert auditor_saves == {"round_001": 1, "round_002": 1}, (
        f"each round's auditor result must be saved exactly once, got {auditor_saves}"
    )
    # CLI rounds take no environment screenshot at all (GUI rounds take one
    # per GUI episode that ran); a leftover second capture is counted here.
    assert screenshots == 0


# --------------------------------------------------------------------------
# Pass criterion 2c: an operator instruction queued while the worker was lost
# is delivered exactly once, by the next round that actually runs a manager.
#
# The round-start claim hands queued instructions to the carryover, and the
# carryover is consumed by "this round's manager prompt".  On a restored round
# there is no manager prompt: the plan is the checkpointed one.  Consuming the
# carryover there would write ``human_instructions.txt`` and log an injection
# that no agent ever saw, and the instruction would be lost for good.  It must
# instead stay in the carryover until the next live manager round.
# --------------------------------------------------------------------------

_INSTRUCTION = "check the config file before anything else"


def _injections(role: Path) -> list[dict[str, Any]]:
    return [item for item in _events(role) if item["event"] == "human_instructions_injected"]


async def _run_with_instruction(tmp_path: Path, *, replies: list[Any]):
    """``_run_manager`` with a queued operator instruction handed to ``run``.

    The instruction is the one the operator typed while the first worker was
    dying.  The producer the CLI wires in (``gate_state.drain_injections``) is
    a drain, and the loop polls it at every round start, so the queue hands
    each instruction out exactly once.
    """
    agent = _ScriptedAgent(replies)
    queue = [[_INSTRUCTION]]

    def _drain() -> list[str]:
        return queue.pop(0) if queue else []

    report = await run(
        task="finish the refactor",
        env=LocalEnvironment(str(tmp_path / "tmp")),
        config=_config(tmp_path),
        agent=agent,
        resume=True,
        pending_instructions=_drain,
    )
    return report, agent


@pytest.mark.parametrize(
    ("phase", "lost_replies", "resumed_replies"),
    [
        # Lost while producing the EXECUTOR reply: the plan finished, so the
        # restored round skips its manager and runs executor -> auditor.
        (
            "manager_done",
            [_PLAN, _EXECUTED, _CLEAN_AUDIT, _PLAN, _Killed("manager killed")],
            [_EXECUTED, _CLEAN_AUDIT, _DONE],
        ),
        # Lost while producing the AUDITOR reply: the effect was issued, so the
        # restored round skips manager AND executor and runs only its auditor.
        (
            "executor_done",
            [_PLAN, _EXECUTED, _CLEAN_AUDIT, _PLAN, _EXECUTED, _Killed("manager killed")],
            [_CLEAN_AUDIT, _DONE],
        ),
    ],
    ids=["manager_done", "executor_done"],
)
@pytest.mark.asyncio
async def test_an_instruction_queued_across_a_mid_round_loss_is_delivered_exactly_once(
    tmp_path: Path,
    phase: str,
    lost_replies: list[Any],
    resumed_replies: list[Any],
) -> None:
    """The queued instruction reaches the next live manager prompt, once."""

    # ---- First worker: round 1 completes; round 2 is lost in flight. -------
    await _run_manager(tmp_path, resume=False, replies=lost_replies)
    assert _mid_round_checkpoint(tmp_path)["phase"] == phase
    assert _injections(_role_dir(tmp_path)) == [], "nothing was queued on the first worker"

    # ---- Second worker: fresh invocation; the instruction is in its queue. -
    report, agent = await _run_with_instruction(tmp_path, replies=resumed_replies)

    assert report["status"] == "complete"
    assert [item["round_index"] for item in _ledger_rounds(tmp_path)] == [1, 2, 3]
    resumed_events = [
        item for item in _events(_role_dir(tmp_path)) if item["event"] == "managed_round_resumed"
    ]
    assert "manager_done" in resumed_events[0]["skipped_phases"], (
        "the restored round really skipped its manager episode"
    )

    # The restored round 2 ran no manager, so it must not have consumed the
    # instruction: no injection is recorded for it and no file is written.
    rounds_dir = _role_dir(tmp_path) / "rounds"
    assert not (rounds_dir / "round_002" / "human_instructions.txt").exists(), (
        "the restored round has no manager prompt to inject into"
    )

    # The next live manager round (round 3) delivers it, exactly once.  Only
    # the role prompts that can act on it are counted: the final-response role
    # legitimately sees the operator's instruction again as history, so that
    # its reply can address it.
    manager_prompts = [p for p in agent.prompts if p.startswith(_MANAGER_PROMPT)]
    assert len(manager_prompts) == 1, "only round 3 is planned by a live manager"
    assert _INSTRUCTION in manager_prompts[0], "the instruction reaches the manager prompt"
    acting_prompts = [
        p
        for p in agent.prompts
        if p.startswith((_MANAGER_PROMPT, _CLI_EXECUTOR_PROMPT, _GUI_EXECUTOR_PROMPT))
    ]
    assert sum(_INSTRUCTION in prompt for prompt in acting_prompts) == 1, (
        "the instruction is injected into exactly one manager or executor prompt"
    )
    assert (rounds_dir / "round_003" / "human_instructions.txt").read_text(
        encoding="utf-8"
    ) == _INSTRUCTION
    injections = _injections(_role_dir(tmp_path))
    assert [(item["round"], item["chars"]) for item in injections] == [(3, len(_INSTRUCTION))], (
        "exactly one human_instructions_injected event, for the round that delivered it"
    )
