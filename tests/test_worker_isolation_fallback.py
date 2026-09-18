"""The fallback routing of per-run worker memory isolation (TASKS 202 + 208).

When the preferred mechanisms cannot apply a limit, the worker must still
start — but TASK 208 fixed what the fallback does:

- the delegated cgroup mechanism (per-episode child cgroup + ``memory.max``,
  an RSS bound) is preferred and needs no systemd manager at all;
- when even that is unavailable (no ``Delegate=memory pids`` deployed), the
  fallback is *log-only and unbounded*: an address-space rlimit cannot bound
  a Node 22/V8 agent worker (it reserves ~5.3 GiB of address space, peaks at
  9.27 GiB virtual, uses 0.26 GiB resident — the pre-208 three-gibibyte
  address-space cap killed every CT110 episode on sight), so no rlimit of
  any kind may return to this path;
- the scope-failure decision stays structural: any non-zero ``systemd-run``
  exit with no scope cgroup is a failed launch (on CT110, every launch).

The real RSS-bound mechanism itself is exercised in
``tests/test_worker_rss_bound.py``; this file covers the plumbing and the
CT110 structural findings.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Exercise the checkout's own src tree, not whatever lh_harness copy a host
# venv may have installed: a non-editable install predates worker_isolation.
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src")
)

from lh_harness import worker_isolation  # noqa: E402
from lh_harness.worker_isolation import (  # noqa: E402
    DEFAULT_WORKER_MEMORY_MAX,
    ENV_WORKER_MEMORY_MAX,
    MECHANISM_CGROUP,
    MECHANISM_SCOPE,
    MECHANISM_UNBOUNDED,
    await_scope_exit,
    classify_memory_death,
    fallback_plan,
    prepare_launch,
    scope_launch_failed,
    to_bytes,
    unbounded_plan,
)

_PROBE_NONE = lambda: None  # noqa: E731


# --- TASK 208: the instrument must match the quantity ----------------------

def test_no_address_space_rlimit_remains_in_the_live_path() -> None:
    """A1 guard: the old address-space rlimit must stay dead.

    The pre-208 fallback capped address space (three gibibytes) and on CT110
    — where the scope path always fails — that cap killed every episode
    before it started (Node 22/V8 reserves ~5.3 GiB of address space while
    using 0.26 GiB resident).  The regression this test guards against is
    the instrument returning to the code, under any spelling.
    """

    src_root = Path(__file__).resolve().parent.parent / "src"
    offenders = []
    for path in src_root.rglob("*.py"):
        if ".backup" in path.name:
            continue  # untracked task-206 strays, not part of the module
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in ("RLIMIT_AS", "3G"):
            if token in text:
                offenders.append(f"{path.relative_to(src_root)} mentions {token!r}")
    assert offenders == []


def test_default_limit_is_rss_sized_not_address_space_sized() -> None:
    """The default must be sized from the measured peak with headroom."""

    # CT110, 2026-09-18, live agent runs: VmPeak 9.27 GiB, VmSize 5.31 GiB,
    # VmRSS 0.26 GiB.  2G admits the measured RESIDENT use with ~7x headroom while
    # staying an RSS bound (memory.max), which never counts the ~5.3 GiB
    # address-space reservation.
    assert DEFAULT_WORKER_MEMORY_MAX == "2G"
    # The cap must sit BELOW physical RAM (CT110: 6.0 GiB) or the kernel
    # OOM-kills globally before the cgroup limit is ever reached - the
    # whole-unit failure task 202 exists to prevent. And it must stay well
    # above the measured resident high-water mark of a real agent (0.29 GiB).
    assert to_bytes(DEFAULT_WORKER_MEMORY_MAX) < to_bytes("6G")
    assert to_bytes(DEFAULT_WORKER_MEMORY_MAX) > to_bytes("1G")
    # And the rejected mechanism's value must never be a limit again.
    assert "3G" != DEFAULT_WORKER_MEMORY_MAX


def test_rlimit_helpers_are_gone() -> None:
    """The pre-208 rlimit API is not merely unused — it does not exist."""

    assert not hasattr(worker_isolation, "apply_rlimit_as")
    assert not hasattr(worker_isolation, "rlimit_plan")
    assert not hasattr(worker_isolation, "MECHANISM_RLIMIT")
    assert not hasattr(worker_isolation, "MECHANISM_RLIMIT_DATA")


# --- mechanism resolution ---------------------------------------------------

def test_unbounded_mechanism_when_no_systemd_run_and_no_delegation() -> None:
    plan = prepare_launch(
        command=["/bin/true", "--flag", "x"],
        run_id="run-fallback",
        memory_max=DEFAULT_WORKER_MEMORY_MAX,
        probe=_PROBE_NONE,
        cgroup_writable=lambda: False,
    )

    assert plan.mechanism == MECHANISM_UNBOUNDED
    # The bare worker argv is launched unchanged, with no limit applied.
    assert plan.command == ["/bin/true", "--flag", "x"]
    assert plan.preexec is None
    assert plan.unit == ""
    assert plan.cgroup == ""
    record = plan.record()
    assert record == {
        "mechanism": MECHANISM_UNBOUNDED,
        "limit": DEFAULT_WORKER_MEMORY_MAX,
        "unit": "",
        "cgroup": "",
        "oom_kill_base": None,
    }


def test_unbounded_deaths_are_never_attributed_to_the_limit(tmp_path) -> None:
    """The log-only plan applied nothing, so it must not claim kills."""

    record = unbounded_plan("512M", ["python"]).record()
    assert classify_memory_death(record, returncode=-9) is None


def test_cgroup_plan_prefers_delegated_subtree_and_writes_memory_max(
    tmp_path, monkeypatch
) -> None:
    """The delegated-subtree plan caps RESIDENT memory via memory.max."""

    fake_root = tmp_path / "cgroup"
    own = "/test.slice/fake.service"
    base = fake_root / own.strip("/")
    base.mkdir(parents=True)
    (base / "cgroup.controllers").write_text("memory pids\n")
    (base / "cgroup.subtree_control").write_text("")
    (base / "cgroup.procs").write_text("")
    monkeypatch.setattr(worker_isolation, "own_cgroup_path", lambda: own)

    plan = prepare_launch(
        command=[sys.executable, "-c", "print('worker')"],
        run_id="run-cgroup",
        memory_max="512M",
        probe=_PROBE_NONE,
        cgroup_writable=lambda: True,
        cgroup_root=fake_root,
    )

    assert plan.mechanism == MECHANISM_CGROUP
    assert plan.preexec is not None
    child = fake_root / plan.cgroup.strip("/")
    assert child.is_dir()
    assert (child / "memory.max").read_text() == str(to_bytes("512M"))
    # Controllers were enabled for children so the cap actually applies.
    subtree = (base / "cgroup.subtree_control").read_text()
    assert "+memory" in subtree and "+pids" in subtree
    assert plan.record()["cgroup"] == plan.cgroup


def test_cgroup_preexec_moves_the_child_into_its_cgroup(tmp_path, monkeypatch) -> None:
    """The preexec self-move targets the prepared cgroup's cgroup.procs."""

    fake_root = tmp_path / "cgroup"
    own = "/test.slice/fake.service"
    base = fake_root / own.strip("/")
    base.mkdir(parents=True)
    (base / "cgroup.controllers").write_text("memory pids\n")
    (base / "cgroup.subtree_control").write_text("memory pids\n")
    (base / "cgroup.procs").write_text("")
    monkeypatch.setattr(worker_isolation, "own_cgroup_path", lambda: own)

    probe = "print('child ran')"
    plan = prepare_launch(
        command=[sys.executable, "-c", probe],
        run_id="run-cgroup-move",
        memory_max="256M",
        probe=_PROBE_NONE,
        cgroup_writable=lambda: True,
        cgroup_root=fake_root,
    )
    child = fake_root / plan.cgroup.strip("/")
    # A real cgroupfs dir always carries cgroup.procs; the fake tree gets one
    # only here, after the plan created the directory.
    (child / "cgroup.procs").write_text("")

    # Run the real preexec body against the fake tree and a real child.
    process = subprocess.Popen(
        plan.command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=plan.preexec,
    )
    stdout, _ = process.communicate(timeout=60)
    assert process.returncode == 0
    assert b"child ran" in stdout
    entered = (child / "cgroup.procs").read_text().split()
    assert entered, "the preexec must write the child pid (or 0) into cgroup.procs"


