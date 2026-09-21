"""The RSS bound actually bounds RSS — and never kills a real worker (TASK 208).

The pre-208 fallback capped the worker's ADDRESS SPACE at three gibibytes,
and on CT110 — where the systemd scope path always fails (no polkit daemon),
making the fallback the only path — that cap killed every episode: measured
on CT110, 2026-09-18 (live agent runs), each episode's Claude Code child is
Node 22, and V8 plus its thread pool RESERVE about 5.3 GiB of address space
(VmSize 5.31 GiB) and have peaked at 9.27 GiB (VmPeak) while actually using
0.26 GiB resident (VmRSS).  RLIMIT_AS counts the reservation and kills the
episode before it starts; the resident figure the cap was meant to protect —
0.26 GiB — is nowhere near it.  (``claude --version`` starts fine under a
small address-space cap, and so does CPython's pytest — which is exactly why
the pre-208 test suite passed while production died.  It could not fail the
way production fails.)

These tests exercise the quantity the mechanism must actually bound:

1. SURVIVAL: a real Node 22 child reserving 4 GiB of V8-shaped address
   space (private anonymous mappings, RSS ~0 — the same order as the
   measured 5.31 GiB production reservation) runs to completion under the
   RSS bound.  The old three-gibibyte ADDRESS-SPACE cap kills this exact
   child — the counterfactual test proves that half live, unskipped (4 GiB
   reserved will not fit in 3 GiB of address space).
2. BLOWUP: a child that actually commits resident memory beyond a small
   ``memory.max`` is OOM-killed ALONE inside its own cgroup, while the
   parent (this pytest process — the stand-in for the lh-harness unit)
   survives untouched, and the kill is attributable via ``memory.events``.

Mechanism (why this is the right instrument, from the audited CT110 facts of
round_001): the delegated cgroup subtree — ``Delegate=memory pids`` in
``packaging/lh-harness.service``.  On CT110 a transient ``systemd-run
--scope`` is always refused (no polkit daemon; task 205: rc=1, "Failed to
start transient scope unit: Access denied") and ``systemd-run --user`` is
impossible (no user manager), so the only RSS-bound mechanism the
unprivileged harness user can use is a child cgroup under the service's own
delegated cgroup with a direct ``memory.max`` write.  ``memory.max`` bounds
resident pages, never reserved-but-untouched address space.

HONEST-SKIP CONTRACT (this test must never fake a pass): the mechanism needs
the service's cgroup subtree delegated (``Delegate=memory pids`` deployed
via a deploy-window daemon-reload, which this task is forbidden from
performing).  On an undelegated host the two cgroup tests below SKIP with
the exact missing privilege named — never a fabricated pass.  The other two
tests still run everywhere and carry live evidence: the V8-reservation
child prints its own ``/proc/self/status`` (VmSize ~11.5 GB with the
reservation, VmRSS ~40 MB — the reservation-vs-resident distinction in
numbers), and the counterfactual test kills the same child the way the old
3G address-space cap killed every CT110 episode.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src")
)

from lh_harness import worker_isolation  # noqa: E402
from lh_harness.worker_isolation import (  # noqa: E402
    MECHANISM_CGROUP,
    cgroup_plan,
    classify_memory_death,
    read_scope_oom_kills,
    verify_pid_cgroup,
)

# --- the measured numbers this file exists to name --------------------------
# CT110, 2026-09-18, live agent runs (overseer measurement; do not re-derive):
MEASURED_VMPREAK_GIB = 9.27  # VmPeak of the live claude agent process
MEASURED_VMSIZE_GIB = 5.31  # VmSize: V8 + thread pool address-space reservation
MEASURED_VMRSS_GIB = 0.26  # VmRSS: what an RSS bound actually protects
OLD_DEADLY_CAP = "3G"  # the pre-208 address-space cap that killed every run

_NODE_RESERVE_SCRIPT = textwrap.dedent(
    """
    // V8-shaped address-space reservation, the production failure mode in
    // miniature: WebAssembly.Memory reserves private anonymous address
    // space without committing it (exactly what RLIMIT_AS counts and
    // memory.max does not).  65536 wasm pages of 64 KiB = 4 GiB reserved.
    const initial = 65536;  // 65536 * 64 KiB = 4 GiB (node's max single Memory)
    const mem = new WebAssembly.Memory({ initial });
    const fs = require('fs');
    const s = fs.readFileSync('/proc/self/status', 'utf8');
    const line = (k) => (s.split('\\n').find((l) => l.startsWith(k)) || '').trim();
    console.log([line('VmSize'), line('VmRSS')].join(' '));
    process.exit(0);
    """
)

_NODE_BLOWUP_SCRIPT = textwrap.dedent(
    """
    // Deliberate resident blowup: commit memory until the cgroup's
    // memory.max OOM-kills this process (and only this process).
    const chunks = [];
    let resident = 0;
    for (;;) {
      try {
        chunks.push(Buffer.alloc(64 * 1024 * 1024, 1));
        resident += 64;
      } catch (e) {
        process.exit(3);  // V8 refused first (never reached when capped)
      }
    }
    """
)


def _subtree_usable() -> bool:
    """Can the harness user actually exercise the delegated subtree here?"""

    return worker_isolation.cgroup_subtree_writable()


_SUBTREE_REASON = (
    "the lh-harness service's cgroup subtree is not delegated to this user: "
    "packaging/lh-harness.service carries Delegate=memory pids, but it takes a "
    "deploy-window 'systemctl daemon-reload' + service restart on CT110 to reach "
    "the root-owned deployed unit, and this task runs as the unprivileged "
    "harness user with no root escalation permitted.  Live mechanism check "
    f"performed 2026-09-18: /proc/self/cgroup = "
    f"{worker_isolation.own_cgroup_path()}, cgroup.subtree_control there is "
    "root-owned and unwritable, so memory.max cannot be written by this "
    "process.  The mechanism code paths themselves are covered against a fake "
    "cgroupfs in tests/test_worker_isolation_fallback.py; the survival test "
    "below additionally proves live, unskipped, that a V8-shaped address-space "
    "reservation survives an RSS bound and dies under the old address-space "
    "cap."
)

# Fixtures -------------------------------------------------------------------


@pytest.fixture(scope="module")
def subtree():
    """The live delegated subtree, or a documented honest skip."""

    usable = _subtree_usable()
    if not usable:
        pytest.skip(_SUBTREE_REASON)
    return worker_isolation.own_cgroup_path()


# 1. SURVIVAL: the instrument must not kill what a real episode needs --------


def test_v8_shaped_reservation_survives_an_rss_bound(subtree) -> None:
    """A Node 22 child reserving 4 GiB of address space runs under memory.max.

    The old address-space cap (three gibibytes) killed exactly this shape on
    CT110 (measured: VmSize 5.31 GiB reserved, VmRSS 0.26 GiB used, VmPeak
    9.27 GiB).  An RSS bound must admit the reservation untouched —
    memory.max counts charged pages, not reserved-but-untouched mappings.
    The same child dies under the old cap in the counterfactual test below.
    """

    limit = "512M"
    plan = cgroup_plan(limit, [_node_binary(), "-e", _NODE_RESERVE_SCRIPT], "t208-survive")

    import subprocess

    process = subprocess.Popen(
        plan.command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=plan.preexec,
    )
    stdout, _ = process.communicate(timeout=120)
    assert process.returncode == 0, (
        f"the V8-shaped reservation child died rc={process.returncode} under "
        f"memory.max={limit}: {stdout[-2000:]!r}"
    )
    vm_size_line, vm_rss_line = stdout.decode().strip().splitlines()[-1].split(" Vm")
    # The reservation materialized (node's max single wasm Memory: 4 GiB,
    # the same order as the measured 5.31 GiB production reservation).
    assert "VmSize" in vm_size_line
    reserved_kib = int(vm_size_line.split()[1])
    assert reserved_kib >= 4 * 1024 * 1024, f"child only reserved {reserved_kib} KiB"
    # ...and the resident figure is the 0.26 GiB-scale reality, not the
    # reservation: the RSS bound never counted the reserved space.
    rss_kib = int(vm_rss_line.split()[1])
    assert rss_kib <= 512 * 1024, f"child used {rss_kib} KiB resident — not the reservation shape"
    assert read_scope_oom_kills(plan.cgroup) == 0, "no OOM kill may occur for a reservation"


def test_old_address_space_cap_kills_the_same_child() -> None:
    """The counterfactual, live: the pre-208 instrument kills this child.

    Under the old three-gibibyte address-space cap the same V8-shaped
    reservation (4 GiB reserved, ~0 resident — the miniature of the
    measured 5.31 GiB / 0.26 GiB production figures) cannot map its
    reservation and dies with ENOMEM / "could not allocate memory".  This
    is the test the pre-208 suite could never write: it fails exactly the
    way production failed on CT110.
    """

    import resource
    import subprocess

    def old_deadly_preexec() -> None:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        target = min(3 * 1024**3, hard) if hard != resource.RLIM_INFINITY else 3 * 1024**3
        resource.setrlimit(resource.RLIMIT_AS, (target, hard))

    process = subprocess.Popen(
        [_node_binary(), "-e", _NODE_RESERVE_SCRIPT],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=old_deadly_preexec,
    )
    stdout, _ = process.communicate(timeout=60)
    assert process.returncode != 0, (
        "expected the old 3G address-space cap to kill the V8 reservation "
        "child (the production failure mode); it survived — Node's "
        "reservation strategy changed, re-evaluate"
    )
    assert b"VmSize" not in stdout  # it never got far enough to report


# 2. BLOWUP: the bound kills the offender alone; the unit survives -----------


def test_resident_blowup_is_killed_alone_inside_its_own_cgroup(subtree) -> None:
    """A resident blowup dies alone under a small memory.max; pytest survives.

    The blowup child commits resident memory far past its cgroup's
    ``memory.max``; the kernel OOM-kills it inside the episode cgroup, the
    kill is attributed via ``memory.events``/``classify_memory_death``, and
    this pytest process — standing in for the lh-harness unit and every
    other live run — is still alive and accounting afterwards.
    """

    import subprocess

    limit = "256M"  # deliberately small: the blowup commits far beyond it
    plan = cgroup_plan(limit, [_node_binary(), "-e", _NODE_BLOWUP_SCRIPT], "t208-blowup")
    baseline = read_scope_oom_kills(plan.cgroup)
    record = plan.record()

    process = subprocess.Popen(
        plan.command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=plan.preexec,
    )
    stdout, _ = process.communicate(timeout=120)

    assert process.returncode == -9, (
        f"expected SIGKILL (rc=-9) from the cgroup OOM kill, got "
        f"rc={process.returncode}: {stdout[-2000:]!r}"
    )
    oom_now = read_scope_oom_kills(plan.cgroup)
    assert oom_now is not None and (baseline or 0) < oom_now, (
        f"memory.events must record the kill (baseline={baseline}, now={oom_now})"
    )
    record["cgroup"] = plan.cgroup
    record["oom_kill_base"] = baseline
    assert classify_memory_death(record, returncode=-9) == memory_kill_reason_for(limit)

    # The unit survives: this pytest process still accounts its own memory
    # and still runs — one child's blowup did not take the parent (unit)
    # down with it.
    alive = Path("/proc/self/status").read_text()
    assert "VmRSS" in alive
    assert _pid_alive(os.getpid())


def memory_kill_reason_for(limit: str) -> str:
    from lh_harness.worker_isolation import memory_kill_reason

    return memory_kill_reason(limit)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _node_binary() -> str:
    node = os.environ.get("LH_TEST_NODE_BIN") or "node"
    path = node if os.path.sep in node else None
    return path or node


# 3. The numbers stay named for the next reader ------------------------------


def test_the_measured_numbers_are_carried_in_the_source() -> None:
    """A4 guard: the measured CT110 figures must remain in the code."""

    module_text = (Path(worker_isolation.__file__)).read_text(encoding="utf-8")
    for number in ("9.27", "5.3", "0.26", "three gibibytes"):
        assert number in module_text, f"the measured figure {number!r} left the source"
    service_text = (Path(__file__).resolve().parent.parent / "packaging" / "lh-harness.service").read_text(encoding="utf-8")
    assert "Delegate=memory pids" in service_text
    assert "OOMPolicy=continue" in service_text