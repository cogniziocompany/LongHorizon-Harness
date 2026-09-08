from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

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


def test_launcher_launches_highest_priority_kimi(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 1
    launcher = Launcher(supervisor, store, queue_config=config)

    low = store.create(_base_entry(trio="kimi", priority=1, workspace="./w2"))
    high = store.create(_base_entry(trio="kimi", priority=10, workspace="./w1"))

    asyncio.run(launcher.tick())

    high_updated = store.get(high.queue_id)
    low_updated = store.get(low.queue_id)
    assert high_updated is not None
    assert low_updated is not None
    assert high_updated.status == "launched"
    assert low_updated.status == "pending"
    assert any("kimi at capacity" in reason for reason in low_updated.skip_reasons)
    assert high_updated.run_id is not None
    assert high_updated.run_id in supervisor._runs

    events_path = supervisor._run_logs_dir(high_updated.run_id) / "role_orchestration" / "events.jsonl"
    assert events_path.is_file()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    assert any("queue.launched" in line for line in lines)
    contention_path = root / "queue" / "contention.json"
    assert contention_path.is_file()


def test_launcher_skips_qwen_at_zero_capacity(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["qwen_max"] = 0
    launcher = Launcher(supervisor, store, queue_config=config)

    entry = store.create(_base_entry(trio="qwen"))
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert any("qwen at capacity" in reason for reason in updated.skip_reasons)

    service_log = root / "queue" / "service_events.jsonl"
    assert service_log.is_file()
    assert any("queue.skipped" in line for line in service_log.read_text().splitlines())


def test_launcher_skips_busy_workspace(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 2
    launcher = Launcher(supervisor, store, queue_config=config)

    busy_workspace = str(tmp_path / "busy")
    free_workspace = str(tmp_path / "free")
    supervisor.add_run("active-1", busy_workspace, status="running")

    blocked = store.create(_base_entry(trio="kimi", workspace=busy_workspace, priority=10))
    ready = store.create(_base_entry(trio="kimi", workspace=free_workspace, priority=5))

    asyncio.run(launcher.tick())

    blocked_updated = store.get(blocked.queue_id)
    ready_updated = store.get(ready.queue_id)
    assert blocked_updated is not None
    assert ready_updated is not None
    assert ready_updated.status == "launched"
    assert blocked_updated.status == "pending"
    assert any("has active run active-1" in reason for reason in blocked_updated.skip_reasons)


def test_launcher_idempotent_across_restarts(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 1
    launcher = Launcher(supervisor, store, queue_config=config)

    first = store.create(_base_entry(trio="kimi", workspace="./ws"))
    launched = store.mark_launched(first.queue_id, "run-existing")
    supervisor.add_run("run-existing", "./ws", status="running", trio="kimi")

    second = store.create(_base_entry(trio="kimi", workspace="./ws2", priority=1))
    asyncio.run(launcher.tick())

    first_updated = store.get(first.queue_id)
    second_updated = store.get(second.queue_id)
    assert first_updated is not None
    assert second_updated is not None
    assert first_updated.status == "launched"
    assert first_updated.run_id == "run-existing"
    # The active run already consumes the only kimi slot.
    assert second_updated.status == "pending"
    assert any("kimi at capacity" in reason for reason in second_updated.skip_reasons)


def test_launcher_never_double_launches_same_workspace(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 3
    launcher = Launcher(supervisor, store, queue_config=config)

    workspace = str(tmp_path / "shared")
    supervisor.add_run("manual-run", workspace, status="running")

    first = store.create(_base_entry(trio="kimi", workspace=workspace, priority=10))
    second = store.create(_base_entry(trio="kimi", workspace=workspace, priority=5))

    asyncio.run(launcher.tick())

    assert store.get(first.queue_id).status == "pending"
    assert store.get(second.queue_id).status == "pending"
    assert len(supervisor.created) == 0


def test_launcher_key_health_gating(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["min_healthy_keys"] = 3

    health_file = tmp_path / "health.json"
    health_file.write_text(
        json.dumps({"healthy_keys": [{"healthy": True}, {"healthy": True}, {"healthy": False}]}),
        encoding="utf-8",
    )
    config["capacity"]["key_health_url"] = health_file.as_uri()

    launcher = Launcher(supervisor, store, queue_config=config)
    entry = store.create(_base_entry(trio="kimi"))
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert any("key health insufficient" in reason for reason in updated.skip_reasons)


def test_launcher_key_health_allows_kimi_when_threshold_met(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["min_healthy_keys"] = 2

    health_file = tmp_path / "health.json"
    health_file.write_text(
        json.dumps({"healthy_keys": [{"healthy": True}, {"healthy": True}, {"healthy": False}]}),
        encoding="utf-8",
    )
    config["capacity"]["key_health_url"] = health_file.as_uri()

    launcher = Launcher(supervisor, store, queue_config=config)
    entry = store.create(_base_entry(trio="kimi"))
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "launched"


def test_launcher_records_last_checked_at(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    launcher = Launcher(supervisor, store, queue_config=config)

    entry = store.create(_base_entry(trio="kimi"))
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.last_checked_at is not None


def test_launcher_fails_entry_when_create_run_raises(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    launcher = Launcher(supervisor, store, queue_config=config)

    class BrokenSupervisor(FakeSupervisor):
        def create_run(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("boom")

    broken = BrokenSupervisor(root)
    launcher.supervisor = broken
    entry = store.create(_base_entry(trio="kimi"))
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "failed"
    assert "launch failed" in (updated.reason or "")


def test_launcher_no_double_launch_across_ticks(tmp_path: Path) -> None:
    """A second tick must not launch into a workspace that got an active run between ticks.

    This covers the inter-tick race where a concurrent manual run or the previous
    tick's launched worker becomes active before the next poll finishes.  The
    launcher re-evaluates the active-run set inside its serialized launch section
    and skips the workspace.
    """
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 3
    launcher = Launcher(supervisor, store, queue_config=config)

    workspace = str(tmp_path / "shared")
    first = store.create(_base_entry(trio="kimi", workspace=workspace, priority=10))

    asyncio.run(launcher.tick())
    first_updated = store.get(first.queue_id)
    assert first_updated is not None
    assert first_updated.status == "launched"
    assert first_updated.run_id is not None

    # Simulate a manual/concurrent run appearing in the same workspace before the
    # next launcher tick, in addition to the already-launched queue entry.
    supervisor.add_run("manual-run", workspace, status="running", trio="kimi")

    second = store.create(_base_entry(trio="kimi", workspace=workspace, priority=5))
    asyncio.run(launcher.tick())

    second_updated = store.get(second.queue_id)
    assert second_updated is not None
    assert second_updated.status == "pending"
    assert any("has active run" in reason for reason in second_updated.skip_reasons)
