from __future__ import annotations

import importlib
import re
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
    "worker_memory_max",
    "roles",
    "timeouts",
}
_QUEUE_TRIOS = {"kimi", "qwen"}
# Per-caller tool scoping and budget ceilings (task 174). The complete set of
# tool names the MCP dispatch exposes; a caller's ``tools`` allowlist is
# validated against it. Granting a tool here is what also unlocks its REST
# twin (the resolve route checks the same allowlist).
_MCP_TOOL_NAMES = frozenset(
    {
        "harness_enqueue_task",
        "harness_list_queue",
        "harness_run_status",
        "harness_resolve_gate",
        "harness_list_contentions",
    }
)
# Task 235: read-only overseer-state tools over the migrated apparatus archive.
# They are known to the dispatcher but deliberately NOT scoping-eligible: the
# overseer calls them from an authenticated loopback session that carries no
# caller identity, so requiring a ``tools`` grant would 401 them.  ``tools``
# allowlists keep validating against _MCP_TOOL_NAMES only (merged with 174).
_OVERSEER_TOOL_NAMES = frozenset(
    {
        "get_queue_entry",
        "list_queue",
        "get_task_history",
        "read_ledger",
        "list_open_asks",
        "get_handoff",
    }
)
# The full dispatcher surface: unknown-tool 404 decisions use this set.
_MCP_DISPATCH_TOOL_NAMES = _MCP_TOOL_NAMES | _OVERSEER_TOOL_NAMES
_CALLER_KEYS = {
    "secret_env",
    "tools",
    "max_entries_per_hour",
    "max_rounds_clamp",
    "rest_run_control",
}
# ``anon`` is synthesized by the auth path whenever the HMAC over (caller, ts)
# is missing or bad; it always has an empty allowlist and must never be
# configurable.
_RESERVED_CALLER = "anon"
_CALLER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# Defaults from the migration doc's caller table (section 3.4/3.5): chat-agent
# gets the three queue tools and the tight ceilings; operator/overseer/hydra
# get every tool and no ceilings. ``rest_run_control`` marks the overseer's
# extra REST run-control routes (beyond the resolve twin, which follows the
# tools allowlist). Hydra enforces its own rationale + identity on top, so it
# needs no extra REST surface here. ``secret_env`` is derived per name below.
_ALL_TOOLS = sorted(_MCP_TOOL_NAMES)
_CALLER_DEFAULTS: dict[str, dict[str, Any]] = {
    "chat-agent": {
        "tools": ["harness_enqueue_task", "harness_list_queue", "harness_run_status"],
        "max_entries_per_hour": 10,
        "max_rounds_clamp": 50,
        "rest_run_control": False,
    },
    "operator": {
        "tools": _ALL_TOOLS,
        "max_entries_per_hour": None,
        "max_rounds_clamp": None,
        "rest_run_control": False,
    },
    "overseer": {
        "tools": _ALL_TOOLS,
        "max_entries_per_hour": None,
        "max_rounds_clamp": None,
        "rest_run_control": True,
    },
    "hydra": {
        "tools": _ALL_TOOLS,
        "max_entries_per_hour": None,
        "max_rounds_clamp": None,
        "rest_run_control": False,
    },
}


def _caller_secret_env(name: str) -> str:
    return f"LH_HARNESS_CALLER_{name.upper().replace('-', '_')}_SECRET"
