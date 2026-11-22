"""Scale budget for the runs listings: cost independent of run-dir size.

CT111 (2026-11-21): the web process wedged at 110-140% CPU with 196 run dirs
(10.8 GB, 35,220 files); ``GET /api/runs`` and ``GET /api/runs/latest``
stopped answering (25-45 s timeouts) while ``/api/meta`` stayed fast.  The
merged caches (#179 dashboard scan TTL, #193 shared scan holder, #194 bounded
projection, #195 terminal fast path) each removed one per-request walk, but
every poll still re-enumerated the whole runs root and re-derived every row,
and a run whose records carried no task text made the full listing walk the
ROUND DIRECTORIES per request (``_saved_task_from_rounds``).

The deliverable this module pins: both ``GET /api/runs/latest`` and the
summary ``GET /api/runs?fields=summary`` listing must be served from a small
per-run summary refreshed only when that run's own status record moves, never
by walking or parsing round/transcript artifacts, and the full ``GET /api/runs``
must not re-derive every row from the run directories on each request either.

Budgets (wall time, generous machine tolerance) at N=200 synthetic run dirs
with large per-round files:

* under 3 s cold (first request against a fresh app);
* under 500 ms warm (repeat request);
* no round/transcript file opened while servicing a request;
* after a TTL expiry with nothing changed, the listing does not re-derive
  rows by walking the round directories again.

The fixture lives entirely under pytest's tmp storage.  It does NOT recreate
the CT111 byte-for-byte scale (10.8 GB would make the suite unrunnable); it
recreates the shape that drives the cost: many run dirs, each holding a
rounds/ tree with multi-hundred-KB artifacts, and records that make the full
listing fall back to the rounds walk (no task text in owner/report).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import DashboardState
from lh_harness.supervisor import service as supervisor_service
from lh_harness.supervisor.service import RunSupervisor
from lh_harness.webapi.server import create_app

# Deliverable scale: 200 synthetic run dirs (the CT111 incident had 196).
RUN_COUNT = 200
# A minority of ACTIVE runs mirrors a busy node (most runs are historical);
# /api/runs/latest reports ACTIVE rows only, so the active set must be large
# enough for that endpoint's per-row work to be measurable.
ACTIVE_RUNS = 60
# File-count scale mirrors the incident proportionally: CT111 held 35,220
# files across 196 run dirs (~180 files/run); this fixture builds ~25,000
# files across 200 run dirs (~125 files/run) at ~336 MB total -- the walk
# cost a listing pays per run dir is what pinned the web process, and this
# keeps the suite runnable (build ~3 s, tmp storage only).
ROUNDS_PER_RUN = 24
ROUND_KB = 32

WARM_BUDGET_S = 0.5
COLD_BUDGET_S = 3.0

_summary_shape_fields = frozenset(
    {"id", "status", "workspace", "updated_at", "report_status", "completion_satisfied"}
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_run(root: Path, run_id: str, index: int, *, active: bool) -> None:
    """One synthetic run dir shaped like the CT111 wedge measurement.

    ``control/owner.json`` deliberately carries NO ``task`` text and
    ``report.json`` carries none either: that is the historical shape which
    made ``list_run_items`` fall back to ``_saved_task_from_rounds`` -- a full
    anchored walk of the run's round directories -- on EVERY request.  The
    ``task_contract.txt`` files inside the round dirs give that fallback
    something to find, so the fallback actually pays for the walk.
    """

    run_dir = root / run_id
    status = "running" if active else ("failed" if index % 3 == 0 else "completed")
    # ``managed: False`` marks these as historical/imported runs.  That matters
    # for correctness of the measurement, not just realism: a managed run with
    # a dead pid gets lifecycle-reconciled by ``_refresh`` (a status.json
    # rewrite) the first time an endpoint touches it, which would mutate the
    # shared fixture mid-module and make the run's row flap between tests.
    _write_json(
        run_dir / "control" / "status.json",
        {"run_id": run_id, "status": status, "managed": False, "alive": active},
    )
    _write_json(
        run_dir / "control" / "owner.json",
        {
            "run_id": run_id,
            "state": "running" if active else "stopped",
            "agent": "codex",
            "model": "gpt-5-codex",
            "role_configs": {
                role: {"agent": "codex", "model": "gpt-5-codex"}
                for role in ("manager", "executor", "auditor")
            },
            "max_rounds": 16,
            "prompt_language": "en",
            "workspace": f"/tmp/eb80-fixture-workspace/{run_id}",
            "pid": 0,
        },
    )
    logs_dir = run_dir / "lh_harness"
    role_dir = logs_dir / "role_orchestration"
    role_dir.mkdir(parents=True, exist_ok=True)
    report = {
        # A terminal run's report carries its own status (that is where
        # ``report_status`` comes from); a LIVE run's report is still blank.
        "status": "" if active else status,
        "completion_satisfied": True,
        "abort_reason": "fixture-abort" if index % 7 == 0 else "",
        "rounds": [{"round_index": r} for r in range(1, ROUNDS_PER_RUN + 1)],
    }
    _write_json(logs_dir / "report.json", report)
    _write_json(role_dir / "report.json", report)
    with (role_dir / "events.jsonl").open("w", encoding="utf-8") as handle:
        for round_index in range(1, ROUNDS_PER_RUN + 1):
            for event in ("manager_round_start", "executor_role_start", "manager_round_done"):
                handle.write(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "ts": 100.0 + round_index,
                            "event": event,
                            "round": round_index,
                            "round_index": round_index,
                            "role": "manager",
                            "run_id": run_id,
                        }
                    )
                    + "\n"
                )
        handle.write(
            json.dumps(
                {
                    "schema_version": 2,
                    "ts": 200.0,
                    "event": "executor_role_start" if active else "role_harness_done",
                    "round": ROUNDS_PER_RUN,
                    "round_index": ROUNDS_PER_RUN,
                    "role": "executor",
                    "run_id": run_id,
                }
            )
            + "\n"
        )
    blob = "z" * (ROUND_KB * 1024)
    trajectory_line = json.dumps({"kind": "text", "text": "step " * 8}) + "\n"
    for round_index in range(1, ROUNDS_PER_RUN + 1):
        round_dir = role_dir / "rounds" / f"round_{round_index:03d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        (round_dir / "task_contract.txt").write_text(
            f"task contract for {run_id} round {round_index}\n", encoding="utf-8"
        )
        (round_dir / "manager_plan.txt").write_text("Next: continue\n", encoding="utf-8")
        (round_dir / "executor_output.txt").write_text(blob, encoding="utf-8")
        (round_dir / "auditor_report.txt").write_text(blob, encoding="utf-8")
        (round_dir / "executor_raw_trajectory.jsonl").write_text(
            trajectory_line * 200, encoding="utf-8"
        )


@pytest.fixture(scope="session")
def scale_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the N=200 fixture once, under pytest's tmp storage only.

    The whole point of the budget is the REQUEST cost at this scale, so the
    build is shared by every test in the module, and the total size is
    asserted so the fixture cannot silently shrink to a trivial one.
    """

    root = tmp_path_factory.mktemp("eb80-runs") / "runs"
    root.mkdir()
    started = time.monotonic()
    for index in range(RUN_COUNT):
        _make_run(root, f"run{index:04d}", index, active=(index < ACTIVE_RUNS))
    build_seconds = time.monotonic() - started
    files = [path for path in root.rglob("*") if path.is_file()]
    total_bytes = sum(path.stat().st_size for path in files)
    # Recorded for the PR body; the assertion keeps the fixture honest.
    print(
        f"\neb80 fixture: N={RUN_COUNT} files={len(files)} "
        f"bytes={total_bytes} build={build_seconds:.1f}s"
    )
    assert total_bytes > 100 * 1024 * 1024, "fixture must be large enough to expose walks"
    return root


