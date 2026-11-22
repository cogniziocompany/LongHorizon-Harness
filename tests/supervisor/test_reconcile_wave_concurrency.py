"""Bounded in-flight cap for the post-restart reconcile wave.

LHH-RECONCILE-WAVE-CONCURRENCY: after an API restart no Popen handles
exist, so the FIRST full ``GET /api/runs`` (no ``fields=summary``) calls
``RunSupervisor._refresh`` for every ACTIVE run row in one request.  The
sibling terminal fast path already skips finished rows; the active ones
still fan out, and overlapping pollers multiply that fan-out (measured on
CT130: 3 pollers -> 96 refreshes, 5.0 s wall on 24 rows) while every
refresh holds the supervisor-wide ``_lifecycle_lock``.

The fix: ``list_run_items`` routes its per-row reconcile through
``_refresh_bounded``, which holds one of ``self._reconcile_slots`` (a
``threading.BoundedSemaphore`` sized by
``_reconcile_concurrency_from_env`` -- default 3, clamped to 1..8 via
``LH_HARNESS_RECONCILE_CONCURRENCY``).  The wave stays SEQUENTIAL per
request; the cap only bounds how many refreshes from OVERLAPPING requests
may be in flight at once.

These tests pin the behaviour by COUNTING with wrappers that call
through to the real methods, never by wall time:

* the combined max in-flight ``_refresh`` across two concurrent waves
  never exceeds the cap;
* the cap is configurable and clamped (16 -> 8, 0 -> 1, "abc" -> 3,
  unset -> 3);
* a wave that arrives after another wave terminalized the rows reuses
  the durable terminal statuses instead of stacking a second fan-out;
* terminal rows still never enter ``_refresh`` (regression guard for the
  call-site swap keeping the terminal fast path);
* one sequential wave over a mixed set returns exactly the main-branch
  id/status mapping (the cap must not change results).

NOTE on the widening sleep: on this fixture the rows are DEAD-pid actives
(pid 999999001...).  ``_is_alive`` rejects such a pid at ``os.kill(pid, 0)``
BEFORE the ``_ps_query`` liveness fork is ever consulted, so a ``_ps_query``
mock alone can never widen the reconcile window here.  The gauge therefore
carries the small sleep itself, around the call-through to the real
``_refresh`` -- same effect, honest count.

VARIANT ASSERTED in ``test_wave_refresh_in_flight_never_exceeds_cap``:
the sibling slice ``runs-reconcile-lock-hold`` has NOT landed on main
(9749a25d: ``_refresh``'s head has no early probe line), so the asserted
bound is ``max_in_flight <= cap`` (not ``cap + 1``).

Run with::

    PYTHONPATH=src pytest tests/supervisor/test_reconcile_wave_concurrency.py tests/supervisor/test_terminal_fast_path.py -v
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

import lh_harness.supervisor.service as service_module
from lh_harness.supervisor.service import RunSupervisor, _pid_start_identity


DEFAULT_CAP = 3
ENV_KEY = "LH_HARNESS_RECONCILE_CONCURRENCY"
# Far beyond any real pid on this host, so ``os.kill(pid, 0)`` always
# fails and every row below is a dead-pid ACTIVE row.
DEAD_PID_BASE = 999999001


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_run(
    root: Path,
    run_id: str,
    *,
    status: str,
    pid: int = 0,
    pid_start_identity: str = "",
    attached: bool = False,
    with_report: bool = True,
) -> Path:
    """One run dir shaped like the benchmark fixture.

    ``status`` is the durable ``control/status.json`` lifecycle.  ACTIVE
    rows pass a dead owner pid so reconciliation terminalizes them; the
    spec's dead-pid fixture carries no ``report.json``.
    """

    run_dir = root / run_id
    _write_json(
        run_dir / "control" / "status.json",
        {
            "run_id": run_id,
            "status": status,
            "managed": True,
            "alive": status not in {"completed", "failed", "cancelled"},
        },
    )
    _write_json(
        run_dir / "control" / "owner.json",
        {
            "run_id": run_id,
            "state": status,
            "task": f"task {run_id}",
            "agent": "codex",
            "model": "gpt-5-codex",
            "workspace": f"/tmp/ws/{run_id}",
            "pid": pid,
            "pid_start_identity": pid_start_identity,
            "attached": attached,
        },
    )
    role_dir = run_dir / "lh_harness" / "role_orchestration"
    role_dir.mkdir(parents=True, exist_ok=True)
    if with_report:
        _write_json(
            role_dir / "report.json",
            {
                "task": f"task {run_id}",
                "status": status,
                "final_response": "done",
                "rounds": [{"round_index": 1}],
            },
        )
    (role_dir / "events.jsonl").write_text(
        json.dumps({"round_index": 1, "role": "executor"}) + "\n",
        encoding="utf-8",
    )
    return run_dir


def _make_dead_active_rows(root: Path, count: int) -> list[str]:
    """``count`` status="running" rows whose owner pid is dead, no report."""

    ids = [f"dead{index:02d}run" for index in range(count)]
    for index, run_id in enumerate(ids):
        _make_run(
            root,
            run_id,
            status="running",
            pid=DEAD_PID_BASE + index,
            pid_start_identity=f"proc:{900000 + index}",
            with_report=False,
        )
    return ids


class _InFlightGauge:
    """Max-in-flight gauge around the real ``RunSupervisor._refresh``.

    Lock + counter, calls through, and sleeps briefly inside the measured
    window so two concurrent waves observably overlap.  Each call is also
    tagged with the calling thread's wave name.
    """

    def __init__(self, sleep_s: float = 0.01) -> None:
        self._lock = threading.Lock()
        self._sleep_s = sleep_s
        self._in_flight = 0
        self.max_in_flight = 0
        self.calls: list[str] = []
        self.call_events: list[tuple[str, str]] = []
        self._patch: Any = None

    def calls_for(self, wave: str) -> list[str]:
        """Refreshed run ids attributable to one named wave thread."""

        return [run_id for name, run_id in self.call_events if name == wave]

    def __enter__(self) -> "_InFlightGauge":
        original = RunSupervisor._refresh
        gauge = self

        def gauged_refresh(supervisor: Any, run_id: str) -> dict[str, Any]:
            wave = threading.current_thread().name
            with gauge._lock:
                gauge._in_flight += 1
                gauge.calls.append(run_id)
                gauge.call_events.append((wave, run_id))
                if gauge._in_flight > gauge.max_in_flight:
                    gauge.max_in_flight = gauge._in_flight
            try:
                if gauge._sleep_s:
                    time.sleep(gauge._sleep_s)
                return original(supervisor, run_id)
            finally:
                with gauge._lock:
                    gauge._in_flight -= 1

        self._patch = mock.patch.object(RunSupervisor, "_refresh", gauged_refresh)
        self._patch.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._patch.stop()


def _concurrent_waves(
    supervisor: RunSupervisor,
    *,
    second_waits_for_first: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Two threads through ``list_run_items`` synchronized on a barrier.

    With ``second_waits_for_first`` the second thread additionally waits
    for the first wave's completion: the "reuse the first wave's terminal
    results" property only exists once the first wave has persisted its
    terminal decisions.
    """

    barrier = threading.Barrier(2)
    first_done = threading.Event()
    results: dict[str, list[dict[str, Any]]] = {}
    errors: list[BaseException] = []

    def wave(name: str, gate: threading.Event | None) -> None:
        try:
            barrier.wait(timeout=30)
            if gate is not None:
                gate.wait(timeout=30)
            results[name] = supervisor.list_run_items()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=wave, args=("first", None), name="wave-first"),
        threading.Thread(
            target=wave,
            args=("second", first_done if second_waits_for_first else None),
            name="wave-second",
        ),
    ]
    for thread in threads:
        thread.start()
    threads[0].join(timeout=60)
    first_done.set()
    threads[1].join(timeout=60)
    assert not errors, f"wave threads failed: {errors!r}"
    return results["first"], results["second"]


