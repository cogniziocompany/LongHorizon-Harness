"""How a finished run ended, in a form small enough for the run list.

``GET /api/runs?fields=summary`` is the path Hydra's ``list_fleet_runs`` reads.
Its rows used to say nothing about how a run ended, so a run created outside
the queue (no queue entry carrying the launcher's reason) showed no cause at
all.  This module derives three optional fields for terminal runs:

``outcome``
    The run's final status: the terminal lifecycle status from
    ``control/status.json``, else report.json ``status`` when terminal.
``abort_reason``
    report.json ``abort_reason`` (a short token such as
    ``provider_rate_limit``), else the ``Stopped:`` line the worker printed.
``failure_reason``
    The human-readable cause, built like the launcher's queue reason:
    ``<abort_reason> | <failure_reason>`` from report.json plus the
    supervisor's ``failure_reason`` (status.json), else a supervisor-generated
    report's ``error``, else the ``Stopped:`` line of ``worker.log``.

Both reasons are ASCII-only and at most 200 characters (callers forward them
into ASCII-only channels, e.g. CT110 gate rationales).
"""

from __future__ import annotations

import os
import re
import stat
import threading
from pathlib import Path
from typing import Any, Callable

REASON_MAX_CHARS = 200
# The worker prints its summary (``Stopped:   <abort_reason>``) at the end of
# worker.log; only the tail is read.
WORKER_LOG_TAIL_BYTES = 16 * 1024

_TERMINAL = frozenset({"completed", "failed", "cancelled", "blocked", "incomplete"})
_STOPPED_RE = re.compile(r"^Stopped:\s+(\S.*?)\s*$", re.MULTILINE)
_ASCII_MAP = {
    "—": "-", "–": "-", "‒": "-", "‐": "-", "‑": "-", "−": "-",
    "…": "...", "→": "->", "←": "<-", "⇒": "=>",
    "‘": "'", "’": "'", "“": '"', "”": '"', " ": " ",
    "·": "-", "•": "-",
}


def ascii_reason(value: Any, limit: int = REASON_MAX_CHARS) -> str:
    """Plain ASCII, whitespace collapsed, at most ``limit`` characters."""

    text = str(value or "")
    for src, dst in _ASCII_MAP.items():
        text = text.replace(src, dst)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def _status_value(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def stopped_line(log_tail: str) -> str:
    """The last ``Stopped:`` value in a worker.log tail, or ''."""

    matches = _STOPPED_RE.findall(log_tail or "")
    return matches[-1] if matches else ""


def read_log_tail(path: Path, max_bytes: int = WORKER_LOG_TAIL_BYTES) -> str:
    """Last ``max_bytes`` of a regular file, without following a symlink."""

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return ""
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode):
            return ""
        if meta.st_size > max_bytes:
            os.lseek(fd, meta.st_size - max_bytes, os.SEEK_SET)
        raw = os.read(fd, max_bytes)
    except OSError:
        return ""
    finally:
        os.close(fd)
    return raw.decode("utf-8", "replace")


def derive_outcome(
    lifecycle_status: str,
    report: dict[str, Any] | None,
    status_record: dict[str, Any] | None = None,
    log_tail: str = "",
) -> dict[str, str]:
    """``{"outcome", "abort_reason", "failure_reason"}`` for a terminal run.

    Returns ``{}`` for a run that is not terminal (neither its lifecycle nor
    its report says it ended), so live rows keep their old shape.  Keys whose
    value would be empty are left out.
    """

    report = report if isinstance(report, dict) else {}
    status_record = status_record if isinstance(status_record, dict) else {}
    lifecycle = _status_value(lifecycle_status)
    report_status = _status_value(report.get("status"))
    # The lifecycle status is the reconciled authority (the supervisor can
    # fail a run whose report claims completion); the report fills in when the
    # lifecycle is not terminal yet.
    if lifecycle in _TERMINAL:
        outcome = lifecycle
    elif report_status in _TERMINAL:
        outcome = report_status
    else:
        return {}

    abort = ascii_reason(report.get("abort_reason"))
    parts = [
        abort,
        ascii_reason(report.get("failure_reason")),
        ascii_reason(status_record.get("failure_reason")),
    ]
    if not any(parts) and report.get("supervisor_generated"):
        parts.append(ascii_reason(report.get("error")))
    if not any(parts):
        stopped = ascii_reason(stopped_line(log_tail))
        if stopped:
            abort = abort or stopped
            parts.append(stopped)
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)

    result = {"outcome": outcome}
    if abort:
        result["abort_reason"] = abort
    failure = ascii_reason(" | ".join(seen))
    if failure:
        result["failure_reason"] = failure
    return result


class OutcomeCache:
    """Per-run memo keyed by the report's and log's (mtime_ns, size).

    A finished run's report does not change, so the summary list parses each
    report.json once instead of on every poll.
    """

    def __init__(self, max_entries: int = 4096) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[tuple[Any, ...], dict[str, str]]] = {}
        self._max = max_entries

    @staticmethod
    def _sig(path: Path) -> tuple[int, int] | None:
        try:
            meta = os.stat(path, follow_symlinks=False)
        except OSError:
            return None
        return (meta.st_mtime_ns, meta.st_size)

    def get(
        self,
        run_dir: Path,
        logs_dir: Path,
        lifecycle_status: str,
        status_record: dict[str, Any],
        read_report: Callable[[Path], dict[str, Any]],
    ) -> dict[str, str]:
        report_path = logs_dir / "report.json"
        log_path = run_dir / "worker.log"
        key_sig = (
            _status_value(lifecycle_status),
            str(status_record.get("failure_reason") or ""),
            self._sig(report_path),
            self._sig(log_path),
        )
        cache_key = str(run_dir)
        with self._lock:
            hit = self._entries.get(cache_key)
            if hit is not None and hit[0] == key_sig:
                return dict(hit[1])
        report = read_report(report_path) if key_sig[2] is not None else {}
        lifecycle = key_sig[0]
        needs_log = (
            (lifecycle in _TERMINAL or _status_value(report.get("status")) in _TERMINAL)
            and not report.get("abort_reason")
            and not report.get("failure_reason")
            and not status_record.get("failure_reason")
        )
        log_tail = read_log_tail(log_path) if needs_log and key_sig[3] is not None else ""
        value = derive_outcome(lifecycle_status, report, status_record, log_tail)
        with self._lock:
            if len(self._entries) >= self._max:
                self._entries.clear()
            self._entries[cache_key] = (key_sig, value)
        return dict(value)
