"""easybutt0n-17: the HUMAN close-out route and the artifact head sha.

This module pins the two gaps the close-out contract still had after the
first pass:

1. The HUMAN closer had no real production route.  The manager could read a
   ``close_out_form`` off a gate decision, but nothing in the running system
   ever carried one: the HTTP gate-resolution route, the MCP
   ``harness_resolve_gate`` tool and the dashboard's gate hook all dropped
   it, and ``/stop`` closed a run leaving NO close-out record at all.  These
   tests pin the whole chain:

       operator -> HTTP/MCP gate resolution -> durable approval record
                -> dashboard gate hook -> manager's human close path
                -> the ONE shared validator -> closeout.json + report.json

   and the ``/stop`` route, which closes a run from the control plane
   without a gate at all.

2. ``artifact_refs`` carried no HEAD sha, so a delivered PR named a number
   and a URL but not the commit it delivered.  A PR number can be
   re-pointed at a different commit later, so "we opened PR 96" is not
   evidence of "we delivered commit 773be7e".

``status`` (the process lifecycle) is NOT touched by any of this.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lh_harness.closeout import (
    CLOSE_OUT_OUTCOMES,
    CloseOutValidationError,
    load_close_out,
    validate_close_out,
)
from lh_harness.dashboard.gate import make_human_hook
from lh_harness.dashboard.state import ApprovalOption, DashboardState
from lh_harness.manager import _record_close_out
from lh_harness.supervisor.service import RunSupervisor

# The worked example's artifact: hydra PR 96 at head 773be7e.
_PR_HEAD_SHA = "773be7e"
_PR_REPO = "cogniziocompany/cognizioware-hydra"
_PR_URL = f"https://github.com/{_PR_REPO}/pull/96"


def _delivered_form(*, closed_by: str = "human") -> dict:
    """A complete, valid close-out FORM naming a verifiable PR artifact."""

    return {
        "outcome": "delivered",
        "outcome_reason": "resolved gate 7112035f454d",
        "artifact_refs": [
            {
                "kind": "pr",
                "repo": _PR_REPO,
                "ref": "96",
                "url": _PR_URL,
                "head_sha": _PR_HEAD_SHA,
            }
        ],
        "closed_by": closed_by,
        "closed_by_identity": "hydrafleet-gateway",
        "closed_at": 1762310000.0,
    }


# --------------------------------------------------------------------------
# 1. The artifact head sha.
# --------------------------------------------------------------------------


def test_a_pr_artifact_carries_its_head_sha() -> None:
    """A delivered PR names the commit it delivered, not just its number."""

    record = validate_close_out(_delivered_form(), closed_by="human")
    assert len(record.artifact_refs) == 1
    ref = record.artifact_refs[0]
    assert ref.kind == "pr"
    assert ref.repo == _PR_REPO
    assert ref.ref == "96"
    assert ref.url == _PR_URL
    assert ref.head_sha == _PR_HEAD_SHA
    # The record is JSON-serialisable with the head sha in it.
    assert record.to_dict()["artifact_refs"][0]["head_sha"] == _PR_HEAD_SHA


def test_a_pr_artifact_without_a_head_sha_is_refused() -> None:
    """A PR number alone is not a verifiable artifact."""

    form = _delivered_form()
    form["artifact_refs"][0].pop("head_sha")
    with pytest.raises(CloseOutValidationError) as excinfo:
        validate_close_out(form, closed_by="human")
    assert "head_sha" in str(excinfo.value)


def test_a_pr_artifact_without_a_repo_is_refused() -> None:
    form = _delivered_form()
    form["artifact_refs"][0].pop("repo")
    with pytest.raises(CloseOutValidationError) as excinfo:
        validate_close_out(form, closed_by="human")
    assert "repo" in str(excinfo.value)


def test_a_commit_artifact_mirrors_its_sha_into_the_head_sha() -> None:
    """One field name for the head, whatever the artifact's kind."""

    form = _delivered_form()
    form["artifact_refs"] = [
        {
            "kind": "commit",
            "repo": _PR_REPO,
            "ref": _PR_HEAD_SHA,
            "url": f"https://github.com/{_PR_REPO}/commit/{_PR_HEAD_SHA}",
        }
    ]
    record = validate_close_out(form, closed_by="human")
    assert record.artifact_refs[0].head_sha == _PR_HEAD_SHA


