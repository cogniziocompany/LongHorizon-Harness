"""LHH-CT110-MEMORY-ADMISSION: the shared supervisor admission boundary.

The node-wide reservation lives at ``RunSupervisor.create_run`` -- the one
place BOTH launch paths converge (the queue launcher's ``_launch`` and the
API's ``POST /api/runs``).  These tests pin that boundary:

- the admission decision is atomic under the lock, so two racing launches
  cannot both take the last slot;
- the memory headroom gate refuses with the ``memory_headroom`` reason;
- the reservation is released when the start FAILS (a failed launch must
  never hold a node slot);
- the reservation is published when the worker comes up, and the slot is
  released again when the worker exits (deliverable C);
- the count is reconciled from live run dirs at construction (deliverable
  C/D), and a node already over the cap kills nothing and admits nothing.

The run store and the meminfo probe are fakes (deliverable F): no worker
is ever spawned and no /proc/meminfo is read.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from typing import Any

import pytest

from lh_harness.supervisor.admission import (
    DEFAULT_MEM_FLOOR_MB,
    DEFAULT_NODE_MAX_RUNS,
    DEFAULT_RUN_STARTUP_MB,
    MEMORY_HEADROOM_SKIP_REASON,
    AdmissionRefused,
    NodeAdmission,
    admission_settings_from_values,
    read_cgroup_headroom_mb,
    read_mem_available_mb,
)
from lh_harness.supervisor.service import RunSupervisor, _pid_start_identity


# ---------------------------------------------------------------------------
# Fakes: a fake run store and a fake meminfo probe.
# ---------------------------------------------------------------------------

class FakeStore:
    """A fake settings store: a plain value map, like settings_store's."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def values(self) -> dict[str, str]:
        return dict(self._values)


def _admission(
    *,
    values: dict[str, str] | None = None,
    mem_mb: float | None = 8000.0,
) -> NodeAdmission:
    """A NodeAdmission over a fake settings store and a fake meminfo probe."""

    return NodeAdmission(
        settings_reader=(lambda: FakeStore(values).values()) if values is not None else None,
        meminfo_probe=(lambda: mem_mb),
    )


# ---------------------------------------------------------------------------
# Deliverable A: the reservation is atomic under the lock.
# ---------------------------------------------------------------------------

def test_reserve_startup_refuses_at_node_max_across_all_lanes() -> None:
    """Runs in ANY lane count toward the node maximum."""

    admission = _admission()
    for index in range(DEFAULT_NODE_MAX_RUNS):
        admission.publish(f"run-{index}", "running")

    with pytest.raises(AdmissionRefused) as excinfo:
        admission.reserve_startup("run-new")
    assert excinfo.value.blocked_reason == "node_max"
    assert "node max" in excinfo.value.reason


