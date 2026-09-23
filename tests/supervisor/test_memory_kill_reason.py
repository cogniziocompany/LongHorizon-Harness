"""Memory-kill death attribution in the supervisor (TASK 204).

When a run's worker disappears without a final report and the isolation
record that ``_launch_worker`` persisted in the owner shows a memory kill
(per ``worker_isolation.classify_memory_death``), the supervisor records
``failure_reason`` ``"memory limit exceeded (<limit>)"`` — the exact string
``worker_isolation.memory_kill_reason`` produces — instead of the generic
"worker disappeared without a final report".

Three mechanisms, in the order ``prepare_launch`` resolves them:

- MECHANISM_SCOPE (``systemd-run --scope`` + MemoryMax) and MECHANISM_CGROUP
  (the delegated subtree's ``memory.max`` — the mechanism that works on
  CT110) attribute a death either from their cgroup's ``memory.events``
  ``oom_kill`` counter rising past the launch baseline or from a
  ``MemoryError`` traceback in the worker-log tail.
- MECHANISM_UNBOUNDED applied no limit at all, so its deaths are NEVER
  attributed to the memory limit — even with a MemoryError sitting in the
  worker log.

The tests need no systemd and no live cgroup: the classifier's cgroup read
(``read_scope_oom_kills``) is faked against the record the way the real
launch persists it in ``owner.json`` (``memory_isolation``, from
``IsolationPlan.record()`` plus the resolved cgroup and oom baseline), and
the worker log is written at the run-local path the launch itself opens
(``<run>/worker.log``, see ``RunSupervisor._launch_worker``).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir, "src")
)

from lh_harness.supervisor.service import RunSupervisor
from lh_harness import worker_isolation

RUN_ID = "20260923T000000Z_abcdef12"


def _launch_record(
    run_id: str,
    mechanism: str,
    limit: str,
    oom_kill_base: int | None,
) -> dict:
    """The owner ``memory_isolation`` record as the real launch persists it.

    Shaped exactly like ``IsolationPlan.record()`` plus the cgroup/baseline
    fields ``_spawn_isolated`` fills in (service.py: the record is stored
    under ``owner["memory_isolation"]``).
    """

    return {
        "mechanism": mechanism,
        "limit": limit,
        "unit": "lh-worker-test.scope" if mechanism == worker_isolation.MECHANISM_SCOPE else "",
        "cgroup": f"/fake.slice/lh.service/worker-{run_id}"
        if mechanism in (worker_isolation.MECHANISM_SCOPE, worker_isolation.MECHANISM_CGROUP)
        else "",
        "oom_kill_base": oom_kill_base,
    }


def _seed_run(
    tmp_path: Path,
    *,
    record: dict,
    worker_log_tail: str,
    report: dict | None = None,
) -> tuple[RunSupervisor, Path]:
    """A durable run whose worker disappeared: owner, status, no live pid."""

    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    run_dir = supervisor._run_dir(RUN_ID)
    (run_dir / "control").mkdir(parents=True)
    logs = supervisor._run_logs_dir(RUN_ID)
    owner = {
        "run_id": RUN_ID,
        "pid": 0,  # no live process identity: the worker is gone
        "pgid": 0,
        "state": "running",
        "task": "task 204 fixture",
        "agent": "codex",
        "managed": True,
        "memory_isolation": record,
    }
    (run_dir / "control" / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
    (run_dir / "control" / "status.json").write_text(
        json.dumps({"run_id": RUN_ID, "status": "running", "managed": True}),
        encoding="utf-8",
    )
    if report is not None:
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "report.json").write_text(json.dumps(report), encoding="utf-8")
    # The worker log lives run-locally — the path _launch_worker opened and
    # handed to Popen — not under the result-log directory.
    (supervisor._run_dir(RUN_ID) / "worker.log").write_text(worker_log_tail, encoding="utf-8")
    return supervisor, logs


def _refresh_dead(supervisor: RunSupervisor) -> dict:
    """Run the reconciliation as a post-restart poll would see it."""

    with patch.object(supervisor, "_is_alive", return_value=False):
        return supervisor.status(RUN_ID)


# --- memory kills under a limit-carrying mechanism ---------------------------


def test_cgroup_oom_kill_rising_past_baseline_is_a_memory_kill(tmp_path) -> None:
    """MECHANISM_CGROUP: oom_kill above the launch baseline -> memory reason.

    The delegated subtree is the mechanism that actually bounds workers on
    CT110.  The cgroup path in the record is fake, so the classifier's live
    ``memory.events`` read is faked to report a kill past the recorded
    baseline — exactly the evidence a real OOM kill leaves behind.
    """

    record = _launch_record(
        RUN_ID, worker_isolation.MECHANISM_CGROUP, "1G", oom_kill_base=2
    )
    supervisor, logs = _seed_run(tmp_path, record=record, worker_log_tail="episode started\n")

    with patch.object(
        worker_isolation, "read_scope_oom_kills", return_value=3
    ) as fake_read:
        status = _refresh_dead(supervisor)
    fake_read.assert_called_with(record["cgroup"])

    assert status["status"] == "failed"
    assert status["failure_reason"] == "memory limit exceeded (1G)"
    crash = json.loads((logs / "crash_report.json").read_text(encoding="utf-8"))
    assert crash["reason"] == "memory limit exceeded (1G)"


def test_scope_oom_kill_rising_past_baseline_is_a_memory_kill(tmp_path) -> None:
    """MECHANISM_SCOPE: same evidence path as the cgroup mechanism."""

    record = _launch_record(
        RUN_ID, worker_isolation.MECHANISM_SCOPE, "512M", oom_kill_base=None
    )
    supervisor, logs = _seed_run(tmp_path, record=record, worker_log_tail="episode started\n")

    # A baseline of None counts any observed kill (the launch never read the
    # counter, so one kill is enough to attribute the death).
    with patch.object(worker_isolation, "read_scope_oom_kills", return_value=1):
        status = _refresh_dead(supervisor)

    assert status["status"] == "failed"
    assert status["failure_reason"] == "memory limit exceeded (512M)"
    crash = json.loads((logs / "crash_report.json").read_text(encoding="utf-8"))
    assert crash["reason"] == "memory limit exceeded (512M)"


def test_cgroup_memoryerror_tail_is_a_memory_kill(tmp_path) -> None:
    """MECHANISM_CGROUP: a MemoryError traceback in the worker-log tail.

    The second evidence path the classifier accepts for every mechanism
    that applied a limit.  The log is read from the run-local worker.log —
    the regression this file pins: an earlier wire-up read it from the
    result-log directory, where the launch never writes it.
    """

    record = _launch_record(
        RUN_ID, worker_isolation.MECHANISM_CGROUP, "2G", oom_kill_base=0
    )
    supervisor, logs = _seed_run(
        tmp_path,
        record=record,
        worker_log_tail=(
            "Traceback (most recent call last):\n"
            '  File "episode", line 1, in <module>\n'
            "MemoryError\n"
        ),
    )

    # The cgroup in the record does not exist here; its counter read must
    # not be consulted for the attribution to hold.
    with patch.object(worker_isolation, "read_scope_oom_kills", return_value=None):
        status = _refresh_dead(supervisor)

    assert status["status"] == "failed"
    assert status["failure_reason"] == "memory limit exceeded (2G)"
    crash = json.loads((logs / "crash_report.json").read_text(encoding="utf-8"))
    assert crash["reason"] == "memory limit exceeded (2G)"


def test_scope_memoryerror_tail_is_a_memory_kill(tmp_path) -> None:
    """MECHANISM_SCOPE: MemoryError-tail evidence is accepted too."""

    record = _launch_record(
        RUN_ID, worker_isolation.MECHANISM_SCOPE, "768M", oom_kill_base=0
    )
    supervisor, logs = _seed_run(
        tmp_path,
        record=record,
        worker_log_tail="episode output\nTraceback ...\nMemoryError\n",
    )

    with patch.object(worker_isolation, "read_scope_oom_kills", return_value=None):
        status = _refresh_dead(supervisor)

    assert status["failure_reason"] == "memory limit exceeded (768M)"
    crash = json.loads((logs / "crash_report.json").read_text(encoding="utf-8"))
    assert crash["reason"] == "memory limit exceeded (768M)"


# --- the reasons that must NOT change ----------------------------------------


def test_unbounded_deaths_are_never_attributed_to_a_memory_limit(tmp_path) -> None:
    """MECHANISM_UNBOUNDED applied nothing: a MemoryError is not evidence.

    The log-only plan has no bound to exceed, so even a MemoryError
    traceback in the worker log tail keeps the generic disappearance
    reason.  The cgroup counter read must never be consulted either.
    """

    record = _launch_record(
        RUN_ID, worker_isolation.MECHANISM_UNBOUNDED, "2G", oom_kill_base=None
    )
    supervisor, logs = _seed_run(
        tmp_path,
        record=record,
        worker_log_tail="Traceback ...\nMemoryError\n",
    )

    with patch.object(
        worker_isolation, "read_scope_oom_kills", return_value=9
    ) as fake_read:
        status = _refresh_dead(supervisor)
    fake_read.assert_not_called()

    assert status["status"] == "failed"
    assert status["failure_reason"] == "worker disappeared without a final report"
    crash = json.loads((logs / "crash_report.json").read_text(encoding="utf-8"))
    assert crash["reason"] == "worker disappeared without a final report"


def test_oom_counter_not_past_baseline_stays_a_disappearance(tmp_path) -> None:
    """No new kill and no MemoryError: the generic reason survives."""

    record = _launch_record(
        RUN_ID, worker_isolation.MECHANISM_CGROUP, "1G", oom_kill_base=5
    )
    supervisor, logs = _seed_run(tmp_path, record=record, worker_log_tail="normal output\n")

    with patch.object(worker_isolation, "read_scope_oom_kills", return_value=5):
        status = _refresh_dead(supervisor)

    assert status["failure_reason"] == "worker disappeared without a final report"
    crash = json.loads((logs / "crash_report.json").read_text(encoding="utf-8"))
    assert crash["reason"] == "worker disappeared without a final report"


def test_no_isolation_record_at_all_stays_a_disappearance(tmp_path) -> None:
    """A run launched before the isolation work has no record to read."""

    supervisor, logs = _seed_run(
        tmp_path,
        record=None,  # type: ignore[arg-type]
        worker_log_tail="Traceback ...\nMemoryError\n",
    )

    status = _refresh_dead(supervisor)

    assert status["failure_reason"] == "worker disappeared without a final report"
    crash = json.loads((logs / "crash_report.json").read_text(encoding="utf-8"))
    assert crash["reason"] == "worker disappeared without a final report"


def test_a_worker_report_keeps_its_own_reason(tmp_path) -> None:
    """A durable worker report is authoritative over the attribution."""

    record = _launch_record(
        RUN_ID, worker_isolation.MECHANISM_CGROUP, "1G", oom_kill_base=0
    )
    supervisor, logs = _seed_run(
        tmp_path,
        record=record,
        worker_log_tail="Traceback ...\nMemoryError\n",
        report={
            "schema_version": 2,
            "status": "failed",
            "task": "task 204 fixture",
            "completion_satisfied": False,
            "error": "episode aborted: provider timeout",
        },
    )

    with patch.object(worker_isolation, "read_scope_oom_kills", return_value=4):
        status = _refresh_dead(supervisor)

    assert status["failure_reason"] == "episode aborted: provider timeout"
    # The supervisor must NOT overwrite a report the worker itself wrote.
    assert not (logs / "crash_report.json").exists()


# --- the persisted failure report path (task text: service.py:1401) ---------


def test_memory_kill_reason_is_recognized_by_is_memory_kill_reason(tmp_path) -> None:
    """The persistence gate matches the reason by kind, not by string.

    ``is_memory_kill_reason`` must decide persistence — a literal compare
    would silently drop the crash report for every future limit value.
    """

    reason = worker_isolation.memory_kill_reason("3G")
    assert reason == "memory limit exceeded (3G)"
    assert worker_isolation.is_memory_kill_reason(reason)
    assert not worker_isolation.is_memory_kill_reason("worker disappeared without a final report")
    assert not worker_isolation.is_memory_kill_reason(None)