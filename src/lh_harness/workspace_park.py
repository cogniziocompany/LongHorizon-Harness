"""Auto-park a workspace when its run reaches a terminal state.

After a run ends, the workspace is detached from its run branch and reset to
origin's default branch so the launcher no longer refuses later tasks with
"workspace base refused: checked-out branch ... carries another task's OPEN pull
request" or "occupied: checked-out branch" / "dirty tree".

Nothing is ever discarded: a dirty tree or unpushed commits are preserved under a
backup ref and a named stash, both recorded in the run's report and events.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 90
_BACKUP_REF_NS = "refs/lh-harness/parked"


class WorkspaceParkError(RuntimeError):
    """Parking failed in a non-recoverable way; the workspace is left as-is."""


def _git(repo: Path, *args: str, check: bool = True, timeout: float = _GIT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """Run git in ``repo``; return the completed process."""

    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "lh-harness")
    env.setdefault("GIT_AUTHOR_EMAIL", "lh-harness@example.invalid")
    env.setdefault("GIT_COMMITTER_NAME", "lh-harness")
    env.setdefault("GIT_COMMITTER_EMAIL", "lh-harness@example.invalid")
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "no output"
        raise WorkspaceParkError(
            f"git {' '.join(args)} failed in {repo}: {tail[:400]}"
        )
    return proc


def _git_output(repo: Path, *args: str, default: str | None = None) -> str | None:
    """Run git and return stdout, or ``default`` on failure."""

    try:
        return _git(repo, *args).stdout.strip() or default
    except (WorkspaceParkError, subprocess.SubprocessError, OSError):
        return default


def _is_git_repo(repo: Path) -> bool:
    return (_git_output(repo, "rev-parse", "--git-dir") or "") != ""


def _is_inside_worktree(repo: Path) -> bool:
    return _git_output(repo, "rev-parse", "--is-inside-work-tree") == "true"


def _current_branch(repo: Path) -> str | None:
    return _git_output(repo, "rev-parse", "--abbrev-ref", "HEAD")


def _tree_is_clean(repo: Path) -> bool:
    return not (_git_output(repo, "status", "--porcelain") or "").strip()


def _detect_default_branch(repo: Path) -> str:
    """Resolve the default branch from origin, with the powerplatform override.

    The canonical order is: ``refs/remotes/origin/HEAD`` symbolic ref, then a
    fallback to ``main`` (or ``master``).  Workspaces whose name contains
    ``powerplatform`` explicitly use ``develop`` when origin/HEAD does not name
    a branch, matching the fleet convention.
    """

    symref = _git_output(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
    if symref and symref.startswith("refs/remotes/origin/"):
        return symref[len("refs/remotes/origin/"):]

    workspace_name = repo.name.lower()
    if "powerplatform" in workspace_name:
        if _git_output(repo, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/develop"):
            return "develop"

    for guess in ("main", "master"):
        if _git_output(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{guess}"):
            return guess

    ls = _git_output(repo, "ls-remote", "--symref", "origin", "HEAD")
    if ls:
        match = re.search(r"refs/heads/(\S+)\s+HEAD", ls)
        if match:
            return match.group(1)

    if "powerplatform" in workspace_name:
        return "develop"
    raise WorkspaceParkError(
        f"cannot determine the default branch of 'origin' in {repo} "
        "(no origin/HEAD symref and no origin/main or origin/master)"
    )


def _head_contained_in_origin_branch(repo: Path, branch: str) -> bool:
    """Fetch ``origin/<branch>`` and verify HEAD is contained in the fetched ref.

    The containment check is performed against the freshly fetched remote ref,
    never a stale local tracking ref, and it is exact: ``merge-base
    --is-ancestor`` against the full remote ref name.  A substring scan of
    ``branch -r --contains`` output would let ``origin/main-v2`` vouch for a
    check of ``origin/main`` (or ``origin/maintenance`` for ``origin/main``),
    so a loose name match must never satisfy the check.
    """

    _git(repo, "fetch", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}")
    # Exact containment: exit 0 only when HEAD is a real ancestor of the
    # freshly fetched tip.  Naming the full remote ref (not the abbreviated
    # ``origin/<branch>`` display form) keeps the comparison unambiguous.
    proc = _git(
        repo,
        "merge-base",
        "--is-ancestor",
        "HEAD",
        f"refs/remotes/origin/{branch}",
        check=False,
    )
    return proc.returncode == 0


def _run_branch_from_owner(owner: dict[str, Any]) -> str | None:
    """Extract the run branch name from the owner's durable base summary."""

    summary = str(owner.get("workspace_base_summary") or "")
    match = re.search(r"run-branch='([^']+)'", summary)
    if match:
        return match.group(1)
    return None


