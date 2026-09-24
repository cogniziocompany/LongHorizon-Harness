"""Tests for the MCP streamable-HTTP endpoint (POST /mcp).

The endpoint speaks JSON-RPC 2.0 directly (LiteLLM registers MCP servers over
streamable HTTP) while sharing the tool manifest and the dispatch path with
the REST bridge under /api/mcp/fleet.  These tests cover the six behaviors
required by TASK 228:

- initialize returns the client protocolVersion (echoed) with tools capability
- tools/list returns exactly the same list as GET /api/mcp/fleet/tools
- tools/call reaches the real store for harness_list_queue / harness_run_status
- missing bearer token -> 401
- unknown method -> JSON-RPC error -32601
- tools/call of an unknown tool -> result with isError true
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import DashboardState
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
    # TestClient sends Host: testserver. bind_host must agree or the loopback
    # host check rejects every request (same convention as test_mcp_fleet.py).
    app = create_app(
        state=state,
        runs_root=root,
        control_enabled=True,
        auth_token=token,
        bind_host="testserver",
    )
    return root, state, TestClient(app)


def _rpc(
    client: TestClient,
    payload: dict[str, Any] | list[dict[str, Any]],
    *,
    token: str | None = "test-token",
) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/mcp", json=payload, headers=headers)


def test_mcp_initialize_echoes_supported_protocol_version() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        response = _rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "litellm"}},
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        data = response.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        assert data["result"]["protocolVersion"] == "2025-03-26"
        assert data["result"]["capabilities"] == {"tools": {}}
        assert data["result"]["serverInfo"]["name"] == "lhharness"

        # An unsupported/absent protocolVersion falls back to the server default.
        response = _rpc(
            client,
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
        )
        assert response.json()["result"]["protocolVersion"] == "2025-03-26"

        # The notifications/initialized handshake is accepted with an empty body.
        response = _rpc(client, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert response.status_code == 202
        assert response.content == b""


def test_mcp_tools_list_matches_rest_bridge() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        rest = client.get("/api/mcp/fleet/tools", headers={"Authorization": "Bearer test-token"})
        assert rest.status_code == 200
        response = _rpc(client, {"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 7
        # One source of truth: the MCP tools/list result is byte-identical to
        # the manifest the REST bridge serves.
        assert data["result"]["tools"] == rest.json()["tools"]


def test_mcp_tools_call_list_queue() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        # Seed the real file queue store through the REST bridge so the MCP
        # tools/call exercises the same dispatch path over the same store.
        seeded = client.post(
            "/api/queue",
            json={"name": "mcp seed", "task": "t", "workspace": "./workspace", "trio": "qwen", "requested_by": "ci"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert seeded.status_code == 200, seeded.text

        response = _rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "harness_list_queue", "arguments": {}},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 3
        result = data["result"]
        assert result["isError"] is False
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "text"
        payload = json.loads(result["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["counts"]["pending"] == 1
        assert payload["entries"][0]["name"] == "mcp seed"


def test_mcp_tools_call_run_status() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        response = _rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "harness_run_status", "arguments": {"run_id": "run-1"}},
            },
        )
        assert response.status_code == 200
        data = response.json()
        result = data["result"]
        assert result["isError"] is False
        payload = json.loads(result["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["run_id"] == "run-1"

        # A missing run surfaces as a tool-level error, not an HTTP failure.
        response = _rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "harness_run_status", "arguments": {"run_id": "missing-run"}},
            },
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"] is True
        assert json.loads(result["content"][0]["text"])["code"] == 404


def test_mcp_requires_bearer_token() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp), token="secret")
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert response.status_code == 401

        # A wrong token is rejected too.
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer nope"},
        )
        assert response.status_code == 401

        # And the trailing-slash variant shares the same boundary.
        response = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer secret"},
        )
        assert response.status_code == 200


def test_mcp_unknown_method_returns_32601() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        response = _rpc(client, {"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 9
        assert data["error"]["code"] == -32601


def test_mcp_tools_call_unknown_tool_is_error() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        response = _rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {"name": "not_a_tool", "arguments": {}},
            },
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert payload["ok"] is False
        assert "unknown tool" in payload["error"]