_QUEUE_CAPACITY_KEYS = {
    "kimi_max",
    "qwen_max",
    "min_healthy_keys",
    "key_health_url",
    "poll_seconds",
    "max_retries",
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

# Per-run worker memory isolation (TASK 202 + 208). Each run's worker is
# capped at this RESIDENT-memory limit (a per-episode child cgroup's
# memory.max under the service's delegated cgroup subtree, or a systemd
# scope's MemoryMax where a manager is reachable) so one run's memory blowup
# is OOM-killed alone instead of failing the whole lh-harness service. The
# default (2G) is sized from CT110's measured agent-worker RESIDENT use — VmRSS
# 0.26 GiB, high-water 0.29 GiB (2026-09-18), against 6.0 GiB of physical RAM —
# with roughly seven times headroom and still below RAM, which is what lets the
# cgroup limit fire before the kernel OOM-kills globally. (The 9.27 GiB figure
# often quoted for these agents is VmPeak, an ADDRESS-SPACE number, and must
# never size an RSS bound.); a memory.max bound
# counts resident pages only, never the ~5.3 GiB of address space Node 22/V8
# reserves before the worker touches a page. The
# LH_HARNESS_WORKER_MEMORY_MAX environment variable overrides this value.
# worker_memory_max = "2G"

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
# max_retries = 2       # maximum number of retry attempts for failed entries

# [queue]
# observe = false       # shadow (observe) mode: the launcher computes the full
#                       # launch decision but starts NOTHING -- it appends
#                       # queue.shadow_launch / queue.shadow_skip records to
#                       # runs_root/queue/shadow.jsonl and leaves every entry
#                       # pending. Flip to false to promote the launcher; that
#                       # is the whole code change (migration §5 Step 2).
# occupancy_ignore_dirty = false
#                       # Set true to stop treating a dirty tree (git status
#                       # --porcelain non-empty) or a local branch with commits
#                       # not on origin/main as an OCCUPIED workspace. Per-
#                       # environment overseer override; active-run ownership
#                       # always applies.

# Per-caller tool scoping and budget ceilings (task 174; migration doc
# section 3.4-3.5). Every caller presents its name via the X-Harness-Caller
# REST header or the MCP `caller` field, plus an HMAC over (caller, ts) signed
# with the secret stored in the environment variable named by `secret_env`.
# THE CONFIG HOLDS THE ENV VARIABLE NAME ONLY -- never a secret value.
# Missing/bad signatures are treated as the "anon" caller, whose allowlist is
# empty (401). Defaults (used for callers omitted here):
#   chat-agent: tools = [harness_enqueue_task, harness_list_queue,
#                        harness_run_status],
#               max_entries_per_hour = 10, max_rounds_clamp = 50
#   operator:   all tools, no ceilings
#   overseer:   all tools, no ceilings, rest_run_control = true
#   hydra:      all tools, no ceilings
#
# [callers."chat-agent"]
# secret_env = "LH_HARNESS_CALLER_CHAT_AGENT_SECRET"
# tools = ["harness_enqueue_task", "harness_list_queue", "harness_run_status"]
# max_entries_per_hour = 10   # 429 + Retry-After past this many enqueues/hour
# max_rounds_clamp = 50       # enqueue with more rounds -> 422, never truncated
#
# [callers.operator]
# secret_env = "LH_HARNESS_CALLER_OPERATOR_SECRET"
#
# [callers.overseer]
# secret_env = "LH_HARNESS_CALLER_OVERSEER_SECRET"
#
# [callers.hydra]
# secret_env = "LH_HARNESS_CALLER_HYDRA_SECRET"
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
    unknown_root = set(payload) - {"run", "queue", "callers"}
    if unknown_root:
        raise ProjectConfigError(f"unknown top-level key(s): {_names(unknown_root)}")
    run = payload.get("run", {})
    if not isinstance(run, dict):
        raise ProjectConfigError("[run] must be a TOML table")
    result = _flatten_run_table(run)
    queue = payload.get("queue", {})
    if isinstance(queue, dict):
        result["queue"] = _flatten_queue_table(queue)
    result["callers"] = _flatten_callers_table(payload.get("callers", {}), source)
    return result


def config_defines_callers(path: str | Path) -> bool:
    """True only when the config file explicitly carries a [callers] table.

    Used by the WebAPI to decide whether per-caller scoping (task 174) is
    active: scoping is OFF unless the deployment declares [callers], which
    keeps the pre-174 bearer-only behavior for unconfigured deployments
    (merged decision -- see the server comment and PR).
    """
    source = Path(path)
    if not source.is_file():
        return False
    try:
        with source.open("rb") as handle:
            payload = tomllib.load(handle)
    except Exception:
        return False
    return isinstance(payload.get("callers"), dict)


def load_caller_configs(path: str | Path = PROJECT_CONFIG_PATH) -> dict[str, dict[str, Any]]:
    """Return the merged per-caller authorization table (task 174).

    Every key is a caller name ("anon" is never present: the auth path
    synthesizes it with an empty allowlist). Each value has:

    - ``secret_env``: name of the environment variable holding the caller's
      HMAC secret (name only -- the value is read only when a signature is
      verified, never from config).
    - ``tools``: MCP tool names this caller may dispatch; the REST resolve
      route is gated on ``harness_resolve_gate`` being in this list.
    - ``max_entries_per_hour``: enqueue ceiling, or None for unlimited
      (429 + Retry-After past the ceiling).
    - ``max_rounds_clamp``: highest max_rounds an enqueued entry may request,
      or None for unlimited (over it -> 422, never silently truncated).
    - ``rest_run_control``: True only for callers allowed on the overseer's
      extra REST run-control routes.
    """
    source = Path(path)
    if not source.is_file():
        return _flatten_callers_table({}, source)
    return load_run_defaults(source)["callers"]


def _flatten_callers_table(callers: Any, source: Path) -> dict[str, dict[str, Any]]:
    if not isinstance(callers, dict):
        raise ProjectConfigError(f"[callers] in {source} must be a TOML table")
    # Every configured name overlays the named-caller defaults; unknown names
    # start from deny-all (empty tools, no ceilings, no REST run control).
    merged: dict[str, dict[str, Any]] = {}
    names = set(_CALLER_DEFAULTS) | set(callers)
    for name in sorted(names):
        if name == _RESERVED_CALLER:
            raise ProjectConfigError(
                f"[callers.{name}] is reserved: the auth path always synthesizes "
                "'anon' with an empty allowlist"
            )
        if not isinstance(name, str) or not _CALLER_NAME_RE.match(name):
            raise ProjectConfigError(
                f"caller name {name!r} must match {_CALLER_NAME_RE.pattern!r}"
            )
        base = _CALLER_DEFAULTS.get(
            name,
            {
                "tools": [],
                "max_entries_per_hour": None,
                "max_rounds_clamp": None,
                "rest_run_control": False,
            },
        )
        spec: dict[str, Any] = {
            "secret_env": _caller_secret_env(name),
            "tools": list(base["tools"]),
            "max_entries_per_hour": base["max_entries_per_hour"],
            "max_rounds_clamp": base["max_rounds_clamp"],
            "rest_run_control": base["rest_run_control"],
        }
        overrides = callers.get(name, {})
        if not isinstance(overrides, dict):
            raise ProjectConfigError(f"[callers.{name}] must be a TOML table")
        unknown = set(overrides) - _CALLER_KEYS
        if unknown:
            raise ProjectConfigError(
                f"unknown [callers.{name}] key(s): {_names(unknown)}"
            )
        if "secret_env" in overrides:
            # Env NAME only; refusing empty/whitespace keeps mistakes loud.
            spec["secret_env"] = _string(overrides["secret_env"], f"callers.{name}.secret_env")
        if "tools" in overrides:
            tools = overrides["tools"]
            if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
                raise ProjectConfigError(
                    f"callers.{name}.tools must be an array of tool names"
                )
            unknown_tools = set(tools) - _MCP_TOOL_NAMES
            if unknown_tools:
                raise ProjectConfigError(
                    f"callers.{name}.tools: unknown tool(s): {_names(unknown_tools)}"
                )
            spec["tools"] = sorted(set(tools))
        if "max_entries_per_hour" in overrides:
            value = overrides["max_entries_per_hour"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProjectConfigError(
                    f"callers.{name}.max_entries_per_hour must be a non-negative integer"
                )
            spec["max_entries_per_hour"] = value
        if "max_rounds_clamp" in overrides:
            spec["max_rounds_clamp"] = _positive_int(
                overrides["max_rounds_clamp"], f"callers.{name}.max_rounds_clamp"
            )
        if "rest_run_control" in overrides:
            spec["rest_run_control"] = _boolean(
                overrides["rest_run_control"], f"callers.{name}.rest_run_control"
            )
        merged[name] = spec
    return merged


def _flatten_queue_table(queue: dict[str, Any]) -> dict[str, Any]:
    trios = queue.get("trios", {})
    if not isinstance(trios, dict):
        raise ProjectConfigError("[queue.trios] must be a TOML table")
    unknown_trios = set(trios) - _QUEUE_TRIOS
    if unknown_trios:
        raise ProjectConfigError(f"unknown queue trio(s): {_names(unknown_trios)}")
    # ``database_url`` is a legitimate key (``queue.py`` reads it to build the
    # PgQueueStore) and the flattened result carries it onward so
    # ``_select_queue_store`` receives the DSN. The URL itself never contains
    # the password: deployments supply it out-of-band via the
    # ``LH_HARNESS_DB_PASSWORD`` environment variable (name only, never a
    # value), which ``pg_queue._resolve_connection_url`` appends at connect
    # time.
    unknown_queue_keys = set(queue) - {
        "trios",
        "capacity",
        "backend",
        "observe",
        "occupancy_ignore_dirty",
        "database_url",
    }
    if unknown_queue_keys:
        raise ProjectConfigError(f"unknown [queue] key(s): {_names(unknown_queue_keys)}")
    observe = queue.get("observe", False)
    if not isinstance(observe, bool):
        raise ProjectConfigError("[queue].observe must be a boolean")
    # Occupancy override (task 173, scope 4): some environments (e.g. one
    # workspace shared by sequential operators) legitimately keep dirty trees;
    # the overseer flips this to true to disable only the dirty-tree and
    # unpushed-branch occupancy probes, never the active-run rule.
    occupancy_ignore_dirty = queue.get("occupancy_ignore_dirty", False)
    if not isinstance(occupancy_ignore_dirty, bool):
        raise ProjectConfigError("[queue].occupancy_ignore_dirty must be a boolean")

    # ``backend`` selects the storage engine. The file store is the default and
    # stays hermetic; only ``postgres`` reaches PgQueueStore.
    backend = str(queue.get("backend", "file")).strip().lower()
    if backend not in {"file", "postgres"}:
        raise ProjectConfigError(f"unknown [queue.backend]: {backend!r}")
    normalized_backend = backend

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
        "max_retries": 2,
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
    if isinstance(capacity.get("max_retries"), int):
        if capacity["max_retries"] < 0:
            raise ProjectConfigError("[queue.capacity].max_retries must be non-negative")
        normalized_capacity["max_retries"] = capacity["max_retries"]
    else:
        # If present but not int, raise error
        if "max_retries" in capacity:
            raise ProjectConfigError("[queue.capacity].max_retries must be an integer")
    database_url = queue.get("database_url", "")
    if not isinstance(database_url, str):
        raise ProjectConfigError("[queue].database_url must be a string")
    return {
        "trios": normalized_trios,
        "capacity": normalized_capacity,
        "backend": normalized_backend,
        "observe": observe,
        "occupancy_ignore_dirty": occupancy_ignore_dirty,
        "database_url": database_url.strip(),
    }


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
    if "worker_memory_max" in run:
        defaults["worker_memory_max"] = _worker_memory_max(run["worker_memory_max"])

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


def _worker_memory_max(value: Any) -> str:
    from .worker_isolation import parse_memory_limit

    try:
        return parse_memory_limit(value)
    except ValueError as exc:
        raise ProjectConfigError(f"run.worker_memory_max: {exc}") from exc


def _names(values: set[str]) -> str:
    return ", ".join(sorted(values))
