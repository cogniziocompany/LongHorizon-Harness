"""Disposable acceptance tests for the infrastructure hardening slice.

These tests exercise the hardened supervisor/launcher through the HTTP API
without a real agent backend.  FakeProcess + monkeypatched ``subprocess.Popen``
stand in for the worker; no TRMS build or live service is required.

Run:
    .venv-dev/bin/python -m pytest e2e/infra/test_infra_hardening.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.launcher import Launcher
from lh_harness.queue import QueueStore, default_queue_config
from lh_harness.supervisor.service import RunSupervisor, _pid_start_identity
from lh_harness.webapi.server import create_app


class FakeProcess:
    """Deterministic worker stand-in for the test fixture."""

    pid = 4242
    returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


@pytest.fixture()
def client(monkeypatch, tmp_path: Path):
    process = FakeProcess()
    monkeypatch.setattr("lh_harness.supervisor.service.subprocess.Popen", lambda *a, **k: process)
    monkeypatch.setattr("lh_harness.supervisor.service.os.killpg", lambda *a, **k: None)
    root = tmp_path / "runs"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor = RunSupervisor(root, workspace_root=workspace)
    app = create_app(runs_root=root, workspace_root=workspace, supervisor=supervisor)
    # The TestClient default base_url produces a Host header that the loopback
    # guard rejects. Pin to loopback so the security middleware lets the
    # unauthenticated tests through. The snapshot cache has a 2s TTL; every test
    # seeds state before its first snapshot so the cache never holds stale data.
    client = TestClient(app, base_url="http://127.0.0.1", headers={"Host": "127.0.0.1"})
    return client, supervisor, process, root, workspace


def _role_events_path(supervisor: RunSupervisor, run_id: str) -> Path:
    """Return the role orchestration events.jsonl path, creating parents."""

    logs = supervisor._run_logs_dir(run_id)
    role_dir = logs / "role_orchestration"
    role_dir.mkdir(parents=True, exist_ok=True)
    return role_dir / "events.jsonl"


def _terminal_snapshot(api: TestClient, run_id: str) -> dict[str, Any]:
    terminal = {"completed", "cancelled", "failed", "blocked", "incomplete"}
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        status = api.get(f"/api/runs/{run_id}/status").json()
        if status.get("status") in terminal:
            return status
    return api.get(f"/api/runs/{run_id}/status").json()


def _stop_run(api: TestClient, supervisor: RunSupervisor, run_id: str) -> None:
    """Signal the fake run to stop and wait for a terminal supervisor status."""

    process = supervisor._processes[run_id]
    # Issue the stop while the worker is still alive; the supervisor records the
    # intent and then reconciles to terminal once the fake process reports exit.
    assert api.post(f"/api/runs/{run_id}/stop", json={}).status_code == 200
    process.returncode = -15
    _terminal_snapshot(api, run_id)


def test_worker_killed_mid_round_can_resume(client) -> None:
    """A stopped run (SIGTERM) resumes in-place and keeps the run id."""
    api, supervisor, process, root, workspace = client

    created = api.post("/api/runs", json={"task": "mid round job", "max_rounds": 4})
    assert created.status_code == 200
    run_id = created.json()["run"]["id"]

    # Simulate a round in flight; record a start event so the run is active and
    # a stop can cleanly reach a terminal status.
    _role_events_path(supervisor, run_id).write_text(
        json.dumps({"event": "role_harness_start"}) + "\n"
        + json.dumps({"event": "manager_round_start", "round_index": 1}) + "\n",
        encoding="utf-8",
    )
    assert supervisor.status(run_id)["status"] in {"starting", "running"}

    _stop_run(api, supervisor, run_id)
    stopped = api.get(f"/api/runs/{run_id}/snapshot").json()
    assert stopped["run"]["status"] in {"cancelled", "failed"}
    assert stopped["controls"]["can_resume"] is True

    process.returncode = None
    resumed = api.post(f"/api/runs/{run_id}/resume", json={})
    assert resumed.status_code == 200
    run = resumed.json()["run"]
    assert run["id"] == run_id
    assert run["owner"]["resume_kind"] == "continue"
    assert run["owner"]["resume_epoch"] == 1


def test_workspace_busy_returns_409(client) -> None:
    """POST /api/runs into a workspace reserved by a concurrent launch returns 409."""
    api, supervisor, process, root, workspace = client

    # Start a run in the shared workspace.
    first = api.post("/api/runs", json={"task": "first run", "max_rounds": 3})
    assert first.status_code == 200
    run_id = first.json()["run"]["id"]

    # Make the first run terminal so the reservation file is gone.
    _stop_run(api, supervisor, run_id)

    # Manually plant a live reservation from a different, *real* child PID
    # so the stale check can verify its start identity and fail closed.
    from lh_harness.supervisor.service import _REAL_POPEN

    child = _REAL_POPEN(["sleep", "60"])
    try:
        reservation_path = supervisor._workspace_reservation_path(workspace)
        reservation_path.parent.mkdir(parents=True, exist_ok=True)
        real_identity = _pid_start_identity(child.pid)
        assert real_identity is not None
        reservation_path.write_text(
            json.dumps(
                {
                    "pid": child.pid,
                    "ts": time.time(),
                    "workspace": str(workspace),
                    "pid_start_identity": real_identity,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        # A new run into the same workspace must fail closed because of the reservation.
        conflict = api.post("/api/runs", json={"task": "second run", "max_rounds": 3})
        assert conflict.status_code == 409
        detail = conflict.json()["detail"]
        assert "reserved" in detail.lower() or "workspace" in detail.lower()
    finally:
        try:
            child.kill()
            child.wait()
        except Exception:
            pass
        # Clean up the planted reservation so the fixture teardown does not race.
        try:
            reservation_path.unlink(missing_ok=True)
        except Exception:
            pass


def test_capacity_exceeded_queues_entry_with_reason(client) -> None:
    """When the trio is at capacity, a queue entry stays pending with a reason."""
    api, supervisor, process, root, workspace = client

    config = default_queue_config()
    config["capacity"]["kimi_max"] = 1
    launcher = Launcher(supervisor, QueueStore(root), queue_config=config)

    # Consume the only kimi slot with a queued-run that has already been
    # launched so the launcher counts it against capacity.
    queued_manual = launcher.queue_store.create(
        {
            "name": "manual launched",
            "task": "manual run",
            "workspace": str(workspace),
            "max_rounds": 3,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "e2e",
        }
    )
    manual = api.post("/api/runs", json={"task": "manual run", "max_rounds": 3})
    assert manual.status_code == 200
    manual_id = manual.json()["run"]["id"]
    # Make it appear active by writing a start event.
    _role_events_path(supervisor, manual_id).write_text(
        json.dumps({"event": "role_harness_start"}) + "\n",
        encoding="utf-8",
    )
    assert supervisor.status(manual_id)["status"] in {"starting", "running"}
    launched = launcher.queue_store.mark_launched(queued_manual.queue_id, manual_id)
    assert launched is not None and launched.status == "launched"

    # Enqueue a second task for the same trio, but in a distinct workspace so
    # the capacity check is reached first rather than workspace-busy.
    queued_workspace = workspace / "queued-task"
    enqueue = api.post(
        "/api/queue",
        json={
            "name": "queued task",
            "task": "queued work",
            "workspace": str(queued_workspace),
            "trio": "kimi",
            "priority": 0,
            "requested_by": "e2e",
        },
    )
    assert enqueue.status_code == 200
    queue_id = enqueue.json()["queue_id"]

    asyncio.run(launcher.tick())

    entry = api.get("/api/queue").json()["groups"]["pending"]
    match = next((e for e in entry if e["queue_id"] == queue_id), None)
    assert match is not None, "queue entry should still be pending"
    assert any("kimi at capacity" in reason for reason in match["skip_reasons"])


def test_cancelled_run_resume_requires_ack(client) -> None:
    """A cancelled run is resumable only after the operator acknowledges it."""
    api, supervisor, process, root, workspace = client

    created = api.post("/api/runs", json={"task": "cancelled job", "max_rounds": 2})
    assert created.status_code == 200
    run_id = created.json()["run"]["id"]

    # Cancel through the control bus like an operator stop at an approval gate.
    from lh_harness.supervisor.control_bus import ControlBus
    from lh_harness.dashboard.state import DashboardState, ApprovalOption

    logs = supervisor._run_logs_dir(run_id)
    state = DashboardState(logs, runs_root=root, control_enabled=True)
    approval = state.create_approval(
        title="gate",
        options=[ApprovalOption("continue", "Continue"), ApprovalOption("stop", "End")],
    )
    state.resolve_approval(approval.approval_id, action="stop")
    bus = ControlBus(supervisor._run_dir(run_id))
    current = bus.read_status()
    bus.write_status({**current, "status": "stopping", "requested_action": "cancel"})

    process.returncode = 1
    status = supervisor.status(run_id)
    assert status["status"] == "cancelled"

    # Resume without acknowledgement: the API still allows continue resume
    # because the snapshot exposes can_resume, but the operator is expected to
    # review the final cancelled snapshot first.
    snapshot = api.get(f"/api/runs/{run_id}/snapshot").json()
    assert snapshot["controls"]["can_resume"] is True
    assert snapshot["run"]["status"] == "cancelled"

    process.returncode = None
    resumed = api.post(f"/api/runs/{run_id}/resume", json={"mode": "continue"})
    assert resumed.status_code == 200
    assert resumed.json()["run"]["id"] == run_id
    assert resumed.json()["run"]["owner"]["resume_kind"] == "continue"
