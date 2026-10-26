"""LHH-CT110-MEMORY-ADMISSION: node-wide atomic admission in the launcher.

The incident that motivated this: the per-lane queue_capacity rows summed
to far more than the fleet node record's maxConcurrentRuns, and the
launcher admitted per lane only, so many more runs ran at once than the
node could hold; the OOM killer took the service and every launched run
ended "worker disappeared without a final report".  These tests pin the
three admission limits (A), the memory headroom gate (B),
release/reconciliation (C), the bound of existing in-flight with no kill
(D), and the sum(caps) > node_max clamp (E) -- all through a fake run
store and a fake meminfo probe (F).
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from lh_harness.launcher import (
    DEFAULT_MEM_FLOOR_MB,
    DEFAULT_NODE_MAX_ADMISSION,
    DEFAULT_RUN_STARTUP_MB,
    MEMORY_HEADROOM_SKIP_REASON,
    WORKER_DISAPPEARED_REASON,
    Launcher,
    _admission_settings_from_values,
    read_mem_available_mb,
)
from lh_harness.queue import default_queue_config

from .test_launcher import FakeSupervisor, _base_entry, _fixture


def _config(**capacity: int) -> dict[str, Any]:
    config = default_queue_config()
    config["capacity"].update(capacity)
    return config


def _launcher(
    tmp_path: Path,
    *,
    meminfo_mb: float | None = 8000.0,
    capacity: dict[str, int] | None = None,
    settings_values: dict[str, str] | None = None,
) -> tuple[Launcher, Any, Any]:
    """A Launcher over the fake run store with a fake meminfo probe."""

    root, store, supervisor = _fixture(tmp_path)
    config = _config(**(capacity or {}))
    launcher = Launcher(supervisor, store, queue_config=config)
    # Fake meminfo (deliverable F): the probe seam replaces /proc/meminfo.
    launcher._meminfo_probe = (lambda: meminfo_mb) if meminfo_mb is not _SENTINEL else None
    if settings_values is not None:
        class _FakeStore:
            def values(self_inner: Any) -> dict[str, str]:
                return dict(settings_values)

        launcher._settings_store = _FakeStore()
    return launcher, store, supervisor


_SENTINEL = object()


# ---------------------------------------------------------------------------
# Deliverable A: atomic node-wide admission
# ---------------------------------------------------------------------------

def test_node_admission_counts_all_lanes_and_refuses_at_node_max(tmp_path: Path) -> None:
    """Running runs in DIFFERENT lanes still count toward node_max.

    Lane caps are wide open (5 each) and the runs are spread over three
    trios, so no per-lane check could ever refuse; only the node gate can.
    This is the exact ct110 shape (lane caps summing past the node maximum).
    """

    launcher, store, supervisor = _launcher(tmp_path, capacity={"kimi_max": 5, "qwen_max": 5, "glm_max": 5})
    supervisor.add_run("active-1", "./busy-1", status="running", trio="kimi")
    supervisor.add_run("active-2", "./busy-2", status="running", trio="qwen")
    supervisor.add_run("active-3", "./busy-3", status="running", trio="glm")
    supervisor.add_run("active-4", "./busy-4", status="running", trio="kimi")
    supervisor.add_run("active-5", "./busy-5", status="running", trio="qwen")

    entry = store.create(_base_entry(trio="kimi", workspace="./free"))
    asyncio.run(launcher.tick())

    refused = store.get(entry.queue_id)
    assert refused is not None
    assert refused.status == "pending"
    assert any("node at capacity" in r for r in refused.skip_reasons), refused.skip_reasons
    assert supervisor.created == []


def test_node_admission_counts_starting_runs_too(tmp_path: Path) -> None:
    """A run still in creating/starting holds a node slot (deliverable A).

    The startup window is exactly where RSS grows fastest; a starting run
    must not let the launcher admit a peer into the last slot.
    """

    launcher, store, supervisor = _launcher(tmp_path, capacity={"kimi_max": 5, "qwen_max": 5})
    supervisor.add_run("booting-1", "./busy-1", status="starting", trio="kimi")
    supervisor.add_run("booting-2", "./busy-2", status="creating", trio="qwen")
    supervisor.add_run("active-3", "./busy-3", status="running", trio="glm")
    supervisor.add_run("active-4", "./busy-4", status="running", trio="kimi")
    supervisor.add_run("active-5", "./busy-5", status="running", trio="qwen")

    entry = store.create(_base_entry(trio="kimi", workspace="./free"))
    asyncio.run(launcher.tick())

    refused = store.get(entry.queue_id)
    assert refused is not None
    assert refused.status == "pending"
    assert any("node at capacity" in r for r in refused.skip_reasons), refused.skip_reasons


def test_admission_releases_when_a_worker_exits(tmp_path: Path) -> None:
    """Deliverable C: an exiting worker frees its slot on the next pass.

    Five runs fill the node; after one goes terminal the next pass launches
    exactly one new run (the freed slot) and refuses the second entry.
    """

    launcher, store, supervisor = _launcher(tmp_path, capacity={"kimi_max": 5, "qwen_max": 5})
    for i in range(5):
        supervisor.add_run(f"active-{i}", f"./busy-{i}", status="running", trio="kimi")

    entry = store.create(_base_entry(trio="kimi", workspace="./free"))
    asyncio.run(launcher.tick())
    assert store.get(entry.queue_id).status == "pending"

    # One worker exits: its slot is released by the next pass's count.
    supervisor._statuses["active-0"] = {"status": "completed", "run_id": "active-0"}
    supervisor._runs.pop("active-0", None)
    asyncio.run(launcher.tick())

    assert store.get(entry.queue_id).status == "launched"
    assert len(supervisor.created) == 1
    assert launcher.admission_snapshot()["running"] + launcher.admission_snapshot()["starting"] < 5


def test_admission_is_one_decision_per_pass_under_a_lock(tmp_path: Path) -> None:
    """Deliverable A: one decision per launch cycle, serialized by the lock.

    Seven pending entries, node_max 5, empty node.  A pass launches at most
    one entry (existing launcher design: the batch flag), but across passes
    the node count is re-read from the authoritative store, so the sixth
    launch is never admitted no matter how many passes run -- even though
    every lane cap (9) still has room.  That is the atomicity the incident
    was missing: the running+starting count is a cross-lane, node-level fact.
    """

    launcher, store, supervisor = _launcher(tmp_path, capacity={"kimi_max": 9, "qwen_max": 9})
    entries = [
        store.create(_base_entry(trio="kimi", priority=10 - i, workspace=f"./w{i}"))
        for i in range(7)
    ]
    for _ in range(10):
        asyncio.run(launcher.tick())

    launched = [e for e in entries if store.get(e.queue_id).status == "launched"]
    assert len(launched) == DEFAULT_NODE_MAX_ADMISSION
    assert len(supervisor.created) == DEFAULT_NODE_MAX_ADMISSION
    snapshot = launcher.admission_snapshot()
    assert snapshot["running"] + snapshot["starting"] == DEFAULT_NODE_MAX_ADMISSION
    for e in entries[DEFAULT_NODE_MAX_ADMISSION:]:
        assert any("node at capacity" in r for r in store.get(e.queue_id).skip_reasons)


def test_concurrent_passes_never_admit_past_node_max(tmp_path: Path) -> None:
    """Two passes run concurrently; together they never exceed node_max.

    The admission lock serializes the decision; the node count includes
    launches the other pass made (it re-reads the store/supervisor), so the
    two together cannot admit into the last slot twice.
    """

    launcher, store, supervisor = _launcher(tmp_path, capacity={"kimi_max": 9, "qwen_max": 9})
    for i in range(4):
        supervisor.add_run(f"active-{i}", f"./busy-{i}", status="running", trio="kimi")
    for i in range(4):
        store.create(_base_entry(trio="kimi", priority=10 - i, workspace=f"./w{i}"))

    def _run_passes() -> None:
        for _ in range(3):
            launcher._tick_sync()

    threads = [threading.Thread(target=_run_passes) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snapshot = launcher.admission_snapshot()
    total = snapshot["running"] + snapshot["starting"]
    assert total <= DEFAULT_NODE_MAX_ADMISSION
    assert len(supervisor.created) + 4 <= DEFAULT_NODE_MAX_ADMISSION


def test_node_max_runs_setting_overrides_default(tmp_path: Path) -> None:
    """LH_HARNESS_NODE_MAX_RUNS in the settings store beats the default 5."""

    launcher, store, supervisor = _launcher(
        tmp_path, settings_values={"LH_HARNESS_NODE_MAX_RUNS": "3"}, capacity={"kimi_max": 9}
    )
    for i in range(3):
        supervisor.add_run(f"active-{i}", f"./busy-{i}", status="running", trio="kimi")
    entry = store.create(_base_entry(trio="kimi", workspace="./free"))
    asyncio.run(launcher.tick())

    refused = store.get(entry.queue_id)
    assert refused.status == "pending"
    assert any("node max 3" in r for r in refused.skip_reasons), refused.skip_reasons


# ---------------------------------------------------------------------------
# Deliverable B: the memory headroom gate
# ---------------------------------------------------------------------------

def test_memory_headroom_gate_refuses_and_records_the_skip_reason(tmp_path: Path) -> None:
    """MemAvailable 1500: 1500 - 600 < 1024, so the launch is refused.

    The entry's latest skip reason must carry the exact ``memory_headroom``
    marker and the entry stays pending.
    """

    launcher, store, supervisor = _launcher(tmp_path, meminfo_mb=1500.0, capacity={"kimi_max": 5})
    entry = store.create(_base_entry(trio="kimi", workspace="./free"))
    asyncio.run(launcher.tick())

    refused = store.get(entry.queue_id)
    assert refused is not None
    assert refused.status == "pending"
    latest = refused.skip_reasons[-1]
    assert latest.startswith(MEMORY_HEADROOM_SKIP_REASON), refused.skip_reasons
    assert "1500" in latest and "1024" in latest
    assert supervisor.created == []


def test_memory_headroom_gate_passes_with_headroom(tmp_path: Path) -> None:
    """MemAvailable 3000: 3000 - 600 >= 1024, so the launch goes through."""

    launcher, store, supervisor = _launcher(tmp_path, meminfo_mb=3000.0, capacity={"kimi_max": 5})
    entry = store.create(_base_entry(trio="kimi", workspace="./free"))
    asyncio.run(launcher.tick())

    assert store.get(entry.queue_id).status == "launched"
    assert len(supervisor.created) == 1


def test_memory_headroom_boundary_is_exclusive(tmp_path: Path) -> None:
    """MemAvailable exactly floor + startup (1624) passes; one MB less fails."""

    launcher, store, supervisor = _launcher(tmp_path, meminfo_mb=1624.0, capacity={"kimi_max": 5})
    entry = store.create(_base_entry(trio="kimi", workspace="./free"))
    asyncio.run(launcher.tick())
    assert store.get(entry.queue_id).status == "launched"

    launcher2, store2, supervisor2 = _launcher(tmp_path / "boundary", meminfo_mb=1623.9, capacity={"kimi_max": 5})
    entry2 = store2.create(_base_entry(trio="kimi", workspace="./free"))
    asyncio.run(launcher2.tick())
    refused = store2.get(entry2.queue_id)
    assert refused.status == "pending"
    assert refused.skip_reasons[-1].startswith(MEMORY_HEADROOM_SKIP_REASON)


def test_memory_settings_come_from_the_settings_store(tmp_path: Path) -> None:
    """LH_HARNESS_RUN_STARTUP_MB / LH_HARNESS_MEM_FLOOR_MB override defaults."""

    # floor 2500: 3000 - 600 = 2400 < 2500 -> refused (default floor would pass)
    launcher, store, supervisor = _launcher(
        tmp_path,
        meminfo_mb=3000.0,
        capacity={"kimi_max": 5},
        settings_values={"LH_HARNESS_MEM_FLOOR_MB": "2500"},
    )
    entry = store.create(_base_entry(trio="kimi", workspace="./free"))
    asyncio.run(launcher.tick())
    refused = store.get(entry.queue_id)
    assert refused.status == "pending"
    assert refused.skip_reasons[-1].startswith(MEMORY_HEADROOM_SKIP_REASON)

    # startup allowance 2500: 3000 - 2500 = 500 < 1024 -> refused too
    launcher2, store2, supervisor2 = _launcher(
        tmp_path / "startup",
        meminfo_mb=3000.0,
        capacity={"kimi_max": 5},
        settings_values={"LH_HARNESS_RUN_STARTUP_MB": "2500"},
    )
    entry2 = store2.create(_base_entry(trio="kimi", workspace="./free"))
    asyncio.run(launcher2.tick())
    refused2 = store2.get(entry2.queue_id)
    assert refused2.status == "pending"
    assert refused2.skip_reasons[-1].startswith(MEMORY_HEADROOM_SKIP_REASON)


def test_missing_meminfo_fails_open_but_node_cap_still_bounds(tmp_path: Path) -> None:
    """No meminfo at all: the memory gate steps aside, the node cap holds."""

    launcher, store, supervisor = _launcher(tmp_path, meminfo_mb=None, capacity={"kimi_max": 9})
    for i in range(5):
        supervisor.add_run(f"active-{i}", f"./busy-{i}", status="running", trio="kimi")
    entry = store.create(_base_entry(trio="kimi", workspace="./free"))
    asyncio.run(launcher.tick())
    refused = store.get(entry.queue_id)
    assert refused.status == "pending"
    assert any("node at capacity" in r for r in refused.skip_reasons)
    assert not any(r.startswith(MEMORY_HEADROOM_SKIP_REASON) for r in refused.skip_reasons)


def test_read_mem_available_mb_with_fake_meminfo(tmp_path: Path) -> None:
    """The procfs reader parses MemAvailable and honours a finite cgroup limit."""

    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       16000000 kB\nMemFree:         1000000 kB\nMemAvailable:    4000000 kB\n"
    )
    unlimited = tmp_path / "memory.max"
    # follow_self_cgroup=False: read EXACTLY the paths given (fake files),
    # never the test host's own cgroup limit.
    assert read_mem_available_mb(meminfo, unlimited, follow_self_cgroup=False) == pytest.approx(
        4000000 / 1024.0
    )
    unlimited.write_text("max")
    assert read_mem_available_mb(meminfo, unlimited, follow_self_cgroup=False) == pytest.approx(
        4000000 / 1024.0
    )
    # A 2 GiB container limit bounds the host-wide number.
    unlimited.write_text(str(2 * 1024 * 1024 * 1024))
    assert read_mem_available_mb(meminfo, unlimited, follow_self_cgroup=False) == pytest.approx(2048.0)
    # Neither readable -> None (the caller decides fail-open).
    broken = tmp_path / "broken"
    assert read_mem_available_mb(broken, broken, follow_self_cgroup=False) is None


# ---------------------------------------------------------------------------
# Deliverable C: release and startup reconciliation
# ---------------------------------------------------------------------------

def test_startup_reconciliation_marks_orphaned_launched_failed(tmp_path: Path) -> None:
    """A launched entry whose run vanished is failed, crash-named, not requeued.

    After a service crash (the incident's 13 runs), the entry is 'launched'
    but the supervisor never heard of the run again: ``start()`` reconciles
    it to failed with the existing 'worker disappeared' reason and creates NO
    successor entry.
    """

    launcher, store, supervisor = _fixture(tmp_path)[:3]
    launcher_obj = Launcher(supervisor, store, queue_config=_config(kimi_max=5))

    entry = store.create(_base_entry(trio="kimi", workspace="./w"))
    launched = store.mark_launched(entry.queue_id, "run-that-vanished")
    assert launched is not None

    before = {e.queue_id for e in store.list()}
    asyncio.run(launcher_obj.start())
    asyncio.run(launcher_obj.stop())

    reconciled = store.get(entry.queue_id)
    assert reconciled is not None
    assert reconciled.status == "failed"
    assert WORKER_DISAPPEARED_REASON in (reconciled.reason or "")
    # Never requeued: the same single entry, no successor row appeared.
    assert {e.queue_id for e in store.list()} == before


def test_admission_count_reconciled_from_live_processes_at_start(tmp_path: Path) -> None:
    """Deliverable C/D: at start the count is rebuilt from live processes.

    Four live runs exist before the launcher first runs; ``start()``'s
    reconcile rebuilds the count from the live set, so the first pass admits
    exactly one (filling node_max) and refuses the rest with the node reason.
    """

    launcher, store, supervisor = _launcher(tmp_path, capacity={"kimi_max": 9, "qwen_max": 9})
    for i in range(4):
        supervisor.add_run(f"active-{i}", f"./busy-{i}", status="running", trio="kimi")
    entries = [
        store.create(_base_entry(trio="kimi", priority=10 - i, workspace=f"./w{i}"))
        for i in range(3)
    ]
    asyncio.run(launcher.start())
    try:
        # start()'s own reconcile pass admits one; the loop's first tick runs
        # immediately after and refuses the rest.
        asyncio.run(launcher.tick())
    finally:
        asyncio.run(launcher.stop())

    launched = [e for e in entries if store.get(e.queue_id).status == "launched"]
    assert len(launched) == 1
    # One more pass refreshes the snapshot: the new run is "creating"
    # (starting), so the node count is 4 running + 1 starting = node_max,
    # rebuilt from the live set, not from the launcher's own bookkeeping.
    asyncio.run(launcher.tick())
    snapshot = launcher.admission_snapshot()
    assert snapshot["running"] == 4
    assert snapshot["starting"] == 1
    assert snapshot["running"] + snapshot["starting"] == DEFAULT_NODE_MAX_ADMISSION
    assert snapshot["node_max"] == DEFAULT_NODE_MAX_ADMISSION


# ---------------------------------------------------------------------------
# Deliverable D: bound existing in-flight, kill nothing, /api/meta state
# ---------------------------------------------------------------------------

def test_over_limit_at_start_kills_nothing_and_admits_nothing(tmp_path: Path) -> None:
    """Node ALREADY over node_max at start: nothing is killed, nothing admitted.

    Seven live runs against node_max 5 (the post-cap-change shape): the
    launcher must not stop any of them, and must admit nothing until the
    count drops below the cap.
    """

    launcher, store, supervisor = _launcher(tmp_path, capacity={"kimi_max": 9})
    for i in range(7):
        supervisor.add_run(f"active-{i}", f"./busy-{i}", status="running", trio="kimi")

    entry = store.create(_base_entry(trio="kimi", workspace="./free"))
    asyncio.run(launcher.tick())

    # Nothing killed: every pre-existing run is still running.
    for i in range(7):
        assert supervisor.status(f"active-{i}")["status"] == "running"
    # Nothing admitted.
    assert supervisor.created == []
    refused = store.get(entry.queue_id)
    assert refused.status == "pending"
    assert any("node at capacity" in r for r in refused.skip_reasons)


def test_admission_snapshot_carries_exactly_the_five_meta_fields(tmp_path: Path) -> None:
    """/api/meta's admission object: node_max, running, starting,
    mem_available_mb, blocked_reason -- exactly these five keys."""

    launcher, store, supervisor = _launcher(tmp_path, meminfo_mb=900.0, capacity={"kimi_max": 5})
    for i in range(5):
        supervisor.add_run(f"active-{i}", f"./busy-{i}", status="running", trio="kimi")
    store.create(_base_entry(trio="kimi", workspace="./free"))
    asyncio.run(launcher.tick())

    snapshot = launcher.admission_snapshot()
    assert set(snapshot.keys()) == {
        "node_max", "running", "starting", "mem_available_mb", "blocked_reason",
    }
    assert snapshot["node_max"] == 5
    assert snapshot["running"] == 5
    assert snapshot["starting"] == 0
    assert snapshot["mem_available_mb"] is not None
    assert snapshot["blocked_reason"] == "node_max"


def test_admission_snapshot_blocked_reason_memory(tmp_path: Path) -> None:
    """blocked_reason is memory_headroom when the memory gate is the blocker."""

    launcher, store, supervisor = _launcher(tmp_path, meminfo_mb=500.0, capacity={"kimi_max": 5})
    store.create(_base_entry(trio="kimi", workspace="./free"))
    asyncio.run(launcher.tick())
    snapshot = launcher.admission_snapshot()
    assert snapshot["blocked_reason"] == MEMORY_HEADROOM_SKIP_REASON
    assert snapshot["mem_available_mb"] == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# Deliverable E: sum(caps) > node_max warning + clamp, regression test
# ---------------------------------------------------------------------------

def test_regression_sum_of_lane_caps_over_node_max_never_admits_more(tmp_path: Path) -> None:
    """THE regression test for the incident shape.

    Lane caps sum to 20 across seven lanes against node_max 5 -- the shape
    of a queue_capacity table whose lanes were configured far above what
    the node could hold.  With an empty node and 20 pending entries spread
    over every lane, no sequence of passes may ever leave more than
    node_max runs running+starting, even though every single lane cap still
    has room.  That invariant is what per-lane checks alone could not
    express.
    """

    caps = {
        "glm_max": 5, "minimax_max": 4, "qwen_max": 4, "zai_max": 2,
        "mm31f_max": 2, "zaiq_max": 2, "kimi_max": 1,
    }
    launcher, store, supervisor = _launcher(tmp_path, capacity=caps)
    assert sum(caps.values()) == 20
    assert sum(caps.values()) > DEFAULT_NODE_MAX_ADMISSION
    trios = [key[: -len("_max")] for key in caps]
    for i in range(20):
        store.create(_base_entry(trio=trios[i % len(trios)], priority=20 - i, workspace=f"./w{i}"))
    for _ in range(20):
        asyncio.run(launcher.tick())

    assert len(supervisor.created) <= DEFAULT_NODE_MAX_ADMISSION
    snapshot = launcher.admission_snapshot()
    assert snapshot["running"] + snapshot["starting"] <= DEFAULT_NODE_MAX_ADMISSION
    # The launcher did run and did launch: the cap bound real launches,
    # not an inert gate.
    assert len(supervisor.created) >= 1


def test_caps_over_node_max_logs_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Deliverable E: the mismatch is surfaced as a WARNING, once per process."""

    import logging

    launcher, store, supervisor = _launcher(tmp_path, capacity={"kimi_max": 9, "qwen_max": 9})
    with caplog.at_level(logging.WARNING, logger="lh_harness.launcher"):
        asyncio.run(launcher.tick())
        asyncio.run(launcher.tick())  # second pass: the warning must not repeat
    warnings = [r for r in caplog.records if "admission clamp" in r.getMessage()]
    assert len(warnings) == 1
    assert "node maximum" in warnings[0].getMessage()


def test_admission_settings_resolver_defaults_and_overrides() -> None:
    """The resolver: documented defaults, settings-store overrides, bad values."""

    assert _admission_settings_from_values({}) == {
        "node_max": DEFAULT_NODE_MAX_ADMISSION,
        "startup_mb": DEFAULT_RUN_STARTUP_MB,
        "mem_floor_mb": DEFAULT_MEM_FLOOR_MB,
    }
    resolved = _admission_settings_from_values({
        "LH_HARNESS_NODE_MAX_RUNS": "8",
        "LH_HARNESS_RUN_STARTUP_MB": "700",
        "LH_HARNESS_MEM_FLOOR_MB": "2048",
    })
    assert resolved == {"node_max": 8, "startup_mb": 700, "mem_floor_mb": 2048}
    # Garbage keeps the defaults; the gate never dies on a bad row.
    assert _admission_settings_from_values({"LH_HARNESS_NODE_MAX_RUNS": "banana"})["node_max"] == 5
    assert _admission_settings_from_values(None)["mem_floor_mb"] == DEFAULT_MEM_FLOOR_MB


def test_settings_catalog_lists_the_three_admission_names() -> None:
    """The three setting names are registered in the settings store catalog."""

    from lh_harness import settings_store

    names = {spec.name for spec in settings_store.CATALOG}
    assert "LH_HARNESS_NODE_MAX_RUNS" in names
    assert "LH_HARNESS_RUN_STARTUP_MB" in names
    assert "LH_HARNESS_MEM_FLOOR_MB" in names


# ---------------------------------------------------------------------------
# Deliverable D: the /api/meta surface (through the real HTTP app)
# ---------------------------------------------------------------------------

def test_api_meta_exposes_the_admission_object(tmp_path: Path) -> None:
    """/api/meta.admission carries exactly the five documented keys."""

    from fastapi.testclient import TestClient

    from lh_harness.webapi.server import create_app

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    supervisor = FakeSupervisor(root)
    for i in range(5):
        supervisor.add_run(f"active-{i}", f"./busy-{i}", status="running", trio="kimi")
    client = TestClient(create_app(runs_root=root, supervisor=supervisor))
    launcher = client.app.state.launcher
    launcher._meminfo_probe = lambda: 4000.0
    asyncio.run(launcher.tick())

    admission = client.get("/api/meta").json()["admission"]
    assert set(admission.keys()) == {
        "node_max", "running", "starting", "mem_available_mb", "blocked_reason",
    }
    assert admission["node_max"] == 5
    assert admission["running"] == 5
    assert admission["starting"] == 0
    assert admission["mem_available_mb"] == pytest.approx(4000.0)
    assert admission["blocked_reason"] == "node_max"


def test_api_meta_omits_admission_when_no_launcher(tmp_path: Path) -> None:
    """A supervisor-less app has no launcher and no admission object."""

    from fastapi.testclient import TestClient

    from lh_harness.webapi.server import create_app

    client = TestClient(create_app(runs_root=tmp_path / "runs"))
    assert "admission" not in client.get("/api/meta").json()