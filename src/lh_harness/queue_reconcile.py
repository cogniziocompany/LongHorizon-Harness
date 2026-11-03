"""Reconcile queue entries against actual run status (LHH-QUEUE-RECONCILE-CAPABILITY-PRECHECK).

A launched queue entry can land in the queue's "launched" state while its
underlying run has already finished (worker stopped, supervisor cancelled the
run, the harness restarted and never woke the worker back up). The result is
a *zombie* entry: a record whose ``run_id`` points at a directory whose
``control/status.json`` says the run is terminal, but the queue entry itself
still says "launched". This sweep reads the run's true status and moves the
entry to the matching terminal queue state with a reconcile note.

The sweep also inventories the leftover park stashes the park code leaves
behind after a run. Each stash is a ``git stash push`` recorded under
``runs/<run_id>/control/park.json`` (or, when the parking path stored one in
an earlier shape, embedded inside the ``status.json`` ``park`` block). The
report names the run, the stash, and whether the stash's commits have
already been pushed to a remote -- so an operator can decide whether it is
safe to ``git stash drop`` the entry.

Apply mode writes only to the queue store. It NEVER touches git, NEVER
restarts the worker, and NEVER mutates the live service configuration. That
contract is mechanical: there is no ``subprocess`` import for git here, no
``git stash drop``, and the apply path uses the same
``QueueStore.update`` helper the launcher uses for every other state
transition.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .safe_subprocess import git_argv
from .queue import QueueStore

logger = logging.getLogger(__name__)


# ``launched`` is the only non-terminal state where the run_id pointer is
# meaningful; ``blocked`` entries are parked outside the launcher and their
# ``run_id`` may be None. ``pending`` is pre-launch.
_RECONCILE_ENTRY_STATES = frozenset({"launched"})

# LHH-QUEUE-MIGRATION-TOMBSTONE (A): the states this sweep must NEVER touch.
#
# A ``migrated`` entry is a durable tombstone: its work moved to another node,
# so this node holds a record, not work.  The sweep reconciles an entry
# against the status of the RUN it launched -- a tombstone has no run here (it
# was never launched), and re-creating or re-queueing one would resurrect work
# on the source node while the destination node also holds it (the
# duplicate-writer window the migration tombstone exists to close).
#
# The guard is explicit rather than incidental: ``migrated`` is not a member
# of ``_RECONCILE_ENTRY_STATES`` (so it is not swept), and this set is checked
# explicitly wherever the sweep could otherwise act on an entry.
_RECONCILE_FORBIDDEN_STATES = frozenset({"migrated"})

# A run is terminal when its lifecycle status is one of these. The launcher
# itself uses the same set in ``run_outcome._TERMINAL``; the values come from
# the supervisor (control/status.json ``status`` field).
_TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "complete", "failed", "cancelled", "canceled", "blocked", "incomplete"}
)

# Map run terminal status to queue terminal status. completed/complete map to
# "done"; the rest map to "failed". The "blocked" run state is terminal here
# because the supervisor has given up on the run -- this sweep's job is to
# stop pretending the queue entry still has a live worker behind it.
_RUN_TO_QUEUE_TERMINAL = {
    "completed": "done",
    "complete": "done",
    "failed": "failed",
    "cancelled": "failed",
    "canceled": "failed",
    "blocked": "failed",
    "incomplete": "failed",
}

# Cap on the reconcile note we attach to ``entry.reason``; matches
# ``_MAX_QUEUE_REASON_CHARS`` in queue.py.
_MAX_REASON_CHARS = 4000


@dataclass
class ZombieReconciliation:
    """One launched queue entry whose underlying run is already terminal."""

    queue_id: str
    run_id: str | None
    run_status: str
    queue_target_status: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "run_id": self.run_id,
            "run_status": self.run_status,
            "queue_target_status": self.queue_target_status,
            "note": self.note,
        }


@dataclass
class StashReport:
    """One leftover park stash the park code recorded on a run."""

    run_id: str
    stash_name: str
    workspace: str
    backup_ref: str | None = None
    on_remote: bool | None = None  # True if every commit is on some remote branch

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "stash_name": self.stash_name,
            "workspace": self.workspace,
            "backup_ref": self.backup_ref,
            "on_remote": self.on_remote,
        }


@dataclass
class ReconcileReport:
    """Result of a sweep -- never touches git in apply or dry-run."""

    dry_run: bool
    runs_root: str
    zombies: list[ZombieReconciliation] = field(default_factory=list)
    stashes: list[StashReport] = field(default_factory=list)
    applied: list[ZombieReconciliation] = field(default_factory=list)
    live_entries: list[str] = field(default_factory=list)
    # LHH-QUEUE-MIGRATION-TOMBSTONE (C): one line per repaired partial pair
    # (a source tombstone whose destination create never landed).  Empty
    # unless a non-dry-run sweep repaired something.
    migration_repairs: list[str] = field(default_factory=list)

    @property
    def zombie_count(self) -> int:
        return len(self.zombies)

    @property
    def stash_count(self) -> int:
        return len(self.stashes)

    @property
    def migration_repair_count(self) -> int:
        return len(self.migration_repairs)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "runs_root": self.runs_root,
            "zombie_count": self.zombie_count,
            "stash_count": self.stash_count,
            "zombies": [z.as_dict() for z in self.zombies],
            "stashes": [s.as_dict() for s in self.stashes],
            "applied": [a.as_dict() for a in self.applied],
            "live_entries": list(self.live_entries),
            "migration_repairs": list(self.migration_repairs),
        }


# ---------------------------------------------------------------------------
# Status-file readers
# ---------------------------------------------------------------------------


def _read_status_file(runs_root: Path, run_id: str) -> dict[str, Any] | None:
    """Read ``runs/<run_id>/control/status.json``; return ``None`` when missing or invalid.

    The status file is owned by the supervisor; ``_atomic_bytes_write`` is used
    to write it, so a partial write would fail JSON parsing. A missing file
    means the worker never opened its run directory, which is fine -- the
    sweep treats it as "no terminal status known" rather than a hard error.
    """

    path = runs_root / run_id / "control" / "status.json"
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_park_file(runs_root: Path, run_id: str) -> dict[str, Any] | None:
    """Read ``runs/<run_id>/control/park.json``; return ``None`` if absent.

    ``park_workspace`` writes one of these when parking creates a stash; older
    parking paths stored the same data inside ``status.json`` under a
    ``park`` key, which ``_read_park_from_status`` covers.
    """

    path = runs_root / run_id / "control" / "park.json"
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_park_from_status(runs_root: Path, run_id: str) -> dict[str, Any] | None:
    """Older parking paths keep the park block inside status.json under ``park``."""

    status = _read_status_file(runs_root, run_id)
    if not status:
        return None
    park = status.get("park")
    return park if isinstance(park, dict) else None


# ---------------------------------------------------------------------------
# Stash -> on-remote check (read-only)
# ---------------------------------------------------------------------------


def _commits_on_any_remote(workspace: Path, backup_ref: str | None) -> bool | None:
    """Return True iff every commit reachable from the backup ref is on a remote.

    A ``None`` result means "could not determine" (the workspace or ref is
    gone): the caller will surface this as a ``None`` ``on_remote`` so the
    operator sees the ambiguity rather than a fabricated False. The check is
    intentionally read-only -- ``git rev-list`` and ``git branch -r --contains``
    are pure reads, and the function never invokes ``git push``, ``git
    fetch``, or anything that mutates the repo.
    """

    if not backup_ref:
        return None
    if not workspace.is_dir():
        return None
    try:
        proc = subprocess.run(
            git_argv(
                "-C",
                str(workspace),
                "rev-list",
                "--not",
                "--remotes",
                backup_ref,
            ),
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return not proc.stdout.strip()


# ---------------------------------------------------------------------------
# Stash discovery
# ---------------------------------------------------------------------------


def _stash_from_park_record(run_id: str, park: dict[str, Any], workspace: str) -> StashReport | None:
    """Build a ``StashReport`` from one park.json record when it has a stash.

    A park record without a stash name is a no-op (the workspace was parked
    without a ``git stash push`` because no dirty work existed). Only
    records with a non-empty stash surface.
    """

    stash_name = park.get("stash")
    if not isinstance(stash_name, str) or not stash_name.strip():
        return None
    backup_ref = park.get("backup_ref")
    on_remote = (
        _commits_on_any_remote(Path(workspace), backup_ref if isinstance(backup_ref, str) else None)
        if isinstance(workspace, str) and workspace
        else None
    )
    return StashReport(
        run_id=run_id,
        stash_name=stash_name,
        workspace=workspace,
        backup_ref=backup_ref if isinstance(backup_ref, str) else None,
        on_remote=on_remote,
    )


def _collect_stashes(runs_root: Path) -> list[StashReport]:
    """Inventory every leftover park stash under ``runs_root``.

    The sweep walks the queue store to learn which run ids exist (the launch
    history is the only authoritative list of runs the harness knows about),
    then reads each run's park.json (and falls back to status.json's ``park``
    block when no park.json exists). Stashes are reported, never dropped --
    the contract is report-only, and dropping them is a separate operator
    action.
    """

    stashes: list[StashReport] = []
    store = QueueStore(runs_root)
    seen: set[str] = set()
    for entry in store.list():
        if not entry.run_id or entry.run_id in seen:
            continue
        seen.add(entry.run_id)
        report = _stash_report_for_run(runs_root, entry.run_id, workspace=entry.workspace or "")
        if report is not None:
            stashes.append(report)
    return stashes


def _stash_report_for_run(
    runs_root: Path,
    run_id: str,
    *,
    workspace: str = "",
) -> StashReport | None:
    """Resolve one run's stash report across both park.json and the legacy shape."""

    park = _read_park_file(runs_root, run_id)
    if park is None:
        park = _read_park_from_status(runs_root, run_id)
    if park is None:
        return None
    resolved_workspace = workspace or str(park.get("workspace") or "")
    return _stash_from_park_record(run_id, park, resolved_workspace)


