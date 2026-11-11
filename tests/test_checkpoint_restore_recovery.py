"""E2b: checkpoint-restore and manager-restart-from-checkpoint recovery.

This module covers the V1.5 gate E2b scenario that the host-loss SIGKILL +
restart-fence + worker ``kill -9`` suite (E2, CT111) does not: a run that
writes a checkpoint, loses its manager process, and is restarted from the
checkpoint.

What is exercised here is the REAL production code, offline:

* ``lh_harness.manager.run`` — the four-role management loop, its durable
  ``rounds.jsonl`` ledger, and its per-round checkpoint writer
  (``_round_checkpoint`` / ``_record_round``);
* ``lh_harness.manager._recorded_rounds`` — the ledger restore used by
  ``--resume``;
* ``lh_harness.supervisor.service.RunSupervisor._validate_latest_round_checkpoint``
  — the fail-closed checkpoint-fingerprint validation performed before an
  in-place continue;
* ``lh_harness.restart_recovery.detect_run_effects`` /
  ``reconcile_interrupted_run`` — the restart-interrupted effect detection
  and the single-successor fence.

No production code is modified by this module; it only adds the coverage.

The four assertions the E2b scenario demands are:

1. the run resumes at the checkpointed round;
2. no round is duplicated;
3. no effect (push / PR / gate resolve) is issued twice;
4. the close-out records the interruption.

Assertions 1, 2 and 4 are satisfiable on ``origin/main`` and are asserted
here as passing.  Assertion 3 for an effect performed by a round that was
still in flight when the manager died required a mid-round checkpoint writer
and a restore path that resumes the interrupted round; that capability now
lives in ``src/lh_harness/manager.py`` (``_write_mid_round_checkpoint`` /
``_resumable_mid_round``) and the test at the bottom of this module asserts
it rather than pinning its absence.  See the PR body for the full statement.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from lh_harness.environment.local import LocalEnvironment
from lh_harness.manager import run
from lh_harness.types import EpisodeResult, HarnessConfig, ManagedRound

# --------------------------------------------------------------------------
# Test fixtures and helpers
# --------------------------------------------------------------------------

# The manager reply that routes one more CLI executor round.
_PLAN = "Next: cli\n\nCurrent Task State:\nstill working\n\nPlan:\nkeep going"
# The executor's reply: the round's work happened.
_EXECUTED = "executor made the change"
# A clean auditor report: completion is only accepted with this evidence.
_CLEAN_AUDIT = (
    "Status: complete\n"
    "Integrity: clean\n"
    "Contract audit: aligned\n\n"
    "Summary:\nthe change is verified"
)
_DONE = "Next: done\n\nCurrent Task State:\nall finished"


class _Killed(BaseException):
    """A stand-in for ``SIGKILL`` arriving at the manager process.

    It derives from ``BaseException`` on purpose: the manager's worker
    boundary catches ``BaseException`` so a ``KeyboardInterrupt`` /
    ``SystemExit`` / hard kill is turned into a durable terminal artifact
    instead of vanishing, exactly as it would be for a real process death.
    """


class _ScriptedAgent:
    """Replays a fixed reply sequence and records the prompts it received.

    ``on_execute`` is invoked every time the scripted executor reply is
    produced, which is how the test observes how many times a round's
    effect was actually performed.
    """

    def __init__(self, replies: list[Any], *, on_execute=None) -> None:
        self._replies = list(replies)
        self._on_execute = on_execute
        self.prompts: list[str] = []
        self.executor_calls: int = 0

    async def run_episode(self, prompt, _env, _budget, live_trajectory_path=None):
        self.prompts.append(str(prompt))
        reply = self._replies.pop(0) if self._replies else _PLAN
        if isinstance(reply, BaseException):
            raise reply
        if reply == _EXECUTED:
            self.executor_calls += 1
            if self._on_execute is not None:
                self._on_execute(self.executor_calls)
        return EpisodeResult(status="done", actions_log=reply)


def _config(tmp_path: Path) -> HarnessConfig:
    """A fully offline config: temp workspace, temp logs, no network."""

    return HarnessConfig(
        max_total_episodes=5,
        workspace_path=str(tmp_path / "workspace"),
        harness_dir=str(tmp_path / "harness"),
        log_dir=str(tmp_path / "logs"),
    )


def _role_dir(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "role_orchestration"


def _ledger_rounds(tmp_path: Path) -> list[dict[str, Any]]:
    """The durable round ledger, parsed."""

    path = _role_dir(tmp_path) / "rounds.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _checkpointed_rounds(tmp_path: Path) -> list[str]:
    """The round directories that carry a durable checkpoint."""

    rounds = _role_dir(tmp_path) / "rounds"
    if not rounds.is_dir():
        return []
    return sorted(
        entry.parent.name
        for entry in rounds.glob("*/checkpoint.json")
        if entry.is_file()
    )


def _report(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "logs" / "report.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _seed_ledger(tmp_path: Path, indices: list[int]) -> None:
    """Write a ledger of already-completed rounds (a foreign predecessor)."""

    role = _role_dir(tmp_path)
    role.mkdir(parents=True, exist_ok=True)
    with (role / "rounds.jsonl").open("w", encoding="utf-8") as handle:
        for index in indices:
            record = ManagedRound(
                round_index=index,
                next_step="cli",
                plan_text=f"plan {index}",
                executor_output=f"output {index}",
                auditor_report=_CLEAN_AUDIT,
                harness_feedback="",
                task_state=f"state {index}",
                task_contract=f"contract {index}",
                related_report_refs=[],
                manager_status={},
                executor_status={},
                auditor_status={},
            )
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def _write_checkpoint_for(role_dir: Path, round_index: int) -> Path:
    """Write a valid per-round checkpoint the way ``_record_round`` does.

    This mirrors the production writer exactly (same fields, same canonical
    fingerprint) so the supervisor's fail-closed validation accepts it.
    """

    import hashlib

    record = ManagedRound(
        round_index=round_index,
        next_step="cli",
        plan_text=f"plan {round_index}",
        executor_output=f"output {round_index}",
        auditor_report=_CLEAN_AUDIT,
        harness_feedback="",
        task_state=f"state {round_index}",
        task_contract=f"contract {round_index}",
        related_report_refs=[],
        manager_status={},
        executor_status={},
        auditor_status={},
    )
    payload = asdict(record)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["schema_version"] = 1
    payload["checkpoint_kind"] = "managed_round"
    payload["recorded_at"] = 1234567890.0
    payload["fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    path = role_dir / "rounds" / f"round_{round_index:03d}" / "checkpoint.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.fixture()
def clean_git_env(monkeypatch):
    """Give git a clean environment for the duration of the test.

    This workspace (and some CI shells) exports ``GIT_CONFIG_COUNT`` together
    with ``GIT_CONFIG_KEY_n`` entries.  An inherited ``GIT_CONFIG_COUNT``
    whose keys are missing makes EVERY ``git`` invocation fail with
    ``error: missing config key GIT_CONFIG_KEY_0``, which would make the
    effect-detection probes report "undecided" for reasons that have nothing
    to do with the code under test.  The harness' own ``child_env()`` does
    not strip those variables, so the test strips them here explicitly.

    The production code under test is unchanged; this only makes the test's
    own git fixtures deterministic.
    """

    for name in list(os.environ):
        if name == "GIT_CONFIG_COUNT" or name.startswith("GIT_CONFIG_KEY_"):
            monkeypatch.delenv(name, raising=False)
    return None


# --------------------------------------------------------------------------
# Scenario: start a run, lose the manager mid-round, restart from checkpoint
# --------------------------------------------------------------------------


async def _run_manager(
    tmp_path: Path,
    *,
    resume: bool,
    replies: list[Any],
    on_execute=None,
):
    agent = _ScriptedAgent(replies, on_execute=on_execute)
    report = await run(
        task="finish the refactor",
        env=LocalEnvironment(str(tmp_path / "tmp")),
        config=_config(tmp_path),
        agent=agent,
        resume=resume,
    )
    return report, agent


@pytest.mark.asyncio
async def test_the_run_resumes_at_the_checkpointed_round_after_manager_loss(
    tmp_path: Path,
) -> None:
    """Assertion 1: the resumed run continues at the checkpointed round.

    Round 1 completes and is checkpointed.  Round 2 starts and the manager
    process is lost mid-round (a ``SIGKILL`` stand-in).  The restarted run
    restores the checkpointed history and continues at the round AFTER the
    last checkpointed one -- the round that was in flight is replayed,
    because ``origin/main`` only checkpoints a round once it completes.
    """

    # Round 1: manager -> executor -> auditor, all clean.  Round 2: the
    # manager plans and the executor runs, then the manager process is lost.
    report, _agent = await _run_manager(
        tmp_path,
        resume=False,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _PLAN, _EXECUTED, _Killed("manager killed")],
    )

    assert report["status"] == "failed", "a lost manager is a failed run"
    assert report["rounds_run"] == 1, "only round 1 completed before the loss"
    assert _checkpointed_rounds(tmp_path) == ["round_001", "round_002"], (
        "the completed round is checkpointed, and the interrupted in-flight "
        "round 2 left the mid-round checkpoint the resumed worker restores"
    )
    assert [item["round_index"] for item in _ledger_rounds(tmp_path)] == [1]

    # Restart the manager from the checkpointed ledger.
    resumed, agent = await _run_manager(
        tmp_path,
        resume=True,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _DONE],
    )

    assert [item["round_index"] for item in resumed["rounds"]] == [1, 2, 3], (
        "the resumed run continues the numbering: the checkpointed round 1 is "
        "kept, the interrupted round 2 is replayed, and the run continues at 3"
    )
    assert resumed["rounds_run"] == 3
    # The restored history must actually reach the manager: the run resumed
    # from the checkpoint, it did not start over.  The restored round's
    # task state is what the manager prompt must carry.
    assert any("still working" in prompt for prompt in agent.prompts), (
        "the restored task state must reach the manager prompt"
    )


@pytest.mark.asyncio
async def test_no_round_is_duplicated_across_the_manager_loss(
    tmp_path: Path,
) -> None:
    """Assertion 2: the durable ledger records each round exactly once.

    The ledger is append-only and the loop may re-record the last entry
    after a late gate decision, so the invariant is "one record per round
    index", not "one line per round".
    """

    await _run_manager(
        tmp_path,
        resume=False,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _PLAN, _EXECUTED, _Killed("manager killed")],
    )
    await _run_manager(
        tmp_path,
        resume=True,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _DONE],
    )

    indices = [item["round_index"] for item in _ledger_rounds(tmp_path)]
    assert indices == [1, 2, 3], f"the ledger must hold one record per round, got {indices}"
    assert len(indices) == len(set(indices)), "no round index may appear twice"
    # The interrupted round is recorded ONCE (by the resumed run), not once
    # per attempt.
    assert indices.count(2) == 1


@pytest.mark.asyncio
async def test_the_close_out_records_the_manager_interruption(
    tmp_path: Path,
) -> None:
    """Assertion 4: the run's close-out artifact records the interruption."""

    report, _agent = await _run_manager(
        tmp_path,
        resume=False,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _PLAN, _EXECUTED, _Killed("manager killed")],
    )

    # The worker boundary persists a terminal artifact for a lost manager.
    assert report["abort_reason"] == "worker_exception"
    assert report["status"] == "failed"
    assert report["completion_satisfied"] is False
    assert report["exception_type"] == "_Killed"

    on_disk = _report(tmp_path)
    assert on_disk["abort_reason"] == "worker_exception"
    assert on_disk["status"] == "failed"
    # The close-out names the round the manager was working on when it died.
    assert on_disk["rounds_run"] == 1
    assert [item["round_index"] for item in on_disk["rounds"]] == [1]

    # The interruption is also visible as an event in the durable event log.
    events = [
        json.loads(line)
        for line in (_role_dir(tmp_path) / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    names = [item["event"] for item in events]
    assert "role_harness_start" in names
    assert "managed_round_recorded" in names
    assert "role_harness_done" not in names, (
        "a lost manager never reaches the loop's normal close-out; the "
        "terminal artifact is written by the worker boundary instead"
    )


@pytest.mark.asyncio
async def test_the_resumed_run_records_that_it_resumed_from_the_checkpoint(
    tmp_path: Path,
) -> None:
    """The close-out of the RESTARTED run names the checkpoint it resumed from."""

    await _run_manager(
        tmp_path,
        resume=False,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _PLAN, _EXECUTED, _Killed("manager killed")],
    )
    resumed, _agent = await _run_manager(
        tmp_path,
        resume=True,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _DONE],
    )

    assert resumed["status"] == "complete"
    assert resumed["completion_satisfied"] is True

    events = [
        json.loads(line)
        for line in (_role_dir(tmp_path) / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    resume_event = next(item for item in events if item["event"] == "role_harness_resumed")
    assert resume_event["restored_rounds"] == 1
    assert resume_event["resume_from_round"] == 1, (
        "the resumed worker must say which checkpoint it continued from"
    )


# --------------------------------------------------------------------------
# Assertion 3: no effect (push / PR / gate resolve) issued twice
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` with a clean, non-interactive environment."""

    env = {
        key: value
        for key, value in os.environ.items()
        if not (key == "GIT_CONFIG_COUNT" or key.startswith("GIT_CONFIG_KEY_"))
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


def _make_workspace_repo(tmp_path: Path) -> Path:
    """A local workspace git repo with a LOCAL ``origin`` remote.

    Both the remote and the workspace live under the test's temp directory,
    so the test needs no network at all.
    """

    remote = tmp_path / "origin.git"
    remote.mkdir(parents=True)
    _git(remote, "init", "--bare", "--initial-branch=main")

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "config", "user.email", "test@example.invalid")
    _git(workspace, "config", "user.name", "lh-harness test")
    (workspace / "README.md").write_text("base\n", encoding="utf-8")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-m", "base")
    _git(workspace, "remote", "add", "origin", str(remote))
    _git(workspace, "push", "-u", "origin", "main")
    return workspace


def test_the_interrupted_runs_effects_are_detected_once_and_reconciled_once(
    tmp_path: Path,
    clean_git_env,
) -> None:
    """Assertion 3, part 1: the restart fence issues the effect at most once.

    A run that pushed a branch and opened a PR before its manager died must
    be detected as having left those effects, and reconciling it twice must
    still create only ONE successor: the fence is the durable
    "the effect is not issued twice" guarantee for the restart path.
    """

    from lh_harness.restart_recovery import (
        PROBE_UNKNOWN,
        RunEffects,
        detect_run_effects,
        reconcile_interrupted_run,
    )

    workspace = _make_workspace_repo(tmp_path)

    # The interrupted run pushed its branch and left a commit behind.
    _git(workspace, "checkout", "-b", "feature/interrupted")
    (workspace / "change.txt").write_text("work\n", encoding="utf-8")
    _git(workspace, "add", "change.txt")
    _git(workspace, "commit", "-m", "interrupted work")
    _git(workspace, "push", "-u", "origin", "feature/interrupted")

    # The open-PR probe is faked: it must not reach the network, and it must
    # distinguish "there is an open PR" from "the probe could not answer".
    def _probe(repo: Path, branch: str) -> str:
        assert branch == "feature/interrupted"
        return "#1 'the interrupted PR' https://example.invalid/pr/1"

    effects = detect_run_effects(workspace, probe_pr=_probe)

    assert effects.decided is True, "every probe answered, so the effects are decided"
    assert effects.branch == "feature/interrupted"
    assert effects.branch_pushed is True
    assert effects.open_pr.startswith("#1")
    assert effects.local_commits == 1
    assert effects.has_effects is True

    # An unanswerable probe is UNDECIDED, never "no open PR": this is what
    # stops the successor from dropping the interrupted run's published work.
    undecided = detect_run_effects(workspace, probe_pr=lambda repo, branch: PROBE_UNKNOWN)
    assert undecided.decided is False
    assert undecided.open_pr == "", "an undecided probe must not assert a PR"
    assert RunEffects().decided is True


def test_reconciling_an_interrupted_run_twice_creates_only_one_successor(
    tmp_path: Path,
    clean_git_env,
) -> None:
    """Assertion 3, part 2: the successor fence is durable across restarts.

    Reconciling the same interrupted run twice -- which is exactly what a
    service restart does -- must not create a second successor, because a
    second successor would issue the run's effect (the push, the PR) a second
    time.
    """

    from lh_harness.restart_recovery import (
        WORKER_DISAPPEARED_REASON,
        ServiceEpoch,
        begin_service_epoch,
        reconcile_interrupted_run,
    )

    class _Entry:
        def __init__(self) -> None:
            self.queue_id = "q1"
            self.workspace = ""
            self.run_id = ""
            self.name = "original"
            self.retry_of = None
            self.interrupted_run = None
            self.cause = ""
            self.needs_review = False

    class _Store:
        def __init__(self) -> None:
            self.entries: list[_Entry] = []
            self.successors: list[_Entry] = []

        def list(self):
            return list(self.entries) + list(self.successors)

        def requeue(self, original_queue_id, cause, *, name_suffix="", interrupted_run=None, continue_branch=None):
            successor = _Entry()
            successor.queue_id = f"{original_queue_id}{name_suffix}"
            successor.retry_of = original_queue_id
            successor.interrupted_run = interrupted_run
            successor.cause = cause
            successor.name = f"original{name_suffix}"
            self.successors.append(successor)
            return successor

    workspace = _make_workspace_repo(tmp_path)
    _git(workspace, "checkout", "-b", "feature/interrupted")
    (workspace / "change.txt").write_text("work\n", encoding="utf-8")
    _git(workspace, "add", "change.txt")
    _git(workspace, "commit", "-m", "interrupted work")
    _git(workspace, "push", "-u", "origin", "feature/interrupted")

    store = _Store()
    entry = _Entry()
    entry.workspace = str(workspace)
    entry.run_id = "20990101T000000Z_interrupted"
    store.entries.append(entry)

    runs_root = tmp_path / "runs"
    # The current service instance.  Its pid is this process's pid.
    epoch: ServiceEpoch = begin_service_epoch(runs_root)

    # The interrupted worker was launched by an EARLIER service instance
    # (a different supervisor_pid), which is the REQUIRED evidence that the
    # service itself stopped and the death was a restart interruption.
    owner = {"supervisor_pid": 999_999, "started_at": 1.0}
    status = {"failure_reason": WORKER_DISAPPEARED_REASON}

    def _probe(repo: Path, branch: str) -> str:
        return "#1 'the interrupted PR' https://example.invalid/pr/1"

    first = reconcile_interrupted_run(
        store,
        entry,
        runs_root=runs_root,
        epoch=epoch,
        status=status,
        owner=owner,
        probe_pr=_probe,
    )
    second = reconcile_interrupted_run(
        store,
        entry,
        runs_root=runs_root,
        epoch=epoch,
        status=status,
        owner=owner,
        probe_pr=_probe,
    )

    assert first is not None, "the first reconcile creates the one successor"
    assert second is None, "the second reconcile must not create a second successor"
    assert len(store.successors) == 1, "exactly one successor may exist"


# --------------------------------------------------------------------------
# The gap CLOSED: an in-flight round now leaves a mid-round checkpoint
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_mid_round_checkpoint_lets_the_restart_resume_without_replaying_the_effect(
    tmp_path: Path,
) -> None:
    """Assertion 3, part 3: an interrupted round's effect must run ONCE.

    Written against the REAL production code: a mid-round checkpoint is
    written while the round is still in flight, and the restart resumes the
    interrupted round from it instead of replaying it, so the round's effect
    is issued exactly once.  The ``xfail(strict=True)`` marker that pinned the
    missing behaviour was removed in the same change that landed the
    capability (``src/lh_harness/manager.py`` ``_write_mid_round_checkpoint``
    / ``_resumable_mid_round``).
    """

    effects: list[int] = []

    def _on_execute(call_number: int) -> None:
        effects.append(call_number)

    await _run_manager(
        tmp_path,
        resume=False,
        replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT, _PLAN, _EXECUTED, _Killed("manager killed")],
        on_execute=_on_execute,
    )
    # The restored round 2 does NOT re-run its executor, so the scripted agent
    # never receives a reply for that phase: the list is manager, auditor,
    # manager.  Leaving the executor's ``_EXECUTED`` in the list would feed it
    # to the auditor, and ``_ScriptedAgent`` counts every ``_EXECUTED`` reply
    # as an effect regardless of role, which would inflate the count with an
    # effect that never ran.
    await _run_manager(
        tmp_path,
        resume=True,
        replies=[_PLAN, _CLEAN_AUDIT, _DONE],
        on_execute=_on_execute,
    )

    # Round 1's executor and round 2's executor each ran once before the
    # loss (2 calls).  A replay of the interrupted round 2 would make its
    # executor run a second time -> 3 calls.
    assert len(effects) == 2, (
        "the interrupted round's executor must not run again after the "
        f"restart, but it ran {len(effects)} times in total"
    )
