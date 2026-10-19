"""Reporting slice B: five-key LLM request metadata, attached at one site.

Every LLM request a role agent makes must carry exactly five reporting keys
(run_id, queue_id, trio, role, round) on the LiteLLM `metadata` carrier, and
nothing else about the request decoration may change.  Claude Code owns its
HTTP body, so the single attach point (ClaudeCodeAdapter.episode_env) uses the
langfuse_* request-header convention LiteLLM merges into the logged
generation metadata; the deviation from the body field is documented in the
PR body.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from pathlib import Path

import pytest

from lh_harness.adapters.claude_code import ClaudeCodeAdapter, episode_session_id
from lh_harness.types import EpisodeBudget, ExecResult


REPORTING_KEYS = ("run_id", "queue_id", "trio", "role", "round")

EXPECTED_HEADERS = {
    "run_id": "run-1",
    "queue_id": "q-abc123",
    "trio": "zaiq",
    "role": "manager",
    "round": "round_001",
}


def _header_dict(adapter: ClaudeCodeAdapter, label: str) -> dict[str, str]:
    """Parse the episode env's ANTHROPIC_CUSTOM_HEADERS into {name: value}."""
    value = adapter.episode_env(label)["ANTHROPIC_CUSTOM_HEADERS"]
    headers: dict[str, str] = {}
    for line in value.split("\n"):
        name, _, header_value = line.partition(":")
        headers[name.strip()] = header_value.strip()
    return headers


def _metadata_from_headers(headers: dict[str, str]) -> dict[str, str]:
    """The metadata keys LiteLLM's langfuse_* header merge produces.

    Mirrors litellm.integrations.langfuse add_metadata_from_header: every
    "langfuse_<key>" request header becomes metadata["<key>"].
    """
    metadata: dict[str, str] = {}
    for name, value in headers.items():
        if name.startswith("langfuse_"):
            metadata[name[len("langfuse_"):]] = value
    return metadata


def test_request_metadata_carries_exactly_the_five_reporting_keys() -> None:
    adapter = ClaudeCodeAdapter(
        role="manager", run_id="run-1", queue_id="q-abc123", trio="zaiq"
    )
    metadata = _metadata_from_headers(
        _header_dict(adapter, "round_001_manager_raw_trajectory")
    )
    # Exactly the five keys the reporting surface reads - no more, no less.
    assert set(metadata) == set(REPORTING_KEYS)
    assert metadata == EXPECTED_HEADERS


@pytest.mark.parametrize(
    "role",
    ["manager", "cli_executor", "cli_auditor", "gui_executor", "gui_auditor"],
)
def test_request_metadata_role_values(role: str) -> None:
    adapter = ClaudeCodeAdapter(
        role=role, run_id="run-1", queue_id="q-abc123", trio="zaiq"
    )
    metadata = _metadata_from_headers(
        _header_dict(adapter, "round_002_" + role + "_raw_trajectory")
    )
    assert metadata["role"] == role
    assert metadata["run_id"] == "run-1"
    assert metadata["queue_id"] == "q-abc123"
    assert metadata["trio"] == "zaiq"
    assert metadata["round"] == "round_002"


def test_request_metadata_round_follows_the_episode_label() -> None:
    adapter = ClaudeCodeAdapter(role="manager", run_id="run-9")
    first = _metadata_from_headers(
        _header_dict(adapter, "round_041_manager_raw_trajectory")
    )
    second = _metadata_from_headers(
        _header_dict(adapter, "round_007_manager_raw_trajectory")
    )
    assert first["round"] == "round_041"
    assert second["round"] == "round_007"


def test_direct_launch_without_queue_context_marks_placeholders() -> None:
    # Direct (non-queue) launches have no queue_id/trio; the keys stay present
    # with the stable placeholder so the request shape never varies.
    adapter = ClaudeCodeAdapter(role="cli_executor", run_id="run-direct")
    metadata = _metadata_from_headers(
        _header_dict(adapter, "round_003_cli_executor_raw_trajectory")
    )
    assert set(metadata) == set(REPORTING_KEYS)
    assert metadata["queue_id"] == "unknown"
    assert metadata["trio"] == "unknown"
    assert metadata["run_id"] == "run-direct"
    assert metadata["role"] == "cli_executor"
    assert metadata["round"] == "round_003"


def test_request_decoration_unchanged_outside_the_new_headers() -> None:
    # Nothing else in the request decoration changes: the two pre-existing
    # header lines keep their exact values, and no third non-langfuse header
    # appears.
    adapter = ClaudeCodeAdapter(
        role="cli_auditor", run_id="run-1", queue_id="q-abc123", trio="zaiq"
    )
    headers = _header_dict(adapter, "round_005_cli_auditor_raw_trajectory")
    expected_session = episode_session_id("run-1", "round_005_cli_auditor_raw_trajectory", "cli_auditor")
    assert headers["x-litellm-tags"] == (
        f"lh-run/run-1,round_005,cli_auditor,lh-session/{expected_session}"
    )
    assert headers["x-litellm-session-id"] == "run-1"
    # Only the two pre-existing headers plus the five langfuse_* lines exist.
    assert set(headers) == {
        "x-litellm-tags",
        "x-litellm-session-id",
        *("langfuse_" + key for key in REPORTING_KEYS),
    }