def _scale_client(root: Path) -> TestClient:
    """A fresh app over the fixture root: cold caches, real supervisor.

    Mirrors the production web process: one app, a RunSupervisor over the
    runs root, and the dashboard state anchored at one run.  A FRESH client
    per test means cold timings are real first-request timings, never served
    by anything a previous test warmed.
    """

    supervisor = RunSupervisor(root, workspace_root=root.parent)
    state = DashboardState(
        root / "run0000" / "lh_harness",
        runs_root=root,
        control_enabled=False,
    )
    app = create_app(
        state=state,
        runs_root=root,
        run_id="run0000",
        supervisor=supervisor,
        control_enabled=False,
        probe_open_pr=None,
        caller_configs=None,
    )
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture()
def scale_client(scale_root: Path) -> Iterator[tuple[TestClient, Path]]:
    # Deliberately NOT entered as a context manager: entering starts the app
    # lifespan, which starts the fleet-reporter thread, and that thread issues
    # its own full runs listings in the background.  Those background walks
    # would race the request-window counters below and make the "no rounds
    # walk" assertions flaky.  The repo's other webapi tests construct the
    # plain client the same way for the same reason.
    yield _scale_client(scale_root), scale_root


class _RoundArtifactReads:
    """Count opens of files inside any ``rounds/`` directory tree.

    The harness reads run artifacts through anchored ``O_NOFOLLOW`` opens
    (``_saved_task_from_rounds``, ``_open_nofollow``), not ``Path.read_text``,
    so ``os.open`` is the layer every such read funnels through.  Counting
    every open whose path contains a ``rounds`` path component covers all of
    them; stat/scandir of a round DIRECTORY is metadata, not a read.

    The secured-read guards test ``os.open in os.supports_dir_fd`` to decide
    whether anchored openat walks are available.  A plain wrapper function is
    NOT a member of that frozenset, so swapping ``os.open`` alone would
    silently disable the secured-read path and change the behaviour under
    test.  The wrapper is therefore also added to ``supports_dir_fd`` for the
    patch window, keeping the guards armed.
    """

    def __init__(self) -> None:
        self.count = 0
        self.paths: list[str] = []
        self._lock = threading.Lock()
        self._active = False

    def __enter__(self) -> "_RoundArtifactReads":
        original_open = os.open
        original_supports = os.supports_dir_fd
        counter = self

        def counting_open(path, *args, **kwargs):
            if counter._active:
                text = str(path)
                if os.sep + "rounds" + os.sep in text:
                    with counter._lock:
                        counter.count += 1
                        if len(counter.paths) < 20:
                            counter.paths.append(text)
            return original_open(path, *args, **kwargs)

        self._original_open = original_open
        self._original_supports = original_supports
        os.open = counting_open
        os.supports_dir_fd = frozenset(set(original_supports) | {counting_open})
        self._active = True
        return self

    def __exit__(self, *_exc: object) -> None:
        self._active = False
        os.open = self._original_open
        os.supports_dir_fd = self._original_supports

    def reset(self) -> None:
        with self._lock:
            self.count = 0
            self.paths.clear()


