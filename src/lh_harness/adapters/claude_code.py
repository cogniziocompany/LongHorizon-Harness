from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from ..agent_logs import visible_output as extract_claude_visible_output
from ..agent_registry import normalise_reasoning_effort
from .claude_permissions import (
    ClaudeRole,
    is_auditor_role,
    path_deny_rules,
    policy_for_role,
    snapshot_workspace,
    workspace_snapshot_diff,
)
from .cli_agent import CommandAgentAdapter, _episode_prompt_label
from ..environment.base import Environment
from ..provider_errors import GUARD_REJECTION_MESSAGE
from ..types import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_TMP_DIR,
    DEFAULT_WORKSPACE_PATH,
    EpisodeBudget,
    EpisodeResult,
)


class ClaudeCodeAdapter(CommandAgentAdapter):
    def __init__(
        self,
        *,
        model: str = DEFAULT_CLAUDE_MODEL,
        api_key: str | None = None,
        base_url: str | None = None,
        workspace_path: str = DEFAULT_WORKSPACE_PATH,
        prompt_dir: str = f"{DEFAULT_TMP_DIR}/prompts",
        mcp_config: str | None = None,
        mcp_profile: str | None = None,
        add_dirs: list[str] | None = None,
        role: ClaudeRole = "cli_executor",
        hidden_paths: tuple[str, ...] = (),
        guard_exclude_paths: tuple[str, ...] = (),
        reasoning_effort: str | None = None,
        run_id: str | None = None,
        run_dir: str | None = None,
    ) -> None:
        policy = policy_for_role(role)
        effort = normalise_reasoning_effort(reasoning_effort)
        env_parts: list[str] = []
        if api_key:
            quoted_key = shlex.quote(api_key)
            env_parts.append(f"ANTHROPIC_API_KEY={quoted_key}")
            env_parts.append(f"ANTHROPIC_AUTH_TOKEN={quoted_key}")
        if base_url:
            raw_url = base_url.rstrip("/")
            if raw_url.endswith("/v1"):
                raw_url = raw_url[:-3]
            env_parts.append(f"ANTHROPIC_BASE_URL={shlex.quote(raw_url)}")
        env_parts.extend(
            [
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",
                "CLAUDE_CODE_SKIP_PROMPT_HISTORY=1",
                f"LH_HARNESS_CLAUDE_ROLE={shlex.quote(role)}",
            ]
        )

        # MCP support remains opt-in. --strict-mcp-config keeps unrelated
        # user/project MCP servers out of every role. Per-episode generated
        # configs live under the harness-owned run directory, outside the agent
        # workspace, so they cannot be read by the agent but are picked up by
        # the adapter's --mcp-config path below.
        self.mcp_profile_name = mcp_profile
        self.run_dir = run_dir
        mcp_config = mcp_config or os.getenv("LH_HARNESS_CLAUDECODE_MCP_CONFIG")
        if mcp_config:
            candidate = Path(mcp_config).expanduser()
            if candidate.is_file():
                mcp_config = str(candidate.resolve())
        resolved_add_dirs = list(add_dirs or [])
        env_add_dirs = os.getenv("LH_HARNESS_CLAUDECODE_ADD_DIRS") or os.getenv(
            "LH_HARNESS_MCP_ADD_DIRS"
        )
        if env_add_dirs:
            resolved_add_dirs.extend(part for part in env_add_dirs.split(os.pathsep) if part)
        if resolved_add_dirs:
            raise ValueError(
                "Claude Code role isolation does not allow additional directories; "
                "put task files inside the run workspace instead."
            )

        if is_auditor_role(role):
            env_parts.extend(
                [
                    "GIT_OPTIONAL_LOCKS=0",
                    "GIT_PAGER=cat",
                    "PAGER=cat",
                ]
            )
            for key, value in policy.env_overrides.items():
                if value is not None:
                    env_parts.append(f"{key}={shlex.quote(value)}")

        # Variables that must be removed from the subprocess environment are
        # deleted from the env dict passed to Popen (see run_episode); they must
        # never be rendered as shell arguments such as --unsetenvvar=..., which
        # /bin/sh treats as a command name and fails with "not found".
        self._env_unset_keys = (
            tuple(
                key
                for key, value in policy.env_overrides.items()
                if value is None
            )
            if is_auditor_role(role)
            else ()
        )

        env_prefix = (" ".join(env_parts) + " ") if env_parts else ""
        command_parts = [
            "claude",
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            # Honor the role-isolation contract described above: without this
            # flag Claude Code auto-loads the workspace's .mcp.json, and large
            # MCP tool inventories can push built-in tool schemas out of the
            # context window on local models. Only an explicit mcp_config
            # (--mcp-config below) is ever loaded.
            "--strict-mcp-config",
        ]
        deny_tools = [*policy.disallowed_tools, *path_deny_rules(hidden_paths)]
        if deny_tools:
            command_parts.append("--disallowedTools")
            command_parts.extend(shlex.quote(tool) for tool in deny_tools)
        self.computer_mcp_configured = bool(policy.load_computer_mcp and mcp_config)
        if self.computer_mcp_configured:
            command_parts.extend(["--mcp-config", shlex.quote(mcp_config)])
        command_parts.extend(["--model", shlex.quote(model)])
        # Claude Code warns and continues at its default when the value is not
        # one it knows, so an unusable effort will not fail the run here.
        if effort:
            command_parts.extend(["--effort", shlex.quote(effort)])

        self.role = role
        self.policy = policy
        self.reasoning_effort = effort
        self.run_id = run_id
        # Snapshot-only exclusions: unlike hidden_paths these are not denied
        # to the agent — the guard just refrains from walking directories that
        # legitimately churn (build outputs) during an audit window.
        self.guard_exclude_paths = tuple(guard_exclude_paths)
        super().__init__(
            command_template=f"{env_prefix}{' '.join(command_parts)} < {{prompt_path}}",
            prompt_dir=prompt_dir,
            workspace_path=workspace_path,
            visible_output_parser=extract_claude_visible_output,
            hidden_paths=hidden_paths,
        )

    def episode_env(self, label: str) -> dict[str, str]:
        # Stamp run/round/role onto every proxied request as LiteLLM tags so
        # proxy-side observability (Langfuse per-key logging) can group traces
        # by harness run. Claude Code forwards ANTHROPIC_CUSTOM_HEADERS
        # ("Name: value") on each API call; the tag values carry no secrets.
        session_id = episode_session_id(self.run_id, label, self.role)
        if not session_id or session_id == "unknown":
            return {}
        tags = f"lh-run/{self.run_id},{_extract_round_tag(label)},{self.role},lh-session/{session_id}"
        return {"ANTHROPIC_CUSTOM_HEADERS": f"x-litellm-tags: {tags}"}

    def episode_session_id(self, label: str) -> str:
        """Derived session id used to join a run's episodes in the proxy logs."""
        return episode_session_id(self.run_id, label, self.role)

    async def run_episode(
        self,
        prompt: str,
        env: Environment,
        budget: EpisodeBudget,
        live_trajectory_path: str | None = None,
    ) -> EpisodeResult:
        before = (
            snapshot_workspace(
                self.workspace_path,
                (*self.hidden_paths, *self.guard_exclude_paths),
            )
            if is_auditor_role(self.role)
            else None
        )
        # Carry per-episode env into the subprocess, removing auditor-unset keys.
        label = _episode_prompt_label(live_trajectory_path)
        if self._env_unset_keys:
            env = _EnvUnsetWrapper(env, self._env_unset_keys)
        result = await super().run_episode(
            prompt,
            env,
            budget,
            live_trajectory_path=live_trajectory_path,
        )
        label = _episode_prompt_label(live_trajectory_path)
        result.metadata.update(
            {
                "claude_role": self.role,
                "claude_permission_mode": self.policy.permission_mode,
                "claude_dangerously_skip_permissions": True,
                "claude_hooks_enabled": False,
                "claude_native_sandbox_enabled": False,
                "claude_tool_policy": "default-minus-disallowed",
                "claude_disallowed_tools": list(self.policy.disallowed_tools),
                "claude_computer_mcp_loaded": self.computer_mcp_configured,
                "claude_workspace_read_only": self.policy.workspace_read_only,
                "claude_reasoning_effort": self.reasoning_effort,
                "lh_session_id": episode_session_id(self.run_id, label, self.role),
            }
        )
        _inject_session_header_into_mcp_config(self.run_id, label, self.role, self.run_dir)
        if before is not None:
            after = snapshot_workspace(
                self.workspace_path,
                (*self.hidden_paths, *self.guard_exclude_paths),
            )
            diff = workspace_snapshot_diff(before, after)
            result.metadata.update(diff)
            # Record the effective exclusions with every audited episode so
            # the guard's reduced coverage is visible in the run artifacts.
            result.metadata["verifier_guard_exclude_paths"] = list(self.guard_exclude_paths)
            snapshot_errors = diff.get("verifier_workspace_snapshot_errors")
            if snapshot_errors:
                # Escalate only a successful status: a real timeout (or
                # cancellation) is stronger evidence and must stay visible to the
                # runtime-failure classifier.
                if result.status == "done":
                    result.status = "error"
                guard_error = GUARD_REJECTION_MESSAGE
                result.error = f"{result.error}\n{guard_error}".strip() if result.error else guard_error
        return result


