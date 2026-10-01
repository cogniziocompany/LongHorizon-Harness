"""Launcher queue.* events are pushed to fleet-admin, fail-open.

Deliverable 3 of fc-H1b: ``queue.launched``/``queue.done``/``queue.failed``
(run events) and ``queue.requeued``/``queue.skipped`` (service events) are
still written to their local JSONL ledgers AND pushed through
``fleet.post_event_record``; with fleet unconfigured the push is a no-op.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from lh_harness.fleet.reporter import get_reporter
from lh_harness.launcher import Launcher
from lh_harness.queue import QueueStore, default_queue_config


class _FakeSupervisor:
    """Minimal supervisor stand-in providing the run logs dir."""

    def __init__(self, runs_root: Path) -> None:
        self.runs_root = runs_root

    def _run_logs_dir(self, run_id: str) -> Path:
        return self.runs_root / run_id / "lh_harness"


def _getenv() -> dict[str, str | None]:
    names = (
        "LH_HARNESS_FLEET_URL",
        "LH_HARNESS_FLEET_NODE",
        "LH_HARNESS_FLEET_KEY",
        "LH_HARNESS_FLEET_LABELS",
    )
    return {name: os.environ.get(name) for name in names}


def _setenv(values: dict[str, str | None]) -> None:
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(autouse=True)
def _isolate_reporter():
    """Reset the reporter singleton and stash fleet env before each test."""
    original = _getenv()
    get_reporter(reset=True)
    yield original
    _setenv(original)
    get_reporter(reset=True)


@pytest.fixture
def launcher(tmp_path: Path) -> tuple[Launcher, Path]:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _FakeSupervisor(root)
    return Launcher(supervisor, store, queue_config=default_queue_config()), root


def _capture_reporter() -> list[dict[str, Any]]:
    """Point the configured singleton's egress at an in-memory capture list."""
    reporter = get_reporter(reset=True)
    captured: list[dict[str, Any]] = []

    def _capture(endpoint: str, payload: Any, *, gzip_body: bool = True) -> None:
        captured.append({"endpoint": endpoint, "payload": payload})

    reporter._post = _capture  # type: ignore[method-assign]
    return captured


def _set_fleet_env() -> None:
    _setenv(
        {
            "LH_HARNESS_FLEET_URL": "http://127.0.0.1:1",
            "LH_HARNESS_FLEET_NODE": "queue-node",
            "LH_HARNESS_FLEET_KEY": "queue-key",
            "LH_HARNESS_FLEET_LABELS": None,
        }
    )


def test_queue_run_events_reach_fake_reporter(launcher, _isolate_reporter):
    """queue.launched/done/failed run events are pushed as fleet envelopes."""
    _set_fleet_env()
    captured = _capture_reporter()
    launcher_obj, root = launcher

    # Same event types the launcher emits at its reconciliation/launch sites.
    launcher_obj._emit_run_event("run-1", "queue.launched", {"queue_id": "q-1", "run_id": "run-1"})
    launcher_obj._emit_run_event("run-1", "queue.done", {"queue_id": "q-1", "run_id": "run-1"})
    launcher_obj._emit_run_event("run-2", "queue.failed", {"queue_id": "q-2", "run_id": "run-2"})

    events = [item["payload"] for item in captured if item["endpoint"] == "/harness/events"]
    flat = [event for batch in events for event in batch]
    by_run_type = {(event["run_id"], event["type"]) for event in flat}
    assert ("run-1", "queue.launched") in by_run_type
    assert ("run-1", "queue.done") in by_run_type
    assert ("run-2", "queue.failed") in by_run_type

    # The local run ledger still receives every record (fleet is a side-car).
    ledger = root / "run-1" / "lh_harness" / "role_orchestration" / "events.jsonl"
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "queue.launched" in lines[0]
    assert "queue.done" in lines[1]


def test_queue_service_events_reach_fake_reporter(launcher, _isolate_reporter):
    """queue.requeued/skipped service events are pushed as fleet envelopes."""
    _set_fleet_env()
    captured = _capture_reporter()
    launcher_obj, root = launcher

    launcher_obj._emit_service_event(
        "queue.requeued",
        {"original": "q-1", "successor": "q-2", "cause": "provider_rate_limit", "attempt": 2},
    )
    launcher_obj._emit_service_event(
        "queue.skipped",
        {"queue_id": "q-3", "trio": "kimi", "workspace": "./w", "reason": "capacity"},
    )

    events = [item["payload"] for item in captured if item["endpoint"] == "/harness/events"]
    flat = [event for batch in events for event in batch]
    types = [event["type"] for event in flat]
    assert "queue.requeued" in types
    assert "queue.skipped" in types
    skipped = next(event for event in flat if event["type"] == "queue.skipped")
    # Service events carry no run_id; the fleet envelope namespaces them.
    assert skipped["run_id"] in ("local", "")
    assert skipped["payload"]["payload"]["queue_id"] == "q-3"

    # The local service ledger still receives every record.
    service_log = root / "queue" / "service_events.jsonl"
    lines = service_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "queue.requeued" in lines[0]
    assert "queue.skipped" in lines[1]


def test_queue_events_noop_when_fleet_unconfigured(launcher, _isolate_reporter):
    """Without LH_HARNESS_FLEET_* the fleet push is a no-op; JSONL still written."""
    _setenv({name: None for name in _getenv()})
    reporter = get_reporter(reset=True)
    assert reporter.enabled is False
    launcher_obj, root = launcher

    launcher_obj._emit_run_event("run-1", "queue.launched", {"queue_id": "q-1", "run_id": "run-1"})
    launcher_obj._emit_service_event("queue.skipped", {"queue_id": "q-1", "reason": "capacity"})

    # No enabled reporter appeared as a side effect of the emit path.
    assert get_reporter() is reporter
    assert get_reporter().enabled is False

    ledger = root / "run-1" / "lh_harness" / "role_orchestration" / "events.jsonl"
    assert "queue.launched" in ledger.read_text(encoding="utf-8")
    service_log = root / "queue" / "service_events.jsonl"
    assert "queue.skipped" in service_log.read_text(encoding="utf-8")


def test_queue_records_still_parseable_as_json(launcher, _isolate_reporter):
    """Every pushed queue record round-trips through the events.jsonl shape."""
    _set_fleet_env()
    captured = _capture_reporter()
    launcher_obj, root = launcher

    launcher_obj._emit_run_event("run-9", "queue.launched", {"queue_id": "q-9"})

    ledger = root / "run-9" / "lh_harness" / "role_orchestration" / "events.jsonl"
    record = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    assert record["type"] == "queue.launched"
    assert record["run_id"] == "run-9"

    flat = [event for item in captured for event in item["payload"]]
    assert flat[0]["type"] == "queue.launched"
    assert flat[0]["run_id"] == "run-9"