def test_fallback_plan_prefers_cgroup_then_logs(tmp_path, monkeypatch) -> None:
    """fallback_plan: cgroup when it can, log-only unbounded when it cannot."""

    fake_root = tmp_path / "cgroup"
    own = "/test.slice/fake.service"
    base = fake_root / own.strip("/")
    base.mkdir(parents=True)
    (base / "cgroup.controllers").write_text("memory pids\n")
    (base / "cgroup.subtree_control").write_text("")
    (base / "cgroup.procs").write_text("")
    monkeypatch.setattr(worker_isolation, "own_cgroup_path", lambda: own)

    plan = fallback_plan(
        "256M", [sys.executable, "-m", "lh_harness.worker"], "run-y", root=fake_root
    )
    assert plan.mechanism == MECHANISM_CGROUP
    assert plan.preexec is not None

    # With no delegated subtree either, the worker must still launch —
    # unbounded, log-only, never an address-space rlimit.
    monkeypatch.setattr(worker_isolation, "own_cgroup_path", lambda: None)
    plan = fallback_plan("256M", [sys.executable, "-m", "lh_harness.worker"], "run-x")
    assert plan.mechanism == MECHANISM_UNBOUNDED
    assert plan.preexec is None
    assert plan.cgroup == ""


def test_env_override_name_unchanged() -> None:
    assert ENV_WORKER_MEMORY_MAX == "LH_HARNESS_WORKER_MEMORY_MAX"


