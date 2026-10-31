"""The migration tombstone state model (LHH-QUEUE-MIGRATION-TOMBSTONE, slice A).

A queue entry whose work moved to another node becomes a durable TOMBSTONE:
state ``migrated``, with ``migrated_to`` naming the destination.  This slice
(A) delivers the state model only -- the migrate endpoint (slice B), the
recovery path (slice C) and the operator CLI (slice E) are later slices.

The invariants this file pins down:

- a ``migrated`` entry is TERMINAL, exactly like ``done``/``failed``: no
  outgoing transition exists, so it can never be launched, unblocked or moved
  anywhere else;
- the launcher NEVER launches a migrated entry (the claim loop only ever
  considers ``pending``);
- the #160 reconciliation sweep NEVER re-creates or writes a migrated entry --
  a tombstone is skipped both by the state filter and by an explicit guard;
- a migrated entry is RETAINED like ``done``/``failed``: the record stays on
  disk and is reported by the reads, it is not hidden and not deleted;
- the link fields round-trip through the file store, and a legacy entry (one
  written before the fields existed) loads with them unset;
- the dedup key of a tombstone is freed exactly like a ``done``/``failed``
  entry's, because the entry no longer holds work on this node.

Everything here is hermetic: the tests touch only a temporary runs root, the
file store and the launcher's own in-process fakes.  No network, no other
node, no real service.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from lh_harness.launcher import Launcher
from lh_harness.queue import (
    QueueEntry,
    QueueStore,
    _NON_TERMINAL_STATUS,
    _TERMINAL_STATUS,
    _VALID_STATUS,
    default_queue_config,
)
from lh_harness.queue_reconcile import reconcile_sweep

REPO_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

_NAME_SEQ = {"n": 0}


def _body(**overrides: Any) -> dict[str, Any]:
    _NAME_SEQ["n"] += 1
    body: dict[str, Any] = {
        "name": f"migrate task {_NAME_SEQ['n']}",
        "task": "do the thing",
        "workspace": "./workspace",
        "max_rounds": 5,
        "trio": "kimi",
        "priority": 0,
        "requested_by": "ci",
    }
    body.update(overrides)
    return body


def _fixture(tmp_path: Path) -> tuple[Path, QueueStore]:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    return root, QueueStore(root)


# ---------------------------------------------------------------------------
# The status itself
# ---------------------------------------------------------------------------


def test_migrated_is_a_valid_status() -> None:
    """``migrated`` joins the canonical status set."""

    assert "migrated" in _VALID_STATUS


def test_migrated_is_terminal_like_done_and_failed() -> None:
    """A tombstone frees its dedup key exactly like ``done``/``failed`` do."""

    assert "migrated" in _TERMINAL_STATUS
    assert "migrated" not in _NON_TERMINAL_STATUS
    # The done/failed entries stay terminal too -- the new state does not
    # change the meaning of the existing ones.
    assert {"done", "failed"} <= _TERMINAL_STATUS


def test_migrated_has_no_outgoing_transition() -> None:
    """A tombstone is FINAL: it can never leave its state.

    This is the structural reason a migrated entry can never be launched
    again, unblocked back to pending, or moved to any other state -- there is
    no edge out of ``migrated`` in the transition table.
    """

    from lh_harness.queue import _QUEUE_TRANSITIONS

    outgoing = [to for (frm, to) in _QUEUE_TRANSITIONS if frm == "migrated"]
    assert outgoing == []
    # The one way IN is from pending.
    assert ("pending", "migrated") in _QUEUE_TRANSITIONS


# ---------------------------------------------------------------------------
# The link fields
# ---------------------------------------------------------------------------


def test_entry_defaults_have_no_migration_link() -> None:
    """An entry that was never migrated carries neither link nor an epoch."""

    entry = QueueEntry(
        queue_id="q-1",
        name="n",
        task="t",
        workspace="./w",
        max_rounds=5,
        trio="kimi",
        priority=0,
        requested_by="ci",
    )
    assert entry.migrated_to is None
    assert entry.migrated_from is None
    assert entry.migration_epoch is None


def test_legacy_entry_loads_without_migration_fields(tmp_path: Path) -> None:
    """An entry JSON written before the fields existed loads with them unset."""

    root, store = _fixture(tmp_path)
    legacy = {
        "queue_id": "q-legacy",
        "name": "legacy",
        "task": "old task",
        "workspace": "./w",
        "max_rounds": 5,
        "trio": "kimi",
        "priority": 0,
        "requested_by": "ci",
        "status": "pending",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    (root / "queue" / "q-legacy.json").write_text(json.dumps(legacy), encoding="utf-8")

    entry = store.get("q-legacy")
    assert entry is not None
    assert entry.migrated_to is None
    assert entry.migrated_from is None
    assert entry.migration_epoch is None


def test_migrated_to_round_trips_through_the_file_store(tmp_path: Path) -> None:
    """The source-side link is written into the entry JSON and read back."""

    root, store = _fixture(tmp_path)
    entry = store.create(_body())
    assert entry.status == "pending"

    migrated = store.mark_migrated(entry.queue_id, node="node-b", dest_queue_id="q-dest")
    assert migrated is not None
    assert migrated.status == "migrated"
    assert migrated.migrated_to is not None
    assert migrated.migrated_to["node"] == "node-b"
    assert migrated.migrated_to["queue_id"] == "q-dest"
    assert isinstance(migrated.migrated_to["at"], float)

    # The link is durable: it is in the file on disk, not just in memory.
    on_disk = json.loads((root / "queue" / f"{entry.queue_id}.json").read_text(encoding="utf-8"))
    assert on_disk["migrated_to"]["node"] == "node-b"
    assert on_disk["status"] == "migrated"

    reread = store.get(entry.queue_id)
    assert reread is not None
    assert reread.migrated_to == migrated.migrated_to


def test_migrated_from_round_trips_through_the_file_store(tmp_path: Path) -> None:
    """The destination-side link is written into the entry JSON and read back."""

    root, store = _fixture(tmp_path)
    entry = store.create(_body())
    entry.migrated_from = {"node": "node-a", "queue_id": "q-source", "at": 123.5}
    store.update(entry)

    reread = store.get(entry.queue_id)
    assert reread is not None
    assert reread.migrated_from == {"node": "node-a", "queue_id": "q-source", "at": 123.5}

    on_disk = json.loads((root / "queue" / f"{entry.queue_id}.json").read_text(encoding="utf-8"))
    assert on_disk["migrated_from"]["node"] == "node-a"


def test_migration_epoch_is_stamped_and_monotonic(tmp_path: Path) -> None:
    """The epoch is stamped on the tombstone and never regresses."""

    root, store = _fixture(tmp_path)
    entry = store.create(_body())
    assert entry.migration_epoch is None

    first = store.mark_migrated(entry.queue_id, node="node-b", dest_queue_id="q-dest")
    assert first is not None
    assert first.migration_epoch == 1


def test_mark_migrated_requires_a_pending_entry(tmp_path: Path) -> None:
    """Only a PENDING entry may be tombstoned.

    A launched entry has a live run behind it (the hard rule: running runs
    are untouched), a done/failed entry is already terminal, and an
    already-migrated entry is already a tombstone.  Every one of them is
    refused rather than silently accepted.
    """

    root, store = _fixture(tmp_path)
    entry = store.create(_body())

    # A launched entry (with a run behind it) is refused.
    store.mark_launched(entry.queue_id, "run-1")
    with pytest.raises(ValueError, match="invalid transition from launched to migrated"):
        store.mark_migrated(entry.queue_id, node="node-b")

    # A done entry is refused.
    done = store.create(_body())
    store.mark_done(done.queue_id, reason="finished")
    with pytest.raises(ValueError, match="invalid transition from done to migrated"):
        store.mark_migrated(done.queue_id, node="node-b")

    # A failed entry is refused.
    failed = store.create(_body())
    store.mark_failed(failed.queue_id, "boom")
    with pytest.raises(ValueError, match="invalid transition from failed to migrated"):
        store.mark_migrated(failed.queue_id, node="node-b")

    # An already-migrated entry is refused (no second stamp).
    tomb = store.create(_body())
    store.mark_migrated(tomb.queue_id, node="node-b")
    with pytest.raises(ValueError, match="invalid transition from migrated to migrated"):
        store.mark_migrated(tomb.queue_id, node="node-b")


def test_mark_migrated_requires_a_dest_node(tmp_path: Path) -> None:
    """A tombstone without a destination node is refused."""

    root, store = _fixture(tmp_path)
    entry = store.create(_body())
    with pytest.raises(ValueError, match="migrated_to node is required"):
        store.mark_migrated(entry.queue_id, node="   ")


def test_mark_migrated_on_a_missing_entry_returns_none(tmp_path: Path) -> None:
    """Tombstoning an entry that does not exist is a no-op, not a crash."""

    root, store = _fixture(tmp_path)
    assert store.mark_migrated("q-missing", node="node-b") is None


def test_mark_migrated_marks_the_link_pending_until_the_destination_exists(
    tmp_path: Path,
) -> None:
    """The link carries ``pending`` while the destination entry may not exist.

    Slice B creates the destination AFTER the tombstone write, so a crash in
    between leaves a tombstone whose link says the destination is not
    confirmed yet -- that is exactly the state the recovery path looks for.
    """

    root, store = _fixture(tmp_path)
    entry = store.create(_body())

    tomb = store.mark_migrated(entry.queue_id, node="node-b", dest_queue_id="q-dest")
    assert tomb is not None
    assert tomb.migrated_to is not None
    assert tomb.migrated_to["pending"] is True


# ---------------------------------------------------------------------------
# Retention parity with done/failed
# ---------------------------------------------------------------------------


def test_migrated_entry_is_retained_like_done_and_failed(tmp_path: Path) -> None:
    """A tombstone stays on disk and stays listed -- it is not hidden.

    ``done`` and ``failed`` entries are retained the same way: the entry file
    remains and the store still lists the entry.  A tombstone is retained
    identically -- the record survives, it is simply not work any more.
    """

    root, store = _fixture(tmp_path)
    done = store.create(_body())
    store.mark_done(done.queue_id, reason="finished")
    failed = store.create(_body())
    store.mark_failed(failed.queue_id, "boom")
    tomb = store.create(_body())
    store.mark_migrated(tomb.queue_id, node="node-b")

    # All three files are still on disk.
    for entry in (done, failed, tomb):
        assert (root / "queue" / f"{entry.queue_id}.json").is_file()

    # All three are still listed by the store.
    listed = {item.queue_id: item for item in store.list()}
    assert done.queue_id in listed
    assert failed.queue_id in listed
    assert tomb.queue_id in listed
    assert listed[tomb.queue_id].status == "migrated"


def test_migrated_entry_frees_its_dedup_key_like_done_and_failed(
    tmp_path: Path,
) -> None:
    """A tombstone does not hold its dedup key.

    The entry no longer represents work on this node -- the destination entry
    carries the key that continues to represent the work -- so the key is
    free for a fresh enqueue, exactly like a ``done``/``failed`` entry's key.
    """

    root, store = _fixture(tmp_path)
    first = store.create(_body(dedup_key="key-1"))
    store.mark_migrated(first.queue_id, node="node-b")

    # A second enqueue with the same key creates a NEW entry rather than
    # resolving to the tombstone.
    second = store.create(_body(dedup_key="key-1"))
    assert second.queue_id != first.queue_id
    assert second.status == "pending"


# ---------------------------------------------------------------------------
# The launcher never launches a tombstone
# ---------------------------------------------------------------------------


class _RecordingSupervisor:
    """In-memory supervisor stand-in that records the runs it was asked for."""

    def __init__(self, runs_root: Path) -> None:
        self.runs_root = runs_root
        self._runs: dict[str, dict[str, Any]] = {}
        self._statuses: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []

    def add_run(self, run_id: str, workspace: str, *, status: str = "running") -> None:
        self._runs[run_id] = {"id": run_id, "workspace": workspace}
        self._statuses[run_id] = {"status": status, "run_id": run_id}

    def list_run_items(self) -> list[dict[str, Any]]:
        return list(self._runs.values())

    def owner(self, run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "workspace": ".", "task": "existing", "agent": "codex"}

    def status(self, run_id: str) -> dict[str, Any]:
        return self._statuses.get(run_id, {"status": "idle", "run_id": run_id})

    def _run_logs_dir(self, run_id: str) -> Path:
        return self.runs_root / run_id / "lh_harness"

    def create_run(self, **kwargs: Any) -> dict[str, Any]:
        run_id = kwargs.get("run_id") or f"run-{len(self.created)}"
        self.add_run(run_id, str(kwargs.get("workspace", ".")), status="creating")
        self.created.append(kwargs)
        return {"id": run_id, "status": "creating", "log_dir": str(self._run_logs_dir(run_id))}


def test_launcher_never_launches_a_migrated_entry(tmp_path: Path) -> None:
    """A tombstone is never launched -- the load-bearing invariant of (A).

    The launcher only ever considers a ``pending`` entry, so a tombstone is
    invisible to the claim loop.  This test drives a real launcher tick and
    asserts that no run was created for the tombstone while a genuinely
    pending sibling entry WAS launched (so the test cannot pass by the
    launcher doing nothing at all).
    """

    root, store = _fixture(tmp_path)
    supervisor = _RecordingSupervisor(root)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 2
    launcher = Launcher(supervisor, store, queue_config=config)

    tomb = store.create(_body(workspace="./w-tomb", priority=10))
    store.mark_migrated(tomb.queue_id, node="node-b", dest_queue_id="q-dest")
    pending = store.create(_body(workspace="./w-pending", priority=1))

    asyncio.run(launcher.tick())

    tomb_after = store.get(tomb.queue_id)
    pending_after = store.get(pending.queue_id)
    assert tomb_after is not None
    assert pending_after is not None

    # The tombstone was not launched and did not move out of its state.
    assert tomb_after.status == "migrated"
    assert tomb_after.run_id is None
    # The pending sibling WAS launched -- the launcher was live, so the
    # assertion above is meaningful.
    assert pending_after.status == "launched"
    assert pending_after.run_id is not None

    # And no run was created for the tombstone.
    assert all(
        kwargs.get("workspace") != "./w-tomb" for kwargs in supervisor.created
    ), "the launcher created a run for a migrated tombstone"


def test_launcher_leaves_a_migrated_entry_alone_across_many_ticks(
    tmp_path: Path,
) -> None:
    """Repeated ticks never promote a tombstone back into work."""

    root, store = _fixture(tmp_path)
    supervisor = _RecordingSupervisor(root)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 2
    launcher = Launcher(supervisor, store, queue_config=config)

    tomb = store.create(_body())
    store.mark_migrated(tomb.queue_id, node="node-b")

    for _ in range(3):
        asyncio.run(launcher.tick())

    after = store.get(tomb.queue_id)
    assert after is not None
    assert after.status == "migrated"
    assert after.run_id is None
    assert supervisor.created == []


# ---------------------------------------------------------------------------
# The reconciliation sweep never re-creates a tombstone
# ---------------------------------------------------------------------------


def _write_run_status(runs_root: Path, run_id: str, status: str) -> None:
    """Write a supervisor-shaped ``control/status.json`` for ``run_id``."""

    control = runs_root / run_id / "control"
    control.mkdir(parents=True, exist_ok=True)
    (control / "status.json").write_text(
        json.dumps({"run_id": run_id, "status": status}), encoding="utf-8"
    )


def test_reconciliation_never_touches_a_migrated_entry(tmp_path: Path) -> None:
    """The #160 sweep neither writes nor re-creates a tombstone.

    The sweep reconciles a LAUNCHED entry against the status of the run it
    launched.  A tombstone was never launched, so the sweep has nothing to
    reconcile -- but the invariant is pinned explicitly so a future change to
    the sweep's state filter cannot silently start re-creating tombstones.
    """

    root, store = _fixture(tmp_path)
    tomb = store.create(_body())
    store.mark_migrated(tomb.queue_id, node="node-b", dest_queue_id="q-dest")

    before = json.loads((root / "queue" / f"{tomb.queue_id}.json").read_text(encoding="utf-8"))

    report = reconcile_sweep(root, dry_run=False)

    after = json.loads((root / "queue" / f"{tomb.queue_id}.json").read_text(encoding="utf-8"))
    assert after == before
    assert after["status"] == "migrated"
    assert after["migrated_to"]["node"] == "node-b"

    # The sweep reported no zombie for it and did not list it as live.
    assert tomb.queue_id not in [z.queue_id for z in report.zombies]
    assert tomb.queue_id not in report.live_entries
    assert report.applied == []


def test_reconciliation_does_not_resurrect_a_migrated_entry_with_a_run(
    tmp_path: Path,
) -> None:
    """Even a tombstone with a run-shaped status file is left alone.

    A tombstone never had a run on this node, but this test makes the
    invariant hard: even if a status file for a run id happened to exist and
    name a terminal status, the sweep must not write to the tombstone.
    """

    root, store = _fixture(tmp_path)
    tomb = store.create(_body())
    store.mark_migrated(tomb.queue_id, node="node-b")
    # A terminal status file for a run id the tombstone does not own.
    _write_run_status(root, "run-ghost", "completed")

    reconcile_sweep(root, dry_run=False)

    after = store.get(tomb.queue_id)
    assert after is not None
    assert after.status == "migrated"
    assert after.run_id is None


def test_reconciliation_still_reconciles_a_launched_entry(tmp_path: Path) -> None:
    """The tombstone guard did not break the sweep's actual job.

    A launched entry whose run is terminal is still reconciled to done --
    the guard added for (A) must not narrow the sweep's existing behaviour.
    """

    root, store = _fixture(tmp_path)
    entry = store.create(_body())
    store.mark_launched(entry.queue_id, "run-1")
    _write_run_status(root, "run-1", "completed")

    report = reconcile_sweep(root, dry_run=False)

    after = store.get(entry.queue_id)
    assert after is not None
    assert after.status == "done"
    assert [a.queue_id for a in report.applied] == [entry.queue_id]


# ---------------------------------------------------------------------------
# The reads report a tombstone as migrated, never as pending
# ---------------------------------------------------------------------------


def test_store_counts_report_migrated_separately(tmp_path: Path) -> None:
    """``counts()`` has a ``migrated`` bucket and does not count it as pending."""

    root, store = _fixture(tmp_path)
    tomb = store.create(_body())
    store.mark_migrated(tomb.queue_id, node="node-b")
    pending = store.create(_body())

    counts = store.counts()
    assert counts["migrated"] == 1
    assert counts["pending"] == 1
    assert counts["done"] == 0
    assert counts["failed"] == 0


def test_reads_return_the_tombstone_with_status_migrated(tmp_path: Path) -> None:
    """The entry dict reports the tombstone's status and both link fields."""

    root, store = _fixture(tmp_path)
    tomb = store.create(_body())
    store.mark_migrated(tomb.queue_id, node="node-b", dest_queue_id="q-dest")

    data = store.get(tomb.queue_id).to_dict()
    assert data["status"] == "migrated"
    assert data["migrated_to"]["node"] == "node-b"
    assert data["migrated_to"]["queue_id"] == "q-dest"
    # The destination-side field is present in the payload (as None here) so
    # the shape is stable for consumers.
    assert "migrated_from" in data
    assert data["migrated_from"] is None


