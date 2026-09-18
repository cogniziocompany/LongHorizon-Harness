"""Workspace branch guard on ``POST /api/runs`` (task 200).

Task 195 wired :func:`prepare_workspace_base` into the queue ``Launcher`` only,
but the fleet creates every real run through ``POST /api/runs`` — so none of
those launches were guarded.  These tests prove the create path obeys the same
contract: a run is never created on another task's branch as-is, another
task's leftover work is never destroyed, and a refused base fails loudly as
409 without creating a run row.

Every git-path behaviour is proven against throwaway repos built under
``tmp_path`` (bare origin + clone) — never against a live workspace.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.queue import QueueStore
from lh_harness.webapi.server import create_app

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "guard-test",
    "GIT_AUTHOR_EMAIL": "guard-test@example.invalid",
    "GIT_COMMITTER_NAME": "guard-test",
    "GIT_COMMITTER_EMAIL": "guard-test@example.invalid",
    "HOME": os.environ.get("HOME", "/tmp"),
}

NO_PR = lambda repo, branch: None


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


def _feature_branch(repo: Path, name: str = "feat/other-task", *, push: bool = True) -> None:
    _git(repo, "checkout", "-q", "-b", name)
    (repo / "feature.txt").write_text(f"feature work {uuid.uuid4().hex[:6]}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feature work")
    if push:
        _git(repo, "push", "-q", "-u", "origin", name)


def _tree_digest(root: Path) -> dict[str, str]:
    """Byte fingerprint of every working-tree file (git internals excluded).

    ``byte-identical`` in the guard's hard requirement means: the prepare step
    changes the bytes of no file in the original tree.  ``.git`` internals
    necessarily change when a linked worktree is registered, so they are
    excluded; every tracked, untracked and modified file under the workspace
    itself must match before and after.
    """

    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        digest[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


class FakeSupervisor:
    """In-memory supervisor stand-in that records create_run calls."""

    def __init__(self, runs_root: Path, workspace_root: Path) -> None:
        self.runs_root = runs_root
        self.workspace_root = workspace_root
        self.created: list[dict[str, Any]] = []

    def list_run_items(self) -> list[dict[str, Any]]:
        return []

    def owner(self, run_id: str) -> dict[str, Any]:
        return {}

    def status(self, run_id: str) -> dict[str, Any]:
        return {"status": "idle", "run_id": run_id}

    def _run_logs_dir(self, run_id: str) -> Path:
        return self.runs_root / run_id / "lh_harness"

    def create_run(self, **kwargs: Any) -> dict[str, Any]:
        result = {"id": f"run-{len(self.created) + 1}", **kwargs}
        self.created.append(result)
        return result


def _client(tmp_path: Path, probe_open_pr: Any = NO_PR) -> tuple[TestClient, FakeSupervisor, Path]:
    root = tmp_path / "runs"
    root.mkdir(parents=True, exist_ok=True)
    supervisor = FakeSupervisor(root, workspace_root=tmp_path)
    app = create_app(runs_root=root, supervisor=supervisor, probe_open_pr=probe_open_pr)
    return TestClient(app), supervisor, root


def _run_events(root: Path, run_id: str) -> str:
    path = root / run_id / "lh_harness" / "role_orchestration" / "events.jsonl"
    return path.read_text(encoding="utf-8")


def test_clean_default_branch_creates_run_unchanged_with_base_summary(tmp_path: Path) -> None:
    """A clean workspace on the default branch creates the run in place."""

    repo = _make_repo(tmp_path, name="ws-clean")
    client, supervisor, root = _client(tmp_path)

    response = client.post(
        "/api/runs",
        json={"task": "guard me", "workspace": str(repo), "max_rounds": 2},
    )

    assert response.status_code == 200
    assert len(supervisor.created) == 1
    assert Path(supervisor.created[0]["workspace"]).resolve() == repo.resolve()
    # The workspace was not touched: still on the default branch, still clean.
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(repo, "status", "--porcelain").strip() == ""
    # The run-created event carries the guard's base summary.
    events = _run_events(root, response.json()["run"]["id"])
    assert "run.created" in events
    assert '"workspace_base": "mode=on-default' in events
    assert "checked-out-branch='main'" in events


def test_dirty_foreign_branch_creates_run_in_worktree_tree_byte_identical(tmp_path: Path) -> None:
    """Foreign uncommitted work is preserved; the run starts in a fresh worktree."""

    repo = _make_repo(tmp_path, name="ws-dirty")
    _feature_branch(repo, push=False)  # unpushed commit on a foreign branch
    foreign_uncommitted = repo / "fleet-admin" / "other-task.txt"
    foreign_uncommitted.parent.mkdir()
    foreign_uncommitted.write_text("another task's uncommitted work\n", encoding="utf-8")
    _git(repo, "add", "fleet-admin")
    (repo / "tracked_change.txt").write_text("uncommitted tracked edit\n", encoding="utf-8")
    ahead_sha = _git(repo, "rev-parse", "HEAD")
    before = _tree_digest(repo)

    client, supervisor, root = _client(tmp_path)
    response = client.post(
        "/api/runs",
        json={"task": "guard me", "workspace": str(repo), "max_rounds": 2},
    )

    assert response.status_code == 200
    assert len(supervisor.created) == 1
    # The run executes in a fresh linked worktree branched from origin/main...
    run_workspace = Path(supervisor.created[0]["workspace"])
    assert run_workspace != repo
    assert run_workspace.is_dir()
    assert (run_workspace / ".git").is_file()  # linked-worktree marker
    assert _git(run_workspace, "rev-parse", "--abbrev-ref", "HEAD").startswith("lh-run/")
    assert _git(run_workspace, "rev-parse", "HEAD") == _git(
        repo, "rev-parse", "refs/remotes/origin/main"
    )
    assert not list(run_workspace.glob("fleet-admin/**"))
    # ...while the original tree is byte-identical: same branch, same HEAD,
    # same dirty files, every file's bytes unchanged by the prepare step.
    assert _tree_digest(repo) == before
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feat/other-task"
    assert _git(repo, "rev-parse", "HEAD") == ahead_sha
    assert foreign_uncommitted.read_text(encoding="utf-8").startswith("another task's")
    assert (repo / "tracked_change.txt").exists()
    assert _git(repo, "status", "--porcelain").strip() != ""
    # The run-created event names the worktree base and the untouched branch.
    events = _run_events(root, response.json()["run"]["id"])
    assert '"workspace_base": "mode=worktree' in events
    assert "checked-out-branch='feat/other-task'" in events
    assert f"run-branch='{_git(run_workspace, 'rev-parse', '--abbrev-ref', 'HEAD')}'" in events


def test_refused_base_returns_409_and_creates_no_run_row(tmp_path: Path) -> None:
    """A colliding OPEN PR refuses loudly: HTTP 409 and no run row."""

    repo = _make_repo(tmp_path, name="ws-refused")
    _feature_branch(repo)
    client, supervisor, root = _client(
        tmp_path, probe_open_pr=lambda repo, branch: "#108 'other task work' https://gh.example/pr/108"
    )

    response = client.post(
        "/api/runs",
        json={"task": "guard me", "workspace": str(repo), "max_rounds": 2},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail.startswith("workspace base refused:")
    assert "feat/other-task" in detail
    assert "#108" in detail and "https://gh.example/pr/108" in detail
    assert "refusing to launch" in detail
    # No run row was created and nothing in the workspace moved.
    assert supervisor.created == []
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feat/other-task"
    # The guard cut no run branch: the only branches are the pre-existing ones.
    assert set(_git(repo, "branch", "--format=%(refname:short)").splitlines()) == {"main", "feat/other-task"}
    # Only the main worktree is registered; the guard created no run worktree.
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_unresolvable_base_also_returns_409_without_a_run(tmp_path: Path) -> None:
    """No resolvable clean base is a refusal too — even with the probe off."""

    repo = _make_repo(tmp_path, name="ws-unresolvable")
    _feature_branch(repo)
    # Point origin at an empty bare remote: no refs, so no default branch can
    # be resolved after the guard's fetch.
    empty = tmp_path / "empty.origin.git"
    subprocess.run(["git", "init", "--bare", str(empty)], check=True, capture_output=True, env=GIT_ENV)
    _git(repo, "remote", "set-url", "origin", str(empty))
    client, supervisor, root = _client(tmp_path)

    response = client.post(
        "/api/runs",
        json={"task": "guard me", "workspace": str(repo), "max_rounds": 2},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "feat/other-task" in detail
    assert "refusing to launch" in detail
    assert supervisor.created == []
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feat/other-task"


def test_probe_open_pr_disabled_path_proceeds_without_pr_check(tmp_path: Path) -> None:
    """``probe_open_pr=None`` disables the PR probe: no refusal, run proceeds.

    The control case first proves the very same state refuses with a probe
    enabled, so the pass-through is attributable to the disabled probe.
    """

    # Control: the same foreign-branch state with a PR probe enabled refuses.
    probed = _make_repo(tmp_path, name="ws-probed")
    _feature_branch(probed)
    client_probed, supervisor_probed, _ = _client(
        tmp_path, probe_open_pr=lambda repo, branch: "#154 'MCP namespace contract' https://gh.example/pr/154"
    )
    refused = client_probed.post(
        "/api/runs",
        json={"task": "guard me", "workspace": str(probed), "max_rounds": 2},
    )
    assert refused.status_code == 409
    assert "#154" in refused.json()["detail"]
    assert supervisor_probed.created == []

    # Disabled probe: the identical request proceeds.  The branch is clean and
    # synced with origin, so the guard still refuses to run on it as-is and
    # cuts a fresh run branch from origin/main in place.
    disabled = _make_repo(tmp_path, name="ws-disabled")
    _feature_branch(disabled)
    client_disabled, supervisor_disabled, root = _client(tmp_path, probe_open_pr=None)
    allowed = client_disabled.post(
        "/api/runs",
        json={"task": "guard me", "workspace": str(disabled), "max_rounds": 2},
    )
    assert allowed.status_code == 200
    assert len(supervisor_disabled.created) == 1
    assert Path(supervisor_disabled.created[0]["workspace"]).resolve() == disabled.resolve()
    assert _git(disabled, "rev-parse", "--abbrev-ref", "HEAD").startswith("lh-run/")
    assert _git(disabled, "rev-parse", "HEAD") == _git(
        disabled, "rev-parse", "refs/remotes/origin/main"
    )
    events = _run_events(root, allowed.json()["run"]["id"])
    assert '"workspace_base": "mode=in-place' in events


def test_missing_workspace_skips_the_guard_and_creates_the_run(tmp_path: Path) -> None:
    """No explicit workspace: the supervisor uses its configured root as before."""

    client, supervisor, root = _client(tmp_path)

    response = client.post("/api/runs", json={"task": "no workspace", "max_rounds": 2})

    assert response.status_code == 200
    assert len(supervisor.created) == 1
    assert supervisor.created[0]["workspace"] is None
    # Without an explicit workspace there is no repo to guard and no base to
    # record: the run-created provenance event is not emitted.
    run_id = response.json()["run"]["id"]
    events_path = root / run_id / "lh_harness" / "role_orchestration" / "events.jsonl"
    assert not events_path.exists()


def test_create_app_default_probes_open_pr(tmp_path: Path) -> None:
    """Default-on wiring: create_app without the hook uses the real gh probe."""

    from lh_harness.workspace_guard import probe_open_pr_gh

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    supervisor = FakeSupervisor(root, workspace_root=tmp_path)
    app = create_app(runs_root=root, supervisor=supervisor, probe_open_pr=probe_open_pr_gh)
    launcher = getattr(app.state, "launcher", None)
    assert launcher is not None
    assert launcher.probe_open_pr is probe_open_pr_gh