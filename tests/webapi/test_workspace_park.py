"""Auto-park tests (task 260).

Every git-path behaviour is proven against throwaway repos built under
``tmp_path`` (bare origin + clone) — never against a live workspace.

The four endings named by task 260 (end_of_round stop, cancelled, done,
failed) all canonicalize into :data:`TERMINAL_STATUSES`, and the supervisor's
single terminal gate calls :func:`park_workspace`; these tests prove the park
itself for the three tree states (clean+pushed, dirty, unpushed) and that a
queued task launches on the parked workspace immediately afterwards.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from lh_harness.supervisor.lifecycle import TERMINAL_STATUSES, is_terminal_status
from lh_harness.workspace_park import (
    _BACKUP_REF_NS,
    _detect_default_branch,
    park_workspace,
)

from .test_launcher_workspace_guard import (
    GIT_ENV,
    NO_PR,
    _entry,
    _feature_branch,
    _git,
    _launcher,
    _make_repo,
)

_BACKUP_REF = "refs/lh-harness/parked"


def _owner(run_branch: str) -> dict[str, Any]:
    """An owner record shaped like the durable launch reservation (launcher.py
    stores ``workspace_base_summary`` with the run branch in it)."""

    return {
        "workspace": "irrelevant-here",
        "workspace_base_summary": (
            f"mode=in-place; checked-out-branch='{run_branch}'; "
            f"run-branch='{run_branch}'; base=origin/main"
        ),
    }


def _park(
    run_id: str,
    repo: Path,
    *,
    events: list | None = None,
    reports: list | None = None,
    owner: dict[str, Any] | None = None,
) -> dict[str, Any]:
    def record_event(event_type: str, payload: dict[str, Any]) -> None:
        if events is not None:
            events.append((event_type, payload))

    def record_report(payload: dict[str, Any]) -> None:
        if reports is not None:
            reports.append(payload)

    return park_workspace(
        run_id,
        repo,
        owner=owner if owner is not None else _owner("feat/other-task"),
        record_event=record_event,
        record_report=record_report,
    )


def _stash_list(repo: Path) -> list[str]:
    return [line for line in _git(repo, "stash", "list").splitlines() if line.strip()]


def test_all_four_task260_endings_are_terminal() -> None:
    """The park gate sees every task-260 ending through one terminal set.

    ``end_of_round`` stops end as ``completed``; ``done`` is an accepted alias
    of ``completed``; cancelled and failed are their own terminal statuses.
    """

    assert {"completed", "failed", "cancelled"} <= TERMINAL_STATUSES
    assert is_terminal_status("done")
    assert is_terminal_status("complete")
    assert is_terminal_status("failed")
    assert is_terminal_status("cancelled")
    assert not is_terminal_status("running")


def test_park_clean_and_pushed_detaches_to_origin_default(tmp_path: Path) -> None:
    """Deliverable 1 (clean path): pushed run branch, clean tree → detach.

    Measured refusal (TASK 260): a run's PR branch stayed checked out after
    the run ended, so the launcher refused every later task for that
    workspace.  After the park the workspace is detached at origin/<default>
    and nothing needs a backup.
    """

    repo = _make_repo(tmp_path)
    _feature_branch(repo)  # clean and synced with origin
    pre_park_head = _git(repo, "rev-parse", "HEAD")
    events: list = []
    reports: list = []

    result = _park("run-260-clean", repo, events=events, reports=reports)

    assert result["parked"] is True, result["error"]
    assert result["from_branch"] == "feat/other-task"
    assert result["default_branch"] == "main"
    assert result["backup_ref"] is None
    assert result["stash"] is None
    # Detached exactly at origin/<default>.
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "refs/remotes/origin/main")
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    # The run branch itself survives untouched (never deleted).
    assert "feat/other-task" in _git(repo, "branch", "--list", "feat/other-task")
    assert _git(repo, "rev-parse", "feat/other-task") == pre_park_head
    # No backup ref, no stash was needed or created.
    assert not _backup_ref_exists(repo, "run-260-clean")
    assert _stash_list(repo) == []
    # The real event fired exactly once with the park summary.
    assert [(kind, payload["run_id"]) for kind, payload in events] == [
        ("workspace.parked", "run-260-clean")
    ]
    payload = events[0][1]
    assert payload["from_branch"] == "feat/other-task"
    assert payload["backup_ref"] is None
    assert payload["parked"] is True
    # A successful park is recorded in the run's report even without a backup
    # so the fleet can see the workspace is no longer occupied.
    assert len(reports) == 1
    report = reports[0]
    assert report["status"] == "parked"
    assert report["run_id"] == "run-260-clean"
    assert report["backup_ref"] is None
    assert report["stash"] is None
    assert report["default_branch"] == "main"


def _backup_ref(repo: Path, run_id: str) -> str:
    return f"{_BACKUP_REF}/{run_id}"


def _backup_ref_exists(repo: Path, run_id: str) -> bool:
    probe = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
         _backup_ref(repo, run_id)],
        capture_output=True, text=True, env=GIT_ENV,
    )
    return probe.returncode == 0


def test_park_dirty_preserves_work_in_backup_ref_and_named_stash(tmp_path: Path) -> None:
    """Deliverable 2 (dirty path): backup ref + `stash push -u`, nothing lost.

    Nothing may be discarded: uncommitted (tracked and untracked) work is
    preserved under the backup ref and a named stash before the detach, and
    both are recorded so the work is recoverable and visible.
    """

    repo = _make_repo(tmp_path)
    _feature_branch(repo)
    tracked = repo / "tracked_change.txt"
    tracked.write_text("uncommitted tracked edit\n", encoding="utf-8")
    untracked_dir = repo / "fleet-admin"
    untracked_dir.mkdir()
    untracked = untracked_dir / "other-task.txt"
    untracked.write_text("another task's uncommitted work\n", encoding="utf-8")
    _git(repo, "add", "tracked_change.txt")  # staged tracked edit; untracked left as-is
    pre_park_head = _git(repo, "rev-parse", "HEAD")
    events: list = []
    reports: list = []

    result = _park("run-260-dirty", repo, events=events, reports=reports)

    assert result["parked"] is True, result["error"]
    # The backup ref preserves the pre-park HEAD exactly.
    backup = _backup_ref(repo, "run-260-dirty")
    assert result["backup_ref"] == backup
    assert _git(repo, "rev-parse", backup) == pre_park_head
    # A named stash with -u preserved the dirty work (tracked AND untracked).
    stash_message = "lh-harness park run-260-dirty"
    assert result["stash"] == stash_message
    stash_lines = [line for line in _stash_list(repo) if stash_message in line]
    assert len(stash_lines) == 1, _stash_list(repo)
    stash_ref = stash_lines[0].split()[0].rstrip(":")  # e.g. stash@{0}
    # `stash show --name-only` lists the tracked work in the stash (the ^3
    # parent of the stash commit carries the untracked files that -u adds).
    tracked_files = _git(repo, "stash", "show", "--name-only", stash_ref)
    assert "tracked_change.txt" in tracked_files
    untracked_files = _git(repo, "show", "--format=", "--name-only", f"{stash_ref}^3")
    assert "fleet-admin/other-task.txt" in untracked_files
    # The worktree is now parked on origin/<default>, detached.
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "refs/remotes/origin/main")
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") != "feat/other-task"
    # Nothing was lost or discarded: the original branch is still there.
    assert "feat/other-task" in _git(repo, "branch", "--list", "feat/other-task")
    # The run's report records both the backup ref and the stash.
    assert reports, "the park summary must be recorded in the run's report"
    report = reports[-1]
    assert report["backup_ref"] == backup
    assert report["stash"] == stash_message
    assert report["run_id"] == "run-260-dirty"
    # ...and the queue event carries them for the overseer/fleet.
    kinds = [kind for kind, _payload in events]
    assert kinds == ["workspace.parked"]
    payload = events[0][1]
    assert payload["run_id"] == "run-260-dirty"
    assert payload["from_branch"] == "feat/other-task"
    assert payload["backup_ref"] == backup
    assert payload["stash"] == stash_message


def test_park_unpushed_commits_preserves_them_in_backup_ref(tmp_path: Path) -> None:
    """Deliverable 2 (unpushed commits): HEAD not on the fetched remote ref.

    A clean tree has nothing to stash, so the backup ref alone must carry the
    unpushed commits; parking still detaches and loses nothing.
    """

    repo = _make_repo(tmp_path)
    _feature_branch(repo)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "local-only commit ahead of remote")
    pre_park_head = _git(repo, "rev-parse", "HEAD")
    # Sanity: HEAD really is ahead of the fetched origin/<run branch>.
    assert _git(repo, "rev-list", "--count", "refs/remotes/origin/feat/other-task..HEAD") == "1"
    events: list = []
    reports: list = []

    result = _park("run-260-unpushed", repo, events=events, reports=reports)

    assert result["parked"] is True, result["error"]
    backup = _backup_ref(repo, "run-260-unpushed")
    assert result["backup_ref"] == backup
    assert _git(repo, "rev-parse", backup) == pre_park_head
    # A clean tree has nothing to stash: no stash, no error.
    assert result["stash"] is None
    assert result["error"] is None
    # Detached at origin/<default>; the commits survive on the backup ref
    # (and on the never-deleted run branch).
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "refs/remotes/origin/main")
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert _git(repo, "rev-parse", "feat/other-task") == pre_park_head
    assert "feat/other-task" in _git(repo, "branch", "--list", "feat/other-task")
    # Report + event record the recoverable backup ref.
    assert reports and reports[-1]["backup_ref"] == backup
    payload = events[-1][1]
    assert payload["backup_ref"] == backup


def test_queued_task_launches_immediately_after_park(tmp_path: Path) -> None:
    """Deliverable 4: the parked workspace accepts the next queued task now.

    The measured failure was priority inversion: the launcher refused every
    later task for the ended run's workspace until someone detached it by
    hand.  After the park the next queued entry launches in the same cycle it
    becomes eligible — no hand-parking, no refusal.
    """

    repo = _make_repo(tmp_path)
    _feature_branch(repo)  # the finished run's PR branch, still checked out
    events: list = []
    result = _park("run-260-then-next", repo, events=events)
    assert result["parked"] is True, result["error"]
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"

    launcher, store, supervisor = _launcher(tmp_path, probe_open_pr=NO_PR)
    entry = _entry(store, repo)

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None and updated.status == "launched", updated.skip_reasons
    assert len(supervisor.created) == 1
    assert Path(supervisor.created[0]["workspace"]).resolve() == repo.resolve()
    # The queued task runs on a fresh branch cut from origin/<default>.
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").startswith("lh-run/")
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "refs/remotes/origin/main")
    # Nothing in the queue log refused the parked workspace.
    service_log = tmp_path / "runs" / "queue" / "service_events.jsonl"
    if service_log.is_file():
        assert "workspace base refused" not in service_log.read_text(encoding="utf-8")


def test_queued_task_launches_after_a_dirty_park_too(tmp_path: Path) -> None:
    """Deliverable 4, dirty variant: the stash path also unblocks the queue."""

    repo = _make_repo(tmp_path)
    _feature_branch(repo)
    dirty = repo / "leftover.txt"
    dirty.write_text("uncommitted leftover\n", encoding="utf-8")
    _git(repo, "add", "leftover.txt")
    result = _park("run-260-dirty-then-next", repo)
    assert result["parked"] is True, result["error"]

    launcher, store, supervisor = _launcher(tmp_path, probe_open_pr=NO_PR)
    entry = _entry(store, repo)

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None and updated.status == "launched", updated.skip_reasons
    assert len(supervisor.created) == 1
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").startswith("lh-run/")


def test_park_default_branch_resolution(tmp_path: Path) -> None:
    """Deliverable 1: origin/HEAD symref first, then main, then the
    powerplatform workspace's develop convention."""

    # A symbolic origin/HEAD always wins.
    symrepo = _make_repo(tmp_path, name="ws-symref", default_branch="main")
    _git(symrepo, "push", "-q", "origin", "main:develop")
    subprocess.run(
        ["git", "-C", str(symrepo), "remote", "set-head", "origin", "develop"],
        check=True, capture_output=True, env=GIT_ENV,
    )
    assert _detect_default_branch(symrepo) == "develop"

    # Without a symref, a plain workspace falls back to main...
    plain = _make_repo(tmp_path, name="ws-plain", default_branch="main")
    _git(plain, "remote", "set-head", "origin", "--delete")
    assert _detect_default_branch(plain) == "main"

    # ...and a powerplatform workspace falls back to develop.
    power = _make_repo(tmp_path, name="cognizioware-powerplatform", default_branch="main")
    _git(power, "push", "-q", "origin", "main:develop")
    _git(power, "remote", "set-head", "origin", "--delete")
    assert _detect_default_branch(power) == "develop"