def test_a_delivery_still_needs_an_artifact() -> None:
    """The head sha did not weaken the artifactless-delivery refusal."""

    form = _delivered_form()
    form["artifact_refs"] = []
    with pytest.raises(CloseOutValidationError) as excinfo:
        validate_close_out(form, closed_by="human")
    assert "artifact" in str(excinfo.value).lower()


def test_the_outcome_vocabulary_is_unchanged() -> None:
    assert CLOSE_OUT_OUTCOMES == (
        "delivered",
        "delivered_partial",
        "blocked",
        "failed",
        "aborted",
        "incomplete",
    )


# --------------------------------------------------------------------------
# 2. The gate-resolution production route carries the close-out form.
# --------------------------------------------------------------------------


def _state(tmp_path: Path) -> DashboardState:
    run_dir = tmp_path / "run-1"
    (run_dir / "role_orchestration").mkdir(parents=True)
    return DashboardState(run_dir, runs_root=tmp_path, control_enabled=True)


def test_the_gate_resolution_route_carries_the_close_out_form(tmp_path: Path) -> None:
    """An operator's form survives the durable approval record."""

    state = _state(tmp_path)
    approval = state.create_approval(
        title="Task complete. Continue the run?",
        options=[ApprovalOption("continue", "Continue run"), ApprovalOption("stop", "End run")],
    )
    assert state.resolve_approval(
        approval.approval_id,
        action="stop",
        reason="delivered hydra#96",
        close_out_form=_delivered_form(),
    ) is True
    resolved = state.get_approval(approval.approval_id)
    assert resolved is not None
    assert resolved.status == "resolved"
    assert resolved.action == "stop"
    assert resolved.close_out_form["outcome"] == "delivered"
    assert resolved.close_out_form["artifact_refs"][0]["head_sha"] == _PR_HEAD_SHA


def test_the_gate_resolution_route_keeps_the_form_on_disk(tmp_path: Path) -> None:
    """The durable record keeps the form, so an auditor can read it later."""

    state = _state(tmp_path)
    approval = state.create_approval(
        title="Task complete. Continue the run?",
        options=[ApprovalOption("continue", "Continue run"), ApprovalOption("stop", "End run")],
    )
    state.resolve_approval(
        approval.approval_id,
        action="stop",
        close_out_form=_delivered_form(),
    )
    # Reading the approval applies the pending resolution and writes the
    # resolved record to the durable log.
    assert state.get_approval(approval.approval_id) is not None
    approvals = (tmp_path / "run-1" / "role_orchestration" / "approvals.jsonl").read_text(
        encoding="utf-8"
    )
    records = [json.loads(line) for line in approvals.splitlines() if line.strip()]
    resolved = [r for r in records if r.get("status") == "resolved"]
    assert resolved, "the resolved approval must be persisted"
    assert resolved[-1]["close_out_form"]["outcome"] == "delivered"
    assert resolved[-1]["close_out_form"]["artifact_refs"][0]["head_sha"] == _PR_HEAD_SHA


def test_the_gate_hook_hands_the_form_to_the_manager(tmp_path: Path) -> None:
    """The dashboard's hook is the manager's only source of the form."""

    state = _state(tmp_path)
    hook = make_human_hook(state, poll_interval=0.01)

    async def _resolve() -> None:
        # Wait for the hook to raise its gate, then resolve it with a form.
        for _ in range(500):
            pending = [a for a in state.list_approvals() if a.get("status") == "pending"]
            if pending:
                state.resolve_approval(
                    pending[0]["approval_id"],
                    action="stop",
                    close_out_form=_delivered_form(),
                )
                return
            await __import__("asyncio").sleep(0.01)
        raise AssertionError("the hook never raised a gate")

    import asyncio

    async def _drive() -> dict:
        task = asyncio.ensure_future(
            hook(
                {
                    "phase": "end_of_round",
                    "outcome": "completed",
                    "reached_max": False,
                    "round_index": 1,
                    "task": "ship it",
                    "task_state": "done",
                    "rounds": [],
                }
            )
        )
        await _resolve()
        return await task

    decision = asyncio.run(_drive())
    assert decision["action"] == "stop"
    assert decision["close_out_form"]["outcome"] == "delivered"
    assert decision["close_out_form"]["artifact_refs"][0]["head_sha"] == _PR_HEAD_SHA


