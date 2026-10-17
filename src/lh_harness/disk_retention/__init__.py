"""Harness disk retention for CT110.

Five components, all configurable via the harness settings DB:

1. ``runs.py`` — prune ``runs/<id>`` folders older than 14 days; move
   failed-run report/summary files to ``runs/_failed-reports/<id>/`` before
   deletion; never touch a run that is running, gated, or queued.
2. ``worktrees.py`` — remove parked work trees idle > 7 days with no active
   run; if dirty, list in the report instead of deleting.
3. ``caches.py`` — cap npm and pip caches at a configurable size (default
   4 GB each), pruning oldest entries.
4. ``disk_check.py`` — warn at 80 % used; pause new launches at 90 %
   (``skip_reason: disk_full``); auto-resume under 85 %.
5. ``config.py`` — a single ``DiskRetentionConfig`` object that reads every
   threshold from the settings DB with safe defaults, and the dry-run
   ``RetainMode`` used by all four retention modules.

Limits: dry-run is the first mode an operator runs; no module here deletes
or moves a real file unless ``dry_run=False`` is explicit.  See
``docs/disk-retention.md`` for the operator runbook.
"""

from __future__ import annotations

from .config import (
    DEFAULT_CONFIG,
    DiskRetentionConfig,
    RetainMode,
    config_from_runtime,
    config_from_values,
)
from .caches import prune_caches, prune_caches_report
from .disk_check import (
    DiskState,
    check_disk,
    disk_skip_reason,
    format_warning_event,
)
from .runs import (
    RunRetentionReport,
    prune_runs,
    prune_runs_report,
)
from .worktrees import (
    WorkTreeRetentionReport,
    prune_worktrees,
    prune_worktrees_report,
)

__all__ = [
    "DEFAULT_CONFIG",
    "DiskRetentionConfig",
    "RetainMode",
    "config_from_runtime",
    "config_from_values",
    "prune_caches",
    "prune_caches_report",
    "prune_runs",
    "prune_runs_report",
    "prune_worktrees",
    "prune_worktrees_report",
    "check_disk",
    "disk_skip_reason",
    "format_warning_event",
    "DiskState",
    "RunRetentionReport",
    "WorkTreeRetentionReport",
]
