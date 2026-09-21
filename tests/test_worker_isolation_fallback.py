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
    ensure_service_relocated,
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


# --- self-relocation tests ---------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_relocation_state():
    """Each test starts from un-relocated module state (the process restarts
    per test run in production, so no relocation may leak between tests)."""

    was_relocated = worker_isolation._RELOCATED
    was_parent = worker_isolation._WORKER_PARENT_CGROUP
    worker_isolation._RELOCATED = False
    worker_isolation._WORKER_PARENT_CGROUP = None
    yield
    worker_isolation._RELOCATED = was_relocated
    worker_isolation._WORKER_PARENT_CGROUP = was_parent


def _fake_delegated_cgroup(root, own="/test.slice/fake.service", *, procs=""):
    """A fake delegated service cgroup (the shape ``Delegate=memory pids``
    produces), with the calling process resident and the leaf ``main`` not
    yet created."""

    base = root / own.strip("/")
    base.mkdir(parents=True)
    (base / "cgroup.controllers").write_text("memory pids\n")
    (base / "cgroup.subtree_control").write_text("")
    (base / "cgroup.procs").write_text(procs)
    return base


def test_self_relocation_happens_once_and_updates_parent(tmp_path, monkeypatch) -> None:
    """Test that self-relocation moves the PID into a leaf cgroup and remembers the parent."""

    fake_root = tmp_path / "cgroup"
    # Initial cgroup where the process resides
    own = "/test.slice/fake.service"
    base = _fake_delegated_cgroup(fake_root, own, procs="123\n")
    # Leaf cgroup that will be created for relocation
    leaf = base / "main"
    leaf.mkdir()
    (leaf / "cgroup.procs").write_text("")

    monkeypatch.setattr(worker_isolation, "own_cgroup_path", lambda: own)
    monkeypatch.setattr(os, "getpid", lambda: 123)

    # First call should attempt relocation
    worker_isolation._attempt_relocation(root=fake_root)
    assert worker_isolation._RELOCATED is True
    assert worker_isolation._WORKER_PARENT_CGROUP == own
    # Verify the PID moved to the leaf cgroup
    assert (leaf / "cgroup.procs").read_text().strip() == "123"
    # Original cgroup should now be empty of processes (simulate the move)
    (base / "cgroup.procs").write_text("")
    assert (base / "cgroup.procs").read_text().strip() == ""

    # Second call should be idempotent and not move anything
    worker_isolation._attempt_relocation(root=fake_root)
    assert worker_isolation._RELOCATED is True  # still True
    assert worker_isolation._WORKER_PARENT_CGROUP == own
    # PID still in leaf
    assert (leaf / "cgroup.procs").read_text().strip() == "123"


def test_self_relocation_fails_gracefully(tmp_path, monkeypatch) -> None:
    """When the leaf's cgroup.procs cannot be written, relocation gives up.

    (A read-only leaf file models the real failure mode — an EACCES from
    cgroupfs — without touching directory permissions that would break
    tmp_path cleanup.)
    """

    fake_root = tmp_path / "cgroup"
    own = "/test.slice/fake.service"
    base = _fake_delegated_cgroup(fake_root, own, procs="123\n")
    # The ``main`` leaf exists but refuses the move: its cgroup.procs is
    # read-only, the way cgroupfs rejects a write it will not honour.
    leaf = base / "main"
    leaf.mkdir()
    (leaf / "cgroup.procs").write_text("")
    (leaf / "cgroup.procs").chmod(0o444)

    monkeypatch.setattr(worker_isolation, "own_cgroup_path", lambda: own)
    monkeypatch.setattr(os, "getpid", lambda: 123)
    monkeypatch.setattr(worker_isolation, "CGROUP_ROOT", fake_root)

    # Relocation should fail but not raise
    worker_isolation._attempt_relocation()
    assert worker_isolation._RELOCATED is False  # still False
    assert worker_isolation._WORKER_PARENT_CGROUP is None
    # PID should still be in original cgroup
    assert (base / "cgroup.procs").read_text().strip() == "123"


