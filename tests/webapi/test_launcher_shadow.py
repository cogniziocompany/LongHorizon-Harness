"""Observe (shadow) mode — task 173, migration doc §5 Step 2.

With ``[queue] observe = true`` the launcher computes the full launch
decision (eligibility, capacity, key health, trio resolve) and then —
immediately before ``_launch`` — emits ``queue.shadow_launch`` /
``queue.shadow_skip`` and appends one JSON line per decision to
``runs_root/queue/shadow.jsonl``.  It never calls ``create_run`` and every
entry stays pending.  With the flag off (default) behaviour is unchanged:
no shadow path runs and no shadow log is written.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from lh_harness.launcher import Launcher
from lh_harness.queue import QueueStore, default_queue_config

from .test_launcher import FakeSupervisor, _base_entry


def _fixture(tmp_path: Path) -> tuple[Path, QueueStore, FakeSupervisor]:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = FakeSupervisor(root)
    return root, store, supervisor


def _observe_config() -> dict[str, Any]:
    config = default_queue_config()
    config["observe"] = True
    return config


def _shadow_lines(root: Path) -> list[dict[str, Any]]:
    path = root / "queue" / "shadow.jsonl"
    assert path.is_file(), "shadow.jsonl must exist after an observe tick"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _service_types(root: Path) -> list[str]:
    path = root / "queue" / "service_events.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line).get("type")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_observe_mode_launches_nothing_and_entry_stays_pending(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=_observe_config())

    entry = store.create(_base_entry(trio="kimi", workspace=str(tmp_path / "ws")))
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    # The entry stays pending and nothing was created anywhere.
    assert updated.status == "pending"
    assert updated.run_id is None
    assert supervisor.created == []
    assert supervisor._runs == {}


def test_observe_mode_emits_shadow_launch_with_decision_fields(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=_observe_config())

    entry = store.create(_base_entry(trio="kimi", workspace=str(tmp_path / "ws")))
    asyncio.run(launcher.tick())

    lines = _shadow_lines(root)
    assert len(lines) == 1
    record = lines[0]
    assert record["type"] == "queue.shadow_launch"
    payload = record["payload"]
    assert payload["queue_id"] == entry.queue_id
    assert payload["trio"] == "kimi"
    assert payload["workspace"] == str(tmp_path / "ws")
    assert set(payload["roles"]) == {"manager", "executor", "auditor"}
    assert payload["roles"]["manager"]["agent"] == "claude_code"
    assert isinstance(payload["would_run_at"], float)
    # The decision is also visible on the fleet event stream.
    assert "queue.shadow_launch" in _service_types(root)


def test_observe_mode_emits_shadow_skip_with_reason(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = _observe_config()
    config["capacity"]["kimi_max"] = 0
    launcher = Launcher(supervisor, store, queue_config=config)

    entry = store.create(_base_entry(trio="kimi"))
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert supervisor.created == []

    lines = _shadow_lines(root)
    assert len(lines) == 1
    assert lines[0]["type"] == "queue.shadow_skip"
    assert lines[0]["payload"]["queue_id"] == entry.queue_id
    assert lines[0]["payload"]["reason"] == "kimi at capacity"
    assert "queue.shadow_skip" in _service_types(root)


def test_observe_mode_unknown_trio_is_a_shadow_skip(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=_observe_config())

    entry = store.create(_base_entry(trio="qwen"))
    # Remove the trio spec but keep the capacity key so the entry passes the
    # eligibility gate and reaches the trio resolve (where _launch would have
    # skipped it).
    launcher._config["trios"]["qwen"] = None
    asyncio.run(launcher.tick())

    lines = _shadow_lines(root)
    assert len(lines) == 1
    assert lines[0]["type"] == "queue.shadow_skip"
    assert "unknown trio" in lines[0]["payload"]["reason"]
    assert supervisor.created == []


def test_observe_mode_batch_bookkeeping_matches_real_launch(tmp_path: Path) -> None:
    """The second pending entry sees the same capacity skip a real launch
    would have produced, so the shadow log is a faithful comparison source."""

    root, store, supervisor = _fixture(tmp_path)
    config = _observe_config()
    config["capacity"]["kimi_max"] = 1
    launcher = Launcher(supervisor, store, queue_config=config)

    high = store.create(_base_entry(trio="kimi", priority=10, workspace=str(tmp_path / "w1")))
    low = store.create(_base_entry(trio="kimi", priority=1, workspace=str(tmp_path / "w2")))
    asyncio.run(launcher.tick())

    lines = _shadow_lines(root)
    types = [(line["payload"]["queue_id"], line["type"]) for line in lines]
    assert (high.queue_id, "queue.shadow_launch") in types
    assert (low.queue_id, "queue.shadow_skip") in types
    assert supervisor.created == []
    for entry in (high, low):
        updated = store.get(entry.queue_id)
        assert updated is not None
        assert updated.status == "pending"
        # Observe mode records the decision, not a skip: skip_reasons must
        # stay empty so promotion (observe -> false) starts from a clean entry.
        assert updated.skip_reasons == []


def test_observe_false_writes_no_shadow_log(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    assert config["observe"] is False
    launcher = Launcher(supervisor, store, queue_config=config)

    entry = store.create(_base_entry(trio="kimi", workspace=str(tmp_path / "ws")))
    asyncio.run(launcher.tick())

    assert not (root / "queue" / "shadow.jsonl").exists()
    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "launched"
    assert "queue.shadow_launch" not in _service_types(root)
    assert "queue.shadow_skip" not in _service_types(root)


def test_shadow_log_rotates_daily(tmp_path: Path) -> None:
    import calendar
    import os
    import time

    root, store, _supervisor = _fixture(tmp_path)
    config = _observe_config()
    config["capacity"]["kimi_max"] = 0

    # Simulate yesterday's log: append a record, then backdate the file to
    # yesterday 00:00 UTC.
    yesterday = time.gmtime(time.time() - 86_400)
    yesterday_midnight = calendar.timegm((yesterday.tm_year, yesterday.tm_mon, yesterday.tm_mday, 0, 0, 0, 0, 0, 0))
    yesterday_stamp = time.strftime("%Y%m%d", yesterday)
    store.append_shadow_record({"schema_version": 2, "type": "queue.shadow_launch", "ts": 1.0, "payload": {}})
    log_path = root / "queue" / "shadow.jsonl"
    os.utime(log_path, (yesterday_midnight, yesterday_midnight))

    # A rotate call with today should move yesterday's file aside.
    today = time.strftime("%Y%m%d", time.gmtime())
    store._rotate_shadow_log(today)
    rotated = root / "queue" / f"shadow-{yesterday_stamp}.jsonl"
    assert rotated.is_file()
    assert "queue.shadow_launch" in rotated.read_text(encoding="utf-8")

    launcher = Launcher(_supervisor, store, queue_config=config)
    entry = store.create(_base_entry(trio="kimi"))
    asyncio.run(launcher.tick())
    fresh = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(fresh) == 1
    assert fresh[0]["type"] == "queue.shadow_skip"
    # Both days are readable through the store reader (fleet window source).
    records = store.read_shadow_records(since=0.0)
    assert {r["type"] for r in records} == {"queue.shadow_launch", "queue.shadow_skip"}


def test_shadow_reader_filters_by_since(tmp_path: Path) -> None:
    root, store, _supervisor = _fixture(tmp_path)
    store.append_shadow_record({"schema_version": 2, "type": "queue.shadow_launch", "ts": 100.0, "payload": {"queue_id": "q-old"}})
    store.append_shadow_record({"schema_version": 2, "type": "queue.shadow_skip", "ts": 200.0, "payload": {"queue_id": "q-new"}})
    records = store.read_shadow_records(since=150.0)
    assert [r["payload"]["queue_id"] for r in records] == ["q-new"]


def test_api_queue_shadow_endpoint(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from lh_harness.webapi.server import create_app

    root, store, _supervisor = _fixture(tmp_path)
    store.append_shadow_record(
        {
            "schema_version": 2,
            "type": "queue.shadow_launch",
            "ts": 500.0,
            "payload": {"queue_id": "q-1", "trio": "kimi", "workspace": "./w"},
        }
    )
    app = create_app(runs_root=root)
    client = TestClient(app)

    response = client.get("/api/queue/shadow")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["count"] == 1
    assert data["events"][0]["payload"]["queue_id"] == "q-1"

    # Epoch-seconds and ISO-8601 windows both filter; future windows are empty.
    assert client.get("/api/queue/shadow", params={"since": "400"}).json()["count"] == 1
    assert client.get("/api/queue/shadow", params={"since": "600"}).json()["count"] == 0
    iso = client.get("/api/queue/shadow", params={"since": "1970-01-01T00:08:20Z"})
    assert iso.status_code == 200
    assert iso.json()["count"] == 1
    bad = client.get("/api/queue/shadow", params={"since": "not-a-time"})
    assert bad.status_code == 422