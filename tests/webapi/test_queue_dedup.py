"""Hermetic tests for the queue entry dedup / idempotency key.

These prove the single-orchestrator guarantee on enqueue: two callers asking
for the same work (same ``dedup_key``) resolve to a single non-terminal entry,
so the work can only be launched once. No live gateway or harness API calls are
made -- every test uses a tmp directory and the in-process TestClient.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.queue import QueueStore
from lh_harness.webapi.server import create_app


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    return root


def _payload(**overrides) -> dict:
    body = {
        "name": "dedup",
        "task": "do the thing",
        "workspace": "./workspace",
        "trio": "kimi",
        "priority": 0,
        "requested_by": "orchestrator",
    }
    body.update(overrides)
    return body


def test_same_dedup_key_yields_one_entry(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    first = store.create(_payload(dedup_key="build-feature-x"))
    second = store.create(_payload(dedup_key="build-feature-x"))
    # Idempotent enqueue: the second call returns the existing entry, not a new one.
    assert second.queue_id == first.queue_id
    assert len(store.list()) == 1
    assert store.counts()["pending"] == 1


def test_no_dedup_key_mints_unique_ids(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    a = store.create(_payload())
    b = store.create(_payload())
    assert a.queue_id != b.queue_id
    assert a.dedup_key is None
    assert b.dedup_key is None
    assert len(store.list()) == 2


def test_distinct_dedup_keys_create_distinct_entries(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    a = store.create(_payload(dedup_key="key-a"))
    b = store.create(_payload(dedup_key="key-b"))
    assert a.queue_id != b.queue_id
    assert {a.dedup_key, b.dedup_key} == {"key-a", "key-b"}
    assert len(store.list()) == 2


def test_dedup_key_reuse_after_done_creates_new_entry(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    first = store.create(_payload(dedup_key="retry-me"))
    store.mark_launched(first.queue_id, "run-1")
    store.mark_done(first.queue_id, reason="ok")
    # A terminal entry frees the key: reusing it creates a fresh entry (a retry).
    second = store.create(_payload(dedup_key="retry-me"))
    assert second.queue_id != first.queue_id
    assert second.status == "pending"
    assert len([e for e in store.list() if e.dedup_key == "retry-me"]) == 2


def test_dedup_key_reuse_after_failed_creates_new_entry(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    first = store.create(_payload(dedup_key="retry-me"))
    store.mark_failed(first.queue_id, reason="boom")
    second = store.create(_payload(dedup_key="retry-me"))
    assert second.queue_id != first.queue_id
    assert second.status == "pending"


def test_dedup_collapses_while_launched(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    first = store.create(_payload(dedup_key="in-flight"))
    store.mark_launched(first.queue_id, "run-1")
    # "launched" is non-terminal: a concurrent enqueue must not fork a second entry.
    second = store.create(_payload(dedup_key="in-flight"))
    assert second.queue_id == first.queue_id
    assert second.status == "launched"
    assert len(store.list()) == 1


def test_dedup_survives_restart_read(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    created = store.create(_payload(dedup_key="persist-key"))
    # A fresh store instance (simulating a service restart) reads the on-disk
    # entry and de-duplicates against it; the key also round-trips through JSON.
    reopened = QueueStore(root)
    again = reopened.create(_payload(dedup_key="persist-key"))
    assert again.queue_id == created.queue_id
    assert reopened.get(created.queue_id).dedup_key == "persist-key"


def test_empty_dedup_key_behaves_as_no_key(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    a = store.create(_payload(dedup_key="   "))
    b = store.create(_payload(dedup_key=""))
    assert a.queue_id != b.queue_id
    assert a.dedup_key is None
    assert b.dedup_key is None
    assert len(store.list()) == 2


def test_dedup_key_rejects_non_string(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    body = _payload()
    body["dedup_key"] = 123
    with pytest.raises(ValueError, match="dedup_key must be a string"):
        store.create(body)


def test_dedup_key_rejects_too_long(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    with pytest.raises(ValueError, match="dedup_key is too long"):
        store.create(_payload(dedup_key="x" * 10_000))


def test_dedup_prevents_double_launch(tmp_path: Path) -> None:
    """Two orchestrators enqueue the same work; the work launches exactly once."""
    root = _fixture(tmp_path)
    store = QueueStore(root)
    a = store.create(_payload(dedup_key="ship-it"))
    b = store.create(_payload(dedup_key="ship-it"))
    assert a.queue_id == b.queue_id
    # First launch claims the single entry.
    store.mark_launched(a.queue_id, "run-1")
    # A second launch attempt against the same entry is refused -- it is no
    # longer pending -- so the work cannot be launched twice.
    with pytest.raises(ValueError, match="entry is not pending"):
        store.mark_launched(a.queue_id, "run-2")
    assert store.get(a.queue_id).run_id == "run-1"


def test_api_dedup_key_passes_through(tmp_path: Path) -> None:
    """POST /api/queue forwards dedup_key to the store with no server.py change."""
    root = _fixture(tmp_path)
    app = create_app(runs_root=root)
    client = TestClient(app)
    body = _payload(dedup_key="api-key")
    first = client.post("/api/queue", json=body).json()
    second = client.post("/api/queue", json=body).json()
    assert first["queue_id"] == second["queue_id"]
    assert client.get("/api/queue").json()["counts"]["pending"] == 1
