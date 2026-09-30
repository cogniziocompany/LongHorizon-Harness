"""Visionary intake (deliverable 1): spec enqueue -> spec_pending + Mark ready.

Ported by hand from the source branch.  Deliverable 1 is intentionally narrower
than the source spec-staging feature: it adds the ``spec_pending`` status and a
human-gated "Mark ready" promotion, and optional ``spec`` / ``spec_file`` /
``spec_status`` fields.  It does NOT port the token-measurement / ``spec_stats``
machinery (that is a separate, out-of-scope task), so the tests below assert only
what this deliverable provides.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.launcher import Launcher
from lh_harness.queue import QueueStore, default_queue_config
from lh_harness.webapi.server import create_app


def _spec(status: str, body: str = "## Intent\nShip the thing.\n\n## Verification\n- run tests\n") -> str:
    return f"---\ntitle: t\nstatus: {status}\ncontext: []\n---\n{body}"


def _base_entry(**overrides) -> dict:
    body = {
        "name": "spec task",
        "task": "do the thing",
        "workspace": "./workspace",
        "trio": "kimi",
        "requested_by": "ci",
    }
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def _no_exact_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_spec_file_creates_spec_pending(tmp_path) -> None:
    store = QueueStore(tmp_path / "runs")
    spec = tmp_path / "t.spec.md"
    spec.write_text(_spec("draft"), encoding="utf-8")
    entry = store.create(_base_entry(spec_file=str(spec)))
    assert entry.status == "spec_pending"
    assert entry.spec_file == str(spec)
    assert entry.spec is not None
    assert entry.spec_status is None
    # spec fields persist through a re-read from disk.
    reloaded = store.get(entry.queue_id)
    assert reloaded.status == "spec_pending"
    assert reloaded.spec_file == str(spec)
    assert reloaded.spec is not None
    assert store.counts()["spec_pending"] == 1
    assert store.counts()["pending"] == 0


def test_spec_status_draft_creates_spec_pending(tmp_path) -> None:
    store = QueueStore(tmp_path / "runs")
    entry = store.create(_base_entry(spec_status="draft"))
    assert entry.status == "spec_pending"
    assert entry.spec_status == "draft"


def test_spec_status_ready_for_dev_enqueues_as_pending(tmp_path) -> None:
    """A spec already flagged ready-for-dev launches today."""
    store = QueueStore(tmp_path / "runs")
    entry = store.create(_base_entry(spec_status="ready-for-dev"))
    assert entry.status == "pending"
    assert entry.spec_status == "ready-for-dev"


def test_spec_inline_text_creates_spec_pending(tmp_path) -> None:
    store = QueueStore(tmp_path / "runs")
    entry = store.create(_base_entry(spec="## draft spec\nhello"))
    assert entry.status == "spec_pending"
    assert entry.spec == "## draft spec\nhello"
    assert entry.spec_file is None


def test_enqueue_without_spec_is_unchanged(tmp_path) -> None:
    """Backward compatibility: no spec field -> pending, counts unchanged."""
    store = QueueStore(tmp_path / "runs")
    entry = store.create(_base_entry())
    assert entry.status == "pending"
    assert entry.spec is None
    assert entry.spec_file is None
    assert entry.spec_status is None
    assert entry.spec_ready_at is None
    counts = store.counts()
    assert counts["pending"] == 1
    assert counts["spec_pending"] == 0


def test_mark_spec_ready_rejects_non_spec_entry(tmp_path) -> None:
    store = QueueStore(tmp_path / "runs")
    entry = store.create(_base_entry())
    assert entry.status == "pending"
    # A plain pending entry is not spec_pending -- the transition is refused.
    with pytest.raises(ValueError, match="spec_pending"):
        store.mark_spec_ready(entry.queue_id)


def test_mark_spec_ready_rejects_already_promoted(tmp_path) -> None:
    store = QueueStore(tmp_path / "runs")
    spec = tmp_path / "t.spec.md"
    spec.write_text(_spec("draft"), encoding="utf-8")
    entry = store.create(_base_entry(spec_file=str(spec)))
    store.mark_spec_ready(entry.queue_id, requested_by="alice")
    with pytest.raises(ValueError, match="spec_pending"):
        store.mark_spec_ready(entry.queue_id)


def test_mark_spec_ready_rejects_missing_file(tmp_path) -> None:
    store = QueueStore(tmp_path / "runs")
    entry = store.create(_base_entry(spec_file=str(tmp_path / "missing.spec.md")))
    assert entry.status == "spec_pending"
    with pytest.raises(ValueError, match="does not exist"):
        store.mark_spec_ready(entry.queue_id)


def test_mark_spec_ready_promotes_and_records(tmp_path) -> None:
    store = QueueStore(tmp_path / "runs")
    spec = tmp_path / "t.spec.md"
    spec.write_text(_spec("draft"), encoding="utf-8")
    entry = store.create(_base_entry(spec_file=str(spec)))
    updated = store.mark_spec_ready(entry.queue_id, requested_by="alice")
    assert updated is not None
    assert updated.status == "pending"
    assert updated.spec_ready_at is not None
    assert store.counts()["pending"] == 1
    assert store.counts()["spec_pending"] == 0


def test_mark_spec_ready_missing_entry_returns_none(tmp_path) -> None:
    store = QueueStore(tmp_path / "runs")
    assert store.mark_spec_ready("q-nope") is None


def test_launcher_never_launches_spec_pending(tmp_path) -> None:
    import asyncio

    root = tmp_path / "runs"
    store = QueueStore(root)
    supervisor = FakeSupervisor(root)
    spec = tmp_path / "t.spec.md"
    spec.write_text(_spec("draft"), encoding="utf-8")
    entry = store.create(_base_entry(spec_file=str(spec)))
    launcher = Launcher(supervisor, store, queue_config=default_queue_config())
    asyncio.run(launcher.tick())
    asyncio.run(launcher.tick())
    assert supervisor.created == []
    # Not launched, and not counted against capacity.
    assert store.get(entry.queue_id).status == "spec_pending"


def test_read_spec_returns_spec_fields(tmp_path) -> None:
    store = QueueStore(tmp_path / "runs")
    spec = tmp_path / "t.spec.md"
    spec.write_text(_spec("draft"), encoding="utf-8")
    entry = store.create(_base_entry(spec_file=str(spec), spec_status="draft"))
    data = store.read_spec(entry.queue_id)
    assert data is not None
    assert data["queue_id"] == entry.queue_id
    assert data["spec_file"] == str(spec)
    assert data["spec_status"] == "draft"
    assert data["spec"] is not None


def test_read_spec_returns_none_for_plain_entry(tmp_path) -> None:
    store = QueueStore(tmp_path / "runs")
    entry = store.create(_base_entry())
    assert store.read_spec(entry.queue_id) is None


def test_api_spec_enqueue_is_spec_pending(tmp_path) -> None:
    """The enqueue path parks a spec-bearing entry as spec_pending."""
    root = tmp_path / "runs"
    root.mkdir()
    spec = tmp_path / "t.spec.md"
    spec.write_text(_spec("draft"), encoding="utf-8")
    app = create_app(runs_root=root)
    client = TestClient(app)

    created = client.post("/api/queue", json=_base_entry(spec_file=str(spec)))
    assert created.status_code == 200, created.text
    queue_id = created.json()["queue_id"]

    listing = client.get("/api/queue").json()
    assert any(item["queue_id"] == queue_id for item in listing["entries"])
    assert listing["counts"]["spec_pending"] == 1

    # No spec-bearing entry landed as a launchable pending entry.
    assert client.get("/api/queue?status=pending").json()["counts"]["pending"] == 0


def test_api_enqueue_without_spec_unchanged(tmp_path) -> None:
    """Backward compatibility: no spec field -> pending, counts unchanged."""
    root = tmp_path / "runs"
    root.mkdir()
    app = create_app(runs_root=root)
    client = TestClient(app)
    created = client.post(
        "/api/queue",
        json={"name": "x", "task": "t", "workspace": "w", "trio": "kimi", "requested_by": "ci"},
    )
    assert created.status_code == 200
    assert created.json()["queue_id"].startswith("q-")
    assert client.get("/api/queue").json()["counts"]["spec_pending"] == 0
    assert client.get("/api/queue").json()["counts"]["pending"] == 1


class FakeSupervisor:
    """In-memory supervisor stand-in that records create_run calls."""

    def __init__(self, runs_root) -> None:
        self.runs_root = runs_root
        self._runs: dict[str, dict] = {}
        self._owners: dict[str, dict] = {}
        self._statuses: dict[str, dict] = {}
        self.created: list[dict] = []

    def add_run(self, run_id: str, workspace: str, *, status: str = "running", trio: str | None = None) -> None:
        self._runs[run_id] = {
            "id": run_id,
            "workspace": str(workspace),
            "trio": trio,
        }
        self._owners[run_id] = {
            "run_id": run_id,
            "workspace": str(workspace),
            "task": "existing task",
            "agent": "codex",
        }
        self._statuses[run_id] = {"status": status, "run_id": run_id}

    def list_run_items(self) -> list[dict]:
        return list(self._runs.values())

    def owner(self, run_id: str) -> dict:
        return self._owners.get(run_id, {})

    def status(self, run_id: str) -> dict:
        return self._statuses.get(run_id, {"status": "idle", "run_id": run_id})

    def _run_logs_dir(self, run_id: str):
        return self.runs_root / run_id / "lh_harness"

    def create_run(self, **kwargs) -> dict:
        run_id = kwargs.get("run_id") or f"run-{__import__('uuid').uuid4().hex[:8]}"
        workspace = str(kwargs.get("workspace", "."))
        agent = kwargs.get("agent", "codex")
        self.add_run(run_id, workspace, status="creating", trio=agent)
        self._owners[run_id].update(
            {
                "task": kwargs.get("task", ""),
                "agent": agent,
                "model": kwargs.get("model"),
                "role_configs": kwargs.get("role_configs"),
                "max_rounds": kwargs.get("max_rounds"),
                "mcp_profile": kwargs.get("mcp_profile"),
            }
        )
        result = {
            "id": run_id,
            "task": kwargs.get("task", ""),
            "status": "creating",
            "log_dir": str(self._run_logs_dir(run_id)),
            "owner": self._owners[run_id],
        }
        self.created.append(result)
        return result
