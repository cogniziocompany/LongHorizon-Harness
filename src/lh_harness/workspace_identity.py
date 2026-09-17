"""Read-only identity for a workspace path.

HARD RULES
(1) NEVER invoke any ``git worktree`` subcommand.  Windows git treats
    WSL-created worktrees as prunable and prune/remove destroys live trees;
    ``worktree list`` is the tempting adjacent API and must stay out.
(2) --no-optional-locks / GIT_OPTIONAL_LOCKS=0 is not optional.  A bare
    rev-parse can refresh and lock the index while an executor is mid-commit,
    which would make the harness CAUSE the collision it reports.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_GIT_TIMEOUT = 5.0
_CACHE_TTL = 60.0
_MAX_CACHE_SIZE = 256


@dataclass(frozen=True)
class WorkspaceIdentity:
    """Stable identity for one workspace path, derived from git metadata."""

    path: str
    key: str = ""
    is_git: bool = False
    repo_id: str | None = None
    branch: str | None = None
    git_common_dir: str | None = None
    resolved_at: float = 0.0
    error: str | None = None


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    return env


def _popen_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": _GIT_TIMEOUT,
        "check": False,
        "env": _git_env(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs


def _run_git(path: str, *args: str) -> str:
    """Run git in ``path`` and return stdout, swallowing all failures as empty."""

    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", path, *args],
            **_popen_kwargs(),
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _absolutise_git_dir(worktree_path: str, git_dir: str) -> str:
    """Make a relative ``.git`` directory comparable with absolute worktree paths."""

    if os.path.isabs(git_dir):
        return os.path.normcase(os.path.realpath(git_dir))
    joined = os.path.join(worktree_path, git_dir)
    return os.path.normcase(os.path.realpath(joined))


def _parse_rev_parse_output(raw: str) -> tuple[str | None, str | None]:
    """Return (git_common_dir, branch) from the two-line rev-parse output."""

    lines = raw.splitlines()
    if not lines:
        return None, None
    # With --path-format=absolute we expect two lines.  On very old git the
    # second rev-parse call is omitted, so tolerate a single line.
    git_dir = lines[0].strip()
    branch = lines[1].strip() if len(lines) > 1 else ""
    # detached HEAD or missing branch -> None
    branch = branch if branch and not branch.startswith("(") else None
    return git_dir, branch


def _git_common_dir_and_branch(path: str) -> tuple[str | None, str | None]:
    """Two read-only subprocesses to learn where the git metadata lives.

    ``--git-common-dir`` is queried separately from ``--abbrev-ref HEAD`` so a
    brand-new repository with no commits still reports its commondir even though
    HEAD cannot be resolved.
    """

    worktree_path = os.path.realpath(path)
    common_dir: str | None = None

    # First: commondir with absolute formatting when git is new enough.
    first = _run_git(
        worktree_path,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    if first.strip():
        common_dir = _absolutise_git_dir(worktree_path, first.strip())
    else:
        # Fallback for git < 2.31.
        second = _run_git(worktree_path, "rev-parse", "--git-common-dir")
        if second.strip():
            common_dir = _absolutise_git_dir(worktree_path, second.strip())

    # Second: current branch.  ``rev-parse --abbrev-ref HEAD`` fails on an
    # unborn branch, so fall back to ``symbolic-ref --short HEAD``.
    branch_raw = _run_git(worktree_path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if not branch_raw or branch_raw == "HEAD":
        branch_raw = _run_git(worktree_path, "symbolic-ref", "--short", "HEAD").strip()
    branch = branch_raw if branch_raw and branch_raw != "HEAD" else None

    return common_dir, branch


def _remote_origin_url(path: str) -> str:
    """One read-only config lookup; never touches the network."""

    return _run_git(path, "config", "--get", "remote.origin.url")


def _normalise_remote_url(raw: str) -> str | None:
    """Return a stable repo id string from a remote URL, or None."""

    text = raw.strip()
    if not text:
        return None
    # SCP-style "[user@]host:path" is not a real URL.  Detect it before
    # urlsplit turns the host into a bogus scheme.  SCP URLs never contain ``://``.
    if "://" not in text:
        scp_match = re.match(r"^(?:[^@]+@)?([^/:]+):(.+)$", text)
        if scp_match:
            host = scp_match.group(1).lower()
            path = scp_match.group(2).lower()
            path = re.sub(r"\.git$", "", path, flags=re.IGNORECASE)
            return f"remote:{host}/{path.lstrip('/')}"
    # Real URL: urlsplit removes credentials from hostname for standard URLs.
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    path = re.sub(r"\.git$", "", path, flags=re.IGNORECASE)
    if host:
        return f"remote:{host}{path}"
    return None


def _repo_id(common_dir: str | None, remote_url: str) -> str | None:
    if remote_url:
        normalised = _normalise_remote_url(remote_url)
        if normalised:
            return normalised
    if common_dir:
        digest = hashlib.sha256(common_dir.encode("utf-8")).hexdigest()
        return f"commondir:{digest[:16]}"
    return None


def _resolve_identity(path: str) -> WorkspaceIdentity:
    """Build an identity for one path, never raising."""

    now = time.time()
    try:
        real = os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return WorkspaceIdentity(
            path=path, key=os.path.normcase(os.path.abspath(path)), resolved_at=now
        )

    common_dir, branch = _git_common_dir_and_branch(real)
    if common_dir is None:
        return WorkspaceIdentity(
            path=path, key=real, is_git=False, branch=branch, resolved_at=now
        )

    remote_url = _remote_origin_url(real)
    repo = _repo_id(common_dir, remote_url)
    return WorkspaceIdentity(
        path=path,
        key=real,
        is_git=True,
        repo_id=repo,
        branch=branch,
        git_common_dir=common_dir,
        resolved_at=now,
    )


class _IdentityCache:
    """Bounded TTL cache for workspace identities."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, WorkspaceIdentity]] = {}

    def get(self, path: str) -> WorkspaceIdentity | None:
        with self._lock:
            inserted, identity = self._entries.get(path, (0.0, None))  # type: ignore[misc]
            if identity is not None and (time.time() - inserted) < _CACHE_TTL:
                return identity
            return None

    def set(self, path: str, identity: WorkspaceIdentity) -> None:
        with self._lock:
            # Simple LRU eviction on overflow.
            while len(self._entries) >= _MAX_CACHE_SIZE:
                try:
                    self._entries.pop(next(iter(self._entries)))
                except RuntimeError:
                    break
            self._entries[path] = (time.time(), identity)


_CACHE = _IdentityCache()


def resolve_workspace(path: str) -> WorkspaceIdentity:
    """Resolve a single workspace path with caching."""

    cached = _CACHE.get(path)
    if cached is not None:
        return cached
    identity = _resolve_identity(path)
    _CACHE.set(path, identity)
    return identity


def resolve_many(paths: list[str], budget_seconds: float = 3.0) -> dict[str, WorkspaceIdentity]:
    """Resolve many paths without letting a hung filesystem stall a tick.

    Each uncached path costs up to two git subprocesses.  We enforce a hard
    wall-clock budget: once exhausted, remaining paths return a minimal fallback.
    """

    deadline = time.time() + max(0.0, budget_seconds)
    results: dict[str, WorkspaceIdentity] = {}
    for path in paths:
        if time.time() > deadline:
            results[path] = WorkspaceIdentity(
                path=path,
                key=os.path.normcase(os.path.abspath(path)),
                error="resolve budget exhausted",
                resolved_at=time.time(),
            )
            _CACHE.set(path, results[path])
            continue
        results[path] = resolve_workspace(path)
    return results


def clear_cache() -> None:
    """Clear the module-level identity cache.  Useful in tests."""

    with _CACHE._lock:
        _CACHE._entries.clear()