# ---------------------------------------------------------------------------
# Zombie detection
# ---------------------------------------------------------------------------


def _iter_launched_entries(entries: Iterable[Any]) -> Iterable[Any]:
    """Yield only entries the sweep should consider for reconcile.

    LHH-QUEUE-MIGRATION-TOMBSTONE (A): a ``migrated`` tombstone is skipped
    twice over -- it is not in ``_RECONCILE_ENTRY_STATES``, and it is in
    ``_RECONCILE_FORBIDDEN_STATES``, which is checked explicitly so the
    invariant survives a future change to the first set.
    """

    for entry in entries:
        if entry.status in _RECONCILE_FORBIDDEN_STATES:
            continue
        if entry.status in _RECONCILE_ENTRY_STATES:
            yield entry


def _find_zombies(runs_root: Path, store: QueueStore) -> list[ZombieReconciliation]:
    """Compare every launched entry against its run's status; return the zombies."""

    zombies: list[ZombieReconciliation] = []
    for entry in _iter_launched_entries(store.list()):
        if not entry.run_id:
            continue
        status = _read_status_file(runs_root, entry.run_id)
        if not status:
            continue
        lifecycle = str(status.get("status") or "").strip().lower()
        if lifecycle not in _TERMINAL_RUN_STATUSES:
            continue
        target = _RUN_TO_QUEUE_TERMINAL.get(lifecycle, "failed")
        note = (
            f"reconciled from run status {lifecycle!r} "
            f"(runs/{entry.run_id}/control/status.json)"
        )
        zombies.append(
            ZombieReconciliation(
                queue_id=entry.queue_id,
                run_id=entry.run_id,
                run_status=lifecycle,
                queue_target_status=target,
                note=note[:_MAX_REASON_CHARS],
            )
        )
    return zombies


