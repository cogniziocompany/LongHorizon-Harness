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
import uuid
from pathlib import Path
from typing import Any

import pytest

from lh_harness.launcher import Launcher
from lh_harness.queue import QueueEntry, QueueStore, default_queue_config
from lh_harness.restart_recovery import (
    INTERRUPTED_BY_RESTART,
    ServiceEpoch,
    already_restart_retried,
    begin_service_epoch,
    classify_worker_death,
    find_restart_successor,
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
    ) -> None:
        """Make a run look like a worker that vanished."""

        owner = self._owners.setdefault(run_id, {"run_id": run_id})
        if owner_pid is not None:
            owner["supervisor_pid"] = owner_pid
        if started_at is not None:
            owner["started_at"] = started_at
        self._statuses[run_id] = {
            "status": "failed",
            "run_id": run_id,
            "failure_reason": reason,
            "alive": False,
        }


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
    epoch = ServiceEpoch(pid=100, started_at=5_000.0)
    assert (
        classify_worker_death(
            failure_reason=WORKER_DISAPPEARED,
            status={"status": "failed", "exit_signal": "SIGKILL"},
            owner={"supervisor_pid": 100},
            epoch=epoch,
        )
        == INTERRUPTED_BY_RESTART
    )
    assert (
        classify_worker_death(
            failure_reason=WORKER_DISAPPEARED,
            status={"status": "failed", "exit_code": -15},
            owner={"supervisor_pid": 100},
            epoch=epoch,
        )
        == INTERRUPTED_BY_RESTART
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