def park_workspace(
    run_id: str,
    workspace: str | Path,
    owner: dict[str, Any] | None = None,
    *,
    record_event: Callable[[str, dict[str, Any]], None] | None = None,
    record_report: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Park ``workspace`` after ``run_id`` reaches a terminal state.

    Returns a summary dict with keys ``parked`` (bool), ``workspace``,
    ``from_branch``, ``default_branch``, ``backup_ref`` (if any), ``stash``
    (message if any), and ``error`` (if parking failed without mutating the
    workspace).

    ``record_event`` is called with ``(event_type, payload)`` so the caller can
    append a ``workspace.parked`` event through its real queue/report mechanism.
    ``record_report`` is called with a small report payload so the backup ref and
    stash are recoverable from the run's durable report.
    """

    repo = Path(workspace).expanduser().resolve()
    result: dict[str, Any] = {
        "parked": False,
        "workspace": str(repo),
        "from_branch": None,
        "default_branch": None,
        "backup_ref": None,
        "stash": None,
        "error": None,
    }

    if not repo.is_dir() or not _is_git_repo(repo) or not _is_inside_worktree(repo):
        result["error"] = f"{repo} is not a usable git workspace; nothing to park"
        _maybe_emit(record_event, run_id, result)
        return result

    from_branch = _current_branch(repo)
    result["from_branch"] = from_branch

    try:
        default_branch = _detect_default_branch(repo)
    except WorkspaceParkError as exc:
        result["error"] = str(exc)
        _maybe_emit(record_event, run_id, result)
        return result
    result["default_branch"] = default_branch

    run_branch = _run_branch_from_owner(owner or {})
    if not run_branch:
        run_branch = from_branch

    try:
        head_contained = _head_contained_in_origin_branch(repo, run_branch) if run_branch else False
    except WorkspaceParkError:
        head_contained = False

    dirty = not _tree_is_clean(repo)
    needs_backup = dirty or not head_contained

    backup_ref = None
    stash_message = None

    if needs_backup:
        backup_ref = f"{_BACKUP_REF_NS}/{run_id}"
        try:
            _git(repo, "update-ref", backup_ref, "HEAD")
        except WorkspaceParkError as exc:
            result["error"] = f"could not create backup ref {backup_ref}: {exc}"
            _maybe_emit(record_event, run_id, result)
            return result
        result["backup_ref"] = backup_ref

        stash_message = f"lh-harness park {run_id}"
        try:
            _git(
                repo,
                "stash",
                "push",
                "-u",
                "-m",
                stash_message,
            )
        except WorkspaceParkError as exc:
            result["error"] = f"created backup ref {backup_ref} but stash failed: {exc}"
            _maybe_emit(record_event, run_id, result)
            return result
        result["stash"] = stash_message

    try:
        _git(repo, "checkout", "--detach", f"origin/{default_branch}")
    except WorkspaceParkError as exc:
        result["error"] = f"could not detach to origin/{default_branch}: {exc}"
        _maybe_emit(record_event, run_id, result)
        return result

    result["parked"] = True

    if record_report:
        record_report(
            {
                "schema_version": 2,
                "status": "parked",
                "run_id": run_id,
                "parked_at": time.time(),
                "workspace": str(repo),
                "from_branch": from_branch,
                "default_branch": default_branch,
                "backup_ref": backup_ref,
                "stash": stash_message,
            }
        )

    _maybe_emit(record_event, run_id, result)
    return result


def _maybe_emit(
    record_event: Callable[[str, dict[str, Any]], None] | None,
    run_id: str,
    result: dict[str, Any],
) -> None:
    if record_event is None:
        return
    try:
        record_event(
            "workspace.parked",
            {
                "run_id": run_id,
                "workspace": result.get("workspace"),
                "from_branch": result.get("from_branch"),
                "default_branch": result.get("default_branch"),
                "backup_ref": result.get("backup_ref"),
                "stash": result.get("stash"),
                "parked": result.get("parked"),
                "error": result.get("error"),
                "ts": time.time(),
            },
        )
    except Exception:
        logger.exception("workspace.parked event emission failed for run %s", run_id)
