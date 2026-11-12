"""A control-only run folder is not a run (easybutt0n-17).

A run folder that holds ONLY ``control/`` — a ``control/status.json`` and a
``control/.control.lock``, with no worker log, no ``lh_harness/`` tree and no
``agent.log`` — is a leftover control reservation, not a run.  It happens
whenever a control write (operator message, stop, approval) reaches a run id
whose work files were pruned or never created.

Before the fix such a folder was reported by the run listing as an ``idle``
run with an empty task, while ``/api/runs/<id>`` answered ``404`` for the very
same id.  The fleet aggregate then counted every one of them as an *active*
run, so a node advertised far more active runs than it was allowed to run.

The fix is a single shared predicate — ``run_is_servable`` — used by both the
run listing (which skips a folder without a run record) and the
``/api/runs/<id>`` boundary (which answers ``404`` for it).  On top of that,
an ``idle`` status whose worker is not alive is never counted as an active
run.

Every directory used here is created by the test itself under ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lh_harness.dashboard.state import DashboardState
from lh_harness.fleet.reporter import _is_inert_idle
from lh_harness.supervisor.lifecycle import TERMINAL_STATUSES, canonical_lifecycle_status
from lh_harness.supervisor.service import RunSupervisor
from lh_harness.utils.run_boundary import run_is_servable
from lh_harness.webapi.server import StateRegistry


def _make_real_run(root: Path, run_id: str) -> Path:
    """Create one run folder in the layout a run record requires.

    A run record is the durable result tree with its role ledger
    (``lh_harness/role_orchestration/role_management``).  This is what the
    ``run_is_servable`` predicate the listing and the 404 boundary share
    requires.
    """

    run_dir = root / run_id
    role_dir = run_dir / "lh_harness" / "role_orchestration" / "role_management"
    (role_dir / "rounds").mkdir(parents=True)
    (run_dir / "lh_harness" / "role_orchestration" / "report.json").write_text(
        json.dumps({"status": "running", "task": "the real task"}), encoding="utf-8"
    )
    (run_dir / "control").mkdir()
    (run_dir / "control" / "status.json").write_text(
        json.dumps({"alive": True, "status": "running"}), encoding="utf-8"
    )
    (run_dir / "control" / "owner.json").write_text(
        json.dumps({"task": "the real task", "agent": "codex"}), encoding="utf-8"
    )
    return run_dir


def _make_control_only_run(root: Path, run_id: str) -> Path:
    """Create one control-only folder: ``control/`` and nothing else.

    This is the shape of the leftover reservation described above: no
    ``worker.log``, no ``lh_harness/`` tree, no ``agent.log``.
    """

    run_dir = root / run_id
    (run_dir / "control").mkdir(parents=True)
    (run_dir / "control" / "status.json").write_text(
        json.dumps({"alive": False, "status": "idle"}), encoding="utf-8"
    )
    (run_dir / "control" / ".control.lock").write_text("", encoding="utf-8")
    return run_dir


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    """A temp runs root with one real run and one control-only folder."""

    root = tmp_path / "runs"
    root.mkdir()
    _make_real_run(root, "run-real")
    _make_control_only_run(root, "run-control-only")
    return root


def test_run_list_skips_control_only_folder(runs_root: Path) -> None:
    """The run listing returns the real run and nothing else."""

    supervisor = RunSupervisor(runs_root=runs_root, workspace_root=runs_root.parent / "ws")

    listed = supervisor.list_run_items()
    assert [item["id"] for item in listed] == ["run-real"]

    cheap = supervisor.list_run_summaries()
    assert [item["id"] for item in cheap] == ["run-real"]


def test_control_only_folder_is_skipped_on_the_cached_scan_path(
    runs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TTL-cached dashboard scan skips it too, not just a fresh scan.

    The cached scan and the fresh scan are the two emission paths of the run
    list; the skip has to hold on both, otherwise a cached scan keeps
    reporting the leftover folder for the cache's whole TTL.
    """

    monkeypatch.setenv("LH_HARNESS_RUNS_SCAN_TTL", "5")
    state = DashboardState(runs_root=runs_root)

    first = state.list_runs()
    second = state.list_runs()

    assert [item["id"] for item in first] == ["run-real"]
    assert [item["id"] for item in second] == ["run-real"]