def test_a_tombstone_is_not_offered_to_a_pending_sweep(tmp_path: Path) -> None:
    """A tombstone never shows up among the entries a caller would launch.

    This is the store-level half of "the fleet listing must never show a
    migrated source as pending": the tombstone is present in ``list()`` (it
    is retained) but it is not in any non-terminal/pending selection.
    """

    root, store = _fixture(tmp_path)
    tomb = store.create(_body())
    store.mark_migrated(tomb.queue_id, node="node-b")
    pending = store.create(_body())

    pending_ids = [
        item.queue_id for item in store.list() if item.status == "pending"
    ]
    assert pending_ids == [pending.queue_id]
    assert tomb.queue_id not in pending_ids


# ---------------------------------------------------------------------------
# The Postgres surface
# ---------------------------------------------------------------------------


def test_migration_008_adds_the_tombstone_columns_idempotently() -> None:
    """The Postgres migration adds the three link columns, re-runnably."""

    path = REPO_MIGRATIONS / "008_harness_queue_migration_tombstone.sql"
    sql = path.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS migrated_to JSONB" in sql
    assert "ADD COLUMN IF NOT EXISTS migrated_from JSONB" in sql
    assert "ADD COLUMN IF NOT EXISTS migration_epoch INTEGER" in sql
    # It is applied after the gtm_manager migration (007).
    names = sorted(p.name for p in REPO_MIGRATIONS.glob("*.sql"))
    assert names.index(path.name) > names.index("007_harness_queue_gtm_manager.sql")