def test_the_gate_hook_reports_no_form_when_the_operator_supplied_none(tmp_path: Path) -> None:
    """A stop with no form is an incomplete close-out, not a delivery."""

    state = _state(tmp_path)
    hook = make_human_hook(state, poll_interval=0.01)

    import asyncio

    async def _resolve() -> None:
        for _ in range(500):
            pending = [a for a in state.list_approvals() if a.get("status") == "pending"]
            if pending:
                state.resolve_approval(pending[0]["approval_id"], action="stop")
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the hook never raised a gate")

    async def _drive() -> dict:
        task = asyncio.ensure_future(
            hook(
                {
                    "phase": "end_of_round",
                    "outcome": "completed",
                    "reached_max": False,
                    "round_index": 1,
                    "task": "ship it",
                    "task_state": "done",
                    "rounds": [],
                }
            )
        )
        await _resolve()
        return await task

    decision = asyncio.run(_drive())
    assert decision["action"] == "stop"
    assert decision["close_out_form"] == {}


# --------------------------------------------------------------------------
# 3. The /stop route: the operator closes the run from the control plane.
# --------------------------------------------------------------------------


class _Process:
    pid = 9191

    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        # The launcher asks the worker to settle before deciding whether the
        # launch succeeded.  A stubbed worker "exits" immediately.
        return self.returncode if self.returncode is not None else 0


def _supervisor(monkeypatch, tmp_path: Path) -> tuple[RunSupervisor, str]:
    process = _Process()
    monkeypatch.setattr(
        "lh_harness.supervisor.service.subprocess.Popen", lambda *args, **kwargs: process
    )
    monkeypatch.setattr("lh_harness.supervisor.service.os.killpg", lambda *args, **kwargs: None)
    supervisor = RunSupervisor(tmp_path / "runs", workspace_root=tmp_path / "workspace")
    created = supervisor.create_run(task="stop me")
    return supervisor, created["id"]


def test_stop_persists_the_operators_close_out_next_to_report_json(
    monkeypatch, tmp_path: Path
) -> None:
    """``/stop`` is a real closer: it writes the close-out record."""

    supervisor, run_id = _supervisor(monkeypatch, tmp_path)
    logs = tmp_path / "runs" / run_id / "lh_harness"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "report.json").write_text(
        json.dumps({"status": "cancelled", "completion_satisfied": False}), encoding="utf-8"
    )

    result = supervisor.stop(
        run_id,
        close_out_form=_delivered_form(),
        closed_by_identity="hydrafleet-gateway",
    )

    assert result["close_out"]["recorded"] is True
    assert result["close_out"]["outcome"] == "delivered"
    assert result["close_out"]["closed_by"] == "human"

    record = load_close_out(logs)
    assert record is not None
    assert record.outcome == "delivered"
    assert record.delivered is True
    assert record.closed_by == "human"
    assert record.closed_by_identity == "hydrafleet-gateway"
    assert record.artifact_refs[0].head_sha == _PR_HEAD_SHA

    # The record is mirrored into the run's report status fields, and the
    # process lifecycle is untouched.
    report = json.loads((logs / "report.json").read_text(encoding="utf-8"))
    assert report["outcome"] == "delivered"
    assert report["delivered"] is True
    assert report["artifact_refs"][0]["head_sha"] == _PR_HEAD_SHA
    assert report["status"] == "cancelled"


def test_stop_without_a_close_out_leaves_the_run_incomplete(
    monkeypatch, tmp_path: Path
) -> None:
    """``/stop`` no longer closes a run leaving NO close-out record."""

    supervisor, run_id = _supervisor(monkeypatch, tmp_path)
    logs = tmp_path / "runs" / run_id / "lh_harness"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "report.json").write_text(
        json.dumps({"status": "cancelled"}), encoding="utf-8"
    )

    result = supervisor.stop(run_id)

    assert result["close_out"]["recorded"] is True
    assert result["close_out"]["outcome"] == "incomplete"
    assert result["close_out"]["delivered"] is False

    record = load_close_out(logs)
    assert record is not None
    assert record.outcome == "incomplete"
    assert record.delivered is False
    # The refusal reason is recorded, so a reader knows WHY.
    assert "close-out form" in record.outcome_reason.lower()

    report = json.loads((logs / "report.json").read_text(encoding="utf-8"))
    assert report["outcome"] == "incomplete"
    assert report["delivered"] is False
    # The process lifecycle is untouched.
    assert report["status"] == "cancelled"


