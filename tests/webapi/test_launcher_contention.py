from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from lh_harness import workspace_identity as wi
from lh_harness.launcher import Launcher
from lh_harness.queue import QueueStore, default_queue_config


class FakeSupervisor:
    """In-memory supervisor stand-in that records create_run calls."""

    def __init__(self, runs_root: Path) -> None:
        self.runs_root = runs_root
        self._runs: dict[str, dict[str, Any]] = {}
        self._owners: dict[str, dict[str, Any]] = {}
        self._statuses: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []

    def add_run(
        self,
        run_id: str,
        workspace: str | Path,
        *,
        status: str = "running",
        trio: str | None = None,
    ) -> None:
        self._runs[run_id] = {
            "id": run_id,
            "workspace": str(workspace),
            "trio": trio,
        }
        self._owners[run_id] = {
            "run_id": run_id,
            "workspace": str(workspace),
            "task": "existing task",
            "agent": "codex",
        }
        self._statuses[run_id] = {"status": status, "run_id": run_id}

    def list_run_items(self) -> list[dict[str, Any]]:
        return list(self._runs.values())

    def owner(self, run_id: str) -> dict[str, Any]:
        return self._owners.get(run_id, {})

    def status(self, run_id: str) -> dict[str, Any]:
        return self._statuses.get(run_id, {"status": "idle", "run_id": run_id})

    def _run_logs_dir(self, run_id: str) -> Path:
        return self.runs_root / run_id / "lh_harness"

    def create_run(self, **kwargs: Any) -> dict[str, Any]:
        import uuid

        run_id = kwargs.get("run_id") or f"run-{uuid.uuid4().hex[:8]}"
        workspace = str(kwargs.get("workspace", "."))
        agent = kwargs.get("agent", "codex")
        self.add_run(run_id, workspace, status="creating", trio=agent)
        self._owners[run_id].update(
            {
                "task": kwargs.get("task", ""),
                "agent": agent,
                "model": kwargs.get("model"),
                "role_configs": kwargs.get("role_configs"),
                "max_rounds": kwargs.get("max_rounds"),
                "mcp_profile": kwargs.get("mcp_profile"),
            }
        )
        result = {
            "id": run_id,
            "task": kwargs.get("task", ""),
            "status": "creating",
            "log_dir": str(self._run_logs_dir(run_id)),
            "owner": self._owners[run_id],
        }
        self.created.append(result)
        return result


def _fixture(tmp_path: Path) -> tuple[Path, QueueStore, FakeSupervisor]:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = FakeSupervisor(root)
    return root, store, supervisor


def _base_entry(trio: str = "kimi", priority: int = 0, workspace: str = "./workspace") -> dict[str, Any]:
    return {
        "name": "task",
        "task": "do something",
        "workspace": workspace,
        "trio": trio,
        "priority": priority,
        "requested_by": "ci",
    }


def _git_init(path: Path, *, bare: bool = False) -> None:
    args = ["git", "init", "-q"]
    if bare:
        args.append("--bare")
    args.append(str(path))
    subprocess.run(args, check=True, capture_output=True)


def _set_remote(repo: Path, url: str) -> None:
    exists = subprocess.run(
        ["git", "-C", str(repo), "remote"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subcommand = "set-url" if "origin" in exists.splitlines() else "add"
    subprocess.run(
        ["git", "-C", str(repo), "remote", subcommand, "origin", url],
        check=True,
        capture_output=True,
    )


def test_contention_emits_event_per_member_and_persists_json(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    _git_init(repo_a)
    _git_init(repo_b)
    _set_remote(repo_a, "https://github.com/org/repo")
    _set_remote(repo_b, "https://github.com/org/repo")
    subprocess.run(["git", "-C", str(repo_a), "checkout", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(repo_b), "checkout", "-q", "-b", "main"], check=True)

    supervisor.add_run("run-a", str(repo_a), status="running")
    supervisor.add_run("run-b", str(repo_b), status="running")

    launcher = Launcher(supervisor, store, queue_config=default_queue_config())
    asyncio.run(launcher.tick())

    contention_path = root / "queue" / "contention.json"
    assert contention_path.is_file()
    data = json.loads(contention_path.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert data["available"] is True
    assert len(data["contentions"]) == 1
    group = data["contentions"][0]
    assert group["severity"] == "same_repo_same_branch"
    assert {m["run_id"] for m in group["members"]} == {"run-a", "run-b"}

    # One event per member, stamped with that member's own run_id.
    for run_id in ("run-a", "run-b"):
        events_path = supervisor._run_logs_dir(run_id) / "role_orchestration" / "events.jsonl"
        lines = events_path.read_text(encoding="utf-8").splitlines()
        matching = [line for line in lines if "fleet.contention.detected" in line]
        assert len(matching) == 1, f"expected one detected event for {run_id}"
        record = json.loads(matching[0])
        assert record["run_id"] == run_id
        peers = record["payload"]["peers"]
        assert all(peer["run_id"] != run_id for peer in peers)


def test_contention_is_warn_only_and_does_not_block_launch(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    _set_remote(repo, "https://github.com/org/repo")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "main"], check=True)

    supervisor.add_run("run-a", str(repo), status="running")

    entry = store.create(_base_entry(trio="kimi", workspace=str(repo)))
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 3
    launcher = Launcher(supervisor, store, queue_config=config)
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    # The exact-path eligibility still skips, but that is pre-existing behaviour.
    # The important invariant is that contention never adds a new skip reason.
    assert not any(reason.startswith("contention") for reason in updated.skip_reasons)


def test_contention_cleared_on_next_tick(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    _git_init(repo_a)
    _git_init(repo_b)

    supervisor.add_run("run-a", str(repo_a), status="running")
    supervisor.add_run("run-b", str(repo_b), status="running")

    launcher = Launcher(supervisor, store, queue_config=default_queue_config())
    asyncio.run(launcher.tick())
    assert len(launcher._contentions) == 0

    # Make repo_b share the same repo id as repo_a.  Clear the identity cache
    # so the new remotes are observed on the next tick.
    _set_remote(repo_a, "https://github.com/org/repo")
    _set_remote(repo_b, "https://github.com/org/repo")
    wi.clear_cache()
    asyncio.run(launcher.tick())
    assert len(launcher._contentions) == 1

    # Change repo_b to a different repo id.
    _set_remote(repo_b, "https://github.com/org/other")
    wi.clear_cache()
    asyncio.run(launcher.tick())
    assert len(launcher._contentions) == 0

    # A cleared event should have been emitted for run-a.
    events_path = supervisor._run_logs_dir("run-a") / "role_orchestration" / "events.jsonl"
    lines = events_path.read_text(encoding="utf-8").splitlines()
    assert any("fleet.contention.cleared" in line for line in lines)
