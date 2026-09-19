"""Per-run worker memory isolation (TASKS 202 + 208).

A single run's worker used to share the ``lh-harness.service`` cgroup with
every other run.  One run's memory blowup therefore hit the *service* memcg
limit: the kernel OOM-killed the offending child, systemd failed the whole
unit, the service restarted, and every other live run died with "worker
disappeared without a final report".

This module gives each run's worker its own memory boundary.  Mechanisms,
in the order ``prepare_launch`` picks them:

- preferred: a per-episode child cgroup under the *service's own delegated
  cgroup subtree* (``Delegate=memory pids`` in ``packaging/lh-harness.service``).
  The worker code creates ``worker-<run>-<id>`` under its own cgroup, writes
  ``memory.max`` there, and the forked child moves itself in via a preexec.
  ``memory.max`` is an RESIDENT-memory cap (cgroup memory accounting): it
  counts pages actually charged, never reserved-but-untouched address space.

  TASK 208 mechanism choice and the reason for it (measured on CT110,
  2026-09-18, audited round_001): a transient ``systemd-run --scope`` with
  ``MemoryMax`` is the textbook mechanism, but on CT110 it is refused —
  there is no polkit daemon to authorize it (task 205 measured
  ``systemd-run --scope`` exiting rc=1 with "Failed to start transient scope
  unit: Access denied"), and there is no user manager, so ``systemd-run
  --user`` with lingering is impossible too.  The delegated subtree is the
  only mechanism the (unprivileged) harness user can use: ``Delegate=memory
  pids`` in the shipped unit makes the service's own cgroup directory
  writable by the service user, so worker code can create child cgroups and
  write ``memory.max`` directly, with no daemon conversation and no root.

- still supported: a transient systemd scope (``systemd-run --scope``)
  carrying a ``MemoryMax`` cap, for hosts where a manager is reachable and
  authorizes it.  ``MemoryMax`` is the same RSS bound (it is ``memory.max``
  behind the scenes); on CT110 this launch always fails and the flow falls
  through to the mechanisms above/below.

- last resort: *unbounded, log-only*.  There is deliberately NO rlimit
  fallback.  The pre-208 design applied an address-space rlimit cap
  (three gibibytes) to every worker, and on CT110 — where the scope path
  always fails, making the fallback the only path — that cap killed every
  episode: each episode's Claude Code child is Node 22, and V8 plus its
  thread pool RESERVE about 5.3 GiB of virtual address space (VmSize
  5.31 GiB) and have peaked at 9.27 GiB (VmPeak) while actually using a
  quarter of a gibibyte (VmRSS 0.26 GiB).  An address-space rlimit bounds
  VIRTUAL mappings, not resident memory, so it counts the reservation the
  child needs to exist and kills the episode before it starts.  The
  resident figure it was meant to protect — 0.26 GiB — is nowhere near it.
  RLIMIT_DATA would not fix this either: on modern kernels it counts V8's
  private anonymous mappings (verified 2026-09-18: a Node 22 child
  allocating 40x32 MiB resident fails under a 1 GiB data limit), so any
  data limit small enough to bound a blowup also kills legitimate
  reservations, and one large enough to admit them (measured 9.27 GiB peak
  plus headroom) exceeds CT110's physical RAM and protects nothing.  The
  fallback therefore only logs — loudly — that the worker is NOT
  memory-bounded, so no future reader believes the fleet is protected when
  it is not.  ``unbounded_plan`` below carries the same numbers.

The limit comes from (highest precedence first) the
``LH_HARNESS_WORKER_MEMORY_MAX`` environment override, the
``[run] worker_memory_max`` project-config key, and the 2G default —
sized from the measured 9.27 GiB VmPeak with headroom.  Note the
difference in kind from the pre-208 cap: this is an RSS bound, so the
~5.3 GiB address-space reservation is invisible to it and a real episode
(VmRSS 0.26 GiB) runs untouched, while a genuine resident blowup is
OOM-killed *inside the episode's own cgroup* — with ``OOMPolicy=continue``
in the unit, the service and every other live run keep running.

Every launch records which mechanism actually bounded the child (or that
none did) in the run's owner record, and a log line always states it.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Sized from RESIDENT measurements on CT110, 2026-09-18, because this value
# drives ``memory.max``, which is an RSS bound:
#   worst resident agent observed : VmRSS 0.29 GiB (claude, ps -eo rss)
#   live agents during a run      : VmRSS 0.25-0.26 GiB
#   CT110 physical RAM            : 6.0 GiB
# 2G leaves roughly seven times the observed resident high-water mark while
# staying WELL BELOW physical RAM, which is the whole point: a cgroup limit
# only isolates when it is reached BEFORE the box runs out. The previous 12G
# default was carried over from the 9.27 GiB VmPeak - an ADDRESS-SPACE figure,
# the very thing this module's docstring says must never drive this value - and
# on a 6 GiB box it can never fire: the kernel would OOM-kill globally first,
# which is exactly the whole-unit failure task 202 exists to prevent.
# Operators raise it via env/config for a workload that genuinely needs more.
DEFAULT_WORKER_MEMORY_MAX = "2G"
ENV_WORKER_MEMORY_MAX = "LH_HARNESS_WORKER_MEMORY_MAX"
CONFIG_KEY_WORKER_MEMORY_MAX = "worker_memory_max"
MEMORY_KILL_REASON_TEMPLATE = "memory limit exceeded ({limit})"

MECHANISM_SCOPE = "systemd-run-scope"
MECHANISM_CGROUP = "cgroup-subtree"
MECHANISM_UNBOUNDED = "unbounded-log-only"

CGROUP_ROOT = Path("/sys/fs/cgroup")

# systemd's own byte suffixes (IEC, binary multiples).  A bare value is bytes.
_SUFFIXES = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4, "p": 1024**5}
_LIMIT_RE = re.compile(r"^(?P<value>\d+)(?P<suffix>[kKmMgGtTpP])?$")

# systemd-run talks to a manager: the user manager when it exists (the
# service's own user), otherwise the system bus.  Neither marker present
# means the host is not systemd-managed; on CT110 the manager is reachable
# but refuses scope creation (no polkit daemon), so a successful probe here
# does not imply a working launch — the delegated cgroup mechanism is
# preferred exactly because it needs no manager at all.
_USER_MANAGER_MARKER = "/run/user/{uid}/systemd"
_SYSTEM_MANAGER_MARKERS = ("/run/systemd/system", "/run/dbus/system_bus_socket")

# Signatures systemd-run has been observed to write to stderr when it cannot
# create the scope (no bus, no authorization, ...).  These are logging
# diagnostics only: the fallback decision is structural (non-zero exit with
# no scope cgroup), because the exact wording varies across systemd builds
# and failure modes (e.g. "Failed to start transient scope unit: Access
# denied" matches none of these).
_SCOPE_FAILURE_SIGNATURES = (
    "failed to start transient service unit",
    "failed to start transient scope unit",
    "failed to connect to bus",
    "failed to allocate",
    "bus connection refused",
)


def memory_kill_reason(limit: str) -> str:
    """Exact failure reason recorded when a worker dies from a memory kill."""

    return MEMORY_KILL_REASON_TEMPLATE.format(limit=limit)


def is_memory_kill_reason(value: object) -> bool:
    return isinstance(value, str) and value.startswith("memory limit exceeded (")


def parse_memory_limit(value: object) -> str:
    """Validate a systemd-style byte size such as ``12G`` and return it.

    The string is stored and applied verbatim (systemd accepts the same
    suffixes, and so does ``memory.max``'s byte form via ``to_bytes``).
    """

    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not _LIMIT_RE.match(value.strip()):
        raise ValueError(
            "worker memory limit must be an integer byte count or a "
            "systemd byte size such as 512M, 12G or 1T"
        )
    return value.strip()


def to_bytes(value: object) -> int:
    """Convert a validated byte size into an integer byte count."""

    text = str(value).strip()
    match = _LIMIT_RE.match(text)
    if not match:
        raise ValueError(f"invalid memory limit: {text!r}")
    multiplier = _SUFFIXES.get(text[-1].lower(), 1)
    digits = text[:-1] if multiplier > 1 else text
    return int(digits) * multiplier


def resolve_memory_limit(
    *,
    explicit: object = None,
    env: object = None,
    config: object = None,
) -> str:
    """Effective limit: explicit argument, env override, config key, 2G."""

    for source in (explicit, env, config):
        if source is None:
            continue
        if isinstance(source, str) and not source.strip():
            continue
        return parse_memory_limit(source)
    return DEFAULT_WORKER_MEMORY_MAX


@dataclass(frozen=True)
class IsolationPlan:
    """One resolved launch: mechanism, effective argv, and its record."""

    mechanism: str
    limit: str
    command: list[str]
    preexec: Callable[[], None] | None
    unit: str = ""
    cgroup: str = ""

    def record(self) -> dict[str, Any]:
        return {
            "mechanism": self.mechanism,
            "limit": self.limit,
            "unit": self.unit,
            "cgroup": self.cgroup,
            "oom_kill_base": None,
        }


# --- delegated cgroup subtree (preferred mechanism; works on CT110) ---------

CGROUP_CONTROLLERS_NEEDED = ("memory", "pids")


def own_cgroup_path() -> str | None:
    """This process's cgroup v2 path, as written in ``/proc/self/cgroup``."""

    return _cgroup_of_pid(os.getpid())


def cgroup_subtree_writable(
    cgroup_path: str | None = None, *, root: Path | None = None
) -> bool:
    """Can this process create child cgroups and cap them (RSS bound)?

    True only when the service's own cgroup subtree is delegated to us: the
    directory is writable (``mkdir`` works), ``cgroup.subtree_control`` is
    writable (controllers can be enabled for children), and the memory
    controller is available in ``cgroup.controllers``.  That is exactly what
    ``Delegate=memory pids`` in ``packaging/lh-harness.service`` grants —
    without it (pre-deploy CT110, ad-hoc pytest runs) this returns False and
    the launch falls back to ``unbounded_plan``.
    """

    cgroup = cgroup_path if cgroup_path is not None else own_cgroup_path()
    if not cgroup:
        return False
    base = (root or CGROUP_ROOT) / cgroup.strip("/")
    try:
        if not base.is_dir():
            return False
        controllers = (base / "cgroup.controllers").read_text().split()
        if not all(name in controllers for name in CGROUP_CONTROLLERS_NEEDED):
            return False
    except OSError:
        return False
    return os.access(base, os.W_OK) and os.access(base / "cgroup.subtree_control", os.W_OK)


def _prepare_child_cgroup(run_id: str, limit: str, *, root: Path | None = None) -> str:
    """Create this episode's child cgroup under our own cgroup, cap its RSS.

    Returns the child's cgroup path (root-relative, leading slash kept, the
    same convention ``/proc/<pid>/cgroup`` uses).  ``memory.max`` is an RSS
    cap: pages actually charged, not address space reserved.
    """
    _attempt_relocation(root=root)
    # Determine the base cgroup: if we have relocated, use the parent (original) cgroup;
    # otherwise, use the current cgroup.
    if _WORKER_PARENT_CGROUP is not None:
        base_cgroup = _WORKER_PARENT_CGROUP
    else:
        base_cgroup = own_cgroup_path()
        if not base_cgroup:
            raise OSError("no cgroup v2 path for this process in /proc/self/cgroup")
    base = (root or CGROUP_ROOT) / base_cgroup.strip("/")
    controllers = (base / "cgroup.controllers").read_text().split()
    wanted = [name for name in CGROUP_CONTROLLERS_NEEDED if name in controllers]
    current = (base / "cgroup.subtree_control").read_text().split()
    enable = " ".join(f"+{name}" for name in wanted if name not in current)
    if enable:
        (base / "cgroup.subtree_control").write_text(enable)

    # Housekeeping: drop child cgroups of episodes that already exited.  A
    # cgroup can only be removed when it holds no processes; live episodes
    # are left alone.
    for stale in sorted(base.glob("worker-*")):
        try:
            if not (stale / "cgroup.procs").read_text().split():
                stale.rmdir()
        except OSError:
            continue

    child = base / f"worker-{_scope_unit_name(run_id).removeprefix('lh-worker-')}"
    child.mkdir()
    (child / "memory.max").write_text(str(to_bytes(limit)))
    return f"{base_cgroup.rstrip('/')}/{child.name}"


def _enter_cgroup(child_cgroup: str, *, root: Path | None = None) -> None:
    """Child-side ``preexec_fn`` body: move this process into its cgroup.

    Runs after fork, before exec: writing ``0`` (this process) to the child
    cgroup's ``cgroup.procs`` puts the worker — and everything it spawns,
    including the Claude Code child — inside the RSS-bounded cgroup from its
    first page.  A failure here raises, aborting the launch, rather than
    silently starting an unbounded worker while the record claims a bound.
    """

    procs = (root or CGROUP_ROOT) / child_cgroup.strip("/") / "cgroup.procs"
    with open(procs, "w", encoding="utf-8") as handle:
        handle.write("0")


def cgroup_plan(
    limit: str,
    command: list[str],
    run_id: str,
    *,
    root: Path | None = None,
) -> IsolationPlan:
    """The preferred plan: a per-episode child cgroup capped at ``limit`` RSS.

    Raises when the service's own cgroup subtree is not delegated (no
    ``Delegate=memory pids`` in the deployed unit); callers decide what to
    fall back to — see ``fallback_plan``.
    """

    child = _prepare_child_cgroup(run_id, limit, root=root)
    return IsolationPlan(
        mechanism=MECHANISM_CGROUP,
        limit=limit,
        command=list(command),
        preexec=lambda: _enter_cgroup(child, root=root),
        cgroup=child,
    )


def unbounded_plan(limit: str, command: list[str]) -> IsolationPlan:
    """The last-resort plan: launch the worker with NO memory bound at all.

    This exists so no future reader believes the fleet is protected when it
    is not: there is no rlimit here, on purpose.  The pre-208 fallback
    capped the worker's address space at three gibibytes, and on CT110 —
    where the scope launch always fails, so this fallback is the only path
    taken — that cap killed every episode before it started.  Measured on
    CT110, 2026-09-18 (live runs, overseer): the episode's Claude Code
    child is Node 22, and V8 plus its thread pool reserve about 5.3 GiB of
    address space (VmSize 5.31 GiB) and peak at 9.27 GiB (VmPeak) while
    using 0.26 GiB resident (VmRSS).  An address-space cap counts those
    reserved-but-untouched mappings and kills the episode before it starts;
    the resident figure it was meant to protect, 0.26 GiB, is nowhere near
    it.  RLIMIT_DATA does not rescue this either (see the module docstring:
    V8's private anonymous mappings count against it).  So: no bound, and a
    log line that says so plainly — the correct instrument (the delegated
    cgroup RSS bound) is what must be made to work instead.
    """

    logger.warning(
        "worker memory isolation: no mechanism could bound this worker; "
        "launching UNBOUNDED (requested limit %s not applied).  An "
        "address-space rlimit cannot bound an agent worker — Node 22/V8 "
        "reserves ~5.3 GiB of address space and peaks at 9.27 GiB virtual "
        "while using 0.26 GiB resident, which the old three-gibibyte "
        "address-space cap killed on sight; RSS needs a cgroup memory.max "
        "(Delegate=memory pids in packaging/lh-harness.service, then a "
        "deploy-window daemon-reload).  One run's blowup can now take the "
        "service cgroup down with it.",
        limit,
    )
    return IsolationPlan(
        mechanism=MECHANISM_UNBOUNDED,
        limit=limit,
        command=list(command),
        preexec=None,
    )


def fallback_plan(
    limit: str,
    command: list[str],
    run_id: str,
    *,
    root: Path | None = None,
) -> IsolationPlan:
    """Plan for a launch whose scope (or preferred mechanism) never started.

    Prefers the delegated cgroup mechanism (independent of any systemd
    manager); when that is not available either, the worker launches
    unbounded with a loud log — never an address-space rlimit.
    """

    try:
        return cgroup_plan(limit, command, run_id, root=root)
    except OSError as exc:
        logger.warning(
            "worker memory isolation: cgroup mechanism unavailable (%s: %s); "
            "falling back to the log-only unbounded plan",
            type(exc).__name__,
            exc,
        )
        return unbounded_plan(limit, command)


def probe_systemd_run() -> str | None:
    """Return the ``systemd-run`` binary when a manager is reachable.

    Overridable in tests so the scope path can be exercised on a host
    without one.
    """

    path = shutil.which("systemd-run")
    if not path:
        return None
    if os.path.exists(_USER_MANAGER_MARKER.format(uid=os.getuid())):
        return path
    for marker in _SYSTEM_MANAGER_MARKERS:
        if os.path.exists(marker):
            return path
    return None


def prepare_launch(
    *,
    command: list[str],
    run_id: str,
    memory_max: str,
    probe: Callable[[], str | None] | None = None,
    cgroup_writable: Callable[[], bool] | None = None,
    cgroup_root: Path | None = None,
) -> IsolationPlan:
    """Resolve how one worker launch should be memory-isolated.

    Order: the delegated cgroup subtree (RSS bound, works with no manager —
    the mechanism that works on CT110), then the systemd scope (RSS bound,
    needs an authorizing manager), then the log-only unbounded plan.  An
    address-space rlimit is deliberately NOT among the options; see the
    module docstring.
    """

    limit = parse_memory_limit(memory_max)
    writable = (cgroup_writable or cgroup_subtree_writable)()
    if writable:
        try:
            return cgroup_plan(limit, command, run_id, root=cgroup_root)
        except OSError as exc:
            logger.warning(
                "worker memory isolation: cgroup probe passed but the child "
                "cgroup could not be prepared (%s: %s); trying the scope "
                "mechanism",
                type(exc).__name__,
                exc,
            )
    runner = (probe or probe_systemd_run)()
    if runner:
        unit = _scope_unit_name(run_id)
        wrapped = [
            runner,
            "--scope",
            "--quiet",
            f"--unit={unit}",
            f"--property=MemoryMax={limit}",
            "--",
            *command,
        ]
        return IsolationPlan(
            mechanism=MECHANISM_SCOPE,
            limit=limit,
            command=wrapped,
            preexec=None,
            unit=unit,
        )
    return unbounded_plan(limit, command)


def _scope_unit_name(run_id: str) -> str:
    """A unique, unit-name-safe id so resume/retry never collides."""

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)[:64]
    return f"lh-worker-{safe}-{uuid.uuid4().hex[:8]}"


