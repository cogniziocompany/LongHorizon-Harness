from __future__ import annotations

import inspect
import os
import posixpath
import re
import shlex
import time
import uuid
from collections.abc import Callable
from pathlib import PurePath

from ..environment.base import Environment
from ..environment.remote_files import write_remote_text
from ..runtime_signals import detect_runtime_signals
from ..types import DEFAULT_TMP_DIR, DEFAULT_WORKSPACE_PATH, EpisodeBudget, EpisodeResult

# Stalled-episode detection.  The liveness signal is OUTPUT PROGRESS: a working
# agent CLI streams stream-json records continuously (the 09-16 fleet snapshot
# shows 76 s completions interleaved with six 3600 s timeouts whose
# runtime_signals were EMPTY -- the same workspace, the same role), so a
# hung child is the one that stops emitting entirely.  Wall-clock duration
# cannot separate "working slowly" from "hung" (both burn the budget); the
# absence of any stdout/stderr byte for a full silent window can.
#
# The silent window is: LH_HARNESS_STALL_SECONDS (ops override) >
# EpisodeBudget.stall_seconds (per-episode override) > a conservative quarter
# of the episode budget, floored at 120 s and capped at 900 s.  With the
# 3600 s executor wall this is 900 s of total silence -- far beyond anything
# the 76 s completions suggest a healthy backend needs, yet eight times
# faster than the 3600 s hangs were costing.
DEFAULT_STALL_SECONDS_CAP = 900.0
DEFAULT_STALL_SECONDS_FLOOR = 120.0
NO_OUTPUT_STALL_SIGNAL = "NO_OUTPUT_STALL"


def _stall_window(budget: EpisodeBudget) -> float | None:
    configured = budget.stall_seconds
    if configured is None:
        override = os.environ.get("LH_HARNESS_STALL_SECONDS")
        if override:
            try:
                configured = float(override)
            except ValueError:
                configured = None
    if configured is not None:
        return float(configured) if configured > 0 else None
    quarter = budget.max_duration_seconds / 4.0
    return float(min(DEFAULT_STALL_SECONDS_CAP, max(DEFAULT_STALL_SECONDS_FLOOR, quarter)))


def _env_supports_stall_watchdog(env: Environment) -> bool:
    """True when this Environment's exec accepts no_output_stall_seconds."""
    exec_fn = getattr(env, "exec", None)
    if exec_fn is None:
        return False
    try:
        params = inspect.signature(exec_fn).parameters
    except (TypeError, ValueError):
        return False
    return "no_output_stall_seconds" in params

_SECRET_NAME = r"(?:API[_-]?KEY|AUTH[_-]?TOKEN|ACCESS[_-]?TOKEN|SECRET|PASSWORD|TOKEN)"
_SECRET_VALUE = r"(?:'[^']*'|\"[^\"]*\"|\S+)"
_SECRET_PATTERNS = (
    re.compile(rf"\b([A-Za-z0-9_]*{_SECRET_NAME}\s*=){_SECRET_VALUE}", re.I),
    re.compile(rf"(--[A-Za-z0-9-]*{_SECRET_NAME}[= ]){_SECRET_VALUE}", re.I),
)


