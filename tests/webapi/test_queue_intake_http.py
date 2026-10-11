"""HTTP-level tests for the form-like queue intake (2026-10-11).

Covers the two deterministic-intake additions on the REST surface:
  * POST /api/queue refuses a non-terminal same-normalized-name duplicate with
    a structured 409 "form" body (field + message), so an agent caller can act
    without guessing.
  * GET /api/queue/stats returns one small stamped payload (counts, by_trio,
    runs, reconciliation, computed_at) the fleet window can show verbatim.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.webapi.server import create_app


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    return root


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(runs_root=_fixture(tmp_path), auth_token="secret", bind_host="testserver")
    )


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def _payload(**overrides) -> dict:
    body = {
        "name": "E2E-QUAR-EXPIRY-1013",
        "task": "triage the quarantine entries",
        "workspace": "./workspace",
        "trio": "kimi",
        "priority": 980,
        "requested_by": "orchestrator",
    }
    body.update(overrides)
    return body


def test_duplicate_name_posts_409_with_form_body(tmp_path: Path) -> None:
    client = _client(tmp_path)
    first = client.post("/api/queue", json=_payload(), headers=_auth())
    assert first.status_code in (200, 201)
    dup = client.post(
        "/api/queue",
        json=_payload(name="  e2e-quar-expiry-1013  "),
        headers=_auth(),
    )
    assert dup.status_code == 409
    detail = dup.json()["detail"]
    assert detail["error"] == "duplicate_queue_entry"
    assert detail["existing_queue_id"]
    assert detail["existing_status"] == "pending"
    form = detail["form"]
    assert form and form[0]["field"] == "name"
    assert "already exists" in form[0]["message"]


def test_reslice_names_are_not_duplicates(tmp_path: Path) -> None:
    client = _client(tmp_path)
    for name in ("OVERSEER-LEDGER-1", "OVERSEER-LEDGER-1a", "OVERSEER-LEDGER-1b"):
        resp = client.post("/api/queue", json=_payload(name=name), headers=_auth())
        assert resp.status_code in (200, 201), resp.text
    assert client.get("/api/queue/stats", headers=_auth()).json()["counts"]["pending"] == 3


def test_queue_stats_shape_and_reconciliation(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.post("/api/queue", json=_payload(trio="kimi"), headers=_auth())
    client.post(
        "/api/queue", json=_payload(name="OTHER-TASK", trio="zai"), headers=_auth()
    )
    stats = client.get("/api/queue/stats", headers=_auth()).json()
    assert stats["counts"]["pending"] == 2
    assert stats["by_trio"]["kimi"]["pending"] == 1
    assert stats["by_trio"]["zai"]["pending"] == 1
    assert "queue_launched_minus_runs_running" in stats["reconciliation"]
    assert isinstance(stats["computed_at"], float)
    # With no launched entries and no live runs, the drift signal is exactly zero.
    assert stats["reconciliation"]["queue_launched_minus_runs_running"] == 0


def test_queue_stats_requires_auth(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/api/queue/stats").status_code in (401, 403)