def test_wave_refresh_in_flight_never_exceeds_cap(tmp_path: Path) -> None:
    """Combined max in-flight _refresh across two concurrent waves <= cap.

    VARIANT: the sibling slice runs-reconcile-lock-hold has NOT landed on
    main (9749a25d -- ``_refresh``'s head has no early probe line), so the
    asserted bound is ``<= cap`` (3), not ``<= cap + 1``.
    """

    root = tmp_path / "runs"
    root.mkdir()
    ids = _make_dead_active_rows(root, 6)

    supervisor = RunSupervisor(root)

    with _InFlightGauge() as gauge:
        _concurrent_waves(supervisor)

    assert gauge.max_in_flight <= DEFAULT_CAP, (
        "combined in-flight _refresh across overlapping waves exceeded the "
        f"cap: max {gauge.max_in_flight} > {DEFAULT_CAP}"
    )
    assert sorted(set(gauge.calls)) == sorted(ids), (
        "every active row must still be reconciled by id across the two "
        f"waves; refreshed ids: {sorted(set(gauge.calls))}"
    )

    # Make the cap BINDING: with the default cap of 3, two waves cannot
    # exceed it by construction, so pin the semaphore itself with cap=1 --
    # without the bounded wrapper two concurrent waves would overlap and
    # the gauge would read 2.
    with mock.patch.dict(os.environ, {ENV_KEY: "1"}):
        capped = RunSupervisor(root)
        with _InFlightGauge() as gauge:
            _concurrent_waves(capped)
    assert gauge.max_in_flight <= 1, (
        "with LH_HARNESS_RECONCILE_CONCURRENCY=1 the semaphore must "
        f"serialize the waves; observed max in-flight {gauge.max_in_flight}"
    )


