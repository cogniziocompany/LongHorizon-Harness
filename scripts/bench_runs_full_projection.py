#!/usr/bin/env python3
"""Micro-benchmark for the FULL ``GET /api/runs`` projection.

``GET /api/runs`` with no ``fields`` parameter returns complete run objects.
Every row is produced by ``build_run_summary(item, state=registry.state_for(id))``
(``webapi/server.py``), and ``state_for`` builds/refreshes one ``DashboardState``
per run.  This script builds N synthetic run directories in a TEMP directory
(nothing is written under the repo), drives that exact production path through
``create_app`` + ``TestClient``, and reports:

  * wall time for one warm full listing, total and per run;
  * the work counters behind it -- ``build_run_summary`` calls,
    ``DashboardState.snapshot()`` calls, runs-root scans (``_scan_runs``),
    ``os.stat`` calls and ``json.loads`` calls -- so the cost is expressed as
    deterministic work, not only as a machine-dependent duration.

Usage:
    PYTHONPATH=src python3 scripts/bench_runs_full_projection.py
    PYTHONPATH=src python3 scripts/bench_runs_full_projection.py --runs 50 372

The fixture directory is a ``tempfile.mkdtemp()`` tree and is removed at the
end; the script writes nothing into the checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_TESTS_ROOT = Path(__file__).resolve().parents[1] / "tests"
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
import conftest  # noqa: F401  (loopback TestClient Host patch)

from fastapi.testclient import TestClient

from lh_harness.dashboard import state as dashboard_state
from lh_harness.dashboard.state import DashboardState
from lh_harness.supervisor import control_bus
from lh_harness.supervisor.service import RunSupervisor
from lh_harness.webapi import server as webapi_server
from lh_harness.webapi.server import create_app

# How many synthetic run dirs to measure.  372 is the CT111 run count from the
# incident; 50 is the small control that shows the per-run slope.
DEFAULT_SIZES = (50, 372)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_run(root: Path, run_id: str, index: int) -> None:
    """One synthetic run directory shaped like a real, finished run.

    ``control/status.json`` + ``control/owner.json`` are what the supervisor
    listing reads; ``lh_harness/role_orchestration/report.json`` and
    ``events.jsonl`` are what the per-run state reads.  ``owner.json`` carries
    no ``task`` so ``list_run_items`` falls back to ``report.json`` exactly as
    it does for a real run.
    """

    run_dir = root / run_id
    _write_json(
        run_dir / "control" / "status.json",
        {"run_id": run_id, "status": "completed", "managed": True, "alive": False},
    )
    # Production shape: ``RunSupervisor._launch_worker`` writes the reservation
    # as owner.json and that reservation carries ``task`` (and the launch
    # provenance fields).  ``pid`` is 0 for a finished run, so the liveness
    # probe short-circuits without spawning ``ps``.
    _write_json(
        run_dir / "control" / "owner.json",
        {
            "run_id": run_id,
            "state": "running",
            "task": f"synthetic benchmark task {index}",
            "agent": "codex",
            "model": "gpt-5-codex",
            "role_configs": {
                role: {"agent": "codex", "model": "gpt-5-codex"}
                for role in ("manager", "executor", "auditor")
            },
            "max_rounds": 8,
            "prompt_language": "en",
            "workspace": f"/tmp/bench-workspace/{run_id}",
            "pid": 0,
        },
    )
    role_dir = run_dir / "lh_harness" / "role_orchestration"
    role_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "task": f"synthetic benchmark task {index}",
        "status": "completed",
        "final_response": f"benchmark run {index} finished",
        "rounds": [{"round_index": 1}, {"round_index": 2}],
    }
    # The manager writes report.json twice (log dir + role ledger) and
    # ``DashboardState.read_report`` reads the log-dir copy first, so a faithful
    # fixture carries both.
    _write_json(run_dir / "lh_harness" / "report.json", report)
    _write_json(role_dir / "report.json", report)
    with (role_dir / "events.jsonl").open("w", encoding="utf-8") as handle:
        for round_index in (1, 2):
            handle.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "ts": 100.0 + round_index,
                        "event": "role_harness_done",
                        "round_index": round_index,
                        "run_id": run_id,
                        "role": "manager",
                    }
                )
                + "\n"
            )
    for round_index in (1, 2):
        round_dir = role_dir / "rounds" / f"round_{round_index:03d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        (round_dir / "manager_plan.txt").write_text("Next: continue\n", encoding="utf-8")


class _Counters:
    """Deterministic work counters for one measured request.

    Every entry is a COUNT, so the result is reproducible on a loaded machine
    where wall-clock timings are not.  ``scan_walks`` is the number of full
    runs-root walks: the #193 shared scan cache is expected to keep that at 1
    even when every run builds its own ``DashboardState``.
    """

    def __init__(self) -> None:
        self.list_run_items_calls = 0
        self.build_summary_calls = 0
        self.snapshot_calls = 0
        self.scan_runs_calls = 0
        self.scan_walks = 0
        self.report_reads = 0
        self.approvals_reads = 0
        self.events_reads = 0
        self.owner_reads = 0
        self.json_loads = 0
        self.stat_calls = 0
        self.trajectory_path_probes = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@contextmanager
def _instrument() -> Iterator[_Counters]:
    """Count the work the full projection performs, without changing it."""

    counters = _Counters()

    original_build = webapi_server.build_run_summary
    original_snapshot = DashboardState.snapshot
    original_scan = DashboardState._scan_runs
    original_scan_uncached = DashboardState._scan_runs_uncached
    original_list_items = RunSupervisor.list_run_items
    original_report = DashboardState.read_report
    original_approvals = DashboardState.list_approvals
    original_events = DashboardState.read_events
    original_traj = DashboardState._trajectory_path
    original_owner = control_bus.ControlBus.read_owner
    original_json_loads = json.loads
    original_stat = os.stat

    def counting_build(item, *, state=None):
        counters.build_summary_calls += 1
        return original_build(item, state=state)

    def counting_snapshot(self):
        counters.snapshot_calls += 1
        return original_snapshot(self)

    def counting_scan(self):
        counters.scan_runs_calls += 1
        return original_scan(self)

    def counting_scan_uncached(self):
        counters.scan_walks += 1
        return original_scan_uncached(self)

    def counting_list_items(self):
        counters.list_run_items_calls += 1
        return original_list_items(self)

    def counting_report(self):
        counters.report_reads += 1
        return original_report(self)

    def counting_approvals(self):
        counters.approvals_reads += 1
        return original_approvals(self)

    def counting_events(self, **kwargs):
        counters.events_reads += 1
        return original_events(self, **kwargs)

    def counting_traj(round_dir, role):
        counters.trajectory_path_probes += 1
        return original_traj(round_dir, role)

    def counting_owner(self):
        counters.owner_reads += 1
        return original_owner(self)

    def counting_json_loads(*args, **kwargs):
        counters.json_loads += 1
        return original_json_loads(*args, **kwargs)

    def counting_stat(*args, **kwargs):
        counters.stat_calls += 1
        return original_stat(*args, **kwargs)

    webapi_server.build_run_summary = counting_build
    DashboardState.snapshot = counting_snapshot
    DashboardState._scan_runs = counting_scan
    DashboardState._scan_runs_uncached = counting_scan_uncached
    RunSupervisor.list_run_items = counting_list_items
    DashboardState.read_report = counting_report
    DashboardState.list_approvals = counting_approvals
    DashboardState.read_events = counting_events
    DashboardState._trajectory_path = staticmethod(counting_traj)
    control_bus.ControlBus.read_owner = counting_owner
    json.loads = counting_json_loads
    os.stat = counting_stat
    try:
        yield counters
    finally:
        webapi_server.build_run_summary = original_build
        DashboardState.snapshot = original_snapshot
        DashboardState._scan_runs = original_scan
        DashboardState._scan_runs_uncached = original_scan_uncached
        RunSupervisor.list_run_items = original_list_items
        DashboardState.read_report = original_report
        DashboardState.list_approvals = original_approvals
        DashboardState.read_events = original_events
        DashboardState._trajectory_path = original_traj
        control_bus.ControlBus.read_owner = original_owner
        json.loads = original_json_loads
        os.stat = original_stat


def _build_app(root: Path):
    state = DashboardState(root / "bench0000run" / "lh_harness", runs_root=root)
    app = create_app(
        state=state,
        runs_root=root,
        supervisor=RunSupervisor(root),
        control_enabled=False,
        bind_host="127.0.0.1",
        probe_open_pr=None,
        caller_configs=None,
    )
    return app


def _measure(root: Path, repeats: int) -> dict[str, Any]:
    """Wall time and work counts for the full listing, measured separately.

    Wall clock is measured WITHOUT instrumentation: patching ``os.stat`` and
    ``json.loads`` would inflate the very number being reported.  The work
    counters come from separate, instrumented requests whose own duration is
    discarded.
    """

    # 1. Cold wall clock on a fresh app (no instrumentation).
    with TestClient(_build_app(root), base_url="http://127.0.0.1") as client:
        started = time.perf_counter()
        first = client.get("/api/runs")
        cold_seconds = time.perf_counter() - started
        if first.status_code != 200:
            raise SystemExit(f"GET /api/runs failed: {first.status_code} {first.text[:400]}")
        rows = len(first.json()["runs"])
        body_bytes = len(first.content)

    # 2. Cold work counts on a fresh app (counters only; duration discarded).
    with TestClient(_build_app(root), base_url="http://127.0.0.1") as client:
        with _instrument() as counters:
            counted = client.get("/api/runs")
        if counted.status_code != 200:
            raise SystemExit(f"GET /api/runs failed: {counted.status_code}")
        cold_counters = counters.as_dict()

        # 3. Warm wall clock (no instrumentation).
        timings: list[float] = []
        for _ in range(repeats):
            started = time.perf_counter()
            response = client.get("/api/runs")
            timings.append(time.perf_counter() - started)
            if response.status_code != 200:
                raise SystemExit(f"GET /api/runs failed: {response.status_code}")
        rows = max(rows, len(response.json()["runs"]))

        # 4. Warm work counts.
        with _instrument() as counters:
            warm = client.get("/api/runs")
        if warm.status_code != 200:
            raise SystemExit(f"GET /api/runs failed: {warm.status_code}")
        warm_counters = counters.as_dict()

    return {
        "rows": rows,
        "median_seconds": statistics.median(timings),
        "min_seconds": min(timings),
        "max_seconds": max(timings),
        "cold_seconds": cold_seconds,
        "cold_counters": cold_counters,
        "warm_counters": warm_counters,
        "counters": warm_counters,
        "body_bytes": body_bytes,
    }


def _stage_breakdown(root: Path, repeats: int = 2) -> list[dict[str, Any]]:
    """Time each stage of the warm full listing separately.

    The handler (``webapi/server.py`` ``runs()``) runs exactly three stages per
    request: ``supervisor.list_run_items()`` (one row per run), then, per row,
    ``StateRegistry.state_for()`` and ``build_run_summary()``.  Timing them
    individually shows WHICH one makes the request expensive, which a single
    total cannot.
    """

    from lh_harness.webapi.server import StateRegistry
    from lh_harness.webapi.snapshot import build_run_summary

    supervisor = RunSupervisor(root)
    state = DashboardState(root / "bench0000run" / "lh_harness", runs_root=root)
    registry = StateRegistry(state=state, runs_root=root, supervisor=supervisor)
    stages: list[dict[str, Any]] = []

    def timed(label: str, call) -> Any:
        best = float("inf")
        value = None
        for _ in range(repeats):
            started = time.perf_counter()
            value = call()
            best = min(best, time.perf_counter() - started)
        stages.append({"stage": label, "seconds": best})
        return value

    items = timed("supervisor.list_run_items() (full rows)", supervisor.list_run_items)
    timed("supervisor.list_run_summaries() (cheap rows)", supervisor.list_run_summaries)
    states = timed(
        "StateRegistry.state_for() per run",
        lambda: [registry.state_for(str(item.get("id") or "")) for item in items],
    )
    timed(
        "build_run_summary() per run (with state)",
        lambda: [build_run_summary(item, state=st) for item, st in zip(items, states)],
    )
    timed(
        "build_run_summary() per run (state=None)",
        lambda: [build_run_summary(item, state=None) for item in items],
    )
    return stages


def run_size(count: int, repeats: int) -> dict[str, Any]:
    """Build ``count`` synthetic run dirs in a TEMP dir and measure the full path."""
    tmp = tempfile.mkdtemp(prefix=f"lh-runs-full-bench-{count}-")
    root = Path(tmp) / "runs"
    root.mkdir(parents=True)
    try:
        started = time.perf_counter()
        for index in range(count):
            _make_run(root, f"bench{index:04d}run", index)
        fixture_seconds = time.perf_counter() - started
        measured = _measure(root, repeats)
        stages = _stage_breakdown(root)
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    measured.update(
        {
            "run_count": count,
            "fixture_seconds": fixture_seconds,
            "per_run_ms": (measured["median_seconds"] * 1000.0) / max(1, measured["rows"]),
            "stages": stages,
        }
    )
    return measured


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs",
        type=int,
        nargs="+",
        default=list(DEFAULT_SIZES),
        help="synthetic run-dir counts to measure (default: 50 and 372)",
    )
    parser.add_argument("--repeats", type=int, default=3, help="warm listings per size")
    parser.add_argument("--json", type=Path, default=None, help="also write the raw result here")
    args = parser.parse_args(argv)

    results = []
    for count in args.runs:
        result = run_size(count, args.repeats)
        results.append(result)
        print(f"\n=== N={count} synthetic run dirs (median of {args.repeats} warm listings) ===")
        print(f"rows returned              : {result['rows']}")
        print(f"fixture build              : {result['fixture_seconds'] * 1000.0:.0f} ms")
        print(f"cold full listing          : {result['cold_seconds'] * 1000.0:.1f} ms"
              f"  ({result['cold_seconds'] * 1000.0 / max(1, result['rows']):.3f} ms/run)")
        print(f"warm full listing          : {result['median_seconds'] * 1000.0:.1f} ms"
              f"  (min {result['min_seconds'] * 1000.0:.1f}, max {result['max_seconds'] * 1000.0:.1f})")
        print(f"per run (warm)             : {result['per_run_ms']:.3f} ms/run")
        print(f"response body              : {result['body_bytes'] / 1024.0:.0f} KiB")
        print("work per warm listing:")
        for key, value in result["warm_counters"].items():
            print(f"  {key:24s}: {value}")
        print("work per cold listing:")
        for key, value in result["cold_counters"].items():
            print(f"  {key:24s}: {value}")
        print("stage breakdown (best of 2, warm):")
        for stage in result["stages"]:
            per_run = stage["seconds"] * 1000.0 / max(1, result["rows"])
            print(f"  {stage['stage']:44s}: {stage['seconds'] * 1000.0:8.1f} ms  ({per_run:.3f} ms/run)")

    if args.json is not None:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nraw results written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