def scope_cgroup_for_pid(pid: int, unit: str, *, timeout: float = 2.0) -> str | None:
    """Wait briefly for the scope cgroup to appear for ``pid``.

    systemd moves the launched process into the new scope cgroup
    asynchronously; the run's owner record stores the resolved path so a
    later death can compare ``memory.events`` oom counters against the
    baseline captured here.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        path = _cgroup_of_pid(pid)
        if path is None:
            # The pid vanished (launch failed) or /proc has no cgroup file
            # for it; do not burn the whole budget polling a dead pid.
            return None
        if path.rsplit("/", 1)[-1] == f"{unit}.scope":
            return path
        time.sleep(0.05)
    return None


def _cgroup_of_pid(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/cgroup", "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    for line in lines:
        # cgroup v2 unified hierarchy: "0::<path>".
        fields = line.split(":", 2)
        if len(fields) == 3 and (fields[0] == "0" or fields[1] == "memory"):
            return fields[2]
    return None


def read_scope_oom_kills(cgroup_path: str | None) -> int | None:
    """Current ``oom_kill`` counter from a cgroup's ``memory.events``."""

    if not cgroup_path:
        return None
    events = Path("/sys/fs/cgroup") / cgroup_path.strip("/") / "memory.events"
    try:
        with events.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                key, _, count = line.partition(" ")
                if key.strip() == "oom_kill":
                    return int(count.strip())
    except (OSError, ValueError):
        return None
    return None


