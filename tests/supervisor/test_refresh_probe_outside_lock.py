"""``_refresh`` must probe liveness OUTSIDE the supervisor-wide lock.

``_lifecycle_lock`` exists to serialize status WRITES across the whole
supervisor.  The liveness probe (``/proc`` stat, the owner record, an
optional ``ps`` fork for the PID-reuse check) only READS and takes no locks
itself, yet ``_refresh`` used to run it while holding the lock.  On CT130
(9749a25d, 24 dead-pid "running" rows) that put 18.82 ms of a 19.99 ms
per-row reconcile -- the probe plus the fsync writes -- inside the lock, and
a 196-row cold pass held it for 4.65 s while every supervisor-touching
endpoint queued behind it.

The second half of the same wedge: a LIVE unchanged row still performed
exactly one ``update_status`` fsync write per poll, rewriting identical
content (measured 14.05 ms/row).  ``_refresh`` now skips that write when
nothing durable changed.

These tests pin the behaviour by COUNTING, not by wall time:

* the probe is never entered while ``_lifecycle_lock`` is held (and the
  refresh really does take the lock, so the claim is not vacuous);
* a dead-pid row still performs exactly one durable ``update_status`` write
  and lands terminal with a ``failure_reason``;
* a live unchanged row performs ZERO ``update_status`` writes after priming;
* a terminal transition is still written (regression guard for the skip);
* two concurrent refreshes overlap INSIDE the probe -- impossible when the
  probe ran under the supervisor-wide RLock.

Run with::

    PYTHONPATH=src pytest tests/supervisor/test_refresh_probe_outside_lock.py -v
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

from lh_harness.supervisor import service as service_module
from lh_harness.supervisor.control_bus import ControlBus
from lh_harness.supervisor.service import RunSupervisor, _pid_start_identity

# A pid that cannot exist: the probe's ``os.kill(pid, 0)`` fails with ESRCH
# before any ``ps`` fork, so the row is deterministically dead.
_DEAD_PID = 999999001


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_run(
    root: Path,
    run_id: str,
    *,
    status: str = "running",
    pid: int = _DEAD_PID,
    live: bool = False,
) -> Path:
    """One run dir shaped like the benchmark fixture.

    ``live=True`` arms the row so the liveness probe answers True: the owner
    pid is this process with a matching ``pid_start_identity`` (and no Popen
    handle in ``_processes``).  ``attached`` stays False, so the probe walks
    the full path including the ``ps`` command-line check -- the tests stub
    ``_ps_query`` to answer as this run's worker.  Dead rows carry
    ``_DEAD_PID`` and deliberately have NO report.json anywhere.
    """

    run_dir = root / run_id
    _write_json(
        run_dir / "control" / "status.json",
        {"run_id": run_id, "status": status, "managed": True, "alive": True},
    )
    owner: dict[str, Any] = {
        "run_id": run_id,
        "state": "running",
        "task": f"reconcile task {run_id}",
        "agent": "codex",
        "model": "gpt-5-codex",
        "workspace": f"/tmp/ws/{run_id}",
        "pid": os.getpid() if live else pid,
        "attached": False,
    }
    if live:
        owner["pid_start_identity"] = _pid_start_identity(os.getpid())
    _write_json(run_dir / "control" / "owner.json", owner)
    role_dir = run_dir / "lh_harness" / "role_orchestration"
    role_dir.mkdir(parents=True, exist_ok=True)
    if live:
        # First durable role event: keeps a live "running" row "running".
        (role_dir / "events.jsonl").write_text(
            json.dumps({"round_index": 1, "role": "executor"}) + "\n",
            encoding="utf-8",
        )
    return run_dir


def _worker_command(run_id: str) -> str:
    """The ``ps -o command=`` line of this run's worker, for the stub."""

    return f"/usr/bin/lh-harness worker --run-id {run_id}"


class _UpdateStatusCounter:
    """Counts real ``ControlBus.update_status`` calls, delegating to the real one.

    ``_bus`` builds a fresh ``ControlBus`` per call, so the count has to wrap
    the class, not an instance.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._patch: Any = None

    def __enter__(self) -> "_UpdateStatusCounter":
        original = ControlBus.update_status

        def counting_update_status(bus, *args: Any, **kwargs: Any):
            self.calls.append(str(bus.run_dir))
            return original(bus, *args, **kwargs)

        self._patch = mock.patch.object(
            ControlBus, "update_status", counting_update_status
        )
        self._patch.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._patch.stop()

    def reset(self) -> None:
        self.calls.clear()


class _HeldTrackingLock:
    """Delegates to the real RLock while reporting whether it is held.

    Installed on the supervisor instance so ``with self._lifecycle_lock:``
    runs through here; every method calls through to the real lock.
    """

    def __init__(self, inner: threading.Lock) -> None:
        self._inner = inner
        self.depth = 0
        self.acquires = 0
        self.on_acquire: Any = None  # optional hook(probe_active: bool)

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        acquired = self._inner.acquire(*args, **kwargs)
        if acquired:
            self.acquires += 1
            self.depth += 1
            if self.on_acquire is not None:
                self.on_acquire()
        return acquired

    def release(self) -> None:
        self.depth -= 1
        self._inner.release()

    def __enter__(self) -> "_HeldTrackingLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _install_held_tracking_lock(supervisor: RunSupervisor) -> _HeldTrackingLock:
    wrapper = _HeldTrackingLock(supervisor._lifecycle_lock)
    supervisor._lifecycle_lock = wrapper
    return wrapper


def _live_ps_stub(run_id: str) -> Any:
    """Context manager making ``_ps_query`` answer as this run's worker."""

    return mock.patch.object(
        service_module, "_ps_query", lambda pid, field: _worker_command(run_id)
    )


