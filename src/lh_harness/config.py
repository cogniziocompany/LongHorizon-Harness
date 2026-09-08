from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from .types import MAX_ROUNDS

try:
    tomllib = importlib.import_module("tomllib")
except ModuleNotFoundError:
    tomllib = importlib.import_module("tomli")

PROJECT_CONFIG_PATH = Path(".lh-harness/config.toml")

_AGENT_CHOICES = {"claude_code", "codex", "deepseek_harness", "opencode"}
_ROLE_NAMES = {
    "manager",
    "executor",
    "gui_executor",
    "cli_executor",
    "auditor",
    "gui_auditor",
    "cli_auditor",
    "final_response",
}
_TIMEOUT_NAMES = {"manager", "gui_executor", "cli_executor", "auditor"}
_RUN_KEYS = {
    "agent",
    "model",
    "reasoning_effort",
    "env",
    "runs_root",
    "workspace",
    "harness_dir",
    "log_dir",
    "base_url",
    "prompt_language",
    "claude_mcp_config",
    "codex_mcp_config",
    "mcp_profile",
    "mcp_profiles",
    "mcp_add_dirs",
    "guard_exclude_paths",
    "max_rounds",
    "dashboard",
    "dashboard_port",
    "allow_auditor_write_mcp",
    "roles",
    "timeouts",
}
_QUEUE_TRIOS = {"kimi", "qwen"}
_QUEUE_CAPACITY_KEYS = {
    "kimi_max",
    "qwen_max",
    "min_healthy_keys",
    "key_health_url",
    "poll_seconds",
}
_STRING_KEYS = {
    "model",
    "runs_root",
    "workspace",
    "harness_dir",
    "log_dir",
    "base_url",
    "claude_mcp_config",
    "codex_mcp_config",
    "mcp_profile",
}

CONFIG_TEMPLATE = """# LongHorizon-Harness project defaults.
# Explicit CLI arguments override these values.

[run]
agent = "codex"
model = "gpt-5.6-sol"

# Reasoning depth, forwarded to whichever backend exposes it (Codex through
# `model_reasoning_effort`, Claude Code through `--effort`, OpenCode through
# `--variant`). Any value the backend accepts is allowed, so a newer tier does
# not need a harness release. Leave unset to keep the provider's own setting.
# reasoning_effort = "high"

env = "local"
runs_root = "./.lh-harness/runs"
# Agents work in the directory lh-harness was started from unless set here.
# workspace = "./workspace"
# harness_dir = "./.lh-harness/runs/<run-id>/harness"
# log_dir = "./lh_harness"

# base_url = "https://api.example.com/v1"

prompt_language = "en"
# Each agent reads its own format; installed plugins are loaded automatically.
# claude_mcp_config = "/path/to/mcp.json"
# codex_mcp_config = "/path/to/mcp.toml"
mcp_add_dirs = []

# Build/cache directories the auditor read-only guard should not snapshot,
# e.g. ["target", "node_modules", "build", ".venv"]. Agents can still read
# them. Exclusions must stay inside the workspace; ".git" and harness-owned
# control/state paths are rejected at startup, and the effective list is
# echoed at run start and recorded in each audited episode's metadata.
# Passing --guard-exclude-path replaces this list rather than adding to it.
guard_exclude_paths = []

max_rounds = 25
dashboard = true
# Embedded dashboards use an OS-assigned port by default so concurrent runs
# cannot accidentally share or race a fixed listener. Standalone `web` keeps
# its explicit 8799 default for the operator-facing control plane.
dashboard_port = 0

[run.timeouts]
manager = 300
gui_executor = 1800
cli_executor = 1800
auditor = 300

[run.roles.manager]
# agent = "codex"
# model = "gpt-5.6-sol"
# reasoning_effort = "high"

[run.roles.executor]
# agent = "codex"
# model = "gpt-5.6-sol"

[run.roles.gui_executor]
# agent = "codex"
# model = "gpt-5.6-sol"

[run.roles.cli_executor]
# agent = "codex"
# model = "gpt-5.6-sol"

[run.roles.auditor]
# agent = "codex"
# model = "gpt-5.6-sol"

[run.roles.gui_auditor]
# agent = "codex"
# model = "gpt-5.6-sol"

[run.roles.cli_auditor]
# agent = "codex"
# model = "gpt-5.6-sol"

# Writes the closing reply to you; falls back to the manager's agent/model.
[run.roles.final_response]
# agent = "codex"
# model = "gpt-5.6-sol"

# Fleet queue configuration. Trios are named server-side so clients only ask
# for "kimi" or "qwen"; the service resolves the actual agent/model/profile.
# [queue.trios.kimi]
# agent = "claude_code"
# model = "kimi-k2.7-code:cloud"
# mcp_profile = "ops"

# [queue.trios.qwen]
# agent = "codex"
# model = "qwen3.8"
# mcp_profile = "audit"

# [queue.capacity]
# kimi_max = 3          # concurrent kimi runs allowed
# qwen_max = 1          # concurrent qwen runs allowed (QA only, one at a time)
# min_healthy_keys = 2  # healthy Ollama Cloud keys required before kimi launches
# key_health_url = "https://litellm.easybutt0n.ai/health"
# poll_seconds = 15
"""