def redact_secrets(text: str) -> str:
    """Mask credential values in text before it is logged or shown to a role."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1***REDACTED***", text)
    return text


class CommandAgentAdapter:
    def __init__(
        self,
        *,
        command_template: str,
        prompt_dir: str = f"{DEFAULT_TMP_DIR}/prompts",
        workspace_path: str = DEFAULT_WORKSPACE_PATH,
        visible_output_parser: Callable[[str], str] | None = None,
        hidden_paths: tuple[str, ...] = (),
    ) -> None:
        self.command_template = command_template
        self.prompt_dir = prompt_dir.rstrip("/") or "."
        self.workspace_path = workspace_path.rstrip("/")
        self.visible_output_parser = visible_output_parser
        self.hidden_paths = tuple(hidden_paths)

    def episode_env(self, label: str) -> dict[str, str]:
        """Extra env for one episode's agent process, keyed off the round/role label.

        Adapters override this to stamp per-episode metadata (e.g. request tags
        for proxy-side observability). Values are shell-quoted by the caller.
        """
        return {}

    async def run_episode(
        self,
        prompt: str,
        env: Environment,
        budget: EpisodeBudget,
        live_trajectory_path: str | None = None,
    ) -> EpisodeResult:
        start = time.monotonic()
        # Never reuse one global prompt.md. A run owns its prompt directory and
        # every role episode gets a distinct filename, so concurrent harnesses
        # (and future concurrent roles within one harness) cannot overwrite the
        # input while an agent CLI is still reading it.
        label = _episode_prompt_label(live_trajectory_path)
        prompt_path = posixpath.join(
            self.prompt_dir,
            f"{label}_{uuid.uuid4().hex[:12]}.md",
        )
        await write_remote_text(env, prompt_path, prompt + _hidden_paths_notice(self.hidden_paths))
        # Substituted by explicit replace, not str.format: templates embed literal
        # braces (e.g. Codex passes inline-TOML `-c` overrides) that format() would
        # try to interpret as placeholders.
        command_body = self.command_template
        for placeholder, value in (
            ("{prompt_path}", shlex.quote(prompt_path)),
            ("{timeout}", str(budget.max_duration_seconds)),
        ):
            command_body = command_body.replace(placeholder, value)
        # Per-episode env assignments compose with the template's own inline
        # `VAR=val` prefix; both sit before the executable in the same simple
        # command, which POSIX sh accepts.
        env_assigns = "".join(
            f"{key}={shlex.quote(value)} " for key, value in self.episode_env(label).items()
        )
        command = f"cd {shlex.quote(self.workspace_path)} && {env_assigns}{command_body}"
        # When a live path is given (local runs), the environment mirrors stdout
        # to that file line-by-line so the dashboard shows the trajectory live.
        # The stall watchdog rides the same call when the environment supports
        # it: the liveness signal is output progress (see _stall_window above),
        # so a hung episode fails fast with a runtime signal instead of burning
        # the full budget while producing nothing.
        stall_window = _stall_window(budget)
        exec_kwargs: dict[str, object] = {
            "timeout": budget.max_duration_seconds,
            "tee_path": live_trajectory_path,
        }
        if stall_window is not None and _env_supports_stall_watchdog(env):
            exec_kwargs["no_output_stall_seconds"] = stall_window
        result = await env.exec(command, **exec_kwargs)  # type: ignore[arg-type]
        duration_ms = int((time.monotonic() - start) * 1000)
        stalled = result.termination_reason == "stall"
        if result.termination_reason == "timeout":
            status = "timeout"
        else:
            status = "done" if result.exit_code == 0 else "error"
        stdout_log = result.stdout
        actions_log = stdout_log
        visible_output = (
            self.visible_output_parser(stdout_log).strip()
            if self.visible_output_parser is not None
            else ""
        )
        runtime_signals = detect_runtime_signals(stdout_log)
        if stalled:
            # A stalled episode must carry a hard runtime signal: the 09-16
            # evidence showed the failure mode as 3600000-ish ms duration with
            # an EMPTY runtime_signals array.  This label (plus the
            # termination_reason here) is what downstream classification keys
            # on, so the hang is visible in the episode metadata even when the
            # killed CLI produced no output of its own.
            runtime_signals.append(
                {
                    "signal": NO_OUTPUT_STALL_SIGNAL,
                    "evidence": (
                        f"no stdout/stderr output for {stall_window:g}s; "
                        "killed by the stalled-episode watchdog"
                    ),
                }
            )
        if result.termination_reason == "timeout":
            error = f"Episode timed out after {budget.max_duration_seconds}s."
        elif stalled:
            error = (
                f"Episode stalled: no output for {stall_window:g}s "
                f"(termination_reason=stall)."
            )
        else:
            error = redact_secrets(result.stderr[-2000:]) if result.exit_code != 0 else None
        return EpisodeResult(
            status=status,
            actions_log=actions_log,
            error=error,
            duration_ms=duration_ms,
            metadata={
                "command": redact_secrets(command),
                "workspace": self.workspace_path,
                "prompt_path": prompt_path,
                "exit_code": result.exit_code,
                "termination_reason": result.termination_reason,
                "actions_log_chars": len(actions_log),
                "trajectory_format": "jsonl",
                "assistant_visible_output": visible_output,
                "runtime_signals": runtime_signals,
                "stall_window_seconds": stall_window,
                "actions_log_diagnostics_only": bool(
                    self.visible_output_parser is not None and not visible_output
                ),
                "stderr_chars": len(result.stderr),
                "stderr_tail": redact_secrets(result.stderr[-2000:]),
            },
        )


def _hidden_paths_notice(hidden_paths: tuple[str, ...]) -> str:
    """Tell the agent to stay out of the harness's own run directories.

    The run's logs, prompts and harness state may sit inside the workspace. They
    are not task content, and reading them would leak other roles' context.
    """
    if not hidden_paths:
        return ""
    listed = "\n".join(f"- {path}" for path in hidden_paths)
    return (
        "\n\nHarness-owned paths (off limits):\n"
        f"{listed}\n"
        "These hold this run's own logs, prompts, and harness state. Never read, list, "
        "search, or modify them, and never treat their contents as task input or evidence."
    )


def _episode_prompt_label(live_trajectory_path: str | None) -> str:
    """Derive a readable round/role label without relying on it for uniqueness."""
    if not live_trajectory_path:
        return "episode_agent"
    path = PurePath(live_trajectory_path)
    role = path.stem
    suffix = "_raw_trajectory"
    if role.endswith(suffix):
        role = role[: -len(suffix)]
    round_name = path.parent.name if re.fullmatch(r"round_\d+", path.parent.name) else "episode"
    label = f"{round_name}_{role}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("._") or "episode_agent"
