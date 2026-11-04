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

The operator correction (2026-11-04 14:24Z) adds three more, all of them
about what the interrupted run left behind:

* **The effects are detected before the successor is created.**  A branch the
  interrupted run pushed, a PR it opened, commits it made that never reached
  the remote, the files it left behind.  A successor that started fresh would
  silently drop all of them, so a pushed branch or an open PR makes the
  successor CONTINUE that branch (the repo's existing ``continue_branch``
  continuation opt-in) instead of cutting a fresh base.
* **An undecidable effect is never guessed.**  A git probe that fails leaves
  the effect undecided; an undecided effect marks the entry ``needs_review``
  and creates NO successor, for a human to look at.  Nothing is deleted and
  nothing is resubmitted blindly.
* **The lineage is recorded on both ends.**  The successor carries
  ``retry_of`` and ``interrupted_run``; the original entry carries the
  successor's queue id.  The stash name and the detected effects are written
  into the interrupted run's durable report.

A successor also never launches while the interrupted run's own worker is
still alive (the launch gate): two workers for one task is what the gate
exists to prevent, and the successor waits instead of racing the stale
worker.

Only a death that PROVES the service stopped counts.  The proof is the
service epoch: the dead worker belonged to an EARLIER service instance, so
it was live when this instance started and this instance's start-up is what
ended it.  A shutdown signal alone does NOT prove it -- a single worker
killed out of band (OOM killer, operator, another process) dies on the same
``SIGKILL``/``SIGTERM`` while the service keeps running, and such a death
stays an ordinary failure with no successor.

Fencing is untouched.  A round that was never replayed is not replayed here;
this module only decides whether a *run* gets a successor queue entry.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .safe_subprocess import child_env, git_argv

# The canonical failure-cause label for a run whose worker died because the
# service itself stopped.  It is a *cause*, not a status: the entry is still
# marked failed; the label is what makes the restart retry distinguishable
# from every other retry cause.
INTERRUPTED_BY_RESTART = "interrupted_by_restart"

# The suffix appended to the interrupted entry's name for its successor.
RETRY_NAME_SUFFIX = "-r1"

# The reason recorded on an entry whose restart recovery could not decide
# safely (the interrupted run's effects could not be determined).  Such an
# entry gets NO successor: a human looks at it.
NEEDS_REVIEW_REASON = "needs_review: restart recovery could not determine the interrupted run's effects"

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

# The bound the launcher applies to a queue entry's ``reason`` field.
_MAX_REASON_LEN = 500

# The DISTINCT "unknown" result of the open-PR probe (review finding 2).
#
# ``workspace_guard.probe_open_pr_gh`` returns ``None`` both when there is
# no open PR and when ``gh`` could not answer (missing CLI, non-zero exit,
# timeout).  For a *launch guard* that collapse is harmless -- the probe
# fails open and the launch proceeds.  For *effect detection* it is not:
# an undetectable PR on a branch that was pushed nowhere else would be read
# as "no PR", and the successor would then cut a fresh base and silently
# drop the interrupted run's published work.
#
# The recovery path therefore distinguishes the two.  ``PROBE_UNKNOWN`` is
# returned when the probe could not answer; ``None`` means "answered, and
# there is no open PR".  An unknown leaves the effect UNDECIDED, which
# marks the entry ``needs_review`` and creates NO successor.
PROBE_UNKNOWN = "__lh_probe_unknown__"

# The timeout bound for the recovery path's own ``gh`` probe.
_GH_TIMEOUT = 30


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


