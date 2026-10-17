"""Daily disk check (item 4 of CT110-DISK).

Inspect the disk that holds a path and decide:

* ``warn`` at ``config.warn_pct`` (default 80 %)
* ``pause`` at ``config.pause_pct`` (default 90 %)
* ``resume`` when below ``config.resume_pct`` (default 85 %)

The launcher reads :func:`disk_skip_reason`; if it returns ``"disk_full"``
the launch is skipped (mirrors ``_drain_skip_reason`` and
``_maintenance_pause_skip_reason``).  The pause rule is sticky: the
launcher keeps refusing until a fresh check sees the disk below
``resume_pct``, so a 90 % spike cannot be reset by a fleeting 89 %
sample.

The :func:`check_disk` function is the in-process entry point.  It
takes a path so a CT110-style deployment can check the *rootfs* (which
is the actual pressure point in the original incident) instead of the
working directory's filesystem.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .config import DEFAULT_CONFIG, DiskRetentionConfig

logger = logging.getLogger(__name__)


# The skip reason the launcher looks for.  Keep this exact string; the
# dashboard's disk tile filters on it.
SKIP_REASON_DISK_FULL = "disk_full"

# Stable, machine-readable state values; surfaced through
# ``/api/meta.disk`` and the disk-check event.
STATE_OK = "ok"
STATE_WARN = "warn"
STATE_PAUSED = "paused"
STATE_RESUMED = "resumed"


@dataclass
class DiskState:
    """A snapshot of disk usage."""

    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent_used: float
    state: str
    skip_reason: str | None
    checked_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "percent_used": round(self.percent_used, 2),
            "state": self.state,
            "skip_reason": self.skip_reason,
            "checked_at": self.checked_at,
        }


def _statvfs(path: str) -> os.statvfs_result | None:
    try:
        return os.statvfs(path)
    except OSError:
        return None


def _usage_for(path: str) -> tuple[int, int, int, float] | None:
    """Return ``(total, used, free, percent_used)`` for ``path``'s filesystem.

    ``percent_used`` is on a 0..100 scale, not a fraction.  ``None`` is
    returned when the path does not exist or statvfs is unavailable.
    """

    sv = _statvfs(path)
    if sv is None:
        return None
    bs = int(sv.f_frsize) or int(sv.f_bsize)
    total = int(sv.f_blocks) * bs
    free = int(sv.f_bavail) * bs
    used = max(0, total - free)
    pct = (used / total * 100.0) if total > 0 else 0.0
    return total, used, free, pct


def _resolve_state(
    percent: float,
    config: DiskRetentionConfig,
    *,
    previously_paused: bool = False,
) -> tuple[str, str | None]:
    """Map a percent-used reading to ``(state, skip_reason)``.

    The state machine is the documented one:

    * ``pct >= pause_pct``  →  ``paused`` (launcher refuses new runs)
    * ``pct >= warn_pct``   →  ``warn``  (log + ``/api/meta`` + event)
    * otherwise              →  ``ok``     (or ``resumed`` if previously
      ``paused`` — the dashboard uses the latter to confirm the recovery)

    The hysteresis between ``pause_pct`` and ``resume_pct`` lives in the
    caller: ``disk_skip_reason`` only returns ``disk_full`` while
    ``percent >= pause_pct`` OR while the launcher still holds its sticky
    pause flag.  This function returns the *fresh* state; the caller is
    responsible for sticking it.
    """

    if percent >= config.pause_pct:
        return STATE_PAUSED, SKIP_REASON_DISK_FULL
    if percent >= config.warn_pct:
        return STATE_WARN, None
    if previously_paused:
        return STATE_RESUMED, None
    return STATE_OK, None


def check_disk(
    path: str | Path,
    *,
    config: DiskRetentionConfig | None = None,
    previously_paused: bool = False,
    now: float | None = None,
) -> DiskState:
    """Inspect ``path``'s filesystem and return a :class:`DiskState`.

    A path that does not exist (or whose statvfs fails) is reported as
    ``ok`` with ``percent_used=0.0`` so the dashboard does not flash a
    red banner on a transient I/O error.  The error is logged at WARNING
    so an operator sees it in the service log.
    """

    cfg = config or DEFAULT_CONFIG
    path_str = str(Path(path).expanduser().resolve())
    now_ts = float(now) if now is not None else time.time()
    usage = _usage_for(path_str)
    if usage is None:
        logger.warning("disk-retention: statvfs failed for %s; reporting ok", path_str)
        return DiskState(
            path=path_str,
            total_bytes=0,
            used_bytes=0,
            free_bytes=0,
            percent_used=0.0,
            state=STATE_OK,
            skip_reason=None,
            checked_at=now_ts,
        )
    total, used, free, pct = usage
    state, skip_reason = _resolve_state(pct, cfg, previously_paused=previously_paused)
    return DiskState(
        path=path_str,
        total_bytes=total,
        used_bytes=used,
        free_bytes=free,
        percent_used=pct,
        state=state,
        skip_reason=skip_reason,
        checked_at=now_ts,
    )


def disk_skip_reason(
    path: str | Path,
    *,
    config: DiskRetentionConfig | None = None,
    sticky_pause: bool = False,
) -> str | None:
    """The string the launcher reads at launch time.

    Returns ``"disk_full"`` when the disk is at or above ``pause_pct``,
    or when ``sticky_pause`` is set and the disk is still above
    ``resume_pct``.  Returns ``None`` otherwise.
    """

    cfg = config or DEFAULT_CONFIG
    usage = _usage_for(str(Path(path).expanduser().resolve()))
    if usage is None:
        return None
    _total, _used, _free, pct = usage
    if pct >= cfg.pause_pct:
        return SKIP_REASON_DISK_FULL
    if sticky_pause and pct >= cfg.resume_pct:
        return SKIP_REASON_DISK_FULL
    return None


def format_warning_event(state: DiskState) -> dict[str, Any]:
    """The dict the overseer can read from the event stream.

    The schema is stable (consumers can switch on ``kind``); the
    ``percent_used`` field is the one a dashboard tile plots.
    """

    return {
        "kind": "disk.warning" if state.state == STATE_WARN else f"disk.{state.state}",
        "path": state.path,
        "percent_used": round(state.percent_used, 2),
        "total_bytes": state.total_bytes,
        "used_bytes": state.used_bytes,
        "free_bytes": state.free_bytes,
        "skip_reason": state.skip_reason,
        "checked_at": state.checked_at,
    }


def write_report(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Persist a disk-state report as JSON."""

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


__all__ = [
    "DiskState",
    "SKIP_REASON_DISK_FULL",
    "STATE_OK",
    "STATE_PAUSED",
    "STATE_RESUMED",
    "STATE_WARN",
    "check_disk",
    "disk_skip_reason",
    "format_warning_event",
    "write_report",
]