def _timed_get(client: TestClient, path: str) -> tuple[float, dict[str, Any]]:
    started = time.perf_counter()
    response = client.get(path)
    elapsed = time.perf_counter() - started
    assert response.status_code == 200, (path, response.status_code, response.text[:400])
    return elapsed, response.json()


def test_runs_latest_within_budget_and_no_round_artifact_reads(scale_client) -> None:
    """``/api/runs/latest``: under budget cold AND warm, no round-artifact reads."""

    client, _root = scale_client
    with _RoundArtifactReads() as reads:
        cold_seconds, body = _timed_get(client, "/api/runs/latest")
        assert cold_seconds < COLD_BUDGET_S, (
            f"cold /api/runs/latest took {cold_seconds:.2f}s at N={RUN_COUNT} "
            f"(budget {COLD_BUDGET_S}s)"
        )
        assert len(body["runs"]) == ACTIVE_RUNS
        reads.reset()
        warm_seconds, _ = _timed_get(client, "/api/runs/latest")
        assert warm_seconds < WARM_BUDGET_S, (
            f"warm /api/runs/latest took {warm_seconds * 1000:.0f}ms at N={RUN_COUNT} "
            f"(budget {WARM_BUDGET_S * 1000:.0f}ms)"
        )
        assert reads.count == 0, (
            f"servicing /api/runs/latest opened {reads.count} round-artifact files "
            f"(examples: {reads.paths[:3]})"
        )


def test_summary_listing_within_budget_shape_and_no_round_artifact_reads(
    scale_client,
) -> None:
    """The summary listing: under budget, #209 fields kept, no artifact reads."""

    client, _root = scale_client
    with _RoundArtifactReads() as reads:
        cold_seconds, body = _timed_get(client, "/api/runs?fields=summary")
        assert len(body["runs"]) == RUN_COUNT
        assert cold_seconds < COLD_BUDGET_S, (
            f"cold summary /api/runs took {cold_seconds:.2f}s at N={RUN_COUNT} "
            f"(budget {COLD_BUDGET_S}s)"
        )
        reads.reset()
        warm_seconds, warm_body = _timed_get(client, "/api/runs?fields=summary")
        assert warm_seconds < WARM_BUDGET_S, (
            f"warm summary /api/runs took {warm_seconds * 1000:.0f}ms at N={RUN_COUNT} "
            f"(budget {WARM_BUDGET_S * 1000:.0f}ms)"
        )
        assert reads.count == 0, (
            f"servicing the summary listing opened {reads.count} round-artifact "
            f"files (examples: {reads.paths[:3]})"
        )
        rows = warm_body["runs"]
        # Response shape is unchanged (fleet-admin + gateway hydrafleet consume
        # it): summary rows keep their small fields, and #209's
        # report_status + completion_satisfied stay on terminal rows.
        terminal = [row for row in rows if row["status"] in {"completed", "failed"}]
        assert terminal, "fixture must contain terminal runs"
        for row in terminal:
            missing = _summary_shape_fields - set(row)
            assert not missing, f"summary row lost fields: {sorted(missing)}"
            assert row["report_status"] == row["status"], (
                f"terminal row {row['id']} lost its report_status "
                f"(got {row['report_status']!r})"
            )
            assert row["completion_satisfied"] is True
        live = [row for row in rows if row["status"] == "running"]
        assert live, "fixture must contain active runs"
        for row in live:
            # Live rows carry no report verdict (same shape as before #209).
            assert row.get("report_status") is None
            assert row.get("completion_satisfied") is None


