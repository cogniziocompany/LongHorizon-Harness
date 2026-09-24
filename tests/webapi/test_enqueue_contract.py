"""Task 233: enqueue-contract regression tests.

Covers both storage backends (file store, Postgres store) and both enqueue
surfaces (REST ``POST /api/queue``, MCP ``harness_enqueue_task``).  Tests are
named by the surface they exercise.

The Postgres tests run only when ``LH_HARNESS_DB_URL`` names a reachable
scratch database; they skip cleanly otherwise so the hermetic suite stays green.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lh_harness.mcp_tools import dispatch, tools_manifest
from lh_harness.queue import (
    QueueStore,
    UnknownQueueFieldError,
    _normalize_request,
)

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.webapi.server import create_app


def _file_store(tmp_path: Path) -> QueueStore:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    return QueueStore(root)


def _require_pg_store(tmp_path: Path):
    from lh_harness.pg_queue import PgQueueStore

    url = os.environ.get("LH_HARNESS_DB_URL")
    if not url or not url.strip():
        pytest.skip("LH_HARNESS_DB_URL not set; Postgres backend unavailable")
    store = PgQueueStore(url)
    try:
        store.counts()
    except Exception:
        pytest.skip("could not connect to Postgres backend")
    return store


def _api_client(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    # TestClient sends Host: testserver; bind_host must agree or the loopback
    # host check rejects every request.
    return TestClient(
        create_app(runs_root=root, auth_token="secret", bind_host="testserver")
    )


# ---------------------------------------------------------------------------
# _normalize_request (core validation surface)
# ---------------------------------------------------------------------------


def test_normalize_request_persists_continuation_and_priority_and_dedup() -> None:
    """Core validation accepts and normalizes the four task-233 fields."""
    body = {
        "name": "n",
        "task": "t",
        "workspace": "w",
        "trio": "kimi",
        "priority": 5,
        "branch": "feat/continuation",
        "continue_branch": False,
        "dedup_key": "dk",
        "requested_by": "r",
    }
    params = _normalize_request(body)
    assert params["priority"] == 5
    assert params["branch"] == "feat/continuation"
    assert params["continue_branch"] is False
    assert params["dedup_key"] == "dk"


def test_normalize_request_continues_both_opt_ins_rejected() -> None:
    with pytest.raises(ValueError, match="branch or continue_branch, not both"):
        _normalize_request(
            {
                "name": "n",
                "task": "t",
                "workspace": "w",
                "branch": "feat/x",
                "continue_branch": True,
                "requested_by": "r",
            }
        )


def test_normalize_request_unknown_fields_rejected() -> None:
    with pytest.raises(UnknownQueueFieldError, match="unknown field") as exc:
        _normalize_request(
            {
                "name": "n",
                "task": "t",
                "workspace": "w",
                "requested_by": "r",
                "status": "launched",
                "bogus": True,
            }
        )
    message = str(exc.value)
    assert "bogus" in message
    assert "status" in message


def test_normalize_request_accepts_known_key_set_including_aliases() -> None:
    """Every accepted key (incl. the task_file/roles aliases) still passes."""
    _normalize_request(
        {
            "name": "n",
            "task": "t",
            "task_file": None,  # alias key name accepted; only one of task/task_file set in real use
            "workspace": "w",
            "max_rounds": 3,
            "trio": "kimi",
            "roles": None,
            "priority": 1,
            "branch": "",
            "continue_branch": False,
            "base_check": "",
            "requested_by": "r",
            "dedup_key": "",
        }
    )


def test_normalize_request_trio_optional() -> None:
    """Enqueue without trio succeeds and persists trio as unset (empty string)."""
    params = _normalize_request(
        {"name": "n", "task": "t", "workspace": "w", "requested_by": "r"}
    )
    assert params["trio"] == ""


def test_normalize_request_roles_alias() -> None:
    params = _normalize_request(
        {
            "name": "n",
            "task": "t",
            "workspace": "w",
            "roles": "qwen",
            "requested_by": "r",
        }
    )
    assert params["trio"] == "qwen"


# ---------------------------------------------------------------------------
# File store surface
# ---------------------------------------------------------------------------


def test_file_store_persists_continuation_and_dedup(tmp_path: Path) -> None:
    """File store surface: the four fields round-trip through disk read-back."""
    store = _file_store(tmp_path)
    entry = store.create(
        {
            "name": "continuation",
            "task": "t",
            "workspace": "w",
            "trio": "kimi",
            "priority": 4,
            "branch": "feat/233",
            "continue_branch": False,
            "dedup_key": "233-key",
            "requested_by": "file-test",
        }
    )
    read = store.get(entry.queue_id)
    assert read is not None
    assert read.priority == 4
    assert read.branch == "feat/233"
    assert read.continue_branch is False
    assert read.dedup_key == "233-key"


def test_file_store_continue_branch_flag_persisted(tmp_path: Path) -> None:
    """File store surface: continue_branch=True survives the JSON round-trip."""
    store = _file_store(tmp_path)
    entry = store.create(
        {
            "name": "cont-flag",
            "task": "t",
            "workspace": "w",
            "trio": "qwen",
            "continue_branch": True,
            "requested_by": "file-test",
        }
    )
    read = store.get(entry.queue_id)
    assert read is not None
    assert read.continue_branch is True
    assert read.branch == ""


def test_file_store_requeue_keeps_continuation_fields(tmp_path: Path) -> None:
    """File store requeue surface: a retried continuation stays a continuation.

    The original task-233 defect was exactly this inversion -- a continuation
    task being filed as a fresh one -- so the retry path must not drop the
    opt-ins either.
    """
    store = _file_store(tmp_path)
    entry = store.create(
        {
            "name": "cont-retry",
            "task": "t",
            "workspace": "w",
            "trio": "kimi",
            "branch": "feat/cont",
            "requested_by": "file-test",
        }
    )
    store.mark_failed(entry.queue_id, "boom")
    successor = store.requeue(entry.queue_id, "boom")
    assert successor is not None
    assert successor.branch == "feat/cont"
    assert successor.continue_branch is False
    # On-disk read-back, not just the in-memory return value.
    read = store.get(successor.queue_id)
    assert read is not None
    assert read.branch == "feat/cont"


def test_file_store_enqueues_without_trio(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    entry = store.create(
        {
            "name": "no-trio",
            "task": "t",
            "workspace": "w",
            "requested_by": "file-test",
        }
    )
    read = store.get(entry.queue_id)
    assert read is not None
    assert read.trio == ""


# ---------------------------------------------------------------------------
# Postgres store surface
# ---------------------------------------------------------------------------


def test_pg_store_persists_continuation_and_dedup(tmp_path: Path) -> None:
    store = _require_pg_store(tmp_path)
    entry = store.create(
        {
            "name": "continuation",
            "task": "t",
            "workspace": "w",
            "trio": "kimi",
            "priority": 4,
            "branch": "feat/233",
            "continue_branch": False,
            "dedup_key": "233-key",
            "requested_by": "pg-test",
        }
    )
    read = store.get(entry.queue_id)
    assert read is not None
    assert read.priority == 4
    assert read.branch == "feat/233"
    assert read.continue_branch is False
    assert read.dedup_key == "233-key"
    # Full round-trip equality (file-store conformance baseline).
    assert read.to_dict() == entry.to_dict()


def test_pg_store_enqueues_without_trio(tmp_path: Path) -> None:
    store = _require_pg_store(tmp_path)
    entry = store.create(
        {
            "name": "no-trio",
            "task": "t",
            "workspace": "w",
            "requested_by": "pg-test",
        }
    )
    read = store.get(entry.queue_id)
    assert read is not None
    assert read.trio == ""


# ---------------------------------------------------------------------------
# REST API surface
# ---------------------------------------------------------------------------


def _api_payload(**overrides) -> dict:
    body = {
        "name": "api",
        "task": "t",
        "workspace": "w",
        "requested_by": "api-test",
    }
    body.update(overrides)
    return body


def _auth() -> dict:
    return {"Authorization": "Bearer secret"}


def test_api_unknown_fields_400_with_field_names(tmp_path: Path) -> None:
    client = _api_client(tmp_path)
    response = client.post(
        "/api/queue",
        json=_api_payload(bogus=True, status="launched"),
        headers=_auth(),
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "bogus" in detail
    assert "status" in detail


def test_api_known_fields_persisted(tmp_path: Path) -> None:
    client = _api_client(tmp_path)
    response = client.post(
        "/api/queue",
        json=_api_payload(
            trio="kimi",
            priority=7,
            branch="feat/api",
            continue_branch=False,
            dedup_key="api-dk",
        ),
        headers=_auth(),
    )
    assert response.status_code == 200
    queue_id = response.json()["queue_id"]

    listed = client.get("/api/queue", headers=_auth()).json()
    entry = next(item for item in listed["entries"] if item["queue_id"] == queue_id)
    assert entry["priority"] == 7
    assert entry["branch"] == "feat/api"
    assert entry["continue_branch"] is False
    assert entry["dedup_key"] == "api-dk"
    assert entry["trio"] == "kimi"


def test_api_enqueues_without_trio(tmp_path: Path) -> None:
    client = _api_client(tmp_path)
    response = client.post(
        "/api/queue",
        json=_api_payload(),
        headers=_auth(),
    )
    assert response.status_code == 200
    queue_id = response.json()["queue_id"]

    listed = client.get("/api/queue", headers=_auth()).json()
    entry = next(item for item in listed["entries"] if item["queue_id"] == queue_id)
    assert entry["trio"] == ""


def test_api_required_fields_still_422(tmp_path: Path) -> None:
    client = _api_client(tmp_path)
    response = client.post(
        "/api/queue",
        json={"name": "x", "workspace": "w", "trio": "kimi", "requested_by": "r"},
        headers=_auth(),
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# MCP tool surface
# ---------------------------------------------------------------------------


def _enqueue_tool(arguments: dict, store) -> dict:
    return dispatch(
        "harness_enqueue_task",
        arguments,
        queue_store=store,
        registry=None,
        supervisor=None,
        auth_token=None,
        request_token=None,
    )


def test_mcp_tool_schema_documents_every_field() -> None:
    enqueue = next(
        tool for tool in tools_manifest() if tool["name"] == "harness_enqueue_task"
    )
    schema = enqueue["input_schema"]
    assert schema.get("additionalProperties") is False
    props = schema["properties"]
    for field in (
        "name",
        "task",
        "workspace",
        "max_rounds",
        "trio",
        "priority",
        "continue_branch",
        "branch",
        "dedup_key",
        "base_check",
        "requested_by",
    ):
        assert field in props, f"field {field} missing from schema"
        assert "description" in props[field], f"field {field} has no description"


def test_mcp_tool_persists_continuation_and_dedup(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    result = _enqueue_tool(
        {
            "name": "mcp-continuation",
            "task": "t",
            "workspace": "w",
            "trio": "kimi",
            "priority": 8,
            "branch": "feat/mcp",
            "continue_branch": False,
            "dedup_key": "mcp-dk",
            "requested_by": "mcp-test",
        },
        store,
    )
    assert result["ok"] is True
    read = store.get(result["queue_id"])
    assert read is not None
    assert read.priority == 8
    assert read.branch == "feat/mcp"
    assert read.continue_branch is False
    assert read.dedup_key == "mcp-dk"


def test_mcp_tool_enqueues_without_trio(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    result = _enqueue_tool(
        {
            "name": "mcp-no-trio",
            "task": "t",
            "workspace": "w",
            "requested_by": "mcp-test",
        },
        store,
    )
    assert result["ok"] is True
    read = store.get(result["queue_id"])
    assert read is not None
    assert read.trio == ""


def test_mcp_tool_rejects_unknown_field(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    result = _enqueue_tool(
        {
            "name": "x",
            "task": "t",
            "workspace": "w",
            "trio": "kimi",
            "requested_by": "mcp-test",
            "bogus": True,
        },
        store,
    )
    assert result["ok"] is False
    assert result["code"] == 400
    assert "bogus" in result["error"]


# ---------------------------------------------------------------------------
# Streamable-HTTP / REST bridge surface
# ---------------------------------------------------------------------------


def test_mcp_bridge_rejects_unknown_properties(tmp_path: Path) -> None:
    """The REST bridge to the MCP tool returns 400 for unknown tool args."""
    client = _api_client(tmp_path)
    response = client.post(
        "/api/mcp/fleet/harness_enqueue_task",
        json={
            "arguments": {
                "name": "x",
                "task": "t",
                "workspace": "w",
                "trio": "kimi",
                "requested_by": "bridge-test",
                "extra": 1,
            }
        },
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 400
    assert "extra" in response.json()["error"]


def test_mcp_bridge_persists_branch_and_dedup(tmp_path: Path) -> None:
    client = _api_client(tmp_path)
    response = client.post(
        "/api/mcp/fleet/harness_enqueue_task",
        json={
            "arguments": {
                "name": "bridge-cont",
                "task": "t",
                "workspace": "w",
                "trio": "kimi",
                "priority": 6,
                "branch": "feat/bridge",
                "dedup_key": "bridge-dk",
                "requested_by": "bridge-test",
            }
        },
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ok"] is True

    listed = client.get("/api/queue", headers=_auth()).json()
    entry = next(item for item in listed["entries"] if item["queue_id"] == data["queue_id"])
    assert entry["branch"] == "feat/bridge"
    assert entry["priority"] == 6
    assert entry["dedup_key"] == "bridge-dk"


def test_mcp_streamable_http_rejects_unknown_properties(tmp_path: Path) -> None:
    """The real MCP streamable-HTTP endpoint returns an isError result for unknown args."""
    client = _api_client(tmp_path)
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "harness_enqueue_task",
                "arguments": {
                    "name": "x",
                    "task": "t",
                    "workspace": "w",
                    "trio": "kimi",
                    "requested_by": "stream-test",
                    "noSuchField": True,
                },
            },
        },
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["result"]["isError"] is True
    result = json.loads(payload["result"]["content"][0]["text"])
    assert "noSuchField" in result["error"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])