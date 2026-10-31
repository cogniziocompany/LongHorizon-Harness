from unittest.mock import AsyncMock, patch

import pytest

from lh_harness.adapters.cli_agent import CommandAgentAdapter
from lh_harness.auditor_launch import AuditorLaunch, active_auditor_launch
from lh_harness.manager import _run_role_episode
from lh_harness.safe_subprocess import (
    AuditorBoundary,
    AuditorBoundaryKind,
    AuditorBoundaryUnavailable,
)
from lh_harness.types import EpisodeBudget, EpisodeResult
from lh_harness.types import ExecResult


def _fixture_boundary(launcher: str = "/usr/bin/unshare") -> AuditorBoundary:
    """Build a real-looking boundary for in-process tests.

    The harness only consumes ``launcher`` (the path returned by
    ``shutil.which``) and reads ``probe_uid`` for diagnostics; tests
    don't actually fork the launcher.
    """
    return AuditorBoundary(
        kind=AuditorBoundaryKind.USER_NAMESPACE,
        launcher=launcher,
        probe_uid=1,
        rationale="fixture boundary for in-process tests",
    )


@pytest.mark.asyncio
async def test_missing_boundary_never_invokes_agent():
    agent = CommandAgentAdapter(command_template="false")
    agent.run_episode = AsyncMock()
    with patch("lh_harness.manager.prepare_auditor_launch", new=AsyncMock(side_effect=AuditorBoundaryUnavailable("unavailable"))):
        result = await _run_role_episode(agent, "audit", object(), EpisodeBudget(), require_auditor_isolation=True)
    agent.run_episode.assert_not_awaited()
    assert result.metadata["auditor_isolation_failed"]
    assert result.metadata["missing_prerequisite"] in {
        "read_only_mount_boundary",
        "auditor_uid_boundary",
    }


@pytest.mark.asyncio
async def test_unsupported_adapter_still_runs_when_boundary_present():
    """A non-CommandAgentAdapter (e.g. a fake used by unit tests) is no
    longer refused before the audit subprocess is launched. The
    Manager->adapter->spawn path enforces the UID/mount boundary in
    :func:`prepare_auditor_launch`; the adapter shape is the agent
    author's concern. Tests that build their own ``AgentAdapter`` can
    exercise the format-repair timeout/repair paths without needing to
    implement ``bwrap``-aware command construction."""
    agent = AsyncMock()
    boundary = AuditorLaunch("/tmp/lh-auditor-launch-fixture", _fixture_boundary())
    with patch("lh_harness.manager.prepare_auditor_launch", new=AsyncMock(return_value=boundary)):
        await _run_role_episode(agent, "audit", object(), EpisodeBudget(), require_auditor_isolation=True)
    agent.run_episode.assert_awaited()


@pytest.mark.asyncio
async def test_manager_context_scoped_to_auditor_episode():
    agent = CommandAgentAdapter(command_template="false")
    observed = []
    async def run(*args, **kwargs):
        observed.append(active_auditor_launch.get())
        return EpisodeResult(status="done")
    agent.run_episode = run
    boundary = AuditorLaunch("/tmp/lh-auditor-launch-fixture", _fixture_boundary())
    with patch("lh_harness.manager.prepare_auditor_launch", new=AsyncMock(return_value=boundary)):
        await _run_role_episode(agent, "audit", object(), EpisodeBudget(), require_auditor_isolation=True)
    assert observed == [boundary]
    assert active_auditor_launch.get() is None


def test_mount_command_has_only_episode_scratch_writable():
    command = AuditorLaunch(
        "/tmp/lh-auditor-launch-fixture", _fixture_boundary()
    ).command("printf audit")
    assert "--ro-bind / /" in command
    assert "--cap-drop ALL" in command
    assert "--bind /tmp/lh-auditor-launch-fixture /tmp/lh-auditor-launch-fixture" in command
    assert "--tmpfs /tmp" not in command  # retain read access to staged prompts


def test_command_wrapper_layers_boundary_launcher_ahead_of_bwrap():
    """The real manager->adapter->spawn path needs the auditor process to
    enter the uid boundary (``unshare -U``) BEFORE the mount-bound
    ``bwrap`` runs. Otherwise ``bwrap``'s own ``--unshare-user`` would
    only get a same-uid mapping and the kernel check would still be
    permission-blind (the operator-rejected P1 trap)."""
    command = AuditorLaunch(
        "/tmp/lh-auditor-launch-fixture", _fixture_boundary("/usr/bin/unshare")
    ).command("printf audit")
    bwrap_idx = command.index("/usr/bin/bwrap ")
    unshare_idx = command.index("/usr/bin/unshare")
    assert unshare_idx < bwrap_idx, (
        "boundary launcher must precede bwrap so the auditor enters the "
        "uid boundary before the mount boundary"
    )
    # And the launcher argv is split so unshare sees -U as a real argv
    # element, not a flag glued onto the launcher name.
    assert "/usr/bin/unshare -U" in command


def test_command_wrapper_uses_setpriv_when_separate_uid_available():
    """If the deployment grants CAP_SETUID the boundary comes from
    ``setpriv`` instead of ``unshare``. The real launch argv must
    match :func:`launch_auditor_subprocess` so detection and launch
    agree on the boundary shape."""
    command = AuditorLaunch(
        "/tmp/lh-auditor-launch-fixture",
        _fixture_boundary("/usr/bin/setpriv"),
    ).command("printf audit")
    assert "/usr/bin/setpriv --reuid=65534 --regid=65534 --clear-groups" in command
    # setpriv precedes bwrap too.
    assert command.index("/usr/bin/setpriv") < command.index("/usr/bin/bwrap")


def test_command_wrapper_unknown_launcher_refuses_silently_falling_back():
    """An unknown boundary launcher means we cannot wrap the command at
    all; chmod-deny-lists are advisory only and would let the auditor
    write the snapshot's ``.git`` (round_003 P1). Refuse rather than
    silently proceed."""
    bad = AuditorBoundary(
        kind=AuditorBoundaryKind.USER_NAMESPACE,
        launcher="/usr/bin/nosuch-isolation-helper",
        probe_uid=1,
        rationale="fixture unknown launcher",
    )
    launch = AuditorLaunch("/tmp/lh-auditor-launch-fixture", bad)
    with pytest.raises(AuditorBoundaryUnavailable):
        launch.command("printf audit")


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
    token = active_auditor_launch.set(
        AuditorLaunch("/tmp/lh-auditor-launch-fixture", _fixture_boundary())
    )
    try:
        with patch("lh_harness.adapters.cli_agent.write_remote_text", new=AsyncMock()):
            result = await agent.run_episode("audit", env, EpisodeBudget())
    finally:
        active_auditor_launch.reset(token)
    assert result.status == "done"
    assert len(env.commands) == 1
    # Layered boundaries: unshare -U then bwrap.
    assert env.commands[0].startswith("/usr/bin/unshare -U /usr/bin/bwrap ")
    assert "cd /workspace" in env.commands[0]

