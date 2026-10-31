"""``continue`` at a human gate must not silently grant more rounds.

A ``continue`` decision without an explicit positive integer ``extra_rounds``
used to fall through to ``max_total_episodes`` via an ``or``-fallback at the
gate (``src/lh_harness/manager.py::_human_gate`` and
``src/lh_harness/dashboard/gate.py::make_human_hook``).  The result was a run
with ``max_rounds=8`` that quietly reached rounds 9 and 10 because each
``continue`` button press re-granted the full configured budget.  These tests
pin the new contract: absent/zero/negative/non-integer input grants zero; an
explicit positive integer grants exactly that and is recorded; a replayed
approval id never grants twice; and the snapshot surfaces the effective budget
plus the extension history so a stale ``max_rounds`` cannot hide further
rounds.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lh_harness.dashboard.gate import make_human_hook
from lh_harness.dashboard.state import DashboardState
from lh_harness.manager import _GateContext, _human_gate
from lh_harness.webapi.snapshot import build_snapshot


# --- shared scaffolding ------------------------------------------------------


def _events(events_path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _gate_context(
    *,
    tmp_path: Path,
    round_budget: int,
    hook: Any,
    max_total_episodes: int = 8,
    role_dir: Path | None = None,
    final_response: str = "",
    response_round: int = 0,
    events_path: Path | None = None,
) -> _GateContext:
    role_dir = role_dir or (tmp_path / "role_orchestration")
    role_dir.mkdir(parents=True, exist_ok=True)
    if events_path is None:
        events_path = tmp_path / "events.jsonl"
    return _GateContext(
        config=SimpleNamespace(max_total_episodes=max_total_episodes),
        task="explicit round extension contract",
        rounds=[],
        human_hook=hook,
        log_dir=tmp_path,
        events_path=events_path,
        round_budget=round_budget,
        role_dir=role_dir,
        final_response=final_response,
        response_round=response_round,
    )


# --- AC1: a continue with no extra_rounds grants nothing -------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["absent", "zero", "null", "negative", "float", "non_numeric_string"])
async def test_continue_with_no_explicit_extra_rounds_does_not_extend(
    tmp_path: Path, missing: str
) -> None:
    """A blocked continuation with no explicit positive integer grants zero.

    Previously the manager fell back to ``max_total_episodes`` when the
    operator clicked "Continue" without typing a number.  That is exactly
    the bug: each silent click kept re-granting the full configured budget
    until rounds exceeded ``max_rounds``.
    """
    decision: dict[str, Any] = {"action": "continue"}
    if missing != "absent":
        if missing == "zero":
            decision["extra_rounds"] = 0
        elif missing == "null":
            decision["extra_rounds"] = None
        elif missing == "negative":
            decision["extra_rounds"] = -3
        elif missing == "float":
            decision["extra_rounds"] = 1.5
        elif missing == "non_numeric_string":
            decision["extra_rounds"] = "five"
        else:  # pragma: no cover - parametrize guards the cases
            raise AssertionError(missing)

    async def hook(_context: dict[str, Any]) -> dict[str, Any]:
        return decision

    # reached_max at the configured budget; outcome=blocked.
    ctx = _gate_context(tmp_path=tmp_path, round_budget=8, hook=hook)

    should_stop = await _human_gate(ctx, "blocked", 8, "blocked state")

    assert should_stop is True
    assert ctx.abort_reason == "max_rounds_exhausted"
    assert ctx.round_budget == 8
    # The decision is recorded even when the grant is zero, so the snapshot
    # can prove no extension was issued.
    receipt = [
        event
        for event in _events(tmp_path / "events.jsonl")
        if event["event"] == "round_extension_decision"
    ]
    assert len(receipt) == 1
    payload = receipt[0]
    assert payload["granted_extra_rounds"] == 0
    assert payload["round_budget"] == 8


# --- AC2 + AC3: explicit valid extension grants once and is recorded -----


@pytest.mark.asyncio
async def test_an_explicit_extra_rounds_grant_is_recorded_and_extends_once(
    tmp_path: Path,
) -> None:
    """An explicit positive integer grants exactly that and is auditable."""
    async def hook(_context: dict[str, Any]) -> dict[str, Any]:
        return {"action": "continue", "extra_rounds": 4, "approval_id": "appr-1"}

    ctx = _gate_context(tmp_path=tmp_path, round_budget=8, hook=hook)

    should_stop = await _human_gate(ctx, "blocked", 8, "blocked state")

    assert should_stop is False
    # 8 + 4 = 12; clamping to MAX_ROUNDS does not apply here.
    assert ctx.round_budget == 12
    decision_events = [
        event
        for event in _events(tmp_path / "events.jsonl")
        if event["event"] == "round_extension_decision"
    ]
    finish_events = [
        event
        for event in _events(tmp_path / "events.jsonl")
        if event["event"] == "human_continue_after_finish"
    ]
    assert len(decision_events) == 1
    assert decision_events[0]["approval_id"] == "appr-1"
    assert decision_events[0]["granted_extra_rounds"] == 4
    # The decision receipt records the ceiling at decision time; the
    # ``human_continue_after_finish`` event captures the post-grant value.
    assert decision_events[0]["round_budget"] == 8
    assert len(finish_events) == 1
    assert finish_events[0]["approval_id"] == "appr-1"
    assert finish_events[0]["round_budget"] == 12
    # The receipt lives on the gate context so the snapshot can publish it.
    assert len(ctx.extension_history) == 1
    assert ctx.extension_history[0]["approval_id"] == "appr-1"
    assert ctx.extension_history[0]["granted_extra_rounds"] == 4


# --- AC4: exhausted budget stops instead of looping -----------------------


@pytest.mark.asyncio
async def test_an_exhausted_budget_stops_at_the_gate(tmp_path: Path) -> None:
    """When the budget is exhausted the gate stops the run, not the next round.

    Before the fix the loop ran another round and returned to the same gate,
    which silently re-granted the full configured budget and produced the
    observed ``round 9`` / ``round 10`` evidence on a ``max_rounds=8`` run.
    """
    async def hook(_context: dict[str, Any]) -> dict[str, Any]:
        return {"action": "continue", "approval_id": "appr-stop"}

    ctx = _gate_context(tmp_path=tmp_path, round_budget=8, hook=hook)

    should_stop = await _human_gate(ctx, "blocked", 8, "blocked state")

    assert should_stop is True
    assert ctx.abort_reason == "max_rounds_exhausted"
    assert ctx.round_budget == 8
    # No silent extension event was emitted because nothing was granted.
    grants = [
        event
        for event in _events(tmp_path / "events.jsonl")
        if event["event"] == "round_extension_decision"
        and event["granted_extra_rounds"] > 0
    ]
    assert grants == []


# --- AC5: replayed approval id never grants twice --------------------------


@pytest.mark.asyncio
async def test_replaying_an_approval_id_grants_nothing_a_second_time(
    tmp_path: Path,
) -> None:
    """The same approval id cannot grant a second extension.

    Replay can happen through a dashboard REST re-send or a worker resume
    replaying the cached decision.  Without protection the same receipt
    would double-count and the budget would grow on every replay.
    """
    decisions = iter(
        (
            # First pass: explicit grant.
            {"action": "continue", "extra_rounds": 4, "approval_id": "appr-dup"},
            # Second pass: replayed id with a different amount.
            {"action": "continue", "extra_rounds": 9, "approval_id": "appr-dup"},
        )
    )

    async def hook(_context: dict[str, Any]) -> dict[str, Any]:
        return next(decisions)

    ctx = _gate_context(tmp_path=tmp_path, round_budget=8, hook=hook)

    first_stop = await _human_gate(ctx, "blocked", 8, "blocked state")
    assert first_stop is False
    assert ctx.round_budget == 12
    # The replay lands on a round that no longer hits reached_max, but the
    # extension_history still records the (now-rejected) request.
    second_stop = await _human_gate(ctx, "blocked", 9, "blocked state")
    assert second_stop is False
    assert ctx.round_budget == 12, "replayed grant must not extend further"

    decision_events = [
        event
        for event in _events(tmp_path / "events.jsonl")
        if event["event"] == "round_extension_decision"
    ]
    assert len(decision_events) == 2
    assert decision_events[1]["approval_id"] == "appr-dup"
    assert decision_events[1]["granted_extra_rounds"] == 0
    assert decision_events[1]["replayed"] is True
    assert ctx.extension_history[1]["replayed"] is True


# --- snapshot surfacing -----------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_publishes_effective_budget_and_extension_history(
    tmp_path: Path,
) -> None:
    """``build_snapshot`` must expose the effective ceiling and history.

    Without this projection the run list summary keeps reporting the
    originally configured ``max_rounds=8`` while the gate has actually
    enforced a higher ceiling, which was the visual symptom in the
    evidence run.
    """
    # Build the gate inside the run_dir so its event_ids get the run_id
    # prefix the EventTailer requires for replay, and so the events land
    # directly in the role_dir the snapshot tails.
    run_id = "20261016T034523Z_1179ac09"
    run_dir = tmp_path / "runs" / run_id
    log_dir = run_dir / "lh_harness"
    control_dir = run_dir / "control"
    role_dir = log_dir / "role_orchestration"
    role_dir.mkdir(parents=True)
    control_dir.mkdir(parents=True)
    (control_dir / "owner.json").write_text(
        json.dumps({"run_id": run_id, "max_rounds": 8}),
        encoding="utf-8",
    )

    async def hook(_context: dict[str, Any]) -> dict[str, Any]:
        return {"action": "continue", "extra_rounds": 3, "approval_id": "appr-snap"}

    ctx = _gate_context(
        tmp_path=log_dir,
        round_budget=8,
        hook=hook,
        role_dir=role_dir,
        events_path=role_dir / "events.jsonl",
    )

    await _human_gate(ctx, "blocked", 8, "blocked state")

    state = DashboardState(
        log_dir=log_dir, control_enabled=False, runs_root=tmp_path / "runs"
    )
    snapshot = build_snapshot(state, run_id=run_id)

    assert snapshot["effective_max_rounds"] == 11
    history = snapshot["extension_history"]
    assert len(history) == 1
    assert history[0]["approval_id"] == "appr-snap"
    assert history[0]["granted_extra_rounds"] == 3
    assert history[0]["round_budget"] == 11
    assert history[0]["replayed"] is False


# --- dashboard hook: no silent fallback ------------------------------------


def test_dashboard_hook_does_not_apply_a_silent_extra_rounds_default(tmp_path: Path) -> None:
    """``make_human_hook`` passes the operator's number through verbatim.

    A bare "Continue" click must reach the manager as ``extra_rounds=0``
    (not the configured budget), so the gate can stop at the exhausted
    ceiling instead of silently looping.
    """
    state = DashboardState(log_dir=tmp_path / "logs", control_enabled=True)
    hook = make_human_hook(state, poll_interval=0.01)

    async def scenario() -> dict[str, Any]:
        task = asyncio.create_task(
            hook(
                {
                    "outcome": "blocked",
                    "reached_max": True,
                    "round_index": 8,
                    "rounds": [],
                }
            )
        )
        for _ in range(500):
            pending = [item for item in state.list_approvals() if item.get("status") == "pending"]
            if pending:
                break
            await asyncio.sleep(0.01)
        else:  # pragma: no cover - the hook always raises a gate here
            task.cancel()
            raise AssertionError("no approval was raised")
        approval_id = pending[0]["approval_id"]
        assert state.resolve_approval(approval_id, action="continue") is True
        return await asyncio.wait_for(task, timeout=5)

    decision = asyncio.run(scenario())

    # The bug fix: a bare "Continue" must NOT default to max_total_episodes.
    assert decision["action"] == "continue"
    assert decision["extra_rounds"] == 0
    # The approval id is also forwarded so the manager can write the receipt.
    assert decision["approval_id"]


# --- validation table: every unusable input stays zero ---------------------


@pytest.mark.parametrize(
    "value",
    [None, 0, -1, -100, 1.5, True, False, "", " ", "abc", [], {}, "0", "-3"],
)
@pytest.mark.asyncio
async def test_explicit_extension_validation_rejects_unusable_inputs(
    tmp_path: Path, value: Any
) -> None:
    """``_extra_rounds`` and the gate agree: every unusable input grants zero.

    This is the regression table for the field the dashboard sends.  It
    pins behaviour for every documented bad shape so a future change
    cannot quietly grant on the floor of an injected new branch.
    """
    async def hook(_context: dict[str, Any]) -> dict[str, Any]:
        return {"action": "continue", "extra_rounds": value, "approval_id": "appr-bad"}

    ctx = _gate_context(tmp_path=tmp_path, round_budget=8, hook=hook)

    should_stop = await _human_gate(ctx, "blocked", 8, "blocked state")

    assert should_stop is True
    assert ctx.round_budget == 8
    receipt = ctx.extension_history[0]
    assert receipt["granted_extra_rounds"] == 0


@pytest.mark.parametrize(
    "value,expected",
    [(None, 0), (0, 0), (-1, 0), (True, 0), ("5", 5), ("x", 0), (1.5, 0), (4, 4)],
)
def test_extra_rounds_validation_table(value: Any, expected: int) -> None:
    """Validation table for the field shape the dashboard actually sends.

    Numeric strings that fit in an integer are valid; non-numeric strings,
    ``bool``, ``float``, and ``None`` are all 0.  This complements the
    higher-level gate test by pinning the field's contract directly.
    """
    from lh_harness.manager import _extra_rounds

    assert _extra_rounds(value) == expected