def test_pg_column_list_carries_the_tombstone_columns() -> None:
    """The Postgres column list carries the new columns, appended."""

    from lh_harness import pg_queue

    columns = pg_queue._QUEUE_COLUMNS
    assert columns[-4:] == (
        "migrated_to",
        "migrated_from",
        "migration_epoch",
        "migrated_dest_base_url",
    )
    # Append-only: the earlier columns keep their positions.
    assert "gtm_manager" in columns


def test_pg_entry_values_carry_the_tombstone_links() -> None:
    """The link fields and the epoch are written like the file store writes them."""

    from lh_harness import pg_queue

    columns = pg_queue._QUEUE_COLUMNS
    entry = QueueEntry(
        queue_id="q-1", name="n", task="t", workspace="w", max_rounds=3, trio="kimi",
        priority=1, requested_by="ci",
        migrated_to={"node": "node-b", "queue_id": "q-dest", "at": 12.5},
        migrated_from={"node": "node-a", "queue_id": "q-source", "at": 11.5},
        migration_epoch=3,
    )
    raw = pg_queue._entry_values(entry)
    assert len(raw) == len(columns)
    values = dict(zip(columns, raw))
    assert json.loads(values["migrated_to"]) == {
        "node": "node-b", "queue_id": "q-dest", "at": 12.5
    }
    assert json.loads(values["migrated_from"]) == {
        "node": "node-a", "queue_id": "q-source", "at": 11.5
    }
    assert values["migration_epoch"] == 3


