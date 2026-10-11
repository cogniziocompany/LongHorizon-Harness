from pathlib import Path
import json

import pytest

from lh_harness.manager import _GateContext, _human_gate, _resume_parent_debrief
from lh_harness.types import HarnessConfig, ManagedRound
from lh_harness.environment.local import LocalEnvironment
from lh_harness.manager import run
from lh_harness.types import EpisodeResult


def context(tmp_path, hook):
    return _GateContext(
        human_hook=None, task="bounded task",
        rounds=[ManagedRound(round_index=1, next_step="cli", plan_text="plan",
                             executor_output="private trajectory", auditor_report="incomplete")],
        log_dir=tmp_path, config=HarnessConfig(), events_path=tmp_path / "events.jsonl",
        round_budget=2, parent_debrief_hook=hook,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [{}, {"action": "continue"},
    {"action": "continue", "receipt_ref": "receipt", "checkpoint": "stale"}])
async def test_unqualified_parent_decision_holds(tmp_path: Path, decision):
    async def hook(packet):
        assert "private trajectory" not in str(packet)
        return decision
    ctx = context(tmp_path, hook)
    assert await _human_gate(ctx, "progress", 1, "partial")
    assert ctx.abort_reason == "needs_parent_input"
    assert ctx.round_budget == 2


@pytest.mark.asyncio
async def test_durable_informational_debrief_preserves_budget(tmp_path: Path):
    async def hook(packet):
        return {"action": "continue", "checkpoint": packet["checkpoint"],
                "receipt_ref": "outbox:1", "reply_required": False, "extra_rounds": 99}
    ctx = context(tmp_path, hook)
    assert not await _human_gate(ctx, "progress", 1, "partial")
    assert ctx.round_budget == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_ref", [None, "parent-reply:1"])
async def test_required_parent_reply_is_distinct_from_transport_receipt(tmp_path: Path, reply_ref):
    async def hook(packet):
        return {"action": "continue", "checkpoint": packet["checkpoint"],
                "receipt_ref": "outbox:1", "reply_required": True,
                "parent_reply_ref": reply_ref, "instructions": "bounded clarification"}
    ctx = context(tmp_path, hook)
    assert await _human_gate(ctx, "progress", 1, "partial") is (reply_ref is None)
    assert ctx.carryover_instructions == ("bounded clarification" if reply_ref else "")


@pytest.mark.asyncio
async def test_unavailable_parent_holds_without_exposing_error(tmp_path: Path):
    async def hook(packet):
        raise TimeoutError("secret provider response")
    ctx = context(tmp_path, hook)
    assert await _human_gate(ctx, "progress", 1, "partial")
    assert "secret" not in (tmp_path / "events.jsonl").read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["snapshot", "round", "task"])
async def test_resume_refuses_changed_checkpoint_sources(tmp_path: Path, mutation):
    async def hook(projection):
        return {"action": "wait", "checkpoint": projection["checkpoint"],
                "receipt_ref": "outbox:1", "reply_required": True}
    ctx = context(tmp_path, hook)
    assert await _human_gate(ctx, "progress", 1, "partial")
    if mutation == "snapshot":
        path = tmp_path / "role_orchestration" / "parent_debrief.json"
        snapshot = json.loads(path.read_text())
        snapshot["projection"]["outcome"] = "completed"
        path.write_text(json.dumps(snapshot))
    elif mutation == "round":
        ctx.rounds[-1].auditor_report = "changed evidence"
    else:
        ctx.task = "expanded scope"
    async def forbidden(projection):
        pytest.fail("changed checkpoint must not reach parent transport")
    ctx.parent_debrief_hook = forbidden
    assert await _resume_parent_debrief(ctx)
    assert ctx.abort_reason == "needs_parent_input"


@pytest.mark.asyncio
async def test_resume_reuses_original_checkpoint_and_budget(tmp_path: Path):
    seen = []
    async def waiting(projection):
        seen.append(dict(projection))
        return {"action": "wait", "checkpoint": projection["checkpoint"],
                "receipt_ref": "outbox:1", "reply_required": True}
    ctx = context(tmp_path, waiting)
    assert await _human_gate(ctx, "progress", 1, "partial")
    ctx.round_budget = 20
    async def answered(projection):
        assert projection == seen[0]
        return {"action": "continue", "checkpoint": projection["checkpoint"],
                "receipt_ref": "outbox:1", "reply_required": True,
                "parent_reply_ref": "reply:1", "instructions": "clarification"}
    ctx.parent_debrief_hook = answered
    assert not await _resume_parent_debrief(ctx)
    assert ctx.round_budget == 2
    assert ctx.carryover_instructions == "clarification"