def verify_pid_cgroup(pid: int, expected_cgroup: str | None) -> bool:
    """Did a launched pid actually land in the cgroup the plan prepared?

    Belt-and-braces for the preexec self-move: if the pid is not where the
    record claims, callers must correct the record (an unbounded worker must
    never be recorded as bounded).
    """

    if not expected_cgroup:
        return False
    actual = _cgroup_of_pid(pid)
    return actual == expected_cgroup


def classify_memory_death(
    record: object,
    *,
    worker_log: Path | None = None,
    returncode: int | None = None,
) -> str | None:
    """Map a worker death to the memory-kill failure reason.

    Positive evidence only:

    - scope and delegated-cgroup launches compare their cgroup's
      ``memory.events`` ``oom_kill`` counter against the baseline recorded
      at launch;
    - every mechanism that applied a limit may also fall back to a
      ``MemoryError`` traceback in the worker log tail.  The unbounded
      log-only plan applied nothing, so its deaths are never attributed to
      a memory limit.

    ``None`` means the death is not attributable to the memory limit, and the
    caller keeps its existing reasons unchanged.
    """

    if not isinstance(record, dict):
        return None
    limit = str(record.get("limit") or "")
    if not limit:
        return None
    reason = memory_kill_reason(limit)

    if record.get("mechanism") in (MECHANISM_SCOPE, MECHANISM_CGROUP):
        baseline = record.get("oom_kill_base")
        oom_now = read_scope_oom_kills(str(record.get("cgroup") or ""))
        if oom_now is not None and (baseline is None or oom_now > int(baseline)):
            return reason

    if record.get("mechanism") != MECHANISM_UNBOUNDED and _log_tail_mentions_memory_error(
        worker_log
    ):
        return reason
    return None


