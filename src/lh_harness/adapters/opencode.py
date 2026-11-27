from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import urllib.error
import urllib.request

from ..agent_logs import visible_output as extract_opencode_visible_output
from ..agent_registry import normalise_reasoning_effort
from ..environment.base import Environment
from ..types import (
    DEFAULT_OPENCODE_MODEL,
    DEFAULT_TMP_DIR,
    DEFAULT_WORKSPACE_PATH,
    EpisodeBudget,
    EpisodeResult,
)
from ..utils.agent_cli import resolve_opencode_binary
from .cli_agent import CommandAgentAdapter

_CONFIG_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class OpenCodeAdapter(CommandAgentAdapter):
    """Run OpenCode headlessly through LongHorizon Harness.

    ``opencode run --format json`` streams one JSON event per line on stdout
    (``step_start``, ``tool_use``, ``text``, ``step_finish``, ``error``), which
    the shared agent_logs parsers normalize into the same views every other
    backend provides.  ``--yolo`` is OpenCode's documented alias of ``--auto``
    for headless runs; it approves every permission the harness already
    scopes for the episode.  Reasoning effort maps to ``--variant``, the
    provider-specific variant switch OpenCode exposes on ``opencode run``.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_OPENCODE_MODEL,
        api_key: str | None = None,
        base_url: str | None = None,
        workspace_path: str = DEFAULT_WORKSPACE_PATH,
        prompt_dir: str = f"{DEFAULT_TMP_DIR}/prompts",
        effort: str | None = None,
        reasoning_effort: str | None = None,
        role: str = "cli_executor",
        hidden_paths: tuple[str, ...] = (),
    ) -> None:
        normalized_model = model.strip()
        if not normalized_model:
            raise ValueError("OpenCode model must not be empty")
        if "\x00" in normalized_model or len(normalized_model) > 256:
            raise ValueError("OpenCode model contains invalid characters or is too long")
        # One shared validator for every backend: a length check alone would let
        # a quote or newline through into the command line.
        normalized_effort = normalise_reasoning_effort(
            reasoning_effort if reasoning_effort is not None else effort
        )

        opencode_binary = resolve_opencode_binary() or "opencode"
        env_parts: list[str] = []
        if api_key:
            # OpenCode falls back to OPENCODE_API_KEY when a provider has no
            # key of its own, so one harness credential covers every model.
            env_parts.append(f"OPENCODE_API_KEY={shlex.quote(api_key)}")
        if base_url:
            provider_id = normalized_model.split("/", 1)[0].strip() or "opencode"
            # Best-effort: when the endpoint is a LiteLLM-style gateway,
            # /model/info publishes each alias's context/output limits
            # (easybutt0n-272). Any failure (not a gateway, key not allowed
            # on the admin route, timeout) degrades to a config without
            # limits; the run itself must never fail here.
            model_limits = _fetch_gateway_model_limits(base_url, api_key)
            config_path = _write_endpoint_config(
                prompt_dir,
                provider_id,
                base_url,
                model_limits=model_limits,
                model_id=normalized_model.split("/", 1)[-1] if "/" in normalized_model else normalized_model,
            )
            # OpenCode merges config files instead of replacing them, and
            # OPENCODE_CONFIG sits between the global and project configs, so
            # a per-run file carrying only the provider override keeps the
            # user's own providers, models, and MCP servers intact.
            env_parts.append(f"OPENCODE_CONFIG={shlex.quote(config_path)}")

        command_parts = [
            shlex.quote(opencode_binary),
            "run",
            "--format",
            "json",
            "--yolo",
            "--model",
            shlex.quote(normalized_model),
        ]
        if normalized_effort:
            command_parts.extend(["--variant", shlex.quote(normalized_effort)])
        # `opencode run` reads the prompt from stdin when no positional message
        # is given, keeping long prompts off the command line.
        command_parts.append("< {prompt_path}")

        env_prefix = (" ".join(env_parts) + " ") if env_parts else ""
        super().__init__(
            command_template=f"{env_prefix}{' '.join(command_parts)}",
            prompt_dir=prompt_dir,
            workspace_path=workspace_path,
            visible_output_parser=extract_opencode_visible_output,
            hidden_paths=hidden_paths,
        )
        self.model = normalized_model
        self.effort = normalized_effort
        self.role = role

    async def run_episode(
        self,
        prompt: str,
        env: Environment,
        budget: EpisodeBudget,
        live_trajectory_path: str | None = None,
    ) -> EpisodeResult:
        result = await super().run_episode(
            prompt,
            env,
            budget,
            live_trajectory_path=live_trajectory_path,
        )
        result.metadata.update(
            {
                "opencode_role": self.role,
                "opencode_model": self.model,
                "opencode_variant": self.effort,
                "opencode_approval_mode": "yolo",
            }
        )
        return result


def _fetch_gateway_model_limits(
    base_url: str, api_key: str | None
) -> dict[str, tuple[int, int]] | None:
    """Best-effort fetch of alias context/output limits from /model/info.

    Returns ``None`` on any failure: not a gateway, the key is not allowed on
    the admin route, a timeout, a malformed payload. Callers treat limits as
    optional decoration, never a precondition for the run. Only aliases whose
    deployments all publish both ``max_input_tokens`` and ``max_output_tokens``
    are included, reduced to the MIN across non-blocked deployments (a pool
    serves at its smallest member).
    """
    base = base_url.strip().rstrip("/")
    # A completions endpoint usually ends in /v1; /model/info sits at the
    # gateway root.
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    url = base + "/model/info"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError):
        return None
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    limits: dict[str, tuple[int, int]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("model_name")
        info = row.get("model_info") or {}
        if not name or info.get("blocked"):
            continue
        try:
            max_in = int(info["max_input_tokens"])
            max_out = int(info["max_output_tokens"])
        except (KeyError, TypeError, ValueError):
            continue
        current = limits.get(name)
        if current is None or (max_in, max_out) < current:
            limits[name] = (max_in, max_out)
    return limits or None


def _write_endpoint_config(
    prompt_dir: str,
    provider_id: str,
    base_url: str,
    model_limits: dict[str, tuple[int, int]] | None = None,
    model_id: str | None = None,
) -> str:
    """Write an OPENCODE_CONFIG file overriding one provider's base URL.

    When ``model_limits`` carries the gateway's published limits for
    ``model_id`` (easybutt0n-272), the same file also pins the model's
    ``limit.context`` / ``limit.output`` so the run picks up the gateway's
    context window the way Claude Code does.

    The harness runs OpenCode with ``cd <workspace> && ...``, so the config
    lives inside the run's own prompt directory instead of the workspace.
    """
    normalized_url = base_url.strip().rstrip("/")
    if not normalized_url or "\x00" in normalized_url:
        raise ValueError("OpenCode base URL must be a non-empty endpoint")
    normalized_dir = prompt_dir.rstrip("/") or "."
    safe_provider = _CONFIG_FILENAME_RE.sub("_", provider_id).strip("_") or "opencode"
    provider_config: dict[str, object] = {"options": {"baseURL": normalized_url}}
    if model_limits and model_id and model_id in model_limits:
        max_in, max_out = model_limits[model_id]
        provider_config["models"] = {
            model_id: {"limit": {"context": max_in, "output": max_out}}
        }
    config = {"provider": {provider_id: provider_config}}
    path = posixpath.join(normalized_dir, f"opencode-endpoint-{safe_provider}.json")
    try:
        os.makedirs(normalized_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise ValueError(f"could not write OpenCode endpoint config: {exc}") from exc
    return path