def test_prepare_child_cgroup_uses_parent_after_relocation(tmp_path, monkeypatch) -> None:
    """Test that after relocation, child cgroups are created under the parent cgroup."""

    fake_root = tmp_path / "cgroup"
    # Initial cgroup where the process resides, as the sole resident — the
    # real relocation shape (the move itself is exercised, not pre-empted).
    own = "/test.slice/fake.service"
    base = _fake_delegated_cgroup(fake_root, own, procs="999\n")

    monkeypatch.setattr(worker_isolation, "own_cgroup_path", lambda: own)
    monkeypatch.setattr(os, "getpid", lambda: 999)

    # Trigger relocation
    worker_isolation._attempt_relocation(root=fake_root)
    assert worker_isolation._RELOCATED is True
    assert worker_isolation._WORKER_PARENT_CGROUP == own
    # The move itself happened: the process's pid is in the leaf.
    leaf = base / "main"
    assert leaf.is_dir()
    assert (leaf / "cgroup.procs").read_text().strip() == "999"

    # Now prepare a child cgroup - it should be under the parent (own), not the leaf
    plan = worker_isolation.prepare_launch(
        command=[sys.executable, "-c", "print('worker')"],
        run_id="run-relocated",
        memory_max="256M",
        probe=_PROBE_NONE,
        cgroup_writable=lambda: True,
        cgroup_root=fake_root,
    )

    assert plan.mechanism == worker_isolation.MECHANISM_CGROUP
    # The cgroup path should be under the parent cgroup (own), not the leaf
    assert plan.cgroup.startswith(own.rstrip("/") + "/worker-")
    # Verify the child cgroup directory was created under the parent
    child_dir = fake_root / plan.cgroup.strip("/")
    assert child_dir.is_dir()
    assert (child_dir / "memory.max").read_text() == str(worker_isolation.to_bytes("256M"))
    # Controllers should be enabled on the parent cgroup for children
    subtree = (base / "cgroup.subtree_control").read_text()
    assert "+memory" in subtree and "+pids" in subtree


def test_ensure_service_relocated_moves_the_pid_and_is_idempotent(
    tmp_path, monkeypatch
) -> None:
    """The startup entry point the supervisor calls does the real move.

    This is the function ``RunSupervisor.__init__`` invokes at service
    startup (TASK 211): it must relocate the process's own PID into the
    ``main`` leaf against a fake cgroupfs, record the parent for worker
    creation, and be a no-op on the second call.
    """

    fake_root = tmp_path / "cgroup"
    own = "/system.slice/fake.service"
    base = _fake_delegated_cgroup(fake_root, own, procs="4242\n")

    monkeypatch.setattr(worker_isolation, "own_cgroup_path", lambda: own)
    monkeypatch.setattr(os, "getpid", lambda: 4242)

    worker_isolation.ensure_service_relocated(root=fake_root)

    assert worker_isolation._RELOCATED is True
    assert worker_isolation._WORKER_PARENT_CGROUP == own
    # The process's own PID now lives in the leaf.  (On a real cgroupfs the
    # single write to the leaf's cgroup.procs relocates the process, so the
    # delegated cgroup loses the PID automatically — the empty-parent
    # property belongs to cgroupfs, not to a second write of ours.)
    leaf = base / "main"
    assert (leaf / "cgroup.procs").read_text().split() == ["4242"]
    # The relocation made exactly one write: the parent's cgroup.procs was
    # not rewritten by the code (the fake cannot mirror the move itself).
    assert (base / "cgroup.procs").read_text().split() == ["4242"]

    # Idempotent: a second startup-shaped call must not touch anything.
    worker_isolation.ensure_service_relocated(root=fake_root)
    assert (leaf / "cgroup.procs").read_text().split() == ["4242"]
    assert (base / "cgroup.procs").read_text().split() == ["4242"]