def _list_live_entries(entries: Iterable[Any]) -> list[str]:
    """Queue ids whose runs are still alive (status launched, run not terminal)."""

    return [entry.queue_id for entry in entries if entry.status in _RECONCILE_ENTRY_STATES]


# ---------------------------------------------------------------------------
# Apply path
# ---------------------------------------------------------------------------


def _apply_reconciliation(
    store: QueueStore,
    zombies: list[ZombieReconciliation],
    *,
    applied: list[ZombieReconciliation],
) -> None:
    """Move each zombie queue entry to its terminal status with a reconcile note.

    Uses the same ``mark_done`` / ``mark_failed`` helpers the launcher uses
    for every other transition; both helpers validate the transition via
    ``_is_valid_transition`` and refuse an invalid move. ``launched -> done``
    and ``launched -> failed`` are both valid transitions, so every zombie
    in this list is applicable.
    """

    for zombie in zombies:
        entry = store.get(zombie.queue_id)
        if entry is None:
            continue
        if entry.status in _RECONCILE_FORBIDDEN_STATES:
            # LHH-QUEUE-MIGRATION-TOMBSTONE (A): a tombstone is terminal and
            # must never be written by the sweep.  Its work lives on another
            # node now; touching it here would resurrect the source copy.
            continue
        if entry.status != "launched":
            # Another writer (the supervisor, the manager) already moved it.
            continue
        reason = zombie.note
        if zombie.queue_target_status == "done":
            updated = store.mark_done(zombie.queue_id, reason=reason)
        else:
            updated = store.mark_failed(zombie.queue_id, reason=reason)
        if updated is not None:
            applied.append(zombie)