@pytest.mark.asyncio
async def test_crash_before_new_debrief_does_not_skip_boundary(tmp_path: Path):
    async def hook(projection):
        return {"action": "continue", "checkpoint": projection["checkpoint"],
                "receipt_ref": "outbox:1", "reply_required": False}
    ctx = context(tmp_path, hook)
    assert not await _human_gate(ctx, "progress", 1, "partial")
    ctx.rounds.append(ManagedRound(round_index=2, next_step="cli", plan_text="next plan"))
    assert await _resume_parent_debrief(ctx)
    assert ctx.abort_reason == "needs_parent_input"


@pytest.mark.asyncio
async def test_missing_checkpoint_with_enabled_boundary_holds(tmp_path: Path):
    ctx = context(tmp_path, None)
    role = tmp_path / "role_orchestration"
    role.mkdir()
    (role / "parent_debrief_enabled.json").write_text('{"version":1}')
    assert await _resume_parent_debrief(ctx)
    assert ctx.abort_reason == "needs_parent_input"


@pytest.mark.asyncio
@pytest.mark.parametrize("reflection_text", ["unstructured completion", "private provider failure"])
async def test_receipt_cannot_override_unqualified_manager_reflection(tmp_path: Path, reflection_text):
    class Agent:
        async def run_episode(self, prompt, env, budget, live_trajectory_path=None):
            return EpisodeResult(status="done", actions_log=reflection_text)
    async def hook(projection):
        assert projection["reflection"] == {"status": "unavailable", "reason": "reflection_unqualified"}
        assert reflection_text not in str(projection)
        return {"action": "continue", "checkpoint": projection["checkpoint"],
                "receipt_ref": "outbox:1", "reply_required": False}
    ctx = context(tmp_path, hook)
    ctx.reflection_agent = Agent()
    ctx.env = LocalEnvironment(str(tmp_path / "env"))
    assert await _human_gate(ctx, "progress", 1, "partial")
    assert ctx.abort_reason == "needs_parent_input"
    assert ctx.round_budget == 2


@pytest.mark.asyncio
async def test_round_wait_occurs_after_executor_and_auditor(tmp_path: Path):
    calls = []
    replies = iter([
        "Next: cli\n\nCurrent Task State:\npartial\n\nPlan:\nmake one change",
        "executor returned partial work",
        "Status: incomplete\nIntegrity: clean\nContract audit: aligned\n\nSummary:\nneeds more work",
        json.dumps({"summary": "Auditor reports incomplete work", "disposition": "wait",
                    "next_slice": "", "question": "Which remaining gap should be addressed?", "evidence_refs": []}),
    ])
    class Agent:
        async def run_episode(self, prompt, env, budget, live_trajectory_path=None):
            calls.append("role")
            return EpisodeResult(status="done", actions_log=next(replies))
    async def hook(projection):
        assert calls == ["role", "role", "role", "role"]
        assert "needs more work" in projection["auditor_report"]
        assert projection["reflection"]["status"] == "qualified"
        calls.append("debrief")
        return {"action": "wait", "receipt_ref": "outbox:1",
                "checkpoint": projection["checkpoint"], "reply_required": True}
    report = await run(task="make and verify one change", agent=Agent(),
        env=LocalEnvironment(str(tmp_path / "env")),
        config=HarnessConfig(max_total_episodes=3, prompt_language="en",
            workspace_path=str(tmp_path / "workspace"), harness_dir=str(tmp_path / "harness"),
            log_dir=str(tmp_path / "logs")), parent_debrief_hook=hook)
    assert calls == ["role", "role", "role", "role", "debrief"]
    assert report["rounds_run"] == 1
    assert report["abort_reason"] == "needs_parent_input"
    assert not report["completion_satisfied"]

    # Resume must revisit the same debrief, not run Manager/Executor first or
    # silently turn the configured additional budget into permission to proceed.
    observed = []
    async def still_waiting(projection):
        observed.append(projection)
        return {"action": "wait", "receipt_ref": "outbox:1",
                "checkpoint": projection["checkpoint"], "reply_required": True}
    resumed = await run(task="make and verify one change", agent=Agent(),
        env=LocalEnvironment(str(tmp_path / "env")),
        config=HarnessConfig(max_total_episodes=3, prompt_language="en",
            workspace_path=str(tmp_path / "workspace"), harness_dir=str(tmp_path / "harness"),
            log_dir=str(tmp_path / "logs")), parent_debrief_hook=still_waiting, resume=True)
    assert calls == ["role", "role", "role", "role", "debrief"]
    assert observed[0]["round_index"] == 1
    assert resumed["rounds_run"] == 1
    assert resumed["abort_reason"] == "needs_parent_input"

    missing_adapter = await run(task="make and verify one change", agent=Agent(),
        env=LocalEnvironment(str(tmp_path / "env")),
        config=HarnessConfig(max_total_episodes=3,
            workspace_path=str(tmp_path / "workspace"), harness_dir=str(tmp_path / "harness"),
            log_dir=str(tmp_path / "logs")), resume=True)
    assert missing_adapter["abort_reason"] == "needs_parent_input"
    assert calls == ["role", "role", "role", "role", "debrief"]
