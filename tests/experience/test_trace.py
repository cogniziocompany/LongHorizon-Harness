"""TraceUnit field rules: caps mirroring HarnessConfig, absent-not-empty device
dimension, successor inheritance, and round-to-trace assembly."""

from __future__ import annotations

import pytest

from lh_harness.experience import (
    ROUTE_RATIONALE_MAX_CHARS,
    RewardTerms,
    RoleTrace,
    TraceCaps,
    TraceUnit,
    task_context_id,
    trace_unit_from_round,
)
from lh_harness.types import HarnessConfig

_ROUND = {
    "round_index": 4,
    "next_step": "cli",
    "plan_text": "plan text",
    "executor_output": "executor output",
    "auditor_report": "Status: incomplete\nIntegrity: clean\nContract audit: aligned\n",
    "harness_feedback": "harness feedback",
    "task_state": "current task state",
    "task_contract": "current task contract",
    "manager_status": {"status": "done"},
    "executor_status": {"status": "done"},
    "auditor_status": {},
}


def test_caps_mirror_harness_config_clipping() -> None:
    config = HarnessConfig(auditor_output_chars=900, role_verified_context_chars=1200, role_history_chars=1500)
    caps = TraceCaps.from_config(config)
    assert caps.state_summary_chars == 900
    assert caps.reflection_chars == 900
    assert caps.observation_chars == 1200
    assert caps.action_chars == 1500

    defaults = TraceCaps.from_config(HarnessConfig())
    # The live clipping budget, not invented values.
    assert defaults.state_summary_chars == HarnessConfig.auditor_output_chars
    assert defaults.observation_chars == HarnessConfig.role_verified_context_chars
    assert defaults.action_chars == HarnessConfig.role_history_chars


def test_long_fields_are_clipped_to_the_caps() -> None:
    caps = TraceCaps(state_summary_chars=200, reflection_chars=150, observation_chars=120, action_chars=100)
    long_round = _ROUND | {
        "task_state": "state " * 60,
        "executor_output": "action " * 40,
        "auditor_report": "reflection " * 30,
        "harness_feedback": "observation " * 25,
    }
    unit = trace_unit_from_round(
        long_round,
        run_id="run-a",
        caps=caps,
        reward_terms=RewardTerms(goal=0.5, process=0.4, satisfaction=0.3),
    )
    assert len(unit.state_summary) <= caps.state_summary_chars
    assert len(unit.reflection) <= caps.reflection_chars
    assert len(unit.observation) <= caps.observation_chars
    assert len(unit.action) <= caps.action_chars
    # The truncation marker names what was cut, like the role prompts do.
    assert "kept head and tail" in unit.action
    # Fields below their caps pass through untouched.
    short = trace_unit_from_round(_ROUND, run_id="run-a", caps=caps)
    assert short.action == "executor output"


def test_device_fields_are_absent_not_empty() -> None:
    unit = trace_unit_from_round(_ROUND, run_id="run-a")
    assert unit.device_id is None
    assert unit.terminal_id is None
    assert unit.hydra_node is None
    payload = unit.to_dict()
    # Absent dimensions are omitted, never emitted as empty strings.
    assert "device_id" not in payload
    assert "terminal_id" not in payload
    assert "hydra_node" not in payload


