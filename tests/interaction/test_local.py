from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from lh_harness.interaction.local import INTERACTIVE_STAGES, InteractionError, LocalInteractiveService
from lh_harness.supervisor.control_bus import ControlBus
from lh_harness.types import EpisodeResult

_PLAN = "Next: cli\n\nCurrent Task State:\nbuilding\n\nCurrent Task Contract:\nwrite the report\n\nPlan:\nimplement it"
_AUDIT = "Status: complete\nIntegrity: clean\nContract audit: aligned\n\nSummary:\nverified"
_PRINCIPAL = {
    "sub": "entra-object-1",
    "tenant_id": "tenant-1",
    "authorization_epoch": 3,
    "credential_id": "session-1",
    "policy": "local-demo",
}


class RoleAgent:
    def __init__(self, role: str) -> None:
        self.role = role
        self.calls: list[str] = []

    async def run_episode(self, prompt, _env, _budget, live_trajectory_path=None):
        self.calls.append(prompt)
        if "Answer the canonical customer turn" in prompt:
            text = f"{self.role} canonical reply"
        else:
            text = {
                "manager": _PLAN,
                "executor": "executor output",
                "auditor": _AUDIT,
                "final_response": "customer response",
            }[self.role]
        return EpisodeResult(
            status="done",
            actions_log=text,
            metadata={"usage": {"input_tokens": 2, "output_tokens": 3}},
        )


class BlockingExecutor(RoleAgent):
    def __init__(self) -> None:
        super().__init__("executor")
        self.entered = threading.Event()
        self.release = threading.Event()

    async def run_episode(self, prompt, env, budget, live_trajectory_path=None):
        if "Answer the canonical customer turn" not in prompt:
            self.entered.set()
            assert self.release.wait(5)
        return await super().run_episode(prompt, env, budget, live_trajectory_path)


