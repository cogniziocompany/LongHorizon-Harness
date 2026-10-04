"""Child-process hygiene for the harness service (task A3d security review H-B).

Harness workers run as the same uid as the service and control the git
repositories the service inspects (a run can write ``.git/config``). So:

- At startup :func:`seal_process_environment` moves the control-plane
  secrets (``LH_HARNESS_WEB_TOKEN``, ``LH_HARNESS_CALLER_*``) out of
  ``os.environ`` into memory and marks the process non-dumpable, so no child
  inherits them and a same-uid process cannot read ``/proc/<pid>/environ``.
- Every service-side git call uses :func:`git_argv` (``core.fsmonitor`` and
  ``core.hooksPath`` neutralised, so a worker-planted fsmonitor or hook does
  not run on ``git status``) and :func:`child_env` (no secrets, no prompts).
"""

from __future__ import annotations

import os
from typing import Mapping, MutableMapping

SAFE_GIT_CONFIG = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null")
DENY_ENV_PREFIXES = ("LH_HARNESS_CALLER_", "LH_HARNESS_ASK_")
DENY_ENV_NAMES = ("LH_HARNESS_WEB_TOKEN",)
SECRET_ENV_PREFIXES = ("LH_HARNESS_CALLER_",)
SECRET_ENV_NAMES = ("LH_HARNESS_WEB_TOKEN",)

PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3


def scrub_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy of ``env`` (default ``os.environ``) without control-plane settings."""
    source = os.environ if env is None else env
    return {
        k: v
        for k, v in source.items()
        if k not in DENY_ENV_NAMES and not k.startswith(DENY_ENV_PREFIXES)
    }


def child_env(env: Mapping[str, str] | None = None, **extra: str) -> dict[str, str]:
    """Environment for a service-side child (git, gh): scrubbed, never prompting."""
    out = scrub_env(env)
    out.setdefault("GIT_TERMINAL_PROMPT", "0")
    out.update(extra)
    return out


def git_argv(*args: str) -> list[str]:
    """``git`` with fsmonitor and hooks neutralised, then ``args``."""
    return ["git", *SAFE_GIT_CONFIG, *args]


def _prctl(option: int, value: int = 0) -> int:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    return int(libc.prctl(option, value, 0, 0, 0))


def make_non_dumpable() -> bool:
    try:
        return _prctl(PR_SET_DUMPABLE, 0) == 0
    except Exception:  # pragma: no cover - non-Linux
        return False


def is_non_dumpable() -> bool:
    try:
        return _prctl(PR_GET_DUMPABLE) == 0
    except Exception:  # pragma: no cover - non-Linux
        return False


def seal_process_environment(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Move secrets from the environment into memory; make the process non-dumpable.

    Called once by the web service at startup (after the bearer token has
    been read). Returns the NAMES moved (never values). Caller secrets stay
    usable through :func:`lh_harness.caller_auth.captured_secret`.
    """
    from .caller_auth import capture_secret

    environ = os.environ if environ is None else environ
    moved = []
    for name in list(environ):
        if name in SECRET_ENV_NAMES or name.startswith(SECRET_ENV_PREFIXES):
            capture_secret(name, environ.pop(name))
            moved.append(name)
    make_non_dumpable()
    return sorted(moved)


__all__ = [
    "SAFE_GIT_CONFIG",
    "child_env",
    "git_argv",
    "is_non_dumpable",
    "make_non_dumpable",
    "scrub_env",
    "seal_process_environment",
]
