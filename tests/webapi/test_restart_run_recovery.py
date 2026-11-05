"""Restart-interrupted run recovery (task easybutt0n-17).

A worker that vanished because the SERVICE stopped (systemd killed the whole
unit with ``KillMode=control-group``) used to end in a silent terminal
failure: the queue entry went to ``failed`` with no successor.  These tests
pin the recovery that gives such a run ONE fenced second chance:

* (a) a run live at service start-up whose worker is gone -> exactly one
  ``-r1`` successor with ``retry_of`` set;
* (b) calling the reconcile twice still yields one successor;
* (c) an entry that already has a restart retry is not retried again;
* (d) the workspace stash is created when the tree is dirty and skipped when
  clean (against a throwaway git repo under ``tmp_path``).

Every git-path behaviour is proven against a throwaway repo -- never against a
live workspace.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from lh_harness.launcher import Launcher
from lh_harness.queue import QueueEntry, QueueStore, default_queue_config
from lh_harness.restart_recovery import (
    INTERRUPTED_BY_RESTART,
    NEEDS_REVIEW_REASON,
    PROBE_UNKNOWN,
    ServiceEpoch,
    already_restart_retried,
    begin_service_epoch,
    classify_worker_death,
    detect_run_effects,
    find_restart_successor,
    probe_open_pr_gh_strict,
    reconcile_interrupted_run,
    stash_interrupted_workspace,
)

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "restart-recovery-test",
    "GIT_AUTHOR_EMAIL": "restart-recovery-test@example.invalid",
    "GIT_COMMITTER_NAME": "restart-recovery-test",
    "GIT_COMMITTER_EMAIL": "restart-recovery-test@example.invalid",
    "HOME": os.environ.get("HOME", "/tmp"),
}

# The reason the supervisor writes when a worker vanished without a report.
WORKER_DISAPPEARED = "worker disappeared without a final report"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=GIT_ENV,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout.strip()


def _make_repo(tmp_path: Path, name: str = "ws") -> Path:
    """A throwaway git repo (bare origin + clone) with one commit.

    The workspace guard needs an ``origin`` remote to resolve the default
    branch, so the repo is built the same way the guard's own tests build
    theirs.
    """

    origin = tmp_path / f"{name}.origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True, env=GIT_ENV,
    )
    repo = tmp_path / name
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, env=GIT_ENV,
    )
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial commit")
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def _make_dirty_repo(tmp_path: Path, name: str = "ws") -> Path:
    """A throwaway git repo with a modified file AND an untracked file.

    The untracked file is the shape the qualification evidence measured: the
    killed run left an untracked ``graphify-out/`` behind, which made the next
    pending entry on that tree skip as a "dirty tree".
    """

    repo = _make_repo(tmp_path, name)
    (repo / "README.md").write_text("interrupted work\n", encoding="utf-8")
    (repo / "graphify-out").mkdir()
    (repo / "graphify-out" / "result.json").write_text("{}\n", encoding="utf-8")
    return repo


class _Supervisor:
    """Supervisor stand-in whose runs die the way the evidence describes."""

    def __init__(self, runs_root: Path) -> None:
        self.runs_root = runs_root
        self._owners: dict[str, dict[str, Any]] = {}
        self._statuses: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []

    def list_run_items(self) -> list[dict[str, Any]]:
        return [{"id": run_id} for run_id in self._statuses]

    def owner(self, run_id: str) -> dict[str, Any]:
        return self._owners.get(run_id, {})

    def status(self, run_id: str) -> dict[str, Any]:
        return self._statuses.get(run_id, {"status": "idle", "run_id": run_id})

    def _run_logs_dir(self, run_id: str) -> Path:
        return self.runs_root / run_id / "lh_harness"

    def create_run(self, **kwargs: Any) -> dict[str, Any]:
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        self._owners[run_id] = {
            "run_id": run_id,
            "workspace": str(kwargs.get("workspace", ".")),
            "supervisor_pid": os.getpid(),
            "started_at": 1_000.0,
        }
        self._statuses[run_id] = {"status": "running", "run_id": run_id}
        self.created.append(kwargs)
        return {"id": run_id, "status": "creating", "owner": self._owners[run_id]}

    def kill(
        self,
        run_id: str,
        *,
        reason: str = WORKER_DISAPPEARED,
        owner_pid: int | None = None,
        started_at: float | None = None,
        exit_signal: str | None = None,
    ) -> None:
        """Make a run look like a worker that vanished."""

        owner = self._owners.setdefault(run_id, {"run_id": run_id})
        if owner_pid is not None:
            owner["supervisor_pid"] = owner_pid
        if started_at is not None:
            owner["started_at"] = started_at
        status: dict[str, Any] = {
            "status": "failed",
            "run_id": run_id,
            "failure_reason": reason,
            "alive": False,
        }
        if exit_signal is not None:
            status["exit_signal"] = exit_signal
        self._statuses[run_id] = status


@pytest.fixture()
def env(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _Supervisor(root)

    def make() -> Launcher:
        return Launcher(supervisor, store, queue_config=default_queue_config(), probe_open_pr=None)

    return root, store, supervisor, make


def _enqueue(store: QueueStore, name: str = "interrupted task", workspace: str = "./ws") -> QueueEntry:
    return store.create(
        {
            "name": name,
            "task": "do the thing",
            "workspace": workspace,
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )


def _pending(store: QueueStore) -> list[QueueEntry]:
    return [entry for entry in store.list() if entry.status == "pending"]


def _successors(store: QueueStore, original_queue_id: str) -> list[QueueEntry]:
    return [entry for entry in store.list() if entry.retry_of == original_queue_id]


# --- (a) one -r1 successor for a run live at start-up ----------------------


def test_interrupted_run_gets_exactly_one_r1_successor(env) -> None:
    """A run whose worker belonged to the PREVIOUS service instance gets one
    ``-r1`` successor carrying ``retry_of``."""

    root, store, supervisor, make = env
    launcher = make()
    entry = _enqueue(store)

    # Launch it, then make the worker vanish the way the service kill did.
    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    assert launched.status == "launched"
    run_id = launched.run_id
    # The owner names an EARLIER service instance's pid: the worker was live
    # at this service's start-up.
    supervisor.kill(run_id, owner_pid=999_999, started_at=1.0)

    asyncio.run(launcher.tick())

    failed = store.get(entry.queue_id)
    assert failed.status == "failed"
    assert failed.failure_cause == INTERRUPTED_BY_RESTART

    successors = _successors(store, entry.queue_id)
    assert len(successors) == 1
    successor = successors[0]
    assert successor.retry_of == entry.queue_id
    assert successor.name == f"{entry.name}-r1"
    # Same workspace, trio, max_rounds and task text.
    assert successor.workspace == entry.workspace
    assert successor.trio == entry.trio
    assert successor.max_rounds == entry.max_rounds
    assert successor.task == entry.task
    assert successor.status == "pending"
    assert successor.failure_cause == INTERRUPTED_BY_RESTART


def test_non_interrupted_worker_death_gets_no_successor(env) -> None:
    """A worker that vanished WITHOUT a restart signal keeps the old
    behaviour: the entry fails with no successor."""

    root, store, supervisor, make = env
    launcher = make()
    entry = _enqueue(store)

    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    supervisor.kill(launched.run_id, owner_pid=os.getpid(), started_at=10_000.0)

    asyncio.run(launcher.tick())

    assert store.get(entry.queue_id).status == "failed"
    assert _successors(store, entry.queue_id) == []


# --- (a2) a signal alone is not enough: the epoch decides ------------------


def test_same_epoch_worker_sigkill_gets_no_successor(env) -> None:
    """A worker of THIS service instance killed out of band gets NO successor.

    The worker took ``SIGKILL``, but the service never stopped: the owner
    names the CURRENT service instance's pid, so the worker belonged to the
    current epoch and its death is an ordinary failure.  Handing it a
    successor would re-run a task whose service was healthy -- the
    over-match this classification must not have.
    """

    root, store, supervisor, make = env
    launcher = make()
    entry = _enqueue(store)

    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    assert launched.status == "launched"
    # The owner names THIS service instance (the launcher's own epoch pid),
    # and the worker died on a shutdown signal.
    assert launcher._service_epoch is not None
    supervisor.kill(
        launched.run_id,
        owner_pid=launcher._service_epoch.pid,
        exit_signal="SIGKILL",
    )

    asyncio.run(launcher.tick())

    failed = store.get(entry.queue_id)
    assert failed.status == "failed"
    # A plain failure: NOT restart-interrupted, and NO successor.
    assert failed.failure_cause != INTERRUPTED_BY_RESTART
    assert _successors(store, entry.queue_id) == []


def test_previous_epoch_worker_gets_exactly_one_successor(env) -> None:
    """A worker of an EARLIER service instance gets exactly ONE successor.

    The owner names a different pid than the current epoch's, so the worker
    was live when this service instance started -- that is the evidence that
    the SERVICE stopped.
    """

    root, store, supervisor, make = env
    launcher = make()
    entry = _enqueue(store)

    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    assert launched.status == "launched"
    assert launcher._service_epoch is not None
    # The owner names an EARLIER service instance's pid: the worker was live
    # at this service's start-up, so this instance's start-up ended it.
    supervisor.kill(
        launched.run_id,
        owner_pid=launcher._service_epoch.pid + 1,
        exit_signal="SIGKILL",
    )

    asyncio.run(launcher.tick())

    failed = store.get(entry.queue_id)
    assert failed.status == "failed"
    assert failed.failure_cause == INTERRUPTED_BY_RESTART

    successors = _successors(store, entry.queue_id)
    assert len(successors) == 1
    successor = successors[0]
    assert successor.retry_of == entry.queue_id
    assert successor.name == f"{entry.name}-r1"
    assert successor.status == "pending"


# --- (b) the reconcile is idempotent ---------------------------------------


def test_reconcile_twice_still_yields_one_successor(env) -> None:
    """Calling the reconcile twice creates exactly one successor."""

    root, store, supervisor, make = env
    launcher = make()
    entry = _enqueue(store)

    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    supervisor.kill(launched.run_id, owner_pid=999_999, started_at=1.0)

    asyncio.run(launcher.tick())
    first = _successors(store, entry.queue_id)
    assert len(first) == 1

    # A second pass over the same entry must not create a second successor.
    failed = store.get(entry.queue_id)
    epoch = launcher._service_epoch
    again = reconcile_interrupted_run(
        store,
        failed,
        runs_root=root,
        epoch=epoch,
        status=supervisor.status(launched.run_id),
        owner=supervisor.owner(launched.run_id),
    )
    assert again is None
    assert len(_successors(store, entry.queue_id)) == 1


def test_reconcile_is_idempotent_when_called_directly_twice(env) -> None:
    """Two direct reconciles of the same entry yield one successor."""

    root, store, supervisor, make = env
    launcher = make()
    entry = _enqueue(store)

    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    supervisor.kill(launched.run_id, owner_pid=999_999, started_at=1.0)
    failed = store.get(entry.queue_id)
    failed.status = "failed"
    store.update(failed)

    epoch = launcher._service_epoch
    kwargs = {
        "runs_root": root,
        "epoch": epoch,
        "status": supervisor.status(launched.run_id),
        "owner": supervisor.owner(launched.run_id),
    }
    first = reconcile_interrupted_run(store, failed, **kwargs)
    second = reconcile_interrupted_run(store, failed, **kwargs)
    assert first is not None
    assert second is None
    assert len(_successors(store, entry.queue_id)) == 1


# --- (c) an entry that already had its restart retry is not retried again --


def test_entry_already_retried_for_this_reason_is_not_retried_again(env) -> None:
    """The ``-r1`` successor itself gets no second restart retry."""

    root, store, supervisor, make = env
    launcher = make()
    entry = _enqueue(store)

    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    supervisor.kill(launched.run_id, owner_pid=999_999, started_at=1.0)
    asyncio.run(launcher.tick())

    [successor] = _successors(store, entry.queue_id)
    assert successor.failure_cause == INTERRUPTED_BY_RESTART
    assert already_restart_retried(successor, store.list()) is True

    # Now the successor's own worker vanishes across another restart.
    supervisor.kill("run-second", owner_pid=999_999, started_at=1.0)
    second = reconcile_interrupted_run(
        store,
        successor,
        runs_root=root,
        epoch=launcher._service_epoch,
        status={"status": "failed", "failure_reason": WORKER_DISAPPEARED},
        owner={"supervisor_pid": 999_999, "started_at": 1.0},
    )
    assert second is None
    assert _successors(store, successor.queue_id) == []


def test_already_restart_retried_is_false_for_a_fresh_entry(env) -> None:
    root, store, supervisor, make = env
    entry = _enqueue(store)
    assert already_restart_retried(entry, store.list()) is False


def test_successor_launches_instead_of_skipping_as_a_dirty_tree(tmp_path: Path) -> None:
    """The whole point of the stash: the successor is NOT skipped.

    The qualification evidence measured an interrupted run leaving an
    untracked ``graphify-out/`` behind, which made the next pending entry on
    that tree skip as a "dirty tree".  With the stash, the successor's
    workspace is clean and the successor launches.
    """

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _Supervisor(root)
    repo = _make_repo(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=default_queue_config(), probe_open_pr=None)

    entry = store.create(
        {
            "name": "interrupted task",
            "task": "do the thing",
            "workspace": str(repo),
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    assert launched.run_id

    # The interrupted run leaves uncommitted work behind.
    (repo / "README.md").write_text("interrupted work\n", encoding="utf-8")
    (repo / "graphify-out").mkdir()
    (repo / "graphify-out" / "result.json").write_text("{}\n", encoding="utf-8")
    supervisor.kill(launched.run_id, owner_pid=999_999, started_at=1.0)

    # The pass that reconciles the interruption: the successor is created.
    asyncio.run(launcher.tick())
    [successor] = _successors(store, entry.queue_id)
    assert successor.status == "pending"

    # The next pass launches the successor (the workspace was stashed, so it
    # is no longer refused as a "dirty tree").
    asyncio.run(launcher.tick())
    relaunched = store.get(successor.queue_id)
    assert relaunched.status == "launched"
    assert relaunched.run_id
    assert "dirty tree" not in " ".join(relaunched.skip_reasons)


# --- (d) the workspace stash ------------------------------------------------


def test_stash_created_when_the_tree_is_dirty(tmp_path: Path) -> None:
    """A dirty tree (modified + untracked) is stashed, and the stash is KEPT."""

    repo = _make_dirty_repo(tmp_path)
    run_id = "20261104T122016Z_3d4b7584"

    stash_name = stash_interrupted_workspace(repo, run_id)

    assert stash_name == f"lh-harness interrupted {run_id}"
    # The tree is clean afterwards...
    assert _git(repo, "status", "--porcelain") == ""
    # ...and the work is preserved in a stash that still exists.
    listing = _git(repo, "stash", "list")
    assert stash_name in listing
    # The untracked file is inside the stash, not destroyed.
    assert "graphify-out/result.json" in _git(repo, "stash", "show", "--include-untracked", "--name-only")


def test_stash_skipped_when_the_tree_is_clean(tmp_path: Path) -> None:
    """A clean tree has nothing to stash, so no stash is created."""

    repo = _make_repo(tmp_path)
    run_id = "20261104T124058Z_0554822b"

    stash_name = stash_interrupted_workspace(repo, run_id)

    assert stash_name is None
    assert _git(repo, "stash", "list") == ""


def test_stash_never_destroys_work_with_clean_or_reset(tmp_path: Path) -> None:
    """The interrupted run's edits survive the cleanup."""

    repo = _make_dirty_repo(tmp_path)
    stash_interrupted_workspace(repo, "run-x")

    # The edited file's content is recoverable from the stash.
    show = _git(repo, "stash", "show", "--include-untracked", "-p")
    assert "interrupted work" in show
    # The working tree was NOT reset to HEAD: the stash carries the change.
    assert _git(repo, "status", "--porcelain") == ""