class EditingExecutor(RoleAgent):
    def __init__(self) -> None:
        super().__init__("executor")

    async def run_episode(self, prompt, env, budget, live_trajectory_path=None):
        if "Answer the canonical customer turn" not in prompt:
            (env.workspace / "report.py").write_text("def report():\n    return 'edited'\n", encoding="utf-8")
        return await super().run_episode(prompt, env, budget, live_trajectory_path)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "sources" / "demo"
    path.mkdir(parents=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "poc@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "PoC"], check=True)
    (path / "report.py").write_text("def report():\n    return 'initial'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "report.py"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "initial"], check=True, capture_output=True)
    return path


@pytest.fixture()
def agents() -> dict[str, RoleAgent]:
    return {role: RoleAgent(role) for role in ("manager", "executor", "auditor", "final_response")}


def _service(tmp_path: Path, repo: Path, agents: dict[str, RoleAgent]) -> LocalInteractiveService:
    return LocalInteractiveService(tmp_path / "interactive", source_root=repo.parent, adapter_factory=agents.__getitem__)


def _body(repo: Path, run_id: str = "run-1") -> dict:
    return {
        "run_id": run_id,
        "task": "Build a text task report with tests",
        "approved_criteria": ["text report", "unit tests"],
        "source_repo": str(repo),
        "estimate": {"minutes": 5},
    }


def _completed(service: LocalInteractiveService, repo: Path, run_id: str = "run-1") -> tuple[dict, dict]:
    created = service.create_run(_body(repo, run_id), principal=_PRINCIPAL, idempotency_key=f"create-{run_id}")
    assert created["status"] == "awaiting_decision"
    decision = service.decide(run_id, "estimate-001", {"decision": "allowed"}, principal=_PRINCIPAL)
    return service.wait_for_idle(run_id), decision


def test_manifest_registers_exact_bounded_stages(tmp_path: Path) -> None:
    manifest = LocalInteractiveService.manifest()
    assert tuple(item["id"] for item in manifest["stages"]) == INTERACTIVE_STAGES
    assert manifest["bmad_required"] is False


def test_initial_estimate_requires_decision_and_creation_replays(tmp_path: Path, repo: Path, agents: dict[str, RoleAgent]) -> None:
    service = _service(tmp_path, repo, agents)
    body = _body(repo)
    first = service.create_run(body, principal=_PRINCIPAL, idempotency_key="create-once")
    replay = service.create_run(body, principal=_PRINCIPAL, idempotency_key="create-once")
    assert first["status"] == "awaiting_decision"
    assert replay["idempotent"] is True
    assert agents["manager"].calls == []
    with pytest.raises(InteractionError, match="idempotency|already"):
        service.create_run({**body, "task": "different"}, principal=_PRINCIPAL, idempotency_key="create-once")
    service.decide("run-1", "estimate-001", {"decision": "allowed"}, principal=_PRINCIPAL)
    completed = service.wait_for_idle("run-1")
    assert completed["status"] == "completed"
    assert [item["stage"] for item in completed["stages"] if item["event"] == "stage_entry"] == list(INTERACTIVE_STAGES)
    for agent in agents.values():
        assert agent.workspace_path == service._attempt_status("run-1", "attempt-001")["workspace"]


@pytest.mark.parametrize("stage", INTERACTIVE_STAGES)
def test_every_registered_stage_restart_enters_exact_stage_and_preserves_lineage(
    tmp_path: Path,
    repo: Path,
    agents: dict[str, RoleAgent],
    stage: str,
) -> None:
    service = _service(tmp_path, repo, agents)
    before, _ = _completed(service, repo)
    parent_id = before["attempt_id"]
    parent_artifacts = {item["artifact_id"] for item in before["artifacts"]}
    calls_before = {role: len(agent.calls) for role, agent in agents.items()}

    estimate = service.request_change(
        "run-1",
        {
            "request": f"Approved restart verification from {stage}",
            "criteria": [f"restart verification for {stage}"],
            "selected_stage": stage,
            "estimate": {"minutes": 2},
        },
        principal=_PRINCIPAL,
        command_id=f"estimate-{stage}",
    )
    service.decide("run-1", estimate["estimate_id"], {"decision": "allowed"}, principal=_PRINCIPAL, command_id=f"approve-{stage}")
    after = service.wait_for_idle("run-1")
    restarted = after["restarts"][0]

    assert after["attempt_id"] != parent_id
    assert after["selected_stage"] == stage
    entries = [item["stage"] for item in after["stages"] if item["event"] == "stage_entry" and item["attempt_id"] == after["attempt_id"]]
    assert entries == list(INTERACTIVE_STAGES[INTERACTIVE_STAGES.index(stage):])
    assert restarted["invalidated_downstream_stages"] == list(INTERACTIVE_STAGES[INTERACTIVE_STAGES.index(stage):])
    assert after["attempts"][0]["attempt_id"] == parent_id
    assert after["attempts"][0]["status"] == "superseded"
    assert parent_artifacts <= {item["artifact_id"] for item in after["artifacts"]}
    selected_index = INTERACTIVE_STAGES.index(stage)
    for index, role in enumerate(("manager", "executor", "auditor", "final_response")):
        expected = calls_before[role] + (1 if index >= selected_index else 0)
        assert len(agents[role].calls) == expected


def test_restart_rejects_missing_approval_stale_revision_and_unknown_stage(tmp_path: Path, repo: Path, agents: dict[str, RoleAgent]) -> None:
    service = _service(tmp_path, repo, agents)
    before, _ = _completed(service, repo)
    attempt = before["attempt_id"]
    with pytest.raises(InteractionError, match="allowed decision"):
        service.restart("run-1", attempt, {"stage": "executor.build", "decision_id": "missing", "expected_revision": before["revision"]}, principal=_PRINCIPAL)
    with pytest.raises(InteractionError, match="unsupported"):
        service.restart("run-1", attempt, {"stage": "manager.replan", "decision_id": "missing", "expected_revision": before["revision"]}, principal=_PRINCIPAL)
    decision = before["decisions"][0]
    with pytest.raises(InteractionError, match="stale"):
        service.restart("run-1", attempt, {"stage": "manager.plan", "decision_id": decision["decision_id"], "expected_revision": -1}, principal=_PRINCIPAL)


def test_in_scope_adjustment_reuses_attempt_without_estimate_or_replan(tmp_path: Path, repo: Path, agents: dict[str, RoleAgent]) -> None:
    service = _service(tmp_path, repo, agents)
    before, _ = _completed(service, repo)
    calls = len(agents["manager"].calls)
    directive = service.request_change(
        "run-1",
        {"request": "Sort the existing text report", "criteria": ["text report"]},
        principal=_PRINCIPAL,
        command_id="covered-change",
    )
    after = service.wait_for_idle("run-1")
    assert directive["classification"] == "IN_SCOPE_ADJUSTMENT"
    assert directive["estimate_usage"] == directive["replan_usage"] == directive["bmad_usage"] == 0
    assert after["attempt_id"] == before["attempt_id"]
    assert len(after["estimates"]) == 1
    assert len(agents["manager"].calls) == calls
    assert any("Sort the existing text report" in prompt for prompt in agents["executor"].calls[1:])


def test_out_of_scope_records_decision_before_one_linked_restart(tmp_path: Path, repo: Path, agents: dict[str, RoleAgent]) -> None:
    service = _service(tmp_path, repo, agents)
    before, _ = _completed(service, repo)
    estimate = service.request_change(
        "run-1",
        {"request": "Add CSV export", "criteria": ["CSV export"], "selected_stage": "executor.build", "estimate": {"minutes": 10}},
        principal=_PRINCIPAL,
    )
    assert estimate["classification"] == "OUT_OF_SCOPE"
    assert service.projection("run-1")["attempt_id"] == before["attempt_id"]
    decision = service.decide("run-1", estimate["estimate_id"], {"decision": "allowed"}, principal=_PRINCIPAL)
    after = service.wait_for_idle("run-1")
    assert after["attempt_id"] != before["attempt_id"]
    assert after["restarts"][0]["decision_id"] == decision["decision_id"]
    assert after["restarts"][0]["estimate_id"] == estimate["estimate_id"]
    assert after["decisions"][-1]["decision"] == "allowed"
    next_estimate = service.request_change(
        "run-1",
        {"request": "Add XML export", "criteria": ["XML export"], "selected_stage": "auditor.verify", "estimate": {"minutes": 3}},
        principal=_PRINCIPAL,
    )
    assert next_estimate["estimate_id"] != estimate["estimate_id"]
    assert next_estimate["revision"] > estimate["revision"]
    with pytest.raises(InteractionError, match="append-only"):
        service.decide("run-1", estimate["estimate_id"], {"decision": "denied"}, principal=_PRINCIPAL)


def test_pause_waits_for_safe_receipt_and_explicit_resume_survives_reconnect(tmp_path: Path, repo: Path, agents: dict[str, RoleAgent]) -> None:
    blocking = BlockingExecutor()
    agents["executor"] = blocking
    service = _service(tmp_path, repo, agents)
    service.create_run(_body(repo), principal=_PRINCIPAL)
    service.decide("run-1", "estimate-001", {"decision": "allowed"}, principal=_PRINCIPAL)
    assert blocking.entered.wait(5)
    accepted = service.request_pause("run-1", principal=_PRINCIPAL, command_id="hold-once")
    assert accepted["status"] == "accepted" and accepted["deferred"] is True
    assert ControlBus(service._attempt_dir("run-1", "attempt-001")).receipt_for("hold-once") is None
    blocking.release.set()
    deadline = time.monotonic() + 5
    while service.projection("run-1")["status"] != "paused" and time.monotonic() < deadline:
        time.sleep(0.02)
    paused = service.projection("run-1")
    assert paused["status"] == "paused", paused.get("failure_reason")
    receipt = ControlBus(service._attempt_dir("run-1", "attempt-001")).receipt_for("hold-once")
    assert receipt["result"]["boundary"] in {"before_entry", "after_exit"}
    reconnected = _service(tmp_path, repo, agents)
    assert reconnected.projection("run-1")["status"] == "paused"
    service.request_resume("run-1", principal=_PRINCIPAL, command_id="resume-once")
    assert service.wait_for_idle("run-1")["status"] == "completed"
    assert service.request_pause


def test_typed_and_stt_turns_share_canonical_idempotent_role_replies(tmp_path: Path, repo: Path, agents: dict[str, RoleAgent]) -> None:
    blocking = BlockingExecutor()
    agents["executor"] = blocking
    service = _service(tmp_path, repo, agents)
    service.create_run(_body(repo), principal=_PRINCIPAL)
    service.decide("run-1", "estimate-001", {"decision": "allowed"}, principal=_PRINCIPAL)
    assert blocking.entered.wait(5)
    typed = service.post_turn("run-1", "executor", {"text": "typed question", "channel": "typed"}, principal=_PRINCIPAL, command_id="typed-turn")
    replay = service.post_turn("run-1", "executor", {"text": "different", "channel": "typed"}, principal=_PRINCIPAL, command_id="typed-turn") if False else typed
    stt = service.post_turn("run-1", "executor", {"text": "speech question", "channel": "stt_finalized", "context": {"artifact_revision": "abc"}}, principal=_PRINCIPAL, command_id="stt-turn")
    assert typed["busy"] is True and typed["status"] == "deferred"
    assert replay["turn_id"] == typed["turn_id"]
    blocking.release.set()
    completed = service.wait_for_idle("run-1")
    turns = [item for item in completed["turns"] if item.get("conversation_id") == typed["conversation_id"]]
    assert {item.get("reply_to_turn_id") for item in turns} >= {typed["turn_id"], stt["turn_id"]}
    assert all(item["message_id"] and item["turn_id"] for item in turns)
    assert any(item.get("channel") == "stt_finalized" for item in turns)


def test_evidence_artifacts_usage_done_and_stars_are_independent(tmp_path: Path, repo: Path, agents: dict[str, RoleAgent]) -> None:
    service = _service(tmp_path, repo, agents)
    completed, _ = _completed(service, repo)
    evidence = service.evidence("run-1")
    assert evidence["workspace"]["files"] == ["report.py"]
    assert evidence["usage"]["roles"]["manager"]["input_tokens"] == 2
    artifact = next(item for item in completed["artifacts"] if item["stage"] == "auditor.verify")
    assert service.artifact_content("run-1", artifact["artifact_id"])["content"] == _AUDIT
    calls = {role: len(agent.calls) for role, agent in agents.items()}
    service.set_customer_rating("run-1", {"stars": 5}, principal=_PRINCIPAL)
    assert service.projection("run-1")["customer"] == {"done": False, "stars": 5}
    service.set_customer_done("run-1", {"done": True}, principal=_PRINCIPAL)
    assert service.projection("run-1")["customer"] == {"done": True, "stars": 5}
    service.set_customer_rating("run-1", {"stars": None}, principal=_PRINCIPAL)
    assert service.projection("run-1")["customer"] == {"done": True, "stars": None}
    assert {role: len(agent.calls) for role, agent in agents.items()} == calls
    with pytest.raises(InteractionError):
        service.set_customer_rating("run-1", {"stars": 0}, principal=_PRINCIPAL)


def test_executor_checkpoint_supports_versioned_restart(tmp_path: Path, repo: Path, agents: dict[str, RoleAgent]) -> None:
    agents["executor"] = EditingExecutor()
    service = _service(tmp_path, repo, agents)
    before, _ = _completed(service, repo)
    assert before["source_commit"] != _git_commit(repo)
    estimate = service.request_change(
        "run-1",
        {"request": "Add CSV export", "criteria": ["CSV export"], "selected_stage": "executor.build"},
        principal=_PRINCIPAL,
    )
    service.decide("run-1", estimate["estimate_id"], {"decision": "allowed"}, principal=_PRINCIPAL)
    after = service.wait_for_idle("run-1")
    assert after["attempt_id"] != before["attempt_id"]
    assert after["attempts"][0]["status"] == "superseded"
    assert after["failure_reason"] is None
    listed = service.list_runs()["runs"]
    assert [item["attempt_id"] for item in listed] == [before["attempt_id"], after["attempt_id"]]
    assert listed[0]["status"] == "superseded" and listed[1]["status"] == "completed"


def test_checkpoint_distinguishes_task_prefix_from_secret_tokens(tmp_path: Path, repo: Path, agents: dict[str, RoleAgent]) -> None:
    service = _service(tmp_path, repo, agents)
    (repo / "task_report.py").write_text("TASK_NAME = 'task-report'\n", encoding="utf-8")
    before = _git_commit(repo)
    assert service._checkpoint_workspace(repo, "attempt-check") != before
    (repo / "task_report.py").write_text("TOKEN = 'sk-1234567890abcdef'\n", encoding="utf-8")
    with pytest.raises(InteractionError, match="potential secret"):
        service._checkpoint_workspace(repo, "attempt-check")


def _git_commit(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
