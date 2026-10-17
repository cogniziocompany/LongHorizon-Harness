"""Enforced auditor command boundary, shared by audit and format repair.

The entire host filesystem is read-only; only a fresh episode scratch directory
is writable. Availability is established on the execution environment, not on
the manager host. No fallback to an ordinary shell is permitted.

Two boundaries are layered together so that both the mount namespace and
the auditor uid are independent of the harness:

* ``bwrap --unshare-user --ro-bind / / ...`` — the host filesystem is
  read-only to the auditor and only ``scratch`` is writable.
* ``unshare -U`` (or ``setpriv --reuid`` for separate-uid deployments) —
  the auditor subprocess runs under a different uid than the harness, so
  the kernel permission check on every file operation is the trustworthy
  enforcement (chmod a-w on harness-owned files is advisory; same-uid
  processes can reverse it).

When EITHER boundary is missing the harness refuses the auditor launch
and surfaces a missing-prerequisite error. The launch is wired into the
real manager -> adapter -> spawn path through
:func:`lh_harness.manager._run_role_episode`, and the mount wrapper is
re-checked at every call so a deployment change between round boundaries
fails closed.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import shlex

from .safe_subprocess import (
    AuditorBoundary,
    AuditorBoundaryUnavailable,
    auditor_boundary_required,
)


@dataclass(frozen=True)
class AuditorLaunch:
    """Per-episode scratch the auditor can write to, plus the OS-level
    uid/namespace boundary the spawning process must wrap the auditor
    command in.

    Attributes
    ----------
    scratch:
        Writable directory inside the read-only bwrap mount. The audit
        subprocess's stdout/stderr, transient test artifacts and
        ``LH_AUDITOR_*`` probes live here.
    boundary:
        The detected OS-level isolation boundary. ``CommandAgentAdapter``
        prepends ``boundary.launcher`` to the wrapped command so the
        audit subprocess enters the user namespace (USER_NAMESPACE) or
        re-uid's (SEPARATE_UID) before the bwrap mount boundary runs.
    """

    scratch: str
    boundary: AuditorBoundary

    def command(self, command: str) -> str:
        """Wrap ``command`` so it runs inside the mount + uid boundaries.

        The command is run under the boundary launcher (so the auditor
        gets the kernel-isolated uid) which then exec's ``bwrap`` (so
        the harness filesystem is read-only and only ``scratch`` is
        writable). Both boundaries are required; see
        :func:`prepare_auditor_launch`.
        """
        scratch = shlex.quote(self.scratch)
        wrapped = (
            "/usr/bin/bwrap --die-with-parent --new-session --unshare-user "
            "--unshare-pid --unshare-ipc --unshare-uts --cap-drop ALL "
            "--ro-bind / / --proc /proc --dev /dev "
            f"--bind {scratch} {scratch} --setenv HOME {scratch} "
            f"--setenv TMPDIR {scratch} -- /bin/sh -c {shlex.quote(command)}"
        )
        # The boundary launcher's argv is prepended unquoted because
        # ``shlex.quote`` would break the launcher argv shape (the
        # launcher needs its own -U or --reuid flag as a separate argv
        # element). The launcher is a path the harness discovered via
        # ``shutil.which``; running it through shlex.quote still leaves a
        # working executable path on every platform we target.
        launcher = self.boundary.launcher
        prefix_argv = _boundary_prefix_argv(launcher)
        if not prefix_argv:
            return wrapped
        quoted = " ".join(_quote_argv_part(part) for part in prefix_argv)
        return f"{quoted} {wrapped}"


def _quote_argv_part(part: str) -> str:
    """Quote one argv element for embedding in a shell string.

    The boundary launcher (``unshare`` / ``setpriv`` / ``runuser``) is
    invoked through the harness via ``env.exec`` which uses
    ``asyncio.create_subprocess_shell`` — so the argv has to round-trip
    through the shell. We use ``shlex.quote`` for every element so the
    launcher receives exactly the argv it expects.
    """
    return shlex.quote(part)


def _boundary_prefix_argv(launcher: str | None) -> list[str]:
    """Return the argv prefix that places the auditor in its boundary.

    The launcher is the path returned by :func:`shutil.which` for
    ``unshare`` / ``setpriv`` / ``runuser``. The actual flag set used
    here MUST match what :func:`launch_auditor_subprocess` uses for the
    ``Popen`` probe, otherwise the boundary detection and the real
    launch would disagree and the manager could ship a round whose
    isolation is weaker than the probe claimed.
    """
    if not launcher:
        return []
    name = launcher.rsplit("/", 1)[-1]
    if name == "unshare":
        return [launcher, "-U"]
    if name == "setpriv":
        return [
            launcher,
            "--reuid=65534",
            "--regid=65534",
            "--clear-groups",
        ]
    if name == "runuser":
        return [launcher, "-u", "nobody"]
    # Unknown launcher: refuse to wrap rather than risk a silent chmod/
    # command-deny-list fallback (those are not a kernel boundary).
    raise AuditorBoundaryUnavailable(
        f"unknown auditor boundary launcher {launcher!r}; "
        "the harness refuses to fall back to chmod/deny lists because "
        "those are not a kernel boundary"
    )


active_auditor_launch: ContextVar[AuditorLaunch | None] = ContextVar(
    "active_auditor_launch", default=None
)


async def prepare_auditor_launch(env) -> AuditorLaunch:
    """Establish the mount + uid boundary on the execution environment.

    Two conditions must hold for an auditor launch:

    1. :func:`auditor_boundary_required` returns a real
       :class:`AuditorBoundary` (USER_NAMESPACE or SEPARATE_UID with a
       different probe_uid than the calling uid). UNAVAILABLE fails
       closed with ``AuditorBoundaryUnavailable``.
    2. The mount boundary probe (``bwrap`` + read-only root + writable
       scratch) completes inside the env. The exact invariant we verify
       is: inside the bwrap, writing to ``scratch`` succeeds AND writing
       to the harness root fails. Both must be observed; otherwise a
       launcher-side bug could spuriously produce the marker.

    Either condition failing rejects the launch, so the auditor
    subprocess runs zero times when any prerequisite is missing. The
    format-repair pass goes through the same code path so the zero-launch
    guarantee applies to both the primary audit and its repair pass.
    """
    # Condition 1: uid/namespace boundary must exist.
    boundary = auditor_boundary_required()
    # Condition 2: bwrap mount boundary must exist on this execution host.
    made = await env.exec(
        "/usr/bin/mktemp -d /tmp/lh-auditor-launch-XXXXXXXX",
        timeout=10,
    )
    scratch = made.stdout.strip()
    if made.exit_code != 0 or not scratch.startswith("/tmp/lh-auditor-launch-") or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-"
        for char in scratch
    ) or "/" in scratch[len("/tmp/"):]:
        raise AuditorBoundaryUnavailable("cannot create private auditor scratch")
    launch = AuditorLaunch(scratch, boundary)
    # The bwrap probe is the kernel-side verification of the read-only
    # boundary: writing to the (bwrap-bind) ``scratch`` dir succeeds AND
    # writing to the (bwrap-ro-bind) harness root fails. A launcher
    # failure cannot accidentally produce both markers.
    probe = await env.exec(launch.command(
        f"set -e; printf ok > {shlex.quote(scratch + '/probe')}; "
        "if touch /lh-auditor-root-write-probe 2>/dev/null; then exit 1; fi; "
        "printf LH_AUDITOR_MOUNT_READY"
    ), timeout=10)
    if probe.exit_code != 0 or probe.stdout.strip() != "LH_AUDITOR_MOUNT_READY":
        raise AuditorBoundaryUnavailable(
            "read-only mount boundary unavailable on execution host"
        )
    return launch
