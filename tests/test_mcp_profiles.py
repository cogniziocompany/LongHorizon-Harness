from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from lh_harness.adapters.claude_permissions import _AUDITOR_ROLES
from lh_harness.mcp_profiles import (
    MCP_LAN_GATEWAY_URL,
    MCP_PROD_GATEWAY_URL,
    McpProfile,
    _load_user_profiles,
    _normalise_gateway_url,
    gateway_configured,
    list_available_profiles,
    redact_mcp_config_for_display,
    render_mcp_config,
    resolve_profile,
)


@pytest.fixture
def clear_mcp_gateway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LH_HARNESS_MCP_GATEWAY_KEY",
        "LH_HARNESS_MCP_GATEWAY_URL",
        "LH_HARNESS_MCP_GATEWAY_HEADERS_JSON",
        "LH_HARNESS_WEB_DEFAULT_MCP_PROFILE",
        "LH_HARNESS_WEB_DEFAULT_MANAGER_MCP_PROFILE",
        "LH_HARNESS_WEB_DEFAULT_EXECUTOR_MCP_PROFILE",
        "LH_HARNESS_WEB_DEFAULT_AUDITOR_MCP_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = "test-key-do-not-use"
    monkeypatch.setenv("LH_HARNESS_MCP_GATEWAY_KEY", key)
    return key


@pytest.fixture
def fake_state_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv("LH_HARNESS_STATE_ROOT", str(root))
    return root


def test_builtin_profiles_read_only_semantics(clear_mcp_gateway_env) -> None:
    none = resolve_profile("manager", run_profile="none", gateway_key="test")
    assert none.name == "none"
    assert none.read_only is True

    audit = resolve_profile("cli_auditor", gateway_key="test")
    assert audit.name == "audit"
    assert audit.read_only is True

    default = resolve_profile("manager", run_profile="default", gateway_key="test")
    assert default.name == "default"
    assert default.read_only is False

    ops = resolve_profile("cli_executor", run_profile="ops", gateway_key="test")
    assert ops.name == "ops"
    assert ops.read_only is False

    full = resolve_profile("gui_executor", run_profile="full", gateway_key="test")
    assert full.name == "full"
    assert full.read_only is False
    assert full.servers is None


def test_resolve_default_for_roles(clear_mcp_gateway_env, fake_key) -> None:
    manager = resolve_profile("manager")
    assert manager.name == "default"
    assert manager.reason == "built-in default"

    executor = resolve_profile("cli_executor")
    assert executor.name == "default"

    for role in _AUDITOR_ROLES:
        profile = resolve_profile(role)
        assert profile.name == "audit"
        assert profile.read_only is True


def test_precedence_role_override_wins(clear_mcp_gateway_env, fake_key) -> None:
    profile = resolve_profile(
        "manager",
        run_profile="none",
        role_profile="ops",
        config={"mcp_profile": "full", "roles": {"manager": {"mcp_profile": "default"}}},
    )
    assert profile.name == "ops"
    assert "role override" in profile.reason


def test_precedence_run_override_wins(clear_mcp_gateway_env, fake_key) -> None:
    profile = resolve_profile(
        "manager",
        run_profile="ops",
        config={"mcp_profile": "full", "roles": {"manager": {"mcp_profile": "default"}}},
    )
    assert profile.name == "ops"
    assert "run-level override" in profile.reason


def test_precedence_config_role_wins(clear_mcp_gateway_env, fake_key) -> None:
    profile = resolve_profile(
        "manager",
        config={"mcp_profile": "full", "roles": {"manager": {"mcp_profile": "default"}}},
    )
    assert profile.name == "default"
    assert "config [run.roles.manager]" in profile.reason


def test_precedence_config_run_wins(clear_mcp_gateway_env, fake_key) -> None:
    profile = resolve_profile(
        "cli_executor",
        config={"mcp_profile": "ops"},
    )
    assert profile.name == "ops"
    assert "config run.mcp_profile" in profile.reason


