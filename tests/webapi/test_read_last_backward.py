"""Backward-scan fast path for ``EventTailer.read_last(1)`` (easybutt0n-17).

``GET /api/runs/latest`` pays per-active-row work on every poll and the tail
read was the dominant remaining per-row cost: ``read_last(1)`` normalized
every record in the 64 KiB tail window (~80 records) to return one.  The
fast path scans the chunk backward and stops at the first complete record.

Covered here:

- ``read_last(1)`` returns the same final complete record the whole-window
  parse returns (event_id/type/ts/round/role);
- ``normalize_event`` runs exactly once per ``read_last(1)`` call, not once
  per record in the window;
- a truncated final line is skipped and the previous complete record is
  returned;
- ``read_last(n > 1)`` still uses the whole-window path and returns the same
  envelopes as before;
- ``GET /api/runs/latest`` over 200 running runs stays inside the liveness
  budget after one warm-up call.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

import lh_harness.webapi.events as events_module
from lh_harness.dashboard.state import DashboardState
from lh_harness.supervisor.service import RunSupervisor
from lh_harness.webapi.events import EventTailer
from lh_harness.webapi.server import create_app

# Reuse the runs-latest fixture helpers so both suites build runs the same way.
from tests.webapi.test_runs_latest import _event, _make_run

_RUN_ID = "run-backward"


def _write_events(path: Path, records: list[dict], *, truncated_tail: str | None = None) -> None:
    """Write one JSONL record per line, optionally a final line with no newline."""

    body = "".join(json.dumps(record) + "\n" for record in records)
    if truncated_tail is not None:
        body += truncated_tail
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _records(count: int) -> list[dict]:
    return [
        _event(
            1_000.0 + index,
            name="manager_round_start",
            round_no=index + 1,
            role="manager",
            run_id=_RUN_ID,
        )
        | {"event_id": f"{_RUN_ID}:{index:06d}"}
        for index in range(count)
    ]


@pytest.fixture()
def events_path(tmp_path: Path) -> Path:
    """An events.jsonl with ~80 complete records plus a truncated final line."""

    path = tmp_path / "runs" / _RUN_ID / "lh_harness" / "role_orchestration" / "events.jsonl"
    _write_events(path, _records(80), truncated_tail='{"schema_version": 2, "event": "manager_rou')
    return path


def _whole_window_last(path: Path, wanted: int) -> list:
    """What main returns today: parse the whole file, keep the last ``wanted``."""

    raw = path.read_bytes()
    tailer = EventTailer(path, run_id=_RUN_ID)
    events = tailer._parse_tail_records(raw, base_offset=0, head_truncated=False)
    return events[-wanted:]


def test_read_last_1_returns_final_complete_event(events_path: Path) -> None:
    """The fast path returns the last COMPLETE record, not the truncated tail."""

    tailer = EventTailer(events_path, run_id=_RUN_ID)
    fast = tailer.read_last(1)
    expected = _whole_window_last(events_path, 1)
    assert len(fast) == 1
    assert len(expected) == 1
    assert fast[0].event_id == expected[0].event_id
    assert fast[0].type == expected[0].type
    assert fast[0].ts == pytest.approx(expected[0].ts)
    assert fast[0].round == expected[0].round
    assert fast[0].role == expected[0].role
    # The truncated final line (offset past the last newline) is never returned.
    assert fast[0].event_id == f"{_RUN_ID}:000079"


def test_read_last_1_does_not_parse_whole_window(
    events_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``normalize_event`` runs once per call, not once per window record."""

    calls = []
    original = events_module.normalize_event

    def counting_normalize(record, **kwargs):
        calls.append(record.get("event_id"))
        return original(record, **kwargs)

    monkeypatch.setattr(events_module, "normalize_event", counting_normalize)
    tailer = EventTailer(events_path, run_id=_RUN_ID)
    fast = tailer.read_last(1)
    assert len(fast) == 1
    # Main's whole-window parse normalizes all ~80 records; the backward scan
    # stops at the first complete record from the end.
    assert len(calls) == 1
    assert calls[0] == f"{_RUN_ID}:000079"


def test_read_last_1_skips_incomplete_tail(tmp_path: Path) -> None:
    """A final line without a newline is ignored; the previous record wins."""

    path = tmp_path / "events.jsonl"
    records = _records(3)
    _write_events(path, records, truncated_tail='{"schema_version": 2, "event": "trunca')
    tailer = EventTailer(path, run_id=_RUN_ID)
    fast = tailer.read_last(1)
    expected = _whole_window_last(path, 1)
    assert len(fast) == 1
    assert fast[0].event_id == expected[0].event_id == f"{_RUN_ID}:000002"


def test_read_last_n_still_whole_window(events_path: Path) -> None:
    """``read_last(5)`` keeps the whole-window fallback path and its output."""

    tailer = EventTailer(events_path, run_id=_RUN_ID)
    fast = tailer.read_last(5)
    expected = _whole_window_last(events_path, 5)
    assert len(fast) == 5
    assert fast == expected


def test_latest_route_budget(tmp_path: Path) -> None:
    """``GET /api/runs/latest`` with 200 running runs stays inside the budget."""

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    now = time.time()
    for index in range(200):
        run_id = f"run-budget-{index:03d}"
        _make_run(
            root,
            run_id,
            status="running",
            events=[
                _event(
                    now - 60.0,
                    name="executor_role_start",
                    round_no=1,
                    role="executor",
                    run_id=run_id,
                )
            ],
        )
    supervisor = RunSupervisor(root, workspace_root=tmp_path)
    dashboard = DashboardState(root / "run-budget-000" / "lh_harness", runs_root=root, control_enabled=False)
    app = create_app(
        state=dashboard, runs_root=root, run_id="run-budget-000", supervisor=supervisor
    )
    client = TestClient(app)

    warm = client.get("/api/runs/latest")
    assert warm.status_code == 200
    assert len(warm.json()["runs"]) == 200

    start = time.perf_counter()
    response = client.get("/api/runs/latest")
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert response.status_code == 200
    assert len(response.json()["runs"]) == 200
    assert elapsed_ms < 500.0, f"latest route took {elapsed_ms:.0f} ms for 200 runs"