def test_stop_with_an_invalid_close_out_leaves_the_run_incomplete(
    monkeypatch, tmp_path: Path
) -> None:
    """An invalid form is refused: the run is never reported as delivered."""

    supervisor, run_id = _supervisor(monkeypatch, tmp_path)
    logs = tmp_path / "runs" / run_id / "lh_harness"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "report.json").write_text(json.dumps({"status": "cancelled"}), encoding="utf-8")

    form = _delivered_form()
    form["artifact_refs"] = []  # a delivery with no artifact is refused
    result = supervisor.stop(run_id, close_out_form=form)

    assert result["close_out"]["outcome"] == "incomplete"
    record = load_close_out(logs)
    assert record is not None
    assert record.outcome == "incomplete"
    assert record.delivered is False
    assert "artifact" in record.outcome_reason.lower()


def test_stop_with_an_unknown_outcome_leaves_the_run_incomplete(
    monkeypatch, tmp_path: Path
) -> None:
    """The enum is closed: a synonym is refused, not coerced."""

    supervisor, run_id = _supervisor(monkeypatch, tmp_path)
    logs = tmp_path / "runs" / run_id / "lh_harness"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "report.json").write_text(json.dumps({"status": "cancelled"}), encoding="utf-8")

    form = _delivered_form()
    form["outcome"] = "done"  # not one of the six
    result = supervisor.stop(run_id, close_out_form=form)

    assert result["close_out"]["outcome"] == "incomplete"
    record = load_close_out(logs)
    assert record is not None
    assert record.outcome == "incomplete"
    assert "outcome" in record.outcome_reason.lower()


# --------------------------------------------------------------------------
# 4. The manager's human close path: ONE validator, identical record shape.
# --------------------------------------------------------------------------


def test_the_manager_human_path_persists_the_operators_form(tmp_path: Path) -> None:
    """The manager closes a human-supplied form through the shared validator."""

    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    report = {"status": "cancelled"}
    record = _record_close_out(
        logs_dir=logs_dir,
        form=_delivered_form(),
        closed_by="human",
        identity="hydrafleet-gateway",
        rounds=[],
        report=report,
    )
    assert record is not None
    assert record.outcome == "delivered"
    assert record.closed_by == "human"
    assert record.artifact_refs[0].head_sha == _PR_HEAD_SHA
    assert report["delivered"] is True
    # The process lifecycle is untouched.
    assert report["status"] == "cancelled"


def test_the_manager_human_path_with_no_form_records_incomplete(tmp_path: Path) -> None:
    """A human close with no form leaves the run close-out incomplete."""

    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    report = {"status": "cancelled"}
    record = _record_close_out(
        logs_dir=logs_dir,
        form=None,
        closed_by="human",
        identity="hydrafleet-gateway",
        rounds=[],
        report=report,
    )
    assert record is not None
    assert record.outcome == "incomplete"
    assert record.delivered is False
    assert report["outcome"] == "incomplete"
    assert report["delivered"] is False
    # The run is NOT reported as completed.
    assert report["status"] == "cancelled"
    assert "close-out form" in report["outcome_reason"].lower()


def test_the_agent_and_human_paths_produce_identical_record_shapes() -> None:
    """ONE validator, so both closers produce the SAME record shape."""

    agent_form = _delivered_form(closed_by="agent")
    agent_form["closed_by_identity"] = "manager"
    agent_record = validate_close_out(agent_form, closed_by="agent")
    human_record = validate_close_out(_delivered_form(), closed_by="human")
    assert agent_record.to_dict().keys() == human_record.to_dict().keys()
    assert agent_record.schema_version == human_record.schema_version
    assert agent_record.outcome == human_record.outcome == "delivered"
    assert agent_record.delivered is human_record.delivered is True
    assert (
        agent_record.artifact_refs[0].to_dict().keys()
        == human_record.artifact_refs[0].to_dict().keys()
    )
    assert agent_record.artifact_refs[0].head_sha == human_record.artifact_refs[0].head_sha


def test_a_delivery_conflicting_with_the_auditor_is_refused_on_the_human_path() -> None:
    """The auditor is the authority on the human path too."""

    with pytest.raises(CloseOutValidationError) as excinfo:
        validate_close_out(
            _delivered_form(),
            closed_by="human",
            auditor_verdict={"status": "incomplete", "integrity_status": "clean"},
        )
    assert "auditor" in str(excinfo.value).lower()


# --------------------------------------------------------------------------
# 5. The HTTP and MCP production routes carry the form.
# --------------------------------------------------------------------------


