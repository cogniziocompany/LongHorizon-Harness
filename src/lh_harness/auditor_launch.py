"""Enforced auditor command boundary, shared by audit and format repair.

The entire host filesystem is read-only; only a fresh episode scratch directory
is writable. Availability is established on the execution environment, not on
the manager host. No fallback to an ordinary shell is permitted.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import shlex

from .safe_subprocess import AuditorBoundaryUnavailable


@dataclass(frozen=True)
class AuditorLaunch:
    scratch: str

    def command(self, command: str) -> str:
        scratch = shlex.quote(self.scratch)
        return (
            "/usr/bin/bwrap --die-with-parent --new-session --unshare-user "
            "--unshare-pid --unshare-ipc --unshare-uts --cap-drop ALL "
            "--ro-bind / / --proc /proc --dev /dev "
            f"--bind {scratch} {scratch} --setenv HOME {scratch} "
            f"--setenv TMPDIR {scratch} -- /bin/sh -c {shlex.quote(command)}"
        )


active_auditor_launch: ContextVar[AuditorLaunch | None] = ContextVar(
    "active_auditor_launch", default=None
)


async def prepare_auditor_launch(env) -> AuditorLaunch:
    # No agent-controlled command or path is interpolated into this probe.
    made = await env.exec("/usr/bin/mktemp -d /tmp/lh-auditor-launch-XXXXXXXX", timeout=10)
    scratch = made.stdout.strip()
    if made.exit_code != 0 or not scratch.startswith("/tmp/lh-auditor-launch-") or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-" for char in scratch
    ) or "/" in scratch[len("/tmp/"):]:
        raise AuditorBoundaryUnavailable("cannot create private auditor scratch")
    launch = AuditorLaunch(scratch)
    # Positive scratch write and explicit read-only-root rejection are both
    # required. A launcher failure cannot accidentally produce the marker.
    probe = await env.exec(launch.command(
        f"set -e; printf ok > {shlex.quote(scratch + '/probe')}; "
        "if touch /lh-auditor-root-write-probe 2>/dev/null; then exit 1; fi; "
        "printf LH_AUDITOR_MOUNT_READY"
    ), timeout=10)
    if probe.exit_code != 0 or probe.stdout.strip() != "LH_AUDITOR_MOUNT_READY":
        raise AuditorBoundaryUnavailable("read-only mount boundary unavailable on execution host")
    return launch
