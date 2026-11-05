"""easybutt0n-17: the close-out contract where consumers read it.

A contract that lives only in the run layer is invisible to the consumers
that measure it.  This module pins the EXPOSURE half:

* ``GET /api/runs?fields=summary`` carries the authoritative close-out
  signal — ``outcome`` (the six-value enum), the explicit ``delivered``
  boolean, ``outcome_reason``, ``closed_by`` and ``artifact_refs`` — plus
  the process-lifecycle fields a consumer needs to see the two side by
  side (``completion_satisfied``, ``report_status``, ``exit_code``);
* the projection is ADDITIVE: a row whose run carries no close-out record
  keeps exactly the shape it had before the contract existed;
* the queue entry carries the SAME outcome as its run, so a queue consumer
  and a run consumer never disagree;
* the run layer exposes read-only aggregates (per outcome, per node, per
  day) with an observed-vs-derived split, drawn from the single canonical
  vocabulary.

``status`` is NOT touched by any of this.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lh_harness.closeout import (
    CLOSE_OUT_OUTCOMES,
    ArtifactRef,
    CloseOutRecord,
    persist_close_out,
)
from lh_harness.closeout_aggregates import (
    OUTCOME_ORDER,
    aggregate_records,
    aggregate_runs_root,
    current_node,
    outcome_count_matrix,
)

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_PR_HEAD_SHA = "773be7e"
_PR_ARTIFACT = ArtifactRef(
    kind="pr",
    repo="cogniziocompany/cognizioware-hydra",
    ref="96",
    url="https://github.com/cogniziocompany/cognizioware-hydra/pull/96",
    head_sha=_PR_HEAD_SHA,
)


def _record(
    outcome: str,
    *,
    closed_at: float = 1762310000.0,
    derived: bool = False,
    derived_from: str = "",
    artifacts: tuple[ArtifactRef, ...] | None = None,
) -> CloseOutRecord:
    return CloseOutRecord(
        outcome=outcome,
        outcome_reason=f"{outcome} reason",
        artifact_refs=artifacts if artifacts is not None else (),
        closed_by="human",
        closed_by_identity="hydrafleet-gateway",
        closed_at=closed_at,
        delivered=outcome in {"delivered", "delivered_partial"},
        derived=derived,
        derived_from=derived_from,
    )


def _write_run(
    root: Path,
    run_id: str,
    *,
    status: dict,
    report: dict | None = None,
    close_out: CloseOutRecord | None = None,
    owner: dict | None = None,
) -> Path:
    run_dir = root / run_id
    control = run_dir / "control"
    control.mkdir(parents=True)
    (control / "status.json").write_text(json.dumps(status), encoding="utf-8")
    (control / "owner.json").write_text(
        json.dumps(owner or {"task": f"task {run_id}", "workspace": "/tmp/ws", "agent": "codex"}),
        encoding="utf-8",
    )
    logs = run_dir / "lh_harness"
    (logs / "role_orchestration").mkdir(parents=True)
    (logs / "role_orchestration" / "events.jsonl").write_text("", encoding="utf-8")
    if report is not None:
        (logs / "report.json").write_text(json.dumps(report), encoding="utf-8")
    if close_out is not None:
        persist_close_out(logs, close_out)
    return run_dir


# --------------------------------------------------------------------------
# 1. The summary projection carries the new fields.
# --------------------------------------------------------------------------

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from lh_harness.dashboard.state import DashboardState  # noqa: E402
from lh_harness.supervisor.service import RunSupervisor  # noqa: E402
from lh_harness.webapi.server import _build_summary_projection, create_app  # noqa: E402


def test_the_projection_carries_the_close_out_fields() -> None:
    """The projection exposes the new fields, additively."""

    item = {
        "id": "run-1",
        "status": "cancelled",
        "mtime": 1700000000.0,
        "workspace": "/tmp/r",
        "close_out_outcome": "delivered",
        "delivered": True,
        "outcome_reason": "opened hydra#96",
        "closed_by": "human",
        "closed_by_identity": "hydrafleet-gateway",
        "closed_at": 1762310000.0,
        "artifact_refs": [
            {
                "kind": "pr",
                "repo": "cogniziocompany/cognizioware-hydra",
                "ref": "96",
                "url": "https://github.com/cogniziocompany/cognizioware-hydra/pull/96",
                "head_sha": _PR_HEAD_SHA,
                "derived": False,
            }
        ],
        "completion_satisfied": True,
        "report_status": "cancelled",
        "exit_code": 1,
    }
    row = _build_summary_projection(item)
    assert row["outcome"] == "delivered"
    assert row["delivered"] is True
    assert row["outcome_reason"] == "opened hydra#96"
    assert row["closed_by"] == "human"
    assert row["closed_by_identity"] == "hydrafleet-gateway"
    assert row["closed_at"] == 1762310000.0
    assert row["artifact_refs"][0]["url"].endswith("/pull/96")
    assert row["artifact_refs"][0]["ref"] == "96"
    # The PRIMARY PR's head sha is exposed, so a consumer can verify WHICH
    # commit the delivery was.
    assert row["artifact_refs"][0]["head_sha"] == _PR_HEAD_SHA
    assert row["completion_satisfied"] is True
    assert row["report_status"] == "cancelled"
    assert row["exit_code"] == 1
    # The process lifecycle is untouched.
    assert row["status"] == "cancelled"


def test_the_projection_is_additive_for_a_run_with_no_close_out() -> None:
    """A run with no close-out record keeps its old row shape."""

    item = {
        "id": "run-1",
        "status": "failed",
        "mtime": 1700000000.0,
        "workspace": "/tmp/r",
    }
    row = _build_summary_projection(item)
    assert "outcome" not in row
    assert "delivered" not in row
    assert "outcome_reason" not in row
    assert "closed_by" not in row
    assert "artifact_refs" not in row
    assert row["status"] == "failed"


def test_the_projection_keeps_the_lifecycle_outcome_alongside_the_close_out() -> None:
    """The lifecycle value is preserved, not silently dropped."""

    item = {
        "id": "run-1",
        "status": "cancelled",
        "mtime": 1700000000.0,
        "workspace": "/tmp/r",
        "outcome": "cancelled",
        "close_out_outcome": "delivered",
        "delivered": True,
    }
    row = _build_summary_projection(item)
    assert row["outcome"] == "delivered"
    assert row["lifecycle_outcome"] == "cancelled"


def test_the_summary_endpoint_carries_the_close_out(tmp_path: Path) -> None:
    """End to end: a run with a close-out record exposes it."""

    root = tmp_path / "runs"
    root.mkdir()
    _write_run(
        root,
        "run-delivered",
        status={"status": "cancelled"},
        report={"status": "cancelled", "completion_satisfied": True, "exit_code": 1},
        close_out=_record("delivered", artifacts=(_PR_ARTIFACT,)),
    )
    supervisor = RunSupervisor(root, workspace_root=tmp_path)
    dashboard = DashboardState(root / "run-delivered", runs_root=root, control_enabled=False)
    app = create_app(state=dashboard, runs_root=root, run_id="run-delivered", supervisor=supervisor)
    rows = {row["id"]: row for row in TestClient(app).get("/api/runs?fields=summary").json()["runs"]}
    row = rows["run-delivered"]
    assert row["outcome"] == "delivered"
    assert row["delivered"] is True
    assert row["closed_by"] == "human"
    assert row["artifact_refs"][0]["url"].endswith("/pull/96")
    assert row["artifact_refs"][0]["head_sha"] == _PR_HEAD_SHA
    assert row["report_status"] == "cancelled"
    assert row["completion_satisfied"] is True


def test_the_summary_endpoint_leaves_a_run_without_a_close_out_alone(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _write_run(
        root,
        "run-plain",
        status={"status": "failed"},
        report={"status": "failed", "abort_reason": "provider_rate_limit"},
    )
    supervisor = RunSupervisor(root, workspace_root=tmp_path)
    dashboard = DashboardState(root / "run-plain", runs_root=root, control_enabled=False)
    app = create_app(state=dashboard, runs_root=root, run_id="run-plain", supervisor=supervisor)
    rows = {row["id"]: row for row in TestClient(app).get("/api/runs?fields=summary").json()["runs"]}
    row = rows["run-plain"]
    assert "delivered" not in row
    assert "artifact_refs" not in row
    # The legacy lifecycle outcome is untouched.
    assert row["outcome"] == "failed"


# --------------------------------------------------------------------------
# 2. The queue entry carries the SAME outcome as the run.
# --------------------------------------------------------------------------


def test_the_queue_entry_carries_the_same_outcome_as_its_run(tmp_path: Path) -> None:
    from lh_harness.queue import QueueEntry

    entry = QueueEntry(
        queue_id="q1",
        name="task",
        task="do the thing",
        workspace="/tmp/ws",
        max_rounds=3,
        trio="a,b,c",
        priority=0,
        requested_by="ops",
    )
    assert entry.outcome is None
    assert entry.delivered is None
    # The fields round-trip through the durable form.
    payload = entry.to_dict()
    assert payload["outcome"] is None
    assert payload["delivered"] is None

    entry.outcome = "delivered"
    entry.delivered = True
    restored = QueueEntry.from_dict(entry.to_dict())
    assert restored.outcome == "delivered"
    assert restored.delivered is True


def test_the_queue_outcome_uses_the_canonical_vocabulary(tmp_path: Path) -> None:
    """A queue consumer reads the SAME six values the run layer uses."""

    from lh_harness.queue import QueueEntry

    entry = QueueEntry(
        queue_id="q1",
        name="task",
        task="do the thing",
        workspace="/tmp/ws",
        max_rounds=3,
        trio="a,b,c",
        priority=0,
        requested_by="ops",
    )
    for outcome in CLOSE_OUT_OUTCOMES:
        entry.outcome = outcome
        payload = entry.to_dict()
        assert payload["outcome"] in CLOSE_OUT_OUTCOMES
    # A legacy entry (no outcome field at all) still loads.
    legacy = QueueEntry.from_dict(
        {
            "queue_id": "q1",
            "name": "task",
            "task": "do the thing",
            "workspace": "/tmp/ws",
            "max_rounds": 3,
            "trio": "a,b,c",
            "priority": 0,
            "requested_by": "ops",
        }
    )
    assert legacy.outcome is None
    assert legacy.delivered is None


def test_the_queue_entry_and_the_run_agree_on_the_outcome(tmp_path: Path) -> None:
    """The queue entry's outcome is the run's outcome, verbatim."""

    from lh_harness.queue import QueueEntry

    run_record = _record("delivered_partial", artifacts=(_PR_ARTIFACT,))
    entry = QueueEntry(
        queue_id="q1",
        name="task",
        task="do the thing",
        workspace="/tmp/ws",
        max_rounds=3,
        trio="a,b,c",
        priority=0,
        requested_by="ops",
        outcome=run_record.outcome,
        delivered=run_record.delivered,
    )
    assert entry.outcome == run_record.outcome
    assert entry.delivered == run_record.delivered


# --------------------------------------------------------------------------
# 3. The aggregates: per outcome, per node, per day; observed vs derived.
# --------------------------------------------------------------------------


def test_the_aggregates_use_the_canonical_vocabulary() -> None:
    matrix = outcome_count_matrix([])
    assert matrix["vocabulary"] == list(CLOSE_OUT_OUTCOMES)
    assert set(matrix["outcomes"]) == set(CLOSE_OUT_OUTCOMES)
    assert matrix["total"] == 0
    for outcome in CLOSE_OUT_OUTCOMES:
        assert matrix["outcomes"][outcome] == 0
        assert matrix["observed"][outcome] == 0
        assert matrix["derived"][outcome] == 0


def test_the_count_matrix_splits_observed_from_derived() -> None:
    records = [
        _record("delivered", artifacts=(_PR_ARTIFACT,)),
        _record("delivered", derived=True, derived_from="rationale_parse", artifacts=(_PR_ARTIFACT,)),
        _record("blocked"),
        _record("incomplete"),
    ]
    matrix = outcome_count_matrix(records)
    assert matrix["total"] == 4
    assert matrix["outcomes"]["delivered"] == 2
    assert matrix["outcomes"]["blocked"] == 1
    assert matrix["outcomes"]["incomplete"] == 1
    assert matrix["observed"]["delivered"] == 1
    assert matrix["derived"]["delivered"] == 1
    assert matrix["observed"]["blocked"] == 1
    assert matrix["derived"]["blocked"] == 0
    # observed + derived == total, per outcome.
    for outcome in CLOSE_OUT_OUTCOMES:
        assert (
            matrix["observed"][outcome] + matrix["derived"][outcome]
            == matrix["outcomes"][outcome]
        )


def test_the_count_matrix_refuses_an_unknown_outcome() -> None:
    """A seventh vocabulary must never enter the matrix."""

    rogue = CloseOutRecord(
        outcome="completed",  # a lifecycle value, not a close-out value
        outcome_reason="rogue",
        artifact_refs=(),
        closed_by="human",
        closed_by_identity="ops",
        closed_at=1762310000.0,
        delivered=False,
    )
    matrix = outcome_count_matrix([rogue])
    assert matrix["total"] == 0
    assert set(matrix["outcomes"]) == set(CLOSE_OUT_OUTCOMES)


def test_the_aggregate_splits_by_node_and_by_day() -> None:
    records = [
        _record("delivered", closed_at=1762310000.0, artifacts=(_PR_ARTIFACT,)),
        _record("blocked", closed_at=1762310000.0),
        _record("failed", closed_at=1762400000.0),
    ]
    result = aggregate_records(records, node="ct111")
    assert result["node"] == "ct111"
    assert result["overall"]["total"] == 3
    assert result["overall"]["counts"]["delivered"] == 1
    assert result["by_node"]["ct111"]["total"] == 3
    assert len(result["by_day"]) == 2
    day_totals = {day: row["total"] for day, row in result["by_day"].items()}
    assert sum(day_totals.values()) == 3


def test_the_aggregate_counts_the_delivery_outcomes() -> None:
    records = [
        _record("delivered", artifacts=(_PR_ARTIFACT,)),
        _record("delivered_partial", artifacts=(_PR_ARTIFACT,)),
    ]
    result = aggregate_records(records, node="ct111")
    assert result["overall"]["counts"]["delivered"] == 1
    assert result["overall"]["counts"]["delivered_partial"] == 1
    assert result["overall"]["counts"]["total"] == 2


def test_the_aggregate_over_a_runs_root_reads_the_durable_records(tmp_path: Path) -> None:
    """The aggregates come from the run layer's own durable records."""

    root = tmp_path / "runs"
    root.mkdir()
    _write_run(
        root,
        "run-1",
        status={"status": "cancelled"},
        report={"status": "cancelled"},
        close_out=_record("delivered", artifacts=(_PR_ARTIFACT,)),
    )
    _write_run(
        root,
        "run-2",
        status={"status": "failed"},
        report={"status": "failed"},
        close_out=_record("blocked"),
    )
    _write_run(
        root,
        "run-3",
        status={"status": "failed"},
        report={"status": "failed"},
    )
    result = aggregate_runs_root(root, node="ct111")
    assert result["overall"]["total"] == 2
    assert result["overall"]["counts"]["delivered"] == 1
    assert result["overall"]["counts"]["blocked"] == 1
    assert result["by_node"]["ct111"]["total"] == 2


