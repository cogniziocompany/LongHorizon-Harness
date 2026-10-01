"""Task 300: the structured requester identity block on queue entries."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.mcp_tools import dispatch, tools_manifest
from lh_harness.queue import QueueEntry, QueueStore, UnknownQueueFieldError
from lh_harness.webapi.server import create_app

SESSION = "b5038555-9e10-4bb3-a81e-aa03a602d445"

REQUESTER: dict[str, Any] = {
    "kind": "ai",
    "name": "test",
    "host": "ci",
    "cwd": "/tmp",
    "agent": "claude",
    "session_id": SESSION,
}


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "t300",
        "task": "do something",
        "workspace": "./workspace",
        "trio": "kimi",
        "requested_by": "ci",
        "requester": dict(REQUESTER),
    }
    body.update(overrides)
    return body


@pytest.fixture
def strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LH_HARNESS_QUEUE_REQUESTER", "strict")


@pytest.fixture
def store(tmp_path: Path) -> QueueStore:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    return QueueStore(root)


# --- strict mode --------------------------------------------------------------


def test_strict_rejects_missing_block(strict: None, store: QueueStore) -> None:
    body = _body()
    del body["requester"]
    with pytest.raises(ValueError, match=r"requester is required \(kind, name, host"):
        store.create(body)


def test_strict_is_the_default_when_env_unset(monkeypatch: pytest.MonkeyPatch, store: QueueStore) -> None:
    monkeypatch.delenv("LH_HARNESS_QUEUE_REQUESTER", raising=False)
    body = _body()
    del body["requester"]
    with pytest.raises(ValueError, match="requester is required"):
        store.create(body)


@pytest.mark.parametrize("missing", ["agent", "session_id", "cwd"])
def test_ai_block_requires_agent_session_cwd(strict: None, store: QueueStore, missing: str) -> None:
    block = dict(REQUESTER)
    del block[missing]
    with pytest.raises(ValueError, match=rf"requester\.{missing} is required when kind is ai"):
        store.create(_body(requester=block))


@pytest.mark.parametrize("missing", ["kind", "name", "host"])
def test_block_requires_kind_name_host(strict: None, store: QueueStore, missing: str) -> None:
    block = dict(REQUESTER)
    del block[missing]
    with pytest.raises(ValueError, match=rf"requester\.{missing}"):
        store.create(_body(requester=block))


def test_user_block_needs_no_session(strict: None, store: QueueStore) -> None:
    entry = store.create(_body(requester={"kind": "user", "name": "paxton", "host": "PTAIT09"}))
    assert entry.requester is not None
    assert entry.requester["kind"] == "user"
    assert entry.requester["session_id"] is None
    assert entry.requester["notify"] == "none"


@pytest.mark.parametrize("kind", ["unknown", "robot", ""])
def test_rejects_bad_kind(strict: None, store: QueueStore, kind: str) -> None:
    with pytest.raises(ValueError, match=r"requester\.kind must be one of"):
        store.create(_body(requester={**REQUESTER, "kind": kind}))


@pytest.mark.parametrize("key", ["legacy", "observed_addr", "verified_caller"])
def test_rejects_store_set_keys_from_caller(strict: None, store: QueueStore, key: str) -> None:
    with pytest.raises(ValueError, match=rf"requester\.{key} is set by the store"):
        store.create(_body(requester={**REQUESTER, key: "x"}))


def test_rejects_unknown_subkey_by_name(strict: None, store: QueueStore) -> None:
    with pytest.raises(UnknownQueueFieldError, match="colour"):
        store.create(_body(requester={**REQUESTER, "colour": "blue"}))


def test_rejects_bad_agent_token(strict: None, store: QueueStore) -> None:
    with pytest.raises(ValueError, match=r"requester\.agent"):
        store.create(_body(requester={**REQUESTER, "agent": "bad agent!"}))


def test_rejects_non_object_block(strict: None, store: QueueStore) -> None:
    with pytest.raises(ValueError, match="requester must be an object"):
        store.create(_body(requester="paxton"))


# --- normalization and round trip ----------------------------------------------


def test_full_block_round_trips(strict: None, store: QueueStore) -> None:
    entry = store.create(_body(requester={**REQUESTER, "notify": "sendmessage:overseer1", "address": "192.168.21.50"}))
    block = entry.requester
    assert block is not None
    assert block["session_ref"] == SESSION[:6]
    assert block["notify"] == "sendmessage:overseer1"
    assert block["address"] == "192.168.21.50"
    reloaded = store.get(entry.queue_id)
    assert reloaded is not None
    assert reloaded.requester == block
    assert QueueEntry.from_dict(json.loads(json.dumps(entry.to_dict()))).requester == block


def test_supplied_session_ref_is_kept(strict: None, store: QueueStore) -> None:
    entry = store.create(_body(requester={**REQUESTER, "session_ref": "de022f"}))
    assert entry.requester is not None
    assert entry.requester["session_ref"] == "de022f"


def test_old_entry_without_requester_loads_as_none(store: QueueStore) -> None:
    entry = store.create(_body())
    path = store._path(entry.queue_id)  # noqa: SLF001 - simulate a pre-task-300 file
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("requester")
    path.write_text(json.dumps(data), encoding="utf-8")
    reloaded = store.get(entry.queue_id)
    assert reloaded is not None
    assert reloaded.requester is None


def test_requested_by_kept_when_supplied(strict: None, store: QueueStore) -> None:
    entry = store.create(_body(requested_by="paxton via claude"))
    assert entry.requested_by == "paxton via claude"


def test_requested_by_derived_when_omitted(strict: None, store: QueueStore) -> None:
    body = _body()
    del body["requested_by"]
    entry = store.create(body)
    assert entry.requested_by == f"test@ci [{SESSION[:6]}]"


def test_requested_by_still_required_without_block_in_legacy(store: QueueStore) -> None:
    body = _body()
    del body["requested_by"]
    del body["requester"]
    with pytest.raises(ValueError, match="requested_by is required"):
        store.create(body)


# --- legacy mode --------------------------------------------------------------


def test_legacy_synthesizes_block(store: QueueStore) -> None:
    body = _body(requested_by="hydra")
    del body["requester"]
    entry = store.create(body)
    assert entry.requested_by == "hydra"
    assert entry.requester == {
        "kind": "unknown",
        "name": "hydra",
        "host": "unknown",
        "notify": "none",
        "legacy": True,
    }


def test_legacy_still_validates_supplied_block(store: QueueStore) -> None:
    with pytest.raises(ValueError, match=r"requester\.agent is required"):
        store.create(_body(requester={"kind": "ai", "name": "x", "host": "y", "cwd": "/", "session_id": "s"}))


def test_retry_successor_keeps_requester(strict: None, store: QueueStore) -> None:
    entry = store.create(_body())
    assert entry.requester is not None
    # A requeued successor is a new entry for the same request; it carries the
    # same requester so notifications still reach the original asker.
    source = Path(__file__).resolve().parents[2] / "src" / "lh_harness" / "queue.py"
    assert "requester=entry.requester" in source.read_text(encoding="utf-8")


# --- REST ---------------------------------------------------------------------


def _client(tmp_path: Path) -> TestClient:
    root = tmp_path / "api-runs"
    root.mkdir(parents=True)
    return TestClient(create_app(runs_root=root, auth_token="secret", bind_host="testserver"))


AUTH = {"Authorization": "Bearer secret"}


def test_rest_strict_missing_block_is_422(strict: None, tmp_path: Path) -> None:
    client = _client(tmp_path)
    body = _body()
    del body["requester"]
    response = client.post("/api/queue", json=body, headers=AUTH)
    assert response.status_code == 422
    assert "requester is required" in response.text


def test_rest_stamps_observed_addr(strict: None, tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/queue", json=_body(), headers=AUTH)
    assert response.status_code == 200, response.text
    queue_id = response.json()["queue_id"]
    listing = client.get("/api/queue", headers=AUTH).json()
    entry = next(item for item in listing["entries"] if item["queue_id"] == queue_id)
    assert entry["requester"]["observed_addr"] == "testclient"
    assert entry["requester"]["agent"] == "claude"


def test_rest_rejects_caller_supplied_observed_addr(strict: None, tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/queue", json=_body(requester={**REQUESTER, "observed_addr": "1.2.3.4"}), headers=AUTH)
    assert response.status_code == 422


def test_rest_legacy_accepts_missing_block(tmp_path: Path) -> None:
    client = _client(tmp_path)
    body = _body()
    del body["requester"]
    response = client.post("/api/queue", json=body, headers=AUTH)
    assert response.status_code == 200, response.text


# --- MCP ----------------------------------------------------------------------


def _mcp(store: QueueStore, arguments: dict[str, Any]) -> dict[str, Any]:
    return dispatch(
        "harness_enqueue_task",
        arguments,
        queue_store=store,
        registry=None,
        supervisor=None,
        auth_token=None,
        request_token=None,
    )


def test_mcp_schema_declares_requester_object() -> None:
    tool = next(t for t in tools_manifest() if t["name"] == "harness_enqueue_task")
    schema = tool["input_schema"]["properties"]["requester"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) >= {"kind", "name", "host", "cwd", "agent", "session_id", "notify"}
    assert "legacy" not in schema["properties"]
    assert "observed_addr" not in schema["properties"]


def test_mcp_matches_rest_in_strict_mode(strict: None, store: QueueStore) -> None:
    body = _body()
    missing = dict(body)
    del missing["requester"]
    rejected = _mcp(store, missing)
    assert rejected["ok"] is False
    assert rejected["code"] == 422
    assert "requester is required" in rejected["error"]
    accepted = _mcp(store, body)
    assert accepted["ok"] is True, accepted
    entry = store.get(accepted["queue_id"])
    assert entry is not None
    assert entry.requester is not None
    assert entry.requester["session_id"] == SESSION
    assert "observed_addr" not in entry.requester


def test_mcp_legacy_synthesizes(store: QueueStore) -> None:
    body = _body(requested_by="openwebui")
    del body["requester"]
    result = _mcp(store, body)
    assert result["ok"] is True, result
    entry = store.get(result["queue_id"])
    assert entry is not None
    assert entry.requester is not None
    assert entry.requester["legacy"] is True


# --- launcher -----------------------------------------------------------------


def test_queue_launched_event_carries_requester(strict: None, tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("_t300_launcher_helpers", Path(__file__).with_name("test_launcher.py"))
    assert spec is not None and spec.loader is not None
    tl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tl)

    from lh_harness.launcher import Launcher
    from lh_harness.queue import default_queue_config

    _root, launcher_store, supervisor = tl._fixture(tmp_path)
    launcher = Launcher(supervisor, launcher_store, queue_config=default_queue_config())
    body = tl._base_entry(trio="kimi", priority=1, workspace="./w-req")
    body["requester"] = dict(REQUESTER)
    entry = launcher_store.create(body)
    asyncio.run(launcher.tick())
    launched = launcher_store.get(entry.queue_id)
    assert launched is not None and launched.run_id is not None
    events_path = supervisor._run_logs_dir(launched.run_id) / "role_orchestration" / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if "queue.launched" in line]
    assert events
    assert '"session_id": "' + SESSION + '"' in json.dumps(events[0])
