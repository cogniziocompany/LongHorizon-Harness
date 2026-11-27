"""A run ends at the FIRST clean, complete audit that verifies its deliverable.

Measured on the QT-20261021 runs (Paxton, 2026-10-23): runs finished the
deliverable in round 1 and the harness kept auditing anyway — 3-4 rounds, many
ending ``max_rounds_exhausted``.  The manager loop now stops as soon as the
auditor's own control header says the work is complete, clean and contract
aligned, and ends with the normal success outcome reporting that audit.

The stop is narrow: a report that is not clean AND complete must behave exactly
as before — the finding is fed back and another round runs.

These tests drive the real ``lh_harness.manager.run`` loop with the same
sequenced-agent fixture the neighbouring manager tests use, so they exercise the
production round loop rather than a re-implementation of it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lh_harness.environment.local import LocalEnvironment
from lh_harness.manager import run
from lh_harness.types import EpisodeResult, HarnessConfig

_PLAN = "Next: cli\n\nCurrent Task State:\nstill working\n\nPlan:\nkeep going"
_EXECUTED = "executor made the change"

# The verdict the stop is gated on: all three control lines must agree.
_CLEAN_AUDIT = (
    "Status: complete\n"
    "Integrity: clean\n"
    "Contract audit: aligned\n\n"
    "Summary:\nthe change is verified"
)

# Incomplete: the deliverable exists but the auditor did not confirm it. Under
# the old behaviour this is fed back to the manager and another round runs.
_INCOMPLETE_AUDIT = (
    "Status: incomplete\n"
    "Integrity: clean\n"
    "Contract audit: needs_revision\n\n"
    "Gap:\nthe change is not yet verified end to end\n"
    "Next step:\nfinish the verification and re-audit"
)

# Complete but NOT clean: the audit must not be allowed to end the run.
_SUSPECT_AUDIT = (
    "Status: complete\n"
    "Integrity: suspect\n"
    "Contract audit: aligned\n\n"
    "Summary:\nthe change was made but provenance is unconfirmed"
)


class SequencedAgent:
    """Replays a fixed reply sequence and records the prompts it received."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    async def run_episode(self, prompt, _env, _budget, live_trajectory_path=None):
        self.prompts.append(str(prompt))
        reply = self._replies.pop(0) if self._replies else ""
        return EpisodeResult(status="done", actions_log=reply)


def _role_dir(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "role_orchestration"


def _events(tmp_path: Path) -> list[dict[str, Any]]:
    path = _role_dir(tmp_path) / "events.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _events_named(tmp_path: Path, name: str) -> list[dict[str, Any]]:
    return [event for event in _events(tmp_path) if event["event"] == name]


async def _run(tmp_path: Path, *, replies: list[str], max_rounds: int = 4):
    agent = SequencedAgent(replies)
    report = await run(
        task="finish the refactor",
        env=LocalEnvironment(str(tmp_path / "tmp")),
        config=HarnessConfig(
            max_total_episodes=max_rounds,
            workspace_path=str(tmp_path / "workspace"),
            harness_dir=str(tmp_path / "harness"),
            log_dir=str(tmp_path / "logs"),
        ),
        agent=agent,
    )
    return report, agent


@pytest.mark.asyncio
async def test_a_clean_audit_stops_the_run_at_that_round(tmp_path: Path) -> None:
    # Round 1 verifies the deliverable. The budget allows four rounds, and the
    # old loop would have spent rounds 2-4 asking the manager what to do next.
    report, _agent = await _run(
        tmp_path, replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT], max_rounds=4
    )

    assert report["completion_satisfied"] is True
    assert report["status"] == "complete"
    assert report["abort_reason"] == "", "a verified deliverable is not an abort"
    assert report["rounds_run"] == 1
    assert [item["round_index"] for item in report["rounds"]] == [1]

    # The run reports the audit that verified the deliverable.
    assert report["latest_auditor_report"].startswith("Status: complete")
    assert "the change is verified" in report["latest_auditor_report"]
    assert report["rounds"][0]["auditor_report"].strip() == _CLEAN_AUDIT


@pytest.mark.asyncio
async def test_a_clean_audit_starts_no_further_manager_or_audit_round(
    tmp_path: Path,
) -> None:
    _report, agent = await _run(
        tmp_path, replies=[_PLAN, _EXECUTED, _CLEAN_AUDIT], max_rounds=4
    )

    # Exactly one manager turn, one executor and one auditor; the only later
    # episode is the user-facing reply, not another management round.
    assert len(_events_named(tmp_path, "manager_round_start")) == 1
    assert len(_events_named(tmp_path, "executor_role_start")) == 1
    assert len(_events_named(tmp_path, "auditor_role_start")) == 1
    manager_prompts = [
        prompt for prompt in agent.prompts if "You are the LongHorizon-Harness manager agent" in prompt
    ]
    assert len(manager_prompts) == 1

    rounds_dir = _role_dir(tmp_path) / "rounds"
    assert (rounds_dir / "round_001").is_dir()
    assert not (rounds_dir / "round_002").exists(), "no second round may start"

    accepted = _events_named(tmp_path, "completion_accepted_at_clean_audit")
    assert [event["round"] for event in accepted] == [1]
    assert accepted[0]["audit_status"] == "complete"
    assert accepted[0]["integrity_status"] == "clean"
    assert accepted[0]["contract_audit_status"] == "aligned"

    # It ended by completion, not by burning the round budget.
    assert not any(
        event.get("abort_reason") == "max_rounds_exhausted"
        for event in _events_named(tmp_path, "role_harness_done")
    )


