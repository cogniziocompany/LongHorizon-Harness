"""Auditor isolation: read-only snapshot + local ref pinning.

HARNESS-AUDITOR-ISOLATION (incident 20261016T164706Z_5c063e3e): the auditor
ran ``git checkout -B <branch> origin/<branch>`` inside the executor workspace
and rewrote the executor's HEAD away. The deny list already covered network
git operations (fetch/pull/push/...); this module adds the three pieces the
deny list alone cannot deliver:

1. ``create_isolated_snapshot``: before the auditor launches, create a
   read-only worktree (or copy) of the executor's final HEAD in a per-run
   directory. The auditor operates inside the snapshot; the executor
   workspace is untouched and the executor's branch keeps its history.

2. ``pin_executor_head``: before the auditor launches, write a local ref
   ``refs/lh-harness/<run_id>/round_<n>`` pointing at the executor HEAD so
   no later verifier action can lose it.

3. ``isolated_snapshot_path``: a stable helper used by the role-binding code
   that names the snapshot directory with the run id, giving sibling runs
   their own workspace.

Test scratch output is allowed outside the source tree (the snapshot itself
lives next to the executor workspace in ``.snapshot-<run_id>``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .safe_subprocess import child_env, git_argv


# Maximum time for any single git worktree / update-ref subprocess. Kept small
# so a harness hang on a slow worktree never blocks the run loop.
_GIT_TIMEOUT = 30


@dataclass(frozen=True)
class SnapshotInfo:
    """Audit-ready record of where the auditor is operating.

    Written to ``round_dir/snapshot_info.json`` so the round log carries
    the snapshot sha and path even if the auditor's episode is later
    rejected.
    """

    run_id: str
    round_index: int
    executor_head_sha: str
    snapshot_path: str
    snapshot_head_sha: str
    local_ref: str
    mode: str  # "worktree" | "copy" | "disabled"
    read_only: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def snapshot_dir_name(run_id: str) -> str:
    """Return the per-run sibling name used for the auditor snapshot.

    Including the run_id guarantees sibling-run isolation: two runs sharing
    a workspace never share a snapshot path.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", run_id).strip("-") or "local"
    return f".snapshot-{safe}"


def local_ref_name(run_id: str, round_index: int) -> str:
    """Return the per-round local ref pinning the executor HEAD.

    The full path is `refs/lh-harness/<run_id>/round_<n>`.
    """
    safe = re.sub(r"[^A-Za-z0-9._/-]", "-", run_id).strip("-") or "local"
    return f"refs/lh-harness/{safe}/round_{round_index:03d}"


