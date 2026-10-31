import json

import pytest

from lh_harness.manager_reflection import parse_reflection


def answer(**updates):
    return json.dumps({"summary": "Auditor found one remaining gap", "disposition": "continue",
        "next_slice": "Fix the verified gap", "question": "", "evidence_refs": [], **updates})


def test_bounded_reflection_is_not_an_authority_grant():
    parsed = parse_reflection(answer(), evidence_refs=[], remaining_rounds=1, completion_satisfied=False)
    assert parsed["next_slice"] == "Fix the verified gap"


@pytest.mark.parametrize("text", [answer(max_rounds=99), answer(summary=""),
    answer(disposition="complete", next_slice=""), answer(disposition="wait", next_slice="", question=""),
    answer(evidence_refs=["invented-receipt"]), '{"summary":"x","summary":"y"}',
    "```json\n" + answer() + "\n```"])
def test_unqualified_reflection_is_refused(text):
    assert parse_reflection(text, evidence_refs=[], remaining_rounds=1, completion_satisfied=False) is None


def test_reflection_cannot_continue_past_budget():
    assert parse_reflection(answer(), evidence_refs=[], remaining_rounds=0, completion_satisfied=False) is None
