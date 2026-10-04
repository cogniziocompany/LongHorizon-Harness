"""Per-task wall-clock limit (task fc-H2).

Covered:
- ``time_limit_minutes`` validation on enqueue (0, 1441, non-int rejected;
  1, 1440 and omitted accepted) and its file-store round trip;
- ``[queue] default_time_limit_minutes`` config validation;
- the launcher stops an expired active run, emits ``run.time_limit_exceeded``,
  records cause ``time_limit`` and, once the run is terminal, fails the entry
  without a requeue; unexpired / unlimited runs are untouched; the config
  default applies to entries without their own limit;
- ``POST /api/runs/{run_id}/time_limit``: update, clear, 404, rationale and
  value validation, and the ``time_limit`` object on ``/latest``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from lh_harness.config import ProjectConfigError, _flatten_queue_table
from lh_harness.launcher import Launcher
from lh_harness.queue import QueueStore, queue_config_from_config

_BODY = {
    "name": "limited task",
    "task": "do the thing",
    "workspace": "./workspace",
    "max_rounds": 5,
    "trio": "kimi",
    "priority": 0,
    "requested_by": "ci",
}

_CONFIG = {
    "trios": {"kimi": {"agent": "codex", "model": None, "mcp_profile": None}},
    "capacity": {"kimi_max": 1, "qwen_max": 1, "poll_seconds": 1, "max_retries": 2},
}


def _store(tmp_path: Path) -> tuple[QueueStore, Path]:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    return QueueStore(root), root


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, 1441, -5, "30", 1.5, True])
def test_enqueue_rejects_bad_time_limit(tmp_path: Path, bad: Any) -> None:
    store, _ = _store(tmp_path)
    with pytest.raises(ValueError, match="time_limit_minutes"):
        store.create({**_BODY, "time_limit_minutes": bad})


@pytest.mark.parametrize("good", [1, 60, 1440])
def test_enqueue_accepts_and_persists_time_limit(tmp_path: Path, good: int) -> None:
    store, root = _store(tmp_path)
    entry = store.create({**_BODY, "time_limit_minutes": good})
    assert entry.time_limit_minutes == good
    assert QueueStore(root).get(entry.queue_id).time_limit_minutes == good


def test_enqueue_without_time_limit_is_unlimited(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    entry = store.create(dict(_BODY))
    assert entry.time_limit_minutes is None


def test_requeue_carries_the_limit(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    entry = store.create({**_BODY, "time_limit_minutes": 45})
    store.mark_failed(entry.queue_id, "provider_rate_limit")
    successor = store.requeue(entry.queue_id, "provider_rate_limit")
    assert successor is not None and successor.time_limit_minutes == 45


def test_config_default_time_limit_validation() -> None:
    assert _flatten_queue_table({})["default_time_limit_minutes"] is None
    assert _flatten_queue_table({"default_time_limit_minutes": 90})["default_time_limit_minutes"] == 90
    for bad in (0, 1441, "90", 2.5, True):
        with pytest.raises(ProjectConfigError, match="default_time_limit_minutes"):
            _flatten_queue_table({"default_time_limit_minutes": bad})
    flattened = _flatten_queue_table({"default_time_limit_minutes": 30})
    assert queue_config_from_config({"queue": flattened})["default_time_limit_minutes"] == 30
    assert queue_config_from_config({})["default_time_limit_minutes"] is None


# --- launcher ----------------------------------------------------------------


class _Supervisor:
    """Supervisor stand-in: one run whose status the test controls."""

    def __init__(self, runs_root: Path) -> None:
        self.runs_root = runs_root
        self.statuses: dict[str, str] = {}
        self.stopped: list[str] = []

    def list_run_items(self) -> list[dict[str, Any]]:
        return [{"id": run_id, "status": status} for run_id, status in self.statuses.items()]

    def status(self, run_id: str) -> dict[str, Any]:
        return {"status": self.statuses.get(run_id, "idle"), "run_id": run_id}

    def owner(self, run_id: str) -> dict[str, Any]:
        return {}

    def stop(self, run_id: str) -> dict[str, Any]:
        self.stopped.append(run_id)
        return {"status": "stopping"}

    def _run_logs_dir(self, run_id: str) -> Path:
        return self.runs_root / run_id / "lh_harness"


def _launched(store: QueueStore, sup: _Supervisor, run_id: str, *, minutes_ago: float, limit: int | None) -> str:
    body = dict(_BODY)
    if limit is not None:
        body["time_limit_minutes"] = limit
    entry = store.create(body)
    store.mark_launched(entry.queue_id, run_id)
    launched = store.get(entry.queue_id)
    launched.launched_at = time.time() - minutes_ago * 60
    store.update(launched)
    sup.statuses[run_id] = "running"
    (sup.runs_root / run_id / "lh_harness" / "role_orchestration").mkdir(parents=True)
    return entry.queue_id


def _run_events(root: Path, run_id: str) -> list[dict[str, Any]]:
    path = root / run_id / "lh_harness" / "role_orchestration" / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_launcher_stops_expired_run_and_fails_it_without_requeue(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    sup = _Supervisor(root)
    queue_id = _launched(store, sup, "run-late", minutes_ago=31, limit=30)
    launcher = Launcher(sup, store, queue_config=_CONFIG, probe_open_pr=None)

    asyncio.run(launcher.tick())

    assert sup.stopped == ["run-late"]
    events = [e for e in _run_events(root, "run-late") if e["type"] == "run.time_limit_exceeded"]
    assert len(events) == 1
    assert events[0]["payload"]["limit_minutes"] == 30
    assert events[0]["payload"]["elapsed_minutes"] >= 31
    entry = store.get(queue_id)
    assert entry.status == "launched"
    assert entry.failure_cause == "time_limit"

    # A second pass while the run is still winding down does not stop it again.
    asyncio.run(launcher.tick())
    assert sup.stopped == ["run-late"]

    # The run goes terminal: the entry fails with the time_limit cause and
    # no successor is created.
    sup.statuses["run-late"] = "stopped"
    asyncio.run(launcher.tick())
    entry = store.get(queue_id)
    assert entry.status == "failed"
    assert (entry.reason or "").startswith("time_limit")
    assert [e for e in store.list() if e.retry_of == queue_id] == []
    assert store.counts().get("pending", 0) == 0


def test_launcher_leaves_unexpired_and_unlimited_runs_alone(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    sup = _Supervisor(root)
    fresh = _launched(store, sup, "run-fresh", minutes_ago=10, limit=30)
    unlimited = _launched(store, sup, "run-open", minutes_ago=600, limit=None)
    launcher = Launcher(sup, store, queue_config=_CONFIG, probe_open_pr=None)

    asyncio.run(launcher.tick())

    assert sup.stopped == []
    for queue_id in (fresh, unlimited):
        entry = store.get(queue_id)
        assert entry.status == "launched"
        assert entry.failure_cause is None
    assert not any(e["type"] == "run.time_limit_exceeded" for e in _run_events(root, "run-fresh"))


def test_launcher_applies_config_default_limit(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    sup = _Supervisor(root)
    queue_id = _launched(store, sup, "run-default", minutes_ago=61, limit=None)
    own = _launched(store, sup, "run-own", minutes_ago=61, limit=120)
    launcher = Launcher(
        sup, store, queue_config={**_CONFIG, "default_time_limit_minutes": 60}, probe_open_pr=None
    )

    asyncio.run(launcher.tick())

    assert sup.stopped == ["run-default"]  # the entry's own 120 min wins over the default
    assert store.get(queue_id).failure_cause == "time_limit"
    assert store.get(own).failure_cause is None


# --- REST route --------------------------------------------------------------


def _app(tmp_path: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from lh_harness.dashboard.state import DashboardState
    from lh_harness.supervisor.service import RunSupervisor
    from lh_harness.webapi.server import create_app

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    for run_id in ("run-a", "run-b"):
        control = root / run_id / "control"
        control.mkdir(parents=True)
        (control / "status.json").write_text(json.dumps({"status": "running", "mtime": 1.0}), encoding="utf-8")
        (control / "owner.json").write_text(
            json.dumps({"task": "t", "workspace": "/tmp/ws", "agent": "codex"}), encoding="utf-8"
        )
        logs = root / run_id / "lh_harness" / "role_orchestration"
        logs.mkdir(parents=True)
        (logs / "events.jsonl").write_text("", encoding="utf-8")
    supervisor = RunSupervisor(root, workspace_root=tmp_path)
    dashboard = DashboardState(root / "run-a" / "lh_harness", runs_root=root, control_enabled=False)
    app = create_app(state=dashboard, runs_root=root, run_id="run-a", supervisor=supervisor)
    store = QueueStore(root, {})
    entry = store.create({**_BODY, "time_limit_minutes": 30})
    store.mark_launched(entry.queue_id, "run-a")
    return TestClient(app), store, entry.queue_id, root


def test_time_limit_route_updates_and_clears(tmp_path: Path) -> None:
    client, store, queue_id, root = _app(tmp_path)

    response = client.post(
        "/api/runs/run-a/time_limit", json={"minutes": 90, "rationale": "needs one more audit round"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["queue_id"] == queue_id
    assert body["time_limit_minutes"] == 90
    assert body["previous_minutes"] == 30
    assert body["time_limit"]["minutes"] == 90
    assert store.get(queue_id).time_limit_minutes == 90
    events = [e for e in _run_events(root, "run-a") if e["type"] == "run.time_limit_set"]
    assert events and events[-1]["payload"]["rationale"] == "needs one more audit round"

    latest = client.get("/api/runs/run-a/latest").json()["time_limit"]
    assert latest["minutes"] == 90
    assert latest["expires_at"] == pytest.approx(store.get(queue_id).launched_at + 90 * 60)
    assert 0 < latest["remaining_seconds"] <= 90 * 60

    cleared = client.post(
        "/api/runs/run-a/time_limit", json={"minutes": None, "rationale": "operator lifted the cap"}
    )
    assert cleared.status_code == 200
    assert cleared.json()["time_limit_minutes"] is None
    assert store.get(queue_id).time_limit_minutes is None
    assert client.get("/api/runs/run-a/latest").json()["time_limit"] is None


def test_time_limit_route_404_without_queue_entry(tmp_path: Path) -> None:
    client, *_ = _app(tmp_path)
    response = client.post(
        "/api/runs/run-b/time_limit", json={"minutes": 30, "rationale": "a long enough reason"}
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"minutes": 30},
        {"minutes": 30, "rationale": "too short"},
        {"minutes": 30, "rationale": 12345678901},
        {"minutes": 0, "rationale": "a long enough reason"},
        {"minutes": 1441, "rationale": "a long enough reason"},
        {"minutes": "30", "rationale": "a long enough reason"},
        {"rationale": "a long enough reason"},
    ],
)
def test_time_limit_route_validates_body(tmp_path: Path, body: dict[str, Any]) -> None:
    client, store, queue_id, _ = _app(tmp_path)
    response = client.post("/api/runs/run-a/time_limit", json=body)
    assert response.status_code == 422
    assert store.get(queue_id).time_limit_minutes == 30
