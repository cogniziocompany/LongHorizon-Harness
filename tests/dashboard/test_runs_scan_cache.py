"""Tests for the cached runs scan in ``DashboardState`` (easybutt0n-17).

Every ``GET /api/runs`` used to walk the whole runs root again: one
``iterdir`` over the run directories, a ``stat`` per role directory and a
``report.json`` read per run.  Concurrent pollers therefore filled the web
process's thread pool with filesystem work and every authenticated endpoint
stopped answering.

``DashboardState._scan_runs`` now keeps a ``(timestamp, list)`` pair on the
instance and reuses it while it is younger than
``LH_HARNESS_RUNS_SCAN_TTL`` seconds (default 5, ``0`` disables the cache).
The rescan runs under a lock, so concurrent callers trigger exactly one
scan and the rest reuse that result.

Covered here:

- two ``list_runs()`` calls inside the TTL cause a single scan;
- the scan is redone once the cached result has aged past the TTL;
- ``LH_HARNESS_RUNS_SCAN_TTL=0`` rescans on every call;
- twenty concurrent ``list_runs()`` calls cause exactly one scan.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from lh_harness.dashboard.state import DashboardState


def _make_run(root: Path, run_id: str, *, status: str = "ok") -> None:
    """Create one run directory in the layout ``_scan_runs`` recognises."""

    logs = root / run_id / "lh_harness" / "role_orchestration"
    role = logs / "role_management"
    role.mkdir(parents=True)
    (role / "rounds").mkdir()
    (logs / "report.json").write_text(json.dumps({"status": status}), encoding="utf-8")


def _runs_root(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir()
    _make_run(root, "run-a")
    return root


class _ScanCounter:
    """Wrap ``Path.iterdir`` so every runs-root scan is counted."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls = 0
        self._lock = threading.Lock()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = Path.iterdir
        counter = self

        def counting_iterdir(self: Path, *args: object, **kwargs: object):
            if self == counter.root:
                with counter._lock:
                    counter.calls += 1
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "iterdir", counting_iterdir)

    def reset(self) -> None:
        with self._lock:
            self.calls = 0

    @property
    def count(self) -> int:
        with self._lock:
            return self.calls


def _countered_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[DashboardState, _ScanCounter]:
    """Build a state whose scan counter is installed before construction.

    ``DashboardState.__init__`` scans the runs root once to auto-select the
    newest run, so the counter has to be in place before the object exists.
    The counter is reset afterwards so the tests measure only the scans
    triggered by the calls under test.
    """

    root = _runs_root(tmp_path)
    counter = _ScanCounter(root)
    counter.install(monkeypatch)
    state = DashboardState(runs_root=root)
    # ``__init__`` scans the runs root once to auto-select the newest run;
    # drop that warm cache and zero the counter so the tests measure only the
    # scans triggered by the calls under test.
    state._invalidate_runs_scan_cache()
    counter.reset()
    return state, counter


def test_two_calls_within_ttl_scan_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LH_HARNESS_RUNS_SCAN_TTL", "5")
    state, counter = _countered_state(tmp_path, monkeypatch)

    first = state.list_runs()
    second = state.list_runs()

    assert counter.count == 1
    assert first == second
    assert [item["id"] for item in first] == ["run-a"]


def test_rescan_after_ttl_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LH_HARNESS_RUNS_SCAN_TTL", "5")
    state, counter = _countered_state(tmp_path, monkeypatch)

    clock = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    state.list_runs()
    assert counter.count == 1

    # Still inside the TTL: the cached listing is reused.
    clock["now"] = 1004.0
    state.list_runs()
    assert counter.count == 1

    # Past the TTL: the runs root is walked again.
    clock["now"] = 1006.0
    state.list_runs()
    assert counter.count == 2


def test_ttl_zero_always_rescans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LH_HARNESS_RUNS_SCAN_TTL", "0")
    state, counter = _countered_state(tmp_path, monkeypatch)

    state.list_runs()
    state.list_runs()
    state.list_runs()

    assert counter.count == 3


def test_concurrent_callers_scan_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LH_HARNESS_RUNS_SCAN_TTL", "5")
    state, counter = _countered_state(tmp_path, monkeypatch)

    start = threading.Barrier(20)
    results: list[list[dict]] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            start.wait(timeout=10)
            results.append(state.list_runs())
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert len(results) == 20
    assert counter.count == 1
    for item in results:
        assert [entry["id"] for entry in item] == ["run-a"]


def test_returned_list_is_a_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers get a shallow copy, so they cannot mutate the cached list."""

    monkeypatch.setenv("LH_HARNESS_RUNS_SCAN_TTL", "5")
    state, _counter = _countered_state(tmp_path, monkeypatch)

    first = state.list_runs()
    first.append({"id": "injected"})
    first.clear()

    second = state.list_runs()
    assert [item["id"] for item in second] == ["run-a"]
