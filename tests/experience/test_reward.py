"""Terminal reward mapping over every status x abort_reason, plus term behaviour."""

from __future__ import annotations

import pytest

from lh_harness.experience import (
    WEIGHT_GOAL,
    WEIGHT_PROCESS,
    WEIGHT_SATISFACTION,
    RewardBreakdown,
    terminal_reward,
)
from lh_harness.experience.reward import reward_breakdown
from lh_harness.types import HarnessConfig

STATUSES = ("complete", "completed", "incomplete", "blocked", "cancelled", "failed")
ABORT_REASONS = (
    "",
    "user_cancelled",
    "worker_cancelled",
    "worker_exception",
    "manager_blocked",
    "needs_human_input",
    "max_rounds_exhausted",
    "human_abort",
    "provider_timeout",
    "provider_error",
    "provider_authentication",
    "provider_quota",
    "provider_rate_limit",
    "provider_network",
    "provider_model_unavailable",
    "some_unrecognised_reason",
)

_CLEAN_AUDIT_ROUND = {
    "round_index": 1,
    "auditor_report": (
        "Status: incomplete\nIntegrity: clean\nContract audit: aligned\n\nAudit facts.\n"
    ),
    "auditor_status": {},
    "manager_status": {"status": "done"},
    "executor_status": {"status": "done"},
}


def _report(status: str = "complete", abort_reason: str = "", rounds_run: int = 2, max_rounds: int = 25) -> dict:
    return {
        "status": status,
        "abort_reason": abort_reason,
        "completion_satisfied": status in {"complete", "completed"},
        "rounds_run": rounds_run,
        "max_rounds": max_rounds,
        "task": "Build the thing",
    }


def _rounds(n: int = 2) -> list[dict]:
    return [_CLEAN_AUDIT_ROUND | {"round_index": index + 1} for index in range(n)]


def test_weights_match_the_spec() -> None:
    assert (WEIGHT_GOAL, WEIGHT_PROCESS, WEIGHT_SATISFACTION) == (0.45, 0.30, 0.25)
    assert WEIGHT_GOAL + WEIGHT_PROCESS + WEIGHT_SATISFACTION == pytest.approx(1.0)


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("abort_reason", ABORT_REASONS)
def test_every_status_abort_combination_maps_into_range(status: str, abort_reason: str) -> None:
    breakdown = reward_breakdown(_report(status, abort_reason), [], [], _rounds(), HarnessConfig())
    assert breakdown.goal is not None
    assert breakdown.process is not None
    assert breakdown.satisfaction is not None
    for term in (breakdown.goal, breakdown.process, breakdown.satisfaction):
        assert -1.0 <= term <= 1.0
    assert -1.0 <= breakdown.value <= 1.0
    assert breakdown.value == pytest.approx(
        0.45 * breakdown.goal + 0.30 * breakdown.process + 0.25 * breakdown.satisfaction, abs=1e-6
    )
    assert breakdown.value is not None


@pytest.mark.parametrize("status", STATUSES)
def test_web_cancelled_run_has_no_signal(status: str) -> None:
    # user_cancelled attributed to the web control plane is a wallboard stop,
    # not operator feedback: no reward, no terms, an explicit reason.
    report = _report(status, "user_cancelled")
    breakdown = reward_breakdown(
        report, [], [{"kind": "stop", "created_by": "web"}], _rounds(), HarnessConfig()
    )
    assert breakdown.value is None
    assert breakdown.goal is None
    assert breakdown.process is None
    assert breakdown.satisfaction is None
    assert breakdown.reason == "no_signal:user_cancelled_web"
    assert terminal_reward(report, [], [{"kind": "abort", "created_by": "web"}], _rounds(), HarnessConfig()) is None


@pytest.mark.parametrize("status", STATUSES)
def test_non_web_cancelled_run_keeps_a_signal(status: str) -> None:
    # An operator stop from the embedded dashboard or the CLI is deliberate
    # feedback and is valued; so is a cancel with no recorded provenance.
    for messages in ([{"kind": "stop", "created_by": "operator"}], []):
        breakdown = reward_breakdown(
            _report(status, "user_cancelled"), [], messages, _rounds(), HarnessConfig()
        )
        assert breakdown.value is not None
        assert breakdown.reason == ""