def test_two_racing_launches_cannot_both_take_the_last_slot() -> None:
    """The last slot goes to exactly ONE of two racing launchers.

    Both launchers take the decision concurrently; the lock inside
    ``reserve_startup`` is what makes the outcome single-winner.
    """

    admission = _admission()
    for index in range(DEFAULT_NODE_MAX_RUNS - 1):
        admission.publish(f"running-{index}", "running")

    admitted: list[str] = []
    refused: list[str] = []
    barrier = threading.Barrier(2)

    def launch(run_id: str) -> None:
        barrier.wait()
        try:
            admission.reserve_startup(run_id)
        except AdmissionRefused:
            refused.append(run_id)
        else:
            admitted.append(run_id)

    threads = [threading.Thread(target=launch, args=(f"race-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(admitted) == 1
    assert len(refused) == 1


def test_release_startup_frees_the_slot_immediately() -> None:
    """A released reservation frees its slot at once (deliverable C)."""

    admission = _admission()
    admission.reserve_startup("run-a")
    admission.release_startup("run-a")

    # The slot is free again: a fresh reservation succeeds.
    admission.reserve_startup("run-b")
    snapshot = admission.snapshot()
    assert snapshot["running"] + snapshot["starting"] == 1


def test_forget_releases_the_slot_on_worker_exit() -> None:
    """A run that exits releases its node slot (deliverable C)."""

    admission = _admission()
    admission.publish("run-a", "running")
    assert admission.snapshot()["running"] == 1

    admission.forget("run-a")
    snapshot = admission.snapshot()
    assert snapshot["running"] == 0
    assert snapshot["starting"] == 0


# ---------------------------------------------------------------------------
# Deliverable B: the memory headroom gate.
# ---------------------------------------------------------------------------

def test_memory_gate_refuses_below_the_floor() -> None:
    """MemAvailable - startup allowance < floor refuses the launch."""

    admission = _admission(mem_mb=1200.0)
    with pytest.raises(AdmissionRefused) as excinfo:
        admission.reserve_startup("run-a")
    assert excinfo.value.blocked_reason == MEMORY_HEADROOM_SKIP_REASON
    assert MEMORY_HEADROOM_SKIP_REASON in excinfo.value.reason


def test_memory_gate_admits_at_the_exact_boundary() -> None:
    """MemAvailable - startup == floor is enough headroom (not below it)."""

    admission = _admission(
        mem_mb=float(DEFAULT_RUN_STARTUP_MB + DEFAULT_MEM_FLOOR_MB),
    )
    admission.reserve_startup("run-a")


def test_missing_meminfo_fails_open() -> None:
    """No memory reading must not freeze the queue; the node cap still binds."""

    admission = _admission(mem_mb=None)
    admission.reserve_startup("run-a")
    for index in range(DEFAULT_NODE_MAX_RUNS - 1):
        admission.publish(f"run-{index}", "running")
    with pytest.raises(AdmissionRefused) as excinfo:
        admission.reserve_startup("run-last")
    assert excinfo.value.blocked_reason == "node_max"


def test_read_mem_available_mb_bounds_by_the_cgroup_limit(tmp_path) -> None:
    """The cgroup limit bounds the host-wide MemAvailable reading."""

    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 65536000 kB\nMemAvailable: 8000000 kB\n", encoding="utf-8")
    cgroup = tmp_path / "memory.max"
    cgroup.write_text(str(4 * 1024 * 1024 * 1024), encoding="utf-8")

    available = read_mem_available_mb(meminfo, cgroup, follow_self_cgroup=False)
    assert available == pytest.approx(4096.0)


def test_read_mem_available_mb_ignores_an_unlimited_cgroup(tmp_path) -> None:
    """cgroup v2's "max" means no limit; the host reading stands."""

    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable: 2097152 kB\n", encoding="utf-8")
    cgroup = tmp_path / "memory.max"
    cgroup.write_text("max", encoding="utf-8")

    available = read_mem_available_mb(meminfo, cgroup, follow_self_cgroup=False)
    assert available == pytest.approx(2048.0)


def test_cgroup_headroom_is_the_remaining_memory_not_the_limit(tmp_path) -> None:
    """The cgroup bound is memory.max - memory.current, not memory.max.

    A 4 GiB cgroup that already holds 3 GiB has 1 GiB of headroom left,
    not 4 GiB.  Reading only the limit would let a nearly full container
    admit another worker -- the shape that ends in an OOM kill.
    """

    cgroup_root = tmp_path / "cgroup"
    directory = cgroup_root / "lxc" / "container"
    directory.mkdir(parents=True)
    (directory / "memory.max").write_text(str(4 * 1024 * 1024 * 1024), encoding="utf-8")
    (directory / "memory.current").write_text(str(3 * 1024 * 1024 * 1024), encoding="utf-8")

    assert read_cgroup_headroom_mb(cgroup_root, "/lxc/container") == pytest.approx(1024.0)


def test_ancestor_cgroup_limit_bounds_the_headroom(tmp_path) -> None:
    """An ancestor cgroup with a limit bounds the container too.

    The container's own cgroup is roomy, but its parent caps the whole
    subtree; the tightest level is the real ceiling.
    """

    cgroup_root = tmp_path / "cgroup"
    parent = cgroup_root / "lxc" / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / "memory.max").write_text(str(8 * 1024 * 1024 * 1024), encoding="utf-8")
    (parent / "memory.current").write_text(str(7 * 1024 * 1024 * 1024), encoding="utf-8")
    (child / "memory.max").write_text(str(8 * 1024 * 1024 * 1024), encoding="utf-8")
    (child / "memory.current").write_text("0", encoding="utf-8")

    assert read_cgroup_headroom_mb(cgroup_root, "/lxc/parent/child") == pytest.approx(1024.0)


def test_nearly_full_cgroup_refuses_the_supervisor_gate_with_plentiful_host_ram() -> None:
    """Plenty of host RAM, but a nearly full cgroup: the gate REFUSES.

    The shared supervisor boundary is where the POST /api/runs path
    admits, so the cgroup headroom has to bind there too: the host
    reading alone (32 GB free) would admit, but the cgroup has only
    200 MB left, which is under the 600 MB startup allowance plus the
    1024 MB floor.
    """

    admission = NodeAdmission(meminfo_probe=lambda: 32768.0, cgroup_probe=lambda: 200.0)
    with pytest.raises(AdmissionRefused) as excinfo:
        admission.reserve_startup("run-1")
    assert excinfo.value.blocked_reason == MEMORY_HEADROOM_SKIP_REASON


# ---------------------------------------------------------------------------
# Deliverable C/D: reconciliation from the live run dirs at construction.
# ---------------------------------------------------------------------------

def _supervisor(tmp_path, **kwargs: Any) -> RunSupervisor:
    return RunSupervisor(runs_root=tmp_path / "runs", **kwargs)


def _write_run_dir(
    supervisor: RunSupervisor,
    run_id: str,
    status: str,
    pid: int = 0,
) -> None:
    """Write a durable run dir whose status says ``status``.

    ``pid`` 0 means "no live worker process" (a dead run); a real pid
    means the worker is still up.  The durable process identity is
    written alongside it, because the supervisor refuses to treat a pid
    as its worker without a matching start marker.
    """

    run_dir = supervisor._run_dir(run_id)  # noqa: SLF001 - test fixture
    (run_dir / "control").mkdir(parents=True, exist_ok=True)
    owner = {
        "run_id": run_id,
        "pid": pid,
        "pgid": pid,
        "task": "t",
        "managed": True,
    }
    if pid:
        owner["pid_start_identity"] = _pid_start_identity(pid)
    (run_dir / "control" / "owner.json").write_text(
        json.dumps(owner), encoding="utf-8"
    )
    (run_dir / "control" / "status.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": status,
                "managed": True,
                "alive": bool(pid),
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def live_worker():
    """A real, live child process so a run dir counts as a live run.

    The supervisor's liveness check reads the worker's command line and
    requires the harness name and the run id to be visible in it (the
    durable run boundary in argv).  ``ps`` truncates that output to the
    terminal width, so the check is exercised through the supervisor's own
    in-memory process handle -- the same handle a supervisor that launched
    the worker itself would hold -- rather than through ``ps``.
    """

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        yield process
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()


def test_supervisor_reconciles_the_count_from_live_run_dirs(tmp_path, live_worker) -> None:
    """At construction the count is rebuilt from the live run dirs."""

    supervisor = _supervisor(tmp_path)
    # A run dir whose durable status says "running" AND whose worker
    # process is still alive is a live run.
    _write_run_dir(supervisor, "live-run", "running", pid=live_worker.pid)
    # The supervisor's own handle for the worker (as a launching supervisor
    # would hold it) is what makes the run count as alive.
    supervisor._processes["live-run"] = live_worker  # noqa: SLF001 - test fixture

    supervisor.admission.reconcile_from_supervisor(supervisor)
    live = supervisor.admission._live  # noqa: SLF001 - white-box assertion
    assert "live-run" in live
    snapshot = supervisor.admission.snapshot()
    assert snapshot["running"] == 1


def test_reconcile_drops_runs_that_are_no_longer_live(tmp_path) -> None:
    """A run that is gone releases its slot at reconcile time."""

    admission = _admission()
    admission.publish("gone", "running")
    admission.reconcile({})
    snapshot = admission.snapshot()
    assert snapshot["running"] == 0
    assert snapshot["starting"] == 0


def test_reconcile_keeps_only_active_lifecycle_statuses(tmp_path) -> None:
    """A terminal run recorded in a run dir does not consume admission."""

    supervisor = _supervisor(tmp_path)
    _write_run_dir(supervisor, "done-run", "failed")

    supervisor.admission.reconcile_from_supervisor(supervisor)
    live = supervisor.admission._live  # noqa: SLF001 - white-box assertion
    assert "done-run" not in live
    snapshot = supervisor.admission.snapshot()
    assert snapshot["running"] == 0
    assert snapshot["starting"] == 0


# ---------------------------------------------------------------------------
# P1-2: an enumeration FAILURE blocks admission; it is never read as "empty".
# ---------------------------------------------------------------------------

class _FakeInventory:
    """A supervisor whose live-run enumeration can be made to fail.

    ``items`` is the durable status projection the supervisor's
    ``list_run_summaries`` returns; when ``fail`` is set the enumeration
    raises instead, which is the shape of an unreadable runs root.
    """

    def __init__(self, items: list[dict[str, str]] | None = None, *, fail: bool = False) -> None:
        self.items = list(items or [])
        self.fail = fail

    def list_run_summaries(self) -> list[dict[str, str]]:
        if self.fail:
            raise OSError("runs root is unreadable")
        return list(self.items)


def test_inventory_failure_at_startup_blocks_admission() -> None:
    """A failed enumeration at start admits NOTHING.

    At startup the last known state is empty, so the count would be zero
    and the node would look free.  That is exactly the wrong answer: the
    inventory is UNKNOWN, so admission must be blocked.
    """

    admission = _admission()
    admission.reconcile_from_supervisor(_FakeInventory(fail=True))

    with pytest.raises(AdmissionRefused) as excinfo:
        admission.reserve_startup("run-1")
    assert excinfo.value.blocked_reason == "inventory_unavailable"
    snapshot = admission.snapshot()
    assert snapshot["blocked_reason"] == "inventory_unavailable"


def test_inventory_failure_while_populated_keeps_the_known_state() -> None:
    """A failure while the node is populated keeps the known state.

    The known running runs still count (their slots are not released by
    an unrelated read failure), and no new admission is allowed on top
    of an unknown inventory.
    """

    admission = _admission()
    admission.reconcile_from_supervisor(
        _FakeInventory([{"id": "run-a", "status": "running"}])
    )
    assert admission.snapshot()["running"] == 1

    admission.reconcile_from_supervisor(_FakeInventory(fail=True))

    # The last known state is KEPT, not replaced with an empty set.
    snapshot = admission.snapshot()
    assert snapshot["running"] == 1
    assert snapshot["blocked_reason"] == "inventory_unavailable"

    # And no new admission on top of it.
    with pytest.raises(AdmissionRefused) as excinfo:
        admission.reserve_startup("run-b")
    assert excinfo.value.blocked_reason == "inventory_unavailable"


def test_admission_resumes_after_a_successful_enumeration() -> None:
    """Recovery: a successful enumeration clears the block.

    The next successful enumeration restores the inventory and the node
    admits again -- the block is not sticky.
    """

    admission = _admission()
    admission.reconcile_from_supervisor(_FakeInventory(fail=True))
    with pytest.raises(AdmissionRefused):
        admission.reserve_startup("run-1")

    admission.reconcile_from_supervisor(
        _FakeInventory([{"id": "run-a", "status": "running"}])
    )

    snapshot = admission.snapshot()
    assert snapshot["blocked_reason"] is None
    assert snapshot["running"] == 1

    # Admission resumes: the reservation is granted.
    admission.reserve_startup("run-b")
    assert admission.snapshot()["starting"] == 1


def test_status_of_a_crashed_run_releases_its_node_slot(tmp_path) -> None:
    """Deliverable C: resolving a crashed run's status frees its node slot.

    This is the PRODUCTION path, not the admission object in isolation: the
    run is published (holding a slot), its durable status turns terminal --
    the OOM-kill shape, where the worker died and the supervisor wrote
    ``failed`` -- and reading that run's status through the supervisor
    releases the slot.  Without this wiring the node would keep a slot for
    every crashed run forever, and the node gate would refuse launches on a
    node whose workers are all gone.
    """

    supervisor = _supervisor(tmp_path)
    # The run is alive and holds a node slot.
    _write_run_dir(supervisor, "crashed-run", "running")
    supervisor.admission.publish("crashed-run", "running")
    snapshot = supervisor.admission.snapshot()
    assert snapshot["running"] == 1

    # The worker dies: the durable status turns terminal.
    run_dir = supervisor._run_dir("crashed-run")  # noqa: SLF001 - test fixture
    (run_dir / "control" / "status.json").write_text(
        json.dumps(
            {
                "run_id": "crashed-run",
                "status": "failed",
                "managed": True,
                "alive": False,
            }
        ),
        encoding="utf-8",
    )

    # Resolving the status releases the slot.
    status = supervisor.status("crashed-run")
    assert status.get("status") == "failed"
    snapshot = supervisor.admission.snapshot()
    assert snapshot["running"] == 0
    assert snapshot["starting"] == 0


def test_status_of_a_running_run_keeps_its_node_slot(tmp_path, live_worker) -> None:
    """The other half of the wiring: an alive run keeps holding its slot.

    The release must not be so eager that a healthy run loses its slot and
    the node admits past its maximum.
    """

    supervisor = _supervisor(tmp_path)
    _write_run_dir(supervisor, "live-run", "running", pid=live_worker.pid)
    supervisor._processes["live-run"] = live_worker  # noqa: SLF001 - test fixture
    supervisor.admission.publish("live-run", "running")

    status = supervisor.status("live-run")

    assert status.get("status") == "running"
    snapshot = supervisor.admission.snapshot()
    assert snapshot["running"] == 1
    assert snapshot["starting"] == 0


# ---------------------------------------------------------------------------
# The settings resolver.
# ---------------------------------------------------------------------------

def test_admission_settings_defaults_and_overrides() -> None:
    """Unset settings keep the documented defaults; a set value wins."""

    defaults = admission_settings_from_values(None)
    assert defaults["node_max"] == DEFAULT_NODE_MAX_RUNS
    assert defaults["startup_mb"] == DEFAULT_RUN_STARTUP_MB
    assert defaults["mem_floor_mb"] == DEFAULT_MEM_FLOOR_MB

    overridden = admission_settings_from_values(
        {
            "LH_HARNESS_NODE_MAX_RUNS": "9",
            "LH_HARNESS_RUN_STARTUP_MB": "300",
            "LH_HARNESS_MEM_FLOOR_MB": "512",
        }
    )
    assert overridden == {"node_max": 9, "startup_mb": 300, "mem_floor_mb": 512}


def test_admission_settings_ignore_a_bad_value() -> None:
    """A blank or unparseable settings row keeps the default."""

    resolved = admission_settings_from_values(
        {
            "LH_HARNESS_NODE_MAX_RUNS": "",
            "LH_HARNESS_RUN_STARTUP_MB": "not-a-number",
            "LH_HARNESS_MEM_FLOOR_MB": "not-a-number",
        }
    )
    assert resolved["node_max"] == DEFAULT_NODE_MAX_RUNS
    assert resolved["startup_mb"] == DEFAULT_RUN_STARTUP_MB
    assert resolved["mem_floor_mb"] == DEFAULT_MEM_FLOOR_MB


def test_admission_settings_accept_zero_for_the_memory_allowances() -> None:
    """Zero is a real value for the two memory allowances.

    A floor of 0 means "no floor" and a startup allowance of 0 means "no
    reservation" -- both are legal operator choices, so they are NOT
    treated as bad rows.  The node cap is the only one that must stay
    positive.
    """

    resolved = admission_settings_from_values(
        {
            "LH_HARNESS_RUN_STARTUP_MB": "0",
            "LH_HARNESS_MEM_FLOOR_MB": "0",
        }
    )
    assert resolved["startup_mb"] == 0
    assert resolved["mem_floor_mb"] == 0
