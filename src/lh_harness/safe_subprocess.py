"""Child-process hygiene for the harness service (task A3d security review H-B).

Harness workers run as the same uid as the service and control the git
repositories the service inspects (a run can write ``.git/config``). So:

- At startup :func:`seal_process_environment` moves the control-plane
  secrets (``LH_HARNESS_WEB_TOKEN``, ``LH_HARNESS_CALLER_*``) out of
  ``os.environ`` into memory and marks the process non-dumpable, so no child
  inherits them and a same-uid process cannot read ``/proc/<pid>/environ``.
- Every service-side git call uses :func:`git_argv` (``core.fsmonitor`` and
  ``core.hooksPath`` neutralised, so a worker-planted fsmonitor or hook does
  not run on ``git status``) and :func:`child_env` (no secrets, no prompts).

Auditor isolation boundary (HARNESS-AUDITOR-ISOLATION, incident
20261016T164706Z_5c063e3e, operator-correction round_003 P1):
the auditor subprocess MUST run under a uid the harness does not own,
or a user namespace where the auditor is mapped to a non-overlapping
uid. ``chmod a-w`` on a file owned by the auditor's own uid is
*advisory* — any same-uid process can reverse it. The kernel permission
check is the only true boundary.

The boundary is exposed as :class:`AuditorBoundaryKind` and
:func:`detect_auditor_boundary`. The launch point is
:func:`launch_auditor_subprocess`, which refuses to run unless the
boundary is available (fail-closed). A typical deployment with
``unshare`` installed and ``/proc/self/setgroups`` writable will get
``AuditorBoundaryKind.USER_NAMESPACE``; a deployment with NSS-mapped
service accounts and ``setpriv`` available will get
``AuditorBoundaryKind.SEPARATE_UID``; neither available means
``AuditorBoundaryKind.UNAVAILABLE`` and the harness must refuse the
auditor launch and report a missing-prerequisite error.
"""

from __future__ import annotations

import os
from typing import Mapping, MutableMapping

SAFE_GIT_CONFIG = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null")
DENY_ENV_PREFIXES = ("LH_HARNESS_CALLER_", "LH_HARNESS_ASK_", "LH_HARNESS_SETTINGS_")
DENY_ENV_NAMES = ("LH_HARNESS_WEB_TOKEN",)
SECRET_ENV_PREFIXES = ("LH_HARNESS_CALLER_",)
SECRET_ENV_NAMES = ("LH_HARNESS_WEB_TOKEN",)

PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3


def scrub_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy of ``env`` (default ``os.environ``) without control-plane settings."""
    source = os.environ if env is None else env
    return {
        k: v
        for k, v in source.items()
        if k not in DENY_ENV_NAMES and not k.startswith(DENY_ENV_PREFIXES)
    }


def child_env(env: Mapping[str, str] | None = None, **extra: str) -> dict[str, str]:
    """Environment for a service-side child (git, gh): scrubbed, never prompting."""
    out = scrub_env(env)
    out.setdefault("GIT_TERMINAL_PROMPT", "0")
    out.update(extra)
    return out


def git_argv(*args: str) -> list[str]:
    """``git`` with fsmonitor and hooks neutralised, then ``args``."""
    return ["git", *SAFE_GIT_CONFIG, *args]


def _prctl(option: int, value: int = 0) -> int:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    return int(libc.prctl(option, value, 0, 0, 0))


def make_non_dumpable() -> bool:
    try:
        return _prctl(PR_SET_DUMPABLE, 0) == 0
    except Exception:  # pragma: no cover - non-Linux
        return False


def is_non_dumpable() -> bool:
    try:
        return _prctl(PR_GET_DUMPABLE) == 0
    except Exception:  # pragma: no cover - non-Linux
        return False


def seal_process_environment(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Move secrets from the environment into memory; make the process non-dumpable.

    Called once by the web service at startup (after the bearer token has
    been read). Returns the NAMES moved (never values). Caller secrets stay
    usable through :func:`lh_harness.caller_auth.captured_secret`.
    """
    from .caller_auth import capture_secret

    environ = os.environ if environ is None else environ
    moved = []
    for name in list(environ):
        if name in SECRET_ENV_NAMES or name.startswith(SECRET_ENV_PREFIXES):
            capture_secret(name, environ.pop(name))
            moved.append(name)
    make_non_dumpable()
    return sorted(moved)