def test_full_listing_within_budget_and_no_round_artifact_reads(scale_client) -> None:
    """The FULL (default) listing: under budget and no rounds walk per request.

    The full projection keeps its historical response shape; what must change
    is only HOW the rows are produced -- small per-run summaries instead of a
    per-request walk of every run directory, in particular the
    ``_saved_task_from_rounds`` fallback that pinned the CT111 web process.
    """

    client, _root = scale_client
    with _RoundArtifactReads() as reads:
        cold_seconds, body = _timed_get(client, "/api/runs")
        assert len(body["runs"]) == RUN_COUNT
        assert cold_seconds < COLD_BUDGET_S, (
            f"cold full /api/runs took {cold_seconds:.2f}s at N={RUN_COUNT} "
            f"(budget {COLD_BUDGET_S}s)"
        )
        reads.reset()
        warm_seconds, warm_body = _timed_get(client, "/api/runs")
        assert warm_seconds < WARM_BUDGET_S, (
            f"warm full /api/runs took {warm_seconds * 1000:.0f}ms at N={RUN_COUNT} "
            f"(budget {WARM_BUDGET_S * 1000:.0f}ms)"
        )
        assert reads.count == 0, (
            f"servicing the full listing opened {reads.count} round-artifact files "
            f"(examples: {reads.paths[:3]})"
        )
        # The full projection keeps its fields (task/id/status) for readers.
        for row in warm_body["runs"]:
            assert "id" in row and "status" in row and "task" in row


