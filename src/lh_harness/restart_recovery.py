"""Restart-interrupted run recovery: one fenced second chance for a killed run.

Measured defect (V1.5 qualification on CT111, 2026-11-04, lease
``ovf-v15-ct111-qual-20261104``): at 13:04:41Z ``lh-harness`` was SIGKILLed
and systemd restarted it (``KillMode=control-group``, ``Restart=on-failure``).
systemd took the run workers down with the unit.  Both live runs
(``20261104T122016Z_3d4b7584`` round 3/6 and ``20261104T124058Z_0554822b``
round 2/3) ended ``failed`` with "worker disappeared without a final report".
Their queue entries (``q-df0b81e1``, ``q-d1fb4854``) went to ``failed``
with **no successor** — the queue's normal retry path only covers provider
quota / rate-limit / episode-timeout / stall causes, and a service stop is
none of those.  The killed run also left an untracked ``graphify-out/`` in
workspace ``claude-code-teams-h``, so the next pending entry on that tree was
skipped as a "dirty tree" instead of running.

This module adds exactly one recovery path for that case.  It is NOT a
parallel retry mechanism: the launcher calls it from the *same* place that
marks a launched entry failed (``Launcher._update_launched_entries``), and the
successor it creates is an ordinary queue entry with the ordinary
``retry_of`` link — the launcher launches it through the ordinary launch path.

Three properties are load-bearing and are asserted by the tests:

* **One successor, never two.**  The successor is keyed by ``retry_of``; if a
  successor for that ``retry_of`` already exists the reconcile is a no-op, so
  calling it twice (or two launchers racing) still yields exactly one entry.
* **One second chance only.**  An entry that already has a restart retry is
  never retried again for this reason; a restart-interrupted successor that
  is itself interrupted does not spawn a second-generation retry.
* **The interrupted work is preserved, not destroyed.**  The workspace is
  cleaned with ``git stash push -u -m "lh-harness interrupted <run id>"``
  (untracked files included, the stash kept).  ``git clean`` and
  ``git reset --hard`` are never used: the interrupted run's uncommitted work
  stays recoverable in a named stash, and the stash name is logged in the
  run's report.

Fencing is untouched.  A round that was never replayed is not replayed here;
this module only decides whether a *run* gets a successor queue entry.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .safe_subprocess import child_env, git_argv

# The canonical failure-cause label for a run whose worker died because the
# service itself stopped.  It is a *cause*, not a status: the entry is still
# marked failed; the label is what makes the restart retry distinguishable
# from every other retry cause.
INTERRUPTED_BY_RESTART = "interrupted_by_restart"

# The suffix appended to the interrupted entry's name for its successor.
RETRY_NAME_SUFFIX = "-r1"

# The failure reason the supervisor writes when a worker vanished without
# leaving a report.  Compared case-insensitively; the supervisor owns the
# literal (``supervisor/service.py``), this module only reads it.
WORKER_DISAPPEARED_REASON = "worker disappeared without a final report"

# The durable record of "this service instance started at <t> as pid <p>".
# Written once at service start.  A run whose owner names a *different*
# supervisor pid was launched by a previous service instance, so its worker
# was live across a service restart.
EPOCH_FILENAME = "service_epoch.json"

# Signals that mean "the service was going away", as opposed to a worker
# crash of its own accord.
_SHUTDOWN_SIGNALS = frozenset({"SIGKILL", "SIGTERM"})

_GIT_TIMEOUT = 90


@dataclass
class ServiceEpoch:
    """The durable identity of one service instance.

    ``pid`` is the process id of the service that owns the run workers and
    ``started_at`` is the wall-clock second the service instance began.  A run
    whose owner record names a different pid was launched by an *earlier*
    service instance: the worker was live when this service started, and its
    later death is therefore restart-interrupted rather than a spontaneous
    crash.
    """

    pid: int
    started_at: float
    # The pid the previous service instance had, when this epoch replaced one.
    previous_pid: int | None = None
    # The wall-clock second the previous service instance started.
    previous_started_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "previous_pid": self.previous_pid,
            "previous_started_at": self.previous_started_at,
        }


def epoch_path(runs_root: str | Path) -> Path:
    """Return the path of the durable service-epoch record."""

    return Path(runs_root) / "queue" / EPOCH_FILENAME


def read_epoch(runs_root: str | Path) -> ServiceEpoch | None:
    """Read the durable service epoch; ``None`` when absent or unreadable."""

    path = epoch_path(runs_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    started_at = data.get("started_at")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
        return None
    previous_pid = data.get("previous_pid")
    if isinstance(previous_pid, bool) or not isinstance(previous_pid, int):
        previous_pid = None
    previous_started_at = data.get("previous_started_at")
    if isinstance(previous_started_at, bool) or not isinstance(previous_started_at, (int, float)):
        previous_started_at = None
    return ServiceEpoch(
        pid=pid,
        started_at=float(started_at),
        previous_pid=previous_pid,
        previous_started_at=float(previous_started_at) if previous_started_at is not None else None,
    )


def write_epoch(runs_root: str | Path, epoch: ServiceEpoch) -> bool:
    """Persist the service epoch; ``False`` when the write failed.

    The write is best-effort by design: a service that cannot persist its
    epoch must still start and run normally — it simply cannot recognise a
    restart-interrupted death afterwards.
    """

    path = epoch_path(runs_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(epoch.as_dict(), sort_keys=True) + "\n"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def begin_service_epoch(
    runs_root: str | Path,
    *,
    pid: int | None = None,
    started_at: float | None = None,
) -> ServiceEpoch:
    """Record the current service instance's epoch and return it.

    Called once when the service starts.  When an earlier epoch exists, its
    pid is carried over as ``previous_pid`` so a run launched by that earlier
    instance is recognisable as having been live across the restart.
    """

    previous = read_epoch(runs_root)
    epoch = ServiceEpoch(
        pid=int(pid if pid is not None else os.getpid()),
        started_at=float(started_at if started_at is not None else time.time()),
        previous_pid=previous.pid if previous is not None else None,
        previous_started_at=previous.started_at if previous is not None else None,
    )
    write_epoch(runs_root, epoch)
    return epoch


def _owner_pid(owner: dict[str, Any] | None) -> int | None:
    """Read the supervisor pid from a run's owner record, if any."""

    if not isinstance(owner, dict):
        return None
    for key in ("supervisor_pid", "service_pid"):
        value = owner.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value > 0:
            return value
    return None


