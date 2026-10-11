from pathlib import Path

import pytest

from lh_harness.manager import _GateContext, _human_gate
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
async def test_round_wait_occurs_after_executor_and_auditor(tmp_path: Path):
    calls = []
    replies = iter([
        "Next: cli\n\nCurrent Task State:\npartial\n\nPlan:\nmake one change",
        "executor returned partial work",
        "Status: incomplete\nIntegrity: clean\nContract audit: aligned\n\nSummary:\nneeds more work",
    ])
    class Agent:
        async def run_episode(self, prompt, env, budget, live_trajectory_path=None):
            calls.append("role")
            return EpisodeResult(status="done", actions_log=next(replies))
    async def hook(projection):
        assert calls == ["role", "role", "role"]
        assert "needs more work" in projection["auditor_report"]
        calls.append("debrief")
        return {"action": "wait", "receipt_ref": "outbox:1",
                "checkpoint": projection["checkpoint"], "reply_required": True}
    report = await run(task="make and verify one change", agent=Agent(),
        env=LocalEnvironment(str(tmp_path / "env")),
        config=HarnessConfig(max_total_episodes=3, prompt_language="en",
            workspace_path=str(tmp_path / "workspace"), harness_dir=str(tmp_path / "harness"),
            log_dir=str(tmp_path / "logs")), parent_debrief_hook=hook)
    assert calls == ["role", "role", "role", "debrief"]
    assert report["rounds_run"] == 1
    assert report["abort_reason"] == "needs_parent_input"
    assert not report["completion_satisfied"]
