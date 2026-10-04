"""``GET /api/runs?fields=summary`` carries how a finished run ended.

Hydra's ``list_fleet_runs`` reads the summary rows; a run created outside the
queue has no queue entry carrying the launcher's reason, so the row itself must
say why it failed: ``outcome``, ``abort_reason`` and ``failure_reason``
(ASCII, <=200 chars) for terminal runs, absent on live rows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lh_harness.run_outcome import (
    REASON_MAX_CHARS,
    OutcomeCache,
    ascii_reason,
    derive_outcome,
    read_log_tail,
    stopped_line,
)

# --- pure derivation -------------------------------------------------------


def test_ascii_reason_maps_typography_and_bounds_length() -> None:
    text = "provider_rate_limit — retry later… a → b “q” 中文"
    assert ascii_reason(text) == 'provider_rate_limit - retry later... a -> b "q"'
    long = ascii_reason("x" * 500)
    assert len(long) == REASON_MAX_CHARS and long.endswith("...")
    assert ascii_reason(None) == ""
    assert ascii_reason("  a\n\tb  ") == "a b"


def test_report_abort_and_failure_reason_joined_like_the_launcher() -> None:
    out = derive_outcome(
        "failed",
        {"status": "failed", "abort_reason": "provider_rate_limit", "failure_reason": "Executor got HTTP 429"},
    )
    assert out == {
        "outcome": "failed",
        "abort_reason": "provider_rate_limit",
        "failure_reason": "provider_rate_limit | Executor got HTTP 429",
    }


def test_supervisor_failure_reason_is_used_and_lifecycle_wins_over_report_status() -> None:
    out = derive_outcome(
        "failed",
        {"status": "completed"},
        {"status": "failed", "failure_reason": "worker disappeared without a final report"},
    )
    assert out == {"outcome": "failed", "failure_reason": "worker disappeared without a final report"}


def test_supervisor_generated_report_error_is_a_fallback() -> None:
    out = derive_outcome("failed", {"status": "failed", "supervisor_generated": True, "error": "exit 137"})
    assert out == {"outcome": "failed", "failure_reason": "exit 137"}


def test_stopped_line_is_the_last_fallback() -> None:
    tail = "noise\nResult:    failed\nStopped:   provider_stall\nWorkspace: /w\n"
    assert stopped_line(tail) == "provider_stall"
    out = derive_outcome("failed", {}, {}, tail)
    assert out == {"outcome": "failed", "abort_reason": "provider_stall", "failure_reason": "provider_stall"}
    # A report reason beats the log line.
    out = derive_outcome("failed", {"failure_reason": "boom"}, {}, tail)
    assert out == {"outcome": "failed", "failure_reason": "boom"}


def test_completed_run_has_outcome_only_and_live_run_has_nothing() -> None:
    assert derive_outcome("completed", {"status": "completed"}) == {"outcome": "completed"}
    assert derive_outcome("running", {}) == {}
    assert derive_outcome("waiting_approval", {"status": "running"}) == {}
    # Lifecycle not terminal yet, report already terminal: report fills in.
    assert derive_outcome("running", {"status": "cancelled", "abort_reason": "user_cancelled"}) == {
        "outcome": "cancelled",
        "abort_reason": "user_cancelled",
        "failure_reason": "user_cancelled",
    }


def test_read_log_tail_reads_only_the_end_and_refuses_symlinks(tmp_path: Path) -> None:
    log = tmp_path / "worker.log"
    log.write_text("A" * 40000 + "\nStopped:   tail_reason\n", encoding="utf-8")
    tail = read_log_tail(log, max_bytes=1024)
    assert len(tail) <= 1024 and "tail_reason" in tail
    link = tmp_path / "link.log"
    link.symlink_to(log)
    assert read_log_tail(link) == ""
    assert read_log_tail(tmp_path / "missing.log") == ""


def test_cache_parses_a_report_once_and_rereads_after_it_changes(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    logs = run_dir / "lh_harness"
    logs.mkdir(parents=True)
    report = logs / "report.json"
    report.write_text(json.dumps({"status": "failed", "abort_reason": "a1"}), encoding="utf-8")
    calls: list[Path] = []

    def reader(path: Path) -> dict:
        calls.append(path)
        return json.loads(path.read_text(encoding="utf-8"))

    cache = OutcomeCache()
    first = cache.get(run_dir, logs, "failed", {}, reader)
    second = cache.get(run_dir, logs, "failed", {}, reader)
    assert first == second == {"outcome": "failed", "abort_reason": "a1", "failure_reason": "a1"}
    assert len(calls) == 1
    report.write_text(json.dumps({"status": "failed", "abort_reason": "a2-longer"}), encoding="utf-8")
    assert cache.get(run_dir, logs, "failed", {}, reader)["abort_reason"] == "a2-longer"
    assert len(calls) == 2


# --- through the endpoint --------------------------------------------------

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from lh_harness.dashboard.state import DashboardState  # noqa: E402
from lh_harness.supervisor.service import RunSupervisor  # noqa: E402
from lh_harness.webapi.server import create_app  # noqa: E402


def _make_run(
    root: Path,
    run_id: str,
    *,
    status: dict,
    report: dict | None = None,
    worker_log: str | None = None,
) -> None:
    run_dir = root / run_id
    control = run_dir / "control"
    control.mkdir(parents=True)
    (control / "status.json").write_text(json.dumps(status), encoding="utf-8")
    (control / "owner.json").write_text(
        json.dumps({"task": f"task {run_id}", "workspace": "/tmp/ws", "agent": "codex"}), encoding="utf-8"
    )
    logs = run_dir / "lh_harness"
    (logs / "role_orchestration").mkdir(parents=True)
    (logs / "role_orchestration" / "events.jsonl").write_text("", encoding="utf-8")
    if report is not None:
        (logs / "report.json").write_text(json.dumps(report), encoding="utf-8")
    if worker_log is not None:
        (run_dir / "worker.log").write_text(worker_log, encoding="utf-8")


def _client(tmp_path: Path) -> TestClient:
    root = tmp_path / "runs"
    root.mkdir()
    _make_run(
        root,
        "run-ratelimit",
        status={"status": "failed"},
        report={
            "status": "failed",
            "abort_reason": "provider_rate_limit",
            "failure_reason": "Executor: HTTP 429 from kimi — quota " + "x" * 300,
        },
    )
    _make_run(
        root,
        "run-vanished",
        status={"status": "failed", "failure_reason": "worker disappeared without a final report"},
    )
    _make_run(
        root,
        "run-stopped-line",
        status={"status": "failed"},
        report={"status": "failed"},
        worker_log="Result:    failed\nStopped:   provider_stall\n",
    )
    _make_run(root, "run-done", status={"status": "completed"}, report={"status": "completed"})
    _make_run(root, "run-live", status={"status": "running"}, report={"status": "failed", "abort_reason": "old"})
    supervisor = RunSupervisor(root, workspace_root=tmp_path)
    dashboard = DashboardState(root / "run-live", runs_root=root, control_enabled=False)
    app = create_app(state=dashboard, runs_root=root, run_id="run-live", supervisor=supervisor)
    return TestClient(app)


def test_summary_rows_carry_outcome_and_ascii_failure_reason(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/api/runs?fields=summary")
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["runs"]}

    rate = rows["run-ratelimit"]
    assert rate["outcome"] == "failed"
    assert rate["abort_reason"] == "provider_rate_limit"
    assert rate["failure_reason"].startswith("provider_rate_limit | Executor: HTTP 429 from kimi - quota ")
    assert len(rate["failure_reason"]) <= 200
    rate["failure_reason"].encode("ascii")  # raises if not ASCII

    assert rows["run-vanished"]["outcome"] == "failed"
    assert rows["run-vanished"]["failure_reason"] == "worker disappeared without a final report"
    assert "abort_reason" not in rows["run-vanished"]

    assert rows["run-stopped-line"]["abort_reason"] == "provider_stall"
    assert rows["run-stopped-line"]["failure_reason"] == "provider_stall"

    assert rows["run-done"]["outcome"] == "completed"
    assert "failure_reason" not in rows["run-done"]


def test_live_rows_keep_their_old_shape(tmp_path: Path) -> None:
    rows = {row["id"]: row for row in _client(tmp_path).get("/api/runs?fields=summary").json()["runs"]}
    live = rows["run-live"]
    for field in ("outcome", "abort_reason", "failure_reason"):
        assert field not in live
    assert set(live) <= {"id", "status", "updated_at", "workspace", "agent", "model", "max_rounds",
                         "prompt_language", "task_name", "round"}


def test_status_filter_still_applies_with_outcome_fields(tmp_path: Path) -> None:
    rows = _client(tmp_path).get("/api/runs?fields=summary&status=failed").json()["runs"]
    assert {row["id"] for row in rows} == {"run-ratelimit", "run-vanished", "run-stopped-line"}
    assert all(row["outcome"] == "failed" and row["failure_reason"] for row in rows)
