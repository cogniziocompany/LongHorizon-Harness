"""Report status fields survive the real summary-list production path."""

from __future__ import annotations

import json
from pathlib import Path

from lh_harness.supervisor.service import RunSupervisor
from lh_harness.webapi.server import _build_summary_projection


def _make_run(
    root: Path,
    run_id: str,
    *,
    status: str,
    report_status: str,
    completion_satisfied: object,
) -> None:
    run_dir = root / run_id
    control = run_dir / "control"
    logs = run_dir / "lh_harness"
    control.mkdir(parents=True)
    logs.mkdir()
    (control / "status.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    (control / "owner.json").write_text(
        json.dumps({"task": f"task {run_id}", "workspace": "/tmp/ws", "agent": "codex"}),
        encoding="utf-8",
    )
    (logs / "report.json").write_text(
        json.dumps({"status": report_status, "completion_satisfied": completion_satisfied}),
        encoding="utf-8",
    )


def test_summary_projection_receives_report_status_fields_from_list_run_summaries(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _make_run(root, "run-done", status="completed", report_status="completed", completion_satisfied=True)
    _make_run(root, "run-live", status="running", report_status="running", completion_satisfied=False)
    _make_run(root, "run-string", status="completed", report_status="completed", completion_satisfied="true")
    supervisor = RunSupervisor(root, workspace_root=tmp_path)

    items = {item["id"]: item for item in supervisor.list_run_summaries()}
    rows = {run_id: _build_summary_projection(item) for run_id, item in items.items()}

    assert rows["run-done"]["report_status"] == "completed"
    assert rows["run-done"]["completion_satisfied"] is True
    assert rows["run-string"]["completion_satisfied"] is None
    assert rows["run-live"].get("report_status") is None
    assert rows["run-live"].get("completion_satisfied") is None
