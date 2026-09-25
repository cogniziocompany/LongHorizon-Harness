"""Launcher stall detector (TASK 230, fix 2).

A cycle is "stalled" when at least one pending entry is eligible (capacity
available, workspace free, key health OK) but nothing successfully launches.
After ``LH_HARNESS_LAUNCHER_STALL_CYCLES`` consecutive stalled cycles the
launcher must go loud with three observable signals:

1. a ``launcher.stalled`` service event in the queue event stream;
2. a WARNING-level log line (shipped to Seq when seq logging is installed);
3. a ``launcher_stalled`` flag surfaced by ``/api/meta``.

Any successful launch resets the counter and clears the flag.  Cycles with no
eligible entry at all are idle, not stalled, and must not count.

The refusal fixture mirrors the workspace-guard tests: throwaway bare origin +
clone repos built under ``tmp_path`` — never a live workspace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from lh_harness.launcher import Launcher  # noqa: E402
from lh_harness.queue import QueueStore, default_queue_config  # noqa: E402
from lh_harness.webapi.server import create_app  # noqa: E402

from .test_launcher import FakeSupervisor  # noqa: E402
from .test_launcher_workspace_guard import (  # noqa: E402
    _feature_branch,
    _make_repo,
)

_STALL_ENV = "LH_HARNESS_LAUNCHER_STALL_CYCLES"


class StallSupervisor(FakeSupervisor):
    """FakeSupervisor plus the shutdown hook create_app registers."""

    def shutdown(self) -> None:  # pragma: no cover - event handler, not run
        return None


def _service_events(root: Path) -> list[dict[str, Any]]:
    path = root / "queue" / "service_events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _service_types(root: Path) -> list[str]:
    return [ev.get("type") for ev in _service_events(root)]


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


@pytest.fixture(autouse=True)
def _isolate_stall_env() -> None:
    """Reset the stall-detector env var around each test."""

    original = os.environ.get(_STALL_ENV)
    os.environ.pop(_STALL_ENV, None)
    yield
    if original is None:
        os.environ.pop(_STALL_ENV, None)
    else:
        os.environ[_STALL_ENV] = original


def _stalled_app(tmp_path: Path, cycles: int) -> tuple[FastAPI, QueueStore, Launcher, Any, Path]:
    """An API app whose launcher's head entry is refused every cycle.

    The workspace sits on a branch carrying another task's OPEN PR (the
    cutover-168 incident shape), so every tick: the entry is pending, the
    eligibility gate passes, ``prepare_workspace_base`` refuses, and nothing
    launches.  The app is the real ``create_app`` wiring — the launcher
    under test is the one the app itself built.
    """

    repo = _make_repo(tmp_path, name="ws-stall")
    _feature_branch(repo)
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    # The threshold is read at Launcher construction, so the env var must be
    # set before create_app builds the app's launcher.
    os.environ[_STALL_ENV] = str(cycles)
    app = create_app(runs_root=root, supervisor=StallSupervisor(root))
    store = app.state.queue_store
    launcher = app.state.launcher
    launcher.probe_open_pr = lambda repo, branch: "#48 'other task PR' https://gh.example/pr/48"
    head = _entry(store, repo)
    return app, store, launcher, head, repo


def test_stall_detector_does_not_fire_before_threshold(tmp_path: Path) -> None:
    """N-1 stalled cycles must stay silent on all three signals."""

    app, store, launcher, head, repo = _stalled_app(tmp_path, cycles=3)
    root = store.runs_root

    for _ in range(2):
        asyncio.run(launcher.tick())

    assert launcher.stall_cycles == 2
    assert launcher.stall_fired is False
    assert "launcher.stalled" not in [ev.get("type") for ev in _service_events(root)]


def test_stall_detector_fires_at_threshold_with_all_three_signals(tmp_path: Path, caplog: Any) -> None:
    """At N stalled cycles the event, the Seq log line, and the meta flag appear."""

    app, store, launcher, head, repo = _stalled_app(tmp_path, cycles=2)
    root = store.runs_root

    with caplog.at_level(logging.WARNING, logger="lh_harness.launcher"):
        for _ in range(2):
            asyncio.run(launcher.tick())

    # Signal 1 — loud service event, same stream as queue.skipped.
    stalled = [ev for ev in _service_events(root) if ev.get("type") == "launcher.stalled"]
    assert len(stalled) == 1
    payload = stalled[0]["payload"]
    assert payload["stall_cycles"] == 2
    assert payload["threshold"] == 2
    assert "consecutive cycles" in payload["message"]

    # Signal 2 — WARNING log line; seq_logging ships it to Seq when configured.
    assert any(
        "launcher stalled" in record.message and "threshold=2" in record.message
        for record in caplog.records
    )

    # Signal 3 — the flag surfaced by the app's own /api/meta endpoint.
    meta = TestClient(app).get("/api/meta").json()
    assert meta["launcher_stalled"] is True
    assert meta["launcher_stall_cycles"] == 2


def test_stall_counter_resets_on_successful_launch(tmp_path: Path) -> None:
    """A successful launch clears the counter and the fired flag."""

    app, store, launcher, head, repo = _stalled_app(tmp_path, cycles=3)
    root = store.runs_root

    asyncio.run(launcher.tick())
    asyncio.run(launcher.tick())
    assert launcher.stall_cycles == 2
    assert launcher.stall_fired is False

    # A second workspace whose entry launches: after fix 1 the refused head
    # falls through to it, so the same cycle ends with a successful launch.
    free_repo = _make_repo(tmp_path, name="ws-free", default_branch="main")
    store.create(
        {
            "name": "task",
            "task": "do something",
            "workspace": str(free_repo),
            "trio": "kimi",
            "priority": 5,
            "requested_by": "ci",
        }
    )
    asyncio.run(launcher.tick())

    assert launcher.stall_cycles == 0
    assert launcher.stall_fired is False
    assert "launcher.stalled" not in [ev.get("type") for ev in _service_events(root)]


def test_stall_detector_does_not_fire_when_no_eligible_entries_exist(tmp_path: Path) -> None:
    """Cycles with no eligible entry (capacity full) are idle, not stalled."""

    repo = _make_repo(tmp_path, name="ws-idle")
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    os.environ[_STALL_ENV] = "2"
    app = create_app(runs_root=root, supervisor=StallSupervisor(root))
    store = app.state.queue_store
    launcher = app.state.launcher
    launcher._config["capacity"]["kimi_max"] = 0  # nothing is launchable
    _entry(store, repo)

    for _ in range(3):
        asyncio.run(launcher.tick())

    assert launcher.stall_cycles == 0
    assert launcher.stall_fired is False
    assert "launcher.stalled" not in [ev.get("type") for ev in _service_events(root)]


def test_stall_event_emitted_once_per_episode(tmp_path: Path, caplog: Any) -> None:
    """Stalled cycles past the threshold keep the flag up without re-emitting."""

    app, store, launcher, head, repo = _stalled_app(tmp_path, cycles=2)
    root = store.runs_root

    with caplog.at_level(logging.WARNING, logger="lh_harness.launcher"):
        for _ in range(5):
            asyncio.run(launcher.tick())

    assert launcher.stall_fired is True
    assert launcher.stall_cycles == 5
    # One loud event per stall episode, not one per cycle.
    assert [ev.get("type") for ev in _service_events(root)].count("launcher.stalled") == 1


def test_stall_flag_clears_on_next_successful_launch(tmp_path: Path) -> None:
    """Fire, then a successful launch: flag cleared, counter reset, no new event."""

    app, store, launcher, head, repo = _stalled_app(tmp_path, cycles=1)
    root = store.runs_root

    asyncio.run(launcher.tick())
    assert launcher.stall_fired is True
    assert [ev.get("type") for ev in _service_events(root)].count("launcher.stalled") == 1

    free_repo = _make_repo(tmp_path, name="ws-recover", default_branch="main")
    store.create(
        {
            "name": "task",
            "task": "do something",
            "workspace": str(free_repo),
            "trio": "kimi",
            "priority": 5,
            "requested_by": "ci",
        }
    )
    asyncio.run(launcher.tick())

    assert launcher.stall_cycles == 0
    assert launcher.stall_fired is False
    # The episode's single event is still the only one.
    assert [ev.get("type") for ev in _service_events(root)].count("launcher.stalled") == 1