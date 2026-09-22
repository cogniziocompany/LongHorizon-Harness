"""Workspace branch guard tests (task 195).

Every git-path behaviour is proven against throwaway repos built under
``tmp_path`` (bare origin + clone) — never against a live workspace.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from lh_harness.launcher import Launcher
from lh_harness.queue import QueueStore, default_queue_config
from lh_harness.workspace_guard import (
    WorkspaceBaseError,
    prepare_workspace_base,
    probe_open_pr_gh,
)

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "guard-test",
    "GIT_AUTHOR_EMAIL": "guard-test@example.invalid",
    "GIT_COMMITTER_NAME": "guard-test",
    "GIT_COMMITTER_EMAIL": "guard-test@example.invalid",
    "HOME": os.environ.get("HOME", "/tmp"),
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=GIT_ENV,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout.strip()


def _make_repo(tmp_path: Path, name: str = "ws", default_branch: str = "main") -> Path:
    origin = tmp_path / f"{name}.origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", default_branch, str(origin)],
        check=True, capture_output=True, env=GIT_ENV,
    )
    repo = tmp_path / name
    subprocess.run(["git", "init", "-b", default_branch, str(repo)], check=True, capture_output=True, env=GIT_ENV)
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial commit")
    _git(repo, "push", "-q", "-u", "origin", default_branch)
    return repo


def _feature_branch(repo: Path, name: str = "feat/other-task", *, push: bool = True, commit: bool = True) -> None:
    _git(repo, "checkout", "-q", "-b", name)
    if commit:
        (repo / "feature.txt").write_text(f"feature work {uuid.uuid4().hex[:6]}\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "feature work")
        if push:
            _git(repo, "push", "-q", "-u", "origin", name)


class FakeSupervisor:
    """In-memory supervisor stand-in that records create_run calls."""

    def __init__(self, runs_root: Path) -> None:
        self.runs_root = runs_root
        self.workspace_root = runs_root.parent / "workspaces"
        self.created: list[dict[str, Any]] = []

    def list_run_items(self) -> list[dict[str, Any]]:
        return []

    def owner(self, run_id: str) -> dict[str, Any]:
        return {}

    def status(self, run_id: str) -> dict[str, Any]:
        return {"status": "idle", "run_id": run_id}

    def create_run(self, **kwargs: Any) -> dict[str, Any]:
        result = {"id": f"run-{len(self.created) + 1}", **kwargs}
        self.created.append(result)
        return result


def _launcher(
    tmp_path: Path,
    probe_open_pr=None,
    *,
    occupancy_ignore_dirty: bool = False,
) -> tuple[Launcher, QueueStore, FakeSupervisor]:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    supervisor = FakeSupervisor(root)
    store = QueueStore(root)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 1
    # Task 173 scope 4: a dirty tree is an OCCUPIED workspace in the default
    # configuration, so guard tests whose fixtures carry foreign uncommitted
    # work opt the environment out of the occupancy probes to reach the guard
    # layer under test (that override is the documented overseer switch).
    config["occupancy_ignore_dirty"] = occupancy_ignore_dirty
    launcher = Launcher(supervisor, store, queue_config=config, probe_open_pr=probe_open_pr)
    return launcher, store, supervisor


def _entry(store: QueueStore, workspace: Path) -> Any:
    return store.create(
        {
            "name": "task",
            "task": "do something",
            "workspace": str(workspace),
            "trio": "kimi",
            "priority": 10,
            "requested_by": "ci",
        }
    )


NO_PR = lambda repo, branch: None


def test_non_default_branch_gets_fresh_run_branch_from_origin_default(tmp_path: Path) -> None:
    """Deliverable 1: a clean non-default branch is never launched on as-is."""

    repo = _make_repo(tmp_path)
    _feature_branch(repo)  # clean and synced with origin
    launcher, store, supervisor = _launcher(tmp_path, probe_open_pr=NO_PR)
    entry = _entry(store, repo)

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None and updated.status == "launched"
    assert len(supervisor.created) == 1
    launched_workspace = supervisor.created[0]["workspace"]
    assert Path(launched_workspace).resolve() == repo.resolve()
    # The run now sits on its own branch cut fresh from origin/main.
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").startswith("lh-run/")
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "refs/remotes/origin/main")
    # The previous branch survives untouched.
    assert "feat/other-task" in _git(repo, "branch", "--list", "feat/other-task")
    assert _git(repo, "rev-parse", "feat/other-task") != _git(repo, "rev-parse", "HEAD")


def test_dirty_non_default_branch_preserves_foreign_work(tmp_path: Path) -> None:
    """Deliverable 2: foreign uncommitted files and ahead-of-remote commits survive."""

    repo = _make_repo(tmp_path)
    _feature_branch(repo)
    foreign_uncommitted = repo / "fleet-admin" / "other-task.txt"
    foreign_uncommitted.parent.mkdir()
    foreign_uncommitted.write_text("another task's uncommitted work\n", encoding="utf-8")
    _git(repo, "add", "fleet-admin")
    (repo / "tracked_change.txt").write_text("uncommitted tracked edit\n", encoding="utf-8")
    # An ahead-of-remote commit on top of the pushed branch.
    _git(repo, "commit", "-q", "-m", "local-only commit ahead of remote")
    ahead_sha = _git(repo, "rev-parse", "HEAD")
    # The tree here is deliberately dirty (that is the scenario), so this test
    # uses the occupancy override to reach the guard layer (task 173 scope 4).
    launcher, store, supervisor = _launcher(
        tmp_path, probe_open_pr=NO_PR, occupancy_ignore_dirty=True
    )
    entry = _entry(store, repo)

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None and updated.status == "launched"
    assert len(supervisor.created) == 1
    # The run executes in a fresh worktree branched from origin/main...
    run_workspace = Path(supervisor.created[0]["workspace"])
    assert run_workspace != repo
    assert run_workspace.is_dir()
    assert (run_workspace / ".git").is_file()  # linked-worktree marker
    assert _git(run_workspace, "rev-parse", "--abbrev-ref", "HEAD").startswith("lh-run/")
    assert _git(run_workspace, "rev-parse", "HEAD") == _git(repo, "rev-parse", "refs/remotes/origin/main")
    assert not list(run_workspace.glob("fleet-admin/**"))
    # ...while the original branch is untouched: same HEAD, still dirty, still ahead.
    assert _git(repo, "rev-parse", "HEAD") == ahead_sha
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feat/other-task"
    assert foreign_uncommitted.read_text(encoding="utf-8").startswith("another task's")
    assert (repo / "tracked_change.txt").exists()
    assert _git(repo, "status", "--porcelain").strip() != ""


def test_open_pr_collision_blocks_retryably_naming_branch_and_pr(tmp_path: Path) -> None:
    """Deliverable 3: a colliding OPEN PR produces the loud named refusal.

    Task 201: the refusal is retryable — the entry stays ``pending`` with the
    reason recorded — instead of becoming a terminal ``failed`` row.
    """

    repo = _make_repo(tmp_path)
    _feature_branch(repo)
    launcher, store, supervisor = _launcher(
        tmp_path,
        probe_open_pr=lambda repo, branch: "#108 'other task work' https://gh.example/pr/108",
    )
    entry = _entry(store, repo)

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None and updated.status == "pending", (
        "a guard refusal is retryable: the entry must stay pending, not fail"
    )
    assert updated.skip_reasons, "the refusal reason must be recorded on the entry"
    reason = updated.skip_reasons[-1]
    assert "feat/other-task" in reason
    assert "#108" in reason and "https://gh.example/pr/108" in reason
    assert "refusing to launch" in reason
    assert updated.reason is None, "no terminal reason: the entry never failed"
    # Nothing was launched and nothing was touched.
    assert len(supervisor.created) == 0
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feat/other-task"
    assert repo.resolve() not in [Path(p).resolve() for p in _git(repo, "worktree", "list", "--porcelain").splitlines() if Path(p).exists()]


def test_default_configuration_probes_open_pr_and_fails_loudly(tmp_path: Path) -> None:
    """Production wiring: no flag/env disables the PR probe (deliverable 3 default-on).

    When ``Launcher`` is constructed without the explicit ``probe_open_pr`` hook
    used by tests/offline runs, it must default to the real PR probe so a
    non-default branch with a simulated OPEN PR fails loudly naming both the
    branch and the PR in plain production configuration.
    """

    repo = _make_repo(tmp_path)
    _feature_branch(repo)

    # Simulate the production call site (webapi/server.py): no probe kwarg.
    launcher, store, supervisor = _launcher(tmp_path)
    # Override only the gh-probe implementation to avoid needing ``gh`` CLI
    # and a real GitHub remote; keep the default-on behaviour of the Launcher
    # wiring (the probe is not explicitly disabled via ``probe_open_pr=None``).
    launcher.probe_open_pr = lambda repo, branch: "#154 'MCP namespace contract' https://gh.example/pr/154"
    entry = _entry(store, repo)

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None and updated.status == "pending"
    assert updated.skip_reasons, "the refusal reason must be recorded on the entry"
    reason = updated.skip_reasons[-1]
    assert "feat/other-task" in reason
    assert "#154" in reason
    assert "https://gh.example/pr/154" in reason
    assert "refusing to launch" in reason
    assert len(supervisor.created) == 0
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feat/other-task"


def test_unresolvable_base_blocks_retryably_naming_branch(tmp_path: Path) -> None:
    """Deliverable 3: no default branch on origin => loud retryable refusal."""

    repo = _make_repo(tmp_path)
    _feature_branch(repo)
    # Point origin at an empty bare remote: no refs at all, so no default
    # branch can be resolved after the fetch.
    empty = tmp_path / "empty.origin.git"
    subprocess.run(["git", "init", "--bare", str(empty)], check=True, capture_output=True, env=GIT_ENV)
    _git(repo, "remote", "set-url", "origin", str(empty))
    launcher, store, supervisor = _launcher(tmp_path, probe_open_pr=NO_PR)
    entry = _entry(store, repo)

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None and updated.status == "pending"
    assert updated.skip_reasons
    reason = updated.skip_reasons[-1]
    assert "feat/other-task" in reason
    assert "refusing to launch" in reason
    assert len(supervisor.created) == 0


def test_default_branch_launches_unchanged(tmp_path: Path) -> None:
    """Happy path: a workspace already on the default branch launches as-is."""

    repo = _make_repo(tmp_path)
    launcher, store, supervisor = _launcher(tmp_path, probe_open_pr=NO_PR)
    entry = _entry(store, repo)

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None and updated.status == "launched"
    assert len(supervisor.created) == 1
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert Path(supervisor.created[0]["workspace"]).resolve() == repo.resolve()


def test_guard_unit_not_a_repo_returns_launchable(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    outcome = prepare_workspace_base(plain, run_label="x", probe_open_pr=NO_PR)
    assert outcome.mode == "not-a-repo"
    assert outcome.workspace == plain


def test_guard_unit_stash_fallback_preserves_work_in_named_stash(tmp_path: Path) -> None:
    """When a worktree cannot be created, a named stash preserves the leftover."""

    repo = _make_repo(tmp_path)
    _feature_branch(repo)
    foreign = repo / "foreign" / "keep-me.txt"
    foreign.parent.mkdir()
    foreign.write_text("foreign uncommitted work\n", encoding="utf-8")
    _git(repo, "add", "foreign")
    # Make worktree creation impossible: the sibling path is pre-occupied.
    sibling = repo.parent / f"{repo.name}.run-unit-stash"
    sibling.mkdir(parents=True)
    (sibling / "occupied.bin").write_bytes(b"occupied")

    outcome = prepare_workspace_base(repo, run_label="unit-stash", base_root=None, probe_open_pr=NO_PR)

    assert outcome.mode == "stash"
    assert outcome.stashed is True
    assert (repo / "foreign" / "keep-me.txt").exists() is False  # moved into the stash
    stash_list = _git(repo, "stash", "list")
    assert "unit-stash" in stash_list or "prelaunch guard" in stash_list
    # The stash content is recoverable.
    show = subprocess.run(
        ["git", "-C", str(repo), "stash", "show", "--name-only", "stash@{0}"],
        capture_output=True, text=True, env=GIT_ENV,
    )
    assert show.returncode == 0 and "foreign/keep-me.txt" in show.stdout
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").startswith("lh-run/")
    # ...and the run branch is fresh from origin/main.
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "refs/remotes/origin/main")


def test_guard_unit_error_message_names_branch_on_bad_origin(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _feature_branch(repo)
    _git(repo, "remote", "set-url", "origin", "/nonexistent/origin.git")
    with pytest.raises(WorkspaceBaseError) as excinfo:
        prepare_workspace_base(repo, run_label="unit-fail", probe_open_pr=NO_PR)
    message = str(excinfo.value)
    assert "feat/other-task" in message
    assert "refusing to launch" in message


def test_launcher_defaults_probe_to_real_gh_probe() -> None:
    """Default-on wiring: an un-configured Launcher uses the real gh probe."""

    launcher = Launcher(object(), object())  # type: ignore[arg-type]
    assert launcher.probe_open_pr is probe_open_pr_gh


# ---------------------------------------------------------------------------
# Task 201: continuation opt-in (`branch` / `continue_branch`)
# ---------------------------------------------------------------------------


def _entry_with_opt_in(store: QueueStore, workspace: Path, **opt_in: Any) -> Any:
    body = {
        "name": "task",
        "task": "do something",
        "workspace": str(workspace),
        "trio": "kimi",
        "priority": 10,
        "requested_by": "ci",
    }
    body.update(opt_in)
    return store.create(body)


OPEN_PR = "#108 'other task work' https://gh.example/pr/108"


def test_continuation_entry_with_open_pr_launches_on_its_branch(tmp_path: Path) -> None:
    """Task 201 (a): a continuation entry launches on its OPEN-PR branch.

    Under the task 195 guard this shape was refused outright; the opt-in must
    launch the run on the branch as-is, with no relocation, no stash, and no
    refusal, and the round-zero mode the guard chose is "continuation".
    """

    repo = _make_repo(tmp_path)
    _feature_branch(repo, name="feat/other-task")  # pushed; open PR heads it
    launcher, store, supervisor = _launcher(
        tmp_path,
        probe_open_pr=lambda repo, branch: OPEN_PR,
    )
    entry = _entry_with_opt_in(store, repo, branch="feat/other-task")

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None and updated.status == "launched"
    assert len(supervisor.created) == 1
    # The run stayed on the PR branch in the original workspace.
    assert Path(supervisor.created[0]["workspace"]).resolve() == repo.resolve()
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feat/other-task"
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "feat/other-task")
    # The guard's chosen mode reaches the run so the round-zero record can
    # name it (deliverable 4).
    assert supervisor.created[0]["workspace_base_mode"] == "continuation"
    assert "continuation" in supervisor.created[0]["workspace_base_summary"]
    # A push from the run would update the PR branch the task was written to
    # finish: the branch is the one the entry named, unchanged.
    assert _git(repo, "rev-parse", "feat/other-task") == _git(repo, "rev-parse", "origin/feat/other-task")


def test_non_opt_in_entry_on_the_same_branch_is_still_refused(tmp_path: Path) -> None:
    """Task 201 (b): without the opt-in, the OPEN-PR branch is still refused.

    The default guard must keep its full strength: a plain entry pointing at
    the same workspace and branch is blocked (retryably) and the run is never
    relocated onto it.
    """

    repo = _make_repo(tmp_path)
    _feature_branch(repo, name="feat/other-task")
    launcher, store, supervisor = _launcher(
        tmp_path,
        probe_open_pr=lambda repo, branch: OPEN_PR,
    )
    entry = _entry(store, repo)  # no opt-in of any kind

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None and updated.status == "pending"
    reason = updated.skip_reasons[-1]
    assert "feat/other-task" in reason and "#108" in reason
    assert "refusing to launch" in reason
    # Still refused, not silently relocated.
    assert len(supervisor.created) == 0
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feat/other-task"
    assert updated.reason is None, "the refusal stays retryable, not terminal"


def test_continuation_entry_with_dirty_tree_keeps_the_tree(tmp_path: Path) -> None:
    """Task 201 (c): an opt-in entry launches on a dirty branch without touching it.

    No stash, no worktree, no relocation: the uncommitted work the workspace
    carries is still there, uncommitted, when the run starts.
    """

    repo = _make_repo(tmp_path)
    _feature_branch(repo, name="feat/other-task")
    foreign = repo / "fleet-admin" / "other-task.txt"
    foreign.parent.mkdir()
    foreign.write_text("another task's uncommitted work\n", encoding="utf-8")
    _git(repo, "add", "fleet-admin")
    (repo / "tracked_change.txt").write_text("uncommitted tracked edit\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-m", "local-only commit ahead of remote")
    ahead_sha = _git(repo, "rev-parse", "HEAD")
    launcher, store, supervisor = _launcher(tmp_path, probe_open_pr=NO_PR)
    entry = _entry_with_opt_in(store, repo, continue_branch=True)

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None and updated.status == "launched"
    assert len(supervisor.created) == 1
    assert Path(supervisor.created[0]["workspace"]).resolve() == repo.resolve()
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feat/other-task"
    # The tree is exactly as dirty as before: nothing was stashed or moved.
    assert _git(repo, "rev-parse", "HEAD") == ahead_sha
    assert foreign.read_text(encoding="utf-8").startswith("another task's")
    assert (repo / "tracked_change.txt").exists()
    assert "tracked_change.txt" in _git(repo, "status", "--porcelain")
    # No stash was created and no run worktree was spawned.
    assert _git(repo, "stash", "list").strip() == ""
    assert supervisor.created[0]["workspace_base_mode"] == "continuation"


def test_continuation_named_branch_mismatch_blocks_retryably(tmp_path: Path) -> None:
    """A continuation entry naming a branch the workspace is not on is blocked.

    The guard must not silently launch onto the wrong branch; like every
    guard refusal (task 201 deliverable 3) the block is retryable: the entry
    stays pending with the reason naming both branches.
    """

    repo = _make_repo(tmp_path)
    _feature_branch(repo, name="feat/other-task")
    launcher, store, supervisor = _launcher(tmp_path, probe_open_pr=NO_PR)
    entry = _entry_with_opt_in(store, repo, branch="feat/harness-fleet-report")

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None and updated.status == "pending"
    reason = updated.skip_reasons[-1]
    assert "feat/harness-fleet-report" in reason and "feat/other-task" in reason
    assert len(supervisor.created) == 0
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feat/other-task"


def test_guard_unit_continuation_returns_workspace_unchanged(tmp_path: Path) -> None:
    """Unit: continuation returns the workspace as-is, without resolving origin."""

    repo = _make_repo(tmp_path)
    _feature_branch(repo, name="feat/other-task")
    head_before = _git(repo, "rev-parse", "HEAD")

    outcome = prepare_workspace_base(
        repo,
        run_label="unit-cont",
        probe_open_pr=lambda repo, branch: OPEN_PR,  # would refuse without opt-in
        continuation=True,
        requested_branch="feat/other-task",
    )

    assert outcome.mode == "continuation"
    assert outcome.workspace == repo
    assert outcome.original_branch == "feat/other-task"
    assert outcome.run_branch == "feat/other-task"
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feat/other-task"
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert "base=continuation-branch-as-is" in outcome.summary()


def test_guard_unit_continuation_named_branch_mismatch_is_retryable(tmp_path: Path) -> None:
    """Unit: a requested branch that is not checked out is a loud, retryable block."""

    repo = _make_repo(tmp_path)
    _feature_branch(repo, name="feat/other-task")
    with pytest.raises(WorkspaceBaseError) as excinfo:
        prepare_workspace_base(
            repo,
            run_label="unit-mismatch",
            probe_open_pr=NO_PR,
            continuation=True,
            requested_branch="feat/harness-fleet-report",
        )
    message = str(excinfo.value)
    assert "feat/harness-fleet-report" in message
    assert "feat/other-task" in message
    assert "checked-out branch 'feat/other-task'" in message