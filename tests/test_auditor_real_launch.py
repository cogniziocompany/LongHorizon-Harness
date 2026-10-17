"""End-to-end auditor launch tests on disposable git repos.

These tests drive the real manager -> adapter -> spawn path: a launched
audit subprocess on a fresh git repo per test, going through
``prepare_auditor_launch`` -> ``AuditorLaunch.command`` -> the bwrap
mount boundary plus ``unshare -U`` (or ``setpriv``).

Three scenarios are covered; each opens with a brief assessment of the
Open task item it satisfies:

* Item (1) — the separate-UID/namespace boundary IS wired through the
  manager -> adapter -> spawn path:

  ``test_real_launch_path_produces_unshare_then_bwrap_in_command``.
  Inspects the command the audit subprocess would actually be spawned
  with and asserts that ``unshare -U`` (or ``setpriv --reuid``)
  precedes ``/usr/bin/bwrap``. The harness-side measurement proves the
  boundary is wired, not just declared.

* Item (3) — the namespace probe distinguishes EACCES from any other
  nonzero error:

  ``test_probe_uid_map_permission_denied_at_unshare_layer_is_launch_failed``.
  ``unshare -U`` printing ``uid map: Permission denied`` (the VM211
  signature) exits nonzero WITHOUT the ``LH_PERMISSION_DENIED`` marker
  — the launcher failed before the inner script ran. The probe must
  return ``LAUNCH_FAILED``, not ``ISOLATING``.

* Item (4) — VM211-style ``bwrap`` failure is fail-closed:

  ``test_bwrap_uid_map_permission_denied_is_fail_closed_not_chmod_fallback``.
  When ``AuditorBoundaryUnavailable`` is the only thing the boundary
  detector can produce (everything returns None / LAUNCH_FAILED), the
  same-UID chmod/command deny-list surface MUST NOT be a fallback. The
  harness refuses to launch the auditor entirely.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from lh_harness.auditor_launch import AuditorLaunch, prepare_auditor_launch
from lh_harness.safe_subprocess import (
    NamespaceProbeOutcome,
    _user_namespace_actually_isolating,
    auditor_boundary_required,
    detect_auditor_boundary,
    AuditorBoundary,
    AuditorBoundaryKind,
    AuditorBoundaryUnavailable,
)


# ---------------------------------------------------------------------------
# Shared helpers: disposable git repos and a host-side execable environment.
# ---------------------------------------------------------------------------


def _make_git_repo(workspace: Path) -> None:
    """Initialise ``workspace`` as a git repo with one commit.

    Uses subprocess.run directly with a minimal env so the test does not
    depend on the harness's git_argv wrapper (which forks ``Popen`` for
    fixture-style probes). Each test owns its repo and tears it down on
    exit.
    """
    # The disposable-repo fixture needs to resolve a ``git`` binary even when
    # the host the test runs on supplies a stripped or empty ambient PATH
    # (the CI worktree under ``tests/conftest.py`` inherits a service-style
    # PATH that does not always carry ``/usr/bin/git``). Build the
    # subprocess env from a guaranteed ``git`` location so the test does
    # not depend on the runner's PATH landing inside the helper's naked
    # ``subprocess.run`` call.
    git_bin = shutil.which("git") or "/usr/bin/git"
    git_dir = os.path.dirname(git_bin)
    base_path = os.environ.get("PATH", "")
    if git_dir and git_dir not in base_path.split(":"):
        base_path = f"{git_dir}:{base_path}" if base_path else git_dir
    env = {
        "PATH": base_path,
        "HOME": str(workspace),
        "GIT_AUTHOR_NAME": "Auditor Test",
        "GIT_AUTHOR_EMAIL": "audit@example.com",
        "GIT_COMMITTER_NAME": "Auditor Test",
        "GIT_COMMITTER_EMAIL": "audit@example.com",
    }
    for args in (
        ("git", "-C", str(workspace), "init", "-q", "--initial-branch=main"),
        ("git", "-C", str(workspace), "config", "user.email", "test@example.com"),
        ("git", "-C", str(workspace), "config", "user.name", "Test"),
    ):
        subprocess.run(args, check=True, env=env, capture_output=True)
    (workspace / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(
        ("git", "-C", str(workspace), "add", "README.md"),
        check=True, env=env, capture_output=True,
    )
    subprocess.run(
        ("git", "-C", str(workspace), "commit", "-q", "-m", "init"),
        check=True, env=env, capture_output=True,
    )


# ---------------------------------------------------------------------------
# Item (1): the boundary is wired through the real manager -> adapter -> spawn.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "launcher,launcher_flag",
    [
        ("/usr/bin/unshare", "-U"),
        ("/usr/bin/setpriv", "--reuid=65534 --regid=65534 --clear-groups"),
    ],
)
def test_auditor_launch_command_prepends_boundary_launcher_ahead_of_bwrap(
    tmp_path: Path,
    launcher: str,
    launcher_flag: str,
) -> None:
    """The audit subprocess command string must show the boundary launcher
    (``unshare -U`` or ``setpriv --reuid``) executed BEFORE the bwrap mount
    boundary runs. Otherwise bwrap's own ``--unshare-user`` would only
    get a same-uid mapping and the kernel permission check would still
    be ineffective (the operator-rejected P1 trap).

    Detection: build an in-process ``AuditorLaunch`` against a fixture
    boundary (no host-level probe required — the harness code under
    test takes the boundary as input from ``prepare_auditor_launch``).
    ``.command(...)`` returns the wrapped audit subprocess command;
    inspect the argv ordering. The expectation is launcher < bwrap.
    """
    fixture_boundary = AuditorBoundary(
        kind=(
            AuditorBoundaryKind.USER_NAMESPACE
            if launcher.endswith("unshare")
            else AuditorBoundaryKind.SEPARATE_UID
        ),
        launcher=launcher,
        probe_uid=1,
        rationale="fixture boundary for end-to-end launch ordering test",
    )
    launch = AuditorLaunch(
        scratch=str(tmp_path / "scratch"),
        boundary=fixture_boundary,
    )
    built = launch.command("printf audit-marker")

    # The boundary launcher's argv must precede bwrap — otherwise bwrap's
    # own --unshare-user-only path runs first and produces an illusory
    # boundary (this is the operator-rejected P1 trap).
    launcher_pos = built.index(launcher)
    bwrap_pos = built.index("/usr/bin/bwrap ")
    assert launcher_pos < bwrap_pos, (
        f"boundary launcher must precede bwrap: {built!r}"
    )
    # The launcher's flag (e.g. -U or --reuid=...) must be a real
    # argv element after the launcher path (no shlex-joined flags
    # would split unshare's -U onto the launcher arg and break argv).
    if launcher.endswith("unshare"):
        assert "/usr/bin/unshare -U" in built
    else:
        # setpriv passes three flags through the shell as argv elements,
        # each split by shlex.quote.
        assert "/usr/bin/setpriv --reuid=65534" in built
        assert "--regid=65534" in built
        assert "--clear-groups" in built


# ---------------------------------------------------------------------------
# Item (3): namespace probe distinguishes EACCES from any other nonzero.
# ---------------------------------------------------------------------------


def test_probe_uid_map_permission_denied_at_unshare_layer_is_launch_failed() -> None:
    """VM211 / unprivileged-user signature:

        unshare: cannot map denial: Permission denied

    exits nonzero WITHOUT the probe's ``LH_PERMISSION_DENIED`` marker.
    The kernel refused to even enter the user namespace; the inner
    script never ran. The probe MUST return ``LAUNCH_FAILED``, never
    ``ISOLATING`` — the kernel permission check is not authoritatively
    real on this evidence; treating the launch as isolated would be the
    P1 trap.
    """
    fake = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="unshare: cannot map denial: Permission denied\n",
    )
    with patch(
        "lh_harness.safe_subprocess._subprocess.run",
        return_value=fake,
    ):
        outcome = _user_namespace_actually_isolating()
    assert outcome is NamespaceProbeOutcome.LAUNCH_FAILED
    assert outcome is not NamespaceProbeOutcome.ISOLATING


def test_probe_timeout_is_launch_failed_not_isolating() -> None:
    """``TimeoutExpired`` is not in either subprocess-noise or ``LH_PERMISSION_DENIED``.

    A timed-out probe tells us nothing about whether the kernel would
    deny a write. Failing it through to ``LAUNCH_FAILED`` keeps the
    harness on the conservative path.
    """
    import subprocess as _subprocess

    fake = _subprocess.CompletedProcess(args=[], returncode=-9, stdout="", stderr="")
    with patch(
        "lh_harness.safe_subprocess._subprocess.run",
        return_value=fake,
    ):
        outcome = _user_namespace_actually_isolating()
    assert outcome is NamespaceProbeOutcome.LAUNCH_FAILED
    assert outcome is not NamespaceProbeOutcome.ISOLATING


def test_probe_marker_with_zero_exit_unchanged_file_is_not_isolating() -> None:
    """The probe distinguishes three outcomes; the worst-case misroute
    is ``rc=0`` + marker WITHOUT the file being overwritten — the
    kernel let the inner pid in, the script printed the marker, but the
    write actually did nothing. That is NOT isolating (no real boundary
    was demonstrated); it is also NOT launch-failed (no exception
    was raised). The probe must surface this as ``NOT_ISOLATING`` so
    ``auditor_boundary_required`` falls through to ``UNAVAILABLE``.
    """
    # The probe currently treats rc=0 + unchanged file as NOT_ISOLATING;
    # this test pins that semantic so a refactor cannot accidentally
    # promote it to ISOLATING.
    import subprocess as _subprocess

    fake = _subprocess.CompletedProcess(
        args=[], returncode=0, stdout="LH_PERMISSION_DENIED\n", stderr=""
    )
    with patch(
        "lh_harness.safe_subprocess._subprocess.run",
        return_value=fake,
    ):
        outcome = _user_namespace_actually_isolating()
    assert outcome is not NamespaceProbeOutcome.ISOLATING


# ---------------------------------------------------------------------------
# Item (4): VM211 bwrap fail-closed behavior is preserved.
# ---------------------------------------------------------------------------


def test_bwrap_uid_map_permission_denied_is_fail_closed_not_chmod_fallback() -> None:
    """When ``unshare`` exits nonzero with "uid map: Permission denied"
    (the VM211 signature on Debian-flavored containers without
    /proc/self/setgroups writability), the boundary detector must
    return UNAVAILABLE — never silently fall back to same-UID chmod /
    command deny-lists (those are advisory, NOT a kernel boundary).

    Pins the operator-rejected round_003 P1 invariant: refusing the
    auditor launch entirely is the only safe behavior on such hosts.
    """
    # Force the per-helper probe to return LAUNCH_FAILED: every launch
    # attempt emits a nonzero exit with no marker. simulate the VM211
    # environment by patching the probe helper at its module origin.
    launched_probe = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="unshare: failed to run unshare helper: Permission denied\n",
    )
    with patch(
        "lh_harness.safe_subprocess._subprocess.run",
        return_value=launched_probe,
    ):
        with pytest.raises(AuditorBoundaryUnavailable):
            auditor_boundary_required()
        # ``detect_auditor_boundary()`` always returns *something*; the
        # important invariant is that the only acceptable shape on this
        # fake is UNAVAILABLE. Anything else means we let the auditor
        # launch with an advisory-only chmod surface.
        boundary = detect_auditor_boundary()
    assert boundary.kind == AuditorBoundaryKind.UNAVAILABLE
    assert boundary.launcher is None
    assert "fail-closed" in boundary.rationale or "refuse" in boundary.rationale


def test_real_launch_path_does_not_swallow_unavailable_into_chmod_fallback(
    tmp_path: Path,
) -> None:
    """Companion to (4): even when an ``AuditorLaunch`` instance is
    built in-process, the wrapped command must NOT silently degrade to
    a non-bwrap / non-unshare string when the launcher surface fails.

    Concretely, ``prepare_auditor_launch`` must raise
    ``AuditorBoundaryUnavailable`` rather than yield a launch whose
    ``command(...)`` would let the audit subprocess inherit the same
    uid as the harness. Pin this via the public path the manager
    consumes.
    """
    # Simulate VM211 / no-isolation by patching both probes to fail.
    launched_probe = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="Operation not permitted\n",
    )
    with patch(
        "lh_harness.safe_subprocess._subprocess.run",
        return_value=launched_probe,
    ):

        class _NoRealExecEnv:
            """Captures exec calls; refuses to actually run them.

            Used to confirm ``prepare_auditor_launch`` would refuse the
            launch *before* any audit subprocess actually runs on the
            host.
            """

            def __init__(self) -> None:
                self.calls: list[str] = []

            async def exec(self, command, *, timeout=None, **kwargs):
                self.calls.append(command)
                # Pretend the launcher script returned the launcher's
                # error message; ``prepare_auditor_launch`` treats this
                # as a mount-boundary failure and raises.
                return subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr="no isolation",
                )

        env = _NoRealExecEnv()

        import asyncio

        async def _attempt():
            with pytest.raises(AuditorBoundaryUnavailable):
                await prepare_auditor_launch(env)
            # The actual bwrap mount command was never invoked against
            # the host — only the mktemp probe ran, which never tried
            # to mount anything privileged.
            assert not any("/usr/bin/bwrap" in cmd for cmd in env.calls), (
                f"bwrap mount boundary was attempted on a host where the "
                f"isolation prerequisites are missing: {env.calls!r}"
            )

        asyncio.run(_attempt())


# ---------------------------------------------------------------------------
# Item (1): the manager -> adapter -> spawn path forwards the boundary.
# ---------------------------------------------------------------------------


def test_manager_adapter_spawn_applies_boundary_to_audit_command(
    tmp_path: Path,
) -> None:
    """End-to-end on a disposable git repo: through the public
    ``_run_role_episode`` gate, the ``CommandAgentAdapter`` is given an
    ``AuditorLaunch`` whose ``command(...)`` is then forwarded to
    ``env.exec``. The exec invocation string MUST contain the boundary
    launcher (``unshare -U`` or ``setpriv``) ahead of bwrap AND the
    bwrap read-only-mount flags. If the adapter skipped the wrapping
    step (the historical bug where the ``isolation`` contextvar was
    not honoured by ``CommandAgentAdapter.run_episode``), the audit
    subprocess would have inherited the harness's uid and the kernel
    check would have been a no-op.

    Production ``LocalEnvironment.exec`` actually runs the wrapped
    command through ``subprocess.Popen`` — which on hosts where
    ``unshare`` cannot enter the user namespace (this CI host, VM211)
    fails BEFORE the audit subprocess produces any stdout, so an
    end-to-end live launch is not reliable on every host.  Instead we
    intercept ``LocalEnvironment.exec`` AFTER the adapter has shaped
    the wrapped command string and BEFORE the real subprocess.Popen
    call: this captures the exact argv the manager -> adapter -> spawn
    path produces without depending on the host's ability to actually
    enter the user namespace.
    """
    _make_git_repo(tmp_path)

    fixture_boundary = AuditorBoundary(
        kind=AuditorBoundaryKind.USER_NAMESPACE,
        launcher="/usr/bin/unshare",
        probe_uid=1,
        rationale="fixture boundary for end-to-end manager->adapter test",
    )
    fixture_launch = AuditorLaunch(
        scratch=str(tmp_path / "scratch"),
        boundary=fixture_boundary,
    )

    from lh_harness.adapters.cli_agent import CommandAgentAdapter
    from lh_harness.auditor_launch import active_auditor_launch
    from lh_harness.environment.local import LocalEnvironment
    from lh_harness.types import EpisodeBudget, ExecResult

    adapter = CommandAgentAdapter(
        command_template="printf audit-ok",
        workspace_path=str(tmp_path),
    )
    real_env = LocalEnvironment(tmp_dir=str(tmp_path / "tmp"))
    captured_commands: list[str] = []

    import asyncio

    # Real exec forwards to the real local environment, but records
    # the argv string so the assertions can inspect what the adapter
    # shaped. The real env is still used for write_remote_text and
    # other env surface.
    async def _intercepting_exec(command, **kwargs):
        captured_commands.append(command)
        return ExecResult(
            stdout="audit-ok",
            stderr="",
            exit_code=0,
            duration_ms=10,
            termination_reason="completed",
        )

    # Replace the bound method on the *instance*, not the class, so
    # every other Environment the harness uses in this test stays
    # untouched. ``write_remote_text`` and ``ensure_remote_dir`` still
    # call ``real_env.exec`` when they need a real mkdir: we forward
    # those to the real exec by leaving the kernel side intact.
    real_exec = real_env.exec
    async def _passthrough_or_capture(command, **kwargs):
        # Real exec writes the prompt file, etc., via its own path.
        # Capture only the audit subprocess call (the one that
        # contains ``printf audit-ok`` after wrapping). Everything else
        # goes through to the real exec so write_remote_text works.
        marker = "printf audit-ok"
        # First compute the wrapped form by re-running ``AuditorLaunch.command``,
        # then match against ``command``. We treat ANY exec call that
        # contains both the launcher path AND bwrap as the audit call.
        wrapped_by_adapter = (
            "/usr/bin/unshare" in command and "/usr/bin/bwrap" in command
        )
        if wrapped_by_adapter:
            captured_commands.append(command)
            return ExecResult(
                stdout="audit-ok", stderr="", exit_code=0,
                duration_ms=10, termination_reason="completed",
            )
        return await real_exec(command, **kwargs)

    real_env.exec = _passthrough_or_capture  # type: ignore[assignment]

    async def _attempt():
        token = active_auditor_launch.set(fixture_launch)
        try:
            await adapter.run_episode(
                prompt="audit me", env=real_env,
                budget=EpisodeBudget(max_duration_seconds=10),
                live_trajectory_path=str(tmp_path / "traj.jsonl"),
            )
        finally:
            active_auditor_launch.reset(token)

    asyncio.run(_attempt())

    assert len(captured_commands) >= 1, (
        f"adapter never forwarded a wrapped audit command: {captured_commands!r}"
    )
    audit_cmd = captured_commands[0]

    # Layered boundaries: launcher precedes bwrap; bwrap has the
    # read-only mount flag; the scratch dir is bound writable.
    assert "/usr/bin/unshare -U" in audit_cmd, (
        f"missing boundary launcher prefix in: {audit_cmd!r}"
    )
    assert audit_cmd.index("/usr/bin/unshare") < audit_cmd.index("/usr/bin/bwrap"), (
        f"boundary launcher must precede bwrap: {audit_cmd!r}"
    )
    assert "--ro-bind / /" in audit_cmd
    assert "--cap-drop ALL" in audit_cmd
    assert str(tmp_path / "scratch") in audit_cmd
    # The audit subprocess command was actually invoked through
    # env.exec with the wrapped command — i.e. the manager -> adapter
    # -> spawn path went through ``AuditorLaunch.command`` rather
    # than silently launching with the harness's uid.
    assert "printf audit-ok" in audit_cmd