def test_ensure_service_relocated_skips_when_not_sole_resident(
    tmp_path, monkeypatch
) -> None:
    """A shared cgroup is never disturbed: no move, no worker parent.

    The reproduction method for the live proof drops a pytest process into
    the real service cgroup alongside the service's own PID.  Moving only
    when this process is the sole resident means the ad-hoc pytest process
    (and any real co-resident) is left where it is and the launch falls
    through to the unbounded plan — instead of relocating and leaving the
    top-level cgroup occupied (which would reintroduce the EBUSY).
    """

    fake_root = tmp_path / "cgroup"
    own = "/system.slice/fake.service"
    base = _fake_delegated_cgroup(fake_root, own, procs="77 4242\n")  # co-resident

    monkeypatch.setattr(worker_isolation, "own_cgroup_path", lambda: own)
    monkeypatch.setattr(os, "getpid", lambda: 4242)

    worker_isolation.ensure_service_relocated(root=fake_root)

    assert worker_isolation._RELOCATED is False
    assert worker_isolation._WORKER_PARENT_CGROUP is None
    # Nobody moved, nothing was created.
    assert not (base / "main").exists()
    assert (base / "cgroup.procs").read_text().split() == ["77", "4242"]


def test_relocated_process_uses_the_parent_for_worker_probing(
    tmp_path, monkeypatch
) -> None:
    """After relocation the writability probe must target the parent, not the leaf.

    The leaf ``main`` cgroup a freshly relocated process lives in has an
    empty ``cgroup.controllers`` until the parent's ``subtree_control`` is
    enabled — which happens later, inside ``_prepare_child_cgroup``.  If the
    probe (``cgroup_subtree_writable`` with no explicit cgroup) still looked
    at the caller's own cgroup, every post-relocation launch would see
    "not delegated" and fall to the unbounded plan: the RSS bound would be
    dead despite a successful relocation.
    """

    fake_root = tmp_path / "cgroup"
    own = "/system.slice/fake.service"
    base = _fake_delegated_cgroup(fake_root, own, procs="4242\n")

    monkeypatch.setattr(worker_isolation, "own_cgroup_path", lambda: own)
    monkeypatch.setattr(os, "getpid", lambda: 4242)

    worker_isolation.ensure_service_relocated(root=fake_root)
    # The caller now lives in the leaf, whose controllers are empty.
    leaf = base / "main"
    (leaf / "cgroup.controllers").write_text("")  # fresh child cgroup shape
    assert (leaf / "cgroup.controllers").read_text() == ""

    # The no-argument probe follows the worker parent, so the delegated
    # parent — not the empty leaf — decides the mechanism.
    assert worker_isolation.cgroup_subtree_writable(root=fake_root) is True
    # And an explicitly requested cgroup still probes itself (unchanged).
    assert worker_isolation.cgroup_subtree_writable(
        cgroup_path=own + "/main", root=fake_root
    ) is False


def test_supervisor_startup_relocates_the_service_pid(tmp_path, monkeypatch) -> None:
    """Constructing RunSupervisor performs the relocation, at startup.

    The relocation must happen while the service is the unambiguous sole
    resident of the delegated cgroup — i.e. from supervisor construction,
    not lazily at the first worker launch — so the top-level cgroup is
    provably empty before any ``worker-*`` child is created.
    """

    from lh_harness.supervisor.service import RunSupervisor

    fake_root = tmp_path / "cgroup"
    own = "/system.slice/fake.service"
    base = _fake_delegated_cgroup(fake_root, own, procs="4242\n")

    monkeypatch.setattr(worker_isolation, "own_cgroup_path", lambda: own)
    monkeypatch.setattr(os, "getpid", lambda: 4242)
    monkeypatch.setattr(worker_isolation, "CGROUP_ROOT", fake_root)

    RunSupervisor(tmp_path / "runs", workspace_root=tmp_path)

    assert worker_isolation._RELOCATED is True
    assert worker_isolation._WORKER_PARENT_CGROUP == own
    assert (base / "main" / "cgroup.procs").read_text().split() == ["4242"]
    # The move was a single write into the leaf (cgroupfs itself empties the
    # parent on the real host); the code did not rewrite the parent's file.
    assert (base / "cgroup.procs").read_text().split() == ["4242"]


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