"""Maintenance suspend/resume API — fc-H4a.

A deploy drains the queue, then calls POST /api/maintenance/suspend to stop
every ACTIVE run; the stopped runs are parked in
``runs_root/queue/maintenance_manifest.json`` so the launcher never reconciles
their queue entries to failed.  POST /api/maintenance/resume puts each parked
run back with ``mode="continue"`` and deletes the manifest once every run is
active again.  The current manifest is visible at GET /api/maintenance.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.launcher import Launcher
from lh_harness.queue import QueueStore, default_queue_config
from lh_harness.supervisor.lifecycle import TERMINAL_STATUSES, canonical_lifecycle_status
from lh_harness.webapi.server import create_app

from .test_launcher import FakeSupervisor, _base_entry

_AUTH = {"Authorization": "Bearer secret"}
_MANIFEST = Path("queue") / "maintenance_manifest.json"


class _MaintenanceSupervisor(FakeSupervisor):
    """FakeSupervisor plus stop/resume recording for the maintenance routes."""

    def __init__(self, runs_root: Path) -> None:
        super().__init__(runs_root)
        self.stopped: list[str] = []
        self.resumed: list[tuple[str, str]] = []
        self.resume_failures: set[str] = set()

    def stop(self, run_id: str) -> dict[str, Any]:
        lifecycle = canonical_lifecycle_status(self.status(run_id).get("status"))
        if lifecycle in TERMINAL_STATUSES:
            raise ValueError(f"run {run_id} is not active")
        self.stopped.append(run_id)
        self._statuses[run_id] = {"status": "cancelled", "run_id": run_id}
        return {"ok": True, "run_id": run_id}

    def resume(self, run_id: str, *, mode: str = "continue", **kwargs: Any) -> dict[str, Any]:
        if run_id in self.resume_failures:
            raise ValueError(f"resume refused for {run_id}")
        self.resumed.append((run_id, mode))
        owner = self._owners.setdefault(run_id, {"run_id": run_id})
        owner["resume_epoch"] = int(owner.get("resume_epoch") or 0) + 1
        owner["resume_kind"] = mode
        self._statuses[run_id] = {"status": "running", "run_id": run_id}
        return {"id": run_id, "owner": owner}


def _client(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _MaintenanceSupervisor(root)
    app = create_app(
        runs_root=root,
        supervisor=supervisor,
        auth_token="secret",
        bind_host="testserver",
        probe_open_pr=None,
    )
    # Plain TestClient (not a context manager): the lifespan never starts the
    # launcher loop, so no background tick can race the assertions.
    return TestClient(app), root, store, supervisor


def _manifest(root: Path) -> dict[str, Any]:
    return json.loads((root / _MANIFEST).read_text(encoding="utf-8"))


def test_suspend_refused_without_drain(tmp_path: Path) -> None:
    client, root, _store, supervisor = _client(tmp_path)
    supervisor.add_run("run-active", "./w1", status="running")

    response = client.post(
        "/api/maintenance/suspend", json={"reason": "deploy"}, headers=_AUTH
    )

    assert response.status_code == 409
    assert supervisor.stopped == []
    assert not (root / _MANIFEST).exists()

    # The bearer boundary that guards every /api/ route covers maintenance.
    unauthenticated = client.post("/api/maintenance/suspend", json={"reason": "deploy"})
    assert unauthenticated.status_code == 401


def test_suspend_stops_active_runs_and_writes_manifest(tmp_path: Path) -> None:
    client, root, store, supervisor = _client(tmp_path)
    supervisor.add_run("run-a", "./w1", status="running")
    supervisor.add_run("run-done", "./w2", status="completed")
    supervisor.add_run("run-c", "./w3", status="starting")
    entry = store.create(_base_entry(trio="kimi", priority=5, workspace="./w1"))
    store.mark_launched(entry.queue_id, "run-a")
    store.set_drain(True, reason="deploy fc-H4a")

    response = client.post(
        "/api/maintenance/suspend", json={"reason": "deploy fc-H4a"}, headers=_AUTH
    )

    assert response.status_code == 200, response.text
    body = response.json()
    stopped_ids = {item["run_id"] for item in body["stopped"]}
    assert stopped_ids == {"run-a", "run-c"}, "terminal runs must not be stopped"
    assert supervisor.stopped == ["run-a", "run-c"]
    by_id = {item["run_id"]: item for item in body["stopped"]}
    assert by_id["run-a"]["queue_id"] == entry.queue_id
    assert by_id["run-c"]["queue_id"] is None

    manifest = _manifest(root)
    assert manifest["reason"] == "deploy fc-H4a"
    assert isinstance(manifest["created_at"], float)
    assert {item["run_id"] for item in manifest["runs"]} == {"run-a", "run-c"}
    for item in manifest["runs"]:
        assert set(item) == {"run_id", "queue_id", "resume_epoch_before"}
        assert item["resume_epoch_before"] == 0

    # GET surfaces the same manifest; /api/meta advertises the capability.
    assert client.get("/api/maintenance", headers=_AUTH).json() == manifest
    meta = client.get("/api/meta", headers=_AUTH).json()
    assert meta["capabilities"]["maintenance"] is True


def test_suspend_merges_and_never_drops_entries(tmp_path: Path) -> None:
    client, root, _store, supervisor = _client(tmp_path)
    supervisor.add_run("run-a", "./w1", status="running")
    QueueStore(root).set_drain(True, reason="deploy")
    first = client.post(
        "/api/maintenance/suspend", json={"reason": "deploy"}, headers=_AUTH
    )
    assert first.status_code == 200
    first_created = _manifest(root)["created_at"]

    supervisor.add_run("run-b", "./w2", status="running")
    second = client.post(
        "/api/maintenance/suspend", json={"reason": "deploy continued"}, headers=_AUTH
    )
    assert second.status_code == 200

    manifest = _manifest(root)
    assert {item["run_id"] for item in manifest["runs"]} == {"run-a", "run-b"}
    # The first batch's bookkeeping (original window start) is preserved.
    assert manifest["created_at"] == first_created


def test_resume_continues_and_clears_manifest(tmp_path: Path) -> None:
    client, root, _store, supervisor = _client(tmp_path)
    supervisor.add_run("run-a", "./w1", status="running")
    supervisor.add_run("run-b", "./w2", status="running")
    QueueStore(root).set_drain(True, reason="deploy")
    assert client.post(
        "/api/maintenance/suspend", json={"reason": "deploy"}, headers=_AUTH
    ).status_code == 200
    assert (root / _MANIFEST).exists()

    response = client.post("/api/maintenance/resume", json={}, headers=_AUTH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert {item["run_id"] for item in body["runs"]} == {"run-a", "run-b"}
    assert all(item["ok"] and item["error"] is None for item in body["runs"])
    # Every parked run came back with an in-place continue.
    assert sorted(supervisor.resumed) == [("run-a", "continue"), ("run-b", "continue")]
    assert not (root / _MANIFEST).exists()
    assert client.get("/api/maintenance", headers=_AUTH).json() == {"runs": []}


def test_failing_resume_keeps_the_run_in_the_manifest(tmp_path: Path) -> None:
    client, root, _store, supervisor = _client(tmp_path)
    supervisor.add_run("run-a", "./w1", status="running")
    supervisor.add_run("run-b", "./w2", status="running")
    QueueStore(root).set_drain(True, reason="deploy")
    assert client.post(
        "/api/maintenance/suspend", json={"reason": "deploy"}, headers=_AUTH
    ).status_code == 200

    supervisor.resume_failures.add("run-b")
    first = client.post("/api/maintenance/resume", json={}, headers=_AUTH)

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["ok"] is False
    by_id = {item["run_id"]: item for item in body["runs"]}
    assert by_id["run-a"]["ok"] is True
    assert by_id["run-b"]["ok"] is False
    assert "refused" in by_id["run-b"]["error"]
    # Only the failed run stays parked; manifest survives for the retry.
    manifest = _manifest(root)
    assert [item["run_id"] for item in manifest["runs"]] == ["run-b"]

    supervisor.resume_failures.clear()
    second = client.post("/api/maintenance/resume", json={}, headers=_AUTH)
    assert second.status_code == 200
    assert second.json()["ok"] is True
    assert not (root / _MANIFEST).exists()


def test_launcher_keeps_manifest_listed_entry_launched(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _MaintenanceSupervisor(root)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 1
    launcher = Launcher(supervisor, store, queue_config=config, probe_open_pr=None)

    supervisor.add_run("run-parked", "./w1", status="cancelled")
    supervisor.add_run("run-gone", "./w2", status="cancelled")
    parked = store.create(_base_entry(trio="kimi", priority=5, workspace="./w1"))
    orphan = store.create(_base_entry(trio="kimi", priority=4, workspace="./w2"))
    store.mark_launched(parked.queue_id, "run-parked")
    store.mark_launched(orphan.queue_id, "run-gone")

    manifest = {
        "reason": "deploy",
        "created_at": 123.0,
        "runs": [
            {"run_id": "run-parked", "queue_id": parked.queue_id, "resume_epoch_before": 0}
        ],
    }
    (root / "queue").mkdir(parents=True, exist_ok=True)
    (root / _MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

    asyncio.run(launcher.tick())

    parked_entry = store.get(parked.queue_id)
    orphan_entry = store.get(orphan.queue_id)
    assert parked_entry is not None
    assert orphan_entry is not None
    # Parked by the manifest: left launched for /api/maintenance/resume.
    assert parked_entry.status == "launched"
    # A terminal run that is NOT in the manifest reconciles exactly as before.
    assert orphan_entry.status == "failed"
