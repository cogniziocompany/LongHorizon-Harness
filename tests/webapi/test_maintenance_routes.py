"""Maintenance suspend/resume API — fc-H4a, async since LHH-SUSPEND-TIMEOUT.

A deploy drains the queue, then calls POST /api/maintenance/suspend, which
sets the maintenance launch pause synchronously, returns 202 with a suspend
id at once, and pauses every ACTIVE run in a background job bounded by a
per-run grace and an overall deadline.  Paused runs are parked in a manifest
keyed by that suspend id (``runs_root/queue/maintenance_manifest_<id>.json``)
so the launcher never reconciles their queue entries to failed.  POST
/api/maintenance/resume consumes only the manifest of the suspend id it is
given (or of the one open suspend), puts each parked run back with
``mode="continue"``, deletes that manifest once every run is active again,
and clears the launch pause.  GET /api/maintenance/suspend/{id} reports
per-run status; every parked run across manifests is visible at
GET /api/maintenance.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.launcher import (
    Launcher,
    clear_maintenance_launch_pause,
    read_maintenance_launch_pause,
    set_maintenance_launch_pause,
)
from lh_harness.queue import QueueStore, default_queue_config
from lh_harness.supervisor.lifecycle import TERMINAL_STATUSES, canonical_lifecycle_status
from lh_harness.webapi.server import create_app

from .test_launcher import FakeSupervisor, _base_entry

_AUTH = {"Authorization": "Bearer secret"}
_QUEUE_DIR = Path("queue")


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


def _manifest_path(root: Path, suspend_id: str) -> Path:
    return root / _QUEUE_DIR / f"maintenance_manifest_{suspend_id}.json"


def _manifest(root: Path, suspend_id: str) -> dict[str, Any]:
    return json.loads(_manifest_path(root, suspend_id).read_text(encoding="utf-8"))


def _post_suspend(client: TestClient, reason: str = "deploy") -> str:
    """POST the async suspend and return its id; asserts the 202 contract."""

    response = client.post("/api/maintenance/suspend", json={"reason": reason}, headers=_AUTH)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["state"] == "running"
    return str(body["suspend_id"])


def _wait_suspend(client: TestClient, suspend_id: str, timeout: float = 10.0) -> dict[str, Any]:
    """Poll GET /api/maintenance/suspend/{id} until the job is terminal."""

    deadline = time.monotonic() + timeout
    while True:
        response = client.get(f"/api/maintenance/suspend/{suspend_id}", headers=_AUTH)
        assert response.status_code == 200, response.text
        body = response.json()
        if body["state"] != "running" or time.monotonic() >= deadline:
            return body
        time.sleep(0.05)


def test_suspend_refused_without_drain(tmp_path: Path) -> None:
    client, root, _store, supervisor = _client(tmp_path)
    supervisor.add_run("run-active", "./w1", status="running")

    response = client.post(
        "/api/maintenance/suspend", json={"reason": "deploy"}, headers=_AUTH
    )

    assert response.status_code == 409
    assert supervisor.stopped == []
    assert read_maintenance_launch_pause(root)["enabled"] is False

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

    assert response.status_code == 202, response.text
    suspend_id = str(response.json()["suspend_id"])
    # The launch pause is set synchronously in the POST handler, before any
    # run pause: it is already durable when the 202 comes back.
    pause = read_maintenance_launch_pause(root)
    assert pause["enabled"] is True
    assert pause["suspend_id"] == suspend_id

    status = _wait_suspend(client, suspend_id)
    assert status["state"] == "suspended", status
    assert status["launch_pause"]["enabled"] is True
    stopped_ids = {item["run_id"] for item in status["runs"]}
    assert stopped_ids == {"run-a", "run-c"}, "terminal runs must not be stopped"
    assert all(item["state"] == "paused" for item in status["runs"])
    assert sorted(supervisor.stopped) == ["run-a", "run-c"]
    by_id = {item["run_id"]: item for item in status["runs"]}
    assert by_id["run-a"]["queue_id"] == entry.queue_id
    assert by_id["run-c"]["queue_id"] is None

    manifest = _manifest(root, suspend_id)
    assert manifest["suspend_id"] == suspend_id
    assert manifest["reason"] == "deploy fc-H4a"
    assert isinstance(manifest["created_at"], float)
    assert {item["run_id"] for item in manifest["runs"]} == {"run-a", "run-c"}
    for item in manifest["runs"]:
        assert set(item) == {"run_id", "queue_id", "resume_epoch_before"}
        assert item["resume_epoch_before"] == 0

    # GET surfaces the same parked runs; unknown suspend ids are 404;
    # /api/meta advertises the capability.
    assert client.get("/api/maintenance", headers=_AUTH).json() == {"runs": manifest["runs"]}
    unknown = client.get("/api/maintenance/suspend/sus-nope", headers=_AUTH)
    assert unknown.status_code == 404
    meta = client.get("/api/meta", headers=_AUTH).json()
    assert meta["capabilities"]["maintenance"] is True


def test_second_suspend_refused_while_window_open(tmp_path: Path) -> None:
    """The old merge contract is gone: windows never overlap, so one suspend's
    bookkeeping can never be dropped or absorbed into another's manifest."""
    client, root, _store, supervisor = _client(tmp_path)
    supervisor.add_run("run-a", "./w1", status="running")
    QueueStore(root).set_drain(True, reason="deploy")
    suspend_id = _post_suspend(client)
    assert _wait_suspend(client, suspend_id)["state"] == "suspended"
    first = _manifest(root, suspend_id)
    first_created = first["created_at"]

    supervisor.add_run("run-b", "./w2", status="running")
    second = client.post(
        "/api/maintenance/suspend", json={"reason": "deploy continued"}, headers=_AUTH
    )
    assert second.status_code == 409, second.text
    assert suspend_id in second.json()["detail"]

    # The open window's bookkeeping (its id, runs, and original start) is
    # untouched, and run-b was never stopped.
    manifest = _manifest(root, suspend_id)
    assert manifest["suspend_id"] == suspend_id
    assert {item["run_id"] for item in manifest["runs"]} == {"run-a"}
    assert manifest["created_at"] == first_created
    assert supervisor.stopped == ["run-a"]


