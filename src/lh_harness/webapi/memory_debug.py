"""LHH-WEB-LEAK-FIX c3: opt-in live memory measurement for the web process.

Ticket ``easybutt0n-17``.  The CT110 web process grows from ~0.8 GB RSS at
start to >3 GB within ~70 min and is then killed by an RSS guard.  Slices c1
(ticket LHH#175) and the runs-scan cache (ticket LHH#179) did not stop it,
and c2a (ticket LHH#178) found no off-node reproduction.  This module adds a
LIVE, opt-in measurement so an operator can see WHAT grows on the node where
it grows -- without restarting the process.

Everything here is gated on the ``LH_HARNESS_MEM_DEBUG=1`` environment flag:

* :func:`maybe_start_tracemalloc` starts ``tracemalloc`` with 25 frames of
  traceback depth when the flag is on, and does nothing when it is off.
* The ``/api/debug/memory`` family of routes 404s (not 403) when the flag is
  off, so the routes are invisible unless an operator explicitly opted in.

The measurement itself is bounded on purpose:

* the traceback depth in the JSON payload is 6 frames;
* the top-N lists are 30 allocation groups and 15 gc type buckets;
* snapshots are held IN MEMORY ONLY and at most 3 of them are retained.

No snapshot is ever written to disk: this is a diagnostic, not a durable
artefact, and a 3 GB process must not grow further because of its own
diagnostics.
"""

from __future__ import annotations

import gc
import os
import threading
import time
import tracemalloc
from typing import Any

# The opt-in flag.  ``1`` is the only value that enables the feature; any
# other value (including ``0``, ``""`` and unset) leaves the web process
# completely untouched.
MEM_DEBUG_ENV = "LH_HARNESS_MEM_DEBUG"

# ``tracemalloc.start(n)`` frame depth.  25 frames is deep enough to name the
# allocating call site in this codebase and is the depth the ticket asks for.
TRACEMALLOC_FRAMES = 25

# The traceback depth rendered into the JSON payload for each allocation
# group.  Deeper than this is unreadable in a terminal and expensive to build.
REPORT_TRACEBACK_DEPTH = 6

# Top-N limits for the JSON payload.
TOP_ALLOCATION_GROUPS = 30
TOP_GC_TYPES = 15

# How many tracemalloc snapshots are retained in memory.  The diff endpoint
# compares the LATEST TWO, so 3 is enough to compare any two of the last
# three while bounding the memory the diagnostics themselves consume.
MAX_SNAPSHOTS = 3

# The top-N growth entries returned by the diff endpoint.
TOP_DIFF_GROUPS = 30


def memory_debug_enabled() -> bool:
    """True when the operator opted in via ``LH_HARNESS_MEM_DEBUG=1``.

    The value is read on EVERY call (not at import time) so a test -- or an
    operator who sets the knob before starting the process -- sees the
    current state.  Only the exact string ``"1"`` enables the feature: any
    other truthy-looking value is treated as "off" rather than guessed at.
    """

    return (os.environ.get(MEM_DEBUG_ENV) or "").strip() == "1"


def maybe_start_tracemalloc() -> bool:
    """Start ``tracemalloc`` when the debug flag is on.  Returns True if it ran.

    This is the D1 startup hook.  It is a NO-OP unless the flag is set, so
    the default web process pays no tracing overhead at all.
    """

    if not memory_debug_enabled():
        return False
    if tracemalloc.is_tracing():
        # Somebody else (a test, or an embedding harness) already started
        # tracing.  Leave it alone: restarting it would discard whatever the
        # existing tracer has already attributed.
        return False
    tracemalloc.start(TRACEMALLOC_FRAMES)
    return True


def read_rss_bytes() -> int | None:
    """This process's current resident set size in bytes, or ``None``.

    Reads ``/proc/self/status`` ``VmRSS``.  ``None`` means "unavailable",
    which is distinct from ``0`` (which would read as "no memory in use").
    """

    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _format_traceback(traceback: Any) -> list[str]:
    """Render one tracemalloc traceback as a list of ``file:line`` strings.

    The depth is bounded by :data:`REPORT_TRACEBACK_DEPTH`.  Only the
    ``file:line`` prefix of each frame is rendered -- the source echo that
    ``traceback.format()`` would produce reads whole source files into the
    process being measured, which is exactly the kind of self-inflicted
    growth this endpoint exists to avoid.
    """

    frames: list[str] = []
    for frame in list(traceback)[:REPORT_TRACEBACK_DEPTH]:
        filename = getattr(frame, "filename", "?")
        lineno = getattr(frame, "lineno", 0)
        frames.append(f"{filename}:{lineno}")
    return frames


def _allocation_groups(snapshot: tracemalloc.Snapshot, limit: int) -> list[dict[str, Any]]:
    """The ``limit`` largest allocation groups of ``snapshot``, by size.

    The groups are the snapshot's own ``statistics('lineno')`` groups, so
    every allocation made from the same source line is one group with a
    count.  Each group carries its size, its count and a depth-bounded
    traceback.
    """

    groups: list[dict[str, Any]] = []
    for stat in snapshot.statistics("lineno")[:limit]:
        groups.append(
            {
                "size_bytes": stat.size,
                "count": stat.count,
                "traceback": _format_traceback(stat.traceback),
                "location": f"{stat.traceback[0].filename}:{stat.traceback[0].lineno}",
            }
        )
    return groups


