"""Tests for the cheap enumeration mode of ``GET /api/runs`` (task 194).

Covers:
- the ``fields=summary`` projection omits large per-run fields and carries the
  gate-detection fields;
- the ``status=`` comma-list filter returns exactly the matching rows;
- the default (no-params) response keeps the historical ``{"runs": [...]}``
  shape with rows keyed ``id`` and parses with existing repo readers;
- a several-hundred-run fixture shows the cheap path stays near-constant per
  row while the default path grows with total run count.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import DashboardState
from lh_harness.supervisor.service import RunSupervisor
from lh_harness.webapi.server import create_app

SUMMARY_FIELDS = frozenset({"id", "status", "workspace", "updated_at", "round"})
LARGE_FIELDS = frozenset({"task", "task_summary"})


def _make_run(root: Path, run_id: str, *, status: str, task: str = "") -> None:
    """Lay down one minimal run directory directly on disk (no run creation)."""

    run_dir = root / run_id
    control = run_dir / "control"
    control.mkdir(parents=True)
    (control / "status.json").write_text(
        json.dumps({"status": status, "mtime": 1234.0}), encoding="utf-8"
    )
    (control / "owner.json").write_text(
        json.dumps({"task": task or f"task {run_id}", "workspace": "/tmp/ws", "agent": "codex"}),
        encoding="utf-8",
    )
    logs = run_dir / "lh_harness" / "role_orchestration"
    logs.mkdir(parents=True)
    (logs / "events.jsonl").write_text("", encoding="utf-8")


def _supervisor_app(tmp_path: Path, run_count: int) -> tuple[TestClient, Path]:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    for index in range(run_count):
        status = "waiting_approval" if index % 5 == 0 else "failed"
        _make_run(root, f"run-{index:04d}", status=status)
    supervisor = RunSupervisor(root, workspace_root=tmp_path)
    dashboard = DashboardState(root / "run-0000", runs_root=root, control_enabled=False)
    app = create_app(state=dashboard, runs_root=root, run_id="run-0000", supervisor=supervisor)
    return TestClient(app), root


def test_summary_projection_omits_large_fields(tmp_path: Path) -> None:
    client, _ = _supervisor_app(tmp_path, run_count=3)
    payload = client.get("/api/runs?fields=summary")
    assert payload.status_code == 200
    rows = payload.json()["runs"]
    assert rows and len(rows) == 3
    for row in rows:
        for field in SUMMARY_FIELDS - {"round"}:
            assert field in row
        for field in LARGE_FIELDS:
            assert field not in row
        assert isinstance(row["id"], str) and row["id"].startswith("run-")
        assert row["status"] in {"waiting_approval", "failed"}
        assert isinstance(row["updated_at"], (int, float))


def test_status_filter_returns_exactly_matching_rows(tmp_path: Path) -> None:
    client, _ = _supervisor_app(tmp_path, run_count=10)
    payload = client.get("/api/runs?fields=summary&status=waiting_approval")
    assert payload.status_code == 200
    rows = payload.json()["runs"]
    assert rows, "fixture should contain at least one waiting_approval run"
    assert all(row["status"] == "waiting_approval" for row in rows)
    ids = {row["id"] for row in rows}
    assert ids == {f"run-{index:04d}" for index in range(10) if index % 5 == 0}

    combined = client.get("/api/runs?fields=summary&status=waiting_approval,running")
    assert combined.status_code == 200
    assert all(row["status"] in {"waiting_approval", "running"} for row in combined.json()["runs"])

    empty = client.get("/api/runs?fields=summary&status=completed")
    assert empty.status_code == 200
    assert empty.json()["runs"] == []


def test_status_filter_composes_with_default_projection(tmp_path: Path) -> None:
    client, _ = _supervisor_app(tmp_path, run_count=4)
    payload = client.get("/api/runs?status=failed")
    assert payload.status_code == 200
    rows = payload.json()["runs"]
    assert rows
    assert all(row["status"] == "failed" for row in rows)
    # The default projection still carries its full fields.
    assert all("task" in row for row in rows)
    assert all("id" in row for row in rows)


def test_default_response_keeps_id_key_and_parses_for_existing_reader(
    tmp_path: Path,
) -> None:
    client, _ = _supervisor_app(tmp_path, run_count=3)
    payload = client.get("/api/runs")
    assert payload.status_code == 200
    body = payload.json()
    assert set(body.keys()) == {"runs"}
    rows = body["runs"]
    assert rows and len(rows) == 3
    for row in rows:
        assert "id" in row, "default rows must keep the `id` key"
        assert "run_id" not in row, "no reader may be forced to switch to run_id"
        assert "task" in row, "default projection stays full"
    # Representative existing reader: find one row by id (same access pattern
    # as tests/supervisor/test_supervisor.py).
    target = next(item for item in rows if item["id"] == "run-0000")
    assert target["status"] in {"waiting_approval", "failed"}


def test_unknown_fields_value_falls_back_to_default(tmp_path: Path) -> None:
    client, _ = _supervisor_app(tmp_path, run_count=2)
    payload = client.get("/api/runs?fields=nonsense")
    assert payload.status_code == 200
    rows = payload.json()["runs"]
    assert rows and all("task" in row for row in rows)


def test_cheap_path_does_not_scale_per_run_like_the_default_path(
    tmp_path: Path,
) -> None:
    """Compare per-row marginal cost between the cheap and default paths."""

    small_client, _ = _supervisor_app(tmp_path / "small", run_count=50)
    large_client, _ = _supervisor_app(tmp_path / "large", run_count=300)

    def _timed(client: TestClient, query: str, runs_expected: int) -> float:
        best = float("inf")
        for _ in range(3):
            started = time.perf_counter()
            response = client.get(f"/api/runs{query}")
            assert response.status_code == 200
            body = response.json()
            assert len(body["runs"]) == runs_expected
            best = min(best, time.perf_counter() - started)
        return best

    small_cheap = _timed(small_client, "?fields=summary", 50)
    large_cheap = _timed(large_client, "?fields=summary", 300)
    small_full = _timed(small_client, "", 50)
    large_full = _timed(large_client, "", 300)

    # The full path grows per run (state_for + summary per row); the cheap path
    # must stay far below it at 300 runs.
    assert large_cheap < large_full, "cheap path must be faster than the full path at 300 runs"
    cheap_marginal = (large_cheap - small_cheap) / 250
    full_marginal = (large_full - small_full) / 250
    assert cheap_marginal <= full_marginal / 5, (
        f"cheap marginal cost {cheap_marginal * 1000:.4f} ms/run must be far below "
        f"full marginal cost {full_marginal * 1000:.4f} ms/run"
    )


def test_unknown_status_rows_are_excluded_by_default_projection(tmp_path: Path) -> None:
    """Rows without durable control data are absent from the cheap listing."""

    root = tmp_path / "runs"
    (root / "not-a-run").mkdir(parents=True)  # no control/ records at all
    _make_run(root, "run-real", status="running")
    supervisor = RunSupervisor(root, workspace_root=tmp_path)
    dashboard = DashboardState(root / "run-real", runs_root=root, control_enabled=False)
    app = create_app(state=dashboard, runs_root=root, run_id="run-real", supervisor=supervisor)
    client = TestClient(app)

    payload = client.get("/api/runs?fields=summary")
    assert payload.status_code == 200
    ids = {row["id"] for row in payload.json()["runs"]}
    assert "not-a-run" not in ids
    assert "run-real" in ids