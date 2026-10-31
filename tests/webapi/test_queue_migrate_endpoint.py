"""The migrate endpoint and the partial-pair recovery (LHH-QUEUE-MIGRATION-TOMBSTONE, slices B and C).

Slice (A) pinned the tombstone STATE model down.  These two slices move the
work:

* (B) ``POST /api/queue/{queue_id}/migrate`` -- in ONE source-side transaction
  the source entry is tombstoned (status ``migrated``), and ONLY THEN is the
  destination entry created through the destination's own ``POST /api/queue``.
* (C) the recovery path -- on web start and in the #160 reconciliation sweep,
  a source tombstone whose destination create never landed is repaired.

The invariants this file pins down:

- **HAPPY PATH**: the source is tombstoned, the destination entry exists with
  the exact field mapping, ``dedup_key = <source queue_id>:<dest node>``,
  ``requested_by`` carries the source id, and the destination entry links back
  through ``migrated_from``.
- **DESTINATION DOWN, THEN RETRY**: the destination being unreachable leaves
  the source TOMBSTONED with ``migrated_to.pending = true``; the source is
  NOT resurrected (that would reopen the duplicate-writer window); a retry of
  the same call completes the migration.
- **CRASH BETWEEN THE TOMBSTONE AND THE DESTINATION CREATE**: the same
  partial pair, reached by simulating the crash rather than by a network
  failure.  The recovery path repairs it.
- **DOUBLE CALL WITH THE SAME IDEMPOTENCY KEY**: the second call is a no-op
  that reports the same destination entry; there is never a second
  destination entry and never two launchable copies.
- **RECONCILIATION REPAIR**: the #160 sweep repairs a partial pair, logging
  one line per repair.
- **A MIGRATED SOURCE IS NEVER LAUNCHED**: no code path can launch a
  tombstone, and at no instant are there two launchable copies of the work.

Everything here is hermetic.  The destination is a LOCAL STUB (an injected
callable, or a loopback FastAPI app on 127.0.0.1); no test contacts a
non-local host, and no test needs a second machine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lh_harness.queue import QueueEntry, QueueStore
from lh_harness.queue_migrate import (
    MigrationError,
    destination_create_body,
    destination_dedup_key,
    migrate_pending_entry,
    repair_pending_migrations,
    workspace_basename,
)

_NAME_SEQ = {"n": 0}


def _body(**overrides: Any) -> dict[str, Any]:
    _NAME_SEQ["n"] += 1
    body: dict[str, Any] = {
        "name": f"migrate task {_NAME_SEQ['n']}",
        "task": "do the thing",
        "workspace": "/home/harness/work/LongHorizon-Harness-ws",
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


def _auth() -> dict:
    return {"Authorization": "Bearer secret"}


def _api_client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from lh_harness.webapi.server import create_app

    root = tmp_path / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return TestClient(
        create_app(runs_root=root, auth_token="secret", bind_host="testserver")
    )


class _FakeDestination:
    """A LOCAL stand-in for the destination node's queue API.

    It records every create it receives and answers a lookup by dedup_key
    from its own store.  It is the "other node" for every test here: no test
    opens a socket to anything but loopback.
    """

    def __init__(self) -> None:
        self.store = QueueStore(Path("/tmp"))  # replaced in __post_init__ style
        self.creates: list[dict[str, Any]] = []
        self.idempotency_keys: list[str | None] = []
        self.down = False
        self._root = Path("/tmp")

    def bind(self, root: Path) -> None:
        self._root = root
        self.store = QueueStore(root)

    # -- the two seams the migration uses -----------------------------------

    def create(self, dest_base_url: str, body: dict[str, Any], idempotency_key: str | None):
        if self.down:
            raise MigrationError("destination unreachable: connection refused")
        self.creates.append(dict(body))
        self.idempotency_keys.append(idempotency_key)
        entry = self.store.create(dict(body))
        return entry.queue_id, entry.to_dict()

    def lookup(self, dest_base_url: str, dedup_key: str) -> dict[str, Any] | None:
        if self.down:
            raise MigrationError("destination unreachable: connection refused")
        for entry in self.store.list():
            if entry.dedup_key == dedup_key:
                return entry.to_dict()
        return None


@pytest.fixture()
def destination(tmp_path: Path) -> _FakeDestination:
    dest = _FakeDestination()
    dest.bind(tmp_path / "dest-runs")
    (tmp_path / "dest-runs").mkdir(parents=True, exist_ok=True)
    return dest


def _pending_entry(store: QueueStore, **overrides: Any) -> QueueEntry:
    return store.create(_body(**overrides))


# ---------------------------------------------------------------------------
# (B) The endpoint -- happy path
# ---------------------------------------------------------------------------


def test_migrate_happy_path_tombstones_the_source_and_creates_the_destination(
    tmp_path: Path, destination: _FakeDestination
) -> None:
    """The source is tombstoned FIRST; the destination entry is created after."""

    root, store = _fixture(tmp_path)
    source = _pending_entry(store)

    result = migrate_pending_entry(
        store,
        source.queue_id,
        dest_node="node-b",
        dest_base_url="http://127.0.0.1:9",
        idempotency_key="idem-1",
        source_node="node-a",
        dest_create=destination.create,
    )

    assert result.migrated is True
    assert result.status == "migrated"
    assert result.dest_node == "node-b"

    # The SOURCE is a tombstone with the link and the epoch.
    tomb = store.get(source.queue_id)
    assert tomb is not None
    assert tomb.status == "migrated"
    assert tomb.migrated_to is not None
    assert tomb.migrated_to["node"] == "node-b"
    assert tomb.migrated_to["queue_id"] == result.dest_queue_id
    assert "pending" not in tomb.migrated_to, "the finalized link must not stay pending"
    assert tomb.migration_epoch == 1

    # The DESTINATION holds exactly one entry, with the exact field mapping.
    assert len(destination.creates) == 1
    sent = destination.creates[0]
    assert sent["task"] == source.task
    assert sent["name"] == source.name
    assert sent["trio"] == source.trio
    assert sent["priority"] == source.priority
    assert sent["max_rounds"] == source.max_rounds
    assert sent["workspace"] == workspace_basename(source.workspace)
    assert sent["workspace"] == "LongHorizon-Harness-ws"
    assert sent["dedup_key"] == f"{source.queue_id}:node-b"
    assert sent["requested_by"] == source.queue_id
    assert sent["migrated_from"]["node"] == "node-a"
    assert sent["migrated_from"]["queue_id"] == source.queue_id

    # The same idempotency key travelled with the request.
    assert destination.idempotency_keys == ["idem-1"]

    # The destination entry really exists, and it is PENDING there.
    dest_entries = destination.store.list()
    assert [entry.queue_id for entry in dest_entries] == [result.dest_queue_id]
    assert dest_entries[0].status == "pending"
    assert dest_entries[0].dedup_key == f"{source.queue_id}:node-b"


def test_migrate_over_http_moves_the_entry(tmp_path: Path, monkeypatch: Any) -> None:
    """The REST endpoint performs the migration and answers with the result."""

    client = _api_client(tmp_path)
    created = client.post("/api/queue", json=_body(), headers=_auth())
    assert created.status_code == 200, created.text
    queue_id = created.json()["queue_id"]

    store = QueueStore(tmp_path / "runs")
    sent: list[dict[str, Any]] = []

    def fake_create(dest_base_url: str, body: dict[str, Any], idempotency_key: str | None):
        sent.append(dict(body))
        return "q-dest-1", {"queue_id": "q-dest-1"}

    monkeypatch.setattr(
        "lh_harness.queue_migrate.default_dest_create_entry", fake_create
    )

    response = client.post(
        f"/api/queue/{queue_id}/migrate",
        json={
            "dest_node": "node-b",
            "dest_base_url": "http://127.0.0.1:9",
            "idempotency_key": "idem-http-1",
        },
        headers=_auth(),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["queue_id"] == queue_id
    assert payload["status"] == "migrated"
    assert payload["dest_queue_id"] == "q-dest-1"
    assert payload["dedup_key"] == f"{queue_id}:node-b"

    tomb = store.get(queue_id)
    assert tomb is not None and tomb.status == "migrated"
    assert tomb.migrated_to is not None
    assert tomb.migrated_to["node"] == "node-b"
    assert tomb.migrated_to["queue_id"] == "q-dest-1"


def test_migrate_refuses_a_running_entry(tmp_path: Path, destination: _FakeDestination) -> None:
    """A LAUNCHED entry is refused: running runs are untouched."""

    root, store = _fixture(tmp_path)
    entry = _pending_entry(store)
    store.mark_launched(entry.queue_id, run_id="run-1")

    with pytest.raises(MigrationError) as excinfo:
        migrate_pending_entry(
            store,
            entry.queue_id,
            dest_node="node-b",
            dest_base_url="http://127.0.0.1:9",
            idempotency_key="idem-1",
            dest_create=destination.create,
        )
    assert excinfo.value.status == 409
    assert "launched" in excinfo.value.detail

    # The entry is untouched: still launched, still holding its run.
    after = store.get(entry.queue_id)
    assert after is not None
    assert after.status == "launched"
    assert after.run_id == "run-1"
    assert destination.creates == [], "a running entry must not reach the destination"


def test_migrate_refuses_a_missing_entry(tmp_path: Path, destination: _FakeDestination) -> None:
    """An unknown queue id is a 404."""

    root, store = _fixture(tmp_path)
    with pytest.raises(MigrationError) as excinfo:
        migrate_pending_entry(
            store,
            "q-nope",
            dest_node="node-b",
            dest_base_url="http://127.0.0.1:9",
            idempotency_key="idem-1",
            dest_create=destination.create,
        )
    assert excinfo.value.status == 404


def test_migrate_over_http_requires_its_three_fields(tmp_path: Path) -> None:
    """The body must carry dest_node, dest_base_url and idempotency_key."""

    client = _api_client(tmp_path)
    created = client.post("/api/queue", json=_body(), headers=_auth())
    queue_id = created.json()["queue_id"]

    for missing in ("dest_node", "dest_base_url", "idempotency_key"):
        body = {
            "dest_node": "node-b",
            "dest_base_url": "http://127.0.0.1:9",
            "idempotency_key": "idem-1",
        }
        body.pop(missing)
        response = client.post(
            f"/api/queue/{queue_id}/migrate", json=body, headers=_auth()
        )
        assert response.status_code == 422, f"missing {missing} was accepted"


# ---------------------------------------------------------------------------
# (B) Destination down, then retry
# ---------------------------------------------------------------------------


def test_destination_down_leaves_the_source_tombstoned_and_pending(
    tmp_path: Path, destination: _FakeDestination
) -> None:
    """A failed destination create never resurrects the source entry."""

    root, store = _fixture(tmp_path)
    source = _pending_entry(store)
    destination.down = True

    with pytest.raises(MigrationError) as excinfo:
        migrate_pending_entry(
            store,
            source.queue_id,
            dest_node="node-b",
            dest_base_url="http://127.0.0.1:9",
            idempotency_key="idem-1",
            source_node="node-a",
            dest_create=destination.create,
        )
    assert excinfo.value.status == 502

    # The source is a TOMBSTONE -- not pending, so it can never be launched
    # here again.  The link says the destination create is outstanding.
    tomb = store.get(source.queue_id)
    assert tomb is not None
    assert tomb.status == "migrated"
    assert tomb.migrated_to is not None
    assert tomb.migrated_to["pending"] is True
    assert tomb.migrated_to["node"] == "node-b"
    assert tomb.migrated_to["queue_id"] is None
    assert tomb.migration_epoch == 1

    # The destination holds nothing: there is exactly one possible writer
    # (the destination, once it is reachable), and it holds no work yet.
    assert destination.store.list() == []


def test_retry_after_the_destination_comes_back_completes_the_migration(
    tmp_path: Path, destination: _FakeDestination
) -> None:
    """The SAME call retried after the destination recovers completes it."""

    root, store = _fixture(tmp_path)
    source = _pending_entry(store)
    destination.down = True

    with pytest.raises(MigrationError):
        migrate_pending_entry(
            store,
            source.queue_id,
            dest_node="node-b",
            dest_base_url="http://127.0.0.1:9",
            idempotency_key="idem-1",
            source_node="node-a",
            dest_create=destination.create,
        )

    destination.down = False
    result = migrate_pending_entry(
        store,
        source.queue_id,
        dest_node="node-b",
        dest_base_url="http://127.0.0.1:9",
        idempotency_key="idem-1",
        source_node="node-a",
        dest_create=destination.create,
    )

    assert result.migrated is True
    assert result.already_migrated is True
    assert result.dest_queue_id is not None

    tomb = store.get(source.queue_id)
    assert tomb is not None
    assert tomb.status == "migrated"
    assert tomb.migrated_to is not None
    assert "pending" not in tomb.migrated_to
    assert tomb.migrated_to["queue_id"] == result.dest_queue_id
    # The epoch did not restart: the retry is the SAME migration attempt.
    assert tomb.migration_epoch == 1

    # Exactly one destination entry.
    assert len(destination.store.list()) == 1


def test_retry_refuses_to_repoint_a_tombstone_at_another_node(
    tmp_path: Path, destination: _FakeDestination
) -> None:
    """A tombstone is never moved to a second destination."""

    root, store = _fixture(tmp_path)
    source = _pending_entry(store)
    destination.down = True

    with pytest.raises(MigrationError):
        migrate_pending_entry(
            store,
            source.queue_id,
            dest_node="node-b",
            dest_base_url="http://127.0.0.1:9",
            idempotency_key="idem-1",
            dest_create=destination.create,
        )

    with pytest.raises(MigrationError) as excinfo:
        migrate_pending_entry(
            store,
            source.queue_id,
            dest_node="node-c",
            dest_base_url="http://127.0.0.1:9",
            idempotency_key="idem-2",
            dest_create=destination.create,
        )
    assert excinfo.value.status == 409
    assert "node-b" in excinfo.value.detail


# ---------------------------------------------------------------------------
# (B) A double call with the same idempotency key
# ---------------------------------------------------------------------------


def test_double_call_with_the_same_idempotency_key_creates_one_destination_entry(
    tmp_path: Path, destination: _FakeDestination
) -> None:
    """The second call converges on the FIRST destination entry."""

    root, store = _fixture(tmp_path)
    source = _pending_entry(store)

    first = migrate_pending_entry(
        store,
        source.queue_id,
        dest_node="node-b",
        dest_base_url="http://127.0.0.1:9",
        idempotency_key="idem-1",
        source_node="node-a",
        dest_create=destination.create,
    )
    second = migrate_pending_entry(
        store,
        source.queue_id,
        dest_node="node-b",
        dest_base_url="http://127.0.0.1:9",
        idempotency_key="idem-1",
        source_node="node-a",
        dest_create=destination.create,
    )

    assert second.dest_queue_id == first.dest_queue_id
    assert second.already_migrated is True

    # The destination's own dedup_key semantics converge on ONE entry.
    dest_entries = destination.store.list()
    assert len(dest_entries) == 1
    assert dest_entries[0].queue_id == first.dest_queue_id

    # The source is still ONE tombstone with ONE epoch.
    tomb = store.get(source.queue_id)
    assert tomb is not None
    assert tomb.status == "migrated"
    assert tomb.migration_epoch == 1


def test_a_migration_never_leaves_two_launchable_copies(
    tmp_path: Path, destination: _FakeDestination
) -> None:
    """At no instant do both sides hold a launchable copy.

    The source is tombstoned BEFORE the destination entry is created, so the
    overlap window is closed on the source side first.
    """

    root, store = _fixture(tmp_path)
    source = _pending_entry(store)
    observed: list[tuple[str, int]] = []

    def watching_create(dest_base_url: str, body: dict[str, Any], idempotency_key: str | None):
        # The moment the destination create runs, look at both sides.
        src = store.get(source.queue_id)
        observed.append(
            (src.status if src else "?", len(destination.store.list()))
        )
        return destination.create(dest_base_url, body, idempotency_key)

    migrate_pending_entry(
        store,
        source.queue_id,
        dest_node="node-b",
        dest_base_url="http://127.0.0.1:9",
        idempotency_key="idem-1",
        source_node="node-a",
        dest_create=watching_create,
    )

    assert observed, "the destination create never ran"
    source_status, dest_count = observed[0]
    assert source_status == "migrated", "the source was still launchable during the create"
    assert dest_count == 0, "the destination already held a copy during the create"


# ---------------------------------------------------------------------------
# (C) Crash between the tombstone and the destination create
# ---------------------------------------------------------------------------


def test_crash_between_the_tombstone_and_the_destination_create_leaves_a_partial_pair(
    tmp_path: Path, destination: _FakeDestination
) -> None:
    """The simulated crash: the source is tombstoned, the destination never got it.

    This is the case the Codex review named -- the process dies after the
    source-side transaction and before the destination create.  The work
    exists on NEITHER side, and the source must not be resurrected.
    """

    root, store = _fixture(tmp_path)
    source = _pending_entry(store)

    # The crash: the tombstone is written, then the process dies.
    store.mark_migrated(
        source.queue_id,
        node="node-b",
        dest_queue_id=None,
        epoch=1,
        pending=True,
        dest_base_url="http://127.0.0.1:9",
    )

    tomb = store.get(source.queue_id)
    assert tomb is not None
    assert tomb.status == "migrated"
    assert tomb.migrated_to is not None and tomb.migrated_to["pending"] is True
    assert destination.store.list() == []


def test_the_recovery_recreates_the_missing_destination_entry(
    tmp_path: Path, destination: _FakeDestination
) -> None:
    """The recovery re-creates the destination entry from the stored task text."""

    root, store = _fixture(tmp_path)
    source = _pending_entry(store)
    original_task = source.task

    store.mark_migrated(
        source.queue_id,
        node="node-b",
        dest_queue_id=None,
        epoch=1,
        pending=True,
        dest_base_url="http://127.0.0.1:9",
    )

    repairs = repair_pending_migrations(
        store,
        dest_lookup=destination.lookup,
        dest_create=destination.create,
    )

    assert len(repairs) == 1, repairs
    assert source.queue_id in repairs[0]

    # The destination now holds the work, re-created from the stored text.
    dest_entries = destination.store.list()
    assert len(dest_entries) == 1
    assert dest_entries[0].task == original_task
    assert dest_entries[0].dedup_key == f"{source.queue_id}:node-b"

    # The link is finalized: the tombstone is no longer pending.
    tomb = store.get(source.queue_id)
    assert tomb is not None
    assert tomb.status == "migrated"
    assert tomb.migrated_to is not None
    assert "pending" not in tomb.migrated_to
    assert tomb.migrated_to["queue_id"] == dest_entries[0].queue_id


def test_the_recovery_finalizes_a_pair_whose_destination_already_holds_the_entry(
    tmp_path: Path, destination: _FakeDestination
) -> None:
    """The destination DID get the entry; the recovery only finalizes the link.

    This is the other half of the partial pair: the create landed but the
    finalize did not (the process died in between).  The destination already
    holds the work, so the recovery must NOT create a second one.
    """

    root, store = _fixture(tmp_path)
    source = _pending_entry(store)

    # The destination create landed...
    dest_entry = destination.store.create(
        _body(
            name=source.name,
            task=source.task,
            workspace=workspace_basename(source.workspace),
            trio=source.trio,
            priority=source.priority,
            max_rounds=source.max_rounds,
            dedup_key=f"{source.queue_id}:node-b",
            requested_by=source.queue_id,
        )
    )
    # ...but the source's finalize never ran.
    store.mark_migrated(
        source.queue_id,
        node="node-b",
        dest_queue_id=None,
        epoch=1,
        pending=True,
        dest_base_url="http://127.0.0.1:9",
    )

    repairs = repair_pending_migrations(
        store,
        dest_lookup=destination.lookup,
        dest_create=destination.create,
    )

    assert len(repairs) == 1, repairs
    assert "finalized" in repairs[0]

    # Exactly ONE destination entry: the create was not repeated.
    assert len(destination.store.list()) == 1
    assert destination.store.list()[0].queue_id == dest_entry.queue_id

    tomb = store.get(source.queue_id)
    assert tomb is not None
    assert tomb.migrated_to is not None
    assert "pending" not in tomb.migrated_to
    assert tomb.migrated_to["queue_id"] == dest_entry.queue_id


def test_the_recovery_leaves_the_pair_pending_when_the_destination_is_down(
    tmp_path: Path, destination: _FakeDestination
) -> None:
    """An unreachable destination leaves the tombstone pending -- it is safe.

    The source stays tombstoned, so there is still exactly one possible
    writer and no duplicate-writer window.
    """

    root, store = _fixture(tmp_path)
    source = _pending_entry(store)
    store.mark_migrated(
        source.queue_id,
        node="node-b",
        dest_queue_id=None,
        epoch=1,
        pending=True,
        dest_base_url="http://127.0.0.1:9",
    )
    destination.down = True

    repairs = repair_pending_migrations(
        store,
        dest_lookup=destination.lookup,
        dest_create=destination.create,
    )
    assert repairs == []

    tomb = store.get(source.queue_id)
    assert tomb is not None
    assert tomb.status == "migrated"
    assert tomb.migrated_to is not None
    assert tomb.migrated_to["pending"] is True


# ---------------------------------------------------------------------------
# (C) The #160 reconciliation sweep repairs a partial pair
# ---------------------------------------------------------------------------


def test_the_reconcile_sweep_repairs_a_partial_pair(
    tmp_path: Path, destination: _FakeDestination, caplog: Any
) -> None:
    """The #160 sweep repairs the partial pair and logs one line per repair."""

    from lh_harness.queue_reconcile import reconcile_sweep

    root, store = _fixture(tmp_path)
    source = _pending_entry(store)
    store.mark_migrated(
        source.queue_id,
        node="node-b",
        dest_queue_id=None,
        epoch=1,
        pending=True,
        dest_base_url="http://127.0.0.1:9",
    )

    # The sweep reaches the destination through the SAME default seam the
    # migration uses; point it at the local stub.
    import lh_harness.queue_migrate as queue_migrate

    original_lookup = queue_migrate.default_dest_get_entry
    original_create = queue_migrate.default_dest_create_entry
    queue_migrate.default_dest_get_entry = destination.lookup
    queue_migrate.default_dest_create_entry = destination.create
    try:
        with caplog.at_level("INFO"):
            report = reconcile_sweep(root, dry_run=False)
    finally:
        queue_migrate.default_dest_get_entry = original_lookup
        queue_migrate.default_dest_create_entry = original_create

    assert len(report.migration_repairs) == 1, report.migration_repairs
    assert source.queue_id in report.migration_repairs[0]

    # The destination holds the re-created work.
    dest_entries = destination.store.list()
    assert len(dest_entries) == 1
    assert dest_entries[0].task == source.task

    # The link is finalized.
    tomb = store.get(source.queue_id)
    assert tomb is not None
    assert tomb.migrated_to is not None
    assert "pending" not in tomb.migrated_to


