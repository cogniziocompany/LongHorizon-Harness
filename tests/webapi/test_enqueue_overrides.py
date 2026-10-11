"""LHH-QUEUE-HOTRELOAD R6: per-task model/roles overrides on enqueue.

Queue entries now durably carry an optional per-task ``model`` (one model id
validated against the backend's LIVE model list through the same injectable
fetch seam the R4 routes and R5 tools use) and an optional ``roles`` payload:
a STRING keeps its exact legacy trio-alias behaviour (byte-identical, no
fetch, no audit), a DICT is per-role configuration validated against
create_fleet_run's roles semantics via the supervisor's own
``_normalise_role_configs``.  An override-carrying enqueue writes one
settings-audit row (R4/R5 convention) and is refused all-or-nothing when the
override cannot be validated or audited (unknown model 422; provider down
503; settings store down 503).  Both surfaces are covered end to end: the
file store route (``POST /api/queue``) and the hydrafleet MCP
``harness_enqueue_task``.  Every test mocks the model-list fetch -- no
network, no provider probes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from fastapi.testclient import TestClient

from lh_harness.queue import KNOWN_REQUEST_KEYS, QueueStore
from lh_harness.settings_store import SettingsRuntime, SettingsStore
from lh_harness.mcp_tools import dispatch, tools_manifest
from lh_harness.webapi.server import create_app
from lh_harness.config import _caller_secret_env

from .caller_util import caller_args, clear_caller_secrets, install_caller_secret

_FAKE_CATALOG = {
    "agents": [],
    "models": {
        "claude_code": [
            {"id": "opus", "label": "opus · default", "availability": "detected"},
            {"id": "sonnet", "label": "sonnet", "availability": "detected"},
        ],
        "codex": [{"id": "codex-mini", "label": "codex-mini", "availability": "detected"}],
    },
    "model_discovery": {},
}


def _fake_model_catalog(*, force: bool = False, **_kwargs: Any) -> dict[str, Any]:
    return json.loads(json.dumps(_FAKE_CATALOG))


class _FetchSpy:
    """A model_fetch seam that fails the test when the path must not fetch."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return _fake_model_catalog()


