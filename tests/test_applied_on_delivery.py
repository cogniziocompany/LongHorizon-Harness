"""'applied' is written when an operator instruction is DELIVERED, not drained.

LHH#215 review comment 7195082321, note 2: ``DashboardState.drain_injections``
marked a queued instruction 'applied' the moment the worker CLAIMED it from the
control bus.  A restored round skips its manager episode, so the claimed text
waited in the in-memory carryover for the next live round; a worker lost again
in that window took the carryover with it, and the 'applied' receipt removed
the command from ``ControlBus.pending()`` -- the instruction showed 'applied'
yet had been delivered to an agent prompt zero times.

Modelled on ``tests/test_e2b_mid_round_restore.py`` (same scripted agent,
``_Killed`` SIGKILL stand-in, ledger / checkpoint / event readers, and the
``pending_instructions`` plumbing), but the producer is the REAL one the CLI
wires -- ``DashboardState.drain_injections`` and ``DashboardState.mark_instruction_applied``
over a real ``ControlBus`` written under the test's own temp runs root -- so the
receipts asserted here are the ones the bus actually persists.

Every "worker" is a fresh ``DashboardState`` over the same on-disk bus, which is
the real process boundary: the claim set is in memory and dies with the worker,
the commands and receipts are durable.

Scenario (a), the gap: the instruction is claimed (drained) by the first
RESTORED worker, which is then lost again before any live manager round.  The
second restore must deliver it exactly once in total, and 'applied' must appear
only after that delivery -- never during either drained-but-undelivered window.
Scenario (b): the instruction is not consumed at drain (queued while the second
worker was dying, so only the third worker's claim sees it); still exactly once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from lh_harness.dashboard.state import DashboardState
from lh_harness.environment.local import LocalEnvironment
from lh_harness.manager import run
from lh_harness.supervisor.control_bus import ControlBus

from .test_checkpoint_restore_recovery import (
    _CLEAN_AUDIT,
    _DONE,
    _EXECUTED,
    _Killed,
    _PLAN,
    _ScriptedAgent,
    _config,
    _ledger_rounds,
    _role_dir,
    _run_manager,
)
from .test_e2b_mid_round_restore import (
    _CLI_EXECUTOR_PROMPT,
    _GUI_EXECUTOR_PROMPT,
    _MANAGER_PROMPT,
    _events,
)

# Unique marker: not a substring of the scripted task, plans, audits, headings,
# or any other text the prompts are built from.
_INSTRUCTION = "APPLIED-MARKER-9c2e7b verify the receipt after delivery"

# Round 1 completes; round 2's manager plans, then the worker is lost while
# its executor is producing the reply, so the checkpoint names ``manager_done``.
_LOST_AT_MANAGER_DONE = [_PLAN, _EXECUTED, _CLEAN_AUDIT, _PLAN, _Killed("manager killed")]


# --------------------------------------------------------------------------
# Helpers: the real control bus and the real producer/consumer pair
# --------------------------------------------------------------------------


def _bus(tmp_path: Path) -> ControlBus:
    return ControlBus(tmp_path)


def _queue_instruction(tmp_path: Path, text: str = _INSTRUCTION) -> dict[str, Any] | None:
    """Queue ``text`` the way ``POST /api/runs/<id>/instructions`` does and
    return the command's receipt -- ``None`` until it is delivered."""
    state = DashboardState(str(tmp_path / "logs"), task="finish the refactor", control_enabled=True)
    assert state.add_injection(text) is True
    return state.control_bus.receipt_for(state.control_bus.commands()[-1]["command_id"])


def _instruction_commands(tmp_path: Path) -> List[dict[str, Any]]:
    return [item for item in _bus(tmp_path).commands() if item.get("kind") == "inject_instruction"]


def _instruction_receipts(tmp_path: Path) -> List[dict[str, Any]]:
    ids = {str(item.get("command_id")) for item in _instruction_commands(tmp_path)}
    return [item for item in _bus(tmp_path).receipts() if str(item.get("command_id")) in ids]


def _applied_receipts(tmp_path: Path) -> List[dict[str, Any]]:
    return [item for item in _instruction_receipts(tmp_path) if item.get("status") == "applied"]


def _injections(role: Path) -> list[dict[str, Any]]:
    return [item for item in _events(role) if item["event"] == "human_instructions_injected"]