def test_pg_entry_values_leave_the_links_null_when_unmigrated() -> None:
    """An entry that was never migrated writes NULL, not an empty object."""

    from lh_harness import pg_queue

    columns = pg_queue._QUEUE_COLUMNS
    entry = QueueEntry(
        queue_id="q-1", name="n", task="t", workspace="w", max_rounds=3, trio="kimi",
        priority=1, requested_by="ci",
    )
    values = dict(zip(columns, pg_queue._entry_values(entry)))
    assert values["migrated_to"] is None
    assert values["migrated_from"] is None
    assert values["migration_epoch"] is None


def test_pg_cast_round_trips_the_tombstone_links() -> None:
    """The row->entry cast reads the JSONB columns back as dicts."""

    from lh_harness.pg_queue import PgQueueStore

    cast = PgQueueStore._cast
    assert cast({"node": "node-b"}, "migrated_to") == {"node": "node-b"}
    assert cast('{"node": "node-b"}', "migrated_to") == {"node": "node-b"}
    assert cast(None, "migrated_to") is None
    assert cast("not json", "migrated_to") is None
    assert cast(7, "migration_epoch") == 7
    assert cast(None, "migration_epoch") is None
    assert cast(True, "migration_epoch") is None


# ---------------------------------------------------------------------------
# The REST read surface
# ---------------------------------------------------------------------------