def _webapi_fixture(tmp_path: Path):
    """A web app over one run, wired the way the real service wires it."""

    from fastapi.testclient import TestClient

    from lh_harness.webapi.server import create_app

    root = tmp_path / "runs"
    run = root / "run-1"
    role_dir = run / "logs" / "role_management"
    role_dir.mkdir(parents=True)
    (run / "control").mkdir(parents=True)
    (role_dir / "rounds").mkdir(parents=True)
    (run / "logs" / "report.json").write_text(
        json.dumps({"task": "fixture", "status": "running"}), encoding="utf-8"
    )
    (role_dir / "events.jsonl").write_text(
        json.dumps({"ts": 1, "event": "role_harness_start"}) + "\n", encoding="utf-8"
    )
    state = DashboardState(run / "logs", runs_root=root, control_enabled=True)
    app = create_app(
        state=state,
        runs_root=root,
        control_enabled=True,
        auth_token="test-token",
        bind_host="testserver",
    )
    return root, state, TestClient(app)


def test_the_http_resolve_route_carries_the_close_out_form(tmp_path: Path) -> None:
    """The operator's form reaches the run through the HTTP route."""

    root, state, client = _webapi_fixture(tmp_path)
    approval = state.create_approval(
        title="Task complete. Continue the run?",
        options=[
            ApprovalOption(value="continue", label="Continue"),
            ApprovalOption(value="stop", label="End run"),
        ],
    )
    response = client.post(
        f"/api/runs/run-1/approvals/{approval.approval_id}/resolve",
        json={"action": "stop", "close_out_form": _delivered_form()},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    # The form reached the run's durable state.
    resolved = state.get_approval(approval.approval_id)
    assert resolved is not None
    assert resolved.close_out_form["outcome"] == "delivered"
    assert resolved.close_out_form["artifact_refs"][0]["head_sha"] == _PR_HEAD_SHA


def test_the_http_resolve_route_rejects_a_malformed_close_out_form(tmp_path: Path) -> None:
    """A form that is not an object is refused at the edge."""

    root, state, client = _webapi_fixture(tmp_path)
    approval = state.create_approval(
        title="gate",
        options=[
            ApprovalOption(value="continue", label="Continue"),
            ApprovalOption(value="stop", label="End run"),
        ],
    )
    response = client.post(
        f"/api/runs/run-1/approvals/{approval.approval_id}/resolve",
        json={"action": "stop", "close_out_form": "done: hydra#96"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 422
    assert "close_out_form" in response.json()["detail"]


def test_the_mcp_resolve_gate_tool_carries_the_close_out_form(tmp_path: Path) -> None:
    """The fleet/chat gateway route carries the form too.

    This is the route the hydrafleet gateway uses to resolve a gate, so it
    is the production path the worked example travels.
    """

    from lh_harness.mcp_tools import _resolve_gate

    root, state, client = _webapi_fixture(tmp_path)
    approval = state.create_approval(
        title="gate",
        options=[
            ApprovalOption(value="continue", label="Continue"),
            ApprovalOption(value="stop", label="End run"),
        ],
    )
    result = _resolve_gate(
        {
            "run_id": "run-1",
            "approval_id": approval.approval_id,
            "action": "stop",
            "close_out_form": _delivered_form(),
        },
        registry=_Registry(state),
        supervisor=None,
    )
    assert result["ok"] is True
    resolved = state.get_approval(approval.approval_id)
    assert resolved is not None
    assert resolved.close_out_form["outcome"] == "delivered"
    assert resolved.close_out_form["artifact_refs"][0]["head_sha"] == _PR_HEAD_SHA


class _Registry:
    """The tiny slice of the state registry the MCP tool uses."""

    def __init__(self, state: DashboardState) -> None:
        self._state = state

    def state_for(self, run_id: str) -> DashboardState | None:
        return self._state if run_id == "run-1" else None


def test_the_mcp_resolve_gate_tool_rejects_a_non_object_form(tmp_path: Path) -> None:
    from lh_harness.mcp_tools import _resolve_gate

    root, state, client = _webapi_fixture(tmp_path)
    approval = state.create_approval(
        title="gate",
        options=[
            ApprovalOption(value="continue", label="Continue"),
            ApprovalOption(value="stop", label="End run"),
        ],
    )
    result = _resolve_gate(
        {
            "run_id": "run-1",
            "approval_id": approval.approval_id,
            "action": "stop",
            "close_out_form": "done: hydra#96",
        },
        registry=_Registry(state),
        supervisor=None,
    )
    assert result["ok"] is False
    assert result["code"] == 422