def test_custom_header_value_survives_shell_quoting() -> None:
    # The caller shell-quotes the whole value; every newline-separated header
    # line must survive a POSIX shell round-trip unchanged.
    adapter = ClaudeCodeAdapter(
        role="manager", run_id="run-1", queue_id="q-abc123", trio="zaiq"
    )
    value = adapter.episode_env("round_001_manager_raw_trajectory")["ANTHROPIC_CUSTOM_HEADERS"]
    echoed = subprocess.run(
        ["sh", "-c", f"V={shlex.quote(value)} sh -c 'printf %s \"$V\"'"],
        capture_output=True,
        text=True,
    ).stdout
    assert echoed == value
    # 2 pre-existing header lines plus the 5 new langfuse_* lines.
    assert value.count("\n") == 6


def test_episode_env_empty_without_run_id_unchanged() -> None:
    # No run id: the pre-existing behaviour (no decoration at all) is kept -
    # the metadata keys are never attached without run context.
    adapter = ClaudeCodeAdapter(role="manager", queue_id="q-abc123", trio="zaiq")
    assert adapter.episode_env("round_001_manager_raw_trajectory") == {}


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


def test_run_episode_command_carries_the_metadata_headers(tmp_path: Path) -> None:
    # End to end through run_episode: the executed agent command embeds the
    # five langfuse_* header lines, so every LLM request of the episode is
    # stamped.  (The agent CLI turns the env var into request headers.)
    adapter = ClaudeCodeAdapter(
        role="manager",
        run_id="run-test",
        queue_id="q-abc123",
        trio="zaiq",
        workspace_path=str(tmp_path / "workspace"),
        prompt_dir=str(tmp_path / "prompts"),
    )
    env = FakeEnvironment(tmp_path / "staging")
    (tmp_path / "workspace").mkdir()
    budget = EpisodeBudget(max_duration_seconds=1)
    result = asyncio.run(
        adapter.run_episode("test prompt", env, budget, live_trajectory_path=None)
    )
    # The agent command (not the prompt-staging mkdir that precedes it).
    command = result.metadata["command"]
    assert "langfuse_run_id: run-test" in command
    assert "langfuse_queue_id: q-abc123" in command
    assert "langfuse_trio: zaiq" in command
    assert "langfuse_role: manager" in command
    assert "langfuse_round: round_unknown" in command


def test_worker_argv_carries_queue_provenance() -> None:
    # The supervisor's worker command is the plumbing that brings queue
    # context to the attach site; queue-triggered launches append exactly the
    # two flags the CLI parser below consumes.
    from lh_harness.supervisor.service import RunSupervisor

    supervisor = RunSupervisor.__new__(RunSupervisor)
    supervisor.runs_root = "/tmp/runs"
    command = supervisor._worker_command(
        run_id="run-1",
        task="@/tmp/task.md",
        agent="claude_code",
        model=None,
        role_configs=None,
        workspace="/tmp/ws",
        max_rounds=4,
        prompt_language="en",
        queue_id="q-abc123",
        trio="zaiq",
    )
    assert "--queue-id=q-abc123" in command
    assert "--trio=zaiq" in command


def test_worker_argv_omits_queue_flags_for_direct_launches() -> None:
    from lh_harness.supervisor.service import RunSupervisor

    supervisor = RunSupervisor.__new__(RunSupervisor)
    supervisor.runs_root = "/tmp/runs"
    command = supervisor._worker_command(
        run_id="run-1",
        task="@/tmp/task.md",
        agent="claude_code",
        model=None,
        role_configs=None,
        workspace="/tmp/ws",
        max_rounds=4,
        prompt_language="en",
    )
    assert not [part for part in command if part.startswith("--queue-id=")]
    assert not [part for part in command if part.startswith("--trio=")]


def test_run_cli_parser_accepts_queue_provenance_flags(monkeypatch) -> None:
    # The worker CLI accepts the two flags without touching anything else in
    # the namespace: parsing with and without them differs only in these two
    # attributes.
    from lh_harness import cli

    captured: list[object] = []
    monkeypatch.setattr(cli, "load_run_defaults", lambda: {})
    monkeypatch.setattr(
        cli,
        "_run_command",
        lambda args: captured.append(args) or 0,
    )
    assert cli.main(["run", "--task=demo", "--queue-id=q-abc123", "--trio=zaiq"]) == 0
    assert cli.main(["run", "--task=demo"]) == 0
    with_queue, without_queue = captured
    assert with_queue.queue_id == "q-abc123"
    assert with_queue.trio == "zaiq"
    assert without_queue.queue_id is None
    assert without_queue.trio is None
    base = dict(vars(without_queue))
    patched = dict(base)
    patched.update({"queue_id": "q-abc123", "trio": "zaiq"})
    assert vars(with_queue) == patched


def test_metadata_values_carry_no_secrets() -> None:
    # The five values are the same non-secret ids the tags already carry; a
    # guard against accidentally widening the channel later.
    adapter = ClaudeCodeAdapter(
        role="manager", run_id="run-1", queue_id="q-abc123", trio="zaiq"
    )
    value = adapter.episode_env("round_001_manager_raw_trajectory")["ANTHROPIC_CUSTOM_HEADERS"]
    lowered = value.lower()
    for marker in ("bearer ", "api-key", "authorization", "sk-", "pk-lf-", "token="):
        assert marker not in lowered