def test_the_reconcile_sweep_dry_run_repairs_nothing(
    tmp_path: Path, destination: _FakeDestination
) -> None:
    """A dry run never writes: the partial pair stays pending."""

    from lh_harness.queue_reconcile import reconcile_sweep

    root, store = _fixture(tmp_path)
    source = _pending_entry(store)
    store.mark_migrated(
        source.queue_id,
        node="node-b",
        dest_queue_id=None,
        epoch=1,
        pending=True,
        dest_base_url="http://127.0.0.1:9",
    )

    report = reconcile_sweep(root, dry_run=True)
    assert report.migration_repairs == []
    assert destination.store.list() == []
    tomb = store.get(source.queue_id)
    assert tomb is not None and tomb.migrated_to is not None
    assert tomb.migrated_to["pending"] is True


# ---------------------------------------------------------------------------
# A migrated source is NEVER launched
# ---------------------------------------------------------------------------


def test_a_migrated_source_is_never_launched(tmp_path: Path) -> None:
    """The launcher's claim loop skips a tombstone on every tick.

    A genuinely pending SIBLING entry is launched by the same tick, so the
    assertion cannot pass by the launcher doing nothing at all.
    """

    import asyncio

    from lh_harness.launcher import Launcher
    from lh_harness.queue import default_queue_config

    root, store = _fixture(tmp_path)
    created: list[dict[str, Any]] = []

    class _RecordingSupervisor:
        """The same in-memory supervisor stand-in the slice-(A) tests use."""

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

    config = default_queue_config()
    config["capacity"]["kimi_max"] = 2
    supervisor = _RecordingSupervisor(root)
    launcher = Launcher(supervisor, store, queue_config=config)

    tomb = store.create(_body(workspace="./w-tomb", priority=10))
    store.mark_migrated(tomb.queue_id, node="node-b", dest_queue_id="q-dest")
    pending = store.create(_body(workspace="./w-pending", priority=1))

    for _ in range(3):
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
    # And no run was created for the tombstone.
    assert all(
        kwargs.get("workspace") != "./w-tomb" for kwargs in supervisor.created
    ), "the launcher created a run for a migrated tombstone"


