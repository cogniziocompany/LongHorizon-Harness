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
  second worker — carried as ``xfail(strict=True)`` naming the exact missing
  behaviour, per the doc's pass criterion 2 and the merged suite's convention
  at ``tests/test_checkpoint_restore_recovery.py:620-634``.  Mid-round
  checkpoint WRITING does not exist on origin/main
  (``src/lh_harness/manager.py:2472`` ``_record_round`` writes only when the
  round completes), so this case xfails today and may only XPASS when the
  capability lands in the same change that removes the marker.

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
    _checkpointed_rounds,
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
    assert _checkpointed_rounds(tmp_path) == ["round_001"], (
        "the completed round is checkpointed; the in-flight round 2 is not "
        "(that mid-round checkpoint is this row's missing capability)"
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
    report2, agent2 = await _run_manager(
        tmp_path,
        resume=True,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _DONE],
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
# interfaces so it passes EXACTLY when the capability lands; while the
# capability is absent it carries xfail(strict=True) with the exact missing
# behaviour named.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "MISSING BEHAVIOUR on origin/main: no checkpoint is written while a "
        "round is still IN FLIGHT (lh_harness/manager.py::_record_round is "
        "called only at round completion), so a manager lost mid-round leaves "
        "no mid-round checkpoint for the interrupted round, and "
        "lh_harness/manager.py::_recorded_rounds restores only COMPLETED "
        "rounds.  The second worker therefore replays the interrupted round "
        "from scratch and its executor — the round's effect — runs a second "
        "time.  A mid-round checkpoint writer plus a restore path that "
        "RESUMES the interrupted round instead of replaying it does not "
        "exist on main; until it lands this test cannot pass."
    ),
)
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

    While the row's mid-round capability is absent, at least one of these
    fails, and ``strict=True`` turns that into the row's honest status.  When
    the capability lands, this test XPASSes and the marker must be removed in
    the same change.
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
    resumed, _agent2 = await _run_manager(
        tmp_path,
        resume=True,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _DONE],
        on_execute=_on_execute,
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
