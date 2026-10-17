"""Lightweight process-memory introspection for the web process.

The web service exposes its RSS through ``GET /api/meta`` so an Overseer
tick (or any outside observer) can see the same process from the same
place they already poll.  Reading ``/proc/self/status`` avoids the
``psutil`` dependency; the format has been stable since Linux 2.6.

Two surfaces are provided:

* ``read_rss_bytes()`` returns the current RSS or ``None`` when the
  kernel does not expose ``/proc/self/status`` (non-Linux).
* ``memory_snapshot()`` returns a small dict that the server merges into
  ``/api/meta`` -- including the configured threshold, so callers can
  reason about how close the process is to the warning line.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 500 MiB default.  The fleet gateway started failing on a ~25 MB /api/queue
# response before the process reached 5.6 GB RSS, so 500 MiB leaves plenty
# of headroom for normal workloads while still catching a runaway cache
# long before the host OOM-killer fires.
_DEFAULT_RSS_WARN_BYTES = 500 * 1024 * 1024
_ENV_RSS_WARN_BYTES = "LH_HARNESS_WEB_RSS_WARN_BYTES"

# Module-level cache so each /api/meta poll does not re-parse the file.
# The value is invalidated at most every ``_CACHE_TTL_SECONDS`` so a
# sudden spike is visible within a second.
_CACHE_TTL_SECONDS = 1.0
_cache_lock = threading.Lock()
_cached_value: int | None = None
_cached_at: float = 0.0

# Edge-trigger bookkeeping: only fire the warning the first time the RSS
# crosses the threshold in a given run.  ``_warned_above`` holds the last
# value the warning fired at so we re-arm if the process drops and climbs
# back, but never spam the log while RSS stays above the line.
_warned_above: float | None = None


def _read_status_rss() -> int | None:
    """Return RSS in bytes from ``/proc/self/status`` or ``None`` if absent.

    The file ships with every supported CPython target on Linux; on macOS
    or other test platforms the function returns ``None`` so callers can
    degrade gracefully instead of crashing the handshake.
    """

    try:
        text = Path("/proc/self/status").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.startswith("VmRSS:"):
            continue
        parts = line.split()
        if len(parts) < 2:
            return None
        try:
            return int(parts[1]) * 1024  # kB -> bytes
        except ValueError:
            return None
    return None


def read_rss_bytes(*, force: bool = False) -> int | None:
    """Return the process RSS in bytes, cached for ``_CACHE_TTL_SECONDS``.

    Pass ``force=True`` to bypass the cache (used by the regression test
    so it can measure growth across repeated polls).  Returns ``None`` on
    non-Linux platforms.
    """

    global _cached_value, _cached_at
    import time

    if not force:
        with _cache_lock:
            if _cached_value is not None and (time.monotonic() - _cached_at) < _CACHE_TTL_SECONDS:
                return _cached_value
    raw = _read_status_rss()
    if raw is None:
        return None
    with _cache_lock:
        _cached_value = raw
        _cached_at = time.monotonic()
    return raw


def reset_cache_for_tests() -> None:
    """Drop the module-level cache.  Tests call this between probes."""

    global _cached_value, _cached_at, _warned_above
    with _cache_lock:
        _cached_value = None
        _cached_at = 0.0
        _warned_above = None


def warn_threshold_bytes() -> int:
    """Return the configured warning threshold in bytes.

    Reads ``LH_HARNESS_WEB_RSS_WARN_BYTES`` from the environment so an
    operator can lower the threshold on staging or raise it on a host
    that runs the web process with extra headroom.  A missing, empty, or
    unparsable value falls back to ``_DEFAULT_RSS_WARN_BYTES``; the
    configured value is rejected when it would be useless (zero or
    negative, or above 1 TiB).
    """

    raw = os.environ.get(_ENV_RSS_WARN_BYTES, "").strip()
    if not raw:
        return _DEFAULT_RSS_WARN_BYTES
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_RSS_WARN_BYTES
    if not 1 <= value <= 1024 * 1024 * 1024 * 1024:
        return _DEFAULT_RSS_WARN_BYTES
    return value


def check_and_warn(rss: int) -> bool:
    """Log a warning the first time RSS crosses the threshold.

    Returns ``True`` when the warning fired on this call so tests can
    assert the edge-trigger behavior.  Re-fires if RSS drops by at least
    10% below the threshold and climbs back, so a one-shot blip does not
    silence a real leak.
    """

    global _warned_above
    threshold = warn_threshold_bytes()
    if rss >= threshold:
        with _cache_lock:
            should_warn = _warned_above is None or rss >= _warned_above * 1.10
            if should_warn:
                _warned_above = float(rss)
        if should_warn:
            logger.warning(
                "lh-harness web RSS %d bytes >= warning threshold %d bytes; "
                "consider a restart or check /api/meta for rss_bytes",
                rss,
                threshold,
            )
            return True
        return False
    # Below threshold: arm the next warning.
    with _cache_lock:
        if _warned_above is not None and rss < _warned_above * 0.90:
            _warned_above = None
    return False


def memory_snapshot(*, force: bool = False) -> dict[str, Any]:
    """Return the dict merged into ``/api/meta`` under ``memory``.

    Shape::

        {
            "rss_bytes": int,        # current RSS, or None on non-Linux
            "rss_human": str,        # e.g. "123.4 MiB"
            "threshold_bytes": int,  # warning threshold
            "threshold_human": str,  # e.g. "500.0 MiB"
            "over_threshold": bool,  # rss_bytes >= threshold_bytes (False when rss unknown)
        }
    """

    rss = read_rss_bytes(force=force)
    threshold = warn_threshold_bytes()
    snapshot: dict[str, Any] = {
        "rss_bytes": rss,
        "rss_human": _humanize_bytes(rss) if rss is not None else None,
        "threshold_bytes": threshold,
        "threshold_human": _humanize_bytes(threshold),
        "over_threshold": bool(rss is not None and rss >= threshold),
    }
    if rss is not None:
        check_and_warn(rss)
    return snapshot


def _humanize_bytes(value: int | None) -> str | None:
    if value is None:
        return None
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(value)
    idx = 0
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.1f} {units[idx]}"