class ProjectConfigError(ValueError):
    pass


def create_project_config(
    path: str | Path = PROJECT_CONFIG_PATH,
    *,
    force: bool = False,
) -> Path:
    target = Path(path)
    if target.exists() and not force:
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return target


def load_run_defaults(path: str | Path = PROJECT_CONFIG_PATH) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        return {}
    try:
        with source.open("rb") as fh:
            payload = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProjectConfigError(f"could not read {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProjectConfigError(f"{source} must contain a TOML table")
    unknown_root = set(payload) - {"run", "queue"}
    if unknown_root:
        raise ProjectConfigError(f"unknown top-level key(s): {_names(unknown_root)}")
    run = payload.get("run", {})
    if not isinstance(run, dict):
        raise ProjectConfigError("[run] must be a TOML table")
    result = _flatten_run_table(run)
    queue = payload.get("queue", {})
    if isinstance(queue, dict):
        result["queue"] = _flatten_queue_table(queue)
    return result


def _flatten_queue_table(queue: dict[str, Any]) -> dict[str, Any]:
    trios = queue.get("trios", {})
    if not isinstance(trios, dict):
        raise ProjectConfigError("[queue.trios] must be a TOML table")
    unknown_trios = set(trios) - _QUEUE_TRIOS
    if unknown_trios:
        raise ProjectConfigError(f"unknown queue trio(s): {_names(unknown_trios)}")
    normalized_trios: dict[str, dict[str, Any]] = {}
    for name, spec in trios.items():
        if not isinstance(spec, dict):
            raise ProjectConfigError(f"[queue.trios.{name}] must be a TOML table")
        unknown_trio_keys = set(spec) - {"agent", "model", "mcp_profile"}
        if unknown_trio_keys:
            raise ProjectConfigError(
                f"unknown [queue.trios.{name}] key(s): {_names(unknown_trio_keys)}"
            )
        agent = str(spec.get("agent", "")).strip()
        if not agent:
            raise ProjectConfigError(f"[queue.trios.{name}].agent is required")
        normalized_trios[name] = {
            "agent": agent,
            "model": str(spec.get("model", "")).strip() or None,
            "mcp_profile": str(spec.get("mcp_profile", "")).strip() or None,
        }
    for required in _QUEUE_TRIOS:
        if required not in normalized_trios:
            normalized_trios[required] = {
                "agent": "claude_code" if required == "kimi" else "codex",
                "model": None,
                "mcp_profile": None,
            }

    capacity = queue.get("capacity", {})
    if not isinstance(capacity, dict):
        raise ProjectConfigError("[queue.capacity] must be a TOML table")
    unknown_capacity = set(capacity) - _QUEUE_CAPACITY_KEYS
    if unknown_capacity:
        raise ProjectConfigError(
            f"unknown [queue.capacity] key(s): {_names(unknown_capacity)}"
        )
    normalized_capacity: dict[str, Any] = {
        "kimi_max": 3,
        "qwen_max": 1,
        "min_healthy_keys": 2,
        "key_health_url": "",
        "poll_seconds": 15,
    }
    if isinstance(capacity.get("kimi_max"), int):
        normalized_capacity["kimi_max"] = max(0, capacity["kimi_max"])
    if isinstance(capacity.get("qwen_max"), int):
        normalized_capacity["qwen_max"] = max(0, capacity["qwen_max"])
    if isinstance(capacity.get("min_healthy_keys"), int):
        normalized_capacity["min_healthy_keys"] = max(0, capacity["min_healthy_keys"])
    if isinstance(capacity.get("key_health_url"), str):
        normalized_capacity["key_health_url"] = capacity["key_health_url"].strip()
    if isinstance(capacity.get("poll_seconds"), (int, float)):
        normalized_capacity["poll_seconds"] = max(1.0, float(capacity["poll_seconds"]))
    return {"trios": normalized_trios, "capacity": normalized_capacity}


def _flatten_run_table(run: dict[str, Any]) -> dict[str, Any]:
    unknown = set(run) - _RUN_KEYS
    if unknown:
        raise ProjectConfigError(f"unknown [run] key(s): {_names(unknown)}")

    defaults: dict[str, Any] = {}
    for key in _STRING_KEYS:
        if key in run:
            defaults[key] = _string(run[key], f"run.{key}")

    if "agent" in run:
        defaults["agent"] = _choice(run["agent"], "run.agent", _AGENT_CHOICES)
    if "reasoning_effort" in run:
        defaults["reasoning_effort"] = _reasoning_effort(
            run["reasoning_effort"], "run.reasoning_effort"
        )
    if "env" in run:
        defaults["env"] = _choice(run["env"], "run.env", {"local"})
    if "prompt_language" in run:
        defaults["prompt_language"] = _choice(
            run["prompt_language"], "run.prompt_language", {"en", "zh"}
        )
    if "max_rounds" in run:
        defaults["max_rounds"] = _positive_int(run["max_rounds"], "run.max_rounds")
    if "dashboard" in run:
        defaults["dashboard"] = _boolean(run["dashboard"], "run.dashboard")
    if "dashboard_port" in run:
        defaults["dashboard_port"] = _port(run["dashboard_port"], "run.dashboard_port")
    if "mcp_add_dirs" in run:
        value = run["mcp_add_dirs"]
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ProjectConfigError("run.mcp_add_dirs must be an array of non-empty strings")
        defaults["mcp_add_dir"] = list(value)
    if "guard_exclude_paths" in run:
        value = run["guard_exclude_paths"]
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ProjectConfigError("run.guard_exclude_paths must be an array of non-empty strings")
        defaults["guard_exclude_path"] = list(value)
    if "mcp_profile" in run:
        defaults["mcp_profile"] = _string(run["mcp_profile"], "run.mcp_profile")
    if "allow_auditor_write_mcp" in run:
        defaults["allow_auditor_write_mcp"] = _boolean(
            run["allow_auditor_write_mcp"], "run.allow_auditor_write_mcp"
        )

    roles = run.get("roles", {})
    if not isinstance(roles, dict):
        raise ProjectConfigError("[run.roles] must be a TOML table")
    unknown_roles = set(roles) - _ROLE_NAMES
    if unknown_roles:
        raise ProjectConfigError(f"unknown role(s): {_names(unknown_roles)}")
    for role, values in roles.items():
        if not isinstance(values, dict):
            raise ProjectConfigError(f"[run.roles.{role}] must be a TOML table")
        unknown_role_keys = set(values) - {"agent", "model", "reasoning_effort", "mcp_profile"}
        if unknown_role_keys:
            raise ProjectConfigError(
                f"unknown [run.roles.{role}] key(s): {_names(unknown_role_keys)}"
            )
        if "agent" in values:
            defaults[f"{role}_agent"] = _choice(
                values["agent"], f"run.roles.{role}.agent", _AGENT_CHOICES
            )
        if "model" in values:
            defaults[f"{role}_model"] = _string(
                values["model"], f"run.roles.{role}.model"
            )
        if "reasoning_effort" in values:
            defaults[f"{role}_reasoning_effort"] = _reasoning_effort(
                values["reasoning_effort"], f"run.roles.{role}.reasoning_effort"
            )
        if "mcp_profile" in values:
            defaults[f"{role}_mcp_profile"] = _string(
                values["mcp_profile"], f"run.roles.{role}.mcp_profile"
            )

    timeouts = run.get("timeouts", {})
    if not isinstance(timeouts, dict):
        raise ProjectConfigError("[run.timeouts] must be a TOML table")
    unknown_timeouts = set(timeouts) - _TIMEOUT_NAMES
    if unknown_timeouts:
        raise ProjectConfigError(f"unknown timeout role(s): {_names(unknown_timeouts)}")
    for role, value in timeouts.items():
        defaults[f"{role}_timeout"] = _positive_int(value, f"run.timeouts.{role}")
    return defaults


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectConfigError(f"{name} must be a non-empty string")
    return value


def _choice(value: Any, name: str, choices: set[str]) -> str:
    result = _string(value, name)
    if result not in choices:
        raise ProjectConfigError(f"{name} must be one of: {_names(choices)}")
    return result


def _reasoning_effort(value: Any, name: str) -> str:
    from .agent_registry import normalise_reasoning_effort

    try:
        return normalise_reasoning_effort(_string(value, name))
    except ValueError as exc:
        raise ProjectConfigError(f"{name}: {exc}") from exc


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProjectConfigError(f"{name} must be an integer of at least 1")
    if name.endswith("max_rounds") and value > MAX_ROUNDS:
        raise ProjectConfigError(f"{name} must be at most {MAX_ROUNDS}")
    return value


def _port(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
        raise ProjectConfigError(f"{name} must be an integer from 0 to 65535")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ProjectConfigError(f"{name} must be true or false")
    return value


def _names(values: set[str]) -> str:
    return ", ".join(sorted(values))