@pytest.mark.parametrize(
    ("status", "abort_reason"),
    [
        ("complete", ""),
        ("completed", ""),
        ("incomplete", "max_rounds_exhausted"),
        ("blocked", "manager_blocked"),
        ("failed", "provider_error"),
    ],
)
def test_cancelled_runs_only_signal_when_cancelled(status: str, abort_reason: str) -> None:
    # A web-created cancel command must not suppress other abort reasons.
    breakdown = reward_breakdown(
        _report(status, abort_reason),
        [],
        [{"kind": "stop", "created_by": "web"}],
        _rounds(),
        HarnessConfig(),
    )
    assert breakdown.value is not None


def test_deliberately_failed_run_is_negative() -> None:
    report = _report("failed", "provider_authentication", rounds_run=1)
    value = terminal_reward(report, [], [], _rounds(1), HarnessConfig())
    assert value is not None
    assert value < 0
    breakdown = reward_breakdown(report, [], [], _rounds(1), HarnessConfig())
    assert breakdown.goal == pytest.approx(-1.0, abs=1e-6)


def test_completed_run_beats_incomplete_beats_failed() -> None:
    values = [
        terminal_reward(_report(status, reason), [], [], _rounds(), HarnessConfig())
        for status, reason in (
            ("complete", ""),
            ("incomplete", "max_rounds_exhausted"),
            ("failed", "provider_error"),
        )
    ]
    assert values[0] > values[1] > values[2]


def test_completion_satisfied_confirms_complete_status() -> None:
    assert reward_breakdown(_report("complete"), [], [], _rounds(), HarnessConfig()).goal == 1.0


def test_goal_refines_with_the_final_verdict_triple() -> None:
    clean = reward_breakdown(_report("complete"), [], [], [_CLEAN_AUDIT_ROUND], HarnessConfig())
    violation = reward_breakdown(
        _report("complete"),
        [],
        [],
        [
            {
                "round_index": 1,
                "auditor_report": (
                    "Status: incomplete\nIntegrity: violation\nContract audit: invalid\n\nAudit facts.\n"
                ),
                "auditor_status": {},
            }
        ],
        HarnessConfig(),
    )
    assert clean.goal == pytest.approx(1.0, abs=1e-6)
    assert violation.goal < clean.goal
    # The last non-synthetic round decides; a synthetic repair round is skipped.
    repaired = reward_breakdown(
        _report("complete"),
        [],
        [],
        [
            _CLEAN_AUDIT_ROUND,
            {
                "round_index": 2,
                "auditor_report": "Status: incomplete\nIntegrity: violation\nContract audit: invalid\n",
                "auditor_status": {"invalid_completion": True},
            },
        ],
        HarnessConfig(),
    )
    assert repaired.goal == clean.goal


def test_process_tracks_budget_timeouts_and_format_repairs() -> None:
    config = HarnessConfig(max_total_episodes=4)
    frugal = reward_breakdown(_report("complete", rounds_run=1, max_rounds=25), [], [], _rounds(1), config)
    exhausted = reward_breakdown(_report("complete", rounds_run=25, max_rounds=25), [], [], _rounds(25), config)
    assert exhausted.process < frugal.process

    timeout_rounds = [
        _CLEAN_AUDIT_ROUND | {"round_index": 1, "executor_status": {"status": "timeout"}}
    ]
    timed_out = reward_breakdown(_report("complete"), [], [], timeout_rounds, HarnessConfig())
    assert timed_out.process < frugal.process

    repair_rounds = [
        _CLEAN_AUDIT_ROUND | {"round_index": 1, "auditor_status": {"invalid_plan": True}}
    ]
    repaired = reward_breakdown(_report("complete"), [], [], repair_rounds, HarnessConfig())
    assert repaired.process < frugal.process
    # Both penalties can drive the term below zero.
    assert reward_breakdown(
        _report("complete", rounds_run=25, max_rounds=25), [], [], timeout_rounds * 5, config
    ).process < 0.0