def test_the_tombstone_is_never_offered_as_pending_work(tmp_path: Path) -> None:
    """A tombstone is not in the non-terminal set, so it is not live work."""

    from lh_harness.queue import _NON_TERMINAL_STATUS

    root, store = _fixture(tmp_path)
    entry = _pending_entry(store)
    store.mark_migrated(entry.queue_id, node="node-b", dest_queue_id="q-dest")

    after = store.get(entry.queue_id)
    assert after is not None
    assert after.status not in _NON_TERMINAL_STATUS
    assert after.status == "migrated"


# ---------------------------------------------------------------------------
# The field mapping itself
# ---------------------------------------------------------------------------


def test_the_destination_dedup_key_format_is_fixed() -> None:
    """The dedup key is exactly ``<source queue_id>:<dest node>``."""

    assert destination_dedup_key("q-abc", "node-b") == "q-abc:node-b"


def test_the_workspace_travels_as_a_basename() -> None:
    """Only the basename travels: the destination has its own root."""

    assert workspace_basename("/a/b/c-ws") == "c-ws"
    assert workspace_basename("/a/b/c-ws/") == "c-ws"
    assert workspace_basename("c-ws") == "c-ws"


def test_the_destination_body_carries_the_source_id_in_requested_by(
    tmp_path: Path,
) -> None:
    """``requested_by`` carries the source id, and the link matches the key."""

    root, store = _fixture(tmp_path)
    entry = _pending_entry(store)
    body = destination_create_body(
        entry, dest_node="node-b", source_node="node-a"
    )
    assert body["requested_by"] == entry.queue_id
    assert body["dedup_key"] == f"{entry.queue_id}:node-b"
    assert body["migrated_from"]["queue_id"] == entry.queue_id
    assert body["migrated_from"]["node"] == "node-a"


