"""Regression test for the web-process wedge on the dashboard runs listing.

``GET /api/runs`` (the full projection) builds one ``DashboardState`` per run
via ``StateRegistry.state_for`` (``webapi/server.py:3500``).  When a run has no
task text on its listing row, ``build_run_summary`` falls back to
``state.snapshot()`` (``webapi/snapshot.py:260``), and ``snapshot()`` calls
``list_runs()`` (``dashboard/state.py:887``) -- a full walk of the runs root
(``iterdir`` + a boundary walk, a ``stat`` and a ``report.json`` read per run,
``dashboard/state.py:319``).

The #179 TTL/lock cache (``LH_HARNESS_RUNS_SCAN_TTL``, default 5 s) was meant
to absorb exactly that, but it stored its ``(timestamp, list)`` pair on the
``DashboardState`` INSTANCE.  ``/api/runs`` creates a brand-new state for
every run, so each one began cold and walked the whole tree again: one
listing of N runs performed N full walks.  With a few hundred run dirs and
several dashboard pollers that is the busy loop that pins a core and starves
the event loop, and every handler times out.

The fix moves the cached listing into a holder shared by every state built
for the same runs root, so one listing performs ONE walk regardless of how
many states -- and therefore how many runs -- it builds.

The assertions are on the SCAN COUNT (deterministic work performed), not on
wall-clock time, so the test is stable on a loaded machine.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import DashboardState
from lh_harness.supervisor.service import RunSupervisor
from lh_harness.webapi.server import create_app
from lh_harness.webapi.snapshot import build_run_summary

RUN_COUNT = 12


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_run(root: Path, run_id: str, *, task: str) -> None:
    """One run directory with NO task text on its listing row.

    ``control/owner.json`` deliberately omits ``task`` and ``report.json``
    omits ``status``, so ``build_run_summary`` must fall back to
    ``state.snapshot()`` for both -- which is the path that reaches
    ``list_runs()``.  The status fallback is driven by an events.jsonl record
    (``_status`` maps ``role_harness_done`` to ``completed``) and the task
    fallback by ``report.json``.
    """

    run_dir = root / run_id
    _write_json(run_dir / "control" / "owner.json", {"workspace": f"/tmp/shared-scan/{run_id}"})
    logs = run_dir / "lh_harness" / "role_orchestration"
    logs.mkdir(parents=True, exist_ok=True)
    _write_json(logs / "report.json", {"task": task})
    with (logs / "events.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "ts": 100.0,
                    "event": "role_harness_done",
                    "round_index": 1,
                    "run_id": run_id,
                    "role": "manager",
                }
            )
            + "\n"
        )
    (logs / "rounds").mkdir(exist_ok=True)


@pytest.fixture()
def wedge_app(tmp_path: Path):
    """A real app over ``RUN_COUNT`` synthetic run dirs in temp space."""

    root = tmp_path / "runs"
    root.mkdir()
    for index in range(RUN_COUNT):
        _make_run(root, f"scan{index:04d}run", task=f"synthetic task {index}")
    state = DashboardState(
        # The state's log_dir is the CANONICAL LOG DIR (``lh_harness``), not
        # the role ledger inside it: ``StateRegistry.state_for`` compares the
        # cached state's log_dir against the walked ``safe_run_logs`` result.
        root / "scan0000run" / "lh_harness",
        runs_root=root,
    )
    app = create_app(
        state=state,
        runs_root=root,
        supervisor=RunSupervisor(root),
        control_enabled=False,
        bind_host="127.0.0.1",
        probe_open_pr=None,
        caller_configs=None,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, root


def test_one_runs_listing_walks_the_runs_root_once(wedge_app) -> None:
    """ONE ``GET /api/runs`` must walk the runs root once, not once per run.

    Pre-fix this request performed one full walk per run (measured: exactly
    ``RUN_COUNT`` walks for ``RUN_COUNT`` runs, because each per-run
    ``DashboardState`` had its own cold cache).  Post-fix the walks are
    shared per runs root, so a single listing walks the tree once.
    """

    client, _root = wedge_app
    walks: list[int] = []
    original = DashboardState._scan_runs_uncached

    def counting_scan(self):
        walks.append(1)
        return original(self)

    with mock.patch.object(DashboardState, "_scan_runs_uncached", counting_scan):
        response = client.get("/api/runs")

    assert response.status_code == 200
    assert len(response.json()["runs"]) == RUN_COUNT
    assert len(walks) == 1, (
        f"one GET /api/runs performed {len(walks)} full runs-root walks for "
        f"{RUN_COUNT} runs; the listing must be scanned once per request, not "
        f"once per run"
    )


def test_scan_count_does_not_grow_with_run_count(wedge_app) -> None:
    """The walk count is constant in the number of runs, not linear in it."""

    client, root = wedge_app
    walks: list[int] = []
    original = DashboardState._scan_runs_uncached

    def counting_scan(self):
        walks.append(1)
        return original(self)

    with mock.patch.object(DashboardState, "_scan_runs_uncached", counting_scan):
        # A second listing inside the TTL: still no additional walk.
        assert client.get("/api/runs").status_code == 200
        first = len(walks)
        assert client.get("/api/runs").status_code == 200
        second = len(walks) - first

    assert first == 1
    assert second == 0


def test_task_fallback_still_resolved(wedge_app) -> None:
    """The fix must preserve the task fallback the hot path relied on.

    ``build_run_summary`` only reaches ``state.snapshot()`` when the listing
    row lacks a task.  Sharing the scan cache must not change what that
    fallback produces: the task still comes from ``report.json`` even though
    the listing row (built by the supervisor) carries no task text.
    """

    client, _root = wedge_app
    response = client.get("/api/runs")
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["runs"]}
    assert len(rows) == RUN_COUNT
    for index in range(RUN_COUNT):
        row = rows[f"scan{index:04d}run"]
        assert row["task"] == f"synthetic task {index}"


def test_status_fallback_still_resolved(wedge_app) -> None:
    """The status fallback from the event log must keep working.

    The supervisor supplies its own status for a listed run, so the
    ``_status(...)`` fallback inside ``build_run_summary`` is exercised
    directly here with an empty status: it must still map a
    ``role_harness_done`` event to ``completed``.
    """

    client, root = wedge_app
    state = DashboardState(root / "scan0000run" / "lh_harness", runs_root=root)
    summary = build_run_summary({"id": "scan0000run", "status": "", "task": ""}, state=state)
    assert summary["status"] == "completed"
    assert summary["task"] == "synthetic task 0"
