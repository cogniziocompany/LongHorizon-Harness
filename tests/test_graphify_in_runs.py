from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from lh_harness.adapters.claude_code import ClaudeCodeAdapter
from lh_harness.adapters.codex import CodexAdapter
from lh_harness.mcp_profiles import (
    GRAPHIFY_LIVE,
    GRAPHIFY_PREAMBLE,
    GRAPHIFY_PROFILE,
    GRAPHIFY_UAT,
    McpProfile,
    graphify_servers_for_task,
    render_mcp_config,
    resolve_profile,
    with_graphify,
)
from lh_harness.role_prompts import (
    MANAGER_NEXT_CLI,
    build_role_auditor_prompt,
    build_role_executor_prompt,
    build_role_manager_prompt,
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
    key = "test-graphify-key-do-not-use"
    monkeypatch.setenv("LH_HARNESS_MCP_GATEWAY_KEY", key)
    return key


def _profile(name: str, *, servers, read_only: bool = False) -> McpProfile:
    return McpProfile(
        name=name,
        description=f"{name} description",
        servers=servers,
        read_only=read_only,
        source="built-in",
    )


# ---------------------------------------------------------------------------
# graphify_servers_for_task
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ("Refactor the login flow in the web app.", (GRAPHIFY_LIVE,)),
        ("", (GRAPHIFY_LIVE,)),
        ("deploy to CT204", (GRAPHIFY_LIVE, GRAPHIFY_UAT)),
        ("Deploy the chat-uat stack", (GRAPHIFY_LIVE, GRAPHIFY_UAT)),
        ("fix CT100 flake", (GRAPHIFY_LIVE, GRAPHIFY_UAT)),
        ("target chat.uat now", (GRAPHIFY_LIVE, GRAPHIFY_UAT)),
        ("DEPLOY TO ct204", (GRAPHIFY_LIVE, GRAPHIFY_UAT)),
        # Word boundaries: longer numbers must not match.
        ("deploy to CT2040", (GRAPHIFY_LIVE,)),
        ("roll back CT1001", (GRAPHIFY_LIVE,)),
        ("chat-uats are noisy", (GRAPHIFY_LIVE,)),
        ("CT204 and CT1000", (GRAPHIFY_LIVE, GRAPHIFY_UAT)),
    ],
)
def test_graphify_servers_for_task(task: str, expected: tuple[str, ...]) -> None:
    assert graphify_servers_for_task(task) == expected


# ---------------------------------------------------------------------------
# with_graphify
# ---------------------------------------------------------------------------


def test_with_graphify_full_profile_unchanged() -> None:
    full = resolve_profile("cli_executor", run_profile="full", gateway_key="k")
    assert full.servers is None
    merged = with_graphify(full, "deploy to CT204")
    assert merged.servers is None
    assert merged.name == "full"
    assert merged.read_only is False


def test_with_graphify_default_gains_graphify_and_keeps_servers(fake_key, clear_mcp_gateway_env) -> None:
    default = resolve_profile("cli_executor", run_profile="default", gateway_key=fake_key)
    before = default.servers
    assert GRAPHIFY_LIVE not in before
    merged = with_graphify(default, "plain task")
    # Every pre-existing server stays, in order, with graphify appended once.
    assert merged.servers == (*before, GRAPHIFY_LIVE)
    assert merged.read_only == default.read_only
    assert merged.name == default.name


def test_with_graphify_audit_stays_read_only(fake_key, clear_mcp_gateway_env) -> None:
    audit = resolve_profile("cli_auditor", run_profile="audit", gateway_key=fake_key)
    merged = with_graphify(audit, "")
    assert merged.read_only is True
    assert merged.servers == (*audit.servers, GRAPHIFY_LIVE)


def test_with_graphify_no_duplicates() -> None:
    already = _profile("custom", servers=("kb", GRAPHIFY_LIVE, "ssh"), read_only=True)
    merged = with_graphify(already, "plain task")
    assert merged.servers == ("kb", GRAPHIFY_LIVE, "ssh")
    merged_uat = with_graphify(already, "target CT204")
    assert merged_uat.servers == ("kb", GRAPHIFY_LIVE, "ssh", GRAPHIFY_UAT)


def test_with_graphify_builtin_profile_trims_uat_for_plain_tasks(fake_key, clear_mcp_gateway_env) -> None:
    graphify = resolve_profile("cli_executor", run_profile=GRAPHIFY_PROFILE, gateway_key=fake_key)
    assert graphify.servers == (GRAPHIFY_LIVE, GRAPHIFY_UAT)
    assert graphify.read_only is True
    plain = with_graphify(graphify, "refactor the scheduler")
    assert plain.servers == (GRAPHIFY_LIVE,)
    uat = with_graphify(graphify, "sync CT204 fixtures")
    assert uat.servers == (GRAPHIFY_LIVE, GRAPHIFY_UAT)


def test_resolve_profile_graphify_builtin(fake_key, clear_mcp_gateway_env) -> None:
    profile = resolve_profile("manager", run_profile=GRAPHIFY_PROFILE, gateway_key=fake_key)
    assert profile.name == GRAPHIFY_PROFILE
    assert profile.servers == (GRAPHIFY_LIVE, GRAPHIFY_UAT)
    assert profile.read_only is True
    assert profile.source == "built-in"


