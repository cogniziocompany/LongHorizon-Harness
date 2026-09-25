"""Cross-process launcher lease — task 173, scope 5 / migration doc §4.2.

``runs_root/queue/.lease`` holds ``{pid, host, ts}`` for the one launcher
process that may run passes against a runs root.  It is minted with O_EXCL,
refreshed on every pass the holder runs, and goes stale after 3 missed
intervals (interval = the launcher poll interval), at which point another
launcher may reclaim it.  A second launcher process that finds a live lease it
does not own logs and idles for that pass.

Cross-process behaviour is simulated by taking leases under explicit foreign
``pid``/``host`` values — the real guarantee is exactly this identity check,
done by two OS processes that necessarily differ in pid.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from pathlib import Path
from typing import Any

from lh_harness.launcher import Launcher
from lh_harness.queue import (
    QueueStore,
    acquire_lease,
    default_queue_config,
    read_lease,
)

from .test_launcher import FakeSupervisor, _base_entry

_OTHER_PID = 424_242
_OTHER_HOST = "other-launcher-host"

# Well above filesystem timestamp granularity, far below any test timeout.
_TICK_SLEEP = 0.05


def _fixture(tmp_path: Path) -> tuple[Path, QueueStore, FakeSupervisor]:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = FakeSupervisor(root)
    return root, store, supervisor


def _launcher(
    store: QueueStore,
    supervisor: FakeSupervisor,
    *,
    poll_seconds: float = 1.0,
) -> Launcher:
    config = default_queue_config()
    # One poll second => a 3 s staleness horizon: wide enough that test-execution
    # delay cannot flip a "live" case stale, while a stale fixture is written
    # well past the horizon.
    config["capacity"]["poll_seconds"] = poll_seconds
    return Launcher(supervisor, store, queue_config=config)


def _entry(store: QueueStore, workspace: Path) -> Any:
    return store.create(_base_entry(trio="kimi", workspace=str(workspace)))


def _lease_path(root: Path) -> Path:
    return root / "queue" / ".lease"


def _foreign_lease(root: Path, *, age: float = 0.0) -> dict[str, Any]:
    """Write a lease held by another process ``age`` seconds in the past."""

    record = {"pid": _OTHER_PID, "host": _OTHER_HOST, "ts": time.time() - age}
    path = _lease_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


def _service_types(root: Path) -> list[str]:
    path = root / "queue" / "service_events.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line).get("type")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ----------------------------------------------------------------------
# Lease record mechanics
# ----------------------------------------------------------------------


def test_lease_taken_on_first_pass(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=default_queue_config())

    asyncio.run(launcher.tick())

    lease = read_lease(root)
    assert lease is not None
    assert int(lease["pid"]) == os.getpid()
    assert lease["host"] == socket.gethostname()
    assert isinstance(lease["ts"], float)
    assert (_lease_path(root).stat().st_mode & 0o777) == 0o600


def test_lease_is_json_with_contract_fields(tmp_path: Path) -> None:
    root, store, _supervisor = _fixture(tmp_path)
    asyncio.run(_launcher(store, FakeSupervisor(root)).tick())

    raw = _lease_path(root).read_text(encoding="utf-8")
    lease = json.loads(raw)
    assert set(lease) == {"pid", "host", "ts"}


def test_lease_refreshed_each_pass(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    launcher = _launcher(store, supervisor)
    _entry(store, tmp_path / "ws-a")

    asyncio.run(launcher.tick())
    first = read_lease(root)
    assert first is not None

    time.sleep(_TICK_SLEEP)
    asyncio.run(launcher.tick())
    second = read_lease(root)

    assert second is not None
    assert second["ts"] > first["ts"], "each pass must refresh the lease ts"
    # Still held by this process.
    assert int(second["pid"]) == os.getpid()


# ----------------------------------------------------------------------
# Second instance: log and idle
# ----------------------------------------------------------------------


def test_second_instance_with_live_lease_idles_and_launches_nothing(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    _foreign_lease(root, age=0.0)  # live: refreshed this instant
    launcher = _launcher(store, supervisor)

    entry = _entry(store, tmp_path / "ws-a")
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert updated.run_id is None
    assert supervisor.created == []
    # The pass never even listed the queue into action: no skip recorded.
    assert updated.skip_reasons == []
    # The holder's lease is untouched.
    assert read_lease(root)["pid"] == _OTHER_PID


def test_second_instance_logs_refusal(tmp_path: Path, caplog: Any) -> None:
    import logging

    root, store, supervisor = _fixture(tmp_path)
    _foreign_lease(root, age=0.0)
    launcher = _launcher(store, supervisor)

    with caplog.at_level(logging.WARNING, logger="lh_harness.launcher"):
        asyncio.run(launcher.tick())

    assert any("lease" in record.message for record in caplog.records)


def test_refused_instance_does_not_update_launched_entries(tmp_path: Path) -> None:
    """Idling means idling: a refused launcher performs no queue work at all."""

    root, store, supervisor = _fixture(tmp_path)
    _foreign_lease(root, age=0.0)
    launched = store.create(_base_entry(trio="kimi", workspace=str(tmp_path / "ws-a")))
    store.mark_launched(launched.queue_id, "run-1")
    # A run that has gone terminal would normally be reconciled this pass.
    supervisor._statuses["run-1"] = {"status": "completed", "run_id": "run-1"}

    launcher = _launcher(store, supervisor)
    asyncio.run(launcher.tick())

    updated = store.get(launched.queue_id)
    assert updated is not None
    assert updated.status == "launched"


# ----------------------------------------------------------------------
# Stale lease: reclaimed after 3 missed intervals
# ----------------------------------------------------------------------


def test_stale_lease_reclaimed(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    stale_after = 3 * 1.0
    # 4 missed intervals is past the stale horizon.
    _foreign_lease(root, age=stale_after + 1.0)
    launcher = _launcher(store, supervisor)

    entry = _entry(store, tmp_path / "ws-a")
    asyncio.run(launcher.tick())

    lease = read_lease(root)
    assert lease is not None
    assert int(lease["pid"]) == os.getpid()  # reclaimed by this launcher
    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "launched"
    assert supervisor.created, "the pass must run after a stale reclaim"


def test_lease_two_missed_intervals_is_still_live(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    stale_after = 3 * 1.0
    # Only 2 missed intervals: inside the stale horizon, so still held.
    _foreign_lease(root, age=2 * 1.0)
    launcher = _launcher(store, supervisor)

    entry = _entry(store, tmp_path / "ws-a")
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert supervisor.created == []
    assert read_lease(root)["pid"] == _OTHER_PID
    assert stale_after > 0  # sanity: the horizon was configured


def test_acquire_lease_units(tmp_path: Path) -> None:
    """acquire_lease: mint, refuse for a foreign holder, reclaim when stale."""

    root = tmp_path / "runs"
    root.mkdir()

    # Mint with O_EXCL.
    ours = acquire_lease(root, interval_seconds=1.0, pid=1, host="a", now=1000.0)
    assert ours == {"pid": 1, "host": "a", "ts": 1000.0}
    # Live foreign lease refuses a different holder.
    assert acquire_lease(root, interval_seconds=1.0, pid=2, host="b", now=1001.0) is None
    # The owner refreshes.
    refreshed = acquire_lease(root, interval_seconds=1.0, pid=1, host="a", now=1002.0)
    assert refreshed == {"pid": 1, "host": "a", "ts": 1002.0}
    # 3 missed intervals later it is stale and reclaimable.
    assert acquire_lease(root, interval_seconds=1.0, pid=2, host="b", now=1005.0 + 0.1) == {
        "pid": 2,
        "host": "b",
        "ts": 1005.1,
    }
    # Two simultaneous reclaimers: the O_EXCL re-create serializes them.
    stale = {"pid": 3, "host": "c", "ts": 900.0}
    _lease_path(root).write_text(json.dumps(stale), encoding="utf-8")
    assert acquire_lease(root, interval_seconds=1.0, pid=4, host="d", now=2000.0) is not None
    assert acquire_lease(root, interval_seconds=1.0, pid=5, host="e", now=2000.0) is None


def test_observe_mode_still_takes_the_lease(tmp_path: Path) -> None:
    """Observe mode runs real passes, so it holds the lease like any launcher."""

    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["observe"] = True
    launcher = Launcher(supervisor, store, queue_config=config)

    entry = _entry(store, tmp_path / "ws-a")
    asyncio.run(launcher.tick())

    lease = read_lease(root)
    assert lease is not None
    assert int(lease["pid"]) == os.getpid()
    # And the observe behaviour is unchanged: nothing launched, entry pending.
    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert supervisor.created == []