def _log_tail_mentions_memory_error(worker_log: Path | None) -> bool:
    if not worker_log:
        return False
    try:
        with open(worker_log, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 64 * 1024))
            tail = handle.read(64 * 1024)
    except OSError:
        return False
    return b"MemoryError" in tail


# One-time self-relocation of the service's own PID into a leaf cgroup
# to allow enabling subtree_control on the parent cgroup.
_RELOCATED = False
_WORKER_PARENT_CGROUP = None


def _attempt_relocation(*, root: Path | None = None) -> None:
    """Attempt to relocate the current process into a leaf cgroup under its
    current cgroup, so the current cgroup becomes empty of processes
    and can have subtree_control enabled for children.
    """
    global _RELOCATED, _WORKER_PARENT_CGROUP
    if _RELOCATED:
        return
    current = own_cgroup_path()
    if not current:
        return
    effective_root = root if root is not None else CGROUP_ROOT
    current_cgroup_path = effective_root / current.strip("/")
    try:
        with open(current_cgroup_path / "cgroup.procs", "r") as f:
            pids = f.read().split()
    except OSError:
        # Cannot read the cgroup.procs, give up on relocation.
        return
    # If there are no processes in the current cgroup, we are already
    # "relocated" (the cgroup is empty). Record that we have relocated
    # so that future calls use this cgroup as the base for child cgroups.
    if not pids:
        _RELOCATED = True
        _WORKER_PARENT_CGROUP = current
        return
    # Otherwise, try to move the current process to a leaf cgroup named "main"
    leaf = current.rstrip("/") + "/main"
    leaf_path = effective_root / leaf.strip("/")
    try:
        leaf_path.mkdir(parents=True, exist_ok=True)
        current_pid = str(os.getpid())
        # Move the current process into the leaf cgroup.
        with open(leaf_path / "cgroup.procs", "w") as f:
            f.write(current_pid + "\n")
        # Remove the current process from the current cgroup.
        new_pids = [pid for pid in pids if pid != current_pid]
        with open(current_cgroup_path / "cgroup.procs", "w") as f:
            for pid in new_pids:
                f.write(pid + "\n")
        _WORKER_PARENT_CGROUP = current
        _RELOCATED = True
    except OSError as e:
        logger.warning(
            f"worker memory isolation: failed to self-relocate into cgroup {leaf}: {e}"
        )
        # Leave _RELOCATED as False and _WORKER_PARENT_CGROUP as None.


