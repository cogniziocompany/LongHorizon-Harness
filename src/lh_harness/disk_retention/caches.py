"""Cache cap enforcement (item 3 of CT110-DISK).

Cap npm and pip caches at the byte limits in the settings DB (default
4 GiB each), pruning oldest entries.  The cap is per-cache; a separate
``LH_HARNESS_DISK_CACHE_MAX_BYTES`` is the fallback for either cache if
its own setting is missing.

"Oldest" means the entry with the oldest atime/mtime, whichever is
older — the harness's call patterns touch some caches more often than
they write, so atime is the better proxy for "this entry has not been
useful in a while".  The sweep is a best-effort: if the cap is wildly
below the current size, the sweep keeps removing until it is at or
under the cap, but a single sweep that overshoots is fine (the next
sweep is idempotent).

A cap of zero or negative is treated as "no cap" — the function returns
the cache untouched and a zero-sized "no-op" report.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import DEFAULT_CONFIG, DiskRetentionConfig, RetainMode

logger = logging.getLogger(__name__)


@dataclass
class CacheEntryReport:
    """One cache entry's outcome (a file or a directory)."""

    path: str
    size_bytes: int
    state: str  # "removed" | "kept" | "error"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "state": self.state,
            "reason": self.reason,
        }


@dataclass
class CacheReport:
    """A per-cache summary of the cap pass."""

    name: str
    path: str
    cap_bytes: int
    initial_bytes: int
    final_bytes: int
    removed_count: int
    removed_bytes: int
    entries: list[CacheEntryReport] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "cap_bytes": self.cap_bytes,
            "initial_bytes": self.initial_bytes,
            "final_bytes": self.final_bytes,
            "removed_count": self.removed_count,
            "removed_bytes": self.removed_bytes,
            "entries": [e.to_dict() for e in self.entries],
        }


def _safe_stat(p: Path) -> os.stat_result | None:
    try:
        return p.stat()
    except OSError:
        return None


def _size_of(p: Path) -> int:
    """Recursive size of a file or directory, in bytes."""

    if p.is_file() or p.is_symlink():
        st = _safe_stat(p)
        return int(st.st_size) if st else 0
    total = 0
    try:
        for child in p.rglob("*"):
            if child.is_file() or child.is_symlink():
                st = _safe_stat(child)
                if st:
                    total += int(st.st_size)
    except OSError:
        pass
    return total


def _list_entries(cache: Path) -> list[tuple[Path, int, float]]:
    """Return ``(path, size_bytes, oldest_mtime)`` for every direct entry of ``cache``.

    For files, ``oldest_mtime`` is the file's mtime.  For directories, it
    is the minimum mtime of any file inside (a directory mtime changes
    on every rename, so it is useless as "oldest" for a cache directory).
    """

    if not cache.exists() or not cache.is_dir():
        return []
    out: list[tuple[Path, int, float]] = []
    for child in cache.iterdir():
        st = _safe_stat(child)
        if st is None:
            continue
        if child.is_file() or child.is_symlink():
            out.append((child, int(st.st_size), float(st.st_mtime)))
            continue
        # Directory: descend.
        try:
            mtime = min(
                (float(c.stat().st_mtime) for c in child.rglob("*") if c.is_file()),
                default=float(st.st_mtime),
            )
        except (OSError, ValueError):
            mtime = float(st.st_mtime)
        out.append((child, _size_of(child), mtime))
    return out


def _prune_cache(
    name: str,
    path: str | Path,
    cap_bytes: int,
    *,
    mode: RetainMode,
) -> CacheReport:
    if cap_bytes is None or cap_bytes <= 0:
        # Cap of zero or negative means "no cap"; record the no-op.
        return CacheReport(
            name=name,
            path=str(path),
            cap_bytes=cap_bytes or 0,
            initial_bytes=_size_of(Path(path)) if Path(path).exists() else 0,
            final_bytes=_size_of(Path(path)) if Path(path).exists() else 0,
            removed_count=0,
            removed_bytes=0,
        )
    cache = Path(path).expanduser()
    if not cache.exists() or not cache.is_dir():
        return CacheReport(
            name=name,
            path=str(cache),
            cap_bytes=cap_bytes,
            initial_bytes=0,
            final_bytes=0,
            removed_count=0,
            removed_bytes=0,
        )
    entries = _list_entries(cache)
    initial = sum(size for _, size, _ in entries)
    report = CacheReport(
        name=name,
        path=str(cache),
        cap_bytes=cap_bytes,
        initial_bytes=initial,
        final_bytes=initial,
        removed_count=0,
        removed_bytes=0,
    )
    if initial <= cap_bytes:
        return report
    # Sort by oldest first; ties broken by largest-first so the sweep
    # converges in fewer iterations.
    entries.sort(key=lambda t: (t[2], -t[1]))
    current = initial
    for child, size, _ in entries:
        if current <= cap_bytes:
            break
        entry_report = CacheEntryReport(path=str(child), size_bytes=size, state="kept")
        if mode.dry_run:
            entry_report.state = "removed"
            report.removed_count += 1
            report.removed_bytes += size
            current -= size
            report.entries.append(entry_report)
            continue
        try:
            if child.is_dir() and not child.is_symlink():
                import shutil

                shutil.rmtree(child, ignore_errors=False)
            else:
                child.unlink()
        except OSError as exc:
            entry_report.state = "error"
            entry_report.reason = str(exc)
            report.entries.append(entry_report)
            continue
        entry_report.state = "removed"
        report.removed_count += 1
        report.removed_bytes += size
        current -= size
        report.entries.append(entry_report)
    report.final_bytes = max(0, current)
    return report


def prune_caches(
    cache_paths: Mapping[str, str] | Iterable[tuple[str, str]],
    *,
    config: DiskRetentionConfig | None = None,
    mode: RetainMode | None = None,
) -> list[CacheReport]:
    """Sweep every cache in ``cache_paths`` (a name→path map or pairs)."""

    cfg = config or DEFAULT_CONFIG
    if mode is None:
        mode = RetainMode.dry()
    if isinstance(cache_paths, Mapping):
        items: Iterable[tuple[str, str]] = cache_paths.items()
    else:
        items = cache_paths
    reports: list[CacheReport] = []
    for name, path in items:
        cap = cfg.cache_max_bytes_for(name)
        reports.append(_prune_cache(name, path, cap, mode=mode))
    return reports


def prune_caches_report(
    cache_paths: Mapping[str, str] | Iterable[tuple[str, str]],
    *,
    config: DiskRetentionConfig | None = None,
    mode: RetainMode | None = None,
) -> dict[str, Any]:
    """Same as :func:`prune_caches` but returns a JSON-serialisable summary."""

    reports = prune_caches(cache_paths, config=config, mode=mode)
    return {
        "schema_version": 1,
        "mode": "apply" if not (mode or RetainMode.dry()).dry_run else "dry-run",
        "totals": {
            "caches": len(reports),
            "removed_bytes": sum(r.removed_bytes for r in reports),
        },
        "caches": [r.to_dict() for r in reports],
    }


def write_report(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Persist a report payload as JSON."""

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


__all__ = [
    "CacheEntryReport",
    "CacheReport",
    "prune_caches",
    "prune_caches_report",
    "write_report",
]