class _EnvUnsetWrapper:
    """Wrap an Environment so that exec() drops named keys from the child env."""

    def __init__(self, env: Environment, keys: tuple[str, ...]) -> None:
        self._env = env
        self._keys = keys

    def __getattr__(self, name: str) -> Any:
        # Delegate all other Environment attributes (upload, download,
        # staging_dir, etc.) to the wrapped environment unchanged.
        return getattr(self._env, name)

    async def exec(
        self,
        command: str,
        timeout: int = 30,
        tee_path: str | None = None,
    ) -> Any:
        # LocalEnvironment copies os.environ in the child process. We cannot
        # pass a custom env dict through the Environment protocol, so we curate
        # the parent process's os.environ in place for the duration of the call.
        # This is safe here because the wrapper is only used for single-threaded
        # local agent runs and the keys are restored immediately after exec.
        import os as _os

        saved: dict[str, str | None] = {}
        for key in self._keys:
            saved[key] = _os.environ.pop(key, None)
        try:
            return await self._env.exec(command, timeout=timeout, tee_path=tee_path)
        finally:
            for key, value in saved.items():
                if value is not None:
                    _os.environ[key] = value


def episode_session_id(run_id: str | None, label: str, role: str) -> str:
    """Derived session id used to join a run's episodes in the proxy logs."""
    if not run_id:
        return "unknown"
    round_tag = _extract_round_tag(label)
    return f"{run_id}.{round_tag}.{role}"


def _extract_round_tag(label: str) -> str:
    match = re.match(r"round_\d+", label)
    return match.group(0) if match else "round_unknown"


def _inject_session_header_into_mcp_config(
    run_id: str | None,
    label: str,
    role: str,
    run_dir: str | None,
) -> None:
    """If a generated MCP config exists for this role, inject the session header."""
    if not run_dir:
        return
    config_path = Path(run_dir) / "harness" / "mcp" / f"{role}.mcp.json"
    if not config_path.is_file():
        return
    try:
        data = json.loads(config_path.read_text())
        servers = data.get("mcpServers", {})
        session_id = episode_session_id(run_id, label, role)
        for server in servers.values():
            headers = server.setdefault("headers", {})
            headers["X-LH-Session"] = session_id
        config_path.write_text(json.dumps(data, indent=2))
    except Exception:
        pass
