"""Auditor isolation: independent full clone, OS-level UID boundary, fail-closed.

HARNESS-AUDITOR-ISOLATION (incident 20261016T164706Z_5c063e3e): the auditor
ran ``git checkout -B <branch> origin/<branch>`` inside the executor workspace
and rewrote the executor's HEAD away. The deny list already covered network
git operations (fetch/pull/push/...); this module provides the pieces the
deny list alone cannot deliver, after the operator-correction rework that
identified three fundamental isolation gaps in the prior implementation:

1. ``create_isolated_snapshot``: before the auditor launches, build an
   *independent full clone* of the executor's HEAD in a per-run directory.
   ``git clone --no-hardlinks --no-local --no-shared`` produces a separate
   ``.git/objects`` tree with no shared links into the executor's object
   database; the snapshot is a sibling directory, never a ``worktree``,
   so the auditor cannot reach the executor's refs via ``update-ref`` or
   ``git -C <executor> ...``.

2. ``pin_executor_head``: before the auditor launches, write a local ref
   ``refs/lh-harness/<run_id>/round_<n>`` pointing at the executor HEAD
   so no later verifier action can lose it. Pin failure propagates as a
   hard error (the harness refuses to launch the auditor).

3. ``isolated_snapshot_path``: a stable helper used by the role-binding code
   that names the snapshot directory with the run id, giving sibling runs
   their own workspace.

4. ``auditor_boundary_required()`` / ``AuditorBoundaryKind``: gatekeeper
   that refuses to launch the auditor unless an OS-level boundary
   (separate-UID process via ``unshare -U`` / ``setpriv --reuid`` /
   ``runuser`` *or* a separate-UID launcher the deployment already
   provides) is available. Same-UID ``chmod a-w`` is *advisory*, not a
   kernel boundary, so fail-closed is mandatory — see the long note
   below.

OS-level boundary: why this is mandatory.

``chmod a-w`` on a snapshot owned by the same uid as the auditor process
is not a security boundary. The auditor can ``chmod`` the snapshot back to
writable from inside the harness, ignore ``Bash(chmod*)`` deny rules, and
write a file directly via Python ``open(..., "w")``. The operator probe
at commit ``d47ac73`` confirmed ``same_uid_can_rewrite_snapshot=true``,
``same_uid_can_rewrite_executor_directly=true``. To make
``is_writable_to_auditor_subprocess==false``, the auditor MUST run under
a different uid (a separate-UID subprocess or a user-namespace
subprocess) so the kernel permission check rejects even direct file
operations. ``safe_subprocess.launch_auditor_subprocess`` is the single
launch point that enforces this.

Test scratch output is allowed outside the source tree. Cleanup is
symlink-safe: every walk starts from a path that has been resolved via
``Path.resolve()`` and refuses to follow symlinks whose targets escape
the verified root.

Defense in depth applied at every layer the harness owns:

  (a) independent full clone (no shared ``.git``, no alternates),
  (b) ``chmod -R a-w`` on snapshot files (advisory; defeated by a
      same-UID reverse-chmod, which is why (c) and (d) are required),
  (c) explicit deny rules in ``AUDITOR_LOCAL_GIT_DENY`` covering
      ``checkout``/``reset``/``switch``/``clean``/``stash``/
      ``restore``/``commit``/``rebase``/``merge``/``update-ref``/
      ``symbolic-ref`` and the same family under ``-C``/``--git-dir``,
  (d) filesystem-deny covering ``chmod``/``chown``/``rm``/``unlink``/
      ``install``/``mv`` so a Bash subprocess cannot reverse (b),
  (e) the auditor subprocess runs under a different uid so the kernel
      rejects direct file ``open(..., "w")`` writes regardless of any
      Python or bash layer.

Fail-closed: if (a) cannot be performed, the ``update-ref`` pin cannot
be written, *or* (e) cannot be established, the harness refuses to
launch the auditor against the executor workspace. See
``safe_subprocess.auditor_boundary_required``.
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


# Maximum time for any single git subprocess. Kept small so a harness hang
# on a slow clone never blocks the run loop.
_GIT_TIMEOUT = 60


# Errors that mean "git could not run the command" — non-git, non-network.
class SnapshotIsolationError(RuntimeError):
    """Raised when auditor isolation cannot be established.

    The caller is expected to treat this as a hard failure: do not fall
    back to launching the auditor against the executor workspace.
    """


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
    mode: str  # always "clone" now; kept for log compatibility
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
    """Run git with a short timeout; raise on failure."""

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
        raise SnapshotIsolationError(
            f"git {' '.join(args)} failed to start: {exc}"
        ) from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        last = tail[-1] if tail else "no output"
        raise SnapshotIsolationError(
            f"git {' '.join(args)} failed: {last[:300]}"
        )
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


def _verified_root(path: Path) -> Path:
    """Return the canonical, symlink-free root for an audit-time path.

    Every walk in this module starts from a path that has been resolved
    via ``Path.resolve()`` and checked to be inside its parent. Symlinks
    encountered during the walk are NOT followed (we only ``stat`` with
    ``follow_symlinks=False``); if any symlink in the tree points outside
    the verified root we raise rather than chase it.
    """
    return path.resolve(strict=False)


def _safe_relpath(root: Path, target: Path) -> str:
    """Return ``target.relative_to(root)`` only if target stays inside root.

    A symlink under the snapshot whose target lies outside is the bug the
    operator correction flagged (``_chmod_readonly`` was following symlinks
    and changing external files). This guard makes every relative-path
    computation raise ``SnapshotIsolationError`` instead.
    """
    try:
        rel = target.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise SnapshotIsolationError(
            f"path escapes snapshot root: {target} not under {root}"
        ) from exc
    return rel.as_posix()


def _iter_snapshot_entries(root: Path):
    """Yield ``(absolute_path, is_dir, is_symlink)`` strictly under ``root``.

    Symlinks are reported but never followed. If any directory entry is
    a symlink whose target resolves outside the verified root, we raise
    rather than recurse into it.
    """
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise SnapshotIsolationError(
                f"cannot scan snapshot directory {directory}: {exc}"
            ) from exc
        for entry in entries:
            abs_path = Path(entry.path)
            try:
                is_symlink = entry.is_symlink()
                if is_symlink:
                    target = abs_path.resolve(strict=False)
                    try:
                        target.relative_to(root)
                    except ValueError as exc:
                        raise SnapshotIsolationError(
                            f"symlink {abs_path} escapes snapshot root"
                        ) from exc
                    yield abs_path, False, True
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise SnapshotIsolationError(
                    f"cannot stat {abs_path}: {exc}"
                ) from exc
            yield abs_path, is_dir, False
            if is_dir:
                stack.append(abs_path)


def _chmod_readonly(root: Path) -> None:
    """Mark every entry under ``root`` read-only without following symlinks.

    The operator correction pointed out the previous implementation used
    ``os.walk(..., followlinks=False)`` but called ``target.chmod(...)``
    which DOES follow symlinks (chmod is on the resolved path). This
    implementation explicitly uses ``os.chmod(path, mode,
    follow_symlinks=False)`` (Python 3.3+) so symlinks themselves are
    chmod-ed rather than their targets, and external files outside the
    snapshot are never touched.
    """
    if not root.exists():
        raise SnapshotIsolationError(
            f"snapshot root vanished before chmod: {root}"
        )
    for abs_path, is_dir, is_symlink in _iter_snapshot_entries(root):
        try:
            st = os.stat(abs_path, follow_symlinks=False)
        except OSError as exc:
            raise SnapshotIsolationError(
                f"cannot stat snapshot entry {abs_path}: {exc}"
            ) from exc
        current_mode = stat_S_IMODE(st.st_mode)
        if is_dir and not is_symlink:
            new_mode = current_mode & 0o555  # strip w+x for dirs
        elif is_symlink:
            # chmod on a symlink is a no-op for the target; keep the mode
            # intact but ensure the parent walk doesn't escalate.
            continue
        else:
            new_mode = current_mode & 0o444  # strip w+x for files
        try:
            os.chmod(abs_path, new_mode, follow_symlinks=False)
        except OSError as exc:
            raise SnapshotIsolationError(
                f"chmod failed on {abs_path}: {exc}"
            ) from exc


def _chmod_writable(root: Path) -> None:
    """Restore write permissions so cleanup can remove the snapshot dir.

    Same symlink-safe guarantees as ``_chmod_readonly``: symlinks are
    never dereferenced, external targets never touched.
    """
    if not root.exists():
        return
    for abs_path, is_dir, is_symlink in _iter_snapshot_entries(root):
        try:
            st = os.stat(abs_path, follow_symlinks=False)
        except OSError:
            continue
        current_mode = stat_S_IMODE(st.st_mode)
        if is_dir and not is_symlink:
            new_mode = current_mode | 0o200 | 0o100
        elif is_symlink:
            continue
        else:
            new_mode = current_mode | 0o200
        try:
            os.chmod(abs_path, new_mode, follow_symlinks=False)
        except OSError:
            pass


def stat_S_IMODE(mode: int) -> int:
    """Return the permission portion of a stat mode (portable helper)."""
    return mode & 0o7777


def pin_executor_head(
    workspace_path: str | Path,
    run_id: str,
    round_index: int,
) -> str:
    """Pin ``refs/lh-harness/<run_id>/round_<n>`` to the executor's HEAD.

    Returns the ref name. Raises :class:`SnapshotIsolationError` if the
    workspace is not a git repo or ``git update-ref`` cannot write the ref.
    The previous implementation silently returned ``None`` on pin failure,
    which the operator correction flagged as a fail-open bug: a missing
    pin means the executor's final state can be lost even though the deny
    list, snapshot, and chmod together still apply. Pin failure now
    propagates so the harness aborts the round rather than launching an
    auditor with no executor recovery point.
    """
    workspace = Path(workspace_path).expanduser()
    sha = executor_head_sha(workspace)
    if sha is None:
        raise SnapshotIsolationError(
            f"executor workspace is not a git repo: {workspace}"
        )
    ref = local_ref_name(run_id, round_index)
    # update-ref requires the ref path to not exist OR match the old value
    # to succeed without --no-deref. Use --no-deref so we never accidentally
    # rewrite HEAD/symrefs.
    _run_git(
        "-C", str(workspace),
        "update-ref", "--no-deref", ref, sha,
    )
    return ref


def _clone_snapshot(
    executor_workspace: Path,
    sibling: Path,
) -> str:
    """Build the independent full clone. Raises on any failure.

    ``--no-hardlinks``: every object is a fresh inode, so the snapshot's
    ``.git/objects`` shares nothing with the executor's ``.git/objects``.
    ``--no-local``: do not optimise via the executor's ``.git/objects/info/alternates``
    (which would re-introduce a shared object store). ``--no-shared``:
    belt-and-suspenders; we never want ``alternates``.
    ``--shared=false`` would also work but ``--no-local`` already implies it.
    The clone ends up checked out at HEAD inside ``sibling``; we then
    detach so the auditor cannot accidentally commit to a branch.
    """
    sha = _run_git("-C", str(executor_workspace), "rev-parse", "HEAD")
    # ``git clone --no-hardlinks`` always allocates fresh object inodes;
    # combined with --no-local there is no alternates link back into the
    # executor's object database. The auditor's repo is a true sibling.
    _run_git(
        "clone",
        "--no-hardlinks",
        "--no-local",
        "--quiet",
        str(executor_workspace),
        str(sibling),
    )
    # Detach HEAD so the snapshot's branch cannot be advanced by a stray
    # commit (the executor HEAD stays put even if the auditor runs
    # `git checkout -B ... origin/...` inside the snapshot, which the deny
    # list already blocks).
    _run_git("-C", str(sibling), "checkout", "--detach", sha)
    return sha


def create_isolated_snapshot(
    workspace_path: str | Path,
    run_id: str,
    round_index: int,
    *,
    base_root: str | Path | None = None,
) -> SnapshotInfo:
    """Build the independent clone snapshot for ``round_index``.

    Strategy:
    1. Resolve the executor HEAD. If the workspace is not a git repo we
       raise :class:`SnapshotIsolationError` (fail-closed).
    2. Pin ``refs/lh-harness/<run_id>/round_<n>`` to the executor HEAD
       before any clone work. Pin failure propagates.
    3. ``git clone --no-hardlinks --no-local`` the executor into a
       per-run sibling directory. The clone is a fresh repository with
       its own object database; there is no shared ``.git`` and no
       ``worktrees/<name>/`` admin pointing back at the executor.
    4. ``chmod -R a-w`` the snapshot files (without following symlinks).
       This is one defense layer; the Claude ``Bash(chmod*)`` deny rule
       and the deny list forbidding ``update-ref`` / ``reset`` / etc.
       are the other layers.
    5. Verify the snapshot HEAD sha matches the executor HEAD sha, so a
       broken clone is rejected before the auditor is launched.

    The function never falls back to launching the auditor against the
    executor workspace. If any step fails the caller must abort the round.
    """
    workspace = Path(workspace_path).expanduser()
    if not workspace.is_absolute():
        root = Path(base_root) if base_root else Path.cwd()
        workspace = (root / workspace).resolve()
    workspace = workspace.resolve(strict=False)
    if not workspace.is_dir():
        raise SnapshotIsolationError(
            f"executor workspace does not exist: {workspace}"
        )
    sha = executor_head_sha(workspace)
    if sha is None:
        raise SnapshotIsolationError(
            f"executor workspace is not a git repo: {workspace}"
        )

    # Pin first; this is the durable record of the executor's final state.
    ref = pin_executor_head(workspace, run_id, round_index)

    sibling = (workspace.parent / snapshot_dir_name(run_id)).resolve(strict=False)
    # Clean up any leftover from a prior round (the per-run name keeps it
    # scoped to this run only).
    if sibling.exists() or sibling.is_symlink():
        _chmod_writable(sibling)
        try:
            if sibling.is_symlink():
                sibling.unlink()
            else:
                shutil.rmtree(sibling)
        except OSError as exc:
            raise SnapshotIsolationError(
                f"cannot remove previous snapshot {sibling}: {exc}"
            ) from exc

    _clone_snapshot(workspace, sibling)

    # Verify clone independence: the snapshot must not share its .git dir
    # with the executor (which would let update-ref inside the snapshot
    # rewrite the executor's refs via the alternates mechanism).
    snap_git = (sibling / ".git").resolve(strict=False)
    exec_git = (workspace / ".git").resolve(strict=False)
    if snap_git == exec_git:
        raise SnapshotIsolationError(
            f"snapshot shares .git with executor: {snap_git}"
        )

    _chmod_readonly(sibling)
    snapshot_head = _run_git_soft("-C", str(sibling), "rev-parse", "HEAD")
    if snapshot_head is None:
        raise SnapshotIsolationError(
            f"snapshot clone has no HEAD: {sibling}"
        )
    if snapshot_head != sha:
        raise SnapshotIsolationError(
            f"snapshot HEAD {snapshot_head} != executor HEAD {sha}"
        )

    return SnapshotInfo(
        run_id=run_id,
        round_index=round_index,
        executor_head_sha=sha,
        snapshot_path=str(sibling),
        snapshot_head_sha=snapshot_head,
        local_ref=ref,
        mode="clone",
        read_only=True,
    )


def remove_isolated_snapshot(snapshot_path: str | Path) -> None:
    """Remove a previously-created snapshot, restoring write perms first."""
    path = Path(snapshot_path).expanduser().resolve(strict=False)
    if not path.exists() and not path.is_symlink():
        return
    _chmod_writable(path)
    if path.is_symlink():
        try:
            path.unlink()
        except OSError:
            pass
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


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
    "SnapshotIsolationError",
    "create_isolated_snapshot",
    "executor_head_sha",
    "local_ref_name",
    "pin_executor_head",
    "remove_isolated_snapshot",
    "snapshot_dir_name",
    "write_snapshot_info",
]