@pytest.mark.parametrize("field", ("device_id", "terminal_id", "hydra_node"))
def test_empty_device_values_are_rejected(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        TraceUnit(run_id="run-a", task_context_id="run-a", round_index=1, **{field: ""})
    with pytest.raises(ValueError, match=field):
        TraceUnit(run_id="run-a", task_context_id="run-a", round_index=1, **{field: "   "})


@pytest.mark.parametrize("field", ("device_id", "terminal_id", "hydra_node"))
def test_device_ids_are_stored_verbatim(field: str) -> None:
    # Hydra identifiers are never re-keyed or truncated.
    raw = "device-" + "x" * 4000
    unit = TraceUnit(run_id="run-a", task_context_id="run-a", round_index=1, **{field: raw})
    assert getattr(unit, field) == raw
    assert unit.to_dict()[field] == raw


def test_task_context_id_inherits_from_successor_line() -> None:
    assert (
        task_context_id("successor to 20260908-054932-abc — finish the deploy", "run-late")
        == "20260908-054932-abc"
    )
    # Only the first task-text line is scanned.
    assert task_context_id("do something\nsuccessor to run-older", "run-late") == "run-late"
    assert task_context_id("Build the thing", "run-late") == "run-late"
    assert task_context_id("", "run-late") == "run-late"


def test_trace_unit_from_round_maps_the_round_artifacts() -> None:
    unit = trace_unit_from_round(
        _ROUND,
        run_id="run-a",
        task_text="successor to run-prior",
        reward_terms=RewardTerms(goal=1.0, process=0.9, satisfaction=0.2),
        alpha=0.7,
        value=0.776,
        roles={"executor": RoleTrace(role="executor", agent="claude_code", model="claude-opus-5")},
        workspace="/home/harness/work/demo",
        repo="demo",
        branch="feat/x",
    )
    assert unit.task_context_id == "run-prior"
    assert unit.state_summary == "current task state"
    assert unit.action == "executor output"
    assert unit.observation == "harness feedback"
    assert unit.reflection.startswith("Status: incomplete")
    assert unit.next_step == "cli"
    assert unit.evidence_id == "run-a:round_004"
    assert unit.roles["executor"].model == "claude-opus-5"
    assert unit.reward_terms.goal == 1.0


def test_trace_unit_from_round_accepts_dict_records() -> None:
    unit = trace_unit_from_round(_ROUND, run_id="run-a", task_context="run-prior")
    assert unit.task_context_id == "run-prior"
    assert unit.round_index == 4


def test_to_dict_omits_absent_fields() -> None:
    unit = trace_unit_from_round(_ROUND, run_id="run-a")
    payload = unit.to_dict()
    assert payload["run_id"] == "run-a"
    # No terms were recorded: the nested dict is absent, not empty.
    assert "reward_terms" not in payload
    assert "error_signature" not in payload
    assert "occurred_at" not in payload
    assert "branch" not in payload
    # A value of "" for an unrecorded text field is not emitted either.
    assert "repo" not in payload


def test_reward_terms_only_present_when_recorded() -> None:
    unit = trace_unit_from_round(
        _ROUND, run_id="run-a", reward_terms={"goal": 0.5, "process": None, "satisfaction": None}
    )
    assert unit.reward_terms.goal == 0.5
    assert unit.reward_terms.process is None
    assert unit.to_dict()["reward_terms"] == {"goal": 0.5}


def test_tool_names_and_tags_are_capped_and_cleaned() -> None:
    unit = trace_unit_from_round(
        _ROUND,
        run_id="run-a",
        domain_tags=["repo", "", "  ", "linux"],
        tool_names=[f"tool_{index:03d}" for index in range(100)],
    )
    assert unit.domain_tags == ("repo", "linux")
    assert len(unit.tool_names) == TraceCaps().max_tool_names


def test_role_trace_route_rationale_is_capped_and_identities_are_absent_not_empty() -> None:
    role = RoleTrace(role="executor", agent="claude_code", route_rationale="r" * 900)
    assert len(role.route_rationale) == ROUTE_RATIONALE_MAX_CHARS == 512
    assert RoleTrace(role="executor", agent="  ").agent is None
    assert RoleTrace(role="executor").to_dict() == {"role": "executor"}
    with pytest.raises(ValueError):
        RoleTrace(role="")


def test_run_id_is_required() -> None:
    with pytest.raises(ValueError):
        TraceUnit(run_id="", task_context_id="run-a", round_index=1)
    with pytest.raises(ValueError):
        TraceUnit(run_id="run-a", task_context_id="", round_index=1)


def test_plain_dict_terms_and_roles_are_normalised() -> None:
    # Capture wiring may hand raw dicts over; the dataclass normalises them so
    # the serialized trace always has the same shape.
    unit = TraceUnit(
        run_id="run-a",
        task_context_id="run-a",
        round_index=1,
        reward_terms={"goal": 0.5, "process": "0.4", "satisfaction": None},
        roles={"executor": {"agent": "claude_code", "model": "claude-opus-5"}},
    )
    assert isinstance(unit.reward_terms, RewardTerms)
    assert unit.reward_terms.process == 0.4
    assert isinstance(unit.roles["executor"], RoleTrace)
    assert unit.to_dict()["roles"] == {"executor": {"role": "executor", "agent": "claude_code", "model": "claude-opus-5"}}
    assert "route_tier" not in unit.to_dict()["roles"]["executor"]
