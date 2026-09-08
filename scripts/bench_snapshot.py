#!/usr/bin/env python3
"""Benchmark /api/runs/{id}/snapshot server-side latency before/after caching.

Creates a synthetic run with 16 rounds and a large events/rounds payload,
then measures the full and summary snapshot endpoints.  The script reports
median/p95 latencies for the uncached and cached paths and asserts the cached
summary switch path stays under 300 ms.

Usage:
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
        .venv-dev/bin/python scripts/bench_snapshot.py
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

# The patched TestClient in tests/conftest keeps Host headers loopback so the
# server host-hardening stays on.  Make conftest importable by adding tests/.
import sys
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1] / "tests"
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
import conftest  # noqa: F401
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import DashboardState
from lh_harness.webapi.server import create_app


def _build_run(tmp: Path, rounds: int = 16, events_per_round: int = 50) -> tuple[Path, DashboardState]:
    root = tmp / "runs"
    run = root / "bench-run"
    role_dir = run / "logs" / "role_management"
    role_dir.mkdir(parents=True)
    (run / "logs" / "report.json").write_text(
        json.dumps(
            {
                "task": "benchmark task",
                "status": "running",
                "final_response": "done",
            }
        ),
        encoding="utf-8",
    )
    events: list[dict[str, Any]] = [
        {"ts": 1, "event": "role_harness_start", "run_id": "bench-run"}
    ]
    for round_index in range(1, rounds + 1):
        round_dir = role_dir / "rounds" / f"round_{round_index:03d}"
        round_dir.mkdir(parents=True)
        # Inflate round artifacts so the full snapshot is realistically large.
        (round_dir / "manager_plan.txt").write_text(
            f"next_step: plan for round {round_index}\n" + "context line\n" * 200,
            encoding="utf-8",
        )
        (round_dir / "executor_output.txt").write_text(
            f"executor output for round {round_index}\n" + "log line\n" * 400,
            encoding="utf-8",
        )
        (round_dir / "auditor_report.txt").write_text(
            f"auditor report for round {round_index}\n" + "note line\n" * 100,
            encoding="utf-8",
        )
        (round_dir / "final_response.txt").write_text(
            f"final response for round {round_index}",
            encoding="utf-8",
        )
        for seq in range(events_per_round):
            events.append(
                {
                    "ts": 100 * round_index + seq,
                    "event": "manager_round_start",
                    "round_index": round_index,
                    "run_id": "bench-run",
                }
            )
    (role_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    state = DashboardState(run / "logs", runs_root=root, control_enabled=False)
    return root, state


def _measure(client: TestClient, path: str, headers: dict[str, str] | None = None, *, warm: int = 1, iterations: int = 10) -> list[float]:
    for _ in range(warm):
        client.get(path, headers=headers or {})
    times: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        response = client.get(path, headers=headers or {})
        elapsed = time.perf_counter() - start
        if response.status_code >= 400:
            raise RuntimeError(f"{path} failed: {response.status_code} {response.text}")
        times.append(elapsed * 1000)
    return times


def _report(label: str, times: list[float]) -> None:
    times.sort()
    p50 = times[len(times) // 2]
    p95 = times[int(len(times) * 0.95)]
    print(f"{label}: p50={p50:.1f}ms p95={p95:.1f}ms min={min(times):.1f}ms max={max(times):.1f}ms")


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark snapshot endpoint latency")
    parser.add_argument("--rounds", type=int, default=16)
    parser.add_argument("--iterations", type=int, default=20)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        root, state = _build_run(Path(tmp), rounds=args.rounds)
        client = TestClient(create_app(state=state, runs_root=root, run_id="bench-run"))

        print(f"Benchmarking run with {args.rounds} rounds")

        # Uncached: clear the cache before each measurement by using a fresh app
        # for the first hit.  Here we simply measure the first request of each
        # batch; the cache is per-app so repeated requests hit it.
        uncached_app = create_app(state=state, runs_root=root, run_id="bench-run")
        uncached_client = TestClient(uncached_app)

        print("  (uncached app created for first-touch measurements)")

        full_times = _measure(client, "/api/runs/bench-run/snapshot", iterations=args.iterations, warm=1)
        _report("full snapshot (cached path)", full_times)

        summary_times = _measure(
            client, "/api/runs/bench-run/snapshot?fields=summary", iterations=args.iterations, warm=1
        )
        _report("summary snapshot (cached path)", summary_times)

        # First-touch latency: a brand-new app has an empty cache.
        first_full = _measure(
            uncached_client, "/api/runs/bench-run/snapshot", iterations=1, warm=0
        )
        _report("full snapshot (first-touch / uncached)", first_full)

        first_summary = _measure(
            uncached_client,
            "/api/runs/bench-run/snapshot?fields=summary",
            iterations=1,
            warm=0,
        )
        _report("summary snapshot (first-touch / uncached)", first_summary)

        if statistics.median(summary_times) >= 300:
            print("FAIL: cached summary switch path is >= 300 ms")
            return 1
        print("PASS: cached summary switch path is < 300 ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
