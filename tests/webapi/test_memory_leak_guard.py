"""LHH-WEB-LEAK regression guard.

The fleet gateway started failing on a ~25 MB ``/api/queue`` response while the
underlying lh-harness web process grew to 5.6 GB RSS in ``D`` state and OOM'd
the host (incident 2026-10-07).  PR #120 bounded the snapshot cache; this
guard:

* Confirms ``GET /api/meta`` now reports the web process RSS so an Overseer
  tick can alert before OOM.
* Confirms the threshold warning log fires once when RSS crosses the line.
* Hammers the routes that previously retained state (snapshots, runs list,
  ``/api/meta``, ``/api/queue``) and asserts neither RSS nor ``tracemalloc``
  peak grows unboundedly across N repeated calls.

The test is hermetic: a synthetic fixture of small runs is built in a
``tmp_path`` so the suite stays self-contained and fast.
"""

from __future__ import annotations

import json
import logging
import os
import tracemalloc
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lh_harness.webapi import process_memory as process_memory_module
from lh_harness.webapi.server import create_app


# 64 MiB ceiling for RSS growth across N requests.  The leak grew into
# multi-GB territory; a 64 MiB ceiling after a warm-up window catches any
# regression that re-introduces an unbounded retainer while leaving plenty
# of headroom for legitimate cache growth.
_RSS_GROWTH_BUDGET_BYTES = 64 * 1024 * 1024
# 16 MiB ceiling for ``tracemalloc`` peak delta.  tracemalloc tracks Python
# allocations only; this catches in-process leaks that RSS misses (e.g. an
# ``OrderedDict`` that grows but the OS reuses the freed pages).
_TRACEMALLOC_GROWTH_BUDGET_BYTES = 16 * 1024 * 1024
# 20 iterations across 4 runs is enough to amortise warm-up while keeping
# the test under 20 s.  The hot loop bumps the events.jsonl mtime so the
# snapshot cache's filesystem signature changes every iteration, forcing
# the cache to rebuild the snapshot instead of returning the cached copy.
_REQUEST_COUNT = 20


def _build_run(tmp_path: Path, run_id: str) -> None:
    """Materialize a minimal run directory the snapshot builder can read."""

    role_dir = tmp_path / run_id / "lh_harness" / "role_orchestration"
    role_dir.mkdir(parents=True)
    events = role_dir / "events.jsonl"
    # A handful of events so the snapshot is non-trivial; the test only
    # cares about retention, not the content.
    payload = [
        {"ts": 1700000000.0 + i, "kind": "info", "message": f"event {i}"}
        for i in range(5)
    ]
    events.write_text("\n".join(json.dumps(e) for e in payload) + "\n", encoding="utf-8")
    (role_dir / "final_response.txt").write_text("done\n", encoding="utf-8")
    (role_dir / "report.json").write_text(
        json.dumps({"status": "ok", "summary": "ok"}), encoding="utf-8"
    )
    # A round directory with a couple of artifacts.
    rounds = role_dir / "rounds" / "round_001"
    rounds.mkdir(parents=True)
    (rounds / "manager_plan.txt").write_text("plan\n", encoding="utf-8")
    (rounds / "task_contract.txt").write_text("contract\n", encoding="utf-8")
    (rounds / "auditor_report.txt").write_text("ok\n", encoding="utf-8")


@pytest.fixture
def _fixture_runs_root(tmp_path: Path) -> Path:
    """Synthesize 4 runs in a tmp_path so the snapshot list is non-trivial."""

    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    for i in range(4):
        _build_run(runs_root, f"run-{i:04d}")
    return runs_root


@pytest.fixture(autouse=True)
def _restore_process_memory_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    process_memory_module.reset_cache_for_tests()
    # Tests must never inherit a real ``LH_HARNESS_WEB_RSS_WARN_BYTES`` from
    # the host environment; a lower threshold means the warning fires on
    # the test process itself.
    monkeypatch.delenv("LH_HARNESS_WEB_RSS_WARN_BYTES", raising=False)
    yield
    process_memory_module.reset_cache_for_tests()


def _client(runs_root: Path) -> TestClient:
    # ``bind_host="testserver"`` matches the Host header TestClient sends;
    # the loopback host check in the security middleware rejects any other
    # value (see tests/webapi/test_overseer_state.py for the same idiom).
    app = create_app(runs_root=runs_root, bind_host="testserver")
    return TestClient(app)


def test_api_meta_reports_rss_field(_fixture_runs_root: Path) -> None:
    client = _client(_fixture_runs_root)

    meta = client.get("/api/meta").json()

    memory = meta.get("memory")
    assert memory is not None, "/api/meta must expose a 'memory' block (LHH-WEB-LEAK)"
    assert set(memory) >= {
        "rss_bytes",
        "rss_human",
        "threshold_bytes",
        "threshold_human",
        "over_threshold",
    }
    # RSS may legitimately be None on non-Linux; on Linux it must be a
    # positive integer.
    rss = memory["rss_bytes"]
    if rss is not None:
        assert rss > 0
        assert memory["rss_human"].endswith("B")
        assert memory["threshold_bytes"] > 0
        assert memory["over_threshold"] is False