def worker_belonged_to_previous_epoch(
    owner: dict[str, Any] | None,
    epoch: ServiceEpoch | None,
) -> bool:
    """True when the worker's owner proves it belonged to an EARLIER epoch.

    This is the *required* evidence that the SERVICE stopped: the worker was
    launched by a service instance that is not the current one, so it was
    live when the current instance took over, and its later death is a
    restart interruption rather than a spontaneous crash.

    The owner record names the service instance that launched the worker
    (``supervisor_pid``/``service_pid``) and when it launched it
    (``started_at``).  Three ways to prove a previous epoch:

    * the owner pid is the epoch's ``previous_pid`` -- the instance this one
      explicitly replaced (the strongest evidence: the epoch record itself
      names the predecessor);
    * the owner pid is a different pid from the current epoch's pid -- the
      worker was launched by some earlier instance;
    * the owner carries no pid but predates the current epoch's start --
      an older run whose owner predates this service instance.

    A worker whose owner pid EQUALS the current epoch's pid belongs to THIS
    service instance: its death is a plain failure, whatever signal it
    died of.  A worker with no usable owner evidence at all is likewise
    NOT provably from a previous epoch, so it stays a plain failure.
    """

    if epoch is None:
        return False
    owner = owner if isinstance(owner, dict) else {}

    owner_pid = _owner_pid(owner)
    if owner_pid is not None:
        # The epoch record names the instance it replaced: an owner pid that
        # matches it is a previous-epoch worker by direct record.
        if epoch.previous_pid is not None and owner_pid == epoch.previous_pid:
            return True
        # Any other differing pid means an EARLIER instance launched it.
        # A matching pid means THIS instance launched it, so the worker is
        # NOT from a previous epoch.
        return owner_pid != epoch.pid

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
        return True
    return False


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


def death_took_a_shutdown_signal(
    status: dict[str, Any] | None,
    owner: dict[str, Any] | None = None,
) -> bool:
    """True when the worker died on a service-shutdown signal.

    This is CORROBORATING evidence for a restart interruption, never
    sufficient on its own: the same ``SIGKILL``/``SIGTERM`` kills a single
    worker out of band while the service keeps running, so a signal says how
    the worker died, not that the service went down.  The launcher records it
    alongside the classification so the evidence behind a restart successor
    is readable in the logs.
    """

    status = status if isinstance(status, dict) else {}
    owner = owner if isinstance(owner, dict) else {}
    for key in ("exit_signal", "signal", "term_signal", "killed_by_signal"):
        if _signal_is_shutdown_signal(status.get(key)):
            return True
    exit_code = status.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        exit_code = None
    if exit_code is not None and _signal_is_shutdown_signal(exit_code):
        return True
    return _signal_is_shutdown_signal(owner.get("exit_signal"))


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

    The REQUIRED evidence is that the SERVICE stopped: the run was live when
    the current service instance (launcher epoch) started, so the dead worker
    belonged to a PREVIOUS epoch.  That is proved by the run's owner record
    (see ``worker_belonged_to_previous_epoch``): the owner names an earlier
    service instance, either as the epoch's recorded ``previous_pid`` or as a
    different pid, or the owner's launch timestamp predates this epoch's
    start.

    A shutdown signal is only CORROBORATING, never sufficient on its own.
    ``SIGKILL``/``SIGTERM`` (and the exit codes that carry them) say how the
    worker died, not that the service went down: a single worker killed out
    of band -- by the OOM killer, by an operator, by another process -- dies
    on the very same signals while the service keeps running.  Treating such
    a death as restart-interrupted would hand an ordinary failure a successor
    entry, which is exactly the over-match this classification must not
    have.  So a signal is recorded only as supporting evidence for a worker
    that already belongs to a previous epoch.

    A death with no report, or with a worker that provably belongs to THIS
    epoch, is NOT restart-interrupted: it keeps the existing "worker
    disappeared" failure and the existing (absent) retry behaviour.
    """

    if not _is_worker_disappeared(failure_reason):
        return None
    if epoch is None:
        return None

    status = status if isinstance(status, dict) else {}
    owner = owner if isinstance(owner, dict) else {}

    # REQUIRED evidence: the worker belonged to a PREVIOUS service instance,
    # so it was live across this service's start-up.  Without it the death is
    # an ordinary one, whatever signal killed the worker.
    if not worker_belonged_to_previous_epoch(owner, epoch):
        return None

    # The previous-epoch evidence already proves the service stopped (the
    # worker was live when this instance started, so this instance's
    # start-up is what ended it).  A shutdown signal -- what systemd's
    # ``KillMode=control-group`` does to the whole unit's children -- merely
    # CORROBORATES that story; it cannot make a same-epoch death
    # restart-interrupted, and its absence does not weaken the proof.
    return INTERRUPTED_BY_RESTART


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
    lineage: dict[str, Any] | None = None,
) -> bool:
    """Record the stash name in the interrupted run's durable report.

    The report is the artifact of record for the run, so the name of the
    stash that holds the interrupted run's uncommitted work belongs there.
    A missing report is created (the supervisor writes one for a vanished
    worker); a report that cannot be written is not fatal — the stash
    itself is already durable in the workspace.

    The restart-recovery lineage (the original queue id, the successor's
    queue id, the interrupted run id and the detected effects) is written
    into the same report, so the lineage is readable from the run's own
    durable artifact and not only from the queue rows.
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
    if lineage:
        payload["restart_recovery"] = dict(lineage)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return False
    return True


