#!/usr/bin/env python3
"""Fleet-node config materializer.

Reads the harness's own CONFIG_TEMPLATE (src/lh_harness/config.py), overlays
CT110 fleet defaults and install-time env overrides, and writes a validated
project config.toml.  Using the template guarantees the emitted file contains
exactly the keys the harness understands — no invented keys, and all eight
[run.roles.<role>] blocks are present.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

try:
    from lh_harness.config import (
        CONFIG_TEMPLATE,
        _ROLE_NAMES,
        load_run_defaults,
    )
except Exception as exc:  # pragma: no cover - surfaced at image build/run time
    print(f"lh-harness-node: cannot import harness config module: {exc}", file=sys.stderr)
    sys.exit(1)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    import tomli as tomllib  # type: ignore[no-redef]


# Env → value parsers

def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    return int(raw.strip()) if raw.strip() else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# TOML writer (schema is bounded by CONFIG_TEMPLATE, so a small emitter is enough)

def _fmt_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise TypeError(f"cannot format {type(value).__name__} for TOML")


def _fmt_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_fmt_scalar(item) for item in value) + "]"
    return _fmt_scalar(value)


def _write_table(
    lines: list[str], table: dict[str, Any], path: str, *, emit_header: bool = True
) -> None:
    scalar_keys = [k for k, v in table.items() if not isinstance(v, dict)]
    child_keys = [k for k, v in table.items() if isinstance(v, dict)]
    # Emit the header only if this table has scalar keys or no children.
    # Tables that only hold sub-tables (e.g. [run.roles]) are represented
    # implicitly by their child sections, matching the CONFIG_TEMPLATE style.
    if emit_header and (scalar_keys or not child_keys):
        lines.append(f"\n[{path}]")
    for key in scalar_keys:
        lines.append(f"{key} = {_fmt_value(table[key])}")
    for key in child_keys:
        _write_table(lines, table[key], f"{path}.{key}", emit_header=True)


def _write_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, dict):
            _write_table(lines, value, key, emit_header=True)
        else:
            lines.append(f"{key} = {_fmt_value(value)}")
    if lines and lines[0].startswith("\n"):
        lines[0] = lines[0].lstrip("\n")
    return "\n".join(lines).rstrip() + "\n"


# Main

def _apply_overlays(config: dict[str, Any]) -> dict[str, Any]:
    run = config.setdefault("run", {})
    if not isinstance(run, dict):
        raise ValueError("[run] must be a TOML table")

    run["agent"] = _env_str("LH_NODE_AGENT", "claude_code")
    run["model"] = _env_str("LH_NODE_MODEL", "kimi-k2.7-code:cloud")
    run["runs_root"] = _env_str("LH_NODE_RUNS_ROOT", "/home/harness/runs")
    run["prompt_language"] = _env_str("LH_NODE_PROMPT_LANGUAGE", "en")
    run["max_rounds"] = _env_int("LH_NODE_MAX_ROUNDS", 25)
    run["dashboard"] = _env_bool("LH_NODE_DASHBOARD", False)
    run["guard_exclude_paths"] = _env_csv(
        "LH_NODE_GUARD_EXCLUDE_PATHS", ["node_modules", "graphify-out", "logs"]
    )

    timeouts = run.setdefault("timeouts", {})
    if not isinstance(timeouts, dict):
        raise ValueError("[run.timeouts] must be a TOML table")
    timeouts["manager"] = _env_int("LH_NODE_TIMEOUT_MANAGER", 300)
    timeouts["gui_executor"] = _env_int("LH_NODE_TIMEOUT_GUI_EXECUTOR", 5400)
    timeouts["cli_executor"] = _env_int("LH_NODE_TIMEOUT_CLI_EXECUTOR", 5400)
    timeouts["auditor"] = _env_int("LH_NODE_TIMEOUT_AUDITOR", 600)

    # Role overlays: the template already has all eight [run.roles.<role>] blocks.
    # We only fill in values the operator supplied; everything else is left empty
    # so the harness falls back to the top-level agent/model.
    roles = run.setdefault("roles", {})
    if not isinstance(roles, dict):
        raise ValueError("[run.roles] must be a TOML table")

    present_roles = set(roles)
    if present_roles != _ROLE_NAMES:
        missing = sorted(_ROLE_NAMES - present_roles)
        extra = sorted(present_roles - _ROLE_NAMES)
        raise ValueError(
            f"CONFIG_TEMPLATE role set mismatch: missing={missing}, extra={extra}"
        )

    for role in sorted(_ROLE_NAMES):
        role_table = roles.setdefault(role, {})
        if not isinstance(role_table, dict):
            raise ValueError(f"[run.roles.{role}] must be a TOML table")
        model = os.environ.get(f"LH_NODE_{role.upper()}_MODEL", "").strip()
        if model:
            role_table["model"] = model
        agent = os.environ.get(f"LH_NODE_{role.upper()}_AGENT", "").strip()
        if agent:
            role_table["agent"] = agent

    return config


def materialize(config_file: str | Path) -> Path:
    target = Path(config_file)
    target.parent.mkdir(parents=True, exist_ok=True)

    template = tomllib.loads(CONFIG_TEMPLATE)
    config = _apply_overlays(template)
    target.write_text(_write_toml(config), encoding="utf-8")

    # Validate: this raises ProjectConfigError for invented keys or bad values.
    load_run_defaults(target)
    return target


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] != "--config-file":
        print("usage: materialize_node_config.py --config-file <path>", file=sys.stderr)
        sys.exit(2)
    config_file = sys.argv[2]
    try:
        path = materialize(config_file)
    except Exception as exc:
        print(f"lh-harness-node: failed to materialize {config_file}: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"lh-harness-node: materialized {path}")


if __name__ == "__main__":
    main()