@pytest.mark.asyncio
async def test_a_not_clean_audit_still_runs_another_round(tmp_path: Path) -> None:
    # Today's behaviour, unchanged: the incomplete audit is fed back and the
    # next manager round runs. Round 2 then verifies, and the run stops there.
    report, agent = await _run(
        tmp_path,
        replies=[_PLAN, _EXECUTED, _INCOMPLETE_AUDIT, _PLAN, _EXECUTED, _CLEAN_AUDIT],
    )

    assert report["rounds_run"] == 2
    assert [item["round_index"] for item in report["rounds"]] == [1, 2]
    assert report["completion_satisfied"] is True
    assert report["status"] == "complete"

    # Round 1 is recorded exactly as it was before the change: the ordinary
    # progress record, the auditor's own text preserved, no synthetic feedback
    # and no completion marker. Only the clean round 2 ends the run.
    first = report["rounds"][0]
    assert first["next_step"] == "cli"
    assert first["auditor_report"].strip() == _INCOMPLETE_AUDIT
    assert first["harness_feedback"] == ""
    assert not first["auditor_status"].get("invalid_completion")
    assert first["auditor_status"].get("status") == "done"

    # The non-clean finding reached the second manager turn — the repair signal
    # path is untouched.
    manager_prompts = [
        prompt for prompt in agent.prompts if "You are the LongHorizon-Harness manager agent" in prompt
    ]
    assert len(manager_prompts) == 2
    assert "the change is not yet verified end to end" in manager_prompts[1]

    # Round 1 did not end the run; only the clean round 2 did.
    assert len(_events_named(tmp_path, "manager_round_start")) == 2
    assert (_role_dir(tmp_path) / "rounds" / "round_002").is_dir()
    accepted = _events_named(tmp_path, "completion_accepted_at_clean_audit")
    assert [event["round"] for event in accepted] == [2]


@pytest.mark.asyncio
async def test_an_all_unclean_run_spends_its_budget_exactly_as_before(
    tmp_path: Path,
) -> None:
    # Isolates the unchanged path: when no audit is ever clean, the run must run
    # every round it is allowed and end as it always did — ``max_rounds_exhausted``.
    # This test is expected to pass identically against the pre-change manager,
    # which is what makes it a regression guard rather than a restatement of the
    # new stop; the clean-audit tests above are the ones that require the change.
    report, agent = await _run(
        tmp_path,
        replies=[
            _PLAN,
            _EXECUTED,
            _INCOMPLETE_AUDIT,
            _PLAN,
            _EXECUTED,
            _INCOMPLETE_AUDIT,
        ],
        max_rounds=2,
    )

    assert report["rounds_run"] == 2
    assert [item["round_index"] for item in report["rounds"]] == [1, 2]
    assert report["completion_satisfied"] is False
    assert report["status"] != "complete"
    assert report["abort_reason"] == "max_rounds_exhausted"

    # No round was treated as a verification: the stop never fired.
    assert _events_named(tmp_path, "completion_accepted_at_clean_audit") == []

    # Each round is the ordinary progress record with the auditor's own text and
    # no synthetic repair feedback — the non-clean path is untouched.
    for item in report["rounds"]:
        assert item["next_step"] == "cli"
        assert item["auditor_report"].strip() == _INCOMPLETE_AUDIT
        assert item["harness_feedback"] == ""
        assert not item["auditor_status"].get("invalid_completion")

    # The finding reached the next manager turn instead of ending the run.
    manager_prompts = [
        prompt for prompt in agent.prompts if "You are the LongHorizon-Harness manager agent" in prompt
    ]
    assert len(manager_prompts) == 2
    assert "the change is not yet verified end to end" in manager_prompts[1]


@pytest.mark.asyncio
async def test_a_complete_but_suspect_audit_does_not_stop_the_run(tmp_path: Path) -> None:
    # "Clean AND complete": a complete verdict with suspect integrity is not
    # enough to end the run, so round 2 still happens.
    report, _agent = await _run(
        tmp_path,
        replies=[_PLAN, _EXECUTED, _SUSPECT_AUDIT, _PLAN, _EXECUTED, _CLEAN_AUDIT],
    )

    assert report["rounds_run"] == 2
    assert [item["round_index"] for item in report["rounds"]] == [1, 2]
    accepted = _events_named(tmp_path, "completion_accepted_at_clean_audit")
    assert [event["round"] for event in accepted] == [2]