async def _run_worker(tmp_path: Path, *, replies: list[Any], resume: bool = True):
    """One worker process: a fresh ``DashboardState`` over the durable bus.

    The pair wired by the CLI (``cli.py`` ``run(...)``) is passed verbatim:
    ``pending_instructions=state.drain_injections`` and
    ``instruction_delivered=state.mark_instruction_applied``.
    """
    state = DashboardState(str(tmp_path / "logs"), task="finish the refactor", control_enabled=True)
    agent = _ScriptedAgent(replies)
    report = await run(
        task="finish the refactor",
        env=LocalEnvironment(str(tmp_path / "tmp")),
        config=_config(tmp_path),
        agent=agent,
        resume=resume,
        pending_instructions=state.drain_injections,
        instruction_delivered=state.mark_instruction_applied,
    )
    return report, agent


def _acting_prompts(agent: _ScriptedAgent) -> list[str]:
    """Only the role prompts that can act on the instruction.

    The final-response role legitimately sees the operator's instruction again
    as history, so it is excluded from the exactly-once count.
    """
    return [
        p
        for p in agent.prompts
        if p.startswith((_MANAGER_PROMPT, _CLI_EXECUTOR_PROMPT, _GUI_EXECUTOR_PROMPT))
    ]


def _assert_delivery_artifacts(tmp_path: Path, agent: _ScriptedAgent, *, delivered_round: int) -> None:
    """The instruction reached exactly one acting prompt, in round ``delivered_round``."""
    acting = _acting_prompts(agent)
    assert sum(_INSTRUCTION in prompt for prompt in acting) == 1, (
        "the instruction is injected into exactly one manager or executor prompt"
    )
    assert _INSTRUCTION in acting[-1] or any(
        _INSTRUCTION in prompt and prompt.startswith(_MANAGER_PROMPT) for prompt in acting
    ), "the delivery prompt is a live manager prompt"
    rounds_dir = _role_dir(tmp_path) / "rounds"
    delivered = rounds_dir / f"round_{delivered_round:03d}" / "human_instructions.txt"
    assert delivered.read_text(encoding="utf-8") == _INSTRUCTION
    injections = _injections(_role_dir(tmp_path))
    assert [(item["round"], item["chars"]) for item in injections] == [
        (delivered_round, len(_INSTRUCTION))
    ], "exactly one human_instructions_injected event, for the round that delivered it"


# --------------------------------------------------------------------------
# Scenario (a): consumed at drain, interrupted before delivery, restored,
# interrupted AGAIN, restored -> delivered exactly once in total, and 'applied'
# is written only after that delivery.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_twice_interrupted_instruction_is_applied_only_when_delivered(tmp_path: Path) -> None:
    # ---- Worker 1: round 1 completes; round 2 is lost in flight. ------------
    await _run_manager(tmp_path, resume=False, replies=_LOST_AT_MANAGER_DONE)
    assert (_role_dir(tmp_path) / "rounds" / "round_002" / "checkpoint.json").is_file()

    # The operator typed the instruction while that worker was dying.
    assert _queue_instruction(tmp_path) is None, "a queued command has no receipt yet"
    assert len(_instruction_commands(tmp_path)) == 1

    # ---- Worker 2: the first RESTORE.  It claims (drains) the instruction
    # at round start, but round 2 is restored -- no manager episode, so there
    # is no prompt to deliver to -- and the worker is lost again while the
    # auditor is producing the restored round's reply. ------------------------
    report, _agent = await _run_worker(
        tmp_path, replies=[_EXECUTED, _Killed("auditor killed")]
    )
    assert report["status"] == "failed", "the second worker really died mid-round"
    resumed_events = [
        item for item in _events(_role_dir(tmp_path)) if item["event"] == "managed_round_resumed"
    ]
    assert len(resumed_events) == 1, "round 2 was restored exactly once so far"
    assert "manager_done" in resumed_events[0]["skipped_phases"]

    # Drained-but-undelivered window #1: the instruction was consumed from the
    # queue (the worker's claim set is gone with it) but reached no prompt, so
    # it must NOT be receipted 'applied' -- the bus must still offer it.
    assert _instruction_receipts(tmp_path) == [], (
        "a drained-but-undelivered instruction carries no receipt at all"
    )
    assert [item["payload"]["instructions"] for item in _bus(tmp_path).pending()] == [_INSTRUCTION], (
        "the command is still pending on the bus, so a restore can deliver it"
    )
    assert _injections(_role_dir(tmp_path)) == [], "no injection was recorded yet"
    for round_dir in (_role_dir(tmp_path) / "rounds").iterdir():
        assert not (round_dir / "human_instructions.txt").exists(), (
            "no round wrote the instruction file before a live delivery"
        )

    # ---- Worker 3: the second RESTORE.  Round 2 runs its auditor only; round
    # 3 is the first live manager round and delivers the instruction. --------
    report, agent = await _run_worker(tmp_path, replies=[_CLEAN_AUDIT, _DONE])

    assert report["status"] == "complete"
    assert [item["round_index"] for item in _ledger_rounds(tmp_path)] == [1, 2, 3]
    resumed_events = [
        item for item in _events(_role_dir(tmp_path)) if item["event"] == "managed_round_resumed"
    ]
    assert len(resumed_events) == 2, "round 2 was restored twice"
    assert all("manager_done" in item["skipped_phases"] for item in resumed_events)

    # Delivered exactly once in total, across both restores.
    _assert_delivery_artifacts(tmp_path, agent, delivered_round=3)
    assert not (_role_dir(tmp_path) / "rounds" / "round_002" / "human_instructions.txt").exists(), (
        "the twice-restored round 2 never delivered anything"
    )

    # 'applied' is written only now, after the delivery, with the same shape the
    # drain used to write -- and the bus no longer lists the command as pending.
    applied = _applied_receipts(tmp_path)
    assert len(applied) == 1, "exactly one 'applied' receipt, written at delivery"
    command = _instruction_commands(tmp_path)[0]
    assert applied[0]["command_id"] == command["command_id"]
    assert applied[0]["kind"] == "inject_instruction"
    assert applied[0]["run_id"] == command["run_id"]
    assert applied[0]["result"] == {"instructions": _INSTRUCTION}
    assert applied[0]["message"] == ""
    assert _bus(tmp_path).pending() == [], "a delivered instruction is no longer pending"
    assert _bus(tmp_path).receipt_for(command["command_id"])["status"] == "applied"

    # A further worker (round 4 after a resume) must not deliver it again.
    report, agent_again = await _run_worker(tmp_path, replies=[_DONE])
    assert report["status"] == "complete"
    assert sum(_INSTRUCTION in prompt for prompt in _acting_prompts(agent_again)) == 0, (
        "a delivered-and-receipted instruction is never re-delivered"
    )
    assert len(_applied_receipts(tmp_path)) == 1, "the receipt is idempotent across workers"
    assert [(item["round"], item["chars"]) for item in _injections(_role_dir(tmp_path))] == [
        (3, len(_INSTRUCTION))
    ]