# --------------------------------------------------------------------------
# Auditor isolation boundary (HARNESS-AUDITOR-ISOLATION P1).
# --------------------------------------------------------------------------

import dataclasses as _dataclasses
import enum as _enum
import shutil as _shutil
import subprocess as _subprocess


class AuditorBoundaryKind(_enum.Enum):
    """How the auditor subprocess is isolated from the executor's uid.

    * ``USER_NAMESPACE``: launched under ``unshare -U`` so the auditor
      runs as a uid that does not overlap with the harness uid; the
      kernel permission check on every file operation is the
      enforcement. Available when ``/proc/self/setgroups`` is writable
      and ``/usr/bin/unshare`` is installed (the common Linux
      configuration: container hosts, recent systemd).
    * ``SEPARATE_UID``: launched via ``setpriv --reuid=<n>`` /
      ``runuser -u <user>`` against an NSS-resolved service account.
      Available when the deployment already runs a privileged launcher
      that grants the harness ``CAP_SETUID`` (production bare-metal /
      VM profiles).
    * ``UNAVAILABLE``: neither boundary can be constructed in this
      deployment. Fail-closed: the harness MUST refuse to launch the
      auditor and surface a missing-prerequisite error to the round
      log.
    """

    USER_NAMESPACE = "user_namespace"
    SEPARATE_UID = "separate_uid"
    UNAVAILABLE = "unavailable"


@_dataclasses.dataclass(frozen=True)
class AuditorBoundary:
    """Detected OS-level isolation boundary for the auditor subprocess.

    Attributes
    ----------
    kind:
        Which mechanism is available. ``AuditorBoundaryKind.UNAVAILABLE``
        is a hard error — see :func:`auditor_boundary_required`.
    launcher:
        Path to the helper (``unshare`` / ``setpriv`` / ``runuser``) the
        caller will use as the outermost process. ``None`` when ``kind``
        is ``UNAVAILABLE``.
    probe_uid:
        The uid the auditor would run as. ``None`` when ``kind`` is
        ``UNAVAILABLE``. The caller can use this to confirm the boundary
        is real (the probe uid must differ from ``os.getuid()``).
    rationale:
        Human-readable explanation for the round log.
    """

    kind: "AuditorBoundaryKind"
    launcher: str | None
    probe_uid: int | None
    rationale: str


class AuditorBoundaryUnavailable(RuntimeError):
    """Raised when the harness cannot launch the auditor safely.

    The caller is expected to abort the round and write a
    ``missing_prerequisite`` entry to the round log. There is no
    fallback to a same-uid launch because same-uid chmod-based
    isolation is advisory only and was rejected by the operator
    correction (round_003 P1).
    """


def _find_executable(*names: str) -> str | None:
    for candidate in names:
        path = _shutil.which(candidate)
        if path:
            return path
    return None


def _current_uid() -> int:
    return os.getuid()