def test_process_uses_the_report_rounds_run() -> None:
    # The spec names rounds_run/max_rounds; the report's count is authoritative
    # even when the passed round list is shorter.
    breakdown = reward_breakdown(
        _report("complete", rounds_run=10, max_rounds=25), [], [], _rounds(2), HarnessConfig()
    )
    assert breakdown.process == pytest.approx(1.0 - 10 / 25, abs=1e-6)


def test_satisfaction_from_approval_resolutions() -> None:
    resolved_continue = [{"approval_id": "a1", "status": "resolved", "action": "continue"}]
    assert reward_breakdown(_report(), resolved_continue, [], _rounds(), HarnessConfig()).satisfaction > 0

    resolved_stop = [{"approval_id": "a1", "status": "resolved", "action": "stop"}]
    assert reward_breakdown(_report(), resolved_stop, [], _rounds(), HarnessConfig()).satisfaction < 0

    pending = [{"approval_id": "a1", "status": "pending"}]
    assert reward_breakdown(_report(), pending, [], _rounds(), HarnessConfig()).satisfaction < 0

    # A resolved approval followed by its pending original keeps only the
    # resolution (approvals.jsonl appends both).
    both = [
        {"approval_id": "a1", "status": "pending"},
        {"approval_id": "a1", "status": "resolved", "action": "continue"},
    ]
    assert reward_breakdown(_report(), both, [], _rounds(), HarnessConfig()).satisfaction > 0

    # Extra rounds granted with a continue are at least as positive.
    granted = [{"approval_id": "a1", "status": "resolved", "action": "continue", "extra_rounds": 3}]
    plain = [{"approval_id": "a1", "status": "resolved", "action": "continue"}]
    assert (
        reward_breakdown(_report(), granted, [], _rounds(), HarnessConfig()).satisfaction
        >= reward_breakdown(_report(), plain, [], _rounds(), HarnessConfig()).satisfaction
    )

    assert reward_breakdown(_report(), [], [], _rounds(), HarnessConfig()).satisfaction == 0.0


def test_satisfaction_uses_rationale_text_and_operator_messages() -> None:
    praised = [{"approval_id": "a1", "status": "resolved", "action": "continue", "reason": "perfect, thanks"}]
    criticized = [{"approval_id": "a1", "status": "resolved", "action": "continue", "reason": "this is wrong"}]
    positive = reward_breakdown(_report(), praised, [], _rounds(), HarnessConfig()).satisfaction
    negative = reward_breakdown(_report(), criticized, [], _rounds(), HarnessConfig()).satisfaction
    assert positive > negative

    with_message = reward_breakdown(
        _report(), [], [{"kind": "inject_instruction", "created_by": "operator", "instructions": "please redo it, that was wrong"}], _rounds(), HarnessConfig()
    )
    assert with_message.satisfaction < 0.0

    # Keyword nudges are capped so prose can never dominate a resolution.
    gushing = [
        {
            "approval_id": "a1",
            "status": "resolved",
            "action": "stop",
            "reason": "good great perfect thanks lgtm nice works correct verified",
        }
    ]
    capped = reward_breakdown(_report(), gushing, [], _rounds(), HarnessConfig()).satisfaction
    plain_stop = reward_breakdown(
        _report(), [{"approval_id": "a1", "status": "resolved", "action": "stop"}], [], _rounds(), HarnessConfig()
    ).satisfaction
    assert capped <= plain_stop + 0.2 + 1e-9


def test_terms_are_recorded_separately() -> None:
    breakdown = reward_breakdown(_report("complete"), [], [], _rounds(), HarnessConfig())
    assert isinstance(breakdown, RewardBreakdown)
    assert breakdown.to_dict() == {
        "goal": breakdown.goal,
        "process": breakdown.process,
        "satisfaction": breakdown.satisfaction,
        "value": breakdown.value,
        "reason": "",
    }


def test_empty_report_stays_in_range() -> None:
    breakdown = reward_breakdown({}, [], [], [], HarnessConfig())
    assert breakdown.value is not None
    assert -1.0 <= breakdown.value <= 1.0