def _owner_started_at(owner: dict[str, Any] | None) -> float | None:
    """Read the launch timestamp from a run's owner record, if any."""

    if not isinstance(owner, dict):
        return None
    value = owner.get("started_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _is_worker_disappeared(failure_reason: str | None) -> bool:
    """True when the supervisor's reason is the "worker vanished" reason."""

    text = str(failure_reason or "").strip().lower()
    if not text:
        return False
    return WORKER_DISAPPEARED_REASON.lower() in text


def _signal_is_shutdown_signal(value: Any) -> bool:
    """True when a recorded exit signal means "the service was stopping".

    Accepts the signal name (``"SIGKILL"``), the signal number, and the
    conventional negative return code a shell reports for a
    signal-terminated child (``-9`` for SIGKILL, ``-15`` for SIGTERM).
    """

    if isinstance(value, str):
        return value.strip().upper() in _SHUTDOWN_SIGNALS
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    if value < 0:
        return -value in (9, 15)
    return value in (9, 15)


def classify_worker_death(
    *,
    failure_reason: str | None,
    status: dict[str, Any] | None = None,
    owner: dict[str, Any] | None = None,
    epoch: ServiceEpoch | None = None,
) -> str | None:
    """Classify a "worker disappeared" death.

    Returns ``INTERRUPTED_BY_RESTART`` when the worker died because the
    service itself stopped, and ``None`` otherwise (an ordinary crash, a
    memory kill, a human stop — every one of which keeps its current
    behaviour).

    Two independent signals say "the service stopped":

    1. **The run was live at service start-up.**  The run's owner record
       names a supervisor pid that is not this service instance's pid, so the
       worker was launched by an *earlier* service instance and was still
       running when this one took over.  (The owner may also predate the
       current epoch's start.)
    2. **The worker exited on a shutdown signal while the service was
       stopping.**  A recorded ``SIGKILL``/``SIGTERM`` exit is what systemd's
       ``KillMode=control-group`` does to the whole unit's children.

    A death with no report and no such signal is NOT restart-interrupted: it
    keeps the existing "worker disappeared" failure and the existing
    (absent) retry behaviour.
    """

    if not _is_worker_disappeared(failure_reason):
        return None
    if epoch is None:
        return None

    status = status if isinstance(status, dict) else {}
    owner = owner if isinstance(owner, dict) else {}

    # Signal 2: the worker was killed by the service's own shutdown.
    for key in ("exit_signal", "signal", "term_signal", "killed_by_signal"):
        if _signal_is_shutdown_signal(status.get(key)):
            return INTERRUPTED_BY_RESTART
    exit_code = status.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        exit_code = None
    if exit_code is not None and _signal_is_shutdown_signal(exit_code):
        return INTERRUPTED_BY_RESTART
    owner_signal = owner.get("exit_signal")
    if _signal_is_shutdown_signal(owner_signal):
        return INTERRUPTED_BY_RESTART

    # Signal 1: the worker belonged to a previous service instance, so it was
    # live across this service's start-up.
    owner_pid = _owner_pid(owner)
    if owner_pid is not None:
        # The owner names the service instance that launched the worker.  A
        # different pid means an EARLIER instance launched it, so the worker
        # was live when this instance started.  This is the strong signal:
        # when the pid matches, the run belongs to THIS service instance and
        # its death is NOT restart-interrupted, whatever the timestamps say.
        if owner_pid != epoch.pid:
            return INTERRUPTED_BY_RESTART
        return None
    # No pid in the owner record (an older run, or one whose owner was
    # written before the pid was recorded): fall back to the launch
    # timestamp -- a run that started before this service instance began
    # was live across the start-up.
    owner_started_at = _owner_started_at(owner)
    if (
        owner_started_at is not None
        and epoch.started_at > 0
        and owner_started_at < epoch.started_at
    ):
        return INTERRUPTED_BY_RESTART
    return None


def find_restart_successor(entries: list[Any], original_queue_id: str) -> Any | None:
    """Return the successor entry for ``original_queue_id``, if one exists.

    The link is the entry's ``retry_of`` field, which the queue store already
    maintains for every retry.  Looking the successor up by that link is what
    makes the reconcile idempotent: a second call finds the entry the first
    call created and creates nothing.
    """

    for entry in entries:
        retry_of = getattr(entry, "retry_of", None)
        if retry_of == original_queue_id:
            return entry
    return None


def already_restart_retried(entry: Any, entries: list[Any]) -> bool:
    """True when ``entry`` must not be retried again for a restart.

    "Do not retry an entry already retried once for this reason" has two
    shapes, and BOTH are about the entry's own retry history rather than
    about the label on the entry:

    * a successor of this entry already exists whose failure cause is
      ``interrupted_by_restart`` -- the one restart second chance was
      already spent on this entry; and
    * this entry IS itself such a successor: its predecessor (``retry_of``)
      is an entry whose failure cause is ``interrupted_by_restart``, so this
      entry was created as somebody's second chance and does not get a
      second chance of its own.

    A successor created for a *different* reason (provider quota, a timeout)
    does not consume the one restart second chance.

    Note that an entry's OWN ``failure_cause`` is NOT the signal here: that
    field records why the entry DIED, and the interrupted original carries
    ``interrupted_by_restart`` too.
    """

    queue_id = getattr(entry, "queue_id", None)
    for candidate in entries:
        if getattr(candidate, "retry_of", None) != queue_id:
            continue
        cause = str(getattr(candidate, "failure_cause", "") or "").strip().lower()
        if INTERRUPTED_BY_RESTART in cause:
            # A restart successor of this entry already exists.
            return True
    # Is this entry itself a restart successor?  Its predecessor is the
    # entry it was created from; that predecessor died with the restart
    # cause, which is what marks this entry as a second chance.
    predecessor_id = getattr(entry, "retry_of", None)
    if predecessor_id:
        for candidate in entries:
            if getattr(candidate, "queue_id", None) != predecessor_id:
                continue
            cause = str(getattr(candidate, "failure_cause", "") or "").strip().lower()
            if INTERRUPTED_BY_RESTART in cause:
                return True
    return False


def _git(repo: Path, *args: str) -> str | None:
    """Run git in ``repo``; ``None`` when the command fails."""

    try:
        proc = subprocess.run(
            git_argv("-C", str(repo), *args),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            env=child_env(),
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return None if proc.returncode != 0 else proc.stdout.strip()


def workspace_is_dirty(repo: Path) -> bool:
    """True when the workspace has uncommitted or untracked changes."""

    status = _git(repo, "status", "--porcelain")
    if status is None:
        return False
    return bool(status.strip())


def stash_interrupted_workspace(workspace: str | Path, run_id: str) -> str | None:
    """Preserve the interrupted run's uncommitted work in a named stash.

    Uses ``git stash push -u -m "lh-harness interrupted <run id>"``: the
    ``-u`` keeps untracked files (the killed run left an untracked
    ``graphify-out/`` behind, which is what made the next pending entry on
    that tree skip as a "dirty tree") and the stash itself is KEPT — the
    interrupted run's work stays recoverable.

    ``git clean`` and ``git reset --hard`` are never used: they destroy work
    that only exists in the working tree.

    Returns the stash name that was created, or ``None`` when there was
    nothing to stash (a clean tree) or the stash failed.  A failed stash is
    *not* an error the caller must raise: the tree is left exactly as it
    was, which is the safe outcome.
    """

    repo = Path(workspace).expanduser()
    if not repo.is_dir():
        return None
    if not workspace_is_dirty(repo):
        # A clean tree has nothing to preserve; `git stash push` on it is a
        # no-op that creates no stash, so it is skipped entirely.
        return None
    stash_name = f"lh-harness interrupted {run_id}"
    out = _git(repo, "stash", "push", "--include-untracked", "--message", stash_name)
    if out is None:
        return None
    return stash_name


def _report_path(runs_root: str | Path, run_id: str) -> Path:
    return Path(runs_root) / run_id / "lh_harness" / "report.json"


def log_stash_in_report(
    runs_root: str | Path,
    run_id: str,
    stash_name: str,
    *,
    workspace: str = "",
) -> bool:
    """Record the stash name in the interrupted run's durable report.

    The report is the artifact of record for the run, so the name of the
    stash that holds the interrupted run's uncommitted work belongs there.
    A missing report is created (the supervisor writes one for a vanished
    worker); a report that cannot be written is not fatal — the stash
    itself is already durable in the workspace.
    """

    path = _report_path(runs_root, run_id)
    payload: dict[str, Any] = {}
    try:
        if path.is_file():
            raw = path.read_text(encoding="utf-8")
            try:
                existing = json.loads(raw)
            except (ValueError, TypeError):
                existing = {}
            if isinstance(existing, dict):
                payload = existing
    except OSError:
        payload = {}
    payload["stash"] = stash_name
    payload["stash_workspace"] = str(workspace)
    payload["interrupted_by"] = INTERRUPTED_BY_RESTART
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def reconcile_interrupted_run(
    queue_store: Any,
    entry: Any,
    *,
    runs_root: str | Path,
    epoch: ServiceEpoch | None,
    status: dict[str, Any] | None = None,
    owner: dict[str, Any] | None = None,
    cause: str = INTERRUPTED_BY_RESTART,
) -> Any | None:
    """Create the single ``-r1`` successor for a restart-interrupted entry.

    Returns the successor entry, or ``None`` when no successor was created —
    either because the death is not restart-interrupted, or because a
    successor already exists (idempotent), or because the entry already had
    its one restart second chance.
    """

    if epoch is None:
        return None
    failure_reason = None
    if isinstance(status, dict):
        failure_reason = status.get("failure_reason")
    if classify_worker_death(
        failure_reason=failure_reason,
        status=status,
        owner=owner,
        epoch=epoch,
    ) != INTERRUPTED_BY_RESTART:
        return None

    entries = queue_store.list()
    original_queue_id = getattr(entry, "queue_id", None)
    if not original_queue_id:
        return None

    # Idempotency: a successor for this original already exists.
    existing = find_restart_successor(entries, original_queue_id)
    if existing is not None:
        return None

    # One second chance only: never retry an entry that already had a
    # restart retry of its own.
    if already_restart_retried(entry, entries):
        return None

    workspace = str(getattr(entry, "workspace", "") or "")
    run_id = str(getattr(entry, "run_id", "") or "")
    stash_name = ""
    if workspace and run_id:
        stash_name = stash_interrupted_workspace(workspace, run_id) or ""
        if stash_name:
            log_stash_in_report(runs_root, run_id, stash_name, workspace=workspace)

    successor = queue_store.requeue(
        original_queue_id,
        cause,
        name_suffix=RETRY_NAME_SUFFIX,
    )
    if successor is None:
        return None
    return successor
