"""Run-folder retention (item 1 of CT110-DISK).

Prune ``runs/<id>`` folders older than ``config.run_max_age_days``; move
failed-run report and summary files to ``runs/_failed-reports/<id>/`` before
deleting the folder.  A run that is ``running``, ``gated`` or ``queued`` is
never touched, regardless of age.

The prune is callable as a CLI sub-command (``python -m
lh_harness.disk_retention runs``) and as an in-process function for the
launcher or supervisor to call.  Dry-run is the default; the operator
runs the module once to inspect ``report.json``, then re-runs with
``--apply`` to actually move and delete.

The module never reads the harness-owned control directories (the run
``id`` is always a single path component, never ``.`` or ``..``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import DEFAULT_CONFIG, DiskRetentionConfig, RetainMode

logger = logging.getLogger(__name__)


# Files inside a run folder that the retention sweep copies to
# ``_failed-reports`` before deleting the folder.  The set is small and
# deliberate: ``report.json`` (the durable failure summary), every
# ``summary.json`` (role_orchestration snapshots), the round log and the
# raw log.  Anything else is removed with the folder; the moved copies are
# the only durable record of a failed run.
FAILED_REPORT_FILES: tuple[str, ...] = (
    "report.json",
    "summary.json",
    "summary.md",
    "report.md",
    "summary.txt",
    "error.log",
    "log.txt",
    "lh_harness.log",
    "exception.log",
)

# A run id is whatever the queue writer writes: an ISO timestamp plus a
# 32-bit hex suffix, sometimes a task-supplied suffix.  The same regex
# the queue uses, defensively re-implemented here so the retention pass
# never trips over a non-id file the operator dropped into ``runs/``.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}$")

# Run states the retention sweep must skip.  The queue may report a run in
# one of several terminal states; the live states are the only ones that
# block a prune.
LIVE_STATES: frozenset[str] = frozenset({"running", "gated", "queued", "pending", "resuming"})


@dataclass
class RunRetentionReport:
    """A per-folder outcome of the retention sweep.

    The report is a structured record the operator can JSON-dump to a
    file, and is the durable input to the next sweep: an entry with
    ``state="pruned"`` was removed (or would have been in dry-run); an
    entry with ``state="skipped"`` carries the reason.
    """

    run_id: str
    state: str  # "pruned" | "skipped" | "skipped_live" | "skipped_recent" | "skipped_no_folder" | "skipped_not_id" | "error"
    reason: str = ""
    age_seconds: float = 0.0
    moved_to: str = ""
    failed_report_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "reason": self.reason,
            "age_seconds": self.age_seconds,
            "moved_to": self.moved_to,
            "failed_report_paths": list(self.failed_report_paths),
        }


def _is_run_id(name: str) -> bool:
    return bool(_RUN_ID_RE.fullmatch(name))


def _state_for_run(
    run_id: str,
    live_lookup: Mapping[str, str] | None,
    queue_states: Iterable[str] | None,
) -> str | None:
    """Resolve the live state of ``run_id``.

    Returns ``None`` when the run is unknown to the queue (a terminal run
    older than the queue's window) or when the caller passed no state
    source at all.  Returning ``None`` is the conservative answer: the
    sweep treats unknown runs as terminal, the documented behaviour.
    """

    if not live_lookup and not queue_states:
        return None
    if live_lookup is not None and run_id in live_lookup:
        return live_lookup[run_id]
    return None


def _move_failed_reports(
    run_dir: Path,
    failed_root: Path,
    *,
    run_id: str,
) -> list[str]:
    """Move the report/summary files of a failed run out of the folder.

    Creates ``failed_root/<run_id>/`` if missing and returns the list of
    destinations actually written.  Returns an empty list when the run had
    no report/summary files.
    """

    target_dir = failed_root / run_id
    moved: list[str] = []
    for name in FAILED_REPORT_FILES:
        src = run_dir / name
        if not src.is_file():
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        dst = target_dir / name
        # ``shutil.move`` falls back to copy+remove across filesystems,
        # which the retention sweep relies on: ``runs`` and the failed
        # reports tree may live on different mounts.
        shutil.move(str(src), str(dst))
        moved.append(str(dst))
    return moved


def _prune_one(
    run_dir: Path,
    *,
    run_id: str,
    config: DiskRetentionConfig,
    mode: RetainMode,
    failed_root: Path,
    live_lookup: Mapping[str, str] | None,
    now: float,
) -> RunRetentionReport:
    """Decide what to do with one ``runs/<run_id>`` folder."""

    if not _is_run_id(run_id):
        return RunRetentionReport(
            run_id=run_id,
            state="skipped_not_id",
            reason="path component does not look like a run id",
        )
    if not run_dir.is_dir():
        return RunRetentionReport(
            run_id=run_id,
            state="skipped_no_folder",
            reason="folder does not exist",
        )
    # Use the folder's mtime as "age": the queue writes a marker on every
    # round, so the mtime tracks the most recent activity.  Falling back
    # to the inode mtime (stat().st_mtime) is portable; ``getmtime`` on
    # symlinks is the same call.
    try:
        mtime = run_dir.stat().st_mtime
    except OSError as exc:
        return RunRetentionReport(
            run_id=run_id,
            state="error",
            reason=f"stat failed: {exc}",
        )
    age = max(0.0, now - mtime)
    max_age = float(config.run_max_age_days) * 86400.0
    if age < max_age:
        return RunRetentionReport(
            run_id=run_id,
            state="skipped_recent",
            reason=f"age {age/86400.0:.1f}d < threshold {config.run_max_age_days}d",
            age_seconds=age,
        )
    state = _state_for_run(run_id, live_lookup, None)
    if state is not None and state.lower() in LIVE_STATES:
        return RunRetentionReport(
            run_id=run_id,
            state="skipped_live",
            reason=f"run is in live state {state!r}; refusing to prune",
            age_seconds=age,
        )
    # Run is old AND not live.  Decide what to move and whether to delete.
    report = RunRetentionReport(
        run_id=run_id,
        state="pruned",
        reason=f"age {age/86400.0:.1f}d >= threshold {config.run_max_age_days}d",
        age_seconds=age,
    )
    if mode.dry_run:
        # Report what would happen without touching the filesystem.
        for name in FAILED_REPORT_FILES:
            if (run_dir / name).is_file():
                report.failed_report_paths.append(str(failed_root / run_id / name))
        report.moved_to = str(failed_root / run_id)
        return report
    # Live prune.
    try:
        moved = _move_failed_reports(run_dir, failed_root, run_id=run_id)
    except OSError as exc:
        return RunRetentionReport(
            run_id=run_id,
            state="error",
            reason=f"failed to move reports: {exc}",
            age_seconds=age,
        )
    report.failed_report_paths = moved
    if moved:
        report.moved_to = str(failed_root / run_id)
    try:
        shutil.rmtree(run_dir)
    except OSError as exc:
        return RunRetentionReport(
            run_id=run_id,
            state="error",
            reason=f"failed to remove folder: {exc}; reports already at {report.moved_to!r}",
            age_seconds=age,
            moved_to=report.moved_to,
            failed_report_paths=report.failed_report_paths,
        )
    return report


def prune_runs(
    runs_root: str | Path,
    *,
    config: DiskRetentionConfig | None = None,
    mode: RetainMode | None = None,
    live_lookup: Mapping[str, str] | None = None,
    failed_reports_root: str | Path | None = None,
    now: float | None = None,
) -> list[RunRetentionReport]:
    """Sweep ``runs_root`` and return one report per folder visited.

    The function is the in-process entry point; the CLI sub-command
    (and the operator runbook) wraps it with a JSON dump.  ``mode`` is
    dry-run by default; pass ``mode=RetainMode.live()`` to actually
    delete.  ``live_lookup`` is an optional ``run_id -> state`` map; the
    sweep skips any run whose state is in :data:`LIVE_STATES`.
    """

    cfg = config or DEFAULT_CONFIG
    if mode is None:
        mode = RetainMode.dry()
    runs_root_path = Path(runs_root).expanduser().resolve()
    if not runs_root_path.exists() or not runs_root_path.is_dir():
        return []
    if failed_reports_root is None:
        failed_root = runs_root_path / "_failed-reports"
    else:
        failed_root = Path(failed_reports_root).expanduser().resolve()
    failed_root.mkdir(parents=True, exist_ok=True)
    now_ts = float(now) if now is not None else time.time()
    reports: list[RunRetentionReport] = []
    for entry in sorted(runs_root_path.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            # ``_failed-reports`` and dot-files (macOS metadata) are
            # never pruned.  The failed-reports root is the explicit
            # sibling; we are scanning the runs root.
            continue
        if entry.name == "_failed-reports":
            continue
        report = _prune_one(
            entry,
            run_id=entry.name,
            config=cfg,
            mode=mode,
            failed_root=failed_root,
            live_lookup=live_lookup,
            now=now_ts,
        )
        reports.append(report)
    return reports


def prune_runs_report(
    runs_root: str | Path,
    *,
    config: DiskRetentionConfig | None = None,
    mode: RetainMode | None = None,
    live_lookup: Mapping[str, str] | None = None,
    failed_reports_root: str | Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Same as :func:`prune_runs` but returns a JSON-serialisable summary."""

    reports = prune_runs(
        runs_root,
        config=config,
        mode=mode,
        live_lookup=live_lookup,
        failed_reports_root=failed_reports_root,
        now=now,
    )
    by_state: dict[str, int] = {}
    for r in reports:
        by_state[r.state] = by_state.get(r.state, 0) + 1
    return {
        "schema_version": 1,
        "mode": "apply" if not (mode or RetainMode.dry()).dry_run else "dry-run",
        "runs_root": str(Path(runs_root).expanduser().resolve()),
        "failed_reports_root": str(
            Path(failed_reports_root or (Path(runs_root) / "_failed-reports")).expanduser().resolve()
        ),
        "totals": {
            "visited": len(reports),
            "by_state": by_state,
        },
        "entries": [r.to_dict() for r in reports],
    }


def write_report(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Persist a report payload as JSON.  Used by the CLI sub-command."""

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


__all__ = [
    "FAILED_REPORT_FILES",
    "LIVE_STATES",
    "RunRetentionReport",
    "prune_runs",
    "prune_runs_report",
    "write_report",
]
