"""Postgres-backed queue store tests.

Mirrors the hermetic ``QueueStore`` / API matrix (``tests/webapi/test_queue.py``,
``tests/webapi/test_queue_dedup.py``) against ``PgQueueStore``.  Every test runs
only when the Postgres backend is reachable via ``LH_HARNESS_DB_URL``; without
it the whole module is skipped so the no-DB hermetic suite stays green.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.queue import QueueStore, PgQueueStore, _select_queue_store


def _db_url() -> str | None:
    """Return the Postgres URL, or None when the backend is not configured.

    Reads the env name only -- never a secret in a file, fixture, or commit.
    """
    value = os.environ.get("LH_HARNESS_DB_URL")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require_backend(tmp_path: Path) -> PgQueueStore:
    url = _db_url()
    if not url:
        pytest.skip("LH_HARNESS_DB_URL not set; Postgres backend unavailable")
    store = PgQueueStore(url)
    try:
        store.counts()
    except Exception:
        pytest.skip("could not connect to Postgres backend")
    return store


def test_pg_store_creates_entry(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    entry = store.create(
        {
            "name": "test",
            "task": "do something",
            "workspace": "./workspace",
            "max_rounds": 5,
            "roles": "kimi",
            "priority": 10,
            "requested_by": "ci",
        }
    )
    assert entry.queue_id.startswith("q-")
    assert entry.status == "pending"
    assert entry.max_rounds == 5
    assert entry.priority == 10
    assert store.get(entry.queue_id) is not None
    counts = store.counts()
    assert counts["pending"] == 1


def test_pg_store_validates_required_fields(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    with pytest.raises(ValueError, match="task must be a string"):
        store.create({"name": "x", "workspace": "w", "trio": "kimi", "requested_by": "ci"})
    with pytest.raises(ValueError, match="name is required"):
        store.create({"task": "x", "workspace": "w", "trio": "kimi", "requested_by": "ci"})
    with pytest.raises(ValueError, match="workspace is required"):
        store.create({"name": "x", "task": "x", "trio": "kimi", "requested_by": "ci"})
    with pytest.raises(ValueError, match="requested_by is required"):
        store.create({"name": "x", "task": "x", "workspace": "w", "trio": "kimi"})


def test_pg_store_rejects_both_task_and_task_file(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    with pytest.raises(ValueError, match="task or task_file, not both"):
        store.create(
            {
                "name": "x",
                "task": "a",
                "task_file": "/tmp/task.md",
                "workspace": "w",
                "trio": "kimi",
                "requested_by": "ci",
            }
        )


def test_pg_store_lists_and_sorts(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    low = store.create(
        {"name": "low", "task": "t", "workspace": "w", "trio": "kimi", "priority": 1, "requested_by": "ci"}
    )
    high = store.create(
        {"name": "high", "task": "t", "workspace": "w", "trio": "kimi", "priority": 10, "requested_by": "ci"}
    )
    items = store.list()
    assert [item.queue_id for item in items] == [high.queue_id, low.queue_id]


def test_pg_store_priority_update(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    entry = store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 1, "requested_by": "ci"}
    )
    updated = store.set_priority(entry.queue_id, 42)
    assert updated is not None
    assert updated.priority == 42
    assert store.get(entry.queue_id).priority == 42


def test_pg_store_delete_pending(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    entry = store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 0, "requested_by": "ci"}
    )
    removed = store.delete(entry.queue_id)
    assert removed is not None
    assert store.get(entry.queue_id) is None
    assert store.list() == []


def test_pg_store_mark_launched_and_done(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    entry = store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 0, "requested_by": "ci"}
    )
    launched = store.mark_launched(entry.queue_id, "run-123")
    assert launched is not None
    assert launched.status == "launched"
    assert launched.run_id == "run-123"
    done = store.mark_done(entry.queue_id, reason="completed")
    assert done is not None
    assert done.status == "done"
    assert done.reason == "completed"


def test_pg_store_mark_failed(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    entry = store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 0, "requested_by": "ci"}
    )
    failed = store.mark_failed(entry.queue_id, reason="boom")
    assert failed is not None
    assert failed.status == "failed"
    assert failed.reason == "boom"


def test_pg_store_record_skip(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    entry = store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 0, "requested_by": "ci"}
    )
    skipped = store.record_skip(entry.queue_id, "no capacity")
    assert skipped is not None
    assert "no capacity" in skipped.skip_reasons
    # skip_reasons round-trips through JSON and reads back as a list.
    assert isinstance(store.get(entry.queue_id).skip_reasons, list)


def test_pg_store_counts(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 0, "requested_by": "ci"}
    )
    entry = store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 0, "requested_by": "ci"}
    )
    store.mark_launched(entry.queue_id, "run-1")
    counts = store.counts()
    assert counts["pending"] == 1
    assert counts["launched"] == 1
    assert set(counts) == {"pending", "launched", "done", "failed", "blocked"}


def test_pg_store_get_missing_returns_none(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    assert store.get("does-not-exist") is None


def test_pg_store_get_bad_status_ignored(tmp_path: Path) -> None:
    """A row whose status is not a valid queue status is not returned."""
    store = _require_backend(tmp_path)
    entry = store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 0, "requested_by": "ci"}
    )
    store.update(entry)
    # Mark the entry done, then simulate a row with an unknown status by writing
    # a value the store must reject on read.
    store.mark_done(entry.queue_id, reason="ok")
    with store._txn() as txn:  # noqa: SLF001 - test needs the live connection
        txn.execute("UPDATE harness.queue SET status = %s WHERE queue_id = %s", ("bogus", entry.queue_id))
        txn.commit()
    assert store.get(entry.queue_id) is None


def test_pg_dedup_same_key_yields_one_entry(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    first = store.create(
        {
            "name": "dedup",
            "task": "do the thing",
            "workspace": "./workspace",
            "trio": "kimi",
            "priority": 0,
            "requested_by": "orchestrator",
            "dedup_key": "build-feature-x",
        }
    )
    second = store.create(
        {
            "name": "dedup",
            "task": "do the thing",
            "workspace": "./workspace",
            "trio": "kimi",
            "priority": 0,
            "requested_by": "orchestrator",
            "dedup_key": "build-feature-x",
        }
    )
    assert second.queue_id == first.queue_id
    assert len(store.list()) == 1
    assert store.counts()["pending"] == 1


def test_pg_dedup_reuse_after_done_creates_new_entry(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    first = store.create(
        {
            "name": "dedup",
            "task": "do the thing",
            "workspace": "./workspace",
            "trio": "kimi",
            "priority": 0,
            "requested_by": "orchestrator",
            "dedup_key": "retry-me",
        }
    )
    store.mark_launched(first.queue_id, "run-1")
    store.mark_done(first.queue_id, reason="ok")
    second = store.create(
        {
            "name": "dedup",
            "task": "do the thing",
            "workspace": "./workspace",
            "trio": "kimi",
            "priority": 0,
            "requested_by": "orchestrator",
            "dedup_key": "retry-me",
        }
    )
    assert second.queue_id != first.queue_id
    assert second.status == "pending"


def test_pg_dedup_collapses_while_launched(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    first = store.create(
        {
            "name": "dedup",
            "task": "do the thing",
            "workspace": "./workspace",
            "trio": "kimi",
            "priority": 0,
            "requested_by": "orchestrator",
            "dedup_key": "in-flight",
        }
    )
    store.mark_launched(first.queue_id, "run-1")
    second = store.create(
        {
            "name": "dedup",
            "task": "do the thing",
            "workspace": "./workspace",
            "trio": "kimi",
            "priority": 0,
            "requested_by": "orchestrator",
            "dedup_key": "in-flight",
        }
    )
    assert second.queue_id == first.queue_id
    assert len(store.list()) == 1


def test_pg_dedup_rejects_non_string(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    body = {
        "name": "dedup",
        "task": "do the thing",
        "workspace": "./workspace",
        "trio": "kimi",
        "priority": 0,
        "requested_by": "orchestrator",
        "dedup_key": 123,
    }
    with pytest.raises(ValueError, match="dedup_key must be a string"):
        store.create(body)


def test_pg_dedup_rejects_too_long(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    with pytest.raises(ValueError, match="dedup_key is too long"):
        store.create(
            {
                "name": "dedup",
                "task": "do the thing",
                "workspace": "./workspace",
                "trio": "kimi",
                "priority": 0,
                "requested_by": "orchestrator",
                "dedup_key": "x" * 10_000,
            }
        )


def test_pg_dedup_prevents_double_launch(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    a = store.create(
        {
            "name": "dedup",
            "task": "do the thing",
            "workspace": "./workspace",
            "trio": "kimi",
            "priority": 0,
            "requested_by": "orchestrator",
            "dedup_key": "ship-it",
        }
    )
    b = store.create(
        {
            "name": "dedup",
            "task": "do the thing",
            "workspace": "./workspace",
            "trio": "kimi",
            "priority": 0,
            "requested_by": "orchestrator",
            "dedup_key": "ship-it",
        }
    )
    assert a.queue_id == b.queue_id
    store.mark_launched(a.queue_id, "run-1")
    with pytest.raises(ValueError, match="entry is not pending"):
        store.mark_launched(a.queue_id, "run-2")
    assert store.get(a.queue_id).run_id == "run-1"


def test_pg_store_survives_reload(tmp_path: Path) -> None:
    """A fresh store instance reads the same rows back field-for-field."""
    url = _db_url()
    if not url:
        pytest.skip("LH_HARNESS_DB_URL not set; Postgres backend unavailable")
    created = PgQueueStore(url).create(
        {
            "name": "reload",
            "task": "do the thing",
            "workspace": "./workspace",
            "max_rounds": 7,
            "trio": "qwen",
            "priority": 3,
            "requested_by": "reload",
            "base_check": "origin/main",
            "dedup_key": "none",
        }
    )
    reopened = PgQueueStore(url)
    entry = reopened.get(created.queue_id)
    assert entry is not None
    assert entry.queue_id == created.queue_id
    assert entry.status == "pending"
    assert entry.max_rounds == 7
    assert entry.trio == "qwen"
    assert entry.priority == 3
    assert entry.base_check == "origin/main"
    assert entry.dedup_key is None
    assert entry.created_at == created.created_at
    assert entry.updated_at == created.updated_at
    assert entry.to_dict() == created.to_dict()


def _pg_app(tmp_path: Path) -> TestClient:
    url = _db_url()
    if not url:
        pytest.skip("LH_HARNESS_DB_URL not set; Postgres backend unavailable")
    store = PgQueueStore(url)
    from lh_harness.webapi.server import create_app

    return TestClient(create_app(runs_root=tmp_path / "runs", auth_token="secret", queue_store=store))


def _pg_payload(**overrides) -> dict:
    body = {
        "name": "pg-dedup",
        "task": "do the thing",
        "workspace": "./workspace",
        "trio": "kimi",
        "priority": 0,
        "requested_by": "orchestrator",
    }
    body.update(overrides)
    return body


def test_api_pg_dedup_key_passes_through(tmp_path: Path) -> None:
    app = _pg_app(tmp_path)
    client = TestClient(app)
    first = client.post("/api/queue", json=_pg_payload(dedup_key="api-key")).json()
    second = client.post("/api/queue", json=_pg_payload(dedup_key="api-key")).json()
    assert first["queue_id"] == second["queue_id"]
    assert client.get("/api/queue").json()["counts"]["pending"] == 1


def test_pg_select_queue_store_file_default(tmp_path: Path) -> None:
    """No backend configured -> file store, never PgQueueStore."""
    store = _select_queue_store(tmp_path / "runs", None)
    assert isinstance(store, QueueStore)
    if PgQueueStore is not None:
        assert not isinstance(store, PgQueueStore)


def test_pg_select_queue_store_rejects_bad_backend(tmp_path: Path) -> None:
    project = {"queue": {"backend": "influxdb"}}
    with pytest.raises(ValueError, match="unknown"):
        _select_queue_store(tmp_path / "runs", project)


def test_pg_select_requires_database_url(tmp_path: Path) -> None:
    project = {"queue": {"backend": "postgres"}}
    with pytest.raises(ValueError, match="database_url"):
        _select_queue_store(tmp_path / "runs", project)


def test_pg_create_without_driver_raises(tmp_path: Path) -> None:
    """Constructing PgQueueStore with a bad URL fails at connect, not import."""
    if PgQueueStore is None:
        pytest.skip("psycopg not installed; PgQueueStore import fell back to None")
    with pytest.raises(Exception):
        PgQueueStore("postgresql://user:pass@127.0.0.1:1/missing?connect_timeout=1")


def test_pg_empty_store_counts(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    assert store.counts() == {"pending": 0, "launched": 0, "done": 0, "failed": 0, "blocked": 0}


def test_pg_list_empty(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    assert store.list() == []


def test_pg_store_update_roundtrip(tmp_path: Path) -> None:
    store = _require_backend(tmp_path)
    entry = store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 0, "requested_by": "ci"}
    )
    assert store.get(entry.queue_id) is not None
    assert store.counts()["pending"] == 1


def test_pg_store_requeue_creates_successor(tmp_path: Path) -> None:
    """Test that requeue creates a successor entry with correct fields."""
    store = _require_backend(tmp_path)

    # Create original failed entry
    original = store.create(
        {
            "name": "test task",
            "task": "do something",
            "workspace": "./workspace",
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 10,
            "requested_by": "ci",
        }
    )
    store.mark_failed(original.queue_id, "provider_rate_limit")

    # Requeue the failed entry
    successor = store.requeue(original.queue_id, "provider_rate_limit")

    assert successor is not None
    assert successor.queue_id != original.queue_id
    assert successor.status == "pending"
    assert successor.name == original.name
    assert successor.task == original.task
    assert successor.workspace == original.workspace
    assert successor.max_rounds == original.max_rounds
    assert successor.trio == original.trio
    assert successor.priority == original.priority
    assert successor.requested_by == original.requested_by
    assert successor.base_check == original.base_check
    assert successor.retry_of == original.queue_id
    assert successor.attempt == 2  # original attempt was 1
    assert successor.failure_cause == "provider_rate_limit"
    assert successor.dedup_key is None  # retries must not collide with original dedup_key


def test_pg_store_requeue_exceeds_max_retries(tmp_path: Path) -> None:
    """Test that requeue fails when attempt would exceed max_retries.
    Note: PgQueueStore uses a hardcoded max_retries=2 (same as file store default).
    """
    store = _require_backend(tmp_path)

    # Create original failed entry (attempt=1)
    original = store.create(
        {
            "name": "test task",
            "task": "do something",
            "workspace": "./workspace",
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 10,
            "requested_by": "ci",
        }
    )
    store.mark_failed(original.queue_id, "provider_rate_limit")

    # First requeue should succeed (attempt=2)
    successor1 = store.requeue(original.queue_id, "provider_rate_limit")
    assert successor1 is not None
    assert successor1.attempt == 2

    # Mark the first successor as failed
    store.mark_failed(successor1.queue_id, "provider_rate_limit")

    # Second requeue should succeed (attempt=3)
    successor2 = store.requeue(successor1.queue_id, "provider_rate_limit")
    assert successor2 is not None
    assert successor2.attempt == 3

    # Mark the second successor as failed
    store.mark_failed(successor2.queue_id, "provider_rate_limit")

    # Third requeue should fail (would be attempt=4 > max_retries=2)
    with pytest.raises(ValueError, match="exceeded max_retries"):
        store.requeue(successor2.queue_id, "provider_rate_limit")


def test_pg_store_requeue_non_failed_entry(tmp_path: Path) -> None:
    """Test that requeue fails for non-failed entries."""
    store = _require_backend(tmp_path)

    # Create pending entry
    pending = store.create(
        {
            "name": "test task",
            "task": "do something",
            "workspace": "./workspace",
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 10,
            "requested_by": "ci",
        }
    )

    # Try to requeue pending entry
    with pytest.raises(ValueError, match="can only requeue failed entries"):
        store.requeue(pending.queue_id, "provider_rate_limit")

    # Mark as done (via the transition table: pending -> launched -> done)
    # and try again
    store.mark_launched(pending.queue_id, "run-1")
    store.mark_done(pending.queue_id, reason="completed")
    with pytest.raises(ValueError, match="can only requeue failed entries"):
        store.requeue(pending.queue_id, "provider_rate_limit")


def test_pg_store_requeue_preserves_fields(tmp_path: Path) -> None:
    """Test that requeue preserves all relevant fields from original."""
    store = _require_backend(tmp_path)

    # Create original failed entry with all fields
    original_data = {
        "name": "complex task",
        "task": "do something complex with ${VAR}",
        "workspace": "/tmp/workspace",
        "max_rounds": 10,
        "trio": "qwen",
        "priority": 5,
        "requested_by": "user123",
        "base_check": "origin/main",
        "dedup_key": "original-key-123",
    }
    original = store.create(original_data)
    store.mark_failed(original.queue_id, "executor timeout")

    # Requeue
    successor = store.requeue(original.queue_id, "executor timeout")

    assert successor is not None
    assert successor.name == original_data["name"]
    assert successor.task == original_data["task"]
    assert successor.workspace == original_data["workspace"]
    assert successor.max_rounds == original_data["max_rounds"]
    assert successor.trio == original_data["trio"]
    assert successor.priority == original_data["priority"]
    assert successor.requested_by == original_data["requested_by"]
    assert successor.base_check == original_data["base_check"]
    # dedup_key should be None for retry
    assert successor.dedup_key is None
    # retry fields
    assert successor.retry_of == original.queue_id
    assert successor.attempt == 2
    assert successor.failure_cause == "executor timeout"


def test_pg_store_requeue_preserves_continuation_fields(tmp_path: Path) -> None:
    """PG requeue surface: a retried continuation stays a continuation (task 233)."""
    store = _require_backend(tmp_path)
    original = store.create(
        {
            "name": "cont retry",
            "task": "t",
            "workspace": "/tmp/workspace",
            "trio": "kimi",
            "branch": "feat/cont",
            "requested_by": "ci",
        }
    )
    store.mark_failed(original.queue_id, "boom")
    successor = store.requeue(original.queue_id, "boom")
    assert successor is not None
    assert successor.branch == "feat/cont"
    # Read back from the row, not the in-memory return value.
    read = store.get(successor.queue_id)
    assert read is not None
    assert read.branch == "feat/cont"
    assert read.continue_branch is False


def test_pg_store_persists_continue_branch_flag(tmp_path: Path) -> None:
    """PG create/get surface: continue_branch=True round-trips the row."""
    store = _require_backend(tmp_path)
    entry = store.create(
        {
            "name": "cont flag",
            "task": "t",
            "workspace": "/tmp/workspace",
            "trio": "kimi",
            "continue_branch": True,
            "requested_by": "ci",
        }
    )
    read = store.get(entry.queue_id)
    assert read is not None
    assert read.continue_branch is True
    assert read.branch == ""


def test_pg_store_requeue_creates_queue_events_row(tmp_path: Path) -> None:
    """Test that requeue creates a queue_events row for the enqueue event."""
    store = _require_backend(tmp_path)

    # Create a failed entry to retry
    original = store.create(
        {
            "name": "requeue me",
            "task": "sleep",
            "workspace": "./workspace",
            "max_rounds": 1,
            "trio": "kimi",
            "priority": 5,
            "requested_by": "pytest",
        }
    )
    store.mark_failed(original.queue_id, "original failure")

    # Requeue it
    successor = store.requeue(original.queue_id, "executor timeout")
    assert successor is not None

    # Verify a queue_events row was inserted for the enqueue event
    with store._txn() as txn:
        txn.execute(
            "SELECT host, queue_id, ts, event, actor, rationale, payload "
            "FROM harness.queue_events "
            "WHERE queue_id = %s AND event = 'enqueue' "
            "ORDER BY ts DESC LIMIT 1",
            (successor.queue_id,),
        )
        row = txn.fetchone()
        assert row is not None
        host, queue_id, ts, event, actor, rationale, payload = row
        assert queue_id == successor.queue_id
        assert event == "enqueue"
        assert actor == ""
        assert rationale == "executor timeout"
        # Check that the payload matches the successor entry (minus queue_id and created_at)
        import json
        payload_data = json.loads(payload)
        # The payload should have all the fields of the successor except queue_id and created_at
        expected = successor.to_dict()
        expected.pop('queue_id', None)
        expected.pop('created_at', None)
        assert payload_data == expected


def test_pg_store_record_block_and_unblock(tmp_path: Path) -> None:
    """Test that record_block and record_unblock work correctly."""
    store = _require_backend(tmp_path)

    # Create a pending entry
    entry = store.create(
        {
            "name": "test task",
            "task": "do something",
            "workspace": "./workspace",
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 10,
            "requested_by": "ci",
        }
    )
    assert entry.status == "pending"

    # Block the entry
    blocked = store.record_block(entry.queue_id)
    assert blocked is not None
    assert blocked.status == "blocked"
    assert store.get(entry.queue_id).status == "blocked"

    # Try to block again (should not change status)
    blocked_again = store.record_block(entry.queue_id)
    assert blocked_again is not None
    assert blocked_again.status == "blocked"

    # Unblock the entry
    unblocked = store.record_unblock(entry.queue_id)
    assert unblocked is not None
    assert unblocked.status == "pending"
    assert store.get(entry.queue_id).status == "pending"

    # Try to unblock again (should not change status)
    unblocked_again = store.record_unblock(entry.queue_id)
    assert unblocked_again is not None
    assert unblocked_again.status == "pending"

    # Test invalid transitions
    # Mark as launched and try to block (should fail)
    launched = store.mark_launched(entry.queue_id, "run-123")
    assert launched is not None
    assert launched.status == "launched"
    blocked_from_launched = store.record_block(launched.queue_id)
    assert blocked_from_launched is None  # Cannot block from launched
    assert store.get(launched.queue_id).status == "launched"  # Status unchanged

    # Mark as done and try to unblock (should fail)
    done = store.mark_done(entry.queue_id, reason="completed")
    assert done is not None
    assert done.status == "done"
    unblocked_from_done = store.record_unblock(done.queue_id)
    assert unblocked_from_done is None  # Cannot unblock from done
    assert store.get(done.queue_id).status == "done"  # Status unchanged


def test_pg_store_blocked_to_launched_transition(tmp_path: Path) -> None:
    """Test that a blocked entry can transition to launched via mark_launched."""
    store = _require_backend(tmp_path)

    # Create a pending entry
    entry = store.create(
        {
            "name": "test task",
            "task": "do something",
            "workspace": "./workspace",
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 10,
            "requested_by": "ci",
        }
    )
    assert entry.status == "pending"

    # Block the entry
    blocked = store.record_block(entry.queue_id)
    assert blocked is not None
    assert blocked.status == "blocked"

    # Launch from blocked state
    launched = store.mark_launched(entry.queue_id, "run-456")
    assert launched is not None
    assert launched.status == "launched"
    assert launched.run_id == "run-456"
    assert store.get(entry.queue_id).status == "launched"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