def test_precedence_env_per_role_wins(clear_mcp_gateway_env, fake_key, monkeypatch) -> None:
    monkeypatch.setenv("LH_HARNESS_WEB_DEFAULT_EXECUTOR_MCP_PROFILE", "audit")
    monkeypatch.setenv("LH_HARNESS_WEB_DEFAULT_MCP_PROFILE", "ops")
    profile = resolve_profile("cli_executor")
    assert profile.name == "audit"
    assert "LH_HARNESS_WEB_DEFAULT_EXECUTOR_MCP_PROFILE" in profile.reason


def test_precedence_env_global_wins(clear_mcp_gateway_env, fake_key, monkeypatch) -> None:
    monkeypatch.setenv("LH_HARNESS_WEB_DEFAULT_MCP_PROFILE", "full")
    profile = resolve_profile("cli_executor")
    assert profile.name == "full"
    assert "LH_HARNESS_WEB_DEFAULT_MCP_PROFILE" in profile.reason


def test_missing_gateway_key_resolves_every_profile_to_none(clear_mcp_gateway_env) -> None:
    for requested in ("default", "ops", "full", "manager_mcp_profile"):
        profile = resolve_profile(
            "cli_executor",
            run_profile=requested if requested != "manager_mcp_profile" else None,
            config={"mcp_profile": "ops"} if requested == "manager_mcp_profile" else None,
        )
        assert profile.name == "none"
        assert profile.reason == "gateway key not configured"


def test_resolve_unknown_profile_falls_back_to_default(clear_mcp_gateway_env, fake_key) -> None:
    profile = resolve_profile("manager", run_profile="does-not-exist")
    assert profile.name == "default"
    assert "unknown profile" in profile.reason


def test_auditor_read_only_refusal(clear_mcp_gateway_env, fake_key) -> None:
    for role in _AUDITOR_ROLES:
        with pytest.raises(ValueError, match="auditor roles require a read-only profile"):
            resolve_profile(role, run_profile="ops")


def test_auditor_read_only_allowed_by_config(clear_mcp_gateway_env, fake_key) -> None:
    profile = resolve_profile("cli_auditor", run_profile="ops", allow_auditor_write_mcp=True)
    assert profile.name == "ops"
    assert profile.read_only is False


def test_gateway_url_normalisation() -> None:
    assert _normalise_gateway_url(None) == MCP_PROD_GATEWAY_URL
    assert _normalise_gateway_url("") == MCP_PROD_GATEWAY_URL
    assert _normalise_gateway_url("lan") == MCP_LAN_GATEWAY_URL
    assert _normalise_gateway_url("https://example.com/mcp") == "https://example.com/mcp/"
    assert _normalise_gateway_url("https://example.com/mcp/") == "https://example.com/mcp/"


def test_render_mcp_config_none_returns_none(clear_mcp_gateway_env, tmp_path) -> None:
    profile = resolve_profile("manager", run_profile="none", gateway_key="test")
    assert render_mcp_config(profile, run_id="r", role="manager", run_dir=tmp_path, session_id="s") is None


def test_render_mcp_config_requires_key(clear_mcp_gateway_env, tmp_path) -> None:
    profile = resolve_profile("manager", run_profile="default", gateway_key="test")
    with pytest.raises(RuntimeError, match="LH_HARNESS_MCP_GATEWAY_KEY"):
        render_mcp_config(profile, run_id="r", role="manager", run_dir=tmp_path, session_id="s", gateway_key=None)


def test_render_mcp_config_file_mode_and_headers(clear_mcp_gateway_env, fake_key, tmp_path) -> None:
    profile = resolve_profile("manager", run_profile="audit")
    path = render_mcp_config(
        profile,
        run_id="r",
        role="manager",
        run_dir=tmp_path,
        session_id="r.round_001.manager",
    )
    assert path is not None
    assert path == tmp_path / "harness" / "mcp" / "manager.mcp.json"
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600

    data = json.loads(path.read_text())
    server = data["mcpServers"]["cognizioware"]
    assert server["type"] == "http"
    assert server["url"] == MCP_PROD_GATEWAY_URL
    headers = server["headers"]
    assert headers["Authorization"] == f"Bearer {fake_key}"
    assert headers["x-mcp-servers"] == ",".join(profile.servers)
    assert headers["X-LH-Session"] == "r.round_001.manager"


