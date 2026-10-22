"""The go-to-market manager on queue entries (GTM-MANAGER-G1-a, 2026-10-22).

``gtm_manager`` names the overseer that owns a queue task: ``overseer1`` or
``overseer1-op``.  It is separate from the creator fields (``requested_by`` /
``requester``): other agents create tasks, so the creator is not the manager.

Covered:
- validation on enqueue: absent -> the default manager (``overseer1``), both
  known managers accepted, anything else refused with the enqueue path's own
  validation error (a plain ``ValueError`` -> 422 on REST, ``code: 422`` on
  MCP);
- the file store: the manager is written into the entry JSON, comes back on
  read, a legacy entry written before the field existed reads back as the
  default, and a retry (requeue) keeps the manager;
- the REST surface (``POST /api/queue`` + ``GET /api/queue/{id}``);
- the MCP surface (``harness_enqueue_task``);
- the Postgres surface: the migration text, the column, the written value and
  the read-back (hermetic, through a recording fake connection) plus a live
  round trip when a scratch database is configured.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from lh_harness import pg_queue
from lh_harness.mcp_tools import dispatch, tools_manifest
from lh_harness.queue import (
    DEFAULT_GTM_MANAGER,
    GTM_MANAGERS,
    QueueEntry,
    QueueStore,
    _normalize_request,
)

REPO_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

_NAME_SEQ = {"n": 0}


def _body(**overrides: Any) -> dict[str, Any]:
    _NAME_SEQ["n"] += 1
    body: dict[str, Any] = {
        "name": f"gtm task {_NAME_SEQ['n']}",
        "task": "do the thing",
        "workspace": "./workspace",
        "max_rounds": 5,
        "trio": "kimi",
        "priority": 0,
        "requested_by": "ci",
    }
    body.update(overrides)
    return body


def _store(tmp_path: Path) -> tuple[QueueStore, Path]:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    return QueueStore(root), root


# --- validation --------------------------------------------------------------


def test_absent_gtm_manager_defaults_to_overseer1() -> None:
    """A task created by anyone else belongs to overseer1 until reassigned."""
    params = _normalize_request(_body())
    assert params["gtm_manager"] == DEFAULT_GTM_MANAGER
    assert DEFAULT_GTM_MANAGER == "overseer1"
    assert GTM_MANAGERS == {"overseer1", "overseer1-op"}


@pytest.mark.parametrize("manager", ["overseer1", "overseer1-op"])
def test_known_gtm_manager_is_kept(manager: str) -> None:
    assert _normalize_request(_body(gtm_manager=manager))["gtm_manager"] == manager


@pytest.mark.parametrize(
    "bad",
    ["overseer2", "OVerseer1", "overseer1 op", "overseer", "", 7, True, ["overseer1"]],
)
def test_unknown_gtm_manager_is_refused(bad: Any) -> None:
    """Anything but the two known managers is a validation failure (422)."""
    with pytest.raises(ValueError) as excinfo:
        _normalize_request(_body(gtm_manager=bad))
    assert "gtm_manager" in str(excinfo.value)


def test_null_gtm_manager_is_the_default() -> None:
    """An explicit null means "not specified", not "an unknown manager"."""
    assert _normalize_request(_body(gtm_manager=None))["gtm_manager"] == DEFAULT_GTM_MANAGER


# --- file store --------------------------------------------------------------


def test_file_store_persists_the_gtm_manager(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    default_entry = store.create(_body())
    assert default_entry.gtm_manager == "overseer1"
    owned = store.create(_body(gtm_manager="overseer1-op"))
    assert owned.gtm_manager == "overseer1-op"

    # On-disk read-back, not just the in-memory return value.
    reread = QueueStore(root)
    assert reread.get(default_entry.queue_id).gtm_manager == "overseer1"
    assert reread.get(owned.queue_id).gtm_manager == "overseer1-op"
    raw = json.loads((root / "queue" / f"{owned.queue_id}.json").read_text(encoding="utf-8"))
    assert raw["gtm_manager"] == "overseer1-op"


def test_legacy_entry_without_the_field_reads_back_as_the_default(tmp_path: Path) -> None:
    """An entry written before the field existed belongs to the default manager."""
    entry = QueueEntry.from_dict(
        {
            "queue_id": "q-legacy",
            "name": "legacy",
            "task": "t",
            "workspace": "w",
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
        }
    )
    assert entry.gtm_manager == "overseer1"
    # A NULL value (a Postgres row written before migration 007) too.
    legacy_null = QueueEntry.from_dict(
        {
            "queue_id": "q-legacy-null",
            "name": "legacy",
            "task": "t",
            "workspace": "w",
            "max_rounds": 5,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ci",
            "gtm_manager": None,
        }
    )
    assert legacy_null.gtm_manager == "overseer1"


def test_requeue_keeps_the_gtm_manager(tmp_path: Path) -> None:
    """A retry of a task owned by overseer1-op must not fall back to overseer1."""
    store, root = _store(tmp_path)
    entry = store.create(_body(gtm_manager="overseer1-op"))
    store.mark_failed(entry.queue_id, "provider_rate_limit")
    successor = store.requeue(entry.queue_id, "provider_rate_limit")
    assert successor is not None
    assert successor.gtm_manager == "overseer1-op"
    assert QueueStore(root).get(successor.queue_id).gtm_manager == "overseer1-op"


# --- REST surface ------------------------------------------------------------


def _api_client(tmp_path: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from lh_harness.webapi.server import create_app

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    # TestClient sends Host: testserver; bind_host must agree or the loopback
    # host check rejects every request.
    return TestClient(
        create_app(runs_root=root, auth_token="secret", bind_host="testserver")
    )


def _auth() -> dict:
    return {"Authorization": "Bearer secret"}


def test_api_keeps_the_gtm_manager(tmp_path: Path) -> None:
    client = _api_client(tmp_path)
    response = client.post(
        "/api/queue",
        json={**_body(), "gtm_manager": "overseer1-op"},
        headers=_auth(),
    )
    assert response.status_code == 200
    queue_id = response.json()["queue_id"]

    entry = client.get(f"/api/queue/{queue_id}", headers=_auth()).json()
    assert entry["gtm_manager"] == "overseer1-op"


def test_api_defaults_the_gtm_manager(tmp_path: Path) -> None:
    client = _api_client(tmp_path)
    response = client.post("/api/queue", json=_body(), headers=_auth())
    assert response.status_code == 200
    queue_id = response.json()["queue_id"]

    entry = client.get(f"/api/queue/{queue_id}", headers=_auth()).json()
    assert entry["gtm_manager"] == "overseer1"


def test_api_refuses_an_unknown_gtm_manager_with_422(tmp_path: Path) -> None:
    client = _api_client(tmp_path)
    response = client.post(
        "/api/queue",
        json={**_body(), "gtm_manager": "overseer2"},
        headers=_auth(),
    )
    assert response.status_code == 422
    assert "gtm_manager" in response.json()["detail"]


# --- MCP surface -------------------------------------------------------------


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


def test_mcp_schema_documents_the_gtm_manager() -> None:
    enqueue = next(
        tool for tool in tools_manifest() if tool["name"] == "harness_enqueue_task"
    )
    prop = enqueue["input_schema"]["properties"]["gtm_manager"]
    assert prop["type"] == "string"
    assert prop["enum"] == ["overseer1", "overseer1-op"]
    assert "description" in prop


def test_mcp_tool_keeps_the_gtm_manager(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    result = _enqueue_tool(
        {
            "name": "mcp-gtm",
            "task": "t",
            "workspace": "w",
            "trio": "kimi",
            "requested_by": "mcp-test",
            "gtm_manager": "overseer1-op",
        },
        store,
    )
    assert result["ok"] is True
    read = store.get(result["queue_id"])
    assert read is not None
    assert read.gtm_manager == "overseer1-op"


def test_mcp_tool_defaults_the_gtm_manager(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    result = _enqueue_tool(
        {
            "name": "mcp-gtm-default",
            "task": "t",
            "workspace": "w",
            "trio": "kimi",
            "requested_by": "mcp-test",
        },
        store,
    )
    assert result["ok"] is True
    read = store.get(result["queue_id"])
    assert read is not None
    assert read.gtm_manager == "overseer1"


def test_mcp_tool_refuses_an_unknown_gtm_manager(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    result = _enqueue_tool(
        {
            "name": "mcp-gtm-bad",
            "task": "t",
            "workspace": "w",
            "trio": "kimi",
            "requested_by": "mcp-test",
            "gtm_manager": "overseer2",
        },
        store,
    )
    assert result["ok"] is False
    assert result["code"] == 422
    assert "gtm_manager" in result["error"]


# --- Postgres surface --------------------------------------------------------


def test_migration_007_adds_the_gtm_manager_column_idempotently() -> None:
    path = REPO_MIGRATIONS / "007_harness_queue_gtm_manager.sql"
    sql = path.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS gtm_manager VARCHAR(32) NOT NULL DEFAULT 'overseer1'" in sql
    names = sorted(p.name for p in REPO_MIGRATIONS.glob("*.sql"))
    assert names.index(path.name) > names.index("006_harness_queue_time_limit.sql")


def test_pg_column_list_and_values_carry_the_gtm_manager() -> None:
    columns = pg_queue._QUEUE_COLUMNS
    assert columns[-1] == "gtm_manager"
    entry = QueueEntry(
        queue_id="q-1", name="n", task="t", workspace="w", max_rounds=3, trio="kimi",
        priority=1, requested_by="ci", gtm_manager="overseer1-op",
    )
    values = dict(zip(columns, pg_queue._entry_values(entry)))
    assert len(pg_queue._entry_values(entry)) == len(columns)
    assert values["gtm_manager"] == "overseer1-op"
    # A hand-built entry with a nonsense manager never writes a bad value.
    entry.gtm_manager = "not-a-manager"
    values = dict(zip(columns, pg_queue._entry_values(entry)))
    assert values["gtm_manager"] == "overseer1"


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self.conn = conn
        self._result: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.statements.append((sql, params))
        text = " ".join(sql.split())
        cols = pg_queue._QUEUE_COLUMNS
        if text.startswith("INSERT INTO harness.queue ("):
            row = tuple(params)
            self.conn.rows[row[0]] = row
            self._result = []
        elif text.startswith("UPDATE harness.queue SET"):
            row = tuple(params[: len(cols)])
            self.conn.rows[params[-1]] = row
            self._result = []
        elif text.startswith("SELECT") and "FROM harness.queue WHERE queue_id" in text:
            row = self.conn.rows.get(params[0])
            self._result = [row] if row else []
        elif text.startswith("SELECT") and "FROM harness.queue" in text and "WHERE" not in text:
            self._result = list(self.conn.rows.values())
        else:
            self._result = []

    def fetchone(self) -> Any:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._result)

    def close(self) -> None:
        return None


class _FakeConn:
    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []
        self.rows: dict[str, tuple[Any, ...]] = {}
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture()
def fake_store() -> tuple[pg_queue.PgQueueStore, _FakeConn]:
    conn = _FakeConn()
    with patch("lh_harness.pg_queue._pg_connect", return_value=conn):
        store = pg_queue.PgQueueStore("postgresql://fake/scratch")
    return store, conn


def test_pg_store_round_trips_the_gtm_manager(fake_store) -> None:
    store, conn = fake_store
    entry = store.create(
        {"name": "gtm", "task": "t", "workspace": "/w", "trio": "kimi",
         "requested_by": "ci", "gtm_manager": "overseer1-op"}
    )
    row = dict(zip(pg_queue._QUEUE_COLUMNS, conn.rows[entry.queue_id]))
    assert row["gtm_manager"] == "overseer1-op"
    read = store.get(entry.queue_id)
    assert read is not None
    assert read.gtm_manager == "overseer1-op"

    default_entry = store.create(
        {"name": "gtm-default", "task": "t", "workspace": "/w", "trio": "kimi",
         "requested_by": "ci"}
    )
    assert dict(zip(pg_queue._QUEUE_COLUMNS, conn.rows[default_entry.queue_id]))["gtm_manager"] == "overseer1"
    assert store.get(default_entry.queue_id).gtm_manager == "overseer1"

    # A retry keeps the manager (file-store parity).
    store.mark_failed(entry.queue_id, "provider_quota | run failed")
    successor = store.requeue(entry.queue_id, "provider_quota")
    assert successor is not None
    assert store.get(successor.queue_id).gtm_manager == "overseer1-op"


def test_live_pg_store_round_trips_the_gtm_manager(tmp_path: Path) -> None:
    url = os.environ.get("LH_HARNESS_DB_URL")
    if not url or not url.strip():
        pytest.skip("LH_HARNESS_DB_URL not set; Postgres backend unavailable")
    from lh_harness.pg_queue import PgQueueStore

    try:
        store = PgQueueStore(url)
        store.counts()
    except Exception:
        pytest.skip("could not connect to Postgres backend")
    entry = store.create(
        {"name": "gtm-live", "task": "t", "workspace": "/w", "trio": "kimi",
         "requested_by": "ci", "gtm_manager": "overseer1-op"}
    )
    try:
        read = store.get(entry.queue_id)
        assert read is not None
        assert read.gtm_manager == "overseer1-op"
    finally:
        store.delete(entry.queue_id)