def test_the_node_comes_from_the_fleet_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LH_HARNESS_FLEET_NODE", "ct111")
    assert current_node() == "ct111"


def test_the_outcome_order_is_the_canonical_enum() -> None:
    assert OUTCOME_ORDER == CLOSE_OUT_OUTCOMES


# --------------------------------------------------------------------------
# 4. The launcher carries the run's close-out onto the queue entry.
# --------------------------------------------------------------------------


class _FakeSupervisor:
    """The minimum the launcher needs to reconcile a launched entry."""

    def __init__(self, runs_root: Path, statuses: dict[str, dict]) -> None:
        self.runs_root = runs_root
        self._statuses = statuses

    def status(self, run_id: str) -> dict:
        return self._statuses.get(run_id, {"status": "unknown"})


def test_the_launcher_copies_the_runs_close_out_onto_the_queue_entry(tmp_path: Path) -> None:
    """The queue entry's outcome is the run's outcome, read from disk."""

    from lh_harness.launcher import Launcher
    from lh_harness.queue import QueueStore, default_queue_config

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    entry = store.create(
        {
            "name": "task",
            "task": "do the thing",
            "workspace": str(tmp_path / "ws"),
            "max_rounds": 3,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ops",
        }
    )
    store.mark_launched(entry.queue_id, "run-1")

    # The run's durable close-out, next to its report.json.
    run_logs = root / "run-1" / "lh_harness"
    run_logs.mkdir(parents=True)
    (run_logs / "report.json").write_text(
        json.dumps({"status": "cancelled", "completion_satisfied": True}), encoding="utf-8"
    )
    persist_close_out(run_logs, _record("delivered", artifacts=(_PR_ARTIFACT,)))

    supervisor = _FakeSupervisor(root, {"run-1": {"status": "cancelled"}})
    config = default_queue_config()
    config["capacity"]["poll_seconds"] = 1.0
    launcher = Launcher(supervisor, store, queue_config=config)
    launcher._update_launched_entries({})

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "done"
    assert updated.outcome == "delivered"
    assert updated.delivered is True


def test_the_launcher_leaves_a_run_without_a_close_out_without_an_outcome(tmp_path: Path) -> None:
    """A run that predates the contract contributes no outcome to the entry."""

    from lh_harness.launcher import Launcher
    from lh_harness.queue import QueueStore, default_queue_config

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    entry = store.create(
        {
            "name": "task",
            "task": "do the thing",
            "workspace": str(tmp_path / "ws"),
            "max_rounds": 3,
            "trio": "kimi",
            "priority": 0,
            "requested_by": "ops",
        }
    )
    store.mark_launched(entry.queue_id, "run-1")

    run_logs = root / "run-1" / "lh_harness"
    run_logs.mkdir(parents=True)
    (run_logs / "report.json").write_text(
        json.dumps({"status": "cancelled", "completion_satisfied": True}), encoding="utf-8"
    )

    supervisor = _FakeSupervisor(root, {"run-1": {"status": "cancelled"}})
    config = default_queue_config()
    config["capacity"]["poll_seconds"] = 1.0
    launcher = Launcher(supervisor, store, queue_config=config)
    launcher._update_launched_entries({})

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "done"
    # Honest, not inferred.
    assert updated.outcome is None
    assert updated.delivered is None