def test_cap_is_configurable_and_clamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    root.mkdir()

    def _initial_value(value: str | None) -> int:
        if value is None:
            monkeypatch.delenv(ENV_KEY, raising=False)
        else:
            monkeypatch.setenv(ENV_KEY, value)
        supervisor = RunSupervisor(root)
        return int(supervisor._reconcile_slots._initial_value)

    assert _initial_value("16") == 8, "cap must clamp 16 down to 8"
    assert _initial_value("0") == 1, "cap must clamp 0 up to 1"
    assert _initial_value("abc") == 3, "invalid cap falls back to the default 3"
    assert _initial_value(None) == 3, "unset cap defaults to 3"


def test_excess_concurrent_waves_reuse_results_not_stack(tmp_path: Path) -> None:
    """A wave arriving after another terminalized the rows refreshes none.

    The first wave reconciles the 6 dead-pid actives to terminal "failed"
    and persists that decision; the second wave must take the terminal
    fast path for every row (0 active-row refreshes) and return the
    durable statuses.  Total calls stay far under the 6 + 2*cap bound.
    """

    root = tmp_path / "runs"
    root.mkdir()
    ids = _make_dead_active_rows(root, 6)

    supervisor = RunSupervisor(root)

    with _InFlightGauge() as gauge:
        first, second = _concurrent_waves(
            supervisor, second_waits_for_first=True
        )

    assert len(gauge.calls) <= len(ids) + 2 * DEFAULT_CAP, (
        f"total _refresh calls {len(gauge.calls)} exceed the "
        f"{len(ids) + 2 * DEFAULT_CAP} bound"
    )
    assert gauge.calls_for("wave-second") == [], (
        "the second wave must reuse the first wave's terminal results, not "
        f"stack its own fan-out; it refreshed: {gauge.calls_for('wave-second')}"
    )
    assert {item["id"] for item in second} == set(ids), (
        "the second wave must still list every run"
    )
    statuses = {item["id"]: item["status"] for item in second}
    assert set(statuses.values()) == {"failed"}, (
        "the second wave must return the durable terminal statuses "
        f"reconciled by the first wave; got {statuses}"
    )
    first_statuses = {item["id"]: item["status"] for item in first}
    assert first_statuses == statuses, (
        "both waves must agree on the reconciled terminal statuses"
    )


def test_terminal_rows_still_skip_refresh(tmp_path: Path) -> None:
    """Regression guard: the call-site swap keeps the terminal fast path."""

    root = tmp_path / "runs"
    root.mkdir()
    for index, status in enumerate(("cancelled", "failed", "completed")):
        _make_run(root, f"term{index:02d}run", status=status, pid=0)

    supervisor = RunSupervisor(root)

    with _InFlightGauge(sleep_s=0.0) as gauge:
        items = supervisor.list_run_items()

    assert gauge.calls == [], (
        "terminal runs must not be reconciled through _refresh in the full "
        f"listing; refreshed: {gauge.calls}"
    )
    assert len(items) == 3


def test_sequential_wave_results_unchanged(tmp_path: Path) -> None:
    """One sequential wave over a mixed set: main-branch id/status mapping.

    The cap must not change any result.  The mixed set: 2 terminal rows,
    2 dead-pid actives (no report -> reconciled to "failed"), and 1 live
    row owned by this very pid (with its real ``pid_start_identity`` and
    a ``ps`` command line naming the harness and the run id, mocked at
    the ``_ps_query`` seam).
    """

    root = tmp_path / "runs"
    root.mkdir()
    _make_run(root, "term-a", status="cancelled", pid=0)
    _make_run(root, "term-b", status="completed", pid=0)
    _make_run(
        root,
        "dead-a",
        status="running",
        pid=DEAD_PID_BASE + 90,
        pid_start_identity="proc:900090",
        with_report=False,
    )
    _make_run(
        root,
        "dead-b",
        status="running",
        pid=DEAD_PID_BASE + 91,
        pid_start_identity="proc:900091",
        with_report=False,
    )
    _make_run(
        root,
        "live-a",
        status="running",
        pid=os.getpid(),
        pid_start_identity=_pid_start_identity(os.getpid()) or "proc:self",
        attached=False,
    )

    supervisor = RunSupervisor(root)

    with mock.patch.object(
        service_module,
        "_ps_query",
        lambda pid, field: "lh-harness worker --run-id live-a",
    ):
        items = supervisor.list_run_items()

    # Hardcoded main-branch snapshot: ids and statuses identical with and
    # without the bounded wrapper.
    assert {item["id"] for item in items} == {
        "term-a",
        "term-b",
        "dead-a",
        "dead-b",
        "live-a",
    }
    assert {item["id"]: item["status"] for item in items} == {
        "term-a": "cancelled",
        "term-b": "completed",
        "dead-a": "failed",
        "dead-b": "failed",
        "live-a": "running",
    }
