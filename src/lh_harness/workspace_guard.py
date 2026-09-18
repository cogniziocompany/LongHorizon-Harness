"""Workspace branch guard: never launch onto another task's branch silently.

Measured defect (runs 7784478f on ``fix/setup-wizard-prod`` under OPEN PR
#108, and 96563c4c on the already-pushed ``fix/mcp-namespace-contract`` under
OPEN PR #154, 2026-09-16): a run was launched into a workspace whose
checked-out branch already carried another task's work or OPEN PR, so a
commit+push from the run would have landed inside that other task's PR.

``prepare_workspace_base`` runs before ``supervisor.create_run`` and resolves
a launch-safe base:

1. The run is never launched on a checked-out branch other than the repo's
   default.  The default branch is taken fresh from ``origin`` and the run's
   own branch is cut from it.
2. Another task's work is never destroyed.  A dirty tree or a branch ahead of
   its remote is left completely alone; the run instead gets a fresh linked
   worktree branched from ``origin/<default>`` (named-stash fallback when a
   worktree cannot be created).
3. If the checked-out branch carries an OPEN pull request, or no clean
   default base can be resolved, the launch fails loudly naming the branch
   and any colliding PR — it never proceeds onto whatever was checked out.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_GIT_TIMEOUT = 90
_GH_TIMEOUT = 30
_MAX_MESSAGE = 4_000


class WorkspaceBaseError(RuntimeError):
    """No clean default-branch base could be resolved; the launch must fail."""


@dataclass
class WorkspaceBase:
    """A resolved launch base for one workspace."""

    workspace: Path
    mode: str  # "not-a-repo" | "on-default" | "in-place" | "worktree" | "stash"
    original_branch: str
    default_branch: str
    run_branch: str | None = None
    stashed: bool = False

    def summary(self) -> str:
        parts = [f"mode={self.mode}", f"checked-out-branch='{self.original_branch}'"]
        if self.run_branch:
            parts.append(f"run-branch='{self.run_branch}'")
        parts.append(f"base=origin/{self.default_branch}" if self.default_branch else "base=unresolved")
        if self.stashed:
            parts.append("leftover-preserved-in-named-stash")
        return "; ".join(parts)


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo``; raise WorkspaceBaseError on failure."""

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise WorkspaceBaseError(f"git is unavailable in workspace {repo}: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "no output"
        raise WorkspaceBaseError(
            f"git {' '.join(args)} failed in {repo}: {tail[:400]}"
        )
    return proc.stdout.strip()


def _git_soft(repo: Path, *args: str) -> str | None:
    """Run git in ``repo``; return None when the command fails."""

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return None if proc.returncode != 0 else proc.stdout.strip()


def _unique_branch(repo: Path, base: str) -> str:
    candidate = base
    for attempt in range(4):
        if not _git_soft(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}"):
            return candidate
        candidate = f"{base}-{uuid.uuid4().hex[:4]}"
    raise WorkspaceBaseError(f"cannot find an unused run branch name based on '{base}' in {repo}")


def probe_open_pr_gh(repo: Path, branch: str) -> str | None:
    """Return a description of an OPEN PR whose head is ``branch``, if any.

    Uses ``gh`` when it is installed; any failure means "unknown" (None) so a
    missing or broken CLI never fails a launch by itself.
    """

    gh = shutil.which("gh")
    if not gh:
        return None
    env = os.environ.copy()
    # Control-plane credentials must not be needed by, or leak into, the probe.
    env.pop("LH_HARNESS_WEB_TOKEN", None)
    try:
        proc = subprocess.run(
            [gh, "pr", "list", "--head", branch, "--state", "open",
             "--json", "number,title,url", "--limit", "3"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
            env=env,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        items = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(items, list) or not items:
        return None
    parts = []
    for item in items[:2]:
        number = item.get("number")
        title = str(item.get("title") or "").strip()[:60]
        url = item.get("url")
        parts.append(f"#{number} '{title}' {url}".strip())
    return ", ".join(parts)


def _detect_default_branch(repo: Path) -> str:
    symref = _git_soft(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
    if symref and symref.startswith("refs/remotes/origin/"):
        return symref[len("refs/remotes/origin/"):]
    for guess in ("main", "master"):
        if _git_soft(repo, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{guess}"):
            return guess
    # Last resort: ask the remote directly (works offline for local remotes).
    ls = _git_soft(repo, "ls-remote", "--symref", "origin", "HEAD")
    if ls:
        match = re.search(r"refs/heads/(\S+)\s+HEAD", ls)
        if match:
            return match.group(1)
    raise WorkspaceBaseError(
        f"cannot determine the default branch of 'origin' in {repo} "
        "(no origin/HEAD symref and no origin/main or origin/master)"
    )


def prepare_workspace_base(
    workspace: str | Path,
    *,
    run_label: str,
    base_root: str | Path | None = None,
    probe_open_pr: Callable[[Path, str], str | None] | None = probe_open_pr_gh,
) -> WorkspaceBase:
    """Resolve a launch-safe base for ``workspace``, preserving foreign work.

    Returns the path the run should execute in (the original workspace, or a
    freshly created linked worktree branched from ``origin/<default>``).
    Raises :class:`WorkspaceBaseError` when no clean base is resolvable; the
    message always names the checked-out branch and, when known, any
    colliding OPEN PR.
    """

    original = str(workspace)
    repo = Path(original)
    if not repo.is_absolute():
        repo = (Path(base_root) if base_root else Path.cwd()) / repo
    repo = Path(os.path.normpath(str(repo)))

    is_repo = (repo / ".git").exists() or _git_soft(repo, "rev-parse", "--git-dir") is not None
    if not repo.is_dir() or not is_repo:
        # Not a git checkout (e.g. a fresh workspace root): nothing to guard.
        return WorkspaceBase(
            workspace=repo, mode="not-a-repo", original_branch="", default_branch=""
        )

    if _git_soft(repo, "rev-parse", "--is-inside-work-tree") != "true":
        raise WorkspaceBaseError(
            f"workspace {repo} is not a usable git work tree; refusing to launch"
        )

    current = (_git_soft(repo, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD").strip()

    def _prefix(msg: str) -> str:
        return f"checked-out branch '{current}': {msg}"

    try:
        default = _detect_default_branch(repo)
    except WorkspaceBaseError as exc:
        raise WorkspaceBaseError(
            _prefix(f"{exc}; refusing to launch onto the checked-out branch")
        ) from exc

    if current == default:
        return WorkspaceBase(
            workspace=repo, mode="on-default", original_branch=current, default_branch=default
        )

    # Non-default branch: never launch silently on it (deliverable 1).
    colliding_pr = probe_open_pr(repo, current) if probe_open_pr else None
    if colliding_pr:
        raise WorkspaceBaseError(
            _prefix(
                f"carries another task's OPEN pull request ({colliding_pr}); "
                f"refusing to launch onto it — the run must start from a fresh "
                f"branch cut from origin/{default}"
            )
        )

    try:
        _git(repo, "fetch", "origin", "--prune")
    except WorkspaceBaseError as exc:
        raise WorkspaceBaseError(
            _prefix(f"cannot refresh origin for a clean base: {exc}; refusing to launch")
        ) from exc

    origin_default = _git_soft(repo, "rev-parse", "--verify", f"refs/remotes/origin/{default}")
    if not origin_default:
        raise WorkspaceBaseError(
            _prefix(
                f"no clean base: refs/remotes/origin/{default} does not exist; refusing to launch"
            )
        )

    run_branch = _unique_branch(repo, f"lh-run/{run_label}")

    dirty = bool((_git_soft(repo, "status", "--porcelain") or "").strip())
    upstream = _git_soft(repo, "rev-parse", "--abbrev-ref", f"{current}@{{upstream}}")
    ahead = False
    if upstream:
        count = _git_soft(repo, "rev-list", "--count", f"{upstream}..{current}")
        ahead = bool(count and count.strip() not in ("", "0"))

    if not dirty and not ahead:
        # Clean base: switch in place.  Nothing is lost — the previous branch
        # keeps every commit and the tree was clean.
        _git(repo, "checkout", "-B", run_branch, f"refs/remotes/origin/{default}")
        return WorkspaceBase(
            workspace=repo,
            mode="in-place",
            original_branch=current,
            default_branch=default,
            run_branch=run_branch,
        )

    # Foreign work present (dirty tree and/or unpushed commits): leave the
    # checked-out branch completely alone and put the run in a fresh linked
    # worktree branched from origin/<default>.
    sibling = repo.parent / f"{repo.name}.run-{run_label}"
    if base_root is not None:
        root = Path(base_root)
        try:
            sibling.relative_to(root)
        except ValueError:
            sibling = root / f"{repo.name}.run-{run_label}"
    try:
        _git(repo, "worktree", "add", "-b", run_branch, str(sibling), f"refs/remotes/origin/{default}")
        return WorkspaceBase(
            workspace=Path(os.path.normpath(str(sibling))),
            mode="worktree",
            original_branch=current,
            default_branch=default,
            run_branch=run_branch,
        )
    except WorkspaceBaseError:
        pass

    # Worktree unavailable: named stash, then switch in place.  The leftover
    # survives in a stash whose message names the branch it came from.
    stash_message = (
        f"lh-harness prelaunch guard: uncommitted work on '{current}' preserved "
        f"before cutting run branch (label {run_label})"
    )
    stashed = _git_soft(repo, "stash", "push", "--include-untracked", "--message", stash_message)
    if stashed is None:
        raise WorkspaceBaseError(
            f"cannot preserve uncommitted work on checked-out branch '{current}' in {repo} "
            f"(worktree and named stash both failed); refusing to launch — no clean base"
        )
    _git(repo, "checkout", "-B", run_branch, f"refs/remotes/origin/{default}")
    return WorkspaceBase(
        workspace=repo,
        mode="stash",
        original_branch=current,
        default_branch=default,
        run_branch=run_branch,
        stashed=True,
    )


def resolve_run_base(
    workspace: str | Path | None,
    *,
    run_label: str,
    base_root: str | Path | None = None,
    probe_open_pr: Callable[[Path, str], str | None] | None = probe_open_pr_gh,
) -> tuple[WorkspaceBase | None, str | None]:
    """Resolve the guard base and the workspace a run must execute in.

    The one guard helper shared by the ``supervisor.create_run`` call sites
    (the queue ``Launcher`` and ``POST /api/runs``) so neither grows a second
    copy of the try/prepare/worktree-selection logic:

    * ``workspace=None`` (no explicit workspace in the request) skips the
      guard and returns ``(None, None)`` — the supervisor then uses its
      configured workspace root, exactly as before the guard existed.
    * otherwise the guard resolves a launch-safe base and the run executes in
      ``base.workspace`` only when the guard produced a linked worktree, and
      in the requested workspace for every other mode.

    Raises :class:`WorkspaceBaseError` when no clean base can be resolved; the
    message names the checked-out branch and, when known, any colliding PR.
    """

    if workspace is None or not str(workspace).strip():
        return None, None
    base = prepare_workspace_base(
        workspace,
        run_label=run_label,
        base_root=base_root,
        probe_open_pr=probe_open_pr,
    )
    effective = str(base.workspace if base.mode == "worktree" else workspace)
    return base, effective
