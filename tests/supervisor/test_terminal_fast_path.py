"""The full ``GET /api/runs`` enumeration must not reconcile TERMINAL runs.

``RunSupervisor.list_run_items`` is the enumeration behind the FULL
``GET /api/runs`` projection (no ``fields=summary``).  Before the terminal
fast path it called ``self._refresh(run_dir.name)`` for EVERY run directory.

``_refresh`` takes the supervisor-wide ``_lifecycle_lock`` and, per refreshed
run, re-reads the owner record and may fork ``ps`` for the PID-reuse liveness
check.  On CT111 (2026-11-10) 372 of 374 run directories were terminal; every
full listing therefore held the lock for minutes while gateway pollers (10 s
client timeouts) retried every 30 s, saturating the anyio worker pool with
orphaned walks and wedging the whole web API.  Only the ``fields=summary``
path (``list_run_summaries``, no lock) kept answering.

The fast path: when a run's durable ``control/status.json`` already carries a
TERMINAL lifecycle status, the listing uses that record directly.  Terminal
statuses are monotonic (``_refresh`` itself preserves an existing terminal
status over a newer observation), so re-reconciling a finished run cannot
change the answer -- it can only burn the lock.

These tests pin the behaviour by COUNTING, not by wall time:

* a terminal run must NOT enter ``_refresh`` (the counter wraps the real
  method and calls through to it, so the count is honest);
* a live/unknown-status run still gets the full ``_refresh`` reconciliation;
* the listed row for a terminal run still carries the durable status;
* a terminal run whose status.json goes missing falls back to ``_refresh``
  (never silently dropped from the listing).

Run with::

    PYTHONPATH=src pytest tests/supervisor/test_terminal_fast_path.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from lh_harness.supervisor.service import RunSupervisor


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_finished_run(root: Path, run_id: str, status: str = "cancelled") -> Path:
    """One finished run dir shaped like the benchmark fixture."""
    run_dir = root / run_id
    _write_json(
        run_dir / "control" / "status.json",
        {"run_id": run_id, "status": status, "managed": True, "alive": False},
    )
    _write_json(
        run_dir / "control" / "owner.json",
        {
            "run_id": run_id,
            "state": "running",
            "task": f"finished task {run_id}",
            "agent": "codex",
            "model": "gpt-5-codex",
            "workspace": f"/tmp/ws/{run_id}",
            "pid": 0,
        },
    )
    role_dir = run_dir / "lh_harness" / "role_orchestration"
    role_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        role_dir / "report.json",
        {
            "task": f"finished task {run_id}",
            "status": status,
            "final_response": "done",
            "rounds": [{"round_index": 1}],
        },
    )
    (role_dir / "events.jsonl").write_text(
        json.dumps({"round_index": 1, "role": "executor"}) + "\n", encoding="utf-8"
    )
    return run_dir


class _RefreshCounter:
    """Counts real ``RunSupervisor._refresh`` calls, delegating to the real one."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._patch: Any = None

    def __enter__(self) -> "_RefreshCounter":
        original = RunSupervisor._refresh

        def counting_refresh(supervisor, run_id):
            self.calls.append(run_id)
            return original(supervisor, run_id)

        self._patch = mock.patch.object(
            RunSupervisor, "_refresh", counting_refresh
        )
        self._patch.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._patch.stop()

    def reset(self) -> None:
        self.calls.clear()


def test_terminal_runs_do_not_enter_refresh(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    for index in range(6):
        _make_finished_run(root, f"fin{index:02d}run")

    supervisor = RunSupervisor(root)

    with _RefreshCounter() as counter:
        items = supervisor.list_run_items()

    assert sorted(item["id"] for item in items) == sorted(
        f"fin{index:02d}run" for index in range(6)
    )
    assert counter.calls == [], (
        "terminal runs must not be reconciled through _refresh in the full "
        f"listing; refreshed: {counter.calls}"
    )
    for item in items:
        assert item["status"] == "cancelled"


@pytest.mark.parametrize("status", ["cancelled", "failed", "completed"])
def test_terminal_fast_path_keeps_durable_status(
    tmp_path: Path, status: str
) -> None:
    root = tmp_path / "runs"
    _make_finished_run(root, "one-run", status=status)

    supervisor = RunSupervisor(root)
    items = supervisor.list_run_items()

    assert len(items) == 1
    assert items[0]["status"] == status
    assert items[0]["task"] == "finished task one-run"


def test_non_terminal_run_still_refreshes(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run = _make_finished_run(root, "live-run", status="waiting_approval")

    supervisor = RunSupervisor(root)
    with _RefreshCounter() as counter:
        supervisor.list_run_items()

    assert counter.calls == ["live-run"], (
        "a non-terminal run must keep the full _refresh reconciliation; "
        f"refreshed: {counter.calls}"
    )
    assert run.exists()


def test_terminal_run_with_missing_status_file_still_lists(tmp_path: Path) -> None:
    """No durable status at all -> fall back to _refresh, never drop the run."""

    root = tmp_path / "runs"
    run = _make_finished_run(root, "half-run", status="completed")
    (run / "control" / "status.json").unlink()

    supervisor = RunSupervisor(root)
    with _RefreshCounter() as counter:
        items = supervisor.list_run_items()

    assert any(item["id"] == "half-run" for item in items)
    assert counter.calls == ["half-run"]