# --------------------------------------------------------------------------
# Scenario (b): the instruction is NOT consumed at drain (queued while the
# second worker was dying, so only the third worker's claim sees it).  Still
# delivered exactly once, and 'applied' only after that delivery.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_instruction_queued_across_a_second_loss_is_delivered_exactly_once(
    tmp_path: Path,
) -> None:
    # ---- Worker 1: round 1 completes; round 2 is lost in flight. ------------
    await _run_manager(tmp_path, resume=False, replies=_LOST_AT_MANAGER_DONE)

    # ---- Worker 2: the first restore, lost again BEFORE anything was queued
    # for it: its round-start claim finds an empty queue. --------------------
    report, _agent = await _run_worker(
        tmp_path, replies=[_EXECUTED, _Killed("auditor killed")]
    )
    assert report["status"] == "failed"
    assert _instruction_commands(tmp_path) == [], "nothing was queued for the second worker"

    # The operator typed the instruction while THAT worker was dying.
    assert _queue_instruction(tmp_path) is None
    assert _instruction_receipts(tmp_path) == []

    # ---- Worker 3: the second restore; round 3's live manager delivers it. --
    report, agent = await _run_worker(tmp_path, replies=[_CLEAN_AUDIT, _DONE])

    assert report["status"] == "complete"
    assert [item["round_index"] for item in _ledger_rounds(tmp_path)] == [1, 2, 3]
    resumed_events = [
        item for item in _events(_role_dir(tmp_path)) if item["event"] == "managed_round_resumed"
    ]
    assert len(resumed_events) == 2
    _assert_delivery_artifacts(tmp_path, agent, delivered_round=3)
    assert not (_role_dir(tmp_path) / "rounds" / "round_002" / "human_instructions.txt").exists()

    applied = _applied_receipts(tmp_path)
    assert len(applied) == 1, "exactly one 'applied' receipt, written at delivery"
    assert applied[0]["result"] == {"instructions": _INSTRUCTION}
    assert _bus(tmp_path).pending() == []

    report, agent_again = await _run_worker(tmp_path, replies=[_DONE])
    assert report["status"] == "complete"
    assert sum(_INSTRUCTION in prompt for prompt in _acting_prompts(agent_again)) == 0, (
        "a delivered-and-receipted instruction is never re-delivered"
    )
    assert len(_applied_receipts(tmp_path)) == 1