# ----------------------------------------------------------------------
# Effect detection (operator correction, task easybutt0n-17).
#
# Before a successor is created, the interrupted run's effects are
# detected: a branch it pushed, a PR it opened, commits it made that never
# reached the remote, and the files it left behind.  A successor that
# started fresh would silently drop all of them, so:
#
# * a pushed branch or an open PR means the successor CONTINUES that
#   branch (the repo's existing ``continue_branch`` continuation opt-in,
#   task 201) instead of cutting a fresh base;
# * an effect that cannot be determined is not guessed: the entry is
#   marked ``needs_review`` and NO successor is created.
#
# Nothing is ever deleted here, and nothing is resubmitted blindly.
# ----------------------------------------------------------------------


@dataclass
class RunEffects:
    """What the interrupted run left behind in its workspace.

    ``decided`` is False when the effects could not be determined (a git
    probe failed, the workspace is not a repository, ...).  An undecided
    probe is never treated as "no effects": the caller marks the entry
    ``needs_review`` instead of creating a successor.
    """

    # The branch the workspace had checked out, when it had one.
    branch: str = ""
    # True when that branch exists on the remote (git ls-remote).
    branch_pushed: bool = False
    # The open PR on that branch, when one exists.
    open_pr: str = ""
    # Commits on the branch that are not on the remote's default branch.
    local_commits: int = 0
    # Modified or untracked files left in the workspace.
    dirty: bool = False
    # False when any of the above could not be determined.
    decided: bool = True

    @property
    def has_effects(self) -> bool:
        """True when the interrupted run left work that must be continued."""

        return bool(self.branch_pushed or self.open_pr or self.local_commits)

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "branch_pushed": self.branch_pushed,
            "open_pr": self.open_pr,
            "local_commits": self.local_commits,
            "dirty": self.dirty,
            "decided": self.decided,
        }


def _git_out(repo: Path, *args: str) -> tuple[bool, str | None]:
    """Run a git probe; ``(ok, output)``.

    ``ok`` is False when the command could not be run or failed, which is
    what makes an effect *undecided* rather than *absent*.
    """

    try:
        proc = subprocess.run(
            git_argv("-C", str(repo), *args),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            env=child_env(),
        )
    except (subprocess.SubprocessError, OSError):
        return False, None
    if proc.returncode != 0:
        return False, None
    return True, proc.stdout.strip()


