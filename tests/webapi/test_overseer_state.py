"""Task 235: overseer-state tools over the migrated apparatus archive.

The six tools (get_queue_entry, list_queue, get_task_history, read_ledger,
list_open_asks, get_handoff) read the in-repo apparatus produced by task
104b's migration (``tasks/``, ``queue/done/``, ``queue/blocked/``,
``docs/LEDGER.md``, ``queue/OPEN-ASKS.md``, ``docs/handoffs/``).  The tests
here run against a small synthetic archive built in a temp directory, so the
suite stays hermetic; a separate check pins the schema expectations against
the real archive layout.

Nothing here reaches a fleet host, the deployment Postgres, or ``C:/tmp``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import DashboardState
from lh_harness.overseer_state import resolve_overseer_root
from lh_harness.webapi.server import create_app


def _write_archive(root: Path) -> None:
    """Build a minimal-but-realistic apparatus archive under ``root``."""
    tasks_dir = root / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "demo-task-one-task.txt").write_text(
        "TASK demo-one\nBRANCH: fix/demo-one\nDeliverables: A, B. Hard rules: none.",
        encoding="utf-8",
    )
    (tasks_dir / "demo-task-two-task.txt").write_text(
        "TASK demo-two\nBRANCH: fix/demo-two\nDeliverables: C. Hard rules: ASCII only.",
        encoding="utf-8",
    )
    (root / "queue" / "done").mkdir(parents=True)
    (root / "queue" / "blocked").mkdir(parents=True)
    done_entry = {
        "name": "001-demo-task-one",
        "task_file": "C:/tmp/demo-task-one-task.txt",
        "workspace": "demo-workspace",
        "max_rounds": 8,
        "trio": "kimi",
        "run_id": "20260920T120000Z_deadbeef",
        "roles_bound": {"manager": "m", "executor": "e", "auditor": "a"},
        "launched_at": "2026-09-20T12:00:00.000000+00:00",
        "note": " || SKIP reason: at capacity (launcher pass 42)",
    }
    (root / "queue" / "done" / "001-demo-task-one.json").write_text(
        json.dumps(done_entry), encoding="utf-8"
    )
    blocked_entry = {
        "name": "002-demo-task-two",
        "task_file": "C:/tmp/demo-task-two-task.txt",
        "workspace": "demo-workspace",
        "max_rounds": 6,
        "trio": "qwen",
        "note": "*** BLOCKED 2026-09-21 by the overseer. Reason: waiting on Paxton. ***",
    }
    (root / "queue" / "blocked" / "002-demo-task-two.json").write_text(
        json.dumps(blocked_entry), encoding="utf-8"
    )
    docs_dir = root / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "LEDGER.md").write_text(
        "# Deployment ledger\n"
        "\n"
        "## TICK #1 - first tick\n"
        "first body\n"
        "\n"
        "## TICK #2 - second tick\n"
        "second body mentions task 204\n"
        "\n"
        "## 2026-09-16 09:26 PT - SCHEDULED TICK #288\n"
        "third body\n",
        encoding="utf-8",
    )
    (root / "queue" / "OPEN-ASKS.md").write_text(
        "# OPEN ASKS\n"
        "Updated 2026-09-23: ZERO OPEN ROWS.\n"
        "\n"
        "| id | ask | kind | evidence | recommended | default if silent | state |\n"
        "|---|---|---|---|---|---|---|\n"
        "| 1-still-open | Decide X | DECISION | evidence | yes | nothing | open |\n"
        "| 2-closed-row | Old ask | CREDENTIAL | evidence | n/a | nothing | **CLOSED 2026-09-21 - answered.** |\n"
        "\n"
        "## Answered - kept so they are not re-asked\n"
        "\n"
        "| id | answer | when |\n"
        "|---|---|---|\n",
        encoding="utf-8",
    )
    (docs_dir / "handoffs").mkdir()
    (docs_dir / "handoffs" / "HANDOFF-oldest-2026-09-10.md").write_text(
        "oldest handoff text", encoding="utf-8"
    )
    (docs_dir / "handoffs" / "HANDOFF-newest-2026-09-23.md").write_text(
        "newest handoff text", encoding="utf-8"
    )


def _dispatch(tool: str, arguments: dict[str, Any], *, overseer_root: str | None):
    from lh_harness.mcp_tools import dispatch

    return dispatch(
        tool,
        arguments,
        queue_store=None,
        registry=None,
        supervisor=None,
        auth_token=None,
        request_token=None,
        overseer_root=overseer_root,
    )


def _api_client(tmp_path: Path, archive: Path):
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    state = DashboardState(root, runs_root=root, control_enabled=True)
    # TestClient sends Host: testserver; bind_host must agree or the loopback
    # host check rejects every request (same convention as test_mcp_fleet.py).
    app = create_app(
        state=state,
        runs_root=root,
        auth_token="secret",
        bind_host="testserver",
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# resolve_overseer_root
# ---------------------------------------------------------------------------


def test_resolve_overseer_root_prefers_explicit_and_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    archive = tmp_path / "archive-a"
    _write_archive(archive)
    assert resolve_overseer_root(str(archive)) == archive.resolve()

    # An explicit invalid root returns None, it does not fall back silently.
    assert resolve_overseer_root(str(tmp_path / "missing")) is None

    monkeypatch.setenv("LH_HARNESS_APPARATUS_ROOT", str(archive))
    assert resolve_overseer_root() == archive.resolve()


def test_resolve_overseer_root_invalid_env_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LH_HARNESS_APPARATUS_ROOT", str(tmp_path / "not-an-archive"))
    assert resolve_overseer_root() is None


# ---------------------------------------------------------------------------
# get_queue_entry
# ---------------------------------------------------------------------------


def test_get_queue_entry_returns_full_task_text_and_note(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    result = _dispatch("get_queue_entry", {"name": "001-demo-task-one"}, overseer_root=str(archive))
    assert result["ok"] is True
    assert result["record_name"] == "001-demo-task-one"
    # Full task text comes from tasks/<basename>, not from any fleet host.
    assert "TASK demo-one" in result["task_text"]
    assert result["task_file_resolved"] == "tasks/demo-task-one-task.txt"
    # The note (with its skip reason) is carried verbatim.
    assert "SKIP reason" in result["note"]


def test_get_queue_entry_matches_by_run_id(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    result = _dispatch(
        "get_queue_entry", {"name": "20260920T120000Z_deadbeef"}, overseer_root=str(archive)
    )
    assert result["ok"] is True
    assert result["record_name"] == "001-demo-task-one"


def test_get_queue_entry_unknown_record_is_404(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    result = _dispatch("get_queue_entry", {"name": "nope"}, overseer_root=str(archive))
    assert result["ok"] is False
    assert result["code"] == 404


def test_get_queue_entry_unavailable_archive_is_501(tmp_path: Path) -> None:
    result = _dispatch(
        "get_queue_entry", {"name": "x"}, overseer_root=str(tmp_path / "missing")
    )
    assert result["ok"] is False
    assert result["code"] == 501


def test_get_queue_entry_requires_name(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    result = _dispatch("get_queue_entry", {}, overseer_root=str(archive))
    assert result["ok"] is False
    assert result["code"] == 400


# ---------------------------------------------------------------------------
# list_queue (with skip reasons)
# ---------------------------------------------------------------------------


def test_list_queue_returns_entries_with_skip_reason_notes(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    result = _dispatch("list_queue", {}, overseer_root=str(archive))
    assert result["ok"] is True
    assert result["counts"] == {"done": 1, "blocked": 1, "total": 2}
    by_name = {e["record_name"]: e for e in result["entries"]}
    assert "SKIP reason: at capacity" in by_name["001-demo-task-one"]["note"]
    assert "BLOCKED" in by_name["002-demo-task-two"]["note"]


def test_list_queue_filters_by_status(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    result = _dispatch("list_queue", {"status": "blocked"}, overseer_root=str(archive))
    assert result["ok"] is True
    assert result["counts"] == {"done": 0, "blocked": 1, "total": 1}
    assert [e["record_name"] for e in result["entries"]] == ["002-demo-task-two"]


def test_list_queue_rejects_unknown_status(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    result = _dispatch("list_queue", {"status": "bogus"}, overseer_root=str(archive))
    assert result["ok"] is False
    assert result["code"] == 400


# ---------------------------------------------------------------------------
# get_task_history
# ---------------------------------------------------------------------------


def test_get_task_history_keyed_by_task_number_and_name(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    result = _dispatch("get_task_history", {"task": "demo-task"}, overseer_root=str(archive))
    assert result["ok"] is True
    assert result["total"] == 2
    names = [run["record_name"] for run in result["runs"]]
    assert set(names) == {"001-demo-task-one", "002-demo-task-two"}
    # Every run carries its identity fields.
    for run in result["runs"]:
        assert {"record_name", "status", "name", "run_id", "launched_at"} <= set(run)


def test_get_task_history_unknown_task_is_empty(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    result = _dispatch("get_task_history", {"task": "no-such-task"}, overseer_root=str(archive))
    assert result["ok"] is True
    assert result["total"] == 0
    assert result["runs"] == []


# ---------------------------------------------------------------------------
# read_ledger
# ---------------------------------------------------------------------------


def test_read_ledger_returns_recent_rows(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    result = _dispatch("read_ledger", {"limit": 2}, overseer_root=str(archive))
    assert result["ok"] is True
    assert result["total_rows"] == 3
    assert result["returned"] == 2
    # Newest rows come from the end of the file.
    assert result["rows"][-1]["heading"].startswith("2026-09-16 09:26 PT")


def test_read_ledger_query_filters_rows(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    result = _dispatch("read_ledger", {"query": "task 204"}, overseer_root=str(archive))
    assert result["ok"] is True
    assert result["returned"] == 1
    assert "second body" in result["rows"][0]["body"]


# ---------------------------------------------------------------------------
# list_open_asks
# ---------------------------------------------------------------------------


def test_list_open_asks_returns_open_rows_only_by_default(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    result = _dispatch("list_open_asks", {}, overseer_root=str(archive))
    assert result["ok"] is True
    ids = [row["id"] for row in result["rows"]]
    assert ids == ["1-still-open"]
    row = result["rows"][0]
    assert {"id", "ask", "kind", "evidence", "recommended", "default_if_silent", "state"} == set(row)


def test_list_open_asks_include_closed(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    result = _dispatch(
        "list_open_asks", {"include_closed": True}, overseer_root=str(archive)
    )
    assert result["ok"] is True
    assert [row["id"] for row in result["rows"]] == ["1-still-open", "2-closed-row"]


# ---------------------------------------------------------------------------
# get_handoff
# ---------------------------------------------------------------------------


def test_get_handoff_defaults_to_most_recent(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    result = _dispatch("get_handoff", {}, overseer_root=str(archive))
    assert result["ok"] is True
    assert result["handoff"]["name"] == "HANDOFF-newest-2026-09-23"
    assert result["handoff"]["text"] == "newest handoff text"
    assert result["handoff"]["date"] == "2026-09-23"


def test_get_handoff_by_name(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    result = _dispatch(
        "get_handoff", {"name": "HANDOFF-oldest-2026-09-10.md"}, overseer_root=str(archive)
    )
    assert result["ok"] is True
    assert result["handoff"]["name"] == "HANDOFF-oldest-2026-09-10"


# ---------------------------------------------------------------------------
# Manifest + WebAPI wiring (both transports share one dispatch path)
# ---------------------------------------------------------------------------


def test_manifest_includes_six_overseer_tools() -> None:
    from lh_harness.mcp_tools import tools_manifest

    names = {tool["name"] for tool in tools_manifest()}
    assert {
        "get_queue_entry",
        "list_queue",
        "get_task_history",
        "read_ledger",
        "list_open_asks",
        "get_handoff",
    } <= names


def test_webapi_fleet_tools_manifest_lists_overseer_tools(tmp_path: Path) -> None:
    client = _api_client(tmp_path, tmp_path)
    response = client.get(
        "/api/mcp/fleet/tools", headers={"Authorization": "Bearer secret"}
    )
    assert response.status_code == 200
    names = {tool["name"] for tool in response.json()["tools"]}
    assert "get_queue_entry" in names
    assert "get_handoff" in names


def test_webapi_overseer_tool_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    monkeypatch.setenv("LH_HARNESS_APPARATUS_ROOT", str(archive))
    client = _api_client(tmp_path, archive)
    response = client.post(
        "/api/mcp/fleet/get_task_history",
        json={"arguments": {"task": "demo-task"}},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["total"] == 2


def test_webapi_overseer_tool_via_streamable_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    monkeypatch.setenv("LH_HARNESS_APPARATUS_ROOT", str(archive))
    client = _api_client(tmp_path, archive)
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "read_ledger",
                "arguments": {"limit": 1, "query": "task 204"},
            },
        },
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert "result" in payload
    content = payload["result"]["content"][0]
    assert content["type"] == "text"
    data = json.loads(content["text"])
    assert data["ok"] is True
    assert data["returned"] == 1


def test_overseer_tool_requires_auth_like_rest_tools(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    client = _api_client(tmp_path, archive)
    response = client.post(
        "/api/mcp/fleet/get_handoff",
        json={"arguments": {}},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Backing-path hygiene: the archive is the backing, nothing else is read.
# ---------------------------------------------------------------------------


def test_tools_never_resolve_task_text_outside_the_archive(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    # A record whose task_file basename does NOT exist under tasks/ reports the
    # miss instead of reading anywhere else.
    broken = {
        "name": "003-broken-ref",
        "task_file": "C:/tmp/does-not-exist-task.txt",
        "workspace": "w",
        "max_rounds": 4,
        "trio": "kimi",
        "note": "n",
    }
    (archive / "queue" / "done" / "003-broken-ref.json").write_text(
        json.dumps(broken), encoding="utf-8"
    )
    result = _dispatch("get_queue_entry", {"name": "003-broken-ref"}, overseer_root=str(archive))
    assert result["ok"] is True
    assert "task_text" not in result