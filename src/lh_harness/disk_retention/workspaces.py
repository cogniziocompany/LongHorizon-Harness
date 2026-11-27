"""Terminal-run clone-workspace retention (easybutt0n-17).

A run's *working clone* is the directory under the node work root
(e.g. ``/home/harness/work/<workspace>/``) that the supervisor resolves
for the run (``RunSupervisor._resolve_workspace``) and that the worker
edits.  It is not run evidence: the run's own records live under
``.lh-harness/runs/`` and are never this module's business.

Once every run and queue entry that names a workspace has reached a
TERMINAL state, and the workspace has stayed that way for longer than
``LH_HARNESS_DISK_WORKSPACE_MAX_AGE_HOURS`` (default 72), the clone is a
candidate for removal.  The decision is a pure predicate
(:func:`should_retain_workspace`); everything that touches the
filesystem — enumerating the work root, measuring sizes, computing an
age, deleting — lives in the sweep functions below it, so the rule can be
unit-tested without a single stat call.

Deny by default.  A workspace is kept when

* any path component is ``.lh-harness`` (run evidence, never touched);
* no run or queue entry names it (an orphan — we do not know what it is);
* any referencing run/queue entry is in a non-terminal state
  (``running``, ``waiting_approval``, ``queued``, ``pending``, …) or in a
  state we do not recognise.  Runs and queue entries are judged by their
  OWN vocabulary: a queue status is terminal only through the explicit
  ``QUEUE_TERMINAL_STATUSES`` allowlist (``done``/``failed``), so a queue
  entry in ``blocked`` — a NON-terminal queue state even though ``blocked``
  is a terminal *run* state — keeps the workspace;
* its age is at or below the threshold (strictly older selects);
* the explicit ``LH_HARNESS_DISK_WORKSPACE_RETENTION_ENABLED`` flag is not
  set — the sweep then only *lists* candidates.

The sweep is dry-run by default like every other module here, and the
flag above is a second, independent gate in front of ``rmtree``: with the
flag unset no deletion path is reachable at all, whatever mode the caller
passes.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..supervisor.lifecycle import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    canonical_lifecycle_status,
)
from .config import DEFAULT_CONFIG, DiskRetentionConfig, RetainMode

logger = logging.getLogger(__name__)

# The harness-owned control directory.  Anything with this path component
# is run evidence (``.lh-harness/runs``, ``.lh-harness/queue``) or the
# project config and is never a workspace candidate.
HARNESS_OWNED_COMPONENT = ".lh-harness"

SECONDS_PER_HOUR = 3600.0


# ------------------------------------------------------------ reference kinds
# A workspace is referenced by two different things, and they have two
# different status vocabularies that happen to share a word.  A *run* has a
# supervisor lifecycle status (``supervisor/lifecycle.py``): there ``blocked``
# is a TERMINAL manager/auditor outcome.  A *queue entry* has a queue status
# (``queue.py``): there ``blocked`` is a NON-terminal state — the entry is
# still parked work that resumes through ``blocked -> pending``/``launched``
# (``queue._NON_TERMINAL_STATUS``).  Feeding a queue ``blocked`` through the
# run-lifecycle lens therefore mistook a live queue entry for a finished run.
# Each reference is tagged with its source, and a queue status is terminal
# only if it is in the explicit allowlist below — nothing falls through.
SOURCE_RUN = "run"
SOURCE_QUEUE = "queue"

# The ONLY queue statuses that mean "this entry is finished".  Mirrors
# ``queue._VALID_STATUS - queue._NON_TERMINAL_STATUS``; a queue test pins the
# two to each other.  Everything else — every ``_NON_TERMINAL_STATUS`` member
# (``pending``, ``launched``, ``blocked``), an unknown status, ``None`` or a
# missing one — keeps the workspace.
QUEUE_TERMINAL_STATUSES = frozenset({"done", "failed"})


def is_terminal_reference(source: str, status: Any) -> bool:
    """Pure: is one tagged reference (``source`` + its raw ``status``) terminal?

    ``SOURCE_RUN`` is judged by the supervisor lifecycle vocabulary
    (:data:`TERMINAL_STATUSES` after canonicalisation); ``SOURCE_QUEUE`` by
    the explicit :data:`QUEUE_TERMINAL_STATUSES` allowlist.  Any other
    source, and any unknown/empty status, is NOT terminal — deny by default.
    """

    if source == SOURCE_QUEUE:
        text = str(status).strip().lower() if status is not None else ""
        return text in QUEUE_TERMINAL_STATUSES
    if source == SOURCE_RUN:
        canonical = canonical_lifecycle_status(status, default="")
        return bool(canonical) and canonical in TERMINAL_STATUSES
    return False


def _tag(source: str, status: Any) -> str:
    """Render one reference for the predicate's ``states`` input and the report.

    ``"queue:blocked"`` and ``"run:blocked"`` are deliberately different
    strings: the predicate resolves the tag, so a queue status can never be
    mistaken for the run state of the same spelling.
    """

    return f"{source}:{'' if status is None else str(status).strip()}"


def _untag(state: str) -> tuple[str, Any]:
    """Split a tagged reference back into ``(source, status)``.

    An untagged string is a *run* status — the public form the predicate has
    always accepted — so a caller passing bare lifecycle states is unchanged.
    """

    text = str(state)
    if ":" in text:
        source, _, status = text.partition(":")
        if source in (SOURCE_RUN, SOURCE_QUEUE):
            return source, status
    return SOURCE_RUN, text


# ----------------------------------------------------------------- predicate
def should_retain_workspace(
    path: str | os.PathLike[str],
    states: Iterable[str] | None,
    age_seconds: float,
    max_age_seconds: float,
) -> bool:
    """Pure selection predicate: would ``path`` be removed?

    Inputs are exactly the ones the caller has already gathered — the
    workspace path, the states of every run and queue entry that
    references it, its age in seconds, and the maximum age in seconds.
    This function performs no filesystem, clock or state-store access; it
    only classifies.

    ``states`` holds one entry per reference.  A bare string is a *run*
    lifecycle status; ``"queue:<status>"`` is a queue entry's status and is
    terminal only if it is in :data:`QUEUE_TERMINAL_STATUSES` (so a queue
    ``blocked``/``pending``/``launched`` keeps, while a run ``blocked`` is
    terminal).  Use :func:`collect_workspace_refs` to produce the tagged
    form.

    Selects only when ALL hold:

    1. no path component is ``.lh-harness``;
    2. ``states`` is non-empty (an orphan is kept);
    3. every reference is terminal for its own vocabulary
       (:func:`is_terminal_reference`); any non-terminal or unknown
       reference, from either source, or a mix keeps;
    4. ``age_seconds > max_age_seconds`` (equal keeps).

    A negative or non-finite age is treated as "unknown" and kept; a
    negative threshold disables the rule (nothing is ever old enough).
    """

    parts = Path(path).parts
    if HARNESS_OWNED_COMPONENT in parts:
        return False
    try:
        age = float(age_seconds)
        limit = float(max_age_seconds)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(age) or not math.isfinite(limit):
        return False
    if age < 0.0 or limit < 0.0:
        return False
    if states is None:
        return False
    state_list = list(states)
    if not state_list:
        return False
    for raw in state_list:
        source, status = _untag(raw)
        if not is_terminal_reference(source, status):
            return False
    return age > limit


# ------------------------------------------------------------------ collector
@dataclass
class WorkspaceRef:
    """Everything the sweep knows about one workspace, gathered by the caller.

    ``states`` holds one tagged reference per run and queue entry naming the
    workspace (``"run:<status>"`` / ``"queue:<status>"``, see :func:`_tag`),
    which is the form the predicate resolves through
    :func:`is_terminal_reference`.  ``finished_at`` is the epoch timestamp the
    age is computed from: the most recent ``finished_at`` of a referencing
    run when one is known (the run's own terminal-state timestamp), else
    ``None`` and the sweep falls back to the workspace directory's mtime.
    """

    path: str
    states: list[str] = field(default_factory=list)
    finished_at: float | None = None

    def add(self, source: str, status: Any, finished_at: float | None = None) -> None:
        """Record one reference from ``source`` (``run``/``queue``) with its raw status.

        A reference with no status at all still counts as a reference (so a
        workspace is never mistaken for an orphan) and is tagged with an
        empty status, which is not terminal in either vocabulary.
        """

        self.states.append(_tag(source, status))
        if finished_at is not None:
            try:
                value = float(finished_at)
            except (TypeError, ValueError):
                return
            if value == value and value > 0 and (self.finished_at is None or value > self.finished_at):
                self.finished_at = value

    @property
    def age_source(self) -> str:
        return "run_finished_at" if self.finished_at is not None else "workspace_mtime"


def collect_workspace_refs(
    run_items: Iterable[Mapping[str, Any]] | None,
    queue_entries: Iterable[Any] | None = None,
) -> dict[str, WorkspaceRef]:
    """Group run and queue references by workspace path.

    ``run_items`` are the dicts ``RunSupervisor.list_run_items`` returns
    (``workspace`` from the owner record, ``status`` from the durable
    status, ``finished_at`` when the run reached a terminal state).
    ``queue_entries`` are :class:`QueueEntry` objects (or anything with
    ``workspace``/``status``).  Paths are normalised (``os.path.normpath``)
    so a trailing slash or ``..`` cannot make one workspace look like two.
    A run whose queue entry is still non-terminal contributes that entry's
    status too, so a finished run whose queue row is still ``launched``
    keeps its workspace until the row is reconciled.

    Every reference is tagged with its source.  A queue status is NEVER
    judged by the run-lifecycle vocabulary: ``blocked`` is a terminal
    *run* outcome but a NON-terminal *queue* state
    (``queue._NON_TERMINAL_STATUS``), so a queue entry — whatever its
    status — is terminal only through :data:`QUEUE_TERMINAL_STATUSES`
    (``done``/``failed``).  ``pending``, ``launched``, ``blocked``, an
    unknown, ``None`` or missing queue status all keep the workspace.
    Nothing here reads the filesystem.
    """

    refs: dict[str, WorkspaceRef] = {}

    def _key(raw: Any) -> str | None:
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        return os.path.normpath(text)

    for item in run_items or ():
        if not isinstance(item, Mapping):
            continue
        key = _key(item.get("workspace"))
        if key is None:
            continue
        ref = refs.setdefault(key, WorkspaceRef(path=key))
        finished = item.get("finished_at")
        status = item.get("status")
        # Only a terminal run may lend its finished_at: an active run's
        # status file can still carry a previous epoch's timestamp after a
        # resume, which must not make a live workspace look old.
        if is_terminal_reference(SOURCE_RUN, status):
            ref.add(SOURCE_RUN, status, finished)
        else:
            ref.add(SOURCE_RUN, status, None)
    for entry in queue_entries or ():
        workspace = getattr(entry, "workspace", None)
        if workspace is None and isinstance(entry, Mapping):
            workspace = entry.get("workspace")
        key = _key(workspace)
        if key is None:
            continue
        status = getattr(entry, "status", None)
        if status is None and isinstance(entry, Mapping):
            status = entry.get("status")
        # Tagged as a queue reference: the predicate judges it by the queue
        # allowlist, never by the run-lifecycle vocabulary.
        refs.setdefault(key, WorkspaceRef(path=key)).add(SOURCE_QUEUE, status)
    return refs


# --------------------------------------------------------------------- sweep
@dataclass
class WorkspaceRetentionReport:
    """A per-workspace outcome of the sweep."""

    path: str
    state: str  # see _STATE_* below
    reason: str = ""
    age_seconds: float = 0.0
    age_source: str = ""
    size_bytes: int = 0
    size_human: str = ""
    states: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "state": self.state,
            "reason": self.reason,
            "age_seconds": self.age_seconds,
            "age_source": self.age_source,
            "size_bytes": self.size_bytes,
            "size_human": self.size_human,
            "states": list(self.states),
        }


STATE_SELECTABLE = "selectable"  # would be removed (dry-run) / removed (apply, flag on)
STATE_REMOVED = "removed"
STATE_KEPT_HARNESS_OWNED = "kept_harness_owned"
STATE_KEPT_NO_REFS = "kept_no_references"
STATE_KEPT_ACTIVE = "kept_active_reference"
STATE_KEPT_RECENT = "kept_recent"
STATE_SKIPPED = "skipped"
STATE_ERROR = "error"

KEEP_STATES: frozenset[str] = frozenset(
    {
        STATE_KEPT_HARNESS_OWNED,
        STATE_KEPT_NO_REFS,
        STATE_KEPT_ACTIVE,
        STATE_KEPT_RECENT,
        STATE_SKIPPED,
    }
)


def _dir_size_bytes(path: Path) -> int:
    """Apparent size of every regular file under ``path``, in bytes.

    Symlinks are counted by the link itself, never followed, so a workspace
    that links to a large tree outside the work root does not inflate its
    own number (and the walk cannot escape the root).  Errors on individual
    entries are skipped; a vanished tree reads as 0.
    """

    total = 0
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except OSError:
        return 0
    stack = entries
    while stack:
        entry = stack.pop()
        try:
            if entry.is_symlink():
                total += entry.stat(follow_symlinks=False).st_size
            elif entry.is_dir(follow_symlinks=False):
                with os.scandir(entry.path) as it:
                    stack.extend(it)
            elif entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def human_size(num_bytes: int) -> str:
    """Render a byte count the way ``du -h`` does (binary units, one decimal below 10)."""

    size = float(max(0, int(num_bytes)))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(size)}{unit}"
            return f"{size:.1f}{unit}" if size < 10.0 else f"{size:.0f}{unit}"
        size /= 1024.0
    return f"{size:.0f}TiB"


def _mtime_of(path: Path) -> float | None:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return None


def _workspace_age(ref: WorkspaceRef | None, ws: Path, now: float) -> tuple[float | None, str]:
    """Return ``(age_seconds, source)`` for one workspace.

    The run's own terminal timestamp is preferred (``status.json``
    ``finished_at``, surfaced by the supervisor): it is the moment the last
    referencing run stopped touching the clone, which is what "terminal
    for N hours" means.  A directory mtime only changes when an entry is
    added to or removed from the top level, so a clone that has been idle
    for a week but was re-created yesterday would read as a day old.  The
    mtime is the fallback for a workspace whose runs left no timestamp.
    An unreadable workspace has no age and is kept.
    """

    if ref is not None and ref.finished_at is not None:
        return max(0.0, now - ref.finished_at), "run_finished_at"
    mtime = _mtime_of(ws)
    if mtime is None:
        return None, "unreadable"
    return max(0.0, now - mtime), "workspace_mtime"


def _sweep_one(
    ws: Path,
    *,
    ref: WorkspaceRef | None,
    config: DiskRetentionConfig,
    mode: RetainMode,
    now: float,
) -> WorkspaceRetentionReport:
    """Decide what to do with one direct child of the work root."""

    states = list(ref.states) if ref is not None else []
    if HARNESS_OWNED_COMPONENT in ws.parts:
        return WorkspaceRetentionReport(
            path=str(ws),
            state=STATE_KEPT_HARNESS_OWNED,
            reason=f"path contains {HARNESS_OWNED_COMPONENT}/ (run evidence is never a workspace candidate)",
            states=states,
        )
    if not states:
        return WorkspaceRetentionReport(
            path=str(ws),
            state=STATE_KEPT_NO_REFS,
            reason="no run or queue entry references this workspace (orphan); kept",
            states=states,
        )
    max_age = float(config.workspace_max_age_hours) * SECONDS_PER_HOUR
    age, source = _workspace_age(ref, ws, now)
    if age is None:
        return WorkspaceRetentionReport(
            path=str(ws),
            state=STATE_SKIPPED,
            reason="workspace age could not be determined; kept",
            age_source=source,
            states=states,
        )
    if should_retain_workspace(ws, states, age, max_age):
        size = _dir_size_bytes(ws)
        return WorkspaceRetentionReport(
            path=str(ws),
            state=STATE_SELECTABLE,
            reason=(
                f"all {len(states)} referencing run(s)/queue entry(ies) terminal and "
                f"age {age / SECONDS_PER_HOUR:.1f}h > threshold {config.workspace_max_age_hours}h"
            ),
            age_seconds=age,
            age_source=source,
            size_bytes=size,
            size_human=human_size(size),
            states=states,
        )
    # Not selected: say why, so the operator can read the dry-run listing.
    # Same tagged resolution as the predicate, so a kept queue reference is
    # reported as an active reference (never as "recent").
    if any(not is_terminal_reference(*_untag(s)) for s in states):
        return WorkspaceRetentionReport(
            path=str(ws),
            state=STATE_KEPT_ACTIVE,
            reason="a referencing run or queue entry is not terminal; kept",
            age_seconds=age,
            age_source=source,
            states=states,
        )
    return WorkspaceRetentionReport(
        path=str(ws),
        state=STATE_KEPT_RECENT,
        reason=f"age {age / SECONDS_PER_HOUR:.1f}h <= threshold {config.workspace_max_age_hours}h; kept",
        age_seconds=age,
        age_source=source,
        states=states,
    )


def _delete_workspace(ws: Path) -> None:
    """Remove a workspace tree.

    ``shutil.rmtree`` with the symlink attack resistance enabled on
    platforms that support it; the path is a direct child of the configured
    work root and has already passed the predicate.  It is the only call
    in this module that removes anything, and it is reached only through
    the double gate in :func:`sweep_workspaces`.
    """

    shutil.rmtree(ws, ignore_errors=False)


def sweep_workspaces(
    work_root: str | Path | None,
    *,
    run_items: Iterable[Mapping[str, Any]] | None,
    queue_entries: Iterable[Any] | None = None,
    config: DiskRetentionConfig | None = None,
    mode: RetainMode | None = None,
    now: float | None = None,
) -> list[WorkspaceRetentionReport]:
    """Sweep the direct children of ``work_root`` and return one report each.

    ``run_items`` / ``queue_entries`` are the live state sources (see
    :func:`collect_workspace_refs`); the caller supplies them so the sweep
    never reaches into the supervisor or the queue store itself.  Only
    directories that are *direct* children of the work root are visited —
    a workspace is never descended into to find more workspaces.

    Deletion requires BOTH ``mode.dry_run`` to be false AND
    ``config.workspace_retention_enabled`` to be true.  The flag is
    checked before any call to :func:`_delete_workspace`, so with the flag
    off (the default) no deletion path is reachable even in live mode: the
    sweep then reports candidates as ``selectable`` so the listing is
    identical to a dry-run.
    """

    cfg = config or DEFAULT_CONFIG
    retain_mode = mode if mode is not None else RetainMode.dry()
    now_ts = float(now) if now is not None else time.time()
    if not work_root:
        return []
    root = Path(work_root).expanduser()
    try:
        root = root.resolve(strict=True)
    except OSError:
        return []
    if not root.is_dir():
        return []
    # The work root itself must not be harness-owned; pointing the sweep at
    # the runs tree is a configuration error and is refused outright.
    if HARNESS_OWNED_COMPONENT in root.parts:
        logger.warning("workspace retention: work root %s is harness-owned; refusing", root)
        return []
    refs = collect_workspace_refs(run_items, queue_entries)
    reports: list[WorkspaceRetentionReport] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for child in children:
        # Only real directories directly under the root are workspaces.  A
        # symlink is never followed and never removed: it could point out
        # of the work root, and it is not a clone.
        try:
            is_dir = child.is_dir() and not child.is_symlink()
        except OSError:
            is_dir = False
        if not is_dir:
            continue
        key = os.path.normpath(str(child))
        report = _sweep_one(
            child,
            ref=refs.get(key),
            config=cfg,
            mode=retain_mode,
            now=now_ts,
        )
        if report.state != STATE_SELECTABLE:
            reports.append(report)
            continue
        # Second gate: the explicit flag, independent of the mode.
        if retain_mode.dry_run or not cfg.workspace_retention_enabled:
            reports.append(report)
            continue
        try:
            _delete_workspace(child)
        except OSError as exc:
            report.state = STATE_ERROR
            report.reason = f"failed to remove workspace: {exc}"
            reports.append(report)
            continue
        report.state = STATE_REMOVED
        report.reason = f"{report.reason}; removed"
        reports.append(report)
    return reports


def sweep_workspaces_report(
    work_root: str | Path | None,
    *,
    run_items: Iterable[Mapping[str, Any]] | None,
    queue_entries: Iterable[Any] | None = None,
    config: DiskRetentionConfig | None = None,
    mode: RetainMode | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Same as :func:`sweep_workspaces` but returns a JSON-serialisable summary."""

    reports = sweep_workspaces(
        work_root,
        run_items=run_items,
        queue_entries=queue_entries,
        config=config,
        mode=mode,
        now=now,
    )
    by_state: dict[str, int] = {}
    for r in reports:
        by_state[r.state] = by_state.get(r.state, 0) + 1
    candidates = [r for r in reports if r.state in (STATE_SELECTABLE, STATE_REMOVED)]
    return {
        "schema_version": 1,
        "mode": "apply" if not (mode or RetainMode.dry()).dry_run else "dry-run",
        "enabled": bool((config or DEFAULT_CONFIG).workspace_retention_enabled),
        "work_root": str(Path(work_root).expanduser().resolve()) if work_root else "",
        "max_age_hours": (config or DEFAULT_CONFIG).workspace_max_age_hours,
        "age_source": "run finished_at when known, else workspace mtime",
        "totals": {
            "visited": len(reports),
            "candidates": len(candidates),
            "candidate_bytes": sum(r.size_bytes for r in candidates),
            "by_state": by_state,
        },
        "entries": [r.to_dict() for r in reports],
    }


def write_report(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Persist a report payload as JSON."""

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


__all__ = [
    "ACTIVE_STATUSES",
    "HARNESS_OWNED_COMPONENT",
    "KEEP_STATES",
    "QUEUE_TERMINAL_STATUSES",
    "SECONDS_PER_HOUR",
    "SOURCE_QUEUE",
    "SOURCE_RUN",
    "STATE_ERROR",
    "STATE_KEPT_ACTIVE",
    "STATE_KEPT_HARNESS_OWNED",
    "STATE_KEPT_NO_REFS",
    "STATE_KEPT_RECENT",
    "STATE_REMOVED",
    "STATE_SELECTABLE",
    "STATE_SKIPPED",
    "TERMINAL_STATUSES",
    "WorkspaceRef",
    "WorkspaceRetentionReport",
    "collect_workspace_refs",
    "human_size",
    "is_terminal_reference",
    "should_retain_workspace",
    "sweep_workspaces",
    "sweep_workspaces_report",
    "write_report",
]