def test_expired_cache_listing_refreshes_incrementally(
    scale_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After every wall-clock cache expires, an unchanged listing is cheap.

    The #179/#193 TTLs only hide repeats for a few seconds; the wedge is what
    happens when they EXPIRE, so the benchmark must exercise the expired
    path.  Pre-fix, every expired listing re-enumerated the runs root and
    re-READ every run's ``status.json`` + ``owner.json`` (measured on main:
    402 record reads for N=200, per request, forever -- the GIL-bound scan
    the CT111 poller kept pinned).  Post-fix, an expired listing may re-check
    each run's stamp, but with nothing changed no per-run record may be
    re-read and no round directory may be walked.
    """

    client, _root = scale_client
    _timed_get(client, "/api/runs?fields=summary")

    # Force every wall-clock cache in the process to look expired, so the
    # next request cannot be served by a TTL: only per-run stamps may keep it
    # cheap.
    clock = {"now": time.monotonic() + 3600.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    walks: list[int] = []
    original_walk = supervisor_service._saved_task_from_rounds

    def counting_walk(runs_root, run_id, *, first_line):
        walks.append(1)
        return original_walk(runs_root, run_id, first_line=first_line)

    monkeypatch.setattr(supervisor_service, "_saved_task_from_rounds", counting_walk)

    record_reads = {"count": 0}
    original_read = supervisor_service._read_json_file

    def counting_read(path, **kwargs):
        record_reads["count"] += 1
        return original_read(path, **kwargs)

    monkeypatch.setattr(supervisor_service, "_read_json_file", counting_read)
    with _RoundArtifactReads() as reads:
        expired_seconds, body = _timed_get(client, "/api/runs?fields=summary")
        assert len(body["runs"]) == RUN_COUNT
        assert expired_seconds < COLD_BUDGET_S, (
            f"expired-cache summary listing took {expired_seconds:.2f}s "
            f"(budget {COLD_BUDGET_S}s)"
        )
        assert reads.count == 0, (
            f"the expired-cache listing opened {reads.count} round-artifact files "
            f"(examples: {reads.paths[:3]})"
        )
    assert walks == [], (
        f"the expired-cache listing walked the round directories of {len(walks)} "
        "runs to re-derive task text; the per-run summary must carry it so no "
        "request pays a rounds walk"
    )
    assert record_reads["count"] == 0, (
        f"the expired-cache listing re-read {record_reads['count']} per-run "
        f"control records although nothing changed (a fresh scan of N="
        f"{RUN_COUNT} runs re-reads ~{2 * RUN_COUNT}); the refresh must be "
        "incremental, keyed on each run's own status stamp"
    )


def test_twenty_parallel_latest_calls_share_one_refresh(scale_client) -> None:
    """20 concurrent ``/api/runs/latest`` requests cause exactly ONE refresh.

    The fleet pattern that wedged CT111: a ~30 s poll from the dashboard plus
    the fleet-admin poller arriving together.  Without single-flight, every
    concurrent request enumerates the runs root and reads every run's records
    at once (GIL-bound scanning); with single-flight they share one refresh
    and no request starts a second enumeration while one is running.  This
    counts full runs-root enumerations (a ``scandir``/``iterdir`` of the root
    itself -- one per refresh is expected, cheap) and the per-run record
    reads, and bounds the worst single request latency by the cold budget.
    """

    import concurrent.futures

    client, root = scale_client
    # Warm once so the baseline (pre-refresh) state exists.
    _timed_get(client, "/api/runs/latest")

    root_text = str(root)
    lock = threading.Lock()
    root_enumerations = {"count": 0}
    status_opens = {"count": 0}
    original_scandir = os.scandir
    original_open = os.open
    original_supports = os.supports_dir_fd
    monitoring = {"active": True}

    def counting_scandir(path=None):
        if monitoring["active"] and path is not None and str(path) == root_text:
            with lock:
                root_enumerations["count"] += 1
        return original_scandir(path)

    def counting_open(path, *args, **kwargs):
        if monitoring["active"]:
            text = str(path)
            if text.endswith("status.json") and root_text in text:
                with lock:
                    status_opens["count"] += 1
        return original_open(path, *args, **kwargs)

    os.scandir = counting_scandir
    os.open = counting_open
    # Keep the secured-read guards armed (see _RoundArtifactReads).
    os.supports_dir_fd = frozenset(set(original_supports) | {counting_open})
    try:
        def one(_):
            started = time.perf_counter()
            response = client.get("/api/runs/latest")
            return time.perf_counter() - started, response.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            started = time.perf_counter()
            results = list(pool.map(one, range(20)))
            wall = time.perf_counter() - started
    finally:
        monitoring["active"] = False
        os.scandir = original_scandir
        os.open = original_open
        os.supports_dir_fd = original_supports

    worst = max(elapsed for elapsed, _ in results)
    assert {code for _, code in results} == {200}
    assert len(results) == 20
    assert worst < COLD_BUDGET_S, (
        f"worst of 20 concurrent /api/runs/latest requests took {worst:.2f}s "
        f"(budget {COLD_BUDGET_S}s) -- concurrent polls must share one refresh"
    )
    # One shared refresh means the runs root is enumerated (about) once for
    # all 20 requests, not once per request.
    assert root_enumerations["count"] <= 2, (
        f"20 concurrent /api/runs/latest requests enumerated the runs root "
        f"{root_enumerations['count']} times; they must share one refresh"
    )
    # And no per-request re-read of every run's status record: 20 requests
    # over N runs must not cost 20*N reads (one refresh's worth, with headroom
    # for the per-run stamp checks of a second straggler refresh, is fine).
    assert status_opens["count"] <= 2 * RUN_COUNT, (
        f"20 concurrent /api/runs/latest requests read run status records "
        f"{status_opens['count']} times (a single shared refresh reads at most "
        f"{RUN_COUNT}); requests are re-scanning instead of sharing"
    )


def test_status_change_is_visible_after_incremental_refresh(scale_client) -> None:
    """The listing is keyed on the run's status record: a rewrite shows up.

    Pins that the incremental refresh is triggered by the run's own status
    record moving (its mtime), not by a wall-clock interval alone -- the
    fleet poller must see a status change without waiting for a rescan.
    """

    client, root = scale_client
    _first, first_body = _timed_get(client, "/api/runs?fields=summary")
    rows_before = {row["id"]: row["status"] for row in first_body["runs"]}
    target = root / "run0150"
    status_path = target / "control" / "status.json"
    record = json.loads(status_path.read_text(encoding="utf-8"))
    # The row must reflect the durable record before the rewrite, whatever the
    # fixture wrote (the first listing may have reconciled it).
    assert rows_before["run0150"] == record["status"]
    record["status"] = "waiting_approval"
    _write_json(status_path, record)

    _second, second_body = _timed_get(client, "/api/runs?fields=summary")
    rows_after = {row["id"]: row["status"] for row in second_body["runs"]}
    assert rows_after["run0150"] == "waiting_approval", (
        "the listing served a stale status after the run's status record moved; "
        "the incremental refresh must be keyed on that stamp"
    )
