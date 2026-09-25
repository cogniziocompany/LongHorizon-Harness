"""Queue drain flag — task 242.

``POST /api/queue/drain {"enabled": true, "reason": ...}`` stops NEW queue
launches while live runs finish (a deploy must find a zero-active window
with a backlog in place); ``{"enabled": false}`` resumes.  The flag lives in
``runs_root/queue/drain.json`` (QueueStore, same persistence mechanism as
the entries) so it survives a service restart, and its state is surfaced in
``GET /api/meta`` under ``drain``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.launcher import Launcher
from lh_harness.queue import QueueStore, default_queue_config
from lh_harness.webapi.server import create_app

from .test_launcher import FakeSupervisor, _base_entry


def _fixture(tmp_path: Path) -> tuple[Path, QueueStore, FakeSupervisor]:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = FakeSupervisor(root)
    return root, store, supervisor


def _launcher_config() -> dict[str, Any]:
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 3
    return config


def _service_types(root: Path) -> list[str]:
    path = root / "queue" / "service_events.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line).get("type")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Store persistence (restart-survival) — the flag is a file, so a fresh
# QueueStore over the same runs root reads the state a dead process wrote.
# ---------------------------------------------------------------------------


def test_drain_flag_persists_across_store_restart(tmp_path: Path) -> None:
    root, store, _supervisor = _fixture(tmp_path)
    assert store.get_drain() == {"enabled": False, "reason": None, "since": None}

    first = store.set_drain(True, reason="deploy task-228")
    assert first["enabled"] is True
    assert first["reason"] == "deploy task-228"
    assert isinstance(first["since"], float)

    # The "restart": a brand-new store instance over the same runs root, the
    # way the service reopens the queue directory after a deploy.
    restarted = QueueStore(root)
    assert restarted.get_drain() == first

    # Re-enabling an already-drained queue updates the reason but does not
    # re-age the maintenance window.
    re_enabled = restarted.set_drain(True, reason="still deploying")
    assert re_enabled["since"] == first["since"]
    assert re_enabled["reason"] == "still deploying"

    cleared = restarted.set_drain(False)
    assert cleared == {"enabled": False, "reason": None, "since": None}
    assert QueueStore(root).get_drain() == cleared


def test_drain_flag_file_is_not_read_as_a_queue_entry(tmp_path: Path) -> None:
    root, store, _supervisor = _fixture(tmp_path)
    store.set_drain(True, reason="deploy")
    restarted = QueueStore(root)
    # drain.json lives in the queue directory beside the q-*.json entries;
    # list()/counts() must skip it, not crash on the foreign shape.
    assert all(entry.queue_id.startswith("q-") for entry in restarted.list())
    assert restarted.counts()["pending"] == 0


# ---------------------------------------------------------------------------
# Launcher gate — a drained queue launches nothing; live runs are unaffected.
# ---------------------------------------------------------------------------


def test_drained_queue_launches_nothing(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = _launcher_config()
    launcher = Launcher(supervisor, store, queue_config=config)

    high = store.create(_base_entry(trio="kimi", priority=10, workspace="./w1"))
    low = store.create(_base_entry(trio="kimi", priority=5, workspace="./w2"))

    store.set_drain(True, reason="deploy maintenance window")
    asyncio.run(launcher.tick())

    for entry in (high, low):
        updated = store.get(entry.queue_id)
        assert updated is not None
        # Pending, not failed/blocked: the backlog survives the deploy.
        assert updated.status == "pending"
        assert any("queue drained" in reason for reason in updated.skip_reasons)
    assert supervisor.created == []

    # The skip is a real launcher decision, visible in the service log.
    path = root / "queue" / "service_events.jsonl"
    assert path.is_file()
    assert any("queue.skipped" in line for line in path.read_text().splitlines())

    # Clearing the drain resumes launching on the next pass.
    store.set_drain(False)
    asyncio.run(launcher.tick())
    high_updated = store.get(high.queue_id)
    assert high_updated is not None
    assert high_updated.status == "launched"
    assert high_updated.run_id is not None
    assert len(supervisor.created) == 1


def test_drain_gate_sits_at_the_eligibility_point(tmp_path: Path) -> None:
    """The drain reason is returned from _check_eligibility, not _run_pass.

    Same short-circuit shape as shadow mode: the pass runs, every pending
    entry is skipped with the drain reason, and nothing reaches _launch.
    """

    root, store, supervisor = _fixture(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=_launcher_config())
    entry = store.create(_base_entry(trio="kimi"))

    store.set_drain(True)
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert any(reason == "queue drained" for reason in updated.skip_reasons)
    assert supervisor.created == []


def test_drained_queue_leaves_live_runs_unaffected(tmp_path: Path) -> None:
    """While drained, a launched entry with an active run is untouched, and
    the normal reconciliation still promotes it when the run goes terminal.
    """

    root, store, supervisor = _fixture(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=_launcher_config())

    live = store.create(_base_entry(trio="kimi", workspace=str(tmp_path / "live")))
    launched = store.mark_launched(live.queue_id, "run-live")
    assert launched is not None and launched.status == "launched"
    supervisor.add_run("run-live", str(tmp_path / "live"), status="running", trio="kimi")

    store.set_drain(True, reason="deploy")
    asyncio.run(launcher.tick())

    live_updated = store.get(live.queue_id)
    assert live_updated is not None
    # The drain never touches launched entries: no skip, no cancellation.
    assert live_updated.status == "launched"
    assert live_updated.run_id == "run-live"

    # Reconciliation keeps working under drain: the run completes, the entry
    # is promoted to done by the same pass that skips pending entries.
    supervisor._statuses["run-live"] = {"status": "completed", "run_id": "run-live"}
    asyncio.run(launcher.tick())
    promoted = store.get(live.queue_id)
    assert promoted is not None
    assert promoted.status == "done"


# ---------------------------------------------------------------------------
# HTTP surface — POST /api/queue/drain, GET /api/meta drain field, bearer
# guard (the codebase's operator boundary, test_hardening.py style).
# ---------------------------------------------------------------------------


def test_api_queue_drain_requires_bearer_token(tmp_path: Path) -> None:
    root, _store, _supervisor = _fixture(tmp_path)
    client = TestClient(create_app(runs_root=root, auth_token="secret"))
    body = {"enabled": True, "reason": "deploy"}
    assert client.post("/api/queue/drain", json=body).status_code == 401
    wrong = client.post(
        "/api/queue/drain", json=body, headers={"Authorization": "Bearer nope"}
    )
    assert wrong.status_code == 401
    response = client.post(
        "/api/queue/drain", json=body, headers={"Authorization": "Bearer secret"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["drain"]["enabled"] is True
    # GET /api/meta is guarded by the same boundary.
    assert client.get("/api/meta").status_code == 401


def test_api_queue_drain_set_clear_and_meta(tmp_path: Path) -> None:
    root, store, _supervisor = _fixture(tmp_path)
    client = TestClient(create_app(runs_root=root))
    assert client.post("/api/queue/drain", json={}).status_code == 422
    assert client.post("/api/queue/drain", json={"enabled": True}).status_code == 422
    assert client.post(
        "/api/queue/drain", json={"enabled": True, "reason": 7}
    ).status_code == 422

    enabled = client.post(
        "/api/queue/drain", json={"enabled": True, "reason": "deploy 228"}
    )
    assert enabled.status_code == 200
    assert enabled.json()["drain"]["enabled"] is True
    assert enabled.json()["drain"]["reason"] == "deploy 228"
    assert store.get_drain()["enabled"] is True

    meta = client.get("/api/meta").json()
    assert meta["drain"]["enabled"] is True
    assert meta["drain"]["reason"] == "deploy 228"
    assert isinstance(meta["drain"]["since"], float)

    disabled = client.post("/api/queue/drain", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["drain"]["enabled"] is False
    assert client.get("/api/meta").json()["drain"]["enabled"] is False
    assert store.get_drain() == {"enabled": False, "reason": None, "since": None}


def test_api_meta_reports_not_drained_by_default(tmp_path: Path) -> None:
    root, _store, _supervisor = _fixture(tmp_path)
    client = TestClient(create_app(runs_root=root))
    meta = client.get("/api/meta").json()
    assert meta["drain"] == {"enabled": False, "reason": None, "since": None}


def test_api_meta_drain_survives_service_restart(tmp_path: Path) -> None:
    """The restart path: enable via the API, rebuild the app (what a service
    restart does to the queue directory), and the new process must come back
    up still drained until an operator clears it.
    """

    root, _store, _supervisor = _fixture(tmp_path)
    first = TestClient(create_app(runs_root=root, auth_token="secret"))
    response = first.post(
        "/api/queue/drain",
        json={"enabled": True, "reason": "deploy 228"},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200

    # The restart: a fresh create_app over the same runs root.
    second = TestClient(create_app(runs_root=root, auth_token="secret"))
    meta = second.get(
        "/api/meta", headers={"Authorization": "Bearer secret"}
    ).json()
    assert meta["drain"]["enabled"] is True
    assert meta["drain"]["reason"] == "deploy 228"

    cleared = second.post(
        "/api/queue/drain",
        json={"enabled": False},
        headers={"Authorization": "Bearer secret"},
    )
    assert cleared.status_code == 200
    assert (
        second.get("/api/meta", headers={"Authorization": "Bearer secret"})
        .json()["drain"]["enabled"]
        is False
    )