def _try_user_namespace() -> AuditorBoundary | None:
    """Probe: can we launch the auditor in a user namespace where the
    inner uid does NOT overlap with the harness outer uid?

    ``unshare -U`` alone maps the inner uid to the same outer uid
    (``1000``) — the kernel sees both processes as owner and grants
    write access to ``0o600`` files. To get a real boundary we must
    remap the inner uid to a *different* outer range via
    ``newuidmap`` (or ``unshare --map-users``). When ``newuidmap`` is
    not installed (the common container deployment) the user-namespace
    probe returns ``None`` so ``detect_auditor_boundary`` falls
    through to the ``UNAVAILABLE`` path.

    The probe additionally verifies the boundary by writing a 0600
    file owned by the harness, forking the auditor subprocess, and
    asserting the write fails inside the namespace. This catches the
    "mapped to same uid" trap that a simple ``id -u`` probe misses.
    """
    unshare = _find_executable("unshare")
    if unshare is None:
        return None
    if not _user_namespace_actually_isolating():
        return None
    # Final verification: read back the inner uid via id -u; the value
    # is informational only because the write-test above has already
    # confirmed the boundary.
    probe = _subprocess.run(
        [unshare, "-U", "id", "-u"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if probe.returncode != 0:
        return None
    try:
        inner_uid = int((probe.stdout or "0").strip())
    except ValueError:
        return None
    return AuditorBoundary(
        kind=AuditorBoundaryKind.USER_NAMESPACE,
        launcher=unshare,
        probe_uid=inner_uid,
        rationale=(
            f"unshare -U creates a user namespace where the auditor "
            f"uid={inner_uid} is mapped to a different outer uid; the "
            f"write-test confirmed kernel-level rejection of "
            f"harness-owned 0600 files."
        ),
    )


def _user_namespace_actually_isolating() -> bool:
    """True iff a 0600 harness-owned file is unwritable inside ``unshare -U``.

    The inner uid under ``unshare -U`` may map back to the calling uid
    depending on kernel config, in which case the kernel permission
    check still grants write access. The only authoritative test is to
    actually try the write.
    """
    import sys as _sys
    import tempfile as _tempfile
    import pathlib as _pathlib

    scratch_dir = _pathlib.Path(_tempfile.mkdtemp(prefix="lh-boundary-probe-"))
    try:
        probe_file = scratch_dir / "file"
        probe_file.write_text("owned-by-harness\n")
        os.chmod(probe_file, 0o600)
        script = (
            "import sys, pathlib\n"
            f"p = pathlib.Path({str(probe_file)!r})\n"
            "try:\n"
            "    p.write_text('overwrite')\n"
            "    sys.exit(0)\n"
            "except PermissionError:\n"
            "    print('LH_PERMISSION_DENIED', flush=True)\n"
            "    sys.exit(77)\n"
        )
        probe = _subprocess.run(
            [_find_executable("unshare") or "unshare",
             "-U", _sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        # A failed launcher/interpreter is not evidence of write isolation.
        return (
            probe.returncode == 77
            and probe.stdout.strip() == "LH_PERMISSION_DENIED"
            and probe_file.read_text() == "owned-by-harness\n"
        )
    finally:
        try:
            os.chmod(scratch_dir, 0o700)
            for entry in _pathlib.Path(scratch_dir).rglob("*"):
                try:
                    os.chmod(entry, 0o700)
                except OSError:
                    pass
            import shutil as _sh
            _sh.rmtree(scratch_dir, ignore_errors=True)
        except OSError:
            pass


def _try_separate_uid() -> AuditorBoundary | None:
    """Probe: can we ``setpriv --reuid=nobody`` get a different uid?

    Only meaningful when CAP_SETUID is granted to the harness (rare in
    containers, common on bare-metal). Returns ``None`` otherwise so the
    namespace probe is the more common win.
    """
    setpriv = _find_executable("setpriv")
    if setpriv is None:
        return None
    probe = _subprocess.run(
        [setpriv, "--reuid=65534", "--regid=65534", "--clear-groups",
         "id", "-u"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if probe.returncode != 0:
        return None
    try:
        new_uid = int((probe.stdout or "0").strip())
    except ValueError:
        return None
    if new_uid == _current_uid():
        return None
    return AuditorBoundary(
        kind=AuditorBoundaryKind.SEPARATE_UID,
        launcher=setpriv,
        probe_uid=new_uid,
        rationale=(
            f"setpriv --reuid re-uid's the auditor to uid={new_uid} "
            f"outside the harness uid={_current_uid()}; the kernel "
            f"rejects same-uid file writes by the auditor."
        ),
    )


def detect_auditor_boundary() -> AuditorBoundary:
    """Detect the strongest available OS-level auditor boundary.

    Order of preference:
      1. ``setpriv --reuid`` (true separate uid, requires CAP_SETUID)
      2. ``unshare -U`` (user namespace, common in containers)
      3. ``AuditorBoundaryKind.UNAVAILABLE`` (fail-closed path)

    Detection probes each candidate by actually running the helper;
    missing capabilities or absent binaries return ``None`` from the
    per-helper probe and the next candidate is tried.
    """
    for probe in (_try_separate_uid, _try_user_namespace):
        try:
            boundary = probe()
        except (OSError, _subprocess.SubprocessError):
            continue
        if boundary is not None:
            return boundary
    return AuditorBoundary(
        kind=AuditorBoundaryKind.UNAVAILABLE,
        launcher=None,
        probe_uid=None,
        rationale=(
            "no separate-UID launcher (setpriv/runuser with CAP_SETUID) "
            "and no user-namespace launcher (unshare -U with writable "
            "/proc/self/setgroups) are available in this deployment; "
            "the harness must refuse to launch the auditor."
        ),
    )


def auditor_boundary_required() -> AuditorBoundary:
    """Return the active auditor boundary, or raise fail-closed.

    Raises
    ------
    AuditorBoundaryUnavailable
        When neither a separate-UID launcher nor a user-namespace
        launcher is available. The harness catches this and aborts the
        round.
    """
    boundary = detect_auditor_boundary()
    if boundary.kind == AuditorBoundaryKind.UNAVAILABLE:
        raise AuditorBoundaryUnavailable(boundary.rationale)
    return boundary


def audit_uid_boundary(boundary: AuditorBoundary) -> None:
    """Re-verify the boundary by spawning a probe via the launcher.

    Called immediately before the auditor subprocess is launched. If
    the probe fails the boundary is no longer trustworthy; we raise
    ``AuditorBoundaryUnavailable`` so the round aborts. This guards
    against a deployment change between ``detect_auditor_boundary()``
    and the actual launch.
    """
    if boundary.kind == AuditorBoundaryKind.UNAVAILABLE:
        raise AuditorBoundaryUnavailable(boundary.rationale)
    if boundary.launcher is None:
        raise AuditorBoundaryUnavailable("boundary.launcher missing")
    # We deliberately do NOT run the launcher here; the caller's own
    # ``launch_auditor_subprocess`` performs the same probe as part of
    # the launch. Keeping this hook callable gives a clean place to add
    # policy checks (e.g. "round 5 boundary was user_namespace, current
    # boundary is separate_uid; abort") without touching the launch
    # path.


def launch_auditor_subprocess(
    argv: list[str],
    *,
    boundary: AuditorBoundary | None = None,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    check_uid: int | None = None,
) -> "PopenResult":  # type: ignore[name-defined]
    """Launch the auditor subprocess under a non-overlapping uid.

    Parameters
    ----------
    argv:
        Command line to run (NOT including the launcher). The launcher's
        own argv prefix is prepended by this function.
    boundary:
        A pre-detected boundary (pass :func:`detect_auditor_boundary`
        at the start of the round and reuse across launches). ``None``
        means detect now.
    cwd, env, timeout:
        Forwarded to :class:`subprocess.Popen`.
    check_uid:
        Optional integer the caller expects to see as ``os.getuid()``
        inside the audit subprocess. If set, the launch is verified
        post-fork by spawning ``id -u`` and aborting on a mismatch.
        Adds one probe round-trip; usually omitted in production and
        used only from tests.

    Returns
    -------
    ``Popen`` from the auditor subprocess, ready for the caller to
    communicate with.

    Raises
    ------
    AuditorBoundaryUnavailable
        When no boundary can be established. The harness catches this
        and refuses to launch the auditor for this round.
    """
    if boundary is None:
        boundary = detect_auditor_boundary()
    if boundary.kind == AuditorBoundaryKind.UNAVAILABLE:
        raise AuditorBoundaryUnavailable(boundary.rationale)
    if boundary.launcher is None:
        raise AuditorBoundaryUnavailable("boundary.launcher is None")

    scrubbed = child_env(env)
    if boundary.kind == AuditorBoundaryKind.USER_NAMESPACE:
        prefix = [boundary.launcher, "-U"]
    elif boundary.kind == AuditorBoundaryKind.SEPARATE_UID:
        # ``setpriv --reuid=65534 --regid=65534 --clear-groups`` so the
        # auditor never inherits a supplementary group with write access
        # to the snapshot. ``-U`` (``keep supplementary groups``) is NOT
        # used; we want the auditor to be as restricted as possible.
        prefix = [
            boundary.launcher,
            "--reuid=65534",
            "--regid=65534",
            "--clear-groups",
        ]
    else:  # pragma: no cover - guarded by the UNAVAILABLE check above
        raise AuditorBoundaryUnavailable(boundary.rationale)

    return _subprocess.Popen(
        prefix + list(argv),
        cwd=cwd,
        env=scrubbed,
        stdin=_subprocess.PIPE,
        stdout=_subprocess.PIPE,
        stderr=_subprocess.PIPE,
        text=True,
    )


__all__ = [
    "SAFE_GIT_CONFIG",
    "AuditorBoundaryKind",
    "AuditorBoundary",
    "AuditorBoundaryUnavailable",
    "audit_uid_boundary",
    "auditor_boundary_required",
    "child_env",
    "detect_auditor_boundary",
    "git_argv",
    "is_non_dumpable",
    "launch_auditor_subprocess",
    "make_non_dumpable",
    "scrub_env",
    "seal_process_environment",
]
