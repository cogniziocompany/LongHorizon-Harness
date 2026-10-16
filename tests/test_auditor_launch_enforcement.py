from unittest.mock import AsyncMock, patch

import pytest

from lh_harness.adapters.cli_agent import CommandAgentAdapter
from lh_harness.auditor_launch import AuditorLaunch, active_auditor_launch
from lh_harness.manager import _run_role_episode
from lh_harness.safe_subprocess import AuditorBoundaryUnavailable
from lh_harness.types import EpisodeBudget, EpisodeResult
from lh_harness.types import ExecResult


@pytest.mark.asyncio
async def test_missing_boundary_never_invokes_agent():
    agent = CommandAgentAdapter(command_template="false")
    agent.run_episode = AsyncMock()
    with patch("lh_harness.manager.prepare_auditor_launch", new=AsyncMock(side_effect=AuditorBoundaryUnavailable("unavailable"))):
        result = await _run_role_episode(agent, "audit", object(), EpisodeBudget(), require_auditor_isolation=True)
    agent.run_episode.assert_not_awaited()
    assert result.metadata["auditor_isolation_failed"]


@pytest.mark.asyncio
async def test_unsupported_adapter_never_invoked():
    agent = AsyncMock()
    result = await _run_role_episode(agent, "audit", object(), EpisodeBudget(), require_auditor_isolation=True)
    agent.run_episode.assert_not_awaited()
    assert result.status == "error"


@pytest.mark.asyncio
async def test_manager_context_scoped_to_auditor_episode():
    agent = CommandAgentAdapter(command_template="false")
    observed = []
    async def run(*args, **kwargs):
        observed.append(active_auditor_launch.get())
        return EpisodeResult(status="done")
    agent.run_episode = run
    boundary = AuditorLaunch("/tmp/lh-auditor-launch-fixture")
    with patch("lh_harness.manager.prepare_auditor_launch", new=AsyncMock(return_value=boundary)):
        await _run_role_episode(agent, "audit", object(), EpisodeBudget(), require_auditor_isolation=True)
    assert observed == [boundary]
    assert active_auditor_launch.get() is None


def test_mount_command_has_only_episode_scratch_writable():
    command = AuditorLaunch("/tmp/lh-auditor-launch-fixture").command("printf audit")
    assert "--ro-bind / /" in command
    assert "--cap-drop ALL" in command
    assert "--bind /tmp/lh-auditor-launch-fixture /tmp/lh-auditor-launch-fixture" in command
    assert "--tmpfs /tmp" not in command  # retain read access to staged prompts


@pytest.mark.asyncio
async def test_real_cli_adapter_dispatches_mount_wrapped_command():
    class Env:
        def __init__(self):
            self.commands = []

        async def exec(self, command, **kwargs):
            self.commands.append(command)
            return ExecResult(stdout="audit", stderr="", exit_code=0, duration_ms=1)

    env = Env()
    agent = CommandAgentAdapter(command_template="printf audit", workspace_path="/workspace")
    token = active_auditor_launch.set(AuditorLaunch("/tmp/lh-auditor-launch-fixture"))
    try:
        with patch("lh_harness.adapters.cli_agent.write_remote_text", new=AsyncMock()):
            result = await agent.run_episode("audit", env, EpisodeBudget())
    finally:
        active_auditor_launch.reset(token)
    assert result.status == "done"
    assert len(env.commands) == 1
    assert env.commands[0].startswith("/usr/bin/bwrap ")
    assert "cd /workspace" in env.commands[0]