def test_render_mcp_config_full_has_empty_allow_list(clear_mcp_gateway_env, fake_key, tmp_path) -> None:
    profile = resolve_profile("manager", run_profile="full")
    path = render_mcp_config(
        profile,
        run_id="r",
        role="manager",
        run_dir=tmp_path,
        session_id="s",
    )
    data = json.loads(path.read_text())
    assert data["mcpServers"]["cognizioware"]["headers"]["x-mcp-servers"] == ""


def test_render_mcp_config_extra_headers_json(clear_mcp_gateway_env, fake_key, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LH_HARNESS_MCP_GATEWAY_HEADERS_JSON", '{"X-Custom": "yes", "bad": []}')
    profile = resolve_profile("manager", run_profile="audit")
    path = render_mcp_config(
        profile,
        run_id="r",
        role="manager",
        run_dir=tmp_path,
        session_id="s",
    )
    data = json.loads(path.read_text())
    headers = data["mcpServers"]["cognizioware"]["headers"]
    assert headers["X-Custom"] == "yes"
    assert "bad" not in headers


def test_redact_authorization() -> None:
    data = {
        "mcpServers": {
            "cognizioware": {
                "headers": {
                    "Authorization": "Bearer secret",
                    "x-mcp-servers": "audit",
                }
            }
        }
    }
    redacted = redact_mcp_config_for_display(data)
    assert redacted["mcpServers"]["cognizioware"]["headers"]["Authorization"] == "***REDACTED***"
    assert redacted["mcpServers"]["cognizioware"]["headers"]["x-mcp-servers"] == "audit"


def test_user_profile_loader(clear_mcp_gateway_env, fake_state_root, fake_key) -> None:
    (fake_state_root / "mcp_profiles.json").write_text(
        json.dumps(
            {
                "custom": {
                    "description": "Custom user profile",
                    "servers": "a, b",
                    "read_only": False,
                }
            }
        ),
        encoding="utf-8",
    )
    profile = resolve_profile("manager", run_profile="custom")
    assert profile.name == "custom"
    assert profile.description == "Custom user profile"
    assert profile.servers == ("a", "b")
    assert profile.read_only is False
    assert profile.source == "user"


def test_project_profile_loader(clear_mcp_gateway_env, tmp_path, fake_key) -> None:
    config_path = tmp_path / ".lh-harness" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '\n'.join([
            '[run.mcp_profiles.custom]',
            'description = "Project custom"',
            'servers = ["x", "y"]',
            'read_only = true',
        ]),
        encoding="utf-8",
    )
    profile = resolve_profile("manager", run_profile="custom", project_config_path=config_path)
    assert profile.name == "custom"
    assert profile.description == "Project custom"
    assert profile.servers == ("x", "y")
    assert profile.read_only is True
    assert profile.source == "project"


def test_list_available_profiles_excludes_key(clear_mcp_gateway_env, tmp_path, fake_key) -> None:
    config_path = tmp_path / ".lh-harness" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '[run.mcp_profiles.custom]\ndescription = "Project custom"\nservers = ["x"]\n',
        encoding="utf-8",
    )
    profiles = list_available_profiles(project_config_path=config_path)
    names = {p["name"] for p in profiles}
    assert "none" in names
    assert "custom" in names
    for profile in profiles:
        assert "Authorization" not in str(profile)
        assert "key" not in str(profile).lower()


def test_gateway_configured_reflects_env(clear_mcp_gateway_env, monkeypatch) -> None:
    assert gateway_configured() is False
    monkeypatch.setenv("LH_HARNESS_MCP_GATEWAY_KEY", "x")
    assert gateway_configured() is True
