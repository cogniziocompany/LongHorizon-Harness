"""The RLIMIT_AS fallback path of per-run worker memory isolation (TASK 202).

On hosts without a reachable systemd manager the worker must still be
memory-bounded: ``prepare_launch`` resolves to the ``rlimit-as`` mechanism,
wraps nothing, and hands the child a ``preexec_fn`` that caps ``RLIMIT_AS``.
Only this fallback is exercised here; the systemd scope path needs a live
manager and is covered on the host, not in the unit suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest  # noqa: F401  (pytest parametrize)

# Exercise the checkout's own src tree, not whatever lh_harness copy a host
# venv may have installed: a non-editable install predates worker_isolation.
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src")
)

from lh_harness import worker_isolation  # noqa: E402
from lh_harness.worker_isolation import (  # noqa: E402
    DEFAULT_WORKER_MEMORY_MAX,
    ENV_WORKER_MEMORY_MAX,
    MECHANISM_RLIMIT,
    await_scope_exit,
    prepare_launch,
    rlimit_plan,
    scope_launch_failed,
    to_bytes,
)


_PROBE_NONE = lambda: None  # noqa: E731


def test_fallback_mechanism_when_no_systemd_run() -> None:
    plan = prepare_launch(
        command=["/bin/true", "--flag", "x"],
        run_id="run-fallback",
        memory_max="3G",
        probe=_PROBE_NONE,
    )

    assert plan.mechanism == MECHANISM_RLIMIT
    # The bare worker argv is launched unchanged (no systemd-run wrapper).
    assert plan.command == ["/bin/true", "--flag", "x"]
    assert plan.preexec is not None
    assert plan.unit == ""
    record = plan.record()
    assert record == {
        "mechanism": MECHANISM_RLIMIT,
        "limit": "3G",
        "unit": "",
        "cgroup": "",
        "oom_kill_base": None,
    }


def test_fallback_plan_is_the_scope_retry_shape() -> None:
    plan = rlimit_plan("512M", ["python", "-m", "lh_harness.worker"])

    assert plan.mechanism == MECHANISM_RLIMIT
    assert plan.limit == "512M"
    assert plan.command == ["python", "-m", "lh_harness.worker"]
    assert plan.preexec is not None


@pytest.mark.parametrize(
    ("explicit", "env", "config", "expected"),
    [
        (None, None, None, DEFAULT_WORKER_MEMORY_MAX),
        (None, "512M", "3G", "512M"),  # env override beats config
        (None, None, "768M", "768M"),  # config beats the default
        ("1G", "512M", "3G", "1G"),  # explicit argument wins overall
    ],
)
def test_fallback_limit_resolution_precedence(explicit, env, config, expected) -> None:
    # The supervisor feeds os.environ.get(ENV_WORKER_MEMORY_MAX) into env.
    assert (
        worker_isolation.resolve_memory_limit(explicit=explicit, env=env, config=config)
        == expected
    )


def test_fallback_applies_rlimit_as_in_the_child() -> None:
    """The preexec really caps RLIMIT_AS, and the child inherits it."""

    limit_bytes = to_bytes("64M")
    probe = textwrap.dedent(
        """
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        print(soft, hard)
        """
    )
    plan = rlimit_plan("64M", [sys.executable, "-c", probe])

    result = subprocess.run(
        plan.command,
        preexec_fn=plan.preexec,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ},
        check=True,
    )
    soft, hard = (int(value) for value in result.stdout.split())
    assert soft == limit_bytes
    assert hard > soft  # the hard limit is not lowered by the fallback

# --- TASK 205: the scope-to-RLIMIT fallback must fire structurally ----------

# Measured on CT110 as user=harness: systemd-run --scope exits rc=1 with
# exactly this stderr, and none of the legacy phrase signatures match it.
_CT110_SCOPE_STDERR = "Failed to start transient scope unit: Access denied\n"

# A stand-in "systemd-run": records its invocation, prints the real CT110
# failure to stderr, and dies rc=1 without ever starting the wrapped worker —
# exactly how the real launcher fails on CT110 (polkit refusal).
_LAUNCHER_PROBE = textwrap.dedent(
    """
    import sys

    print("launcher", file=open(sys.argv[1], "a", encoding="utf-8"))
    sys.stderr.write("Failed to start transient scope unit: Access denied" + chr(10))
    sys.exit(1)
    """
)

# A launcher that outlives the scope-cgroup wait (poll() still None when the
# supervisor looks) before failing like CT110.
_SLOW_LAUNCHER_PROBE = textwrap.dedent(
    """
    import sys, time

    time.sleep(2.6)
    print("launcher", file=open(sys.argv[1], "a", encoding="utf-8"))
    sys.stderr.write("Failed to start transient scope unit: Access denied" + chr(10))
    sys.exit(1)
    """
)

# The bare worker the retry must launch: record one run, exit cleanly.
_WORKER_PROBE = textwrap.dedent(
    """
    import sys

    print("worker", file=open(sys.argv[-1], "a", encoding="utf-8"))
    sys.exit(0)
    """
)


def _scope_plan(launcher_script: str, launcher_marker: str, worker_marker: str):
    """A real scope-shaped plan whose "systemd-run" is the failing stand-in."""

    plan = prepare_launch(
        command=[sys.executable, "-c", _WORKER_PROBE, worker_marker],
        run_id="run-ct110",
        memory_max="3G",
        probe=lambda: sys.executable,
    )
    # Shape-check the plan, then substitute the failing launcher for the real
    # binary: the wrapped argv keeps its systemd-run form so the retry must
    # re-derive the bare worker command from it.
    assert plan.mechanism == worker_isolation.MECHANISM_SCOPE
    assert plan.unit
    assert plan.command[0] == sys.executable
    assert plan.command[-5:] == ["--", sys.executable, "-c", _WORKER_PROBE, worker_marker]
    plan.command[:] = [
        sys.executable,
        "-c",
        launcher_script,
        launcher_marker,
        *plan.command[1:],
    ]
    return plan


def _marker_lines(marker: str) -> list[str]:
    try:
        with open(marker, encoding="utf-8") as handle:
            return handle.read().splitlines()
    except OSError:
        return []


def _run_isolated(supervisor, isolation, run_id: str, output_path, tmp_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as output:
        record, process = supervisor._spawn_isolated(
            isolation=isolation,
            run_id=run_id,
            cwd=str(tmp_path),
            env={**os.environ},
            output_path=output_path,
            output=output,
        )
    process.wait(timeout=60)
    return record, process


def test_ct110_scope_failure_retries_with_rlimit_as(tmp_path) -> None:
    """rc=1 + "Failed to start transient scope unit: Access denied" falls back.

    The real CT110 stderr matches none of the legacy phrase signatures, so
    the decision must be structural: any non-zero systemd-run exit with no
    scope cgroup appearing is a failed scope launch.
    """

    from lh_harness.supervisor.service import RunSupervisor

    runs_root = tmp_path / "runs"
    supervisor = RunSupervisor(runs_root, workspace_root=tmp_path)
    launcher_marker = str(tmp_path / "launches")
    worker_marker = str(tmp_path / "worker-ran")
    isolation = _scope_plan(_LAUNCHER_PROBE, launcher_marker, worker_marker)

    # The same run-local worker log layout the real launch uses, so the
    # failure tail the fallback decision reads is the launcher's own stderr.
    output_path = runs_root / "run-ct110" / "worker.log"
    record, process = _run_isolated(
        supervisor, isolation, "run-ct110", output_path, tmp_path
    )

    # The retry launched the bare worker argv, and it actually ran.
    assert record["mechanism"] == MECHANISM_RLIMIT
    assert record["limit"] == "3G"
    assert record["cgroup"] == ""
    assert record["oom_kill_base"] is None
    assert process.returncode == 0
    assert _marker_lines(launcher_marker) == ["launcher"]
    assert _marker_lines(worker_marker) == ["worker"]


def test_ct110_stderr_matches_no_legacy_signature_structurally() -> None:
    """The decision is structural; the phrase list stays log-only."""

    tail = _CT110_SCOPE_STDERR.encode("utf-8")
    assert scope_launch_failed(1, tail) is True
    # A clean exit (or a launcher still running) is not a failed launch.
    assert scope_launch_failed(0, tail) is False
    assert scope_launch_failed(None, tail) is False


def test_poll_none_after_cgroup_wait_briefly_waits_for_exit(tmp_path) -> None:
    """poll() still None after the cgroup wait: settle the exit first."""

    from lh_harness.supervisor.service import RunSupervisor

    runs_root = tmp_path / "runs"
    supervisor = RunSupervisor(runs_root, workspace_root=tmp_path)
    launcher_marker = str(tmp_path / "launches")
    worker_marker = str(tmp_path / "worker-ran")
    isolation = _scope_plan(_SLOW_LAUNCHER_PROBE, launcher_marker, worker_marker)

    output_path = runs_root / "run-ct110-slow" / "worker.log"
    record, process = _run_isolated(
        supervisor, isolation, "run-ct110-slow", output_path, tmp_path
    )

    # The launcher outlived the cgroup wait but still died rc=1; the brief
    # exit wait observed that and the fallback fired.
    assert record["mechanism"] == MECHANISM_RLIMIT
    assert process.returncode == 0
    assert _marker_lines(launcher_marker) == ["launcher"]
    assert _marker_lines(worker_marker) == ["worker"]


def test_await_scope_exit_settles_then_reports_none() -> None:
    """The helper waits for a real exit code, None only for a live process."""

    class FakeProcess:
        def __init__(self, code: int | None) -> None:
            self.code = code

        def wait(self, timeout=None):
            return self.code

        def poll(self):
            return self.code

    assert await_scope_exit(FakeProcess(1)) == 1
    assert await_scope_exit(FakeProcess(None)) is None
