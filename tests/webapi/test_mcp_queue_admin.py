"""LHH-QUEUE-HOTRELOAD R5: hydrafleet MCP tools for queue hot-reload.

``list_trios``, ``set_trio_model``, ``set_trio_capacity`` and
``refresh_models`` dispatch through ``lh_harness.mcp_tools.dispatch`` and
reuse the R4 admin-route machinery (``_clean_trio_row`` validation, the
settings store's ``_db``/``_audit`` writes, the injectable live-model fetch
seam), so their validation, refusals and per-change before/after audit rows
match GET/PUT /api/queue/trios + /api/queue/capacity + POST
/api/models/refresh exactly.  Every test mocks the model-list fetch: no
network, no provider probes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("cryptography")

from lh_harness.mcp_tools import _QUEUE_ADMIN_TOOL_NAMES, dispatch, tools_manifest
from lh_harness.settings_store import SettingsRuntime, SettingsStore

from .caller_util import caller_args, clear_caller_secrets, install_caller_secret
from lh_harness.config import _caller_secret_env

# Minimal in-test caller table (secret NAME only; the value lives in the
# per-process environment).  Passed non-empty so ``caller_configs_or_defaults``
# never falls back to the on-disk project config loader -- these tests must
# not read any real node configuration.
_CALLER_CONFIGS: dict[str, dict[str, Any]] = {
    "overseer": {
        "secret_env": _caller_secret_env("overseer"),
        "tools": [],
        "max_entries_per_hour": None,
        "max_rounds_clamp": None,
        "rest_run_control": False,
    }
}

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


class _World:
    """A seeded settings store plus a dispatch shim (no HTTP, no network)."""

    def __init__(self, tmp_path: Path, *, with_store: bool = True) -> None:
        self.store = SettingsStore(tmp_path / "settings.db", "k" * 40) if with_store else None
        if self.store is not None:
            seeded = self.store.seed_queue_defaults(
                {"glm": {"agent": "claude_code", "model": "opus", "mcp_profile": "default"}},
                {"glm_max": 2},
            )
            assert seeded == {"trios": ["glm"], "capacity": ["glm"]}
        self.rt = SettingsRuntime(
            store=self.store,
            error=None if with_store else "the settings store is not configured",
        )

    def call(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        caller: str | None = "overseer",
        caller_configs: Any = "default",
        model_fetch: Any = None,
    ) -> dict[str, Any]:
        args = dict(arguments or {})
        if caller is not None:
            args = caller_args(args, caller)
        return dispatch(
            tool,
            args,
            queue_store=None,
            registry=None,
            supervisor=None,
            auth_token=None,
            request_token=None,
            caller_configs=dict(_CALLER_CONFIGS) if caller_configs == "default" else caller_configs,
            settings_runtime=self.rt,
            model_fetch=_fake_model_catalog if model_fetch is None else model_fetch,
        )


@pytest.fixture(autouse=True)
def _caller_secrets() -> Any:
    install_caller_secret("overseer")
    yield
    clear_caller_secrets()


def test_manifest_exposes_the_four_tools_under_their_literal_names() -> None:
    specs = {tool["name"]: tool for tool in tools_manifest()}
    assert _QUEUE_ADMIN_TOOL_NAMES == {"list_trios", "set_trio_model", "set_trio_capacity", "refresh_models"}
    assert _QUEUE_ADMIN_TOOL_NAMES <= set(specs)
    for name in sorted(_QUEUE_ADMIN_TOOL_NAMES):
        assert specs[name]["input_schema"].get("additionalProperties") is False
        assert specs[name]["description"].strip()
    model_props = specs["set_trio_model"]["input_schema"]["properties"]
    assert set(model_props) == {"trio", "model"}
    capacity_props = specs["set_trio_capacity"]["input_schema"]["properties"]
    assert set(capacity_props) == {"trio", "max_concurrent"}


def test_an_unknown_tool_name_still_404s(tmp_path: Path) -> None:
    world = _World(tmp_path)
    result = world.call("list_trios_typo", {})
    assert result["ok"] is False and result["code"] == 404


def test_list_trios_returns_the_settings_db_state(tmp_path: Path) -> None:
    world = _World(tmp_path)
    result = world.call("list_trios")
    assert result["ok"] is True, result
    trios = {row["name"]: row for row in result["trios"]}
    assert trios["glm"]["agent"] == "claude_code" and trios["glm"]["model"] == "opus"
    assert trios["glm"]["mcp_profile"] == "default" and trios["glm"]["enabled"] is True
    assert result["capacity"] == {"glm": 2}
    assert world.call("list_trios", {"bogus": 1})["code"] == 400


def test_set_trio_model_updates_the_store_and_audits_like_the_rest_admin_api(tmp_path: Path) -> None:
    world = _World(tmp_path)
    assert world.store is not None
    audit_before = len(world.store.audit())
    result = world.call("set_trio_model", {"trio": "glm", "model": "sonnet"})
    assert result["ok"] is True, result
    row = {t["name"]: t for t in result["trios"]}["glm"]
    assert row["model"] == "sonnet"
    # Only the model changed; the trio keeps its other stored fields, and the
    # write is visible to a wholly separate store connection (the way the
    # launcher's per-pass reads see it with no restart).
    stored = {t["name"]: t for t in world.store.queue_trios()}["glm"]
    assert stored["model"] == "sonnet" and stored["agent"] == "claude_code"
    assert stored["mcp_profile"] == "default" and stored["enabled"] is True
    assert stored["updated_by"] == "overseer"
    mirror = SettingsStore(world.store.path, "k" * 40)
    assert {t["name"]: t for t in mirror.queue_trios()}["glm"]["model"] == "sonnet"
    entries = world.store.audit()
    assert len(entries) == audit_before + 1
    top = entries[0]
    assert top["action"] == "set" and top["name"] == "queue_trio:glm" and top["actor"] == "overseer"
    assert top["detail"]["before"] == {
        "agent": "claude_code",
        "model": "opus",
        "mcp_profile": "default",
        "enabled": True,
    }
    assert top["detail"]["after"]["model"] == "sonnet"


def test_set_trio_model_to_the_same_value_is_a_noop_without_audit(tmp_path: Path) -> None:
    world = _World(tmp_path)
    assert world.store is not None
    audit_before = world.store.audit()
    result = world.call("set_trio_model", {"trio": "glm", "model": "opus"})
    assert result["ok"] is True, result
    assert world.store.audit() == audit_before


def test_set_trio_model_refuses_an_unknown_model_and_changes_nothing(tmp_path: Path) -> None:
    world = _World(tmp_path)
    assert world.store is not None
    before_trios = world.store.queue_trios()
    before_audit = world.store.audit()
    result = world.call("set_trio_model", {"trio": "glm", "model": "not-a-real-model"})
    assert result["ok"] is False and result["code"] == 422, result
    assert "not-a-real-model" in result["error"] and "unknown model" in result["error"]
    assert world.store.queue_trios() == before_trios and world.store.audit() == before_audit


def test_set_trio_model_refuses_when_the_live_model_list_is_unavailable(tmp_path: Path) -> None:
    world = _World(tmp_path)
    assert world.store is not None

    def boom(**kwargs: Any) -> None:
        raise RuntimeError("gateway down")

    before = world.store.queue_trios()
    result = world.call("set_trio_model", {"trio": "glm", "model": "sonnet"}, model_fetch=boom)
    assert result["ok"] is False and result["code"] == 503, result
    assert world.store.queue_trios() == before


def test_set_trio_model_unknown_trio_and_bad_arguments(tmp_path: Path) -> None:
    world = _World(tmp_path)
    assert world.store is not None
    before = world.store.queue_trios()
    missing = world.call("set_trio_model", {"trio": "nope", "model": "opus"})
    assert missing["ok"] is False and missing["code"] == 404 and "nope" in missing["error"]
    for args in ({"trio": "", "model": "opus"}, {"model": ""}, {"trio": "glm"}, {}):
        result = world.call("set_trio_model", args)
        assert result["ok"] is False and result["code"] == 422, (args, result)
    assert world.call("set_trio_model", {"trio": "glm", "model": "opus", "extra": 1})["code"] == 400
    assert world.store.queue_trios() == before


def test_set_trio_capacity_updates_and_audits_per_change(tmp_path: Path) -> None:
    world = _World(tmp_path)
    assert world.store is not None
    audit_before = len(world.store.audit())
    result = world.call("set_trio_capacity", {"trio": "glm", "max_concurrent": 5})
    assert result["ok"] is True and result["capacity"] == {"glm": 5}, result
    assert world.store.queue_capacity() == {"glm": 5}
    entries = world.store.audit()
    assert len(entries) == audit_before + 1
    top = entries[0]
    assert top["action"] == "set" and top["name"] == "queue_capacity:glm" and top["actor"] == "overseer"
    assert top["detail"] == {"after": 5, "before": 2}
    mirror = SettingsStore(world.store.path, "k" * 40)
    assert mirror.queue_capacity() == {"glm": 5}


@pytest.mark.parametrize("value", [-1, 3.5, True, "5", None])
def test_set_trio_capacity_refuses_bad_values(tmp_path: Path, value: Any) -> None:
    world = _World(tmp_path)
    assert world.store is not None
    before_audit = world.store.audit()
    result = world.call("set_trio_capacity", {"trio": "glm", "max_concurrent": value})
    assert result["ok"] is False and result["code"] == 422, result
    assert "non-negative integer" in result["error"]
    assert world.store.queue_capacity() == {"glm": 2} and world.store.audit() == before_audit


def test_set_trio_capacity_refuses_an_unknown_trio(tmp_path: Path) -> None:
    world = _World(tmp_path)
    assert world.store is not None
    before_audit = world.store.audit()
    result = world.call("set_trio_capacity", {"trio": "zzz", "max_concurrent": 1})
    assert result["ok"] is False and result["code"] == 422 and "unknown trio" in result["error"]
    assert world.store.queue_capacity() == {"glm": 2} and world.store.audit() == before_audit
    assert world.call("set_trio_capacity", {"trio": "glm", "max_concurrent": 0})["ok"] is True
    assert world.store.queue_capacity() == {"glm": 0}


def test_refresh_models_uses_the_injected_fetch_with_force(tmp_path: Path) -> None:
    world = _World(tmp_path)
    assert world.store is not None
    calls: list[bool] = []

    def fake(*, force: bool = False, **kwargs: Any) -> dict[str, Any]:
        calls.append(force)
        return json.loads(json.dumps(_FAKE_CATALOG))

    before_audit = world.store.audit()
    result = world.call("refresh_models", model_fetch=fake)
    assert result["ok"] is True and result["refreshed"] is True, result
    assert calls == [True]
    assert result["counts"] == {"claude_code": 2, "codex": 1}
    assert result["models"]["claude_code"][0]["id"] == "opus"
    # Refreshing is not a state change: no audit row, no store mutation.
    assert world.store.audit() == before_audit
    broken = world.call("refresh_models", model_fetch=lambda **kw: 1 / 0)
    assert broken["ok"] is False and broken["code"] == 503


def test_queue_admin_tools_require_a_verified_caller_when_scoping_is_on(tmp_path: Path) -> None:
    world = _World(tmp_path)
    assert world.store is not None
    before_trios = world.store.queue_trios()
    before_audit = world.store.audit()
    unsigned_read = world.call("list_trios", caller=None)
    assert unsigned_read["ok"] is False and unsigned_read["code"] == 401
    unsigned_write = world.call("set_trio_model", {"trio": "glm", "model": "sonnet"}, caller=None)
    assert unsigned_write["ok"] is False and unsigned_write["code"] == 401
    assert world.store.queue_trios() == before_trios and world.store.audit() == before_audit


def test_scoping_off_keeps_the_bearer_only_contract_and_audits_channel_mcp(tmp_path: Path) -> None:
    world = _World(tmp_path)
    assert world.store is not None
    result = world.call("set_trio_capacity", {"trio": "glm", "max_concurrent": 7}, caller=None, caller_configs=None)
    assert result["ok"] is True, result
    assert world.store.queue_capacity() == {"glm": 7}
    assert world.store.audit()[0]["actor"] == "mcp"


def test_each_tool_answers_503_without_a_settings_store(tmp_path: Path) -> None:
    world = _World(tmp_path, with_store=False)
    for tool, args in (
        ("list_trios", {}),
        ("set_trio_model", {"trio": "glm", "model": "opus"}),
        ("set_trio_capacity", {"trio": "glm", "max_concurrent": 1}),
        ("refresh_models", {}),
    ):
        result = world.call(tool, args)
        assert result["ok"] is False and result["code"] == 503, (tool, result)