def test_api_meta_warning_log_fires_when_threshold_lowered(
    _fixture_runs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Set the threshold to 1 byte so any RSS reading crosses it.
    monkeypatch.setenv("LH_HARNESS_WEB_RSS_WARN_BYTES", "1")
    client = _client(_fixture_runs_root)

    with caplog.at_level(logging.WARNING, logger="lh_harness.webapi.process_memory"):
        # Bypass the cache so the warning path actually runs.
        client.get("/api/meta")
        # Repeat: edge-trigger means the warning does not re-fire while RSS
        # stays above the threshold.
        client.get("/api/meta")

    warnings = [
        record
        for record in caplog.records
        if record.name == "lh_harness.webapi.process_memory"
        and "RSS" in record.getMessage()
    ]
    assert len(warnings) == 1, (
        f"expected exactly one warning log, got {len(warnings)}: "
        f"{[r.getMessage() for r in warnings]}"
    )


def test_repeated_routes_do_not_leak_memory(_fixture_runs_root: Path) -> None:
    """Hammer the heavy routes; assert RSS + tracemalloc stay bounded.

    This is the regression test for the 5.6 GB leak.  The cache bound added
    in PR #120 keeps the working set small; any future regression that
    re-introduces an unbounded retainer (forgotten ``lru_cache`` on big
    payloads, an unbounded subscriber list, response bodies held in memory)
    must fail this test before it reaches production.
    """

    client = _client(_fixture_runs_root)
    run_ids = sorted(p.name for p in (_fixture_runs_root).iterdir() if p.is_dir())
    assert run_ids, "fixture must contain at least one run"

    # Warm-up: hit each route once so the snapshot cache, model catalog
    # cache, and any other first-call-only initialisation settle.
    for _ in range(3):
        client.get("/api/meta")
        client.get("/api/runs")
        client.get(f"/api/runs/{run_ids[0]}/snapshot")
        client.get("/api/queue")
        client.get("/api/queue/config")

    # Reset tracemalloc after warm-up so the measurement covers the hot
    # loop only, not first-call initialisation.
    tracemalloc.stop()
    tracemalloc.start()
    baseline_rss = process_memory_module.read_rss_bytes(force=True)
    tracemalloc_peak_baseline = tracemalloc.get_traced_memory()[1]

    # Hot loop: hammer the heavy routes while bumping the events.jsonl
    # mtime so the snapshot cache signature changes every iteration --
    # this forces the rebuild path on every call instead of relying on the
    # 2 s TTL, which would otherwise make the test slow.
    import time

    started = time.monotonic()
    rss_samples: list[int] = []
    for i in range(_REQUEST_COUNT):
        run_id = run_ids[i % len(run_ids)]
        # Bump the events file mtime to invalidate the snapshot cache.
        events_path = _fixture_runs_root / run_id / "lh_harness" / "role_orchestration" / "events.jsonl"
        try:
            os.utime(events_path, None)
        except OSError:
            pass
        client.get("/api/meta")
        client.get("/api/runs")
        client.get(f"/api/runs/{run_id}/snapshot")
        client.get("/api/queue")
        client.get("/api/queue/config")
        if i % 20 == 0:
            rss_samples.append(process_memory_module.read_rss_bytes(force=True) or 0)
    elapsed = time.monotonic() - started

    final_rss = process_memory_module.read_rss_bytes(force=True) or 0
    tracemalloc_peak = tracemalloc.get_traced_memory()[1]

    rss_growth = final_rss - (baseline_rss or 0)
    tracemalloc_growth = tracemalloc_peak - tracemalloc_peak_baseline

    # Log so a failure shows the actual numbers without a debugger.
    print(
        f"\n[lhh-web-leak] baseline_rss={baseline_rss} final_rss={final_rss} "
        f"growth={rss_growth} samples={rss_samples} "
        f"tracemalloc_growth={tracemalloc_growth} elapsed={elapsed:.2f}s"
    )

    assert rss_growth < _RSS_GROWTH_BUDGET_BYTES, (
        f"RSS grew by {rss_growth} bytes across {_REQUEST_COUNT} requests "
        f"(budget {_RSS_GROWTH_BUDGET_BYTES}). The web process is leaking again."
    )
    assert tracemalloc_growth < _TRACEMALLOC_GROWTH_BUDGET_BYTES, (
        f"tracemalloc peak grew by {tracemalloc_growth} bytes across "
        f"{_REQUEST_COUNT} requests (budget {_TRACEMALLOC_GROWTH_BUDGET_BYTES}). "
        f"The web process is retaining Python objects."
    )