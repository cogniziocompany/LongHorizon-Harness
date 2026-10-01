"""Tests for the per-run and fleet liveness routes (task fc-H1a).

``GET /api/runs/{run_id}/latest`` and ``GET /api/runs/latest`` let the
overseer tell a quiet run from a dead one without building snapshots or
hand-setting time limits.  Covered here:

- the per-run route returns the last event of a fixture ``events.jsonl`` with
  the type normalized through ``EVENT_TYPE_MAP``;
- ``EventTailer.read_last`` seeks from the end of a large log and never reads
  the whole file (asserted bound on bytes read);
- an unknown run id returns 404;
- ``/api/runs/latest`` lists only ACTIVE-status runs and is registered before
  the ``{run_id}`` routes (it must not be parsed as a run id);
- ``last_event_age_seconds`` equals ``now - ts``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import DashboardState
from lh_harness.queue import QueueStore
from lh_harness.supervisor.service import RunSupervisor
from lh_harness.webapi.events import _TAIL_CHUNK_BYTES, EventTailer
from lh_harness.webapi.server import create_app


def _event(ts: float, *, name: str, round_no: int, role: str, run_id: str) -> dict:
    return {
        "schema_version": 2,
        "event": name,
        "ts": ts,
        "round": round_no,
        "role": role,
        "run_id": run_id,
    }


def _make_run(
    root: Path,
    run_id: str,
    *,
    status: str,
    events: list[dict] | None = None,
) -> None:
    run_dir = root / run_id
    control = run_dir / "control"
    control.mkdir(parents=True)
    (control / "status.json").write_text(
        json.dumps({"status": status, "mtime": 1234.0}), encoding="utf-8"
    )
    (control / "owner.json").write_text(
        json.dumps({"task": f"task {run_id}", "workspace": "/tmp/ws", "agent": "codex"}),
        encoding="utf-8",
    )
    logs = run_dir / "lh_harness" / "role_orchestration"
    logs.mkdir(parents=True)
    lines = [json.dumps(record) for record in (events or [])]
    (logs / "events.jsonl").write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )


def _app(tmp_path: Path) -> tuple[TestClient, Path]:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    now = time.time()
    _make_run(
        root,
        "run-active",
        status="running",
        events=[
            _event(now - 300.0, name="role_harness_start", round_no=1, role="manager", run_id="run-active"),
            _event(now - 120.0, name="manager_round_done", round_no=1, role="manager", run_id="run-active"),
            _event(now - 30.0, name="executor_role_start", round_no=2, role="executor", run_id="run-active"),
        ],
    )
    _make_run(
        root,
        "run-waiting",
        status="waiting_approval",
        events=[
            _event(now - 45.0, name="approval_created", round_no=4, role="manager", run_id="run-waiting"),
        ],
    )
    _make_run(
        root,
        "run-failed",
        status="failed",
        events=[
            _event(now - 4000.0, name="role_harness_failed", round_no=9, role="manager", run_id="run-failed"),
        ],
    )
    supervisor = RunSupervisor(root, workspace_root=tmp_path)
    dashboard = DashboardState(
        root / "run-active" / "lh_harness", runs_root=root, control_enabled=False
    )
    app = create_app(state=dashboard, runs_root=root, run_id="run-active", supervisor=supervisor)
    return TestClient(app), root


def test_latest_fixture_event_is_normalized(tmp_path: Path) -> None:
    client, _ = _app(tmp_path)
    response = client.get("/api/runs/run-active/latest")
    assert response.status_code == 200
    row = response.json()
    assert row["run_id"] == "run-active"
    assert row["status"] == "running"
    assert row["console_path"] == "/runs/run-active"
    assert row["round"] == 2
    assert row["active_role"] == "executor"
    assert row["time_limit"] is None
    last = row["last_event"]
    assert last is not None
    assert last["type"] == "round.executor.started"
    assert last["round"] == 2
    assert last["role"] == "executor"
    assert isinstance(last["id"], str) and last["id"]
    assert isinstance(last["ts"], (int, float))


def test_latest_age_is_now_minus_ts(tmp_path: Path) -> None:
    client, _ = _app(tmp_path)
    row = client.get("/api/runs/run-waiting/latest").json()
    assert row["last_event"] is not None
    assert row["last_event_age_seconds"] == pytest.approx(
        row["now"] - row["last_event"]["ts"], abs=1e-6
    )


def test_latest_unknown_run_is_404(tmp_path: Path) -> None:
    client, _ = _app(tmp_path)
    response = client.get("/api/runs/run-does-not-exist/latest")
    assert response.status_code == 404


def test_runs_latest_lists_only_active_runs(tmp_path: Path) -> None:
    client, _ = _app(tmp_path)
    response = client.get("/api/runs/latest")
    # Registered before the {run_id} routes: this must be the aggregate route,
    # not a 404 "run not found" for a run literally named "latest".
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"now", "runs"}
    assert isinstance(body["now"], (int, float))
    statuses = {row["run_id"]: row["status"] for row in body["runs"]}
    assert statuses == {"run-active": "running", "run-waiting": "waiting_approval"}
    for row in body["runs"]:
        assert row["last_event"] is not None
        assert row["time_limit"] is None
        assert row["console_path"] == f"/runs/{row['run_id']}"


def test_queue_linkage_surfaces_queue_id_and_name(tmp_path: Path) -> None:
    client, root = _app(tmp_path)
    # File-backed store at the same runs root the app's own queue store reads.
    store = QueueStore(root, {})
    entry = store.create(
        {
            "name": "liveness-task",
            "task": "do the thing",
            "workspace": "/tmp/ws",
            "max_rounds": 16,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "orchestrator",
        }
    )
    store.mark_launched(entry.queue_id, "run-active")
    row = client.get("/api/runs/run-active/latest").json()
    assert row["queue_id"] == entry.queue_id
    assert row["name"] == "liveness-task"
    waiting = client.get("/api/runs/run-waiting/latest").json()
    assert waiting["queue_id"] is None
    assert waiting["name"] is None


def test_read_last_seeks_tail_of_large_file(tmp_path: Path) -> None:
    """A large log is read from the tail only, never replayed in full."""

    path = tmp_path / "events.jsonl"
    run_id = "run-large"
    total = 30_000  # ~3.4 MB of small records, far beyond one tail chunk
    with path.open("w", encoding="utf-8") as fh:
        for index in range(total):
            fh.write(
                json.dumps(
                    {
                        "schema_version": 2,
                        "event_id": f"{run_id}:{index:06d}",
                        "event": "manager_round_start",
                        "ts": 1_000.0 + index,
                        "round": index,
                        "role": "manager",
                        "run_id": run_id,
                    }
                )
                + "\n"
            )
    file_size = path.stat().st_size
    assert file_size > 8 * _TAIL_CHUNK_BYTES, "fixture must exceed one tail chunk"

    tailer = EventTailer(path, run_id=run_id)
    tail = tailer.read_last(1)
    assert len(tail) == 1
    assert tail[0].event_id == f"{run_id}:{total - 1:06d}"
    assert tail[0].type == "round.manager.started"
    assert tail[0].ts == pytest.approx(1_000.0 + total - 1)
    # The whole point of the endpoint: bounded tail read, not a full replay.
    assert tailer.last_tail_bytes_read <= 4 * _TAIL_CHUNK_BYTES
    assert tailer.last_tail_bytes_read < file_size // 8


def test_read_last_missing_log_returns_empty(tmp_path: Path) -> None:
    tailer = EventTailer(tmp_path / "missing" / "events.jsonl", run_id="run-x")
    assert tailer.read_last(1) == []
    assert tailer.last_tail_bytes_read == 0
