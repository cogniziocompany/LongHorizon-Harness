"""LHH-GATE-OWNER-NOTE: MCP-tool tests for ``harness_annotate_run_gate``.

Pairs with the round-2 HTTP endpoint test
(``tests/webapi/test_gate_owner_note_endpoint.py``). The MCP tool is the
``hydrafleet``-style counterpart of the REST route, exposed via the fleet
gateway ``POST /api/mcp/fleet/{tool_name}``. These tests cover:

* Manifest registration: the tool is advertised via ``tools_manifest()``
  with the documented parameter surface (run_id, approval_id, owner,
  owner_note) and coexists with ``harness_resolve_gate``.
* Happy path: 200 with the updated approval snapshot, carrying audit
  metadata (``owner_set_by``/``owner_set_at``).
* 200 for either-field-only updates (owner or owner_note alone).
* 404 for unknown run / unknown approval id.
* 409 for resolved (read-only) approvals.
* 422 for a body that supplies neither ``owner`` nor ``owner_note``.
* Idempotency: re-calling overwrites the badge/note and rewrites the audit
  stamp.
* 404 for an unknown tool name (regression guard).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.caller_auth import ANNOTATE_TOOL, RESOLVE_TOOL
from lh_harness.dashboard.state import ApprovalOption, DashboardState
from lh_harness.mcp_tools import tools_manifest
from lh_harness.webapi.server import create_app


def _fixture(tmp_path: Path, *, token: str | None = "test-token") -> tuple[Path, DashboardState, TestClient]:
    root = tmp_path / "runs"
    run = root / "run-1"
    role_dir = run / "logs" / "role_management"
    role_dir.mkdir(parents=True)
    (run / "control").mkdir(parents=True)
    (role_dir / "rounds").mkdir(parents=True)
    (run / "logs" / "report.json").write_text(
        json.dumps({"task": "fixture", "status": "running"}), encoding="utf-8"
    )
    (role_dir / "events.jsonl").write_text(
        json.dumps({"ts": 1, "event": "role_harness_start"}) + "\n",
        encoding="utf-8",
    )
    state = DashboardState(
        run / "logs",
        runs_root=root,
        control_enabled=True,
    )
    app = create_app(
        state=state,
        runs_root=root,
        control_enabled=True,
        auth_token=token,
        bind_host="testserver",
    )
    return root, state, TestClient(app)


_OWNER_NOTE_FIXTURE = (
    "I approve these gates, not you, unless a gate needs a decision only you "
    "can make. See [docs](https://example.local/run) for context."
)


# --------------------------------------------------------------------- manifest --


def test_annotate_gate_is_registered_in_tools_manifest() -> None:
    """The MCP tool is advertised with the documented parameter surface."""
    manifest = {tool["name"]: tool for tool in tools_manifest()}
    assert ANNOTATE_TOOL in manifest
    params = manifest[ANNOTATE_TOOL]["input_schema"]["properties"]
    for required_field in ("run_id", "approval_id"):
        assert params[required_field]["type"] == "string"
    # owner and owner_note are optional upgrades to an existing pending gate.
    assert params["owner"]["type"] == "string"
    assert params["owner_note"]["type"] == "string"


def test_annotate_gate_is_listed_alongside_resolve_gate() -> None:
    names = {tool["name"] for tool in tools_manifest()}
    # Sanity: both tools must coexist; removing resolve is not what we want.
    assert ANNOTATE_TOOL in names
    assert RESOLVE_TOOL in names


# ----------------------------------------------------------- mcp fleet gateway --


def _post_mcp(client: TestClient, arguments: dict, token: str = "test-token"):
    return client.post(
        f"/api/mcp/fleet/{ANNOTATE_TOOL}",
        json={"arguments": arguments},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_mcp_fleet_annotate_run_gate_happy_path(tmp_path: Path) -> None:
    """End-to-end: REST gateway ``/api/mcp/fleet/harness_annotate_run_gate``."""
    _root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="Continue?")

    response = _post_mcp(
        client,
        {
            "run_id": "run-1",
            "approval_id": approval.approval_id,
            "owner": "paxton",
            "owner_note": _OWNER_NOTE_FIXTURE,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["approval_id"] == approval.approval_id
    snapshot = body["approval"]
    assert snapshot["owner"] == "paxton"
    assert snapshot["owner_note"] == _OWNER_NOTE_FIXTURE
    assert snapshot["owner_set_by"] == "overseer1"
    assert isinstance(snapshot["owner_set_at"], (int, float))


def test_mcp_fleet_annotate_run_gate_owner_only_update(tmp_path: Path) -> None:
    _root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="Continue?")

    response = _post_mcp(
        client,
        {
            "run_id": "run-1",
            "approval_id": approval.approval_id,
            "owner": "overseer",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["approval"]["owner"] == "overseer"


def test_mcp_fleet_annotate_run_gate_owner_note_only_update(tmp_path: Path) -> None:
    _root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="Continue?")

    response = _post_mcp(
        client,
        {
            "run_id": "run-1",
            "approval_id": approval.approval_id,
            "owner_note": "blank badge for now",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()["approval"]
    assert body["owner"] == "overseer"  # default
    assert body["owner_note"] == "blank badge for now"


def test_mcp_fleet_annotate_run_gate_empty_body_returns_422(tmp_path: Path) -> None:
    _root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="Continue?")

    response = _post_mcp(
        client,
        {
            "run_id": "run-1",
            "approval_id": approval.approval_id,
        },
    )
    # The fleet gateway translates the dispatch code verbatim:
    # code=422 -> HTTP 422.
    assert response.status_code == 422
    body = response.json()
    # MCP tool failures surface as ``error`` rather than ``detail``.
    assert "owner" in body.get("error", "")


def test_mcp_fleet_annotate_run_gate_unknown_approval_returns_404(tmp_path: Path) -> None:
    _root, _state, client = _fixture(tmp_path)

    response = _post_mcp(
        client,
        {
            "run_id": "run-1",
            "approval_id": "does-not-exist",
            "owner": "overseer",
            "owner_note": "ignored",
        },
    )
    assert response.status_code == 404
    assert response.json()["error"] == "approval not found"


def test_mcp_fleet_annotate_run_gate_unknown_run_returns_404(tmp_path: Path) -> None:
    _root, _state, client = _fixture(tmp_path)

    response = _post_mcp(
        client,
        {
            "run_id": "run-does-not-exist",
            "approval_id": "ap-1",
            "owner": "overseer",
        },
    )
    assert response.status_code == 404


def test_mcp_fleet_annotate_run_gate_resolved_returns_409(tmp_path: Path) -> None:
    _root, state, client = _fixture(tmp_path)
    approval = state.create_approval(
        title="Continue?",
        options=[ApprovalOption(value="continue", label="Continue")],
    )
    assert state.resolve_approval(approval.approval_id, action="continue", user_input="go")

    response = _post_mcp(
        client,
        {
            "run_id": "run-1",
            "approval_id": approval.approval_id,
            "owner": "overseer",
            "owner_note": "should be ignored",
        },
    )
    assert response.status_code == 409
    assert "resolved" in response.json()["error"]


def test_mcp_fleet_annotate_run_gate_idempotent_audit_overwrite(tmp_path: Path) -> None:
    """Re-calling overwrites the badge/note AND rewrites ``owner_set_at``."""
    _root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="Continue?")

    first = _post_mcp(
        client,
        {
            "run_id": "run-1",
            "approval_id": approval.approval_id,
            "owner": "paxton",
            "owner_note": "first note",
        },
    )
    assert first.status_code == 200, first.text
    first_at = first.json()["approval"]["owner_set_at"]
    assert first.json()["approval"]["owner_note"] == "first note"

    second = _post_mcp(
        client,
        {
            "run_id": "run-1",
            "approval_id": approval.approval_id,
            "owner_note": "rewritten note",
        },
    )
    assert second.status_code == 200, second.text
    second_at = second.json()["approval"]["owner_set_at"]
    body = second.json()["approval"]
    # Note refreshed; audit stamp rewritten (>= first).
    assert body["owner_note"] == "rewritten note"
    assert second_at >= first_at


def test_mcp_fleet_annotate_run_gate_unknown_tool_returns_404(tmp_path: Path) -> None:
    """An unknown tool name keeps 404 even when a sibling handler exists."""
    _root, _state, client = _fixture(tmp_path)

    response = client.post(
        "/api/mcp/fleet/harness_definitely_not_a_tool",
        json={"arguments": {}},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 404