def _run_git(*args: str) -> str:
    """Run git with a short timeout; raise ``RuntimeError`` on failure."""

    try:
        proc = subprocess.run(
            git_argv(*args),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            env=child_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"git {' '.join(args)} failed to start: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        last = tail[-1] if tail else "no output"
        raise RuntimeError(f"git {' '.join(args)} failed: {last[:300]}")
    return proc.stdout.strip()


def _run_git_soft(*args: str) -> str | None:
    """Run git, return stdout on success, ``None`` on any failure."""

    try:
        proc = subprocess.run(
            git_argv(*args),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            env=child_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def executor_head_sha(workspace_path: str | Path) -> str | None:
    """Return the current HEAD sha of the executor workspace, ``None`` if not a git repo."""

    return _run_git_soft("-C", str(workspace_path), "rev-parse", "HEAD")


def _chmod_readonly(path: Path) -> None:
    """Mark a snapshot path read-only at the filesystem level.

    Defense in depth: Claude's deny list already excludes Write/Edit, and the
    local git rewrite list plus the snapshot worktree (a separate worktree
    with a checked-out detached HEAD) already block every mutation channel we
    know of. chmod -R a-w is the explicit before-damage guard that does not
    rely on Claude honoring its own deny list.
    """
    if not path.exists():
        return
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            target = Path(root) / name
            try:
                mode = target.stat(follow_symlinks=False).st_mode
                target.chmod(mode & 0o555)
            except OSError:
                # Best-effort: a directory we cannot chmod is a permission
                # problem for the auditor adapter itself, not a stop-the-line
                # guard failure. The deny list still applies.
                pass
        for name in files:
            target = Path(root) / name
            try:
                mode = target.stat(follow_symlinks=False).st_mode
                target.chmod(mode & 0o444)
            except OSError:
                pass


def _chmod_writable(path: Path) -> None:
    """Restore write permissions so cleanup can remove the snapshot dir."""
    if not path.exists():
        return
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            target = Path(root) / name
            try:
                mode = target.stat(follow_symlinks=False).st_mode
                target.chmod(mode | 0o200 | 0o100)
            except OSError:
                pass
        for name in files:
            target = Path(root) / name
            try:
                mode = target.stat(follow_symlinks=False).st_mode
                target.chmod(mode | 0o200)
            except OSError:
                pass


def pin_executor_head(workspace_path: str | Path, run_id: str, round_index: int) -> str | None:
    """Pin ``refs/lh-harness/<run_id>/round_<n>`` to the executor's HEAD.

    Returns the ref name when the ref was written, ``None`` when the workspace
    is not a git repo or git could not be invoked. The function never raises;
    a failure to pin a ref is a non-blocking degradation (the deny list, the
    snapshot, and the read-only chmod together already protect the workspace).
    """
    workspace = Path(workspace_path).expanduser()
    sha = executor_head_sha(workspace)
    if sha is None:
        return None
    ref = local_ref_name(run_id, round_index)
    try:
        _run_git("-C", str(workspace), "update-ref", ref, sha)
    except RuntimeError:
        return None
    return ref


def create_isolated_snapshot(
    workspace_path: str | Path,
    run_id: str,
    round_index: int,
    *,
    base_root: str | Path | None = None,
) -> SnapshotInfo | None:
    """Build the read-only auditor snapshot for ``round_index``.

    Strategy:
    1. Resolve the executor HEAD. If the workspace is not a git repo we
       degrade gracefully and return ``None``; the round log carries the
       reason instead.
    2. Create a git worktree detached at HEAD inside a per-run sibling
       directory. The worktree admin lives under the executor ``.git`` but
       the snapshot files live outside the executor workspace, so an
       auditor cannot ``git checkout`` the executor's branch.
    3. chmod -R a-w the snapshot path so a process that ignores the deny
       list still cannot write to it. We restore write perms on cleanup.
    4. Pin ``refs/lh-harness/<run_id>/round_<n>`` to the executor HEAD so
       the executor's committed state is recoverable even if the
       workspace is later mutated.

    When a git worktree cannot be created (non-git workspace, locked
    worktree, no space, ...) we fall back to a plain copy. The copy is
    also chmod-ed read-only. The fallback exists so a non-git task
    workspace still sees the protection; the executor live workspace is
    untouched either way.
    """
    workspace = Path(workspace_path).expanduser()
    if not workspace.is_absolute():
        root = Path(base_root) if base_root else Path.cwd()
        workspace = (root / workspace).resolve()
    sha = executor_head_sha(workspace)
    if sha is None:
        return None

    sibling = workspace.parent / snapshot_dir_name(run_id)
    # Clean up any leftover from a prior round (the per-run name keeps it
    # scoped to this run only).
    if sibling.exists():
        _chmod_writable(sibling)
        try:
            shutil.rmtree(sibling, ignore_errors=False)
        except OSError:
            pass

    mode = "worktree"
    try:
        _run_git(
            "-C",
            str(workspace),
            "worktree",
            "add",
            "--detach",
            str(sibling),
            sha,
        )
    except RuntimeError:
        # Fall back to a copy + initialization that ignores the executor's
        # .git. The auditor is still constrained to the snapshot path and
        # the live workspace stays untouched.
        mode = "copy"
        try:
            shutil.copytree(workspace, sibling, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        except OSError:
            return None

    _chmod_readonly(sibling)
    snapshot_head = _run_git_soft("-C", str(sibling), "rev-parse", "HEAD") or sha
    ref = pin_executor_head(workspace, run_id, round_index)
    return SnapshotInfo(
        run_id=run_id,
        round_index=round_index,
        executor_head_sha=sha,
        snapshot_path=str(sibling),
        snapshot_head_sha=snapshot_head,
        local_ref=ref or "",
        mode=mode,
        read_only=True,
    )


def remove_isolated_snapshot(snapshot_path: str | Path) -> None:
    """Remove a previously-created snapshot, restoring write perms first."""
    path = Path(snapshot_path).expanduser()
    if not path.exists():
        return
    _chmod_writable(path)
    # Try the git worktree remove first so the executor .git admin is
    # cleaned up; fall back to rmtree for the copy fallback.
    if path.parent.exists():
        _run_git_soft("-C", str(path.parent), "worktree", "remove", "--force", str(path))
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    if path.parent.exists():
        _run_git_soft("-C", str(path.parent), "worktree", "prune")


def write_snapshot_info(round_dir: str | Path, info: SnapshotInfo | None) -> Path | None:
    """Persist ``snapshot_info.json`` next to the round artifacts.

    Returns the file path on success, ``None`` when nothing was written.
    """
    if info is None:
        return None
    target = Path(round_dir) / "snapshot_info.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(info.to_dict(), indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return target


__all__ = [
    "SnapshotInfo",
    "create_isolated_snapshot",
    "executor_head_sha",
    "local_ref_name",
    "pin_executor_head",
    "remove_isolated_snapshot",
    "snapshot_dir_name",
    "write_snapshot_info",
]
