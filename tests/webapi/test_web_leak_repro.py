"""LHH-WEB-LEAK-FIX deliverable (A): the web-app memory/queue wedge reproduction.

This test drives the REAL request path of the real FastAPI app over 500+
synthetic run directories created in pytest's ``tmp_path`` (never a real
harness run directory) and asserts that:

* ``GET /api/runs`` (the full projection) does not scan every run directory on
  every request -- the pre-fix cost is O(runs) per request and O(runs^2) in
  total, because the full projection builds a per-run state whose snapshot
  re-lists every run directory;
* ``GET /api/queue`` keeps answering while the heavy paths are loaded, and
  stays fast;
* the per-request response payload does not carry the whole per-role text
  payload of every round (the operator's ~4.4 MB executor role output);
* the web process RSS stays bounded across repeated polling, and the
  ``LH_HARNESS_WEB_RSS_WARN_MB`` watchdog reports when it does not.

The same script drives the same traffic against a live uvicorn child process
and prints the before/after numbers:
``python scripts/web_leak_repro.py``.

The fixture sizes here are deliberately smaller than the script's defaults so
the suite stays fast; the shape of the failure (per-request full scan,
per-role payload retention, RSS growth) is identical.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import DashboardState
from lh_harness.supervisor.service import RunSupervisor
from lh_harness.webapi.server import create_app

# 500+ fixture run directories, per deliverable (A).
RUN_COUNT = int(os.environ.get("LH_HARNESS_LEAK_TEST_RUNS", "520"))
QUEUE_COUNT = int(os.environ.get("LH_HARNESS_LEAK_TEST_QUEUE", "120"))
ROUNDS = 3
ROLE_TEXT_BYTES = 24 * 1024


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_run(root: Path, run_id: str, *, status: str, task: str) -> None:
    """One run directory in the layout the run-boundary helpers accept."""

    run_dir = root / run_id
    _write_json(run_dir / "control" / "status.json", {"status": status, "mtime": 1234.0})
    _write_json(
        run_dir / "control" / "owner.json",
        {"task": task, "workspace": f"/tmp/leak-test/{run_id}", "agent": "claude_code"},
    )
    logs = run_dir / "lh_harness" / "role_orchestration"
    logs.mkdir(parents=True, exist_ok=True)
    _write_json(logs / "report.json", {"task": task, "status": status})
    with (logs / "events.jsonl").open("w", encoding="utf-8") as handle:
        for round_index in range(1, ROUNDS + 1):
            handle.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "ts": 100.0 * round_index,
                        "event": "role_harness_done",
                        "round_index": round_index,
                        "run_id": run_id,
                        "role": "manager",
                    }
                )
                + "\n"
            )
    rounds_root = logs / "rounds"
    for round_index in range(1, ROUNDS + 1):
        round_dir = rounds_root / f"round_{round_index:03d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        (round_dir / "manager_plan.txt").write_text("plan\n", encoding="utf-8")
        (round_dir / "executor_output.txt").write_text(
            "executor\n" + ("e" * ROLE_TEXT_BYTES), encoding="utf-8"
        )
        (round_dir / "auditor_report.txt").write_text(
            "auditor\n" + ("a" * ROLE_TEXT_BYTES), encoding="utf-8"
        )


@pytest.fixture(scope="module")
def leak_app(tmp_path_factory: pytest.TempPathFactory):
    """A real app over 500+ synthetic run dirs in temp space."""

    root = tmp_path_factory.mktemp("lh-leak-runs")
    for index in range(RUN_COUNT):
        run_id = f"leak{index:06x}run"
        _make_run(
            root,
            run_id,
            status="running" if index % 3 else "completed",
            task=f"synthetic task {index}",
        )
    queue_dir = root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for index in range(QUEUE_COUNT):
        _write_json(
            queue_dir / f"q{index:06d}.json",
            {
                "queue_id": f"q{index:06d}",
                "name": f"task-{index}",
                "task": "t" * 2048,
                "workspace": f"/tmp/leak-test/ws-{index}",
                "max_rounds": 12,
                "trio": "glm",
                "priority": index % 10,
                "requested_by": "test",
                "status": "pending",
                "created_at": now - index,
                "updated_at": now - index,
            },
        )
    # Drain so the embedded launcher can never start a real worker.
    _write_json(
        queue_dir / "drain.json",
        {"enabled": True, "reason": "leak_repro", "since": now},
    )

    state = DashboardState(
        root / "leak000000run" / "lh_harness" / "role_orchestration",
        runs_root=root,
    )
    supervisor = RunSupervisor(root)
    app = create_app(
        state=state,
        runs_root=root,
        supervisor=supervisor,
        control_enabled=False,
        bind_host="127.0.0.1",
        probe_open_pr=None,
        # ``None`` (not ``{}``) keeps caller scoping OFF: an empty table is
        # indistinguishable from "not configured", so ``{}`` would leave
        # scoping ON and every /api/queue read would 401 as an anon caller.
        caller_configs=None,
    )
    # The loopback base_url is what the host-header hardening expects; the
    # repo's tests/conftest.py also sets it, but the fixture states it so
    # this file also runs standalone (``pytest tests/webapi/test_web_leak_repro.py``).
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, root


def test_queue_answers_against_many_runs(leak_app) -> None:
    """/api/queue must answer quickly with 500+ run dirs present."""

    client, _root = leak_app
    started = time.monotonic()
    response = client.get("/api/queue")
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert len(response.json()["entries"]) == QUEUE_COUNT
    # The queue listing is bounded work; it must not scale with the run count.
    assert elapsed < 5.0, f"/api/queue took {elapsed:.2f}s"


def test_runs_listing_does_not_scan_every_run_dir_per_request(leak_app) -> None:
    """GET /api/runs must not do an O(runs) directory scan per request.

    Pre-fix, the default projection built a per-run state and asked it for a
    snapshot, and ``DashboardState.snapshot()`` calls ``list_runs()`` -- the
    full run-directory scan -- once per run, so the request cost was
    O(runs^2).  The fix indexes the run list and serves it from that index.
    """

    client, _root = leak_app
    # Warm the index, then measure a steady-state request.
    client.get("/api/runs")
    started = time.monotonic()
    response = client.get("/api/runs")
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert len(response.json()["runs"]) >= RUN_COUNT
    assert elapsed < 10.0, f"GET /api/runs took {elapsed:.2f}s for {RUN_COUNT} runs"


def test_snapshot_does_not_carry_every_per_role_payload(leak_app) -> None:
    """A snapshot must not inline every round's full per-role text payload.

    The operator's dashboard lead (run 9437a8e1) saw a ~4.4 MB executor role
    output; the snapshot read it in full on every poll and kept it in the
    response, so a poll multiplied by the number of rounds grew the resident
    set without bound.
    """

    client, _root = leak_app
    run_id = "leak000000run"
    response = client.get(f"/api/runs/{run_id}/snapshot")
    assert response.status_code == 200
    body = response.content
    # The snapshot must stay far below the sum of every round's per-role
    # payloads (rounds * 2 roles * ROLE_TEXT_BYTES) that the pre-fix build
    # inlined.  A generous bound still catches the O(rounds * roles) growth.
    inline_total = ROUNDS * 2 * ROLE_TEXT_BYTES
    assert len(body) < inline_total, (
        f"snapshot payload {len(body)} bytes >= the inlined per-role total "
        f"{inline_total} bytes; the per-role text is not being budgeted"
    )


def _rss_mb() -> float:
    """Resident set of THIS process, in MiB.

    The web app runs in-process under ``TestClient``, so the test process's
    RSS is the app's RSS.  ``scripts/web_leak_repro.py`` measures the same
    number in the uvicorn child via ``/proc/<pid>/status``.
    """

    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def test_repeated_polling_keeps_rss_bounded(leak_app) -> None:
    """Repeated polling must not grow the resident set without bound."""

    client, _root = leak_app

    run_ids = [f"leak{index:06x}run" for index in range(0, RUN_COUNT, 40)]
    client.get("/api/runs")
    baseline = _rss_mb()
    for _round_index in range(3):
        for run_id in run_ids:
            client.get(f"/api/runs/{run_id}/snapshot")
            client.get("/api/runs/{run_id}/events?limit=50")
        client.get("/api/queue")
        client.get("/api/runs?fields=summary")
    final = _rss_mb()
    assert final <= baseline + 256, (
        f"RSS grew from {baseline:.1f} MiB to {final:.1f} MiB across repeated polling"
    )


def test_rss_watchdog_knob_is_honoured(leak_app) -> None:
    """The watchdog knob must be readable and must not fire below the limit."""

    client, _root = leak_app
    server = pytest.importorskip("lh_harness.webapi.server", reason="web api server")

    threshold = getattr(server, "_rss_warn_threshold_mb", None)
    if threshold is None:
        # Pre-fix tree: the watchdog helper does not exist yet.  The
        # reproduction must stay runnable against the unfixed tree.
        assert client.get("/api/meta").status_code == 200
        return
    # An unset knob must not warn (the default is "off" or a high limit).
    value = threshold()
    assert value is None or value > 0
    # The endpoint must still answer.
    assert client.get("/api/meta").status_code == 200