def test_the_destination_refuses_a_link_that_contradicts_its_dedup_key(
    tmp_path: Path,
) -> None:
    """The destination validates ``migrated_from`` against the dedup key.

    A body cannot claim an origin that does not match its own idempotency
    key -- the link and the key must tell the same story.
    """

    root, store = _fixture(tmp_path)
    body = destination_create_body(_pending_entry(store), dest_node="node-b", source_node="node-a")
    body["migrated_from"]["queue_id"] = "q-somewhere-else"

    with pytest.raises(ValueError) as excinfo:
        store.create(body)
    assert "dedup_key" in str(excinfo.value)


def test_an_ordinary_enqueue_carries_no_migration_link(tmp_path: Path) -> None:
    """A plain enqueue has ``migrated_from = None``."""

    root, store = _fixture(tmp_path)
    entry = _pending_entry(store)
    assert entry.migrated_from is None
    assert entry.migrated_to is None
    assert entry.migration_epoch is None


def test_the_destination_entry_persists_its_link(tmp_path: Path) -> None:
    """The destination entry keeps ``migrated_from`` through the store."""

    root, store = _fixture(tmp_path)
    entry = _pending_entry(store)
    body = destination_create_body(entry, dest_node="node-b", source_node="node-a")
    created = store.create(body)
    reread = store.get(created.queue_id)
    assert reread is not None
    assert reread.migrated_from is not None
    assert reread.migrated_from["node"] == "node-a"
    assert reread.migrated_from["queue_id"] == entry.queue_id
    assert isinstance(reread.migrated_from["at"], float)
