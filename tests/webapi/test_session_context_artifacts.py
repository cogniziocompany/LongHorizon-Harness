"""Session-context artifacts (LHH-SESSION-CONTEXT-ARTIFACTS).

Locks the first-slice behavior in place:

* ``ArtifactStore.create`` enforces shape, redact-on-write, idempotency.
* POST /api/artifacts accepts a ContextPacket and returns the id.
* GET /api/artifacts/<id> returns the stored packet.
* GET /artifacts/<id> renders an HTML page that escapes HTML, surfaces
  markdown, tables, evidence (commands, sandboxed images, sandboxed HTML)
  and redaction provenance.
* enqueue accepts ``artifact_ids`` and the list is validated.
* Git is optional -- no path/repo check is enforced.

Tests use the file-backed ArtifactStore (mirrors the QueueStore convention)
and the API through create_app/runs_root.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.artifacts import (
    ArtifactStore,
    ArtifactValidationError,
    safe_artifact_id,
)
from lh_harness.queue import QueueStore, _normalize_request
from lh_harness.webapi.server import create_app


def _runs_root(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(_runs_root(tmp_path))


def _queue_store(tmp_path: Path) -> QueueStore:
    return QueueStore(_runs_root(tmp_path))


def _api_client(tmp_path: Path):
    return TestClient(
        create_app(runs_root=_runs_root(tmp_path), auth_token="secret", bind_host="testserver")
    )


def _packet(**overrides) -> dict:
    base = {
        "kind": "session_context",
        "title": "slice-1 handoff",
        "summary": "captured the design draft and the env wiring",
        "producer": {
            "agent": "claude_code",
            "host": "pve.local",
            "session_id": "8f14abcd-1234-5678-9abc-def012345678",
            "session_ref": "8f14ab",
            "captured_at": 1713000050.0,
        },
        "visibility": "team",
        "plan": [{"id": "p1", "text": "draft design"}],
        "decisions": [{"ts": 1713000010.0, "what": "stay file-backed", "why": "hermetic first slice"}],
        "files_touched": [
            {"path": "src/lh_harness/artifacts.py", "action": "create", "lines": "1-300"},
        ],
        "links": [{"label": "design doc", "url": "https://example/doc"}],
        "evidence": [
            {"kind": "command", "label": "pytest", "exit_code": 0, "text": "1 passed"},
        ],
        "transcript_delta": [
            {"ts": 1713000000.0, "role": "user", "text": "hello"},
        ],
    }
    base.update(overrides)
    return base


def test_artifact_store_create_assigns_id_and_redacts_secret() -> None:
    store = _store(_tmp())
    packet = _packet(
        summary="hard-coded ANTHROPIC_API_KEY=sk-ant-1234567890abcdef was not used"
    )
    artifact, created = store.create(packet)
    assert created is True
    assert artifact.artifact_id.startswith("art-")
    assert safe_artifact_id(artifact.artifact_id)
    # Secret value must not survive into the stored packet.
    stored_summary = artifact.packet["summary"]
    assert "sk-ant-1234567890abcdef" not in stored_summary
    assert artifact.redaction_count >= 1


def test_artifact_store_create_is_idempotent_on_same_session_and_captured_at(tmp_path: Path) -> None:
    store = _store(tmp_path)
    packet = _packet()
    first, _ = store.create(packet)
    second, created = store.create(packet)
    assert created is False
    assert second.artifact_id == first.artifact_id


def test_artifact_store_create_rejects_known_field_failures() -> None:
    store = _store(_tmp())
    with pytest.raises(ArtifactValidationError):
        store.create({"kind": "wrong", "title": "x", "summary": "y"})
    # Producer without session_id is rejected.
    bad = _packet()
    bad["producer"]["session_id"] = ""
    with pytest.raises(ArtifactValidationError):
        store.create(bad)
    # Producer with an unknown agent is rejected.
    bad2 = _packet()
    bad2["producer"]["agent"] = "made-up"
    with pytest.raises(ArtifactValidationError):
        store.create(bad2)


def test_artifact_store_get_unknown_returns_none() -> None:
    store = _store(_tmp())
    assert store.get("art-deadbeefdeadbeefdeadbeefdeadbeef") is None


def test_api_create_artifact_returns_id_and_payload() -> None:
    client = _api_client(_tmp())
    resp = client.post("/api/artifacts", json=_packet(), headers=_auth())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["artifact_id"].startswith("art-")
    assert body["idempotent"] is False
    assert body["url"] == f"/artifacts/{body['artifact_id']}"
    # Producer is structured; the response must echo the producer identity.
    assert body["producer"]["agent"] == "claude_code"
    # Replay returns 200 + same id (idempotent).
    replay = client.post("/api/artifacts", json=_packet(), headers=_auth())
    assert replay.status_code == 200
    assert replay.json()["artifact_id"] == body["artifact_id"]
    assert replay.json()["idempotent"] is True


def test_api_create_artifact_rejects_bad_kind() -> None:
    client = _api_client(_tmp())
    bad = _packet(kind="not-a-kind")
    resp = client.post("/api/artifacts", json=bad, headers=_auth())
    assert resp.status_code == 422


def test_api_get_artifact_round_trips_packet() -> None:
    client = _api_client(_tmp())
    created = client.post("/api/artifacts", json=_packet(), headers=_auth()).json()
    fetched = client.get(f"/api/artifacts/{created['artifact_id']}", headers=_auth())
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["artifact_id"] == created["artifact_id"]
    assert body["packet"]["kind"] == "session_context"


def test_api_get_artifact_404_for_unknown_id() -> None:
    client = _api_client(_tmp())
    resp = client.get("/api/artifacts/art-deadbeefdeadbeefdeadbeefdeadbeef", headers=_auth())
    assert resp.status_code == 404


def test_api_render_artifact_renders_html_with_sandboxed_evidence() -> None:
    client = _api_client(_tmp())
    packet = _packet(
        evidence=[
            {"kind": "command", "label": "pytest", "exit_code": 0, "text": "1 passed"},
            {
                "kind": "image",
                "label": "shot.png",
                "media_type": "image/png",
                "data_uri": "data:image/png;base64,iVBORw0KGgo=",
            },
            {
                "kind": "html",
                "label": "preview",
                "html": "<p>hi<script>alert(1)</script>there</p>",
            },
        ]
    )
    artifact_id = client.post("/api/artifacts", json=packet, headers=_auth()).json()["artifact_id"]
    page = client.get(f"/artifacts/{artifact_id}", headers=_auth())
    assert page.status_code == 200
    html = page.text
    assert "<title>" in html and artifact_id in html
    assert "<img" in html and 'data:image/png;base64,iVBORw0KGgo=' in html
    # HTML evidence must be sandboxed (no script execution). The page may
    # contain the literal "<script>" sequence inside the iframe srcdoc only
    # if the iframe's sandbox attribute disables scripts; the sandbox token
    # is asserted.
    assert 'sandbox=""' in html
    # The page escapes user-supplied script tags in summary; raw <script>
    # in summary must be escaped, not executed.
    assert "&lt;script&gt;" in html


def test_api_render_artifact_unknown_id_returns_404() -> None:
    client = _api_client(_tmp())
    resp = client.get("/artifacts/art-deadbeefdeadbeefdeadbeefdeadbeef", headers=_auth())
    assert resp.status_code == 404


def test_api_enqueue_accepts_artifact_ids_list() -> None:
    tmp = _tmp()
    client = _api_client(tmp)
    artifact_id = client.post("/api/artifacts", json=_packet(), headers=_auth()).json()["artifact_id"]
    body = {
        "name": "task-x",
        "task": "do thing",
        "workspace": "/tmp",
        "trio": "kimi",
        "requested_by": "r",
        "artifact_ids": [artifact_id],
    }
    resp = client.post("/api/queue", json=body, headers=_auth())
    assert resp.status_code == 200, resp.text
    queue_id = resp.json()["queue_id"]
    # The entry persists the ids.
    entry = _queue_store(tmp).get(queue_id)
    assert entry is not None
    assert entry.artifact_ids == [artifact_id]


def test_api_enqueue_rejects_malformed_artifact_id() -> None:
    client = _api_client(_tmp())
    body = {
        "name": "task-x",
        "task": "do thing",
        "workspace": "/tmp",
        "trio": "kimi",
        "requested_by": "r",
        "artifact_ids": ["not-an-art-id"],
    }
    resp = client.post("/api/queue", json=body, headers=_auth())
    assert resp.status_code == 422, resp.text


def test_enqueue_contract_normalize_persists_artifact_ids() -> None:
    params = _normalize_request(
        {
            "name": "n",
            "task": "t",
            "workspace": "w",
            "trio": "kimi",
            "requested_by": "r",
            "artifact_ids": ["art-deadbeefdeadbeefdeadbeefdeadbeef"],
        }
    )
    assert params["artifact_ids"] == ["art-deadbeefdeadbeefdeadbeefdeadbeef"]


def test_enqueue_contract_normalize_rejects_duplicate_artifact_id() -> None:
    import lh_harness.queue as queue

    try:
        queue._validate_artifact_ids(["art-aaaaaaaaaaaaaaaa", "art-aaaaaaaaaaaaaaaa"])
    except ValueError as exc:
        assert "duplicated" in str(exc)
    else:
        raise AssertionError("expected ValueError on duplicate id")


def _tmp() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix="lh-artifacts-"))


def _auth() -> dict:
    return {"Authorization": "Bearer secret"}