def test_stash_name_is_logged_in_the_run_report(tmp_path: Path) -> None:
    """The stash name is recorded in the interrupted run's durable report."""

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _Supervisor(root)
    # The workspace is CLEAN at launch time (so the launch succeeds), and the
    # interrupted run leaves uncommitted work behind -- exactly the shape the
    # qualification evidence measured.
    repo = _make_repo(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=default_queue_config(), probe_open_pr=None)

    entry = store.create(
        {
            "name": "interrupted task",
            "task": "do the thing",
            "workspace": str(repo),
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    assert launched.run_id, "the entry should have launched"
    # The interrupted run leaves a modified file and an untracked dir behind.
    (repo / "README.md").write_text("interrupted work\n", encoding="utf-8")
    (repo / "graphify-out").mkdir()
    (repo / "graphify-out" / "result.json").write_text("{}\n", encoding="utf-8")
    supervisor.kill(launched.run_id, owner_pid=999_999, started_at=1.0)

    asyncio.run(launcher.tick())

    report_path = root / launched.run_id / "lh_harness" / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["stash"] == f"lh-harness interrupted {launched.run_id}"
    assert report["interrupted_by"] == INTERRUPTED_BY_RESTART
    # The stash itself is kept in the workspace, and the tree is clean again.
    assert f"lh-harness interrupted {launched.run_id}" in _git(repo, "stash", "list")
    assert _git(repo, "status", "--porcelain") == ""


# --- the classification itself ---------------------------------------------


def test_classify_worker_death_recognises_a_previous_service_instance() -> None:
    epoch = ServiceEpoch(pid=100, started_at=5_000.0)
    assert (
        classify_worker_death(
            failure_reason=WORKER_DISAPPEARED,
            status={"status": "failed"},
            owner={"supervisor_pid": 99, "started_at": 1.0},
            epoch=epoch,
        )
        == INTERRUPTED_BY_RESTART
    )


def test_classify_worker_death_recognises_a_shutdown_signal() -> None:
    """A shutdown signal CORROBORATES a previous-epoch worker.

    The signal says how the worker died; the epoch says the service went
    down.  A previous-epoch worker that took ``SIGKILL``/``SIGTERM`` (what
    systemd's ``KillMode=control-group`` does to the whole unit) is
    restart-interrupted.
    """

    epoch = ServiceEpoch(pid=100, started_at=5_000.0)
    assert (
        classify_worker_death(
            failure_reason=WORKER_DISAPPEARED,
            status={"status": "failed", "exit_signal": "SIGKILL"},
            owner={"supervisor_pid": 99},
            epoch=epoch,
        )
        == INTERRUPTED_BY_RESTART
    )
    assert (
        classify_worker_death(
            failure_reason=WORKER_DISAPPEARED,
            status={"status": "failed", "exit_code": -15},
            owner={"supervisor_pid": 99},
            epoch=epoch,
        )
        == INTERRUPTED_BY_RESTART
    )


def test_classify_worker_death_ignores_a_shutdown_signal_in_the_same_epoch() -> None:
    """A signal on a SAME-epoch worker is NOT a restart interruption.

    A single worker killed out of band -- OOM killer, an operator, another
    process -- dies on the very same signals while the service keeps
    running.  Treating that as restart-driven would hand an ordinary failure
    a successor, so the signal alone never qualifies.
    """

    epoch = ServiceEpoch(pid=100, started_at=5_000.0)
    for status in (
        {"status": "failed", "exit_signal": "SIGKILL"},
        {"status": "failed", "exit_signal": "SIGTERM"},
        {"status": "failed", "exit_code": -9},
        {"status": "failed", "exit_code": -15},
        {"status": "failed", "exit_code": 137},
    ):
        assert (
            classify_worker_death(
                failure_reason=WORKER_DISAPPEARED,
                status=status,
                owner={"supervisor_pid": 100},
                epoch=epoch,
            )
            is None
        ), status


def test_classify_worker_death_uses_the_epoch_recorded_previous_pid() -> None:
    """The epoch's ``previous_pid`` is a previous-epoch owner by record."""

    epoch = ServiceEpoch(pid=100, started_at=5_000.0, previous_pid=42)
    assert (
        classify_worker_death(
            failure_reason=WORKER_DISAPPEARED,
            status={"status": "failed", "exit_signal": "SIGKILL"},
            owner={"supervisor_pid": 42},
            epoch=epoch,
        )
        == INTERRUPTED_BY_RESTART
    )
    # The current epoch's own pid is NOT a previous epoch, however it died.
    assert (
        classify_worker_death(
            failure_reason=WORKER_DISAPPEARED,
            status={"status": "failed", "exit_signal": "SIGKILL"},
            owner={"supervisor_pid": 100},
            epoch=epoch,
        )
        is None
    )


def test_classify_worker_death_ignores_ordinary_failures() -> None:
    epoch = ServiceEpoch(pid=100, started_at=5_000.0)
    # A different failure reason is never a restart interruption.
    assert (
        classify_worker_death(
            failure_reason="provider_rate_limit",
            status={"status": "failed"},
            owner={"supervisor_pid": 99},
            epoch=epoch,
        )
        is None
    )
    # Same service instance, no shutdown signal: an ordinary crash.
    assert (
        classify_worker_death(
            failure_reason=WORKER_DISAPPEARED,
            status={"status": "failed"},
            owner={"supervisor_pid": 100, "started_at": 6_000.0},
            epoch=epoch,
        )
        is None
    )


def test_service_epoch_records_the_previous_instance(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    first = begin_service_epoch(root, pid=111, started_at=100.0)
    assert first.previous_pid is None

    second = begin_service_epoch(root, pid=222, started_at=200.0)
    assert second.pid == 222
    assert second.previous_pid == 111
    assert second.previous_started_at == 100.0


# --- the shipped unit files document KillMode (and do not change it) -------


REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_UNITS = (
    REPO_ROOT / "packaging" / "lh-harness.service",
    REPO_ROOT / "scripts" / "deploy" / "node" / "templates" / "lh-harness-node.service",
)


@pytest.mark.parametrize("unit", SHIPPED_UNITS, ids=lambda p: p.name)
def test_shipped_unit_documents_killmode_without_changing_it(unit: Path) -> None:
    """KillMode is DOCUMENTED in a comment; the VALUE is not set.

    The unit's KillMode stays exactly as it was (unset, i.e. systemd's
    default ``control-group``): a service stop takes every run worker in
    the unit's cgroup down with it.  That behaviour is DOCUMENTED here, and
    the interruption is handled by the queue's restart recovery -- not by
    changing the unit.
    """

    text = unit.read_text(encoding="utf-8")
    # The comment documents KillMode and names the effective value.
    assert "KillMode" in text
    assert "control-group" in text
    # No ACTIVE KillMode= directive: only mentions inside comments.
    active = [
        line
        for line in text.splitlines()
        if line.strip().startswith("KillMode=")
    ]
    assert active == [], f"KillMode must not be changed in this PR: {active}"


# --- (e) a pushed branch / an open PR means the successor CONTINUES it ----
#
# The operator correction (2026-11-04 14:24Z): the interrupted run's
# effects are detected BEFORE the successor is created.  A successor that
# started fresh would silently drop a branch the interrupted run pushed or
# a PR it opened, so the successor carries the repo's existing
# ``continue_branch`` continuation opt-in instead.


def _push_branch(repo: Path, branch: str, message: str = "interrupted work") -> None:
    """Commit on ``branch`` and push it to origin (a published effect)."""

    _git(repo, "checkout", "-B", branch)
    (repo / "README.md").write_text(f"{message}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    _git(repo, "push", "-q", "-u", "origin", branch)


def test_effect_detection_sees_a_pushed_branch(tmp_path: Path) -> None:
    """A branch the interrupted run pushed is detected as a published effect."""

    repo = _make_repo(tmp_path)
    _push_branch(repo, "feature/interrupted")

    effects = detect_run_effects(repo, probe_pr=None)

    assert effects.decided is True
    assert effects.branch == "feature/interrupted"
    assert effects.branch_pushed is True
    assert effects.has_effects is True


def test_effect_detection_sees_an_open_pr(tmp_path: Path) -> None:
    """An open PR on the interrupted run's branch is detected."""

    repo = _make_repo(tmp_path)
    _push_branch(repo, "feature/interrupted")

    effects = detect_run_effects(
        repo, probe_pr=lambda _repo, branch: f"#42 'interrupted task' https://example.invalid/{branch}"
    )

    assert effects.decided is True
    assert effects.open_pr.startswith("#42")
    assert effects.has_effects is True


def test_effect_detection_sees_local_commits_not_on_origin(tmp_path: Path) -> None:
    """Commits on the branch that never reached origin/main are detected."""

    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-B", "feature/local-only")
    (repo / "README.md").write_text("local only\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "local only")

    effects = detect_run_effects(repo, probe_pr=None)

    assert effects.decided is True
    assert effects.local_commits == 1
    assert effects.branch_pushed is False
    assert effects.has_effects is True


def test_pushed_branch_makes_the_successor_continue_that_branch(tmp_path: Path) -> None:
    """(e) A pushed branch -> the successor continues it (``continue_branch``)."""

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _Supervisor(root)
    repo = _make_repo(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=default_queue_config(), probe_open_pr=None)

    entry = store.create(
        {
            "name": "interrupted task",
            "task": "do the thing",
            "workspace": str(repo),
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    assert launched.run_id

    # The interrupted run published a branch before it died.
    _push_branch(repo, "feature/interrupted")
    supervisor.kill(launched.run_id, owner_pid=999_999, started_at=1.0)

    asyncio.run(launcher.tick())

    [successor] = _successors(store, entry.queue_id)
    # The successor CONTINUES the pushed branch instead of starting fresh.
    assert successor.continue_branch is True
    # ...and the lineage names what it continues.
    assert successor.interrupted_run == launched.run_id
    assert successor.retry_of == entry.queue_id
    # The original entry records its successor.
    assert store.get(entry.queue_id).successor == successor.queue_id


def test_open_pr_makes_the_successor_continue_that_branch(tmp_path: Path) -> None:
    """An open PR on the interrupted run's branch -> the successor continues."""

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _Supervisor(root)
    repo = _make_repo(tmp_path)
    # The launcher's own open-PR probe seam reports an open PR on the
    # interrupted run's branch.
    launcher = Launcher(
        supervisor,
        store,
        queue_config=default_queue_config(),
        probe_open_pr=lambda _repo, branch: (
            f"#42 'interrupted task' https://example.invalid/{branch}"
        ),
    )

    entry = store.create(
        {
            "name": "interrupted task",
            "task": "do the thing",
            "workspace": str(repo),
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    assert launched.run_id

    _push_branch(repo, "feature/interrupted")
    supervisor.kill(launched.run_id, owner_pid=999_999, started_at=1.0)

    asyncio.run(launcher.tick())

    [successor] = _successors(store, entry.queue_id)
    assert successor.continue_branch is True
    assert successor.interrupted_run == launched.run_id


def test_uncertain_effects_mark_the_entry_needs_review_and_create_no_successor(
    tmp_path: Path,
) -> None:
    """An UNDECIDABLE effect -> ``needs_review``, and NO successor.

    Nothing is deleted and nothing is resubmitted blindly: the entry is
    flagged for a human instead.
    """

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _Supervisor(root)
    repo = _make_repo(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=default_queue_config(), probe_open_pr=None)

    entry = store.create(
        {
            "name": "interrupted task",
            "task": "do the thing",
            "workspace": str(repo),
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    assert launched.run_id
    supervisor.kill(launched.run_id, owner_pid=999_999, started_at=1.0)
    failed = store.get(entry.queue_id)
    failed.status = "failed"
    store.update(failed)

    # The remote is unreachable, so "was the branch pushed?" is undecidable.
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))

    successor = reconcile_interrupted_run(
        store,
        failed,
        runs_root=root,
        epoch=launcher._service_epoch,
        status=supervisor.status(launched.run_id),
        owner=supervisor.owner(launched.run_id),
        probe_pr=None,
    )

    assert successor is None
    assert _successors(store, entry.queue_id) == []
    reviewed = store.get(entry.queue_id)
    assert reviewed.needs_review is True
    assert reviewed.reason == NEEDS_REVIEW_REASON


def test_needs_review_is_not_set_when_the_effects_are_decided(tmp_path: Path) -> None:
    """A decided effect set leaves the entry alone."""

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _Supervisor(root)
    repo = _make_repo(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=default_queue_config(), probe_open_pr=None)

    entry = store.create(
        {
            "name": "interrupted task",
            "task": "do the thing",
            "workspace": str(repo),
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    supervisor.kill(launched.run_id, owner_pid=999_999, started_at=1.0)

    asyncio.run(launcher.tick())

    assert store.get(entry.queue_id).needs_review is False
    assert len(_successors(store, entry.queue_id)) == 1


# --- (f) a live stale worker -> the successor waits ------------------------
#
# The operator correction: the successor may not launch while a worker for
# the interrupted run id is still alive -- never two workers for one task.


def test_live_stale_worker_makes_the_successor_wait(tmp_path: Path) -> None:
    """(f) A live worker for the interrupted run id -> the successor waits."""

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _Supervisor(root)
    repo = _make_repo(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=default_queue_config(), probe_open_pr=None)

    entry = store.create(
        {
            "name": "interrupted task",
            "task": "do the thing",
            "workspace": str(repo),
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    assert launched.run_id
    supervisor.kill(launched.run_id, owner_pid=999_999, started_at=1.0)

    asyncio.run(launcher.tick())
    [successor] = _successors(store, entry.queue_id)
    assert successor.interrupted_run == launched.run_id

    # A stale worker for the interrupted run is still alive.
    worker = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "lh_harness",
            "run",
            f"--run-id={launched.run_id}",
            "--no-dashboard",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # The gate sees the live worker and refuses the launch.
        reason = launcher._interrupted_worker_alive_reason(successor)
        assert reason is not None
        assert launched.run_id in reason
        assert "live worker" in reason

        # ...and the launcher really skips the successor instead of
        # launching a second worker for the same task.
        asyncio.run(launcher.tick())
        waiting = store.get(successor.queue_id)
        assert waiting.status == "pending"
        assert waiting.run_id is None
        assert any("live worker" in r for r in waiting.skip_reasons)
    finally:
        worker.terminate()
        worker.wait(timeout=30)


def test_the_successor_launches_once_the_stale_worker_is_gone(tmp_path: Path) -> None:
    """With no live worker for the interrupted run, the gate lets it launch."""

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _Supervisor(root)
    repo = _make_repo(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=default_queue_config(), probe_open_pr=None)

    entry = store.create(
        {
            "name": "interrupted task",
            "task": "do the thing",
            "workspace": str(repo),
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    supervisor.kill(launched.run_id, owner_pid=999_999, started_at=1.0)

    asyncio.run(launcher.tick())
    [successor] = _successors(store, entry.queue_id)

    # No live worker for the interrupted run id.
    assert launcher._interrupted_worker_alive_reason(successor) is None

    asyncio.run(launcher.tick())
    relaunched = store.get(successor.queue_id)
    assert relaunched.status == "launched"
    assert relaunched.run_id


def test_an_ordinary_entry_is_not_gated_by_the_stale_worker_gate(tmp_path: Path) -> None:
    """The gate applies to restart successors only; other entries are
    untouched."""

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _Supervisor(root)
    launcher = Launcher(supervisor, store, queue_config=default_queue_config(), probe_open_pr=None)

    ordinary = store.create(
        {
            "name": "ordinary task",
            "task": "do the thing",
            "workspace": "./ws",
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    assert launcher._interrupted_worker_alive_reason(ordinary) is None


# --- the lineage is recorded on both ends and in the report -----------------


def test_the_lineage_is_written_into_the_run_report(tmp_path: Path) -> None:
    """The stash name and the lineage land in the interrupted run's report."""

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _Supervisor(root)
    repo = _make_repo(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=default_queue_config(), probe_open_pr=None)

    entry = store.create(
        {
            "name": "interrupted task",
            "task": "do the thing",
            "workspace": str(repo),
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    assert launched.run_id
    # The interrupted run leaves uncommitted work behind (the shape the
    # qualification evidence measured).
    (repo / "README.md").write_text("interrupted work\n", encoding="utf-8")
    (repo / "graphify-out").mkdir()
    (repo / "graphify-out" / "result.json").write_text("{}\n", encoding="utf-8")
    supervisor.kill(launched.run_id, owner_pid=999_999, started_at=1.0)

    asyncio.run(launcher.tick())

    [successor] = _successors(store, entry.queue_id)
    report_path = root / launched.run_id / "lh_harness" / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    lineage = report["restart_recovery"]
    assert lineage["original_queue_id"] == entry.queue_id
    assert lineage["successor_queue_id"] == successor.queue_id
    assert lineage["interrupted_run"] == launched.run_id
    assert lineage["stash"] == f"lh-harness interrupted {launched.run_id}"
    # The detected effects are recorded too.
    assert lineage["effects"]["decided"] is True
    # The stash is kept.
    assert f"lh-harness interrupted {launched.run_id}" in _git(repo, "stash", "list")


# --- (g) the open-PR probe tells "unknown" apart from "no open PR" ---------
#
# Review finding 2 (PR #182, comment 6635183102): the launch guard's
# ``probe_open_pr_gh`` returns ``None`` BOTH when there is no open PR and
# when ``gh`` could not answer (missing CLI, non-zero exit, timeout).  For
# effect detection that collapse is not harmless: an undetectable PR on a
# branch pushed nowhere else would be read as "no PR", the successor would
# cut a fresh base, and the interrupted run's published work would be
# silently dropped.
#
# The recovery path therefore has its own probe: an UNKNOWN result leaves
# the effects undecided -> ``needs_review`` and NO successor, while a
# genuine "no open PR" stays decided.


def _fake_gh(tmp_path: Path, name: str, *, body: str = "", exit_code: int = 0) -> Path:
    """A fake ``gh`` executable that prints ``body`` and exits ``exit_code``."""

    script = tmp_path / name
    script.write_text(
        "#!/bin/sh\n"
        f"cat <<'EOF'\n{body}\nEOF\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_probe_reports_unknown_when_gh_is_missing(tmp_path: Path, monkeypatch) -> None:
    """``gh`` not installed -> the probe reports UNKNOWN, not "no open PR"."""

    monkeypatch.setattr("lh_harness.restart_recovery.shutil.which", lambda _name: None)

    result = probe_open_pr_gh_strict(tmp_path, "feature/interrupted")

    assert result == PROBE_UNKNOWN
    assert result is not None


def test_probe_reports_unknown_when_gh_exits_non_zero(tmp_path: Path, monkeypatch) -> None:
    """``gh`` failing (non-zero exit) -> UNKNOWN, not "no open PR"."""

    gh = _fake_gh(tmp_path, "gh", body="gh: not authenticated", exit_code=1)
    monkeypatch.setattr("lh_harness.restart_recovery.shutil.which", lambda _name: str(gh))

    result = probe_open_pr_gh_strict(tmp_path, "feature/interrupted")

    assert result == PROBE_UNKNOWN
    assert result is not None


def test_probe_reports_unknown_when_gh_times_out(tmp_path: Path, monkeypatch) -> None:
    """``gh`` timing out -> UNKNOWN, not "no open PR"."""

    class _Timeout:
        returncode = 0
        stdout = ""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    def _raise_timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="gh", timeout=30)

    monkeypatch.setattr("lh_harness.restart_recovery.shutil.which", lambda _name: "/bin/false")
    monkeypatch.setattr("lh_harness.restart_recovery.subprocess.run", _raise_timeout)

    result = probe_open_pr_gh_strict(tmp_path, "feature/interrupted")

    assert result == PROBE_UNKNOWN
    assert result is not None


def test_probe_reports_no_open_pr_as_a_distinct_result(tmp_path: Path, monkeypatch) -> None:
    """``gh`` answering with an empty list -> "no open PR", NOT unknown."""

    gh = _fake_gh(tmp_path, "gh", body="[]")
    monkeypatch.setattr("lh_harness.restart_recovery.shutil.which", lambda _name: str(gh))

    result = probe_open_pr_gh_strict(tmp_path, "feature/interrupted")

    assert result is None
    assert result != PROBE_UNKNOWN


def test_probe_reports_the_open_pr_when_there_is_one(tmp_path: Path, monkeypatch) -> None:
    """``gh`` answering with a PR -> the PR's description."""

    body = '[{"number": 42, "title": "interrupted task", "url": "https://example.invalid/pr/42"}]'
    gh = _fake_gh(tmp_path, "gh", body=body)
    monkeypatch.setattr("lh_harness.restart_recovery.shutil.which", lambda _name: str(gh))

    result = probe_open_pr_gh_strict(tmp_path, "feature/interrupted")

    assert result is not None
    assert result != PROBE_UNKNOWN
    assert result.startswith("#42")


def test_unknown_probe_leaves_the_effects_undecided(tmp_path: Path) -> None:
    """An UNKNOWN probe result -> the effects are UNDECIDED, not "no PR"."""

    repo = _make_repo(tmp_path)
    _push_branch(repo, "feature/interrupted")

    effects = detect_run_effects(repo, probe_pr=lambda _repo, _branch: PROBE_UNKNOWN)

    assert effects.decided is False
    assert effects.open_pr == ""


def test_no_open_pr_leaves_the_effects_decided(tmp_path: Path) -> None:
    """A genuine "no open PR" -> the effects stay DECIDED (distinct path)."""

    repo = _make_repo(tmp_path)
    _push_branch(repo, "feature/interrupted")

    effects = detect_run_effects(repo, probe_pr=lambda _repo, _branch: None)

    assert effects.decided is True
    assert effects.open_pr == ""


def test_unknown_probe_marks_the_entry_needs_review_and_creates_no_successor(
    tmp_path: Path,
) -> None:
    """An UNKNOWN probe -> ``needs_review``, and NO successor.

    The interrupted run's published work must not be dropped on a guess: the
    probe could not answer, so the entry is flagged for a human.
    """

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _Supervisor(root)
    repo = _make_repo(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=default_queue_config(), probe_open_pr=None)

    entry = store.create(
        {
            "name": "interrupted task",
            "task": "do the thing",
            "workspace": str(repo),
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    assert launched.run_id

    _push_branch(repo, "feature/interrupted")
    supervisor.kill(launched.run_id, owner_pid=999_999, started_at=1.0)
    failed = store.get(entry.queue_id)
    failed.status = "failed"
    store.update(failed)

    # The gh probe cannot answer (the CLI is not installed): UNKNOWN.
    successor = reconcile_interrupted_run(
        store,
        failed,
        runs_root=root,
        epoch=launcher._service_epoch,
        status=supervisor.status(launched.run_id),
        owner=supervisor.owner(launched.run_id),
        probe_pr=lambda _repo, _branch: PROBE_UNKNOWN,
    )

    assert successor is None
    assert _successors(store, entry.queue_id) == []
    reviewed = store.get(entry.queue_id)
    assert reviewed.needs_review is True
    assert reviewed.reason == NEEDS_REVIEW_REASON


def test_a_no_open_pr_probe_result_does_not_force_needs_review(tmp_path: Path) -> None:
    """A genuine "no open PR" does NOT force ``needs_review``.

    The two probe outcomes must stay distinct: an answered probe (even when
    the answer is "there is no PR") is DECIDED and the recovery proceeds.
    """

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _Supervisor(root)
    repo = _make_repo(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=default_queue_config(), probe_open_pr=None)

    entry = store.create(
        {
            "name": "interrupted task",
            "task": "do the thing",
            "workspace": str(repo),
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    assert launched.run_id

    _push_branch(repo, "feature/interrupted")
    supervisor.kill(launched.run_id, owner_pid=999_999, started_at=1.0)
    failed = store.get(entry.queue_id)
    failed.status = "failed"
    store.update(failed)

    # The probe ANSWERED: there is no open PR on this branch.
    successor = reconcile_interrupted_run(
        store,
        failed,
        runs_root=root,
        epoch=launcher._service_epoch,
        status=supervisor.status(launched.run_id),
        owner=supervisor.owner(launched.run_id),
        probe_pr=lambda _repo, _branch: None,
    )

    assert successor is not None
    assert len(_successors(store, entry.queue_id)) == 1
    assert store.get(entry.queue_id).needs_review is False


def test_the_launcher_gives_the_recovery_path_the_strict_probe() -> None:
    """The launcher's DEFAULT open-PR seam must not reach the recovery path.

    The launch guard's default probe collapses "gh could not answer" into
    "no open PR"; the recovery path needs the opposite semantics, so the
    launcher swaps in the strict probe for the default seam.  An explicitly
    injected seam stays the caller's own probe, and ``None`` keeps the
    probe disabled.
    """

    from lh_harness.launcher import Launcher as _Launcher
    from lh_harness.workspace_guard import probe_open_pr_gh

    class _Noop:
        pass

    class _NoopQueue:
        pass

    # The DEFAULT seam -> the strict probe.
    default_launcher = _Launcher(_Noop(), _NoopQueue())
    assert default_launcher._restart_probe_pr() is probe_open_pr_gh_strict

    # An explicitly injected seam -> honoured as-is.
    custom = lambda _repo, _branch: "#42 'x' https://example.invalid/pr/42"
    injected_launcher = _Launcher(_Noop(), _NoopQueue(), probe_open_pr=custom)
    assert injected_launcher._restart_probe_pr() is custom

    # An explicitly disabled probe -> stays disabled.
    disabled_launcher = _Launcher(_Noop(), _NoopQueue(), probe_open_pr=None)
    assert disabled_launcher._restart_probe_pr() is None
