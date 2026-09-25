from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import ApprovalOption, DashboardState
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


def _post(client: TestClient, path: str, payload: dict[str, Any], token: str | None = "test-token") -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(path, json=payload, headers=headers)


def test_mcp_enqueue_returns_warning_on_overlap(tmp_path: Path) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        response = _post(
            client,
            "/api/mcp/fleet/harness_enqueue_task",
            {
                "arguments": {
                    "name": "a",
                    "task": "task a",
                    "workspace": "./workspace",
                    "trio": "qwen",
                    "requested_by": "ci",
                }
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert "warnings" not in data

        response = _post(
            client,
            "/api/mcp/fleet/harness_enqueue_task",
            {
                "arguments": {
                    "name": "b",
                    "task": "task b",
                    "workspace": "./workspace",
                    "trio": "qwen",
                    "requested_by": "ci",
                }
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert "warnings" in data
        assert all(w["code"] == "workspace_contention" for w in data["warnings"])
        assert any("workspace" in w["message"].lower() for w in data["warnings"])
        assert all("message_zh" in w for w in data["warnings"])


def test_mcp_list_contentions_tool_reads_file(tmp_path: Path) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        contention_path = root / "queue" / "contention.json"
        contention_path.parent.mkdir(parents=True, exist_ok=True)
        contention_path.write_text(
            json.dumps({"ok": True, "available": True, "contentions": []}),
            encoding="utf-8",
        )
        response = _post(client, "/api/mcp/fleet/harness_list_contentions", {"arguments": {}})
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["contentions"] == []


def test_mcp_list_queue_includes_grouped_contentions(tmp_path: Path) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        contention_path = root / "queue" / "contention.json"
        contention_path.parent.mkdir(parents=True, exist_ok=True)
        contention_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "available": True,
                    "contentions": [
                        {
                            "contention_id": "abc",
                            "severity": "same_repo",
                            "group_key": "repo:host/org/repo",
                            "members": [
                                {"run_id": "run-a", "workspace": "/a", "branch": "main"}
                            ],
                            "truncated": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        response = _post(client, "/api/mcp/fleet/harness_list_queue", {"arguments": {}})
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert len(data["contentions"]) == 1
        assert data["contentions"][0]["contention_id"] == "abc"


def test_mcp_tools_manifest_includes_list_contentions() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        response = client.get("/api/mcp/fleet/tools", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200
        data = response.json()
        names = {tool["name"] for tool in data["tools"]}
        assert "harness_list_contentions" in names
