from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.launcher import Launcher
from lh_harness.queue import QueueStore, default_queue_config
from lh_harness.webapi.server import create_app

from .test_launcher import FakeSupervisor


def _spec(status: str, body: str = "## Intent\nShip the thing.\n\n## Verification\n- run tests\n") -> str:
    return f"---\ntitle: t\nstatus: {status}\ncontext: []\n---\n{body}"


def _entry_body(spec_path: Path, **overrides):
    body = {
        "name": "spec task",
        "spec_file": str(spec_path),
        "workspace": "./workspace",
        "trio": "kimi",
        "requested_by": "ci",
    }
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def _no_exact_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_spec_file_creates_spec_pending_and_measures(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "runs")
    spec = tmp_path / "t.spec.md"
    spec.write_text(_spec("draft"), encoding="utf-8")
    entry = store.create(_entry_body(spec))
    assert entry.status == "spec_pending"
    assert entry.spec_status == "draft"
    assert entry.spec_chars == len(spec.read_text(encoding="utf-8"))
    assert entry.spec_tokens_est and entry.spec_tokens_est > 0
    assert entry.spec_range_state == "unestablished"
    # Round-trips through _read_path (unknown statuses would be dropped).
    assert store.get(entry.queue_id) is not None
    assert store.counts()["spec_pending"] == 1


def test_ready_spec_enqueues_as_pending_with_spec_body_as_task(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "runs")
    spec = tmp_path / "t.spec.md"
    spec.write_text(_spec("ready-for-dev"), encoding="utf-8")
    entry = store.create(_entry_body(spec))
    assert entry.status == "pending"
    assert entry.task.startswith("## Intent")


def test_mark_spec_ready_rejects_draft_and_missing(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "runs")
    spec = tmp_path / "t.spec.md"
    spec.write_text(_spec("draft"), encoding="utf-8")
    entry = store.create(_entry_body(spec, task="original prose"))
    assert entry.task == "original prose"
    with pytest.raises(ValueError, match="ready-for-dev"):
        store.mark_spec_ready(entry.queue_id)
    spec.unlink()
    with pytest.raises(ValueError, match="does not exist"):
        store.mark_spec_ready(entry.queue_id)


def test_mark_spec_ready_promotes_and_records(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "runs")
    spec = tmp_path / "t.spec.md"
    spec.write_text(_spec("draft"), encoding="utf-8")
    entry = store.create(_entry_body(spec, task="original prose"))
    spec.write_text(_spec("ready-for-dev"), encoding="utf-8")
    updated = store.mark_spec_ready(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert updated.task.startswith("## Intent")
    assert updated.spec_status == "ready-for-dev"
    assert store.spec_stats().n == 1
    # Re-marking overwrites the sample rather than duplicating it.
    store.mark_spec_ready(entry.queue_id)
    assert store.spec_stats().n == 1


def test_measure_spec_remeasures_when_file_changes(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "runs")
    spec = tmp_path / "t.spec.md"
    spec.write_text(_spec("draft"), encoding="utf-8")
    entry = store.create(_entry_body(spec))
    before = entry.spec_chars
    spec.write_text(_spec("draft", body="## Intent\n" + "x" * 2000), encoding="utf-8")
    future = entry.spec_measured_at + 10
    os.utime(spec, (future, future))
    updated = store.measure_spec(entry.queue_id)
    assert updated is not None and updated.spec_chars != before


def test_launcher_never_launches_spec_pending(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    store = QueueStore(root)
    supervisor = FakeSupervisor(root)
    spec = tmp_path / "t.spec.md"
    spec.write_text(_spec("draft"), encoding="utf-8")
    entry = store.create(_entry_body(spec))
    launcher = Launcher(supervisor, store, queue_config=default_queue_config())
    launcher._run_pass()
    launcher._run_pass()
    assert supervisor.created == []
    refreshed = store.get(entry.queue_id)
    assert refreshed is not None
    assert refreshed.status == "spec_pending"
    assert refreshed.skip_reasons == ["spec not ready"]
    spec.write_text(_spec("ready-for-dev"), encoding="utf-8")
    store.mark_spec_ready(entry.queue_id)
    launcher._run_pass()
    assert len(supervisor.created) == 1
    assert supervisor.created[0]["task"].startswith("## Intent")


def test_api_spec_routes(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    spec = tmp_path / "t.spec.md"
    spec.write_text(_spec("draft"), encoding="utf-8")
    app = create_app(runs_root=root)
    client = TestClient(app)
    created = client.post("/api/queue", json=_entry_body(spec))
    assert created.status_code == 200, created.text
    queue_id = created.json()["queue_id"]

    listing = client.get("/api/queue").json()
    assert set(listing["groups"]) == {"spec_pending", "pending", "launched", "done", "failed"}
    assert listing["groups"]["spec_pending"][0]["queue_id"] == queue_id
    assert listing["spec_stats"]["established"] is False
    assert listing["entries"][0]["spec_range_state"] == "unestablished"
    assert client.get("/api/queue", params={"status": "spec_pending"}).status_code == 200

    preview = client.get(f"/api/queue/{queue_id}/spec")
    assert preview.status_code == 200
    data = preview.json()
    assert data["frontmatter"]["status"] == "draft"
    assert data["body"].startswith("## Intent")
    assert data["chars"] > 0 and data["tokens_est"] > 0
    assert data["stats"]["n"] == 0

    denied = client.post(f"/api/queue/{queue_id}/spec", json={})
    assert denied.status_code == 409

    spec.write_text(_spec("ready-for-dev"), encoding="utf-8")
    ok = client.post(f"/api/queue/{queue_id}/spec", json={})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "pending"
    assert client.get("/api/queue/spec_stats").json()["n"] == 1

    recorded = client.post("/api/queue/spec_stats/record", json={"key": "file-queue:748", "tokens_est": 1200})
    assert recorded.status_code == 200
    assert recorded.json()["n"] == 2
    assert client.post("/api/queue/spec_stats/record", json={"key": "", "tokens_est": 1}).status_code == 422
    assert client.get("/api/queue/config").json()["spec_stats"] == {"window": 50, "min_samples": 5, "k": 3.0}
    assert client.get("/api/queue/missing/spec").status_code == 404


def test_mcp_spec_tools(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    spec = tmp_path / "t.spec.md"
    spec.write_text(_spec("ready-for-dev"), encoding="utf-8")
    app = create_app(runs_root=root)
    client = TestClient(app)
    names = {tool["name"] for tool in client.get("/api/mcp/fleet/tools").json()["tools"]}
    assert {"harness_get_spec", "harness_mark_spec_ready"} <= names
    queue_id = client.post("/api/queue", json=_entry_body(spec)).json()["queue_id"]
    got = client.post("/api/mcp/fleet/harness_get_spec", json={"arguments": {"queue_id": queue_id}})
    assert got.status_code == 200 and got.json()["ok"] is True
    marked = client.post("/api/mcp/fleet/harness_mark_spec_ready", json={"arguments": {"queue_id": queue_id}})
    assert marked.status_code == 200 and marked.json()["status"] == "pending"