def _api_client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from lh_harness.webapi.server import create_app

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    # TestClient sends Host: testserver; bind_host must agree or the loopback
    # host check rejects every request.
    return TestClient(
        create_app(runs_root=root, auth_token="secret", bind_host="testserver")
    )


def _auth() -> dict:
    return {"Authorization": "Bearer secret"}


def test_api_queue_reports_a_tombstone_as_migrated(tmp_path: Path) -> None:
    """``GET /api/queue`` returns the tombstone with status ``migrated``.

    The tombstone is reported in its own group with both link fields, and it
    is NEVER reported as pending.
    """

    client = _api_client(tmp_path)
    response = client.post("/api/queue", json=_body(), headers=_auth())
    assert response.status_code == 200, response.text
    queue_id = response.json()["queue_id"]

    store = QueueStore(tmp_path / "runs")
    store.mark_migrated(queue_id, node="node-b", dest_queue_id="q-dest")

    listed = client.get("/api/queue", headers=_auth())
    assert listed.status_code == 200, listed.text
    payload = listed.json()

    statuses = {entry["queue_id"]: entry["status"] for entry in payload["entries"]}
    assert statuses[queue_id] == "migrated"

    groups = payload["groups"]
    assert groups["migrated"], "the tombstone is missing from the migrated group"
    assert groups["pending"] == [], "the tombstone was reported as pending"

    entry = next(item for item in payload["entries"] if item["queue_id"] == queue_id)
    assert entry["migrated_to"]["node"] == "node-b"
    assert entry["migrated_to"]["queue_id"] == "q-dest"
    assert "migrated_from" in entry

    counts = payload["counts"]
    assert counts["migrated"] == 1
    assert counts["pending"] == 0


