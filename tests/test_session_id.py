from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from lh_harness.adapters.claude_code import (
    ClaudeCodeAdapter,
    _inject_session_header_into_mcp_config,
    episode_session_id,
)
from lh_harness.environment.base import Environment
from lh_harness.types import EpisodeBudget, EpisodeResult, ExecResult


class FakeEnvironment:
    """In-memory Environment that records the executed command but never runs a CLI."""

    def __init__(self, tmp_dir: Path) -> None:
        self._tmp_dir = tmp_dir
        self.commands: list[str] = []

    @property
    def staging_dir(self) -> Path:
        return self._tmp_dir

    async def upload(self, local_path: str, remote_path: str) -> None:
        dst = Path(remote_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(Path(local_path).read_bytes())

    async def download(self, remote_path: str, local_path: str) -> None:
        Path(local_path).write_bytes(Path(remote_path).read_bytes())

    async def exec(
        self,
        command: str,
        timeout: int = 30,
        tee_path: str | None = None,
    ) -> ExecResult:
        self.commands.append(command)
        return ExecResult(
            stdout='{"type": "final"}\n',
            stderr="",
            exit_code=0,
            duration_ms=1,
            termination_reason="completed",
        )


@pytest.mark.parametrize(
    ("run_id", "label", "role", "expected"),
    [
        ("run-1", "round_001_manager_raw_trajectory", "manager", "run-1.round_001.manager"),
        ("run-1", "round_042_cli_executor_raw_trajectory", "cli_executor", "run-1.round_042.cli_executor"),
        ("run-1", "episode_agent", "manager", "run-1.round_unknown.manager"),
        ("run-1", "round_1_manager_raw_trajectory", "manager", "run-1.round_1.manager"),
    ],
)
def test_episode_session_id_format(
    run_id: str, label: str, role: str, expected: str
) -> None:
    assert episode_session_id(run_id, label, role) == expected


def test_episode_session_id_no_run_id() -> None:
    assert episode_session_id(None, "round_001_manager_raw_trajectory", "manager") == "unknown"


def test_episode_session_id_determinism() -> None:
    """The session id is derived from run/round/role, never random."""
    first = episode_session_id("run-2", "round_003_cli_executor_raw_trajectory", "cli_executor")
    second = episode_session_id("run-2", "round_003_cli_executor_raw_trajectory", "cli_executor")
    assert first == second
    assert first == "run-2.round_003.cli_executor"


@pytest.mark.parametrize(
    "role",
    ["manager", "cli_executor", "cli_auditor"],
)
def test_episode_env_contains_session_tag(role: str) -> None:
    adapter = ClaudeCodeAdapter(role=role, run_id="run-1")
    env = adapter.episode_env("round_001_manager_raw_trajectory")
    assert "ANTHROPIC_CUSTOM_HEADERS" in env
    header = env["ANTHROPIC_CUSTOM_HEADERS"]
    assert header.startswith("x-litellm-tags:")
    expected_session = episode_session_id("run-1", "round_001_manager_raw_trajectory", role)
    assert f",lh-session/{expected_session}" in header


def test_episode_env_empty_without_run_id() -> None:
    adapter = ClaudeCodeAdapter(role="manager")
    assert adapter.episode_env("round_001_manager_raw_trajectory") == {}


@pytest.mark.parametrize(
    "role",
    ["manager", "cli_executor", "cli_auditor"],
)
def test_run_episode_metadata_contains_session_id(role: str, tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter(
        role=role,
        run_id="run-test",
        workspace_path=str(tmp_path / "workspace"),
        prompt_dir=str(tmp_path / "prompts"),
    )
    env = FakeEnvironment(tmp_path / "staging")
    (tmp_path / "workspace").mkdir()
    budget = EpisodeBudget(max_duration_seconds=1)
    result = asyncio.run(
        adapter.run_episode("test prompt", env, budget, live_trajectory_path=None)
    )
    expected = episode_session_id("run-test", "episode_agent", role)
    assert result.metadata["lh_session_id"] == expected
    # The command template also carries the tags for proxy-side observability.
    assert "lh-session/" in result.metadata["command"]


def test_mcp_config_session_header_injected(tmp_path: Path) -> None:
    config_path = tmp_path / "harness" / "mcp" / "manager.mcp.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "cognizioware": {
                        "type": "http",
                        "url": "https://example.com/mcp/",
                        "headers": {
                            "Authorization": "Bearer test-placeholder-token",
                            "x-mcp-servers": "audit",
                        },
                    }
                }
            }
        )
    )
    _inject_session_header_into_mcp_config(
        "run-1",
        "round_001_manager_raw_trajectory",
        "manager",
        str(tmp_path),
    )
    data = json.loads(config_path.read_text())
    headers = data["mcpServers"]["cognizioware"]["headers"]
    assert headers["X-LH-Session"] == "run-1.round_001.manager"
    # Existing Authorization value is preserved unchanged; redaction is handled
    # by the MCP profile renderer (slice B), not this header-injection hook.
    assert headers["Authorization"] == "Bearer test-placeholder-token"


def test_mcp_config_injection_no_file_is_silent(tmp_path: Path) -> None:
    # No config file exists; function must return without raising.
    _inject_session_header_into_mcp_config(
        "run-1",
        "round_001_manager_raw_trajectory",
        "manager",
        str(tmp_path),
    )


def test_auditor_command_template_has_session_tag_and_no_unsetenvvar(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter(role="cli_auditor", run_id="run-a")
    template = adapter.command_template
    assert "--unsetenvvar" not in template
    # Per-episode env carries the session tag; the static template does not.
    env = adapter.episode_env("round_001_cli_auditor_raw_trajectory")
    assert "ANTHROPIC_CUSTOM_HEADERS" in env
    assert "lh-session/run-a.round_001.cli_auditor" in env["ANTHROPIC_CUSTOM_HEADERS"]