def _broken_fetch(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("provider unreachable")


def _queue_store(tmp_path: Path) -> QueueStore:
    root = tmp_path / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return QueueStore(root)


def _settings(tmp_path: Path) -> tuple[SettingsStore, SettingsRuntime]:
    store = SettingsStore(tmp_path / "settings.db", "k" * 40)
    return store, SettingsRuntime(store=store, error=None)


def _audit_rows(store: SettingsStore) -> list[dict[str, Any]]:
    return store.audit(10)


_BASE_BODY: dict[str, Any] = {
    "name": "n",
    "task": "t",
    "workspace": "w",
    "requested_by": "api-test",
}


def _body(**overrides: Any) -> dict[str, Any]:
    return {**_BASE_BODY, **overrides}


# ----------------------------------------------------------------------
# File store surface
# ----------------------------------------------------------------------


def test_model_override_accepted_persisted_and_audited(tmp_path: Path) -> None:
    store = _queue_store(tmp_path)
    settings_store, runtime = _settings(tmp_path)
    entry = store.create(
        _body(trio="glm", model="sonnet"),
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )
    assert entry.model == "sonnet"
    assert entry.roles is None
    # Durable: the override survives a fresh read from disk.
    read = QueueStore(tmp_path / "runs").get(entry.queue_id)
    assert read is not None
    assert read.model == "sonnet"
    # One audit row, R4/R5 shape: action/name/before/after, keyed to the actor.
    rows = _audit_rows(settings_store)
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "enqueue"
    assert row["name"] == f"queue_entry:{entry.queue_id}"
    assert row["actor"] == "api-test"
    assert row["detail"]["before"] is None
    assert row["detail"]["after"] == {"model": "sonnet"}


def test_unknown_model_refused_no_entry_no_audit(tmp_path: Path) -> None:
    store = _queue_store(tmp_path)
    settings_store, runtime = _settings(tmp_path)
    with pytest.raises(ValueError, match="unknown model 'zzz' in the live model list"):
        store.create(
            _body(model="zzz"), model_fetch=_fake_model_catalog, settings_runtime=runtime
        )
    assert store.list() == []
    assert _audit_rows(settings_store) == []


def test_provider_down_refused_503_no_entry_no_audit(tmp_path: Path) -> None:
    store = _queue_store(tmp_path)
    settings_store, runtime = _settings(tmp_path)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        store.create(
            _body(model="sonnet"), model_fetch=_broken_fetch, settings_runtime=runtime
        )
    assert excinfo.value.status_code == 503
    assert "live model list is unavailable" in str(excinfo.value.detail)
    assert store.list() == []
    assert _audit_rows(settings_store) == []


def test_settings_store_down_refuses_overrides_503(tmp_path: Path) -> None:
    store = _queue_store(tmp_path)
    broken = SettingsRuntime(store=None, error="the settings store is not configured")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        store.create(
            _body(model="sonnet"), model_fetch=_fake_model_catalog, settings_runtime=broken
        )
    assert excinfo.value.status_code == 503
    assert store.list() == []
    # Roles-only carries the same audit obligation.
    with pytest.raises(HTTPException):
        store.create(
            _body(roles={"executor": {"model": "codex-mini"}}),
            model_fetch=_broken_fetch,  # must not even be consulted
            settings_runtime=broken,
        )
    assert store.list() == []


def test_legacy_roles_string_alias_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _queue_store(tmp_path)
    # The legacy path must not consult the live list or the settings store at
    # all: both seams raise if touched.
    monkeypatch.setattr(
        "lh_harness.settings_store.active_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("settings store must not be read")),
    )
    spy = _FetchSpy()
    entry = store.create(
        _body(roles="kimi"),
        model_fetch=spy,
        settings_runtime=SettingsRuntime(store=None, error="unconfigured"),
    )
    assert entry.trio == "kimi"
    assert entry.roles is None
    assert entry.model is None
    assert spy.calls == 0
    # Byte-identical legacy precedence: an explicit null roles shadows trio.
    other = store.create(_body(roles=None, trio="glm"), model_fetch=spy)
    assert other.trio == ""
    assert spy.calls == 0


def test_roles_dict_validated_cleaned_persisted(tmp_path: Path) -> None:
    store = _queue_store(tmp_path)
    settings_store, runtime = _settings(tmp_path)
    spy = _FetchSpy()
    entry = store.create(
        _body(
            trio="kimi",
            roles={
                "executor": {"agent": "claude_code", "model": " opus ", "reasoning_effort": "high"},
                "manager": {},  # no pins: dropped
                "auditor": {"model": "  "},  # blank-only: dropped
            },
        ),
        model_fetch=spy,
        settings_runtime=runtime,
    )
    assert entry.trio == "kimi"
    # Cleaned pins only (strings trimmed, empty entries dropped): the trio
    # defaults keep flowing at launch into every field the dict did not pin.
    assert entry.roles == {
        "executor": {"agent": "claude_code", "model": "opus", "reasoning_effort": "high"}
    }
    # No live-list validation for role models -- only the per-task model
    # override fetches.
    assert spy.calls == 0
    read = QueueStore(tmp_path / "runs").get(entry.queue_id)
    assert read is not None
    assert read.roles == entry.roles
    rows = _audit_rows(settings_store)
    assert len(rows) == 1
    assert rows[0]["detail"]["after"] == {"roles": entry.roles}
    assert "model" not in rows[0]["detail"]["after"]


def test_empty_roles_dict_normalizes_to_no_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _queue_store(tmp_path)
    monkeypatch.setattr(
        "lh_harness.settings_store.active_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("settings store must not be read")),
    )
    entry = store.create(_body(roles={}, trio="glm"), model_fetch=_FetchSpy())
    assert entry.roles is None
    assert entry.trio == "glm"


@pytest.mark.parametrize(
    ("roles", "fragment"),
    [
        ({"boss": {}}, "unknown role configuration: boss"),
        ({"executor": "nope"}, "roles.executor must be an object"),
        ({"executor": {"bogus": 1}}, "unknown roles.executor field: bogus"),
        (
            {"executor": {"agent": "gpt9"}},
            "roles.executor.agent must be codex, claude_code, deepseek_harness, or opencode",
        ),
        ({"executor": {"model": 5}}, "roles.executor.model must be a string"),
        (
            {"executor": {"reasoning_effort": "!??"}},
            "roles.executor.reasoning_effort reasoning effort may only contain",
        ),
    ],
)
def test_malformed_roles_dict_refused_422(tmp_path: Path, roles: Any, fragment: str) -> None:
    store = _queue_store(tmp_path)
    with pytest.raises(ValueError, match=fragment.replace("(", "\\(").replace(")", "\\)")):
        store.create(_body(roles=roles), model_fetch=_FetchSpy())
    assert store.list() == []


def test_legacy_non_string_roles_still_refused_422(tmp_path: Path) -> None:
    store = _queue_store(tmp_path)
    with pytest.raises(ValueError, match="trio/roles must be a string"):
        store.create(_body(roles=["kimi"]), model_fetch=_FetchSpy())


def test_model_shape_errors_422(tmp_path: Path) -> None:
    store = _queue_store(tmp_path)
    spy = _FetchSpy()
    with pytest.raises(ValueError, match="model must be a string"):
        store.create(_body(model=5), model_fetch=spy)
    with pytest.raises(ValueError, match="model is too long"):
        store.create(_body(model="m" * 300), model_fetch=spy)
    # A blank model is no override: no fetch, no audit, entry written.
    entry = store.create(_body(model="  "), model_fetch=spy)
    assert entry.model is None
    assert spy.calls == 0


def test_absent_overrides_fully_backward_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _queue_store(tmp_path)
    monkeypatch.setattr(
        "lh_harness.settings_store.active_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("settings store must not be read")),
    )
    spy = _FetchSpy()
    entry = store.create(_body(trio="kimi", dedup_key="dk"), model_fetch=spy)
    assert entry.model is None
    assert entry.roles is None
    assert spy.calls == 0
    # Dedup resubmission with overrides on the file but a valid repeat ask:
    # the second enqueue hits the dedup "change nothing" path; no audit row is
    # due at all on this store (no settings store was ever reachable).
    again = store.create(_body(trio="kimi", dedup_key="dk"), model_fetch=spy)
    assert again.queue_id == entry.queue_id
    assert spy.calls == 0


def test_requeue_preserves_overrides_as_deep_copy(tmp_path: Path) -> None:
    store = _queue_store(tmp_path)
    settings_store, runtime = _settings(tmp_path)
    entry = store.create(
        _body(trio="kimi", model="sonnet", roles={"auditor": {"model": "codex-mini"}}),
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )
    store.mark_failed(entry.queue_id, "boom")
    successor = store.requeue(entry.queue_id, "boom")
    assert successor is not None
    assert successor.model == "sonnet"
    assert successor.roles == {"auditor": {"model": "codex-mini"}}
    # Deep copy: mutating the retry must not mutate the original entry.
    assert successor.roles is not entry.roles
    assert successor.roles["auditor"] is not entry.roles["auditor"]
    read = store.get(successor.queue_id)
    assert read is not None
    assert read.model == "sonnet"
    assert read.roles == successor.roles


# ----------------------------------------------------------------------
# MCP surface (hydrafleet ``harness_enqueue_task``)
# ----------------------------------------------------------------------

_CALLER_CONFIGS: dict[str, dict[str, Any]] = {
    "overseer": {
        "secret_env": _caller_secret_env("overseer"),
        "tools": ["harness_enqueue_task"],
        "max_entries_per_hour": None,
        "max_rounds_clamp": None,
        "rest_run_control": False,
    }
}


@pytest.fixture(autouse=True)
def _caller_secrets() -> Any:
    install_caller_secret("overseer")
    yield
    clear_caller_secrets()


def _mcp_enqueue(
    tmp_path: Path,
    arguments: dict[str, Any],
    *,
    runtime: SettingsRuntime,
    model_fetch: Any = None,
) -> tuple[dict[str, Any], QueueStore]:
    store = _queue_store(tmp_path)
    result = dispatch(
        "harness_enqueue_task",
        caller_args(dict(arguments), "overseer"),
        queue_store=store,
        registry=None,
        supervisor=None,
        auth_token=None,
        request_token=None,
        caller_configs=dict(_CALLER_CONFIGS),
        settings_runtime=runtime,
        model_fetch=_fake_model_catalog if model_fetch is None else model_fetch,
    )
    return result, store


def test_mcp_forwards_model_and_legacy_roles_string(tmp_path: Path) -> None:
    settings_store, runtime = _settings(tmp_path)
    result, store = _mcp_enqueue(
        tmp_path, _body(roles="glm", model="sonnet"), runtime=runtime
    )
    assert result["ok"] is True
    entry = store.get(result["queue_id"])
    assert entry is not None
    # The string roles reaches the store: the legacy alias sets the trio.
    assert entry.trio == "glm"
    assert entry.model == "sonnet"
    rows = _audit_rows(settings_store)
    assert len(rows) == 1
    # The audit actor is the VERIFIED caller, matching the R5 convention.
    assert rows[0]["actor"] == "overseer"


def test_mcp_forwards_roles_dict_and_model(tmp_path: Path) -> None:
    settings_store, runtime = _settings(tmp_path)
    result, store = _mcp_enqueue(
        tmp_path,
        _body(
            trio="kimi",
            model="opus",
            roles={"executor": {"model": "codex-mini", "agent": "codex"}},
        ),
        runtime=runtime,
    )
    assert result["ok"] is True
    entry = store.get(result["queue_id"])
    assert entry is not None
    assert entry.trio == "kimi"
    assert entry.model == "opus"
    assert entry.roles == {"executor": {"agent": "codex", "model": "codex-mini"}}
    rows = _audit_rows(settings_store)
    assert len(rows) == 1
    assert rows[0]["detail"]["after"] == {"model": "opus", "roles": entry.roles}


def test_mcp_unknown_model_422(tmp_path: Path) -> None:
    _, runtime = _settings(tmp_path)
    result, store = _mcp_enqueue(tmp_path, _body(model="zzz"), runtime=runtime)
    assert result["ok"] is False
    assert result["code"] == 422
    assert "unknown model 'zzz' in the live model list" in result["error"]
    assert store.list() == []


def test_mcp_provider_down_503(tmp_path: Path) -> None:
    _, runtime = _settings(tmp_path)
    result, store = _mcp_enqueue(
        tmp_path, _body(model="sonnet"), runtime=runtime, model_fetch=_broken_fetch
    )
    assert result["ok"] is False
    assert result["code"] == 503
    assert "live model list is unavailable" in result["error"]
    assert store.list() == []


def test_mcp_settings_store_down_refuses_overrides_503(tmp_path: Path) -> None:
    broken = SettingsRuntime(store=None, error="the settings store is not configured")
    result, store = _mcp_enqueue(tmp_path, _body(model="sonnet"), runtime=broken)
    assert result["ok"] is False
    assert result["code"] == 503
    assert store.list() == []


def test_mcp_malformed_roles_dict_422(tmp_path: Path) -> None:
    _, runtime = _settings(tmp_path)
    result, store = _mcp_enqueue(
        tmp_path, _body(roles={"boss": {}}), runtime=runtime
    )
    assert result["ok"] is False
    assert result["code"] == 422
    assert "unknown role configuration: boss" in result["error"]
    assert store.list() == []


def test_mcp_legacy_enqueue_needs_neither_seam(tmp_path: Path) -> None:
    # No overrides: an unconfigured settings runtime is fine and the fetch is
    # never made.
    result, store = _mcp_enqueue(
        tmp_path,
        _body(trio="kimi"),
        runtime=SettingsRuntime(store=None, error="unconfigured"),
        model_fetch=_broken_fetch,
    )
    assert result["ok"] is True
    entry = store.get(result["queue_id"])
    assert entry is not None
    assert entry.model is None
    assert entry.roles is None


def test_mcp_unknown_fields_still_rejected_400(tmp_path: Path) -> None:
    _, runtime = _settings(tmp_path)
    result, _ = _mcp_enqueue(tmp_path, _body(rolez="glm"), runtime=runtime)
    assert result["ok"] is False
    assert result["code"] == 400
    assert "rolez" in result["error"]


def test_mcp_schema_advertises_model_and_roles() -> None:
    assert "model" in KNOWN_REQUEST_KEYS
    enqueue = next(tool for tool in tools_manifest() if tool["name"] == "harness_enqueue_task")
    props = enqueue["input_schema"]["properties"]
    assert props["model"]["type"] == "string"
    assert {item["type"] for item in props["roles"]["anyOf"]} == {"string", "object"}


# ----------------------------------------------------------------------
# Web surface (POST /api/queue)
# ----------------------------------------------------------------------


def _api_client(tmp_path: Path) -> TestClient:
    root = tmp_path / "runs"
    root.mkdir(parents=True, exist_ok=True)
    # TestClient sends Host: testserver; bind_host must agree or the loopback
    # host check rejects every request.
    return TestClient(
        create_app(runs_root=root, auth_token="secret", bind_host="testserver")
    )


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


@pytest.fixture
def web_seams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SettingsStore:
    """Offline seams for the web surface, patched at the lazy-import points.

    The web route passes no seams of its own (server.py untouched this
    round), exactly like production: the store resolves the default catalog
    and the process-wide settings runtime at call time.  Patching the module
    attributes here intercepts both defaults -- no network, no real store.
    """

    settings_store = SettingsStore(tmp_path / "settings.db", "k" * 40)
    monkeypatch.setattr(
        "lh_harness.settings_store.active_runtime",
        lambda: SettingsRuntime(store=settings_store, error=None),
    )
    monkeypatch.setattr(
        "lh_harness.model_catalog.discover_model_catalog", _fake_model_catalog
    )
    return settings_store


def test_web_enqueue_model_accepted_persisted_audited(
    tmp_path: Path, web_seams: SettingsStore
) -> None:
    client = _api_client(tmp_path)
    response = client.post("/api/queue", json=_body(trio="glm", model="sonnet"), headers=_auth())
    assert response.status_code == 200, response.text
    queue_id = response.json()["queue_id"]
    entry = QueueStore(tmp_path / "runs").get(queue_id)
    assert entry is not None
    assert entry.model == "sonnet"
    rows = _audit_rows(web_seams)
    assert len(rows) == 1
    assert rows[0]["actor"] == "api-test"  # unscoped: the client's requested_by
    assert rows[0]["detail"]["after"] == {"model": "sonnet"}


def test_web_unknown_model_422(tmp_path: Path, web_seams: SettingsStore) -> None:
    client = _api_client(tmp_path)
    response = client.post("/api/queue", json=_body(model="zzz"), headers=_auth())
    assert response.status_code == 422
    assert "unknown model 'zzz' in the live model list" in response.json()["detail"]
    assert QueueStore(tmp_path / "runs").list() == []
    assert _audit_rows(web_seams) == []


def test_web_provider_down_503(tmp_path: Path, web_seams: SettingsStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lh_harness.model_catalog.discover_model_catalog", _broken_fetch)
    client = _api_client(tmp_path)
    response = client.post("/api/queue", json=_body(model="sonnet"), headers=_auth())
    # The 503 HTTPException propagates to FastAPI's own handler: the route
    # answers with the R4/R5 taxonomy without any server.py change.
    assert response.status_code == 503
    assert "live model list is unavailable" in response.json()["detail"]
    assert QueueStore(tmp_path / "runs").list() == []


def test_web_roles_dict_accepted_persisted(tmp_path: Path, web_seams: SettingsStore) -> None:
    client = _api_client(tmp_path)
    response = client.post(
        "/api/queue",
        json=_body(trio="kimi", roles={"executor": {"model": "codex-mini"}}),
        headers=_auth(),
    )
    assert response.status_code == 200, response.text
    entry = QueueStore(tmp_path / "runs").get(response.json()["queue_id"])
    assert entry is not None
    assert entry.roles == {"executor": {"model": "codex-mini"}}
    rows = _audit_rows(web_seams)
    assert len(rows) == 1
    assert rows[0]["detail"]["after"] == {"roles": entry.roles}


def test_web_malformed_roles_dict_422(tmp_path: Path, web_seams: SettingsStore) -> None:
    client = _api_client(tmp_path)
    response = client.post("/api/queue", json=_body(roles={"boss": {}}), headers=_auth())
    assert response.status_code == 422
    assert "unknown role configuration: boss" in response.json()["detail"]
    assert QueueStore(tmp_path / "runs").list() == []


def test_web_legacy_roles_string_unchanged(tmp_path: Path, web_seams: SettingsStore) -> None:
    client = _api_client(tmp_path)
    response = client.post("/api/queue", json=_body(roles="glm"), headers=_auth())
    assert response.status_code == 200, response.text
    entry = QueueStore(tmp_path / "runs").get(response.json()["queue_id"])
    assert entry is not None
    assert entry.trio == "glm"
    assert entry.roles is None
    assert entry.model is None
    # Neither the live list nor the override audit log was involved.
    assert _audit_rows(web_seams) == []