# --------------------------------------------------------------------------
# The end-of-round gate is the OTHER claim site: in production the operator
# types while a round is running, so the real ``make_human_hook`` drains the
# instruction at the END of that round and its decision carries it into the
# next live manager prompt.  The 'applied' receipt must follow THAT delivery
# too -- a receipt keyed only to the round-start claim would leave the common
# path 'queued' for ever.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_instruction_claimed_by_the_end_of_round_gate_is_applied_on_delivery(
    tmp_path: Path,
) -> None:
    """The real ``make_human_hook`` claims the instruction at the END of a
    round; the 'applied' receipt must still follow the next live delivery.

    The operator types WHILE round 1 is executing -- after round 1's round-start
    claim has already run and before its end-of-round gate -- so the gate is
    the first caller of ``drain_injections`` that sees the command.  That is
    the common production timing, and it enters the carryover through
    ``_human_gate``'s join rather than the round-start claim.
    """
    import asyncio

    from lh_harness.dashboard.gate import make_human_hook

    state = DashboardState(str(tmp_path / "logs"), task="finish the refactor", control_enabled=True)
    assert _instruction_commands(tmp_path) == [], "nothing is queued before the run starts"

    # The operator types while round 1's executor is producing its reply.
    def _operator_types(_call_number: int) -> None:
        assert state.add_injection(_INSTRUCTION) is True

    # Round 2 claims completion, which raises a blocking approval; the
    # operator resolves it "stop" the way the dashboard would, so the run ends.
    async def _operator_decides() -> None:
        while True:
            pending = [item for item in state.list_approvals() if item["status"] == "pending"]
            if pending:
                assert state.resolve_approval(pending[0]["approval_id"], action="stop")
                return
            await asyncio.sleep(0.01)

    agent = _ScriptedAgent([_PLAN, _EXECUTED, _CLEAN_AUDIT, _DONE], on_execute=_operator_types)
    operator = asyncio.ensure_future(_operator_decides())
    try:
        report = await asyncio.wait_for(
            run(
                task="finish the refactor",
                env=LocalEnvironment(str(tmp_path / "tmp")),
                config=_config(tmp_path),
                agent=agent,
                human_hook=make_human_hook(state, poll_interval=0.01),
                pending_instructions=state.drain_injections,
                instruction_delivered=state.mark_instruction_applied,
            ),
            timeout=30,
        )
    finally:
        operator.cancel()

    assert report["status"] == "complete"
    assert [item["round_index"] for item in _ledger_rounds(tmp_path)] == [1, 2]
    # Round 1 had already started when the instruction was queued, so round 1
    # delivered nothing; the gate carried it and round 2's live manager did.
    assert not (_role_dir(tmp_path) / "rounds" / "round_001" / "human_instructions.txt").exists()
    _assert_delivery_artifacts(tmp_path, agent, delivered_round=2)

    # The claim happened in round 1's gate; the receipt happened only at the
    # round 2 delivery, with the same shape the drain used to write.
    applied = _applied_receipts(tmp_path)
    assert len(applied) == 1, "exactly one 'applied' receipt, written at delivery"
    assert applied[0]["result"] == {"instructions": _INSTRUCTION}
    assert applied[0]["message"] == ""
    assert _bus(tmp_path).pending() == []


# --------------------------------------------------------------------------
# An operator instruction may span several lines (the API bounds length, not
# newlines).  The carryover join keeps it verbatim, so it must still be
# receipted on delivery -- asserted over the real bus, without a run.
# --------------------------------------------------------------------------


def test_a_multiline_instruction_is_applied_only_after_its_delivery(tmp_path: Path) -> None:
    state = DashboardState(str(tmp_path / "logs"), task="finish the refactor", control_enabled=True)
    multiline = "APPLIED-MARKER-5d8a1c first line\nsecond line of the same instruction"
    assert state.add_injection(multiline) is True
    command = state.control_bus.commands()[-1]

    # Claim: handed out once, no receipt; a second claim hands out nothing.
    assert state.drain_injections() == [multiline]
    assert state.drain_injections() == []
    assert state.control_bus.receipt_for(command["command_id"]) is None, "a claim is not 'applied'"
    assert [item["command_id"] for item in state.control_bus.pending()] == [command["command_id"]]

    # A fresh worker (new process, same bus) sees the command still pending.
    fresh = DashboardState(str(tmp_path / "logs"), task="finish the refactor", control_enabled=True)
    assert fresh.drain_injections() == [multiline]

    # Delivery of the joined carryover (gate note + the multiline instruction)
    # receipts exactly that command, with the shape the drain used to write.
    state.mark_instruction_applied(f"GATE-NOTE-MARKER-x\n{multiline}")
    receipt = state.control_bus.receipt_for(command["command_id"])
    assert receipt is not None and receipt["status"] == "applied"
    assert receipt["result"] == {"instructions": multiline}
    assert receipt["kind"] == "inject_instruction"
    assert receipt["run_id"] == command["run_id"]
    assert receipt["message"] == ""
    assert state.control_bus.pending() == []
    assert len(state.control_bus.receipts()) == 1, "the gate note matched no command"