@pytest.mark.parametrize(
    ("explicit", "env", "config", "expected"),
    [
        (None, None, None, DEFAULT_WORKER_MEMORY_MAX),
        (None, "512M", "12G", "512M"),  # env override beats config
        (None, None, "768M", "768M"),  # config beats the default
        ("1G", "512M", "12G", "1G"),  # explicit argument wins overall
    ],
)
def test_limit_resolution_precedence_kept(explicit, env, config, expected) -> None:
    # The supervisor feeds os.environ.get(ENV_WORKER_MEMORY_MAX) into env.
    assert (
        worker_isolation.resolve_memory_limit(explicit=explicit, env=env, config=config)
        == expected
    )


def test_ct110_scope_failure_retries_without_an_address_space_rlimit(tmp_path) -> None:
    """rc=1 + "Failed to start transient scope unit: Access denied" retries.

    The real CT110 stderr matches none of the legacy phrase signatures, so
    the decision must be structural: any non-zero systemd-run exit with no
    scope cgroup appearing is a failed scope launch.  TASK 208: the retry
    must be the log-only unbounded plan (or the cgroup mechanism) — never
    the address-space rlimit that killed every CT110 episode.
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

    # The retry launched the bare worker argv, and it actually ran — under
    # no rlimit of any kind.
    assert record["mechanism"] == MECHANISM_UNBOUNDED
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
    assert record["mechanism"] == MECHANISM_UNBOUNDED
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


# --- shared shims for the supervisor-routing tests --------------------------

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
        memory_max=DEFAULT_WORKER_MEMORY_MAX,
        probe=lambda: sys.executable,
        cgroup_writable=lambda: False,
    )
    # Shape-check the plan, then substitute the failing launcher for the real
    # binary: the wrapped argv keeps its systemd-run form so the retry must
    # re-derive the bare worker command from it.
    assert plan.mechanism == MECHANISM_SCOPE
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