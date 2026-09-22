"""Test-only helpers for task-174 caller identity.

The secret VALUES are generated per test process (``secrets.token_hex``) and
installed into the environment variable NAMES the config loader derives by
default, so no secret value ever appears in code -- only the env NAME does.
Every helper returns signature headers/arguments whose timestamp is fresh.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any

from lh_harness.caller_auth import compute_signature
from lh_harness.config import _caller_secret_env, load_caller_configs

_CALLERS = ("chat-agent", "operator", "overseer", "hydra")

_SECRETS: dict[str, str] = {}


def build_specs(overrides: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Full caller table = loader defaults with per-caller overrides merged in.

    Test callers pass partial overrides (``{"chat-agent":
    {"max_entries_per_hour": 2}}``); the per-caller merge mirrors what
    ``_flatten_callers_table`` does for a config file so tests can hand the
    result straight to ``create_app(caller_configs=...)``.
    """
    specs = load_caller_configs()
    for name, overrides_for_caller in (overrides or {}).items():
        if name not in specs:
            specs[name] = {
                "secret_env": _caller_secret_env(name),
                "tools": [],
                "max_entries_per_hour": None,
                "max_rounds_clamp": None,
                "rest_run_control": False,
            }
        specs[name] = {**specs[name], **(overrides_for_caller or {})}
    return specs


def install_caller_secret(caller: str = "overseer") -> str:
    """Generate a throwaway secret for ``caller`` and set its env var."""
    if caller not in _SECRETS:
        _SECRETS[caller] = secrets.token_hex(16)
    env_name = _caller_secret_env(caller)
    os.environ[env_name] = _SECRETS[caller]
    return env_name


def clear_caller_secrets() -> None:
    for caller in _CALLERS:
        os.environ.pop(_caller_secret_env(caller), None)
        _SECRETS.pop(caller, None)


def caller_secret(caller: str) -> str:
    if caller not in _SECRETS:
        install_caller_secret(caller)
    return _SECRETS[caller]


def caller_headers(caller: str = "overseer") -> dict[str, str]:
    """REST headers presenting a valid ``caller`` identity (fresh timestamp)."""
    ts = str(int(time.time()))
    return {
        "X-Harness-Caller": caller,
        "X-Harness-Timestamp": ts,
        "X-Harness-Signature": compute_signature(caller, ts, caller_secret(caller)),
    }


def unsigned_headers(caller: str = "overseer") -> dict[str, str]:
    """Caller header without the signature pair (the legacy bare-name shape)."""
    return {"X-Harness-Caller": caller}


def caller_args(
    arguments: dict[str, Any] | None = None, caller: str = "overseer"
) -> dict[str, Any]:
    """MCP dispatch arguments with the caller identity block added."""
    ts = str(int(time.time()))
    payload: dict[str, Any] = dict(arguments or {})
    payload.update(
        {
            "caller": caller,
            "caller_ts": ts,
            "caller_sig": compute_signature(caller, ts, caller_secret(caller)),
        }
    )
    return payload


def rest_headers_with_ts(
    caller: str, ts: str, *, secret_env_override: str | None = None
) -> dict[str, str]:
    """Build REST headers signed with a specific (test-controlled) timestamp."""
    env_name = secret_env_override or _caller_secret_env(caller)
    secret = os.environ.get(env_name) or caller_secret(caller)
    return {
        "X-Harness-Caller": caller,
        "X-Harness-Timestamp": ts,
        "X-Harness-Signature": compute_signature(caller, ts, secret),
    }