def test_control_only_folder_is_skipped_on_the_cached_registry_path(
    runs_root: Path,
) -> None:
    """The registry's per-run state cache never resolves a record-less folder.

    ``StateRegistry`` keeps one ``DashboardState`` per run id and revalidates
    it for ``LH_HARNESS_WEB_STATE_TTL_S`` seconds with a single ``stat`` of
    the run directory.  That cache must not become a way to resolve a folder
    that has no run record: the first resolution has to reject it, and so
    does every later one, otherwise the ``/api/runs/<id>`` 404 and the run
    listing would disagree about the same id depending on cache age.
    """

    state = DashboardState(runs_root=runs_root)
    registry = StateRegistry(state=state, runs_root=runs_root)

    assert registry.state_for("run-control-only") is None
    # A second resolution hits the registry's cache path.
    assert registry.state_for("run-control-only") is None
    assert registry.state_for("run-real") is not None


def test_control_only_folder_has_no_run_record_so_the_api_answers_404(
    runs_root: Path,
) -> None:
    """The 404 boundary uses the SAME predicate as the listing skip.

    A folder that is skipped by the listing must also be a 404 for
    ``/api/runs/<id>``: otherwise the node would list a run it cannot serve.
    """

    state = DashboardState(runs_root=runs_root)
    registry = StateRegistry(state=state, runs_root=runs_root)

    assert registry.state_for("run-control-only") is None
    assert registry.state_for("run-real") is not None


def test_the_listing_and_the_404_boundary_share_one_predicate(
    runs_root: Path,
) -> None:
    """Both paths ask the SAME predicate, so they can never disagree.

    The listing skip and the ``/api/runs/<id>`` 404 must be ONE decision, not
    two similar-looking ones: if they were separate checks they could drift
    apart and a folder could be listed as a run while the same id answered
    ``404``.  ``run_is_servable`` is that single decision — the listing calls
    it to skip, and ``StateRegistry._run_paths`` calls it to reject.
    """

    assert run_is_servable(runs_root, runs_root / "run-real") is True
    assert run_is_servable(runs_root, runs_root / "run-control-only") is False

    # The listing skips exactly the folders the 404 boundary rejects.
    supervisor = RunSupervisor(runs_root=runs_root, workspace_root=runs_root.parent / "ws")
    listed = {str(item["id"]) for item in supervisor.list_run_items()}
    servable = {
        entry.name
        for entry in runs_root.iterdir()
        if entry.is_dir() and run_is_servable(runs_root, entry)
    }
    assert listed == servable == {"run-real"}


def test_control_only_folder_contributes_zero_to_the_active_count(
    runs_root: Path,
) -> None:
    """The control-only folder is never counted as an active run.

    The fleet aggregate counts every run whose status is non-terminal as
    active.  ``idle`` is non-terminal, so a control-only folder would be
    counted — unless the run is known to be not alive.  The durable
    ``control/status.json`` of the control-only folder says
    ``{"alive": false, "status": "idle"}``, so it contributes zero.
    """

    supervisor = RunSupervisor(runs_root=runs_root, workspace_root=runs_root.parent / "ws")

    rows = supervisor.list_run_summaries()
    control_only_rows = [row for row in rows if row["id"] == "run-control-only"]
    assert control_only_rows == []

    # The guard itself: an ``idle`` row whose worker is not alive is inert
    # and must never be counted as active.
    assert _is_inert_idle({"id": "run-control-only", "status": "idle", "alive": False}) is True

    # A genuinely running run is still active.
    active_rows = [row for row in rows if row["id"] == "run-real"]
    assert len(active_rows) == 1
    assert _is_inert_idle(active_rows[0]) is False

    # The active count the fleet aggregate computes over the whole run list:
    # every run whose status is non-terminal counts as active, except an
    # ``idle`` run whose worker is known to be not alive.
    active_count = sum(
        1
        for row in rows
        if canonical_lifecycle_status(row.get("status")) not in TERMINAL_STATUSES
        and not _is_inert_idle(row)
    )
    assert active_count == 1

    # The control-only folder contributes exactly zero to that count.
    control_only_active = sum(
        1
        for row in rows
        if row["id"] == "run-control-only"
        and canonical_lifecycle_status(row.get("status")) not in TERMINAL_STATUSES
        and not _is_inert_idle(row)
    )
    assert control_only_active == 0