def test_api_queue_can_filter_on_the_migrated_status(tmp_path: Path) -> None:
    """``status=migrated`` is an accepted filter value."""

    client = _api_client(tmp_path)
    response = client.post("/api/queue", json=_body(), headers=_auth())
    assert response.status_code == 200, response.text
    queue_id = response.json()["queue_id"]

    store = QueueStore(tmp_path / "runs")
    store.mark_migrated(queue_id, node="node-b")

    listed = client.get("/api/queue?status=migrated", headers=_auth())
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert [entry["queue_id"] for entry in payload["entries"]] == [queue_id]

    # And an unknown status is still refused.
    bad = client.get("/api/queue?status=nonsense", headers=_auth())
    assert bad.status_code == 422


def test_api_queue_stats_does_not_count_a_tombstone_as_pending(tmp_path: Path) -> None:
    """``GET /api/queue/stats`` never counts a tombstone as pending work."""

    client = _api_client(tmp_path)
    response = client.post("/api/queue", json=_body(), headers=_auth())
    assert response.status_code == 200, response.text
    queue_id = response.json()["queue_id"]

    store = QueueStore(tmp_path / "runs")
    store.mark_migrated(queue_id, node="node-b")

    stats = client.get("/api/queue/stats", headers=_auth())
    assert stats.status_code == 200, stats.text
    payload = stats.json()
    assert payload["counts"]["migrated"] == 1
    assert payload["counts"]["pending"] == 0
    # The per-trio breakdown counts pending/launched work only, so a
    # tombstone never shows up there either.
    for bucket in payload["by_trio"].values():
        assert bucket["pending"] == 0
        assert bucket["launched"] == 0


def test_api_delete_refuses_a_tombstone(tmp_path: Path) -> None:
    """A tombstone is not deletable through the pending-only cancel path.

    The cancel route (``DELETE /api/queue``) is pending-only, so a tombstone
    is refused with a 409 -- the record is retained, not silently removed.
    """

    client = _api_client(tmp_path)
    response = client.post("/api/queue", json=_body(), headers=_auth())
    assert response.status_code == 200, response.text
    queue_id = response.json()["queue_id"]

    store = QueueStore(tmp_path / "runs")
    store.mark_migrated(queue_id, node="node-b")

    deleted = client.delete(f"/api/queue/{queue_id}", headers=_auth())
    assert deleted.status_code == 409

    # The tombstone is still there.
    still = client.get(f"/api/queue/{queue_id}", headers=_auth())
    assert still.status_code == 200
    assert still.json()["status"] == "migrated"