def probe_open_pr_gh_strict(repo: Path, branch: str) -> str | None:
    """Return the OPEN PR on ``branch``, ``None``, or :data:`PROBE_UNKNOWN`.

    The recovery path's own open-PR probe, which -- unlike the launch
    guard's ``workspace_guard.probe_open_pr_gh`` -- tells "the probe could
    not answer" apart from "the probe answered: there is no open PR":

    * ``gh`` is not installed -> :data:`PROBE_UNKNOWN`;
    * ``gh`` exits non-zero -> :data:`PROBE_UNKNOWN`;
    * ``gh`` times out or cannot be spawned -> :data:`PROBE_UNKNOWN`;
    * ``gh`` answers with a PR -> that PR's description;
    * ``gh`` answers with an empty list -> ``None`` (decided: no open PR).

    The distinction matters because effect detection must never read an
    undetectable PR as an absent one: that would let the successor cut a
    fresh base and silently drop the interrupted run's published work.
    """

    gh = shutil.which("gh")
    if not gh:
        return PROBE_UNKNOWN
    # Control-plane credentials (web token, caller secrets, ask settings) must
    # not be needed by, or leak into, the probe (task A3d review H-B).
    env = child_env()
    try:
        proc = subprocess.run(
            [gh, "pr", "list", "--head", branch, "--state", "open",
             "--json", "number,title,url", "--limit", "3"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return PROBE_UNKNOWN
    except (subprocess.SubprocessError, OSError):
        return PROBE_UNKNOWN
    if proc.returncode != 0:
        return PROBE_UNKNOWN
    try:
        items = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return PROBE_UNKNOWN
    if not isinstance(items, list):
        return PROBE_UNKNOWN
    if not items:
        # The probe answered and there is no open PR: DECIDED, and distinct
        # from the unknown above.
        return None
    parts = []
    for item in items[:2]:
        if not isinstance(item, dict):
            return PROBE_UNKNOWN
        number = item.get("number")
        title = str(item.get("title") or "").strip()[:60]
        url = item.get("url")
        parts.append(f"#{number} '{title}' {url}".strip())
    return ", ".join(parts)


def detect_run_effects(
    workspace: str | Path,
    *,
    probe_pr: Callable[[Path, str], str | None] | None = probe_open_pr_gh_strict,
) -> RunEffects:
    """Detect what the interrupted run left behind in ``workspace``.

    Four independent probes, each of which may come back *undecided*:

    * the checked-out branch (``git rev-parse --abbrev-ref HEAD``);
    * whether that branch reached the remote (``git ls-remote --heads
      origin <branch>``);
    * an open PR on that branch (the recovery path's ``gh`` probe, which
      distinguishes "no open PR" from "the probe could not answer");
    * commits on the branch that never reached ``origin/main``, and
      modified/untracked files left in the tree.

    A probe that fails leaves the effect undecided rather than absent:
    ``decided`` is False and the caller must not create a successor.
    """

    repo = Path(workspace).expanduser()
    effects = RunEffects()
    if not repo.is_dir():
        # No workspace at all: nothing to continue and nothing to decide.
        return effects

    ok, inside = _git_out(repo, "rev-parse", "--is-inside-work-tree")
    if not ok:
        # Not a git repository (or git failed).  There is no branch work to
        # continue; the effect set is empty and decided.
        return effects
    if inside != "true":
        return effects

    ok, branch = _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if not ok:
        effects.decided = False
        return effects
    branch = (branch or "").strip()
    if not branch or branch == "HEAD":
        # Detached HEAD: no branch to continue.
        branch = ""
    effects.branch = branch

    if branch:
        ok, remote_heads = _git_out(repo, "ls-remote", "--heads", "origin", branch)
        if not ok:
            # The remote could not be reached: whether the branch was pushed
            # is UNDECIDABLE, and an undecided pushed-branch effect must not
            # be read as "not pushed" (that would drop the interrupted run's
            # published work).
            effects.decided = False
        else:
            effects.branch_pushed = bool(remote_heads)

    if branch and probe_pr is not None:
        try:
            open_pr = probe_pr(repo, branch)
        except Exception:  # noqa: BLE001 - a broken probe must not crash the pass
            # A probe that RAISES could not answer either: undecided, never
            # "no open PR".
            open_pr = PROBE_UNKNOWN
        if open_pr == PROBE_UNKNOWN:
            # The probe could not answer (gh missing, non-zero exit,
            # timeout).  Whether an open PR exists is UNDECIDED: the effect
            # must not be read as "no PR", because the successor would then
            # cut a fresh base and drop the interrupted run's published
            # work.  Undecided -> needs_review, NO successor.
            effects.decided = False
        else:
            effects.open_pr = str(open_pr or "")

    # Commits on the branch that never reached the remote's default branch.
    ok, origin_main = _git_out(repo, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main")
    if not ok:
        # No origin/main ref to compare against: the local-commit effect is
        # undecided (the same fail-open-but-not-guessing discipline the
        # launcher's own occupancy probe uses).
        effects.decided = False
    elif branch:
        ok, count = _git_out(repo, "rev-list", "--count", f"refs/remotes/origin/main..{branch}")
        if not ok:
            effects.decided = False
        else:
            try:
                effects.local_commits = int(count or "0")
            except ValueError:
                effects.decided = False

    ok, status = _git_out(repo, "status", "--porcelain")
    if not ok:
        effects.decided = False
    else:
        effects.dirty = bool((status or "").strip())

    return effects


def workspace_worker_is_alive(workspace: str | Path, run_id: str) -> bool:
    """True when a worker process for ``run_id`` is still alive.

    The gate is by RUN ID, not by pid: the interrupted run's worker may
    have been re-parented and its pid recycled, so the worker's command
    line is matched for the run id.  This is the "never two workers for one
    task" gate: a successor must not launch while the interrupted run's
    own worker is still running.
    """

    repo = Path(workspace).expanduser()
    if not repo.is_dir():
        return False
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            env=child_env(),
        )
    except (subprocess.SubprocessError, OSError):
        return False
    if proc.returncode != 0:
        return False
    needle = f"--run-id={run_id}"
    for line in (proc.stdout or "").splitlines():
        if needle in line and "lh_harness" in line:
            return True
    return False


def log_lineage_in_report(
    runs_root: str | Path,
    run_id: str,
    lineage: dict[str, Any],
) -> bool:
    """Write the restart-recovery lineage into the run's durable report."""

    return log_stash_in_report(
        runs_root,
        run_id,
        str(lineage.get("stash") or ""),
        workspace=str(lineage.get("stash_workspace") or ""),
        lineage=lineage,
    )



def reconcile_interrupted_run(
    queue_store: Any,
    entry: Any,
    *,
    runs_root: str | Path,
    epoch: ServiceEpoch | None,
    status: dict[str, Any] | None = None,
    owner: dict[str, Any] | None = None,
    cause: str = INTERRUPTED_BY_RESTART,
    probe_pr: Callable[[Path, str], str | None] | None = probe_open_pr_gh_strict,
) -> Any | None:
    """Create the single ``-r1`` successor for a restart-interrupted entry.

    Returns the successor entry, or ``None`` when no successor was created —
    either because the death is not restart-interrupted, or because a
    successor already exists (idempotent), or because the entry already had
    its one restart second chance, or because the interrupted run's effects
    could not be determined (the entry is then marked ``needs_review``).

    The order of the steps is load-bearing:

    1. the death is classified as restart-interrupted;
    2. the reconcile is checked for idempotency and for the entry's one
       second chance;
    3. the interrupted run's EFFECTS are detected — a pushed branch, an
       open PR, local commits, a dirty tree.  An effect that cannot be
       decided marks the entry ``needs_review`` and creates NO successor;
       a pushed branch or an open PR makes the successor CONTINUE that
       branch (``continue_branch``);
    4. the workspace is stashed (the stash is kept) and the stash name is
       logged in the run's report;
    5. the successor is created with the lineage (``retry_of``,
       ``interrupted_run``), and the original entry records the successor's
       queue id.
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

    # --- step 3: detect the interrupted run's effects BEFORE creating any
    # successor.  An effect that cannot be determined is never guessed.
    effects = detect_run_effects(workspace, probe_pr=probe_pr) if workspace else RunEffects()
    if not effects.decided:
        _mark_needs_review(queue_store, entry, original_queue_id)
        return None

    # --- step 4: the workspace is cleaned with a KEPT stash.
    stash_name = ""
    if workspace and run_id:
        stash_name = stash_interrupted_workspace(workspace, run_id) or ""

    # A pushed branch or an open PR means the interrupted run's work is
    # already published: the successor must CONTINUE that branch instead of
    # cutting a fresh base (which would drop the published work).
    continue_branch = bool(effects.branch_pushed or effects.open_pr)

    # --- step 5: the one successor, with the lineage.
    successor = queue_store.requeue(
        original_queue_id,
        cause,
        name_suffix=RETRY_NAME_SUFFIX,
        interrupted_run=run_id or None,
        continue_branch=True if continue_branch else None,
    )
    if successor is None:
        return None

    # The original entry records its successor, so the lineage is readable
    # from either end.
    _record_successor(queue_store, entry, successor.queue_id)

    lineage = {
        "original_queue_id": original_queue_id,
        "successor_queue_id": successor.queue_id,
        "interrupted_run": run_id,
        "stash": stash_name,
        "stash_workspace": workspace,
        "effects": effects.as_dict(),
        "continue_branch": continue_branch,
    }
    if run_id:
        log_lineage_in_report(runs_root, run_id, lineage)
    return successor


def _mark_needs_review(queue_store: Any, entry: Any, original_queue_id: str) -> None:
    """Mark the entry ``needs_review``; create NO successor.

    The interrupted run's effects could not be determined, so the recovery
    does not know whether a successor would drop published work.  The entry
    is flagged for a human instead, and nothing is deleted and nothing is
    resubmitted blindly.
    """

    try:
        entry.needs_review = True
        entry.reason = NEEDS_REVIEW_REASON[:_MAX_REASON_LEN]
        queue_store.update(entry)
    except Exception:  # noqa: BLE001 - the flag is best-effort, never fatal
        return


def _record_successor(queue_store: Any, entry: Any, successor_queue_id: str) -> None:
    """Record ``successor`` on the original entry."""

    try:
        entry.successor = successor_queue_id
        queue_store.update(entry)
    except Exception:  # noqa: BLE001 - the lineage link is best-effort
        return