def test_idle_and_not_alive_is_never_active_even_when_the_row_exists(
    runs_root: Path,
) -> None:
    """The active-count guard holds on its own, not only via the skip.

    The listing skip removes the control-only folder before the aggregate
    ever sees it.  The second predicate is independent of that: given a row
    that IS present in the run list, an ``idle`` status whose worker is known
    to be not alive still must not be counted as active.  If it were, a
    control-only folder would come back as soon as anything else put it in
    the list — for example a cached scan that was built before the skip
    existed, or a status file written after the listing was taken.
    """

    # A row that survived into the run list, exactly as the fleet aggregate
    # would receive it if the skip had not removed it.
    stub_row = {"id": "run-control-only", "status": "idle", "alive": False}

    assert canonical_lifecycle_status(stub_row["status"]) not in TERMINAL_STATUSES
    assert _is_inert_idle(stub_row) is True
    assert (
        canonical_lifecycle_status(stub_row["status"]) not in TERMINAL_STATUSES
        and not _is_inert_idle(stub_row)
    ) is False

    # The contrast cases: a running run, and an idle run whose liveness is
    # unknown (``alive`` absent) — neither is inert.
    assert _is_inert_idle({"id": "run-real", "status": "running", "alive": True}) is False
    assert _is_inert_idle({"id": "run-unknown", "status": "idle"}) is False
    assert _is_inert_idle({"id": "run-unknown", "status": "idle", "alive": None}) is False


def test_fleet_heartbeat_does_not_serialize_the_control_only_folder(
    runs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fleet heartbeat body itself carries no control-only run.

    This is the surface the incident was seen on: the node advertises its
    active runs in the heartbeat.  The heartbeat's ``runs[]`` array is built
    from the run list, so a control-only folder must not appear in it — and
    must not be counted in the aggregate the fleet window reads.
    """

    from lh_harness.fleet.reporter import FleetReporter

    supervisor = RunSupervisor(runs_root=runs_root, workspace_root=runs_root.parent / "ws")
    rows = supervisor.list_run_summaries()

    posted: list[dict] = []

    class _CapturingReporter(FleetReporter):
        def _post(self, endpoint: str, payload: Any, gzip_body: bool = True) -> None:
            posted.append({"endpoint": endpoint, "payload": payload})

    monkeypatch.setattr(
        "lh_harness.fleet.reporter.FleetReporter.__init__",
        lambda self, *args, **kwargs: None,
    )
    reporter = _CapturingReporter.__new__(_CapturingReporter)
    reporter._enabled = True
    reporter._node = "node-test"
    reporter._version = "0"
    reporter._labels = {}
    reporter._ui_base_url = ""

    reporter.queue_heartbeat(rows, active=0, cap=5, queue_len=0)

    assert posted, "the heartbeat was not posted"
    body = posted[0]["payload"]
    assert body["node"]["name"] == "node-test"

    serialized_ids = [str(row.get("runId")) for row in body["runs"]]
    assert "run-control-only" not in serialized_ids
    assert serialized_ids == ["run-real"]
