from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lh_harness.launcher import Launcher
from lh_harness.queue import QueueStore, queue_config_from_config


def _fixture(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    return root


def test_queue_store_requeue_creates_successor(tmp_path: Path) -> None:
    """Test that requeue creates a successor entry with correct fields."""
    root = _fixture(tmp_path)
    store = QueueStore(root)
    
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
    assert successor.dedup_key is None  # retries must not collide


def test_queue_store_requeue_exceeds_max_retries(tmp_path: Path) -> None:
    """Test that requeue fails when attempt would exceed max_retries."""
    root = _fixture(tmp_path)
    # Create config with max_retries = 1
    config = queue_config_from_config({"queue": {"capacity": {"max_retries": 1}}})
    store = QueueStore(root, config)
    
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
    
    # First requeue should succeed (attempt 2)
    successor1 = store.requeue(original.queue_id, "provider_rate_limit")
    assert successor1 is not None
    assert successor1.attempt == 2
    
    # Second requeue should fail (would be attempt 3 > max_retries=1)
    with pytest.raises(ValueError, match="exceeded max_retries"):
        store.requeue(successor1.queue_id, "provider_rate_limit")


def test_queue_store_requeue_non_failed_entry(tmp_path: Path) -> None:
    """Test that requeue fails for non-failed entries."""
    root = _fixture(tmp_path)
    store = QueueStore(root)
    
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


def test_queue_store_requeue_with_config_max_retries(tmp_path: Path) -> None:
    """Test that requeue respects max_retries from config."""
    root = _fixture(tmp_path)
    # Create config with max_retries = 0 (no retries allowed)
    config = queue_config_from_config({"queue": {"capacity": {"max_retries": 0}}})
    store = QueueStore(root, config)
    
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
    
    # Try to requeue - should fail because max_retries=0
    with pytest.raises(ValueError, match="exceeded max_retries"):
        store.requeue(original.queue_id, "provider_rate_limit")


def test_queue_store_requeue_preserves_fields(tmp_path: Path) -> None:
    """Test that requeue preserves all relevant fields from original."""
    root = _fixture(tmp_path)
    store = QueueStore(root)

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


def test_retry_fields_survive_disk_round_trip(tmp_path: Path) -> None:
    """retry_of/attempt/failure_cause must survive a save/reload cycle.

    A fresh QueueStore instance (simulating a service restart) re-reads the
    on-disk JSON; the retry fields were previously dropped by from_dict, which
    made every retry look like an original after restart and re-armed the
    max_retries budget.
    """
    root = _fixture(tmp_path)
    store = QueueStore(root)
    original = store.create(
        {
            "name": "round trip",
            "task": "do the thing",
            "workspace": "./workspace",
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 1,
            "requested_by": "ci",
        }
    )
    store.mark_failed(original.queue_id, "provider_rate_limit")
    successor = store.requeue(original.queue_id, "provider_rate_limit")
    assert successor is not None

    # Reload through a fresh store (new QueueStore, same runs root).
    reopened = QueueStore(root)
    reloaded = reopened.get(successor.queue_id)
    assert reloaded is not None
    assert reloaded.retry_of == original.queue_id
    assert reloaded.attempt == 2
    assert reloaded.failure_cause == "provider_rate_limit"

    # The original keeps its fields too, and its attempt is what the cap sees.
    reloaded_original = reopened.get(original.queue_id)
    assert reloaded_original is not None
    assert reloaded_original.attempt == 1
    # attempt=1 <= max_retries default 2 -> the cap would still allow a retry
    # of the ORIGINAL, but the successor (attempt 2) is at the cap boundary.
    assert reopened.get(successor.queue_id) is not None


def test_launcher_failure_path_requeues_retryable_cause(tmp_path: Path) -> None:
    """A launch that raises must fail the entry and spawn a retryable successor."""
    root = _fixture(tmp_path)
    store = QueueStore(root)
    config = {
        "trios": {"kimi": {"agent": "codex", "model": None, "mcp_profile": None}},
        "capacity": {"kimi_max": 1, "qwen_max": 1, "poll_seconds": 1, "max_retries": 2},
    }

    class BrokenSupervisor:
        def list_run_items(self):
            return []

        def status(self, run_id):
            return {"status": "idle", "run_id": run_id}

        def owner(self, run_id):
            return {}

        def create_run(self, **kwargs):  # transport-style failure
            raise ConnectionError("provider 429")

    launcher = Launcher(BrokenSupervisor(), store, queue_config=config)
    entry = store.create(
        {
            "name": "retry me",
            "task": "do the thing",
            "workspace": "./workspace",
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    asyncio.run(launcher.tick())

    failed = store.get(entry.queue_id)
    assert failed is not None
    assert failed.status == "failed"
    assert "launch failed" in (failed.reason or "")
    # The launcher saw a transport error -> retryable -> a pending successor.
    assert store.counts()["pending"] == 1
    successor = next(e for e in store.list() if e.status == "pending")
    assert successor.retry_of == entry.queue_id
    assert successor.attempt == 2
    assert successor.failure_cause is not None

    # The service event log carries queue.requeued with the contract fields.
    events_path = root / "queue" / "service_events.jsonl"
    assert events_path.is_file()
    requeued = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if "queue.requeued" in line
    ]
    assert requeued, "queue.requeued event missing"
    payload = requeued[-1]["payload"]
    assert payload["original"] == entry.queue_id
    assert payload["successor"] == successor.queue_id
    assert payload["attempt"] == 2


def test_launcher_failure_path_does_not_requeue_non_retryable_cause(tmp_path: Path) -> None:
    """invalid task / workspace missing must fail the entry without a successor."""
    root = _fixture(tmp_path)
    store = QueueStore(root)
    config = {
        "trios": {"kimi": {"agent": "codex", "model": None, "mcp_profile": None}},
        "capacity": {"kimi_max": 1, "qwen_max": 1, "poll_seconds": 1, "max_retries": 2},
    }

    class BrokenSupervisor:
        def list_run_items(self):
            return []

        def status(self, run_id):
            return {"status": "idle", "run_id": run_id}

        def owner(self, run_id):
            return {}

        def create_run(self, **kwargs):
            raise ValueError("workspace must be inside configured workspace root")

    launcher = Launcher(BrokenSupervisor(), store, queue_config=config)
    entry = store.create(
        {
            "name": "bad workspace",
            "task": "do the thing",
            "workspace": "./workspace",
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    asyncio.run(launcher.tick())

    failed = store.get(entry.queue_id)
    assert failed is not None
    assert failed.status == "failed"
    # Non-retryable cause -> no successor.
    assert store.counts()["pending"] == 0
    assert len(store.list()) == 1


def test_transition_table_rejects_done_to_pending(tmp_path: Path) -> None:
    """A terminal entry cannot be moved back to pending by any mutator."""
    root = _fixture(tmp_path)
    store = QueueStore(root)
    entry = store.create(
        {
            "name": "done entry",
            "task": "do the thing",
            "workspace": "./workspace",
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    store.mark_launched(entry.queue_id, "run-1")
    store.mark_done(entry.queue_id, reason="ok")
    with pytest.raises(ValueError, match="can only requeue failed entries"):
        store.requeue(entry.queue_id, "provider_rate_limit")
    with pytest.raises(ValueError, match="invalid transition from done to failed"):
        store.mark_failed(entry.queue_id, "late failure")


class _StallSupervisor:
    """Minimal supervisor stand-in that serves a report from disk."""

    def __init__(self, runs_root: Path, statuses: dict[str, dict]) -> None:
        self.runs_root = runs_root
        self._statuses = statuses

    def list_run_items(self):
        return [{"id": run_id, "status": s.get("status")} for run_id, s in self._statuses.items()]

    def status(self, run_id):
        return self._statuses.get(run_id, {"status": "idle", "run_id": run_id})

    def owner(self, run_id):
        return {}

    def _run_logs_dir(self, run_id):
        return self.runs_root / run_id / "lh_harness"


def test_launcher_requeues_stalled_episode_report(tmp_path: Path) -> None:
    """A run report carrying the stall signal must requeue, not strand.

    This is the 09-16 deliverable wired end to end at the queue layer: the
    episode layer fails a hung round fast with a runtime signal (see
    tests/webapi/test_stall_detection.py for the adapter proof), the report
    records provider_stall, and the launcher reconcile path turns that into a
    pending successor instead of a stranding.
    """
    root = _fixture(tmp_path)
    store = QueueStore(root)
    entry = store.create(
        {
            "name": "hang candidate",
            "task": "do the thing",
            "workspace": "./workspace",
            "max_rounds": 12,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    run_id = "run-stall-1"
    store.mark_launched(entry.queue_id, run_id)
    report_dir = root / run_id / "lh_harness"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "completion_satisfied": False,
                "abort_reason": "provider_stall",
                "failure_reason": "Agent 执行无输出停滞：Episode stalled: no output for 900s.",
            }
        ),
        encoding="utf-8",
    )
    supervisor = _StallSupervisor(root, {run_id: {"status": "failed", "run_id": run_id}})
    launcher = Launcher(
        supervisor,
        store,
        queue_config={
            "trios": {"kimi": {"agent": "codex", "model": None, "mcp_profile": None}},
            "capacity": {"kimi_max": 1, "qwen_max": 1, "poll_seconds": 1, "max_retries": 2},
        },
    )
    asyncio.run(launcher.tick())

    failed = store.get(entry.queue_id)
    assert failed is not None
    assert failed.status == "failed"
    assert "provider_stall" in (failed.reason or "")
    successor = next(e for e in store.list() if e.status == "pending")
    assert successor.retry_of == entry.queue_id
    assert successor.attempt == 2
    assert successor.failure_cause is not None and "stall" in successor.failure_cause


def test_launcher_does_not_requeue_human_stop(tmp_path: Path) -> None:
    """needs_human_input / user_cancelled reports must stay failed."""
    root = _fixture(tmp_path)
    store = QueueStore(root)
    for cause in ("needs_human_input", "user_cancelled", "max_rounds_exhausted"):
        entry = store.create(
            {
                "name": f"stop {cause}",
                "task": "do the thing",
                "workspace": "./workspace",
                "max_rounds": 12,
                "trio": "kimi",
                "priority": 0,
                "requested_by": "ci",
            }
        )
        run_id = f"run-stop-{cause}"
        store.mark_launched(entry.queue_id, run_id)
        report_dir = root / run_id / "lh_harness"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "report.json").write_text(
            json.dumps(
                {
                    "status": "incomplete",
                    "completion_satisfied": False,
                    "abort_reason": cause,
                    "failure_reason": f"run stopped: {cause}",
                }
            ),
            encoding="utf-8",
        )
    statuses = {
        f"run-stop-{cause}": {"status": "incomplete", "run_id": f"run-stop-{cause}"}
        for cause in ("needs_human_input", "user_cancelled", "max_rounds_exhausted")
    }
    supervisor = _StallSupervisor(root, statuses)
    launcher = Launcher(
        supervisor,
        store,
        queue_config={
            "trios": {"kimi": {"agent": "codex", "model": None, "mcp_profile": None}},
            "capacity": {"kimi_max": 3, "qwen_max": 1, "poll_seconds": 1, "max_retries": 2},
        },
    )
    asyncio.run(launcher.tick())

    assert store.counts()["pending"] == 0
    assert store.counts()["failed"] == 3


def test_queue_store_blocked_unblock_launch(tmp_path: Path) -> None:
    """Test that a blocked entry can unblock and then launch."""
    root = _fixture(tmp_path)
    store = QueueStore(root)

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

    # Block the entry (pending -> blocked)
    blocked = store.record_block(entry.queue_id)
    assert blocked is not None
    assert blocked.status == "blocked"
    assert store.get(entry.queue_id).status == "blocked"

    # Unblock the entry (blocked -> pending)
    unblocked = store.record_unblock(entry.queue_id)
    assert unblocked is not None
    assert unblocked.status == "pending"
    assert store.get(entry.queue_id).status == "pending"

    # Launch the entry (pending -> launched)
    launched = store.mark_launched(entry.queue_id, "run-123")
    assert launched is not None
    assert launched.status == "launched"
    assert launched.run_id == "run-123"
    assert store.get(entry.queue_id).status == "launched"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
