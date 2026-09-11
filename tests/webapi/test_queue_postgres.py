"""Tests for the Postgres queue store.

These tests mirror the hermetic queue tests but run against a Postgres database.
They are skipped when the LH_HARNESS_DB_PASSWORD environment variable is not set.
"""

from __future__ import annotations

import os
import pytest

pytest.importorskip("psycopg2")
from pathlib import Path

from lh_harness.pg_queue import PgQueueStore


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    return root


def _payload(**overrides) -> dict:
    body = {
        "name": "test",
        "task": "do something",
        "workspace": "./workspace",
        "trio": "kimi",
        "priority": 0,
        "requested_by": "ci",
    }
    body.update(overrides)
    return body


@pytest.mark.skipif(
    not os.environ.get("LH_HARNESS_DB_PASSWORD"),
    reason="LH_HARNESS_DB_PASSWORD not set",
)
def test_pg_queue_store_creates_entry(tmp_path: Path) -> None:
    # Skip if test database URL is not provided
    test_db_url = os.environ.get("LH_HARNESS_TEST_DATABASE_URL")
    if not test_db_url:
        pytest.skip("LH_HARNESS_TEST_DATABASE_URL not set")

    root = _fixture(tmp_path)
    store = PgQueueStore(root, test_db_url)
    entry = store.create(_payload())
    assert entry.queue_id.startswith("q-")
    assert entry.status == "pending"
    # We cannot easily check the file system because the store is Postgres-backed.
    # Instead, we verify by fetching the entry.
    fetched = store.get(entry.queue_id)
    assert fetched is not None
    assert fetched.queue_id == entry.queue_id


@pytest.mark.skipif(
    not os.environ.get("LH_HARNESS_DB_PASSWORD"),
    reason="LH_HARNESS_DB_PASSWORD not set",
)
def test_pg_queue_store_validates_required_fields(tmp_path: Path) -> None:
    test_db_url = os.environ.get("LH_HARNESS_TEST_DATABASE_URL")
    if not test_db_url:
        pytest.skip("LH_HARNESS_TEST_DATABASE_URL not set")

    root = _fixture(tmp_path)
    store = PgQueueStore(root, test_db_url)
    with pytest.raises(ValueError, match="task must be a string"):
        store.create({"name": "x", "workspace": "w", "trio": "kimi", "requested_by": "ci"})
    with pytest.raises(ValueError, match="name is required"):
        store.create({"task": "x", "workspace": "w", "trio": "kimi", "requested_by": "ci"})
    with pytest.raises(ValueError, match="workspace is required"):
        store.create({"name": "x", "task": "x", "trio": "kimi", "requested_by": "ci"})
    with pytest.raises(ValueError, match="requested_by is required"):
        store.create({"name": "x", "task": "x", "workspace": "w", "trio": "kimi"})


@pytest.mark.skipif(
    not os.environ.get("LH_HARNESS_DB_PASSWORD"),
    reason="LH_HARNESS_DB_PASSWORD not set",
)
def test_pg_queue_store_accepts_task_file(tmp_path: Path) -> None:
    test_db_url = os.environ.get("LH_HARNESS_TEST_DATABASE_URL")
    if not test_db_url:
        pytest.skip("LH_HARNESS_TEST_DATABASE_URL not set")

    root = _fixture(tmp_path)
    task_path = tmp_path / "task.md"
    task_path.write_text("file task", encoding="utf-8")
    store = PgQueueStore(root, os.environ.get("LH_HARNESS_TEST_DATABASE_URL"))
    entry = store.create(
        {
            "name": "from file",
            "task_file": str(task_path),
            "workspace": "./workspace",
            "trio": "qwen",
            "priority": 0,
            "requested_by": "fleet",
        }
    )
    assert entry.task == "file task"
    assert entry.trio == "qwen"


@pytest.mark.skipif(
    not os.environ.get("LH_HARNESS_DB_PASSWORD"),
    reason="LH_HARNESS_DB_PASSWORD not set",
)
def test_pg_queue_store_rejects_both_task_and_task_file(tmp_path: Path) -> None:
    test_db_url = os.environ.get("LH_HARNESS_TEST_DATABASE_URL")
    if not test_db_url:
        pytest.skip("LH_HARNESS_TEST_DATABASE_URL not set")

    root = _fixture(tmp_path)
    store = PgQueueStore(root, test_db_url)
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


@pytest.mark.skipif(
    not os.environ.get("LH_HARNESS_DB_PASSWORD"),
    reason="LH_HARNESS_DB_PASSWORD not set",
)
def test_pg_queue_store_lists_and_sorts(tmp_path: Path) -> None:
    test_db_url = os.environ.get("LH_HARNESS_TEST_DATABASE_URL")
    if not test_db_url:
        pytest.skip("LH_HARNESS_TEST_DATABASE_URL not set")

    root = _fixture(tmp_path)
    store = PgQueueStore(root, test_db_url)
    low = store.create(
        {"name": "low", "task": "t", "workspace": "w", "trio": "kimi", "priority": 1, "requested_by": "ci"}
    )
    high = store.create(
        {"name": "high", "task": "t", "workspace": "w", "trio": "kimi", "priority": 10, "requested_by": "ci"}
    )
    items = store.list()
    assert [item.queue_id for item in items] == [high.queue_id, low.queue_id]