# ---------------------------------------------------------------------------
# ClaudeCodeAdapter
# ---------------------------------------------------------------------------


def _mcp_config_path(run_dir: Path, role: str) -> Path:
    return run_dir / "harness" / "mcp" / f"{role}.mcp.json"


def test_claude_adapter_defaults_to_graphify_profile(
    clear_mcp_gateway_env, fake_key, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    adapter = ClaudeCodeAdapter(
        role="cli_executor",
        run_id="run-1",
        run_dir=str(run_dir),
    )
    path = _mcp_config_path(run_dir, "cli_executor")
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    server = data["mcpServers"]["cognizioware"]
    assert server["headers"]["x-mcp-servers"] == GRAPHIFY_LIVE
    assert adapter.mcp_profile_resolved["name"] == GRAPHIFY_PROFILE
    assert adapter.mcp_profile_resolved["read_only"] is True
    # The command still carries --strict-mcp-config and the generated path.
    assert "--strict-mcp-config" in adapter.command_template
    assert str(path) in adapter.command_template


def test_claude_adapter_uat_task_adds_uat_alias(
    clear_mcp_gateway_env, fake_key, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ClaudeCodeAdapter(
        role="manager",
        run_id="run-2",
        run_dir=str(run_dir),
        task="Deploy the chat-uat stack",
    )
    data = json.loads(_mcp_config_path(run_dir, "manager").read_text(encoding="utf-8"))
    server = data["mcpServers"]["cognizioware"]
    assert server["headers"]["x-mcp-servers"] == f"{GRAPHIFY_LIVE},{GRAPHIFY_UAT}"


def test_claude_adapter_explicit_profile_keeps_servers_plus_graphify(
    clear_mcp_gateway_env, fake_key, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ClaudeCodeAdapter(
        role="cli_executor",
        run_id="run-3",
        run_dir=str(run_dir),
        mcp_profile="default",
    )
    data = json.loads(_mcp_config_path(run_dir, "cli_executor").read_text(encoding="utf-8"))
    header = data["mcpServers"]["cognizioware"]["headers"]["x-mcp-servers"]
    servers = header.split(",")
    # The default profile's servers are kept and graphify is appended once.
    assert servers[-1] == GRAPHIFY_LIVE
    assert servers.count(GRAPHIFY_LIVE) == 1
    assert "default" not in servers  # profile name is not an alias


def test_claude_adapter_without_key_writes_no_config(
    clear_mcp_gateway_env, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    adapter = ClaudeCodeAdapter(
        role="cli_executor",
        run_id="run-4",
        run_dir=str(run_dir),
    )
    assert not _mcp_config_path(run_dir, "cli_executor").exists()
    assert adapter.mcp_profile_resolved == {}
    # Without a gateway key the command still builds and stays strict.
    assert "--strict-mcp-config" in adapter.command_template
    assert "--mcp-config" not in adapter.command_template


def test_claude_adapter_without_run_id_writes_no_config(
    clear_mcp_gateway_env, fake_key, tmp_path: Path
) -> None:
    adapter = ClaudeCodeAdapter(role="cli_executor")
    assert adapter.mcp_profile_resolved == {}


def test_render_mcp_config_for_graphify_profile(
    clear_mcp_gateway_env, fake_key, tmp_path: Path
) -> None:
    profile = resolve_profile("manager", run_profile=GRAPHIFY_PROFILE, gateway_key=fake_key)
    merged = with_graphify(profile, "sync CT204 fixtures")
    path = render_mcp_config(
        merged,
        run_id="run-5",
        role="manager",
        run_dir=tmp_path,
        session_id="session-5",
    )
    assert path is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    server = data["mcpServers"]["cognizioware"]
    assert server["headers"]["x-mcp-servers"] == f"{GRAPHIFY_LIVE},{GRAPHIFY_UAT}"
    assert server["headers"]["Authorization"] == f"Bearer {fake_key}"


# ---------------------------------------------------------------------------
# CodexAdapter
# ---------------------------------------------------------------------------


def test_codex_adapter_gateway_override_present(
    clear_mcp_gateway_env, fake_key, tmp_path: Path
) -> None:
    codex = tmp_path / "codex"
    codex.write_text('#!/bin/sh\nexit 0\n', encoding="utf-8")
    codex.chmod(0o755)
    import lh_harness.adapters.codex as codex_module

    original = codex_module.resolve_codex_binary
    codex_module.resolve_codex_binary = lambda: str(codex)
    try:
        adapter = CodexAdapter(model=None, gateway_mcp_servers=graphify_servers_for_task("deploy to CT204"))
    finally:
        codex_module.resolve_codex_binary = original

    command = adapter.command_template
    assert "mcp_servers.cognizioware=" in command
    assert 'bearer_token_env_var = "LH_HARNESS_MCP_GATEWAY_KEY"' in command
    assert 'x-mcp-servers' in command
    assert "graphify,graphifyuat" in command
    # The key VALUE must never reach the command line.
    assert fake_key not in command


def test_codex_adapter_gateway_override_absent_without_key(
    clear_mcp_gateway_env, tmp_path: Path
) -> None:
    import lh_harness.adapters.codex as codex_module

    codex = tmp_path / "codex"
    codex.write_text('#!/bin/sh\nexit 0\n', encoding="utf-8")
    codex.chmod(0o755)
    original = codex_module.resolve_codex_binary
    codex_module.resolve_codex_binary = lambda: str(codex)
    try:
        adapter = CodexAdapter(model=None, gateway_mcp_servers=(GRAPHIFY_LIVE,))
    finally:
        codex_module.resolve_codex_binary = original

    assert "mcp_servers.cognizioware=" not in adapter.command_template


def test_codex_adapter_gateway_override_absent_without_servers(
    clear_mcp_gateway_env, fake_key, tmp_path: Path
) -> None:
    import lh_harness.adapters.codex as codex_module

    codex = tmp_path / "codex"
    codex.write_text('#!/bin/sh\nexit 0\n', encoding="utf-8")
    codex.chmod(0o755)
    original = codex_module.resolve_codex_binary
    codex_module.resolve_codex_binary = lambda: str(codex)
    try:
        adapter = CodexAdapter(model=None)
    finally:
        codex_module.resolve_codex_binary = original

    assert "mcp_servers.cognizioware=" not in adapter.command_template


def test_codex_adapter_static_gateway_config_wins(
    clear_mcp_gateway_env, fake_key, tmp_path: Path
) -> None:
    import lh_harness.adapters.codex as codex_module

    codex = tmp_path / "codex"
    codex.write_text('#!/bin/sh\nexit 0\n', encoding="utf-8")
    codex.chmod(0o755)
    original = codex_module.resolve_codex_binary
    codex_module.resolve_codex_binary = lambda: str(codex)
    static_toml = tmp_path / "static-mcp.toml"
    static_toml.write_text(
        '[mcp_servers.cognizioware]\nurl = "https://static.example/mcp/"\n',
        encoding="utf-8",
    )
    try:
        adapter = CodexAdapter(
            model=None,
            mcp_config=str(static_toml),
            gateway_mcp_servers=(GRAPHIFY_LIVE,),
        )
    finally:
        codex_module.resolve_codex_binary = original

    command = adapter.command_template
    # Exactly one override for the gateway server, and it is the static one.
    occurrences = command.count("mcp_servers.cognizioware=")
    assert occurrences == 1
    override_segment = command.split("mcp_servers.cognizioware=", 1)[1]
    assert 'url = "https://static.example/mcp/"' in f"mcp_servers.cognizioware={override_segment}"
    assert "https://static.example/mcp/" in command
    assert "bearer_token_env_var" not in command


# ---------------------------------------------------------------------------
# Role prompts
# ---------------------------------------------------------------------------


def test_manager_prompt_carries_tool_hint_when_set() -> None:
    prompt = build_role_manager_prompt(
        task="Do the work.",
        rounds=[],
        round_index=1,
        tool_hint=GRAPHIFY_PREAMBLE,
    )
    assert GRAPHIFY_PREAMBLE in prompt
    # One preamble line right after the role instructions.
    body = prompt.split("Original task:", 1)[0]
    assert GRAPHIFY_PREAMBLE in body


def test_manager_prompt_without_tool_hint() -> None:
    prompt = build_role_manager_prompt(task="Do the work.", rounds=[], round_index=1)
    assert GRAPHIFY_PREAMBLE not in prompt


def test_executor_prompt_carries_tool_hint_when_set() -> None:
    prompt = build_role_executor_prompt(
        task="Do the work.",
        plan_text="edit files",
        next_step=MANAGER_NEXT_CLI,
        tool_hint=GRAPHIFY_PREAMBLE,
    )
    body = prompt.split("Original task:", 1)[0]
    assert GRAPHIFY_PREAMBLE in body


def test_executor_prompt_without_tool_hint() -> None:
    prompt = build_role_executor_prompt(
        task="Do the work.",
        plan_text="edit files",
        next_step=MANAGER_NEXT_CLI,
    )
    assert GRAPHIFY_PREAMBLE not in prompt


def test_auditor_prompt_unchanged_by_tool_hint_kwarg() -> None:
    prompt = build_role_auditor_prompt(
        task="Do the work.",
        plan_text="edit files",
        executor_output="done",
        next_step=MANAGER_NEXT_CLI,
    )
    # The auditor builder has no tool_hint kwarg at all.
    import inspect

    params = inspect.signature(build_role_auditor_prompt).parameters
    assert "tool_hint" not in params
    # Auditing must not be told to query graphify.
    assert GRAPHIFY_PREAMBLE not in prompt


def test_graphify_preamble_constant() -> None:
    assert GRAPHIFY_PREAMBLE == (
        "Before editing, query graphify (graphify-query_graph, or graphifyuat-* for "
        "UAT targets) for the affected files."
    )
    assert "\n" not in GRAPHIFY_PREAMBLE