def test_liveness_probe_runs_outside_the_lock(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    run_id = "livelockrun"
    _make_run(root, run_id, live=True)

    supervisor = RunSupervisor(root)
    lock = _install_held_tracking_lock(supervisor)

    gauge_overlaps: list[str] = []
    gauge_state = {"calls": 0, "active": 0}
    original_is_alive = RunSupervisor._is_alive

    def gauged_is_alive(sup: RunSupervisor, probed_run_id: str) -> bool:
        if lock.depth > 0:
            gauge_overlaps.append(f"entered while held: {probed_run_id}")
        gauge_state["calls"] += 1
        gauge_state["active"] += 1
        try:
            return original_is_alive(sup, probed_run_id)
        finally:
            gauge_state["active"] -= 1
            if lock.depth > 0:
                gauge_overlaps.append(f"exited while held: {probed_run_id}")

    def on_acquire() -> None:
        if gauge_state["active"] > 0:
            gauge_overlaps.append("lock acquired during a probe")

    lock.on_acquire = on_acquire

    with _live_ps_stub(run_id):
        with mock.patch.object(RunSupervisor, "_is_alive", gauged_is_alive):
            status = supervisor._refresh(run_id)

    assert gauge_overlaps == [], (
        "the liveness probe must never run while _lifecycle_lock is held; "
        f"overlaps: {gauge_overlaps}"
    )
    assert lock.acquires >= 1, (
        "the refresh must still take the lock for its status write section, "
        "otherwise the no-overlap claim above is vacuous"
    )
    assert gauge_state["calls"] == 1, "exactly one probe per refresh"
    assert gauge_state["active"] == 0
    assert status["status"] == "running"
    assert status["alive"] is True


def test_dead_pid_reconcile_still_writes_exactly_once(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    run_id = "deadonc0run"
    run_dir = _make_run(root, run_id)  # dead pid, no report.json

    supervisor = RunSupervisor(root)
    with _UpdateStatusCounter() as counter:
        supervisor._refresh(run_id)

    assert len(counter.calls) == 1, (
        "a dead-pid reconcile is a real terminal transition: exactly one "
        f"durable update_status write, saw {len(counter.calls)}"
    )
    durable = json.loads(
        (run_dir / "control" / "status.json").read_text(encoding="utf-8")
    )
    assert durable["status"] == "failed"
    assert durable.get("failure_reason")


def test_live_unchanged_row_skips_the_status_write(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    run_id = "liveskiprun"
    _make_run(root, run_id, live=True)

    supervisor = RunSupervisor(root)
    with _live_ps_stub(run_id):
        supervisor._refresh(run_id)  # prime: settle any first-poll effects
        with _UpdateStatusCounter() as counter:
            status = supervisor._refresh(run_id)

    assert counter.calls == [], (
        "an unchanged live row must not rewrite identical durable content "
        f"per poll; update_status calls: {counter.calls}"
    )
    assert status["status"] == "running"
    assert status["alive"] is True


def test_terminal_transition_still_written(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    run_id = "deadwrit0run"
    run_dir = _make_run(root, run_id)  # armed "running", probe is dead

    supervisor = RunSupervisor(root)
    with _UpdateStatusCounter() as counter:
        supervisor._refresh(run_id)

    assert len(counter.calls) >= 1, (
        "the nothing-durable-changed skip must never swallow a real "
        "terminal transition"
    )
    durable = json.loads(
        (run_dir / "control" / "status.json").read_text(encoding="utf-8")
    )
    assert durable["status"] in {"failed", "cancelled", "completed", "blocked", "incomplete"}
    assert durable.get("failure_reason"), (
        "a dead worker with no report must keep its failure_reason"
    )


def test_concurrent_refreshes_do_not_serialize_probes(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    run_ids = ["conc0run", "conc1run"]
    for run_id in run_ids:
        # Live-identity rows whose ``ps`` command-line check fails (this test
        # process is not an lh-harness worker), so each probe answers dead --
        # but only AFTER the forked ``ps`` query, which the wrapper below
        # stretches to 20 ms so the two probes must overlap.
        _make_run(root, run_id, live=True)

    supervisor = RunSupervisor(root)

    real_ps_query = service_module._ps_query

    def slow_ps_query(pid: int, field: str) -> str | None:
        time.sleep(0.02)
        return real_ps_query(pid, field)

    gauge_lock = threading.Lock()
    gauge_state = {"active": 0, "max_active": 0, "calls": 0}
    original_is_alive = RunSupervisor._is_alive

    def gauged_is_alive(sup: RunSupervisor, probed_run_id: str) -> bool:
        with gauge_lock:
            gauge_state["calls"] += 1
            gauge_state["active"] += 1
            gauge_state["max_active"] = max(
                gauge_state["max_active"], gauge_state["active"]
            )
        try:
            return original_is_alive(sup, probed_run_id)
        finally:
            with gauge_lock:
                gauge_state["active"] -= 1

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def refresh(run_id: str) -> None:
        try:
            barrier.wait(timeout=30)
            supervisor._refresh(run_id)
        except BaseException as exc:  # surfaced on the main thread below
            errors.append(exc)

    with mock.patch.object(service_module, "_ps_query", slow_ps_query):
        with mock.patch.object(RunSupervisor, "_is_alive", gauged_is_alive):
            threads = [
                threading.Thread(target=refresh, args=(run_id,))
                for run_id in run_ids
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            assert not any(thread.is_alive() for thread in threads)

    assert errors == [], f"refresh threads failed: {errors!r}"
    assert gauge_state["calls"] == 2
    assert gauge_state["max_active"] == 2, (
        "the two liveness probes must run concurrently; a max in-flight of "
        f"{gauge_state['max_active']} means the supervisor-wide lock still "
        "serializes reads"
    )
