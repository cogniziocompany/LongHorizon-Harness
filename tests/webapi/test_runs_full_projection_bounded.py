"""Bounded-work regression test for the FULL ``GET /api/runs`` projection.

``GET /api/runs`` with no ``fields`` parameter returns complete run objects.
The handler (``webapi/server.py``, ``runs()``) walks every run and calls

    summary = build_run_summary(item, state=registry.state_for(item_run_id))

once per run.  Unlike the ``fields=summary`` path, that projection re-reads each
run's durable per-run metadata (``report.json`` through
``DashboardState.read_report`` and ``control/owner.json`` through
``ControlBus.read_owner``) and re-parses it, so one listing of N runs costs N
projections -- every time, on every request.  With the 372 run dirs measured on
CT111 the full listing took 23.9 s cold and then exceeded 60 s on consecutive
warm calls while the web process sat at 253% CPU.

The work is unbounded per request because nothing memoizes the projection: a
warm request repeats exactly the work a cold one did.  This test pins the
regression down by COUNTING PROJECTION WORK, not wall time:

  * a warm listing must project every run (that is the pre-fix behaviour, and
    it proves the counter wraps the real projection rather than a stub);
  * after ONE run directory is changed, the next listing must re-project at
    most a small constant number of runs, not all N;
  * and that listing must still SHOW the change, so a cache with no
    invalidation -- or one keyed too loosely -- cannot pass.

The mutation rewrites ``control/status.json`` and ``control/owner.json`` inside
one run directory.  Those writes do NOT change the run directory's own
``st_mtime_ns`` (verified below in the test), so an invalidation key that covers
only the run-dir stamp is insufficient: the per-run metadata stamps must be part
of the key.  That is the same failure the #193 shared-scan audit found.

The synthetic run directories come from the committed benchmark
(``scripts/bench_runs_full_projection.py``, ``_make_run``) so the fixture shape
stays identical to the measured one and there is no parallel fixture harness.

Run it with::

    PYTHONPATH=src pytest tests/webapi/test_runs_full_projection_bounded.py -v

On a host whose site-packages also carries an installed ``lh_harness`` copy,
``tests/conftest.py`` deliberately drops that path (it must test this checkout,
not the installed one), which also hides ``fastapi`` when the two live in the
same directory and the import fails with ``ModuleNotFoundError: No module named
'fastapi'``.  Point ``PYTHONPATH`` at a dependency-only directory instead so the
conftest has nothing to strip::

    PYTHONPATH=/path/to/deps-only:src pytest tests/webapi/test_runs_full_projection_bounded.py -v
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import DashboardState
from lh_harness.supervisor import control_bus
from lh_harness.supervisor.service import RunSupervisor
from lh_harness.webapi import server as webapi_server
from lh_harness.webapi.server import create_app

# Large enough that "O(N)" and "bounded" are far apart, small enough that the
# test stays fast.  The assertion is on the count, not the size, so N only has
# to be unambiguous.
RUN_COUNT = 24

# How many runs the second listing may still project.  One is the changed run
# itself; two leaves room for an implementation that re-projects the changed run
# from more than one code path.  Anything at RUN_COUNT means nothing was
# memoized.
MAX_REPROJECTED = 2

_BENCH_PATH = Path(__file__).resolve().parents[2] / "scripts" / "bench_runs_full_projection.py"


def _load_benchmark_module() -> Any:
    """Load the committed benchmark script as a module.

    The script starts with ``import conftest`` for the loopback ``TestClient``
    patch.  Under pytest the conftest is already loaded as ``tests.conftest``,
    so alias it before executing the script: that keeps a single conftest
    instance (one Host patch) instead of importing a second copy.
    """

    if "conftest" not in sys.modules:
        sys.modules["conftest"] = importlib.import_module("tests.conftest")
    spec = importlib.util.spec_from_file_location("_bench_runs_full_projection", _BENCH_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


class _ProjectionCounter:
    """Counts the REAL per-run projection work, delegating to the real code.

    Every counter wraps the production function and calls through to it, so the
    work is actually performed (an untouched run is fully re-projected and the
    count says so).  Nothing here is stubbed or short-circuited.

    ``read_owner`` counts only the reads made while a run is being projected
    (inside ``state_for`` or ``build_run_summary``).  The listing's own
    enumeration (``run_items``) also reads each run's control records, and that
    cheap per-run O(N) work is explicitly allowed to stay.
    """

    def __init__(self) -> None:
        self.build_run_summary = 0
        self.read_report = 0
        self.read_owner = 0
        self._projecting = 0
        self._patches: list[Any] = []

    def __enter__(self) -> "_ProjectionCounter":
        original_build = webapi_server.build_run_summary
        original_state_for = webapi_server.StateRegistry.state_for
        original_report = DashboardState.read_report
        original_owner = control_bus.ControlBus.read_owner

        def counting_state_for(registry, run_id):
            # ``state_for`` builds the DashboardState the projection needs, and
            # that construction reads the run's control records.
            self._projecting += 1
            try:
                return original_state_for(registry, run_id)
            finally:
                self._projecting -= 1

        def counting_build(item, *, state=None):
            self.build_run_summary += 1
            self._projecting += 1
            try:
                return original_build(item, state=state)
            finally:
                self._projecting -= 1

        def counting_report(dashboard_state):
            self.read_report += 1
            return original_report(dashboard_state)

        def counting_owner(bus):
            if self._projecting:
                self.read_owner += 1
            return original_owner(bus)

        for target, name, replacement in (
            (webapi_server, "build_run_summary", counting_build),
            (webapi_server.StateRegistry, "state_for", counting_state_for),
            (DashboardState, "read_report", counting_report),
            (control_bus.ControlBus, "read_owner", counting_owner),
        ):
            patch = mock.patch.object(target, name, replacement)
            patch.start()
            self._patches.append(patch)
        return self

    def __exit__(self, *_exc: object) -> None:
        for patch in reversed(self._patches):
            patch.stop()
        self._patches.clear()

    def reset(self) -> None:
        self.build_run_summary = 0
        self.read_report = 0
        self.read_owner = 0
        self._projecting = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "build_run_summary": self.build_run_summary,
            "read_report": self.read_report,
            "read_owner": self.read_owner,
        }


@pytest.fixture()
def listing_app(tmp_path: Path) -> Iterator[tuple[Any, Path]]:
    """A real app over ``RUN_COUNT`` synthetic run dirs in temp space."""

    bench = _load_benchmark_module()
    root = tmp_path / "runs"
    root.mkdir()
    for index in range(RUN_COUNT):
        bench._make_run(root, f"bnd{index:04d}run", index)
    state = DashboardState(root / "bnd0000run" / "lh_harness", runs_root=root)
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


def _warm_listing(client, counter: _ProjectionCounter) -> dict[str, int]:
    """Issue the cold listing and return its work counts.

    A cold listing is expected to project every run.  Asserting that is also
    what proves the counter wraps the real projection: a counter placed on a
    stub that pre-fix code never reaches could not report ``RUN_COUNT``.
    """

    warm = client.get("/api/runs")
    assert warm.status_code == 200
    assert len(warm.json()["runs"]) == RUN_COUNT
    warm_work = counter.snapshot()
    assert warm_work["build_run_summary"] == RUN_COUNT, (
        "the full projection must call build_run_summary once per run on a "
        f"cold listing; counted {warm_work} for {RUN_COUNT} runs"
    )
    assert warm_work["read_report"] >= RUN_COUNT, (
        "build_run_summary reads each run's report.json in the full "
        f"projection; counted {warm_work} for {RUN_COUNT} runs"
    )
    return warm_work


def test_full_listing_reprojects_only_the_changed_run(listing_app) -> None:
    """After a warm listing, changing ONE run must not re-project all N runs."""

    client, root = listing_app
    target = root / "bnd0007run"
    target_id = target.name

    with _ProjectionCounter() as counter:
        # --- Warm the projection. ------------------------------------------
        warm_work = _warm_listing(client, counter)

        # --- Change exactly ONE run directory. -----------------------------
        # Rewrite the per-run control metadata.  Neither write is allowed to
        # touch the run directory's own stamp, which is why an invalidation key
        # built from the run-dir stamp alone cannot see this change.
        dir_stamp_before = os.stat(target).st_mtime_ns
        owner_path = target / "control" / "owner.json"
        owner = _read_json(owner_path)
        owner["youtrack_issue_id"] = "easybutt0n-80"
        _write_json(owner_path, owner)
        status_path = target / "control" / "status.json"
        status = _read_json(status_path)
        status["status"] = "failed"
        _write_json(status_path, status)
        assert os.stat(target).st_mtime_ns == dir_stamp_before, (
            "the fixture assumes rewriting control/*.json does not move the run "
            "directory's own mtime; if it does, this test would not prove the "
            "per-run metadata stamps are needed"
        )
        assert os.stat(owner_path).st_mtime_ns > 0
        assert os.stat(status_path).st_mtime_ns > 0

        # --- The next listing must re-project only a bounded number. -------
        counter.reset()
        after = client.get("/api/runs")
        assert after.status_code == 200
        after_work = counter.snapshot()
        assert after_work["build_run_summary"] <= MAX_REPROJECTED, (
            f"after changing ONE of {RUN_COUNT} run dirs the full listing "
            f"re-projected {after_work['build_run_summary']} runs "
            f"(warm was {warm_work['build_run_summary']}); at most "
            f"{MAX_REPROJECTED} may be re-projected, so the cost of the full "
            f"projection stays bounded per request (work: {after_work})"
        )
        assert after_work["read_report"] <= MAX_REPROJECTED, (
            f"after changing ONE of {RUN_COUNT} run dirs the full listing "
            f"re-read {after_work['read_report']} report.json files; at most "
            f"{MAX_REPROJECTED} may be re-read (work: {after_work})"
        )
        assert after_work["read_owner"] <= MAX_REPROJECTED, (
            f"after changing ONE of {RUN_COUNT} run dirs the full listing "
            f"re-read {after_work['read_owner']} owner.json files; at most "
            f"{MAX_REPROJECTED} may be re-read (work: {after_work})"
        )

    # --- The change must still be VISIBLE (no cache without invalidation). --
    rows = {row["id"]: row for row in after.json()["runs"]}
    assert len(rows) == RUN_COUNT
    assert rows[target_id].get("youtrack_issue_id") == "easybutt0n-80", (
        "the listing served a stale summary for the changed run: its "
        "owner.json now carries youtrack_issue_id but the response did not"
    )
    assert rows[target_id].get("status") == "failed", (
        "the listing served a stale status for the changed run"
    )
    for index in range(RUN_COUNT):
        run_id = f"bnd{index:04d}run"
        assert run_id in rows
        if run_id == target_id:
            continue
        assert rows[run_id].get("youtrack_issue_id") is None
        assert rows[run_id]["status"] == "completed"


def test_unchanged_warm_listing_does_no_projection_work(listing_app) -> None:
    """A repeat listing over unchanged run dirs must not re-project anything.

    This is the other half of "bounded": the mutation case above shows that a
    change is picked up for one run, and this one shows that the absence of a
    change costs no projection work.  Pre-fix both listings project all N.
    """

    client, _root = listing_app
    with _ProjectionCounter() as counter:
        _warm_listing(client, counter)

        counter.reset()
        repeat = client.get("/api/runs")
        assert repeat.status_code == 200
        assert len(repeat.json()["runs"]) == RUN_COUNT
        repeat_work = counter.snapshot()
        assert repeat_work["build_run_summary"] <= MAX_REPROJECTED, (
            "an unchanged warm listing re-projected "
            f"{repeat_work['build_run_summary']} of {RUN_COUNT} runs; the full "
            "projection must be memoized across requests "
            f"(work: {repeat_work})"
        )


def test_log_dir_only_change_is_visible(listing_app) -> None:
    """A change made ONLY under the run's log dir must not be served stale.

    ``report.json`` (and approvals/events/rounds) live under
    ``<run>/lh_harness/...``, not directly under the run directory.  Rewriting
    them moves neither the run directory's stamp nor ``control/*.json``, so a
    stamp that looks for them beside ``control/`` never sees the change (found
    in the non-author review of this change: the listing kept serving the old
    row).
    """

    client, root = listing_app
    target = root / "bnd0005run"
    first = client.get("/api/runs")
    assert first.status_code == 200
    first_rows = {row["id"]: row for row in first.json()["runs"]}
    assert first_rows[target.name].get("mcp_profile") is None

    dir_stamp_before = os.stat(target).st_mtime_ns
    control_before = [
        os.stat(target / "control" / name).st_mtime_ns
        for name in ("status.json", "owner.json")
    ]
    changed = 0
    for path in (
        target / "lh_harness" / "report.json",
        target / "lh_harness" / "role_orchestration" / "report.json",
    ):
        if path.exists():
            report = _read_json(path)
            report["mcp_profile"] = "review-probe-profile"
            _write_json(path, report)
            changed += 1
    assert changed, "fixture no longer writes report.json under lh_harness/"
    assert os.stat(target).st_mtime_ns == dir_stamp_before
    assert control_before == [
        os.stat(target / "control" / name).st_mtime_ns
        for name in ("status.json", "owner.json")
    ]

    rows = {row["id"]: row for row in client.get("/api/runs").json()["runs"]}
    assert rows[target.name].get("mcp_profile") == "review-probe-profile", (
        "the listing served a stale summary after a report.json-only change"
    )


@pytest.mark.parametrize(
    "relative",
    [
        "report.json",
        "role_orchestration/report.json",
        "role_orchestration/approvals.jsonl",
        "role_orchestration/events.jsonl",
        "role_orchestration/rounds.jsonl",
        "role_orchestration/final_response.txt",
    ],
)
def test_summary_stamp_follows_log_dir_files(listing_app, relative: str) -> None:
    """Every projected log-dir file moves the per-run stamp when it changes."""

    _client, root = listing_app
    run_dir = root / "bnd0003run"
    log_dir = run_dir / "lh_harness"
    before = webapi_server._run_summary_stamp(run_dir, log_dir)
    path = log_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"probe": true}\n')
    assert webapi_server._run_summary_stamp(run_dir, log_dir) != before, (
        f"appending to {relative} under the log dir did not move the stamp"
    )