@pytest.mark.skipif(
    not os.environ.get("LH_HARNESS_DB_PASSWORD"),
    reason="LH_HARNESS_DB_PASSWORD not set",
)
def test_pg_queue_store_priority_update(tmp_path: Path) -> None:
    test_db_url = os.environ.get("LH_HARNESS_TEST_DATABASE_URL")
    if not test_db_url:
        pytest.skip("LH_HARNESS_TEST_DATABASE_URL not set")

    root = _fixture(tmp_path)
    store = PgQueueStore(root, test_db_url)
    entry = store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 1, "requested_by": "ci"}
    )
    updated = store.set_priority(entry.queue_id, 42)
    assert updated is not None
    assert updated.priority == 42


@pytest.mark.skipif(
    not os.environ.get("LH_HARNESS_DB_PASSWORD"),
    reason="LH_HARNESS_DB_PASSWORD not set",
)
def test_pg_queue_store_delete_pending(tmp_path: Path) -> None:
    test_db_url = os.environ.get("LH_HARNESS_TEST_DATABASE_URL")
    if not test_db_url:
        pytest.skip("LH_HARNESS_TEST_DATABASE_URL not set")

    root = _fixture(tmp_path)
    store = PgQueueStore(root, test_db_url)
    entry = store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 0, "requested_by": "ci"}
    )
    removed = store.delete(entry.queue_id)
    assert removed is not None
    assert store.get(entry.queue_id) is None


@pytest.mark.skipif(
    not os.environ.get("LH_HARNESS_DB_PASSWORD"),
    reason="LH_HARNESS_DB_PASSWORD not set",
)
def test_pg_queue_store_mark_launched_and_done(tmp_path: Path) -> None:
    test_db_url = os.environ.get("LH_HARNESS_TEST_DATABASE_URL")
    if not test_db_url:
        pytest.skip("LH_HARNESS_TEST_DATABASE_URL not set")

    root = _fixture(tmp_path)
    store = PgQueueStore(root, test_db_url)
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


# We can add more tests as needed, but the above covers the basic CRUD operations.
# Note: The PgQueueStore does not support the record_skip method? Actually it does, because we implemented it.
# Let's add a test for record_skip and unblock.

@pytest.mark.skipif(
    not os.environ.get("LH_HARNESS_DB_PASSWORD"),
    reason="LH_HARNESS_DB_PASSWORD not set",
)
def test_pg_queue_store_record_skip(tmp_path: Path) -> None:
    test_db_url = os.environ.get("LH_HARNESS_TEST_DATABASE_URL")
    if not test_db_url:
        pytest.skip("LH_HARNESS_TEST_DATABASE_URL not set")

    root = _fixture(tmp_path)
    store = PgQueueStore(root, test_db_url)
    entry = store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 0, "requested_by": "ci"}
    )
    skipped = store.record_skip(entry.queue_id, "test reason")
    assert skipped is not None
    assert skipped.status == "pending"
    assert "test reason" in skipped.skip_reasons


@pytest.mark.skipif(
    not os.environ.get("LH_HARNESS_DB_PASSWORD"),
    reason="LH_HARNESS_DB_PASSWORD not set",
)
def test_pg_queue_store_unblock(tmp_path: Path) -> None:
    test_db_url = os.environ.get("LH_HARNESS_TEST_DATABASE_URL")
    if not test_db_url:
        pytest.skip("LH_HARNESS_TEST_DATABASE_URL not set")

    root = _fixture(tmp_path)
    store = PgQueueStore(root, test_db_url)
    # First create an entry and block it manually by updating the status?
    # But we don't have a direct method to block. We can update the status to blocked via the update method?
    # However, the PgQueueStore does not have a block method. We added an unblock method to QueueStore, but not a block method.
    # We can block by setting the status to blocked via the update method? But we don't want to expose that.
    # Alternatively, we can test the unblock method by first creating an entry, then setting its status to blocked via direct database update?
    # That is too heavy for a unit test.

    # Instead, let's test the unblock method by creating an entry, then we will block it by using the update method to set status to blocked (if we had a method to block, we would use it).
    # Since we don't have a block method in the PgQueueStore, we can skip this test for now or we can test the unblock method by manually setting the status in the database.

    # We'll do: create an entry, then update its status to blocked in the database, then call unblock and check that it becomes pending.

    entry = store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 0, "requested_by": "ci"}
    )
    # Manually block the entry in the database
    with store._get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE harness.queue SET status = 'blocked' WHERE queue_id = %s",
                (entry.queue_id,),
            )
            conn.commit()

    # Now unblock it
    unblocked = store.unblock(entry.queue_id)
    assert unblocked is not None
    assert unblocked.status == "pending"