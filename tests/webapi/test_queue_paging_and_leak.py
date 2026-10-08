"""2026-10-07 CT110 web-process leak fix and /api/queue paging (opt-in).

Covers: the legacy shape is unchanged when no new parameter is passed; view=summary
drops the task text; limit/after paging with next_cursor; status (comma list), name,
trio and since filters; groups=ids|0|full; GET /api/queue/{id} (and that it does not
shadow /api/queue/config or /api/queue/shadow); the snapshot cache and the state
registry stay bounded.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.webapi import server as server_mod
from lh_harness.webapi.server import create_app


def _client(tmp_path: Path) -> TestClient:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    return TestClient(create_app(runs_root=root))


def _enqueue(client: TestClient, name: str, *, trio: str = "kimi", task: str = "t") -> str:
    resp = client.post(
        "/api/queue",
        json={"name": name, "task": task, "workspace": "w", "trio": trio, "priority": 5, "requested_by": "ci"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["queue_id"]


def test_legacy_shape_unchanged_without_new_params(tmp_path: Path) -> None:
    client = _client(tmp_path)
    qid = _enqueue(client, "legacy", task="full task text")
    body = client.get("/api/queue").json()
    assert set(body) == {"entries", "groups", "counts"}
    entry = next(e for e in body["entries"] if e["queue_id"] == qid)
    assert entry["task"] == "full task text"
    assert body["groups"]["pending"][0]["task"] == "full task text"


def test_summary_view_drops_task_text(tmp_path: Path) -> None:
    client = _client(tmp_path)
    long_task = "x" * 5000
    _enqueue(client, "big", task=long_task)
    body = client.get("/api/queue", params={"view": "summary"}).json()
    entry = body["entries"][0]
    assert "task" not in entry
    assert entry["task_preview"] == long_task[:200]
    assert entry["task_bytes"] == 5000
    assert "groups" not in body  # opt-in only
    assert body["view"] == "summary"


def test_paging_newest_first_with_cursor(tmp_path: Path) -> None:
    client = _client(tmp_path)
    ids = []
    for i in range(5):
        ids.append(_enqueue(client, f"page-{i}"))
        time.sleep(0.01)
    first = client.get("/api/queue", params={"view": "summary", "limit": 2}).json()
    assert [e["name"] for e in first["entries"]] == ["page-4", "page-3"]
    assert first["total"] == 5 and first["next_cursor"]
    second = client.get("/api/queue", params={"view": "summary", "limit": 2, "after": first["next_cursor"]}).json()
    assert [e["name"] for e in second["entries"]] == ["page-2", "page-1"]
    third = client.get("/api/queue", params={"view": "summary", "limit": 2, "after": second["next_cursor"]}).json()
    assert [e["name"] for e in third["entries"]] == ["page-0"]
    assert third["next_cursor"] is None


def test_filters_apply_before_serialising(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _enqueue(client, "alpha-kimi", trio="kimi")
    _enqueue(client, "beta-qwen", trio="qwen")
    by_trio = client.get("/api/queue", params={"view": "summary", "trio": "qwen"}).json()
    assert [e["name"] for e in by_trio["entries"]] == ["beta-qwen"]
    by_name = client.get("/api/queue", params={"view": "summary", "name": "ALPHA"}).json()
    assert [e["name"] for e in by_name["entries"]] == ["alpha-kimi"]
    multi = client.get("/api/queue", params={"view": "summary", "status": "pending,done"}).json()
    assert multi["total"] == 2
    future = client.get("/api/queue", params={"view": "summary", "since": time.time() + 3600}).json()
    assert future["entries"] == []
    assert client.get("/api/queue", params={"status": "pending,bogus"}).status_code == 422
    assert client.get("/api/queue", params={"view": "huge"}).status_code == 422
    assert client.get("/api/queue", params={"view": "summary", "after": "garbage"}).status_code == 422


def test_groups_variants(tmp_path: Path) -> None:
    client = _client(tmp_path)
    qid = _enqueue(client, "grouped")
    ids = client.get("/api/queue", params={"view": "summary", "groups": "ids"}).json()
    assert ids["groups"]["pending"] == [qid]
    full = client.get("/api/queue", params={"view": "summary", "groups": "full"}).json()
    assert full["groups"]["pending"][0]["queue_id"] == qid and "task" not in full["groups"]["pending"][0]
    none = client.get("/api/queue", params={"view": "summary", "groups": "0"}).json()
    assert "groups" not in none


def test_get_one_entry_and_no_route_shadowing(tmp_path: Path) -> None:
    client = _client(tmp_path)
    qid = _enqueue(client, "one", task="the whole task")
    one = client.get(f"/api/queue/{qid}")
    assert one.status_code == 200 and one.json()["task"] == "the whole task"
    assert client.get("/api/queue/q-ffffffffffff").status_code == 404
    assert client.get("/api/queue/not-an-id").status_code == 404
    # The static routes declared before the {queue_id} route must still answer.
    assert client.get("/api/queue/config").status_code != 404
    assert client.get("/api/queue/shadow").status_code != 404


def test_snapshot_cache_is_bounded_and_drops_expired() -> None:
    cache = server_mod._SnapshotCache(ttl_seconds=0.05, max_entries=3)
    sig = (0.0, 0, 0.0, "")
    for i in range(10):
        cache.put(f"run-{i}", sig, {"i": i})
    assert len(cache) == 3
    time.sleep(0.08)
    cache.put("fresh", sig, {"i": "fresh"})
    assert len(cache) == 1  # every expired entry was dropped on write


def test_state_registry_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_mod, "_STATE_REGISTRY_MAX", 4)
    root = tmp_path / "runs"
    for i in range(10):
        (root / f"20261007T00000{i}Z_aaaaaaa{i}" / "lh_harness").mkdir(parents=True)
    from lh_harness.dashboard.state import DashboardState

    base = DashboardState(tmp_path / "base", runs_root=root, control_enabled=False)
    registry = server_mod.StateRegistry(state=base, runs_root=root, run_id="base")
    opened = [registry.state_for(f"20261007T00000{i}Z_aaaaaaa{i}") for i in range(10)]
    assert any(state is not None for state in opened)
    assert len(registry._states) <= 4
    assert "base" in registry._states  # the base run is pinned