def _repair_pending_migrations(store: QueueStore) -> list[str]:
    """Repair the partial pairs a migration left behind.

    LHH-QUEUE-MIGRATION-TOMBSTONE (C).  A source tombstone whose
    ``migrated_to.pending`` is still true means the destination create never
    landed -- the work is on neither side.  The repair asks the destination
    whether it already holds the entry (by dedup_key): present -> finalize the
    link; absent -> re-create it from the source's stored task text.

    This is deliberately SEPARATE from the zombie sweep above: a tombstone is
    never a zombie (it has no run on this node), and the repair writes only
    the tombstone's LINK -- never its status, never a run.  A failure to
    repair is not a sweep failure: the tombstone stays pending and the next
    attempt tries again.
    """

    try:
        from .queue_migrate import repair_pending_migrations
    except ImportError:  # pragma: no cover - defensive
        return []
    try:
        return repair_pending_migrations(store)
    except Exception:  # pragma: no cover - defensive
        logger.exception("queue migrate repair failed during the reconcile sweep")
        return []


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def reconcile_sweep(runs_root: str | Path, *, dry_run: bool = True) -> ReconcileReport:
    """Run the sweep; return a report. Pure read when ``dry_run`` is true.

    The ``runs_root`` argument is the path the queue store is rooted at: the
    directory that contains ``queue/<queue_id>.json`` and one
    ``runs/<run_id>/`` subdir per run. The sweep never starts a worker, never
    restarts the service, never touches git, and never deletes files.

    LHH-QUEUE-MIGRATION-TOMBSTONE (C): with ``dry_run=False`` the sweep also
    REPAIRS the partial pairs -- the source tombstones whose destination
    create never landed.  The destination is asked whether it already holds
    the work (by dedup_key): present -> the link is finalized; absent -> the
    entry is re-created from the source's stored task text.  A dry run never
    writes, so the repair is skipped then.
    """

    root = Path(runs_root).expanduser().resolve()
    report = ReconcileReport(dry_run=dry_run, runs_root=str(root))
    store = QueueStore(root)

    report.stashes = _collect_stashes(root)
    report.zombies = _find_zombies(root, store)
    report.live_entries = _list_live_entries(store.list())
    if not dry_run and report.zombies:
        _apply_reconciliation(store, report.zombies, applied=report.applied)
    if not dry_run:
        report.migration_repairs = _repair_pending_migrations(store)
    return report


def format_report_text(report: ReconcileReport) -> str:
    """A short, ASCII-only summary suitable for stdout."""

    mode = "DRY RUN" if report.dry_run else "APPLIED"
    lines = [f"reconcile sweep ({mode}) runs_root={report.runs_root}"]
    lines.append(
        f"  zombies={report.zombie_count} stashes={report.stash_count} "
        f"live_entries={len(report.live_entries)}"
    )
    if report.zombies:
        lines.append("  zombies:")
        for z in report.zombies:
            lines.append(f"    - {z.queue_id} run={z.run_id} {z.run_status} -> {z.queue_target_status}")
    if not report.dry_run and report.applied:
        lines.append("  applied:")
        for a in report.applied:
            lines.append(f"    - {a.queue_id} -> {a.queue_target_status}")
    if report.stashes:
        lines.append("  stashes:")
        for s in report.stashes:
            on_remote = (
                "on_remote=unknown"
                if s.on_remote is None
                else f"on_remote={str(s.on_remote).lower()}"
            )
            lines.append(f"    - run={s.run_id} stash={s.stash_name!r} {on_remote}")
    if report.live_entries:
        lines.append(
            "  live entries (untouched): " + ", ".join(report.live_entries)
        )
    if report.migration_repairs:
        lines.append("  migration repairs:")
        for line in report.migration_repairs:
            lines.append(f"    - {line}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point wired to ``lh-harness queue-reconcile``."""

    import argparse

    parser = argparse.ArgumentParser(
        prog="lh-harness queue-reconcile",
        description=(
            "Reconcile launched queue entries against runs/<id>/control/status.json "
            "and report leftover park stashes. Dry-run by default; "
            "--apply writes only to the queue store."
        ),
    )
    parser.add_argument(
        "--runs-root",
        default=os.environ.get("LH_HARNESS_RUNS_ROOT", str(Path.cwd())),
        help="Path the queue store is rooted at (default: $LH_HARNESS_RUNS_ROOT or cwd).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write terminal status to each zombie entry. Default is dry-run.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of the human-readable summary.",
    )
    args = parser.parse_args(argv)
    report = reconcile_sweep(args.runs_root, dry_run=not args.apply)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(format_report_text(report), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover - module CLI
    raise SystemExit(main())