def _gc_stats() -> dict[str, Any]:
    """GC counters plus the tracked-object count bucketed by type.

    ``gc.get_count()`` is the per-generation collection counter.  The object
    census buckets ``gc.get_objects()`` by type name and keeps the 15
    largest buckets, so the payload stays small no matter how many objects
    the process tracks.
    """

    buckets: dict[str, int] = {}
    for obj in gc.get_objects():
        name = type(obj).__name__
        buckets[name] = buckets.get(name, 0) + 1
    top_types = sorted(buckets.items(), key=lambda item: (-item[1], item[0]))[:TOP_GC_TYPES]
    return {
        "count": list(gc.get_count()),
        "tracked_objects": sum(buckets.values()),
        "type_buckets": [{"type": name, "count": count} for name, count in top_types],
    }


class MemorySnapshotStore:
    """In-memory store of at most :data:`MAX_SNAPSHOTS` tracemalloc snapshots.

    The store is deliberately process-local and never persists to disk: the
    snapshots are large (they describe every tracked allocation) and the
    process being measured is the one that is short of memory.
    """

    def __init__(self, max_snapshots: int = MAX_SNAPSHOTS) -> None:
        self._lock = threading.Lock()
        self._snapshots: list[tuple[float, tracemalloc.Snapshot]] = []
        self._max = max(2, int(max_snapshots))

    def add(self, snapshot: tracemalloc.Snapshot, *, taken_at: float | None = None) -> int:
        """Store ``snapshot``, evicting the oldest beyond the cap.

        Returns the number of snapshots currently held.
        """

        stamp = time.time() if taken_at is None else taken_at
        with self._lock:
            self._snapshots.append((stamp, snapshot))
            while len(self._snapshots) > self._max:
                self._snapshots.pop(0)
            return len(self._snapshots)

    def count(self) -> int:
        with self._lock:
            return len(self._snapshots)

    def latest_two(self) -> list[tuple[float, tracemalloc.Snapshot]]:
        """The two most recent snapshots, oldest first."""

        with self._lock:
            return list(self._snapshots[-2:])


def diff_snapshots(
    older: tracemalloc.Snapshot,
    newer: tracemalloc.Snapshot,
    *,
    limit: int = TOP_DIFF_GROUPS,
) -> list[dict[str, Any]]:
    """The ``limit`` largest GROWTH entries between two snapshots.

    "Growth" means a positive size difference: an allocation site that got
    bigger between the two snapshots.  Shrinking sites are reported with a
    negative ``size_diff`` too, but the list is ordered by growth so the
    caller sees the sites that grew first.  Comparing by ``lineno`` keeps the
    groups on the same granularity as the per-endpoint payload.
    """

    results = snapshot_diff_stats(older, newer)
    entries: list[dict[str, Any]] = []
    for stat in results[:limit]:
        entries.append(
            {
                "size_diff": stat.size_diff,
                "count_diff": stat.count_diff,
                "traceback": _format_traceback(stat.traceback),
                "location": f"{stat.traceback[0].filename}:{stat.traceback[0].lineno}",
            }
        )
    return entries


def snapshot_diff_stats(
    older: tracemalloc.Snapshot,
    newer: tracemalloc.Snapshot,
) -> list[Any]:
    """The raw ``compare_to`` statistics, ordered by size growth (descending).

    ``compare_to`` sorts by size difference already; the explicit sort keeps
    the ordering stable regardless of the CPython version's tie-breaking.
    """

    stats = newer.compare_to(older, "lineno")
    return sorted(stats, key=lambda stat: (-stat.size_diff, stat.count_diff))


def cache_sizes(registry: Any, snapshot_cache: Any = None) -> dict[str, Any]:
    """The sizes of the known in-memory caches of the web process.

    Covers the caches the ticket names explicitly -- the ``DashboardState``
    runs-scan cache -- plus the other per-run caches found in
    ``dashboard/state.py`` and ``webapi/*.py``: the per-run state registry
    and the per-run snapshot cache.

    Every entry is read defensively: a cache that is absent, empty or
    shaped differently than expected contributes ``0`` rather than failing
    the whole endpoint.
    """

    sizes: dict[str, Any] = {}

    # The runs-scan cache: one (timestamp, run list) pair per DashboardState.
    # The registry holds one state per run id, so the cache length is the
    # number of runs held in the scanned listing.
    #
    # The base state is counted too: it is only registered in ``_states``
    # when it has a run id, and the runs listing is scanned on the BASE
    # state, so skipping it would report a cache length of zero on exactly
    # the deployment this endpoint exists to measure.
    scan_entries = 0
    states = getattr(registry, "_states", {}) or {}
    candidates: list[Any] = []
    try:
        candidates.extend(list(states.values()))
    except Exception:
        pass
    base_state = getattr(registry, "base_state", None)
    if base_state is not None and all(base_state is not item for item in candidates):
        candidates.append(base_state)
    try:
        for state in candidates:
            cached = getattr(state, "_runs_scan_cache", None)
            if cached is not None:
                runs = cached[1] if isinstance(cached, tuple) and len(cached) > 1 else cached
                try:
                    scan_entries += len(runs)
                except TypeError:
                    pass
    except Exception:
        pass
    sizes["dashboard_runs_scan_cache_len"] = scan_entries

    # The per-run state registry: one DashboardState per run id.
    sizes["state_registry_entries"] = len(states)

    # The per-run snapshot cache.
    if snapshot_cache is not None:
        data = getattr(snapshot_cache, "_data", None)
        sizes["snapshot_cache_entries"] = len(data) if data is not None else 0
    else:
        sizes["snapshot_cache_entries"] = 0

    return sizes
