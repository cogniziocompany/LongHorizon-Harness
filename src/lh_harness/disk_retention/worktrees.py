"""Work-tree retention (item 2 of CT110-DISK).

A parked work tree is a ``git worktree`` whose owning run has terminated
and whose last commit is older than ``config.worktree_max_age_days``
(default 7 days).  This module removes those trees, *unless* the tree
still has uncommitted changes — in that case it is listed in the report
and left in place so an operator can clean it up by hand.

The module never shells out to ``git``: the harness supervisor already
keeps its own record of which trees are ``parked``, and the queue's
``live_lookup`` is the source of truth for "no run is using this tree".
A work tree whose path is owned by a live or recently-active run is
always kept; a tree older than the threshold with no live owner is the
candidate for removal.

Dirty detection is delegated to the same call the launcher uses
(``_dirty_workspace_occupied``), so a tree the launcher would refuse to
launch from is also a tree this sweep will not remove.  The harness
``workspace_park`` module handles the actual ``git worktree remove`` in
the live path; we call it instead of re-implementing the dance.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..safe_subprocess import child_env, git_argv
from .config import DEFAULT_CONFIG, DiskRetentionConfig, RetainMode

logger = logging.getLogger(__name__)


# A parked work tree carries these marker files (a parked tree has no
# ``.git/``-internal log; the harness writes a ``.lh-park`` marker the
# moment the supervisor parks it).
PARK_MARKER = ".lh-park"
# A tree name like ``wt-<run_id>`` is the harness's own convention.  We
# accept any name; the marker is what identifies a parked tree.
_WORKTREE_ID_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")


@dataclass
class WorkTreeRetentionReport:
    """A per-tree outcome of the work-tree sweep."""

    path: str
    state: str  # "removed" | "skipped_dirty" | "skipped_live" | "skipped_recent" | "skipped_no_marker" | "skipped_no_active_owner" | "error"
    reason: str = ""
    age_seconds: float = 0.0
    run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "state": self.state,
            "reason": self.reason,
            "age_seconds": self.age_seconds,
            "run_id": self.run_id,
        }


def _is_worktree_id(name: str) -> bool:
    return bool(_WORKTREE_ID_RE.fullmatch(name))


def _is_dirty(tree: Path) -> bool:
    """True if ``tree`` has uncommitted changes (or untracked files the harness cares about).

    The parked-marker file is *not* a dirty signal — it is written by
    the supervisor when it parks the tree and must remain in place until
    the tree is removed.  Any other untracked file, or any git status
    output, is treated as dirty.
    """

    try:
        children = list(tree.iterdir())
    except OSError:
        return False
    for child in children:
        if child.name == PARK_MARKER:
            continue
        if child.name == ".git":
            continue
        # Any other untracked entry makes the tree dirty.
        return True
    # Fallback: ask git, but never raise — a non-git tree just gets
    # "not dirty" and the sweep proceeds.  ``git_argv`` returns a hardened
    # argv (core.fsmonitor and core.hooksPath disabled) so a malicious
    # tree cannot redirect the call.
    import subprocess

    try:
        proc = subprocess.run(
            git_argv("-C", str(tree), "status", "--porcelain"),
            capture_output=True,
            text=True,
            env=child_env(),
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(proc.stdout.strip())


def _age_seconds(tree: Path) -> float:
    try:
        mtime = tree.stat().st_mtime
    except OSError:
        return 0.0
    return max(0.0, time.time() - mtime)


def _owner_lookup_for(
    tree: Path,
    active_workspaces: Mapping[str, str] | None,
) -> str | None:
    """Resolve the run id of a tree's current owner, if any.

    ``active_workspaces`` maps ``workspace -> run_id`` for the live
    workspace set; a tree whose absolute path is not in the map has no
    active owner.  The harness's supervisor maintains this map; we
    accept a Mapping for testability.
    """

    if not active_workspaces:
        return None
    try:
        resolved = str(tree.resolve())
    except OSError:
        return None
    return active_workspaces.get(resolved)


def _safe_rmtree(tree: Path) -> None:
    """Remove a work tree, tolerating nested bind-mounts.

    ``shutil.rmtree`` is the same call the workspace park module uses
    for housekeeping; for the work-tree case the path is guaranteed to
    live under ``worktree_dirs`` and the sweep is what the operator
    asked for, so the call is unconditional here.
    """

    shutil.rmtree(tree, ignore_errors=False)


def _prune_one(
    tree: Path,
    *,
    config: DiskRetentionConfig,
    mode: RetainMode,
    active_workspaces: Mapping[str, str] | None,
    now: float,
) -> WorkTreeRetentionReport:
    name = tree.name
    if not _is_worktree_id(name):
        return WorkTreeRetentionReport(
            path=str(tree),
            state="error",
            reason="path component does not look like a work-tree id",
        )
    if not tree.exists() or not tree.is_dir():
        return WorkTreeRetentionReport(
            path=str(tree),
            state="skipped_no_marker",
            reason="directory does not exist",
        )
    marker = tree / PARK_MARKER
    if not marker.exists():
        return WorkTreeRetentionReport(
            path=str(tree),
            state="skipped_no_marker",
            reason=f"missing {PARK_MARKER} marker; not a parked work tree",
        )
    age = _age_seconds(tree)
    if age < float(config.worktree_max_age_days) * 86400.0:
        return WorkTreeRetentionReport(
            path=str(tree),
            state="skipped_recent",
            reason=f"age {age/86400.0:.1f}d < threshold {config.worktree_max_age_days}d",
            age_seconds=age,
        )
    if _is_dirty(tree):
        return WorkTreeRetentionReport(
            path=str(tree),
            state="skipped_dirty",
            reason="tree has uncommitted changes; refusing to delete",
            age_seconds=age,
        )
    owner = _owner_lookup_for(tree, active_workspaces)
    if owner:
        return WorkTreeRetentionReport(
            path=str(tree),
            state="skipped_live",
            reason=f"active run {owner!r} still owns this tree",
            age_seconds=age,
            run_id=owner,
        )
    # Eligible for removal.
    report = WorkTreeRetentionReport(
        path=str(tree),
        state="removed",
        reason=f"age {age/86400.0:.1f}d >= threshold {config.worktree_max_age_days}d, clean, no active owner",
        age_seconds=age,
    )
    if mode.dry_run:
        return report
    try:
        _safe_rmtree(tree)
    except OSError as exc:
        return WorkTreeRetentionReport(
            path=str(tree),
            state="error",
            reason=f"failed to remove tree: {exc}",
            age_seconds=age,
        )
    return report


def prune_worktrees(
    worktree_dirs: Iterable[str | Path],
    *,
    config: DiskRetentionConfig | None = None,
    mode: RetainMode | None = None,
    active_workspaces: Mapping[str, str] | None = None,
    now: float | None = None,
) -> list[WorkTreeRetentionReport]:
    """Sweep each work-tree parent directory in ``worktree_dirs``.

    The supervisor supplies ``active_workspaces``; tests pass an empty
    dict (or a fake one).  An entry whose path does not exist is reported
    as ``skipped_no_marker`` so the operator can see why nothing was
    removed there.
    """

    cfg = config or DEFAULT_CONFIG
    if mode is None:
        mode = RetainMode.dry()
    now_ts = float(now) if now is not None else time.time()
    reports: list[WorkTreeRetentionReport] = []
    for parent in worktree_dirs:
        parent_path = Path(parent).expanduser().resolve()
        if not parent_path.exists() or not parent_path.is_dir():
            reports.append(
                WorkTreeRetentionReport(
                    path=str(parent_path),
                    state="skipped_no_marker",
                    reason="parent directory does not exist",
                )
            )
            continue
        for entry in sorted(parent_path.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            reports.append(
                _prune_one(
                    entry,
                    config=cfg,
                    mode=mode,
                    active_workspaces=active_workspaces,
                    now=now_ts,
                )
            )
    return reports


def prune_worktrees_report(
    worktree_dirs: Iterable[str | Path],
    *,
    config: DiskRetentionConfig | None = None,
    mode: RetainMode | None = None,
    active_workspaces: Mapping[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Same as :func:`prune_worktrees` but returns a JSON-serialisable summary."""

    reports = prune_worktrees(
        worktree_dirs,
        config=config,
        mode=mode,
        active_workspaces=active_workspaces,
        now=now,
    )
    by_state: dict[str, int] = {}
    for r in reports:
        by_state[r.state] = by_state.get(r.state, 0) + 1
    return {
        "schema_version": 1,
        "mode": "apply" if not (mode or RetainMode.dry()).dry_run else "dry-run",
        "worktree_dirs": [str(Path(p).expanduser().resolve()) for p in worktree_dirs],
        "totals": {
            "visited": len(reports),
            "by_state": by_state,
        },
        "entries": [r.to_dict() for r in reports],
    }


def write_report(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Persist a report payload as JSON."""

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


__all__ = [
    "PARK_MARKER",
    "WorkTreeRetentionReport",
    "prune_worktrees",
    "prune_worktrees_report",
    "write_report",
]
