from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.queue import QueueStore, default_queue_config, queue_config_from_config
from lh_harness.webapi.server import create_app


def _fixture(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    return root


def test_queue_store_creates_entry(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
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
    assert (root / "queue" / f"{entry.queue_id}.json").is_file()


def test_queue_store_validates_required_fields(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    with pytest.raises(ValueError, match="task must be a string"):
        store.create({"name": "x", "workspace": "w", "trio": "kimi", "requested_by": "ci"})
    with pytest.raises(ValueError, match="name is required"):
        store.create({"task": "x", "workspace": "w", "trio": "kimi", "requested_by": "ci"})
    with pytest.raises(ValueError, match="workspace is required"):
        store.create({"name": "x", "task": "x", "trio": "kimi", "requested_by": "ci"})
    with pytest.raises(ValueError, match="requested_by is required"):
        store.create({"name": "x", "task": "x", "workspace": "w", "trio": "kimi"})


def test_queue_store_accepts_task_file(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    task_path = tmp_path / "task.md"
    task_path.write_text("file task", encoding="utf-8")
    store = QueueStore(root)
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


def test_queue_store_rejects_both_task_and_task_file(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
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


def test_queue_store_lists_and_sorts(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    low = store.create(
        {"name": "low", "task": "t", "workspace": "w", "trio": "kimi", "priority": 1, "requested_by": "ci"}
    )
    high = store.create(
        {"name": "high", "task": "t", "workspace": "w", "trio": "kimi", "priority": 10, "requested_by": "ci"}
    )
    items = store.list()
    assert [item.queue_id for item in items] == [high.queue_id, low.queue_id]


def test_queue_store_priority_update(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    entry = store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 1, "requested_by": "ci"}
    )
    updated = store.set_priority(entry.queue_id, 42)
    assert updated is not None
    assert updated.priority == 42


def test_queue_store_delete_pending(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    entry = store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 0, "requested_by": "ci"}
    )
    removed = store.delete(entry.queue_id)
    assert removed is not None
    assert store.get(entry.queue_id) is None


def test_queue_store_mark_launched_and_done(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
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


def test_api_queue_create_requires_auth(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    app = create_app(runs_root=root, auth_token="secret")
    client = TestClient(app)
    payload = {
        "name": "x",
        "task": "t",
        "workspace": "w",
        "trio": "kimi",
        "priority": 0,
        "requested_by": "ci",
    }
    assert client.post("/api/queue", json=payload).status_code == 401
    response = client.post("/api/queue", json=payload, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert response.json()["queue_id"].startswith("q-")


def test_api_queue_list_and_delete(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    app = create_app(runs_root=root)
    client = TestClient(app)
    created = client.post(
        "/api/queue",
        json={
            "name": "list-me",
            "task": "t",
            "workspace": "w",
            "trio": "kimi",
            "priority": 5,
            "requested_by": "ci",
        },
    )
    queue_id = created.json()["queue_id"]
    listed = client.get("/api/queue").json()
    assert any(item["queue_id"] == queue_id for item in listed["entries"])
    assert listed["counts"]["pending"] == 1

    deleted = client.delete(f"/api/queue/{queue_id}")
    assert deleted.status_code == 200
    assert client.get("/api/queue").json()["counts"]["pending"] == 0


def test_api_queue_delete_rejects_non_pending(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    entry = store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 0, "requested_by": "ci"}
    )
    store.mark_launched(entry.queue_id, "run-1")
    app = create_app(runs_root=root)
    client = TestClient(app)
    assert client.delete(f"/api/queue/{entry.queue_id}").status_code == 409


def test_api_queue_priority_update(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    app = create_app(runs_root=root)
    client = TestClient(app)
    created = client.post(
        "/api/queue",
        json={
            "name": "prio",
            "task": "t",
            "workspace": "w",
            "trio": "kimi",
            "priority": 1,
            "requested_by": "ci",
        },
    ).json()
    updated = client.post(f"/api/queue/{created['queue_id']}/priority", json={"priority": 99}).json()
    assert updated["priority"] == 99


def test_api_queue_priority_rejects_non_pending(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    entry = store.create(
        {"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "priority": 0, "requested_by": "ci"}
    )
    store.mark_launched(entry.queue_id, "run-1")
    app = create_app(runs_root=root)
    client = TestClient(app)
    assert client.post(f"/api/queue/{entry.queue_id}/priority", json={"priority": 5}).status_code == 409


def test_api_queue_validation_rejects_bad_trio(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    app = create_app(runs_root=root)
    client = TestClient(app)
    response = client.post(
        "/api/queue",
        json={
            "name": "x",
            "task": "t",
            "workspace": "w",
            "trio": "unknown",
            "priority": 0,
            "requested_by": "ci",
        },
    )
    assert response.status_code == 422


def test_api_queue_status_filter(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    store = QueueStore(root)
    pending = store.create(
        {"name": "p", "task": "t", "workspace": "w", "trio": "kimi", "priority": 0, "requested_by": "ci"}
    )
    launched = store.create(
        {"name": "l", "task": "t", "workspace": "w", "trio": "kimi", "priority": 0, "requested_by": "ci"}
    )
    store.mark_launched(launched.queue_id, "run-1")
    app = create_app(runs_root=root)
    client = TestClient(app)
    assert len(client.get("/api/queue?status=pending").json()["entries"]) == 1
    assert len(client.get("/api/queue?status=launched").json()["entries"]) == 1
    assert client.get("/api/queue?status=bad").status_code == 422


def test_default_queue_config_shape() -> None:
    config = default_queue_config()
    assert set(config) == {"trios", "capacity"}
    assert set(config["trios"]) == {"kimi", "qwen"}
    assert config["capacity"]["kimi_max"] == 3
    assert config["capacity"]["qwen_max"] == 1
    assert config["capacity"]["min_healthy_keys"] == 2


def test_queue_config_from_project_config() -> None:
    project = {
        "queue": {
            "trios": {
                "kimi": {"agent": "claude_code", "model": "kimi-test", "mcp_profile": "ops"},
            },
            "capacity": {"kimi_max": 2, "qwen_max": 0, "min_healthy_keys": 1, "key_health_url": "http://health", "poll_seconds": 5},
        }
    }
    config = queue_config_from_config(project)
    assert config["trios"]["kimi"]["agent"] == "claude_code"
    assert config["trios"]["kimi"]["model"] == "kimi-test"
    assert config["trios"]["qwen"]["agent"] == "codex"
    assert config["capacity"]["kimi_max"] == 2
    assert config["capacity"]["qwen_max"] == 0
    assert config["capacity"]["poll_seconds"] == 5


def test_api_queue_config_endpoint(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    app = create_app(runs_root=root)
    client = TestClient(app)
    response = client.get("/api/queue/config")
    assert response.status_code == 200
    data = response.json()
    assert data["capacity"]["kimi_max"] == 3


def test_api_queue_requires_json_content_type(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    app = create_app(runs_root=root)
    client = TestClient(app)
    response = client.post("/api/queue", data='{"name":"x"}', headers={"Content-Type": "text/plain"})
    assert response.status_code == 415
