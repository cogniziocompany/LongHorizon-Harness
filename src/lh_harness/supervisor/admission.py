"""Node-wide run admission at the SHARED supervisor boundary.

LHH-CT110-MEMORY-ADMISSION, operator amendment (2026-10-26, after the
review of PR #169): the node-wide reservation must live where BOTH launch
paths converge -- ``RunSupervisor.create_run`` -- not in the queue
launcher.  PR #169 counted only the runs the launcher had already
published to ``_active_runs`` (its own supervision window), so a POST
/api/runs launch and a queue launch could still admit into the same last
slot, and a run in its startup window that the launcher had not yet
observed was invisible to the check.  The OOM incident that motivated this
was exactly that shape: the per-lane queue_capacity rows summed to far
more than the fleet node record's maxConcurrentRuns, the launcher
admitted per lane only, so many more runs ran at once than the node could
hold, the OOM killer took the service, and every launched run ended
"worker disappeared without a final report".

The reservation protocol implemented here:

1. ``reserve_startup`` is the ONE admission gate.  Every launch -- the
   queue launcher's ``_launch`` and the API's POST /api/runs, both of
   which call ``RunSupervisor.create_run`` -- must hold a PENDING STARTUP
   reservation before any worker process is spawned.  The reservation is
   taken under a lock, and the gate counts reservations + running +
   starting runs across ALL lanes, so two launch paths racing for the
   last slot cannot both win.
2. The reservation is released when the run is published into the live
   supervision window (it then counts as running/starting like any other
   run), when the start fails, or when the caller aborts before
   publishing -- never leaked.  A leaked reservation is also aged out by
   ``reconcile`` (the startup window has a bounded wall-clock horizon).
3. ``memory_headroom``: the same gate reads MemAvailable (bounded by the
   cgroup limit when present) and refuses when
   ``MemAvailable - LH_HARNESS_RUN_STARTUP_MB < LH_HARNESS_MEM_FLOOR_MB``.
4. ``reconcile`` rebuilds the count from live processes and run dirs at
   supervisor construction (service start), so a restart after a crash
   starts from the true set.  When the node is ALREADY over
   ``LH_HARNESS_NODE_MAX_RUNS`` at start, nothing is killed and nothing
   is admitted until the count drops below the cap (deliverable D).

The three limits live in the settings store
(``settings_store.CATALOG``), next to the lane caps, so an operator can
change them at runtime; a missing or unreadable store keeps the
documented defaults (5 / 600 / 1024).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ..settings_store import CATALOG_BY_NAME

# The exact settings-store names (settings_store.CATALOG).  The defaults
# live here, next to the gate that enforces them.
SETTING_NODE_MAX_RUNS = "LH_HARNESS_NODE_MAX_RUNS"
SETTING_RUN_STARTUP_MB = "LH_HARNESS_RUN_STARTUP_MB"
SETTING_MEM_FLOOR_MB = "LH_HARNESS_MEM_FLOOR_MB"

DEFAULT_NODE_MAX_RUNS = 5
DEFAULT_RUN_STARTUP_MB = 600
DEFAULT_MEM_FLOOR_MB = 1024

# Skip reason recorded by the memory gate (deliverable B).  Callers append
# their own numbers after the marker; greps key on the marker alone.
MEMORY_HEADROOM_SKIP_REASON = "memory_headroom"

# The crash-naming reason the supervisor's own reconciliation writes
# (supervisor/service.py, "worker disappeared without a final report");
# the queue launcher reuses the same sentence for orphaned launched
# entries so one grep finds the whole incident class.
WORKER_DISAPPEARED_REASON = "worker disappeared without a final report"

# Lifecycle statuses that hold a node slot: the startup window (creating,
# starting) is where RSS grows fastest, so it counts exactly like running.
STARTING_STATUSES = frozenset({"creating", "starting"})
ACTIVE_LIFECYCLE_STATUSES = frozenset(
    {"creating", "starting", "running", "waiting_approval", "stopping"}
)

# A reservation whose run was never published is stale after this many
# seconds (a worker start either publishes within seconds or the launch
# failed and released); reconcile drops stale ones so a crashed launch
# path cannot hold a slot forever.
_STARTUP_RESERVATION_TTL_SECONDS = 900.0


def _coerce_int(raw: Any, default: int, *, minimum: int) -> int:
    """Settings value -> int, falling back to ``default``.

    Mirrors the harness-wide rule that a bad settings row must never take
    the gate down: missing, blank, unparseable, bool, or below-minimum
    values keep the default.
    """

    if isinstance(raw, bool) or raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return default
    return value


def admission_settings_from_values(
    values: Mapping[str, Any] | None,
) -> dict[str, int]:
    """Resolve the three admission limits from a settings value map.

    Tests pass a fake map; production passes the settings store's
    ``values()`` (or the launcher's queue-capacity store).  Keys are the
    settings-store names above.
    """

    values = values or {}
    return {
        "node_max": _coerce_int(values.get(SETTING_NODE_MAX_RUNS), DEFAULT_NODE_MAX_RUNS, minimum=1),
        "startup_mb": _coerce_int(values.get(SETTING_RUN_STARTUP_MB), DEFAULT_RUN_STARTUP_MB, minimum=0),
        "mem_floor_mb": _coerce_int(values.get(SETTING_MEM_FLOOR_MB), DEFAULT_MEM_FLOOR_MB, minimum=0),
    }


def read_mem_available_mb(
    proc_meminfo: str | Path = "/proc/meminfo",
    cgroup_memory_max: str | Path = "/sys/fs/cgroup/memory.max",
    *,
    follow_self_cgroup: bool = True,
) -> float | None:
    """MemAvailable in MB, bounded by the cgroup limit when present.

    Reads ``MemAvailable`` from procfs.  When this process runs inside a
    cgroup v2 with a finite ``memory.max`` (CT110's LXC), the host-wide
    MemAvailable can overstate what the container may actually touch, so
    the smaller of the two is the honest headroom.  When the given knob
    does not exist, ``follow_self_cgroup`` (default on) resolves this
    process's own cgroup via /proc/self/cgroup and reads ITS ``memory.max``;
    tests pass explicit fake paths with the flag off.  ``"max"`` on the
    knob (and v1's ``-1``) means no limit and is ignored.  Any read
    failure returns None; the caller decides whether that blocks or
    passes, never this helper.
    """

    available: float | None = None
    try:
        text = Path(proc_meminfo).read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) >= 2:
                    available = int(parts[1]) / 1024.0
                break
    except (OSError, ValueError):
        available = None
    limit_mb: float | None = None
    knob = Path(cgroup_memory_max)
    if follow_self_cgroup and not knob.exists():
        # The default knob is the unified root; inside a delegated
        # container this process lives deeper in the tree, so follow
        # /proc/self/cgroup to OUR OWN limit before giving up on the
        # cgroup side of the bound.
        try:
            from ..worker_isolation import CGROUP_ROOT, own_cgroup_path

            path = own_cgroup_path()
            if path:
                knob = CGROUP_ROOT / path.strip("/") / "memory.max"
        except Exception:  # noqa: BLE001 - no cgroup info: MemAvailable alone stands
            knob = Path(cgroup_memory_max)
    try:
        raw = knob.read_text(encoding="utf-8").strip()
        if raw and raw not in ("max", "-1"):
            limit_mb = int(raw) / (1024.0 * 1024.0)
    except (OSError, ValueError):
        limit_mb = None
    if available is None:
        return limit_mb
    if limit_mb is not None:
        return min(available, limit_mb)
    return available


class AdmissionRefused(RuntimeError):
    """Raised by ``reserve_startup`` when the node must not admit a run.

    ``reason`` is the full operator-facing refusal (``blocked_reason``
    plus the numbers); ``blocked_reason`` is the short gate name surfaced
    in /api/meta (``node_max`` or ``memory_headroom``).
    """

    def __init__(self, blocked_reason: str, reason: str) -> None:
        super().__init__(reason)
        self.blocked_reason = blocked_reason
        self.reason = reason


class NodeAdmission:
    """The shared node-wide admission boundary for ONE supervisor.

    Owns the PENDING STARTUP reservation table.  ``reserve_startup`` is
    called by ``RunSupervisor.create_run`` (both launch paths) under this
    object's lock, so one decision is made per launch and the count is
    cross-lane by construction.  The object also holds the /api/meta
    snapshot: exactly ``node_max``, ``running``, ``starting``,
    ``mem_available_mb``, ``blocked_reason``.
    """

    def __init__(
        self,
        *,
        settings_reader: Callable[[], Mapping[str, Any] | None] | None = None,
        meminfo_probe: Callable[[], float | None] | None = None,
    ) -> None:
        # Both readers are fail-open seams: production resolves the live
        # settings store and /proc/meminfo; tests inject fakes (F).
        self._settings_reader = settings_reader
        self._meminfo_probe = meminfo_probe
        self._lock = threading.Lock()
        self._reservations: dict[str, float] = {}
        # The published live set, refreshed by ``publish``/``reconcile``:
        # run_id -> lifecycle status (creating/starting/running/...).
        self._live: dict[str, str] = {}
        self._snapshot: dict[str, Any] = {
            "node_max": DEFAULT_NODE_MAX_RUNS,
            "running": 0,
            "starting": 0,
            "mem_available_mb": None,
            "blocked_reason": None,
        }

    # ----------------------------------------------------------- settings --

    def settings(self) -> dict[str, int]:
        """The three limits, resolved fresh on every decision.

        A settings-store read failure keeps the documented defaults; the
        gate never takes the launch path down over one bad row.
        """

        values: Mapping[str, Any] | None = None
        if self._settings_reader is not None:
            try:
                values = self._settings_reader()
            except Exception:  # noqa: BLE001 - unreadable store keeps defaults
                values = None
        return admission_settings_from_values(values)

    def mem_available_mb(self) -> float | None:
        """MemAvailable in MB via the probe seam, else the real read."""

        if self._meminfo_probe is not None:
            try:
                return self._meminfo_probe()
            except Exception:  # noqa: BLE001 - a broken probe must not crash a launch
                return None
        return read_mem_available_mb()

    def memory_headroom_reason(
        self, settings: dict[str, int], available: float | None
    ) -> str | None:
        """Deliverable B: refuse when MemAvailable - startup allowance < floor.

        Returns the ``memory_headroom`` refusal or None when there is
        headroom.  A MISSING reading fails OPEN: the node cap still
        bounds admission, and a container without meminfo must not have
        its queue frozen forever.
        """

        if available is None:
            return None
        startup = int(settings.get("startup_mb", DEFAULT_RUN_STARTUP_MB))
        floor = int(settings.get("mem_floor_mb", DEFAULT_MEM_FLOOR_MB))
        if available - startup < floor:
            return (
                f"{MEMORY_HEADROOM_SKIP_REASON}: MemAvailable {available:.0f} MB "
                f"minus startup allowance {startup} MB is below the "
                f"{floor} MB floor"
            )
        return None

    # ------------------------------------------------------------- the gate --

    def _counts_locked(self) -> tuple[int, int]:
        """(running, starting) under the lock.

        Pending startup reservations count as STARTING: a run being born
        holds a slot exactly like one whose worker is already up, which
        is what the published-run-only check of #169 missed.
        """

        running = 0
        starting = len(self._reservations)
        for status in self._live.values():
            if status in STARTING_STATUSES:
                starting += 1
            else:
                running += 1
        return running, starting

    def _refresh_snapshot_locked(
        self,
        settings: dict[str, int],
        available: float | None,
        blocked_reason: str | None,
    ) -> None:
        running, starting = self._counts_locked()
        self._snapshot = {
            "node_max": int(settings.get("node_max", DEFAULT_NODE_MAX_RUNS)),
            "running": running,
            "starting": starting,
            "mem_available_mb": round(available, 1) if available is not None else None,
            "blocked_reason": blocked_reason,
        }

    def reserve_startup(
        self,
        run_id: str,
        *,
        _now: Callable[[], float] = time.time,
    ) -> None:
        """Acquire the PENDING STARTUP reservation for ``run_id``.

        THE admission decision (one per launch, under the lock): refuse
        with :class:`AdmissionRefused` when reservations + running +
        starting has reached ``LH_HARNESS_NODE_MAX_RUNS``, or when the
        memory headroom gate refuses.  The caller must hold the returned
        reservation from BEFORE the worker is spawned until ``publish``
        (or release it via ``release_startup`` on a failed start), so the
        startup window can never admit a peer into the same slot.
        """

        settings = self.settings()
        available = self.mem_available_mb()
        memory_reason = self.memory_headroom_reason(settings, available)
        with self._lock:
            self._drop_stale_locked(_now())
            running, starting = self._counts_locked()
            node_max = int(settings.get("node_max", DEFAULT_NODE_MAX_RUNS))
            if running + starting >= node_max:
                self._refresh_snapshot_locked(
                    settings, available, "node_max"
                )
                raise AdmissionRefused(
                    "node_max",
                    f"node at capacity: {running + starting} running+starting "
                    f"runs, node max {node_max}",
                )
            if memory_reason is not None:
                self._refresh_snapshot_locked(
                    settings, available, MEMORY_HEADROOM_SKIP_REASON
                )
                raise AdmissionRefused(MEMORY_HEADROOM_SKIP_REASON, memory_reason)
            self._reservations[run_id] = _now()
            # A successful decision is also the freshest /api/meta view.
            self._refresh_snapshot_locked(settings, available, None)

    def release_startup(self, run_id: str) -> None:
        """Release the PENDING STARTUP reservation of ``run_id``.

        Called when a start fails or the caller aborts before publishing
        (``_create_run_once``'s exception paths), and idempotently after
        ``publish``.  A released reservation frees its slot immediately.
        """

        with self._lock:
            self._reservations.pop(run_id, None)
            self._recount_locked()

    def publish(self, run_id: str, status: str) -> None:
        """Promote a reserved run into the published live set.

        The run now counts as running/starting like any other supervised
        run; its startup reservation is dropped (idempotently) so it does
        not count twice.
        """

        with self._lock:
            self._reservations.pop(run_id, None)
            self._live[run_id] = str(status)
            self._recount_locked()

    def observe(self, run_id: str, status: str) -> None:
        """Record a lifecycle transition for an already published run."""

        with self._lock:
            self._live[run_id] = str(status)
            self._recount_locked()

    def forget(self, run_id: str) -> None:
        """Drop a run from the published set (worker exited or crashed).

        Releasing the admission count on worker exit/crash (deliverable C):
        the slot is free for the next launch the moment the run is gone.
        """

        with self._lock:
            self._reservations.pop(run_id, None)
            self._live.pop(run_id, None)
            self._recount_locked()

    def _recount_locked(self) -> None:
        """Refresh the snapshot's COUNTS after the live set changed.

        The counts in the /api/meta snapshot must never be stale: a run
        that was published, observed, or forgotten changes them at once.
        The node_max, the memory reading, and the blocked reason are left
        alone here -- they belong to the last admission DECISION, which
        only ``reserve_startup`` (and ``refresh_snapshot``) makes.
        """

        running, starting = self._counts_locked()
        self._snapshot["running"] = running
        self._snapshot["starting"] = starting

    def _drop_stale_locked(self, now: float) -> None:
        """Age out reservations whose launch never published.

        A crashed launch path (killed between reserve and publish) must
        not hold a slot forever: past the startup TTL the reservation is
        treated as released.  The normal release is the exception paths in
        ``_create_run_once``; this is the belt to their braces.
        """

        stale = [
            run_id
            for run_id, taken_at in self._reservations.items()
            if now - taken_at > _STARTUP_RESERVATION_TTL_SECONDS
        ]
        for run_id in stale:
            self._reservations.pop(run_id, None)

    # ------------------------------------------------------- reconciliation --

    def reconcile(
        self,
        live_runs: Mapping[str, str],
        *,
        _now: Callable[[], float] = time.time,
    ) -> None:
        """Rebuild the published set from live processes and run dirs.

        Called at service start (supervisor construction) before the
        first launch: ``live_runs`` is the authoritative run_id ->
        lifecycle status map the supervisor derives from its run
        directories (``list_run_items``), i.e. live processes and their
        durable status records.  Everything NOT in that map is gone
        (exited or crashed) and its slot is released; reservations are
        kept only while their run is within the startup TTL.
        """

        with self._lock:
            self._drop_stale_locked(_now())
            self._live = {
                run_id: str(status) for run_id, status in live_runs.items()
            }
            self._recount_locked()

    def reconcile_from_supervisor(self, supervisor: Any) -> None:
        """``reconcile`` using the supervisor's durable run-dir view.

        A separate entry point so the supervisor can reconcile at start
        with its OWN authoritative enumeration (live run dirs and their
        durable status records) without the caller hand-mapping statuses.

        The enumeration is ``list_run_summaries`` -- the durable
        ``status.json`` projection of every run dir -- and NOT
        ``list_run_items``.  ``list_run_items`` walks the full run
        boundary and RECONCILES each run's lifecycle, which has side
        effects (it parks terminal workspaces, runs git, may signal a
        worker).  Reconciling the admission count must be a pure READ:
        it runs at supervisor construction, and a side effect there would
        change the behaviour of every restart on the node.
        """

        live: dict[str, str] = {}
        try:
            for item in supervisor.list_run_summaries():
                run_id = str(item.get("id") or "")
                if run_id:
                    live[run_id] = str(item.get("status") or "unknown")
        except Exception:  # noqa: BLE001 - an unreadable runs root reconciles empty
            live = {}
        # Only runs that still hold a slot count; a terminal run recorded
        # in a run dir is not live and must not consume admission.
        live = {
            run_id: status
            for run_id, status in live.items()
            if status in ACTIVE_LIFECYCLE_STATUSES
        }
        self.reconcile(live)

    # ------------------------------------------------------------ reporting --

    def snapshot(self) -> dict[str, Any]:
        """The /api/meta ``admission`` object (deliverable D).

        Exactly five keys: node_max, running, starting, mem_available_mb,
        blocked_reason.  ``blocked_reason`` names the gate that refused
        the LAST admission decision (``node_max``/``memory_headroom``)
        or None while the boundary is admitting.
        """

        with self._lock:
            return dict(self._snapshot)

    def refresh_snapshot(self) -> dict[str, Any]:
        """Recompute the snapshot without admitting (idle-pass refresh).

        Lets /api/meta stay fresh while no launch is being attempted: the
        counts are recomputed from the reservation table and published
        set, and the memory reading is retaken.  ``blocked_reason`` keeps
        the last decision's value when the node is full, else None.
        """

        settings = self.settings()
        available = self.mem_available_mb()
        with self._lock:
            running, starting = self._counts_locked()
            node_max = int(settings.get("node_max", DEFAULT_NODE_MAX_RUNS))
            blocked = None
            if running + starting >= node_max:
                blocked = "node_max"
            else:
                memory_reason = self.memory_headroom_reason(settings, available)
                if memory_reason is not None:
                    blocked = MEMORY_HEADROOM_SKIP_REASON
            self._refresh_snapshot_locked(settings, available, blocked)
            return dict(self._snapshot)


def admission_settings_from_catalog_defaults() -> dict[str, int]:
    """The three limits resolved with the documented defaults.

    A convenience for callers that want the defaults without a store: the
    CATALOG rows exist for the admin screen; the enforced defaults are
    the constants in this module.
    """

    for name in (SETTING_NODE_MAX_RUNS, SETTING_RUN_STARTUP_MB, SETTING_MEM_FLOOR_MB):
        if name not in CATALOG_BY_NAME:
            raise KeyError(f"{name} missing from the settings catalog")
    return admission_settings_from_values(None)