def test_resume_continues_and_clears_manifest(tmp_path: Path) -> None:
    client, root, _store, supervisor = _client(tmp_path)
    supervisor.add_run("run-a", "./w1", status="running")
    supervisor.add_run("run-b", "./w2", status="running")
    QueueStore(root).set_drain(True, reason="deploy")
    suspend_id = _post_suspend(client)
    assert _wait_suspend(client, suspend_id)["state"] == "suspended"
    assert _manifest_path(root, suspend_id).exists()

    # No explicit id: the one open suspend this process owns is used.
    response = client.post("/api/maintenance/resume", json={}, headers=_AUTH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["suspend_id"] == suspend_id
    assert {item["run_id"] for item in body["runs"]} == {"run-a", "run-b"}
    assert all(item["ok"] and item["error"] is None for item in body["runs"])
    # Every parked run came back with an in-place continue.
    assert sorted(supervisor.resumed) == [("run-a", "continue"), ("run-b", "continue")]
    assert not _manifest_path(root, suspend_id).exists()
    assert read_maintenance_launch_pause(root)["enabled"] is False
    assert client.get("/api/maintenance", headers=_AUTH).json() == {"runs": []}

    # A repeat resume is a safe no-op against the cleared window.
    repeat = client.post(
        "/api/maintenance/resume", json={"suspend_id": suspend_id}, headers=_AUTH
    )
    assert repeat.status_code == 200
    assert repeat.json() == {"ok": True, "suspend_id": suspend_id, "runs": []}


def test_failing_resume_keeps_the_run_in_the_manifest(tmp_path: Path) -> None:
    client, root, _store, supervisor = _client(tmp_path)
    supervisor.add_run("run-a", "./w1", status="running")
    supervisor.add_run("run-b", "./w2", status="running")
    QueueStore(root).set_drain(True, reason="deploy")
    suspend_id = _post_suspend(client)
    assert _wait_suspend(client, suspend_id)["state"] == "suspended"

    supervisor.resume_failures.add("run-b")
    first = client.post(
        "/api/maintenance/resume", json={"suspend_id": suspend_id}, headers=_AUTH
    )

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["ok"] is False
    assert body["suspend_id"] == suspend_id
    by_id = {item["run_id"]: item for item in body["runs"]}
    assert by_id["run-a"]["ok"] is True
    assert by_id["run-b"]["ok"] is False
    assert "refused" in by_id["run-b"]["error"]
    # Only the failed run stays parked; the id-keyed manifest survives for
    # the retry.
    manifest = _manifest(root, suspend_id)
    assert manifest["suspend_id"] == suspend_id
    assert [item["run_id"] for item in manifest["runs"]] == ["run-b"]

    supervisor.resume_failures.clear()
    second = client.post(
        "/api/maintenance/resume", json={"suspend_id": suspend_id}, headers=_AUTH
    )
    assert second.status_code == 200
    assert second.json()["ok"] is True
    assert not _manifest_path(root, suspend_id).exists()


def test_launcher_holds_launches_for_the_whole_suspend_window(tmp_path: Path) -> None:
    """The 2026-10-08 incident gap: the deploy cleared the drain while the
    suspend window was still unresolved and the launcher started a run in the
    gap.  The maintenance launch pause is a separate flag, so clearing the
    drain mid-window must STILL refuse every launch; only resume reopens."""
    client, root, store, supervisor = _client(tmp_path)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 1
    launcher = Launcher(supervisor, store, queue_config=config, probe_open_pr=None)

    store.set_drain(True, reason="deploy")
    suspend_id = _post_suspend(client)
    assert _wait_suspend(client, suspend_id)["state"] == "suspended"

    entry = store.create(_base_entry(trio="kimi", priority=5, workspace="./w1"))
    # The deploy's drain clear runs on its own schedule, mid-window here.
    store.set_drain(False)

    asyncio.run(launcher.tick())

    held = store.get(entry.queue_id)
    assert held is not None
    assert held.status == "pending"
    assert supervisor.created == [], "no launch may start while the window is open"
    assert any(
        "maintenance launch pause" in reason and suspend_id in reason
        for reason in held.skip_reasons
    )

    resumed = client.post(
        "/api/maintenance/resume", json={"suspend_id": suspend_id}, headers=_AUTH
    )
    assert resumed.status_code == 200, resumed.text
    assert read_maintenance_launch_pause(root)["enabled"] is False

    asyncio.run(launcher.tick())

    assert len(supervisor.created) == 1
    assert store.get(entry.queue_id).status == "launched"


def test_clear_launch_pause_only_for_owning_suspend(tmp_path: Path) -> None:
    """A late cleanup or retry resume for an older window must never tear
    down a newer suspend's flag out from under its open window."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)

    set_maintenance_launch_pause(root, reason="deploy", suspend_id="sus-older")
    clear_maintenance_launch_pause(root, if_suspend_id="sus-newer")
    flag = read_maintenance_launch_pause(root)
    assert flag["enabled"] is True
    assert flag["suspend_id"] == "sus-older"

    clear_maintenance_launch_pause(root, if_suspend_id="sus-older")
    assert read_maintenance_launch_pause(root)["enabled"] is False

    # Clearing an already-absent flag stays a safe no-op, owner id or not.
    clear_maintenance_launch_pause(root, if_suspend_id="sus-older")
    clear_maintenance_launch_pause(root)
    assert read_maintenance_launch_pause(root)["enabled"] is False


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

    # The legacy (pre-suspend-id) manifest path still protects reconciliation.
    manifest = {
        "reason": "deploy",
        "created_at": 123.0,
        "runs": [
            {"run_id": "run-parked", "queue_id": parked.queue_id, "resume_epoch_before": 0}
        ],
    }
    (root / "queue").mkdir(parents=True, exist_ok=True)
    (root / _QUEUE_DIR / "maintenance_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    asyncio.run(launcher.tick())

    parked_entry = store.get(parked.queue_id)
    orphan_entry = store.get(orphan.queue_id)
    assert parked_entry is not None
    assert orphan_entry is not None
    # Parked by the manifest: left launched for /api/maintenance/resume.
    assert parked_entry.status == "launched"
    # A terminal run that is NOT in the manifest reconciles exactly as before.
    assert orphan_entry.status == "failed"
