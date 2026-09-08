from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import ApprovalOption, DashboardState
from lh_harness.supervisor.control_bus import ControlBus
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
    # The registry expects the base state to point at the run log directory,
    # not the role subdirectory, so the cached path matches _run_paths().
    state = DashboardState(
        run / "logs",
        runs_root=root,
        control_enabled=True,
    )
    # TestClient sends Host: testserver. bind_host must agree or the loopback
    # host check rejects every request.
    app = create_app(
        state=state,
        runs_root=root,
        control_enabled=True,
        auth_token=token,
        bind_host="testserver",
    )
    return root, state, TestClient(app)


def _post(client: TestClient, path: str, payload: dict[str, Any], token: str | None = "test-token") -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(path, json=payload, headers=headers)


def test_mcp_fleet_tools_manifest(client_fixture: Any = None) -> None:
    # Work around the missing fixture by using a local temp path.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        response = client.get("/api/mcp/fleet/tools", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["gateway_alias"] == "lhharness"
        names = {tool["name"] for tool in data["tools"]}
        assert names == {
            "harness_enqueue_task",
            "harness_list_queue",
            "harness_run_status",
            "harness_resolve_gate",
            "harness_list_contentions",
        }
        enqueue = next(tool for tool in data["tools"] if tool["name"] == "harness_enqueue_task")
        assert "kimi" in enqueue["description"]
        assert "qwen" in enqueue["description"]
        assert "prod" in enqueue["description"]


def test_mcp_fleet_enqueue_requires_auth() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp), token="secret")
        response = client.post(
            "/api/mcp/fleet/harness_enqueue_task",
            json={"arguments": {"name": "x", "task": "y", "workspace": ".", "trio": "kimi", "requested_by": "ci"}},
        )
        assert response.status_code == 401


def test_mcp_fleet_enqueue_and_list() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        response = _post(
            client,
            "/api/mcp/fleet/harness_enqueue_task",
            {
                "arguments": {
                    "name": "test task",
                    "task": "verify queue mcp tool",
                    "workspace": "./workspace",
                    "trio": "qwen",
                    "requested_by": "ci",
                    "priority": 7,
                }
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["ok"] is True
        assert data["status"] == "pending"
        queue_id = data["queue_id"]

        response = _post(
            client,
            "/api/mcp/fleet/harness_list_queue",
            {"arguments": {}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["counts"]["pending"] == 1
        assert any(entry["queue_id"] == queue_id for entry in data["entries"])


def test_mcp_fleet_run_status() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        response = _post(
            client,
            "/api/mcp/fleet/harness_run_status",
            {"arguments": {"run_id": "run-1"}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["run_id"] == "run-1"


def test_mcp_fleet_resolve_gate_rejects_non_ascii() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        approval = state.create_approval(
            title="gate",
            options=[ApprovalOption(value="continue", label="Continue")],
        )

        response = _post(
            client,
            "/api/mcp/fleet/harness_resolve_gate",
            {
                "arguments": {
                    "run_id": "run-1",
                    "approval_id": approval.approval_id,
                    "action": "continue",
                    "user_input": "héllo non-ascii",
                }
            },
        )
        assert response.status_code == 422
        assert "ASCII" in response.json()["error"]


def test_api_resolve_approval_rejects_non_ascii_user_input() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        approval = state.create_approval(
            title="gate",
            options=[ApprovalOption(value="continue", label="Continue")],
        )

        response = _post(
            client,
            f"/api/runs/run-1/approvals/{approval.approval_id}/resolve",
            {"action": "continue", "user_input": "héllo"},
        )
        assert response.status_code == 422
        assert "ASCII" in response.json()["detail"]


def test_api_resolve_approval_accepts_ascii_user_input() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        approval = state.create_approval(
            title="gate",
            options=[ApprovalOption(value="continue", label="Continue")],
        )

        response = _post(
            client,
            f"/api/runs/run-1/approvals/{approval.approval_id}/resolve",
            {"action": "continue", "user_input": "plain ASCII note"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["status"] == "accepted"


def test_mcp_fleet_unknown_tool_returns_404() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        response = _post(client, "/api/mcp/fleet/not_a_tool", {"arguments": {}})
        assert response.status_code == 404
