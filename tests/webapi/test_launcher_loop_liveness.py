"""Launch-loop liveness watchdog (easybutt0n-61).

The service process can outlive its launch task.  The lease remains an exclusion
record, never an independent heartbeat: only real passes refresh it.  A stopped
or stuck loop produces a critical log and a durable service event, and /api/meta
exposes the last completed pass separately from the lease holder.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any, Callable

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lh_harness.launcher import Launcher  # noqa: E402
from lh_harness.queue import (  # noqa: E402
    QueueStore,
    default_queue_config,
    read_lease,
)
from lh_harness.webapi.server import create_app  # noqa: E402

from .test_launcher import FakeSupervisor  # noqa: E402


class ApiSupervisor(FakeSupervisor):
    def shutdown(self) -> None:
        return None


def _fixture(tmp_path: Path, *, poll_seconds: float = 0.01) -> tuple[Path, QueueStore, Launcher]:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    config = default_queue_config()
    config["capacity"]["poll_seconds"] = poll_seconds
    launcher = Launcher(ApiSupervisor(root), store, queue_config=config)
    return root, store, launcher


async def _wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        await asyncio.sleep(0.005)


def _events(root: Path) -> list[dict[str, Any]]:
    path = root / "queue" / "service_events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_loop_death_is_loud_and_nothing_else_refreshes_lease(
    tmp_path: Path, caplog: Any
) -> None:
    root, _store, launcher = _fixture(tmp_path)

    async def exercise() -> None:
        await launcher.start()
        await _wait_for(lambda: launcher.last_pass_at is not None)
        task = launcher._task
        assert task is not None
        task.cancel()
        await _wait_for(lambda: launcher.loop_failure is not None)

        stopped_lease = read_lease(root)
        assert stopped_lease is not None
        await asyncio.sleep(0.06)  # six poll intervals, past the stale horizon
        assert read_lease(root) == stopped_lease

        # The dead task remains fenced: start() cannot create a second launcher.
        await launcher.start()
        assert launcher._task is task
        await launcher.stop()

    with caplog.at_level(logging.CRITICAL, logger="lh_harness.launcher"):
        asyncio.run(exercise())

    assert "cancelled unexpectedly" in str(launcher.loop_failure)
    assert any("launcher loop watchdog" in record.message for record in caplog.records)
    stopped = [event for event in _events(root) if event.get("type") == "launcher.loop_stopped"]
    assert len(stopped) == 1
    assert stopped[0]["payload"]["last_pass_at"] == launcher.last_pass_at


def test_watchdog_detects_idle_loop_without_task_exit(tmp_path: Path) -> None:
    root, _store, launcher = _fixture(tmp_path)

    async def exercise() -> None:
        async def idle_after_one_pass() -> None:
            await launcher.tick()
            while not launcher._stopping:
                await asyncio.sleep(0.01)

        launcher._loop = idle_after_one_pass  # type: ignore[method-assign]
        await launcher.start()
        await _wait_for(lambda: launcher.loop_failure is not None)

        task = launcher._task
        assert task is not None and not task.done()
        assert "no launch pass completed" in str(launcher.loop_failure)
        stopped_lease = read_lease(root)
        await asyncio.sleep(0.04)
        assert read_lease(root) == stopped_lease
        await launcher.stop()

    asyncio.run(exercise())

    stopped = [event for event in _events(root) if event.get("type") == "launcher.loop_stopped"]
    assert len(stopped) == 1
    assert "no launch pass completed" in stopped[0]["payload"]["reason"]


def test_meta_exposes_last_pass_and_lease_holder(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    app = create_app(runs_root=root, supervisor=ApiSupervisor(root))
    launcher = app.state.launcher
    assert launcher is not None

    asyncio.run(launcher.tick())
    meta = TestClient(app).get("/api/meta").json()

    assert meta["launcher_last_pass_at"] == launcher.last_pass_at
    assert meta["launcher_last_pass_at"] is not None
    assert meta["launcher_loop_failure"] is None
    assert meta["lease_holder"] == {
        "pid": os.getpid(),
        "host": socket.gethostname(),
    }