def scope_launch_failed(returncode: int | None, log_tail: bytes) -> bool:
    """Did ``systemd-run`` die before creating the scope (no worker ran)?

    Structural decision, not a phrase match: with the scope cgroup failed to
    appear (established by the caller), *any* non-zero ``systemd-run`` exit
    means the wrapped command was not started, so the retry path applies.
    On CT110 this is every launch — systemd 255 in an LXC container with no
    polkit daemon refuses scope creation ("Failed to start transient scope
    unit: Access denied", task 205).  The exact stderr wording varies across
    systemd builds and failure modes, so ``log_tail`` is used only to pick a
    log diagnostic.
    """

    if returncode is None or returncode == 0:
        return False
    text = log_tail.decode("utf-8", errors="replace").lower()
    matched = [signature for signature in _SCOPE_FAILURE_SIGNATURES if signature in text]
    logger.warning(
        "worker memory isolation: systemd-run exited rc=%s without creating its "
        "scope; failure signature match: %s; log tail: %.512r",
        returncode,
        matched or "none",
        log_tail,
    )
    return True


def await_scope_exit(process: Any, *, timeout: float = 3.0) -> int | None:
    """Settle a launcher whose scope cgroup never appeared onto an exit code.

    ``poll()`` may still be ``None`` right after the cgroup wait: the
    launcher can take a moment to die.  Wait briefly for its exit so the
    failed-launch decision is made on a settled return code, and report
    ``None`` only when it is genuinely still running past the grace period.
    """

    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return process.poll()


# ``signal.SIGKILL`` is the kernel's only tool for an OOM kill; keep the
# constant here so the supervisor's death paths can share the check.
def is_oom_kill_signal(returncode: int | None) -> bool:
    return returncode is not None and returncode == -signal.SIGKILL