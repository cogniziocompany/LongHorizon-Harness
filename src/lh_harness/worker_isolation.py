"""Per-run worker memory isolation (TASK 202).

A single run's worker used to share the ``lh-harness.service`` cgroup with
every other run.  One run's memory blowup therefore hit the *service* memcg
limit: the kernel OOM-killed the offending child, systemd failed the whole
unit, the service restarted, and every other live run died with "worker
disappeared without a final report".

This module gives each run's worker its own memory boundary:

- preferred: a transient systemd scope (``systemd-run --scope``) carrying a
  ``MemoryMax`` cap, so the kernel OOM-kills only the offending run's child;
- fallback: an ``RLIMIT_AS`` limit applied directly on the child, used when
  ``systemd-run`` is unavailable (non-systemd hosts, missing user manager,
  containers).  ``RLIMIT_AS`` bounds the child's address space (and that of
  everything it spawns, since limits are inherited); it is stricter than an
  RSS cap because it counts virtual mappings, but it is the portable
  fallback.

The limit comes from (highest precedence first) the
``LH_HARNESS_WORKER_MEMORY_MAX`` environment override, the
``[run] worker_memory_max`` project-config key, and the 3G default.

Both mechanisms are recorded in the run's owner record, and a log line
always states which mechanism a launch used.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_WORKER_MEMORY_MAX = "3G"
ENV_WORKER_MEMORY_MAX = "LH_HARNESS_WORKER_MEMORY_MAX"
CONFIG_KEY_WORKER_MEMORY_MAX = "worker_memory_max"
MEMORY_KILL_REASON_TEMPLATE = "memory limit exceeded ({limit})"

MECHANISM_SCOPE = "systemd-run-scope"
MECHANISM_RLIMIT = "rlimit-as"

# systemd's own byte suffixes (IEC, binary multiples).  A bare value is bytes.
_SUFFIXES = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4, "p": 1024**5}
_LIMIT_RE = re.compile(r"^(?P<value>\d+)(?P<suffix>[kKmMgGtTpP])?$")

# systemd-run talks to a manager: the user manager when it exists (the
# service's own user), otherwise the system bus.  Neither marker present
# means the host is not systemd-managed and the RLIMIT_AS fallback applies.
_USER_MANAGER_MARKER = "/run/user/{uid}/systemd"
_SYSTEM_MANAGER_MARKERS = ("/run/systemd/system", "/run/dbus/system_bus_socket")

# Signatures systemd-run writes to stderr when it cannot create the scope
# (no bus, no authorization, ...).  The wrapped command is not started in
# that case, so the launch may be retried with the RLIMIT_AS fallback.
_SCOPE_FAILURE_SIGNATURES = (
    "failed to start transient service unit",
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
    """Validate a systemd-style byte size such as ``3G`` and return it.

    The string is stored and applied verbatim (systemd accepts the same
    suffixes), and ``to_bytes`` does the numeric conversion for RLIMIT_AS.
    """

    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not _LIMIT_RE.match(value.strip()):
        raise ValueError(
            "worker memory limit must be an integer byte count or a "
            "systemd byte size such as 512M, 3G or 1T"
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
    """Effective limit: explicit argument, env override, config key, 3G."""

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

    def record(self) -> dict[str, Any]:
        return {
            "mechanism": self.mechanism,
            "limit": self.limit,
            "unit": self.unit,
            "cgroup": "",
            "oom_kill_base": None,
        }


def apply_rlimit_as(limit_bytes: int) -> None:
    """Child-side ``preexec_fn`` body: cap address space at ``limit_bytes``."""

    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    target = limit_bytes if hard == resource.RLIM_INFINITY else min(limit_bytes, hard)
    resource.setrlimit(resource.RLIMIT_AS, (target, hard))


def probe_systemd_run() -> str | None:
    """Return the ``systemd-run`` binary when a manager is reachable.

    Overridable in tests so the fallback path can be exercised on a host
    that actually has systemd-run.
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
) -> IsolationPlan:
    """Resolve how one worker launch should be memory-isolated."""

    limit = parse_memory_limit(memory_max)
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
    return IsolationPlan(
        mechanism=MECHANISM_RLIMIT,
        limit=limit,
        command=list(command),
        preexec=lambda: apply_rlimit_as(to_bytes(limit)),
    )


def rlimit_plan(limit: str, command: list[str]) -> IsolationPlan:
    """The fallback plan, used to retry a launch whose scope never started."""

    return IsolationPlan(
        mechanism=MECHANISM_RLIMIT,
        limit=limit,
        command=list(command),
        preexec=lambda: apply_rlimit_as(to_bytes(limit)),
    )


def _scope_unit_name(run_id: str) -> str:
    """A unique, unit-name-safe scope id so resume/retry never collides."""

    import uuid

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


def classify_memory_death(
    record: object,
    *,
    worker_log: Path | None = None,
    returncode: int | None = None,
) -> str | None:
    """Map a worker death to the memory-kill failure reason.

    Positive evidence only:

    - scope launches compare the scope cgroup's ``memory.events`` ``oom_kill``
      counter against the baseline recorded at launch;
    - every mechanism (including the RLIMIT_AS fallback, where no cgroup is
      dedicated to the run) may fall back to a ``MemoryError`` traceback in
      the worker log tail.

    ``None`` means the death is not attributable to the memory limit, and the
    caller keeps its existing reasons unchanged.
    """

    if not isinstance(record, dict):
        return None
    limit = str(record.get("limit") or "")
    if not limit:
        return None
    reason = memory_kill_reason(limit)

    if record.get("mechanism") == MECHANISM_SCOPE:
        baseline = record.get("oom_kill_base")
        oom_now = read_scope_oom_kills(str(record.get("cgroup") or ""))
        if oom_now is not None and (baseline is None or oom_now > int(baseline)):
            return reason

    if _log_tail_mentions_memory_error(worker_log):
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


def scope_launch_failed(returncode: int | None, log_tail: bytes) -> bool:
    """Did ``systemd-run`` die before creating the scope (no worker ran)?"""

    if returncode is None or returncode == 0:
        return False
    text = log_tail.decode("utf-8", errors="replace").lower()
    return any(signature in text for signature in _SCOPE_FAILURE_SIGNATURES)


# ``signal.SIGKILL`` is the kernel's only tool for an OOM kill; keep the
# constant here so the supervisor's death paths can share the check.
def is_oom_kill_signal(returncode: int | None) -> bool:
    return returncode is not None and returncode == -signal.SIGKILL