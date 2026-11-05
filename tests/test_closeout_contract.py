"""easybutt0n-17: the harness close-out contract.

A run's process lifecycle (``status``) says how the PROCESS ended; it does
not say whether the WORK was delivered.  A normal close-out is a "stop" at
the end-of-round gate, which the harness records as ``cancelled`` — so a
run that delivered its work is indistinguishable from a run that was
killed.  RSI (recursive self-improvement) loops that optimise on that
signal optimise on noise.

This module pins the close-out CONTRACT:

* one canonical six-value outcome enum, separate from ``status``;
* one structured close-out record (the "form") that BOTH closers (the
  agent's "done" path and the human gate/stop path) must supply;
* ONE validator shared by both closers, so both produce identical record
  shapes;
* a ``delivered`` outcome that MUST be backed by a verifiable artifact;
* the independent auditor verdict as the authority — a ``delivered``
  close-out that conflicts with it is rejected;
* one immutable close-out per run, amendable only by an explicit audited
  amendment carrying actor + reason;
* a free-text ``done:`` rationale parsed ONLY as a legacy migration hint,
  always marked ``derived=true``.

``status`` is NOT touched by any of this.  The existing process-lifecycle
vocabulary stays byte-identical; this adds a separate, authoritative
close-out outcome beside it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lh_harness.closeout import (
    CLOSE_OUT_OUTCOMES,
    CloseOutAmendment,
    CloseOutError,
    CloseOutRecord,
    CloseOutValidationError,
    ArtifactRef,
    apply_close_out_to_report,
    load_close_out,
    parse_agent_close_out_proposal,
    parse_legacy_rationale,
    persist_close_out,
    read_close_out,
    validate_close_out,
)

# A verifiable artifact: a PR URL with a number and the HEAD commit it
# delivered.  The head sha is what makes a PR artifact VERIFIABLE: a PR
# number alone can be re-pointed at a different commit later, so "we opened
# PR 96" is not evidence of "we delivered commit 773be7e".
_PR_HEAD_SHA = "773be7e"
_PR_ARTIFACT = ArtifactRef(
    kind="pr",
    repo="cogniziocompany/cognizioware-hydra",
    ref="96",
    url="https://github.com/cogniziocompany/cognizioware-hydra/pull/96",
    head_sha=_PR_HEAD_SHA,
)


# --------------------------------------------------------------------------
# 1. The vocabulary: exactly six values, no synonyms, no extras.
# --------------------------------------------------------------------------


def test_outcome_enum_has_exactly_the_six_required_values() -> None:
    assert set(CLOSE_OUT_OUTCOMES) == {
        "delivered",
        "delivered_partial",
        "blocked",
        "failed",
        "aborted",
        "incomplete",
    }
    assert len(CLOSE_OUT_OUTCOMES) == 6


def test_outcome_enum_has_no_synonyms_or_extra_values() -> None:
    # A value from the pre-contract vocabulary must not have crept in.
    for forbidden in (
        "done",
        "done_at_gate",
        "needs_followup",
        "cancelled",
        "completed",
        "success",
        "ok",
        "delivered_no_artifact",
    ):
        assert forbidden not in CLOSE_OUT_OUTCOMES


# --------------------------------------------------------------------------
# 2. Validator accept matrix.
# --------------------------------------------------------------------------


def _valid_delivered_payload() -> dict:
    return {
        "outcome": "delivered",
        "outcome_reason": "PR opened and verified",
        "artifact_refs": [
            {
                "kind": "pr",
                "repo": "cogniziocompany/cognizioware-hydra",
                "ref": "96",
                "url": "https://github.com/cogniziocompany/cognizioware-hydra/pull/96",
                "head_sha": _PR_HEAD_SHA,
            }
        ],
        "closed_by": "agent",
        "closed_by_identity": "manager",
        "closed_at": 1762310000.0,
    }


@pytest.mark.parametrize("outcome", sorted(CLOSE_OUT_OUTCOMES))
def test_validator_accepts_every_outcome_value(outcome: str) -> None:
    payload = _valid_delivered_payload()
    payload["outcome"] = outcome
    if outcome in {"blocked", "failed", "aborted", "incomplete"}:
        # These need no artifact.
        payload["artifact_refs"] = []
    record = validate_close_out(payload, closed_by="agent")
    assert record.outcome == outcome


def test_validator_accepts_a_complete_delivered_record() -> None:
    record = validate_close_out(_valid_delivered_payload(), closed_by="agent")
    assert record.outcome == "delivered"
    assert record.delivered is True
    assert record.outcome_reason == "PR opened and verified"
    assert record.artifact_refs[0].kind == "pr"
    assert record.artifact_refs[0].ref == "96"
    assert record.closed_by == "agent"
    assert record.closed_by_identity == "manager"
    assert record.closed_at == 1762310000.0


def test_delivered_boolean_is_true_only_for_delivered_outcomes() -> None:
    for outcome in ("delivered", "delivered_partial"):
        payload = _valid_delivered_payload()
        payload["outcome"] = outcome
        assert validate_close_out(payload, closed_by="agent").delivered is True
    for outcome in ("blocked", "failed", "aborted", "incomplete"):
        payload = _valid_delivered_payload()
        payload["outcome"] = outcome
        payload["artifact_refs"] = []
        assert validate_close_out(payload, closed_by="agent").delivered is False


# --------------------------------------------------------------------------
# 3. Validator reject matrix.  Every one of these MUST be refused, and the
#    refusal MUST NOT produce a completed/valid record.
# --------------------------------------------------------------------------


def test_validator_rejects_a_missing_close_out() -> None:
    with pytest.raises(CloseOutValidationError):
        validate_close_out(None, closed_by="agent")


def test_validator_rejects_a_missing_outcome() -> None:
    payload = _valid_delivered_payload()
    del payload["outcome"]
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="agent")


def test_validator_rejects_an_unknown_outcome() -> None:
    payload = _valid_delivered_payload()
    payload["outcome"] = "done"
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="agent")


def test_validator_rejects_a_synonymous_outcome() -> None:
    payload = _valid_delivered_payload()
    payload["outcome"] = "done_at_gate"
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="agent")


def test_validator_rejects_a_missing_reason() -> None:
    payload = _valid_delivered_payload()
    del payload["outcome_reason"]
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="agent")


def test_validator_rejects_an_empty_reason() -> None:
    payload = _valid_delivered_payload()
    payload["outcome_reason"] = "   "
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="agent")


def test_validator_rejects_an_overlong_reason() -> None:
    payload = _valid_delivered_payload()
    payload["outcome_reason"] = "x" * 201
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="agent")


def test_validator_accepts_a_reason_of_exactly_the_limit() -> None:
    payload = _valid_delivered_payload()
    payload["outcome_reason"] = "y" * 200
    assert validate_close_out(payload, closed_by="agent").outcome_reason


def test_validator_rejects_a_non_ascii_reason() -> None:
    payload = _valid_delivered_payload()
    payload["outcome_reason"] = "delivered ✓"
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="agent")


def test_validator_rejects_a_missing_closed_by() -> None:
    payload = _valid_delivered_payload()
    del payload["closed_by"]
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="agent")


def test_validator_rejects_an_unknown_closed_by() -> None:
    payload = _valid_delivered_payload()
    payload["closed_by"] = "robot"
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="agent")


def test_validator_rejects_a_missing_closed_by_identity() -> None:
    payload = _valid_delivered_payload()
    del payload["closed_by_identity"]
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="agent")


def test_validator_rejects_a_missing_closed_at() -> None:
    payload = _valid_delivered_payload()
    del payload["closed_at"]
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="agent")


def test_validator_rejects_a_malformed_artifact_ref() -> None:
    payload = _valid_delivered_payload()
    payload["artifact_refs"] = [{"kind": "wiki", "repo": "x", "ref": "1", "url": "u"}]
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="agent")


def test_validator_rejects_an_artifact_ref_missing_its_url() -> None:
    payload = _valid_delivered_payload()
    payload["artifact_refs"] = [{"kind": "pr", "repo": "x", "ref": "1"}]
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="agent")


def test_validator_rejects_artifact_refs_that_are_not_a_list() -> None:
    payload = _valid_delivered_payload()
    payload["artifact_refs"] = {"kind": "pr"}
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="agent")


# --------------------------------------------------------------------------
# 4. The delivered-needs-artifact rule.  There is no "delivered_no_artifact"
#    variant: an artifactless delivery is REFUSED.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["delivered", "delivered_partial"])
def test_delivered_without_an_artifact_is_refused(outcome: str) -> None:
    payload = _valid_delivered_payload()
    payload["outcome"] = outcome
    payload["artifact_refs"] = []
    with pytest.raises(CloseOutValidationError) as excinfo:
        validate_close_out(payload, closed_by="agent")
    assert "artifact" in str(excinfo.value).lower()


def test_delivered_without_an_artifact_leaves_the_run_incomplete() -> None:
    """An invalid close-out must never be reported as completed."""

    payload = _valid_delivered_payload()
    payload["artifact_refs"] = []
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="agent")


def test_blocked_is_accepted_without_an_artifact() -> None:
    """The refusal has an alternative: blocked/incomplete with a reason."""

    payload = _valid_delivered_payload()
    payload["outcome"] = "blocked"
    payload["outcome_reason"] = "could not open the PR"
    payload["artifact_refs"] = []
    record = validate_close_out(payload, closed_by="agent")
    assert record.outcome == "blocked"
    assert record.delivered is False


# --------------------------------------------------------------------------
# 5. Both closers, ONE validator, identical record shapes.
# --------------------------------------------------------------------------


def test_agent_and_human_paths_produce_identical_record_shapes() -> None:
    payload = _valid_delivered_payload()
    agent_record = validate_close_out(payload, closed_by="agent")
    human_payload = dict(payload)
    human_payload["closed_by"] = "human"
    human_payload["closed_by_identity"] = "hydrafleet-gateway"
    human_record = validate_close_out(human_payload, closed_by="human")
    assert agent_record.to_dict().keys() == human_record.to_dict().keys()
    assert agent_record.schema_version == human_record.schema_version


def test_human_close_path_uses_the_same_validator() -> None:
    """The human path is refused by the very same rules as the agent path."""

    payload = _valid_delivered_payload()
    payload["closed_by"] = "human"
    payload["artifact_refs"] = []
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="human")


def test_closed_by_must_match_the_caller() -> None:
    payload = _valid_delivered_payload()
    payload["closed_by"] = "human"
    with pytest.raises(CloseOutValidationError):
        validate_close_out(payload, closed_by="agent")


# --------------------------------------------------------------------------
# 6. Auditor authority: the manager must not self-certify completion.
# --------------------------------------------------------------------------


def test_delivered_conflicting_with_the_auditor_verdict_is_rejected() -> None:
    payload = _valid_delivered_payload()
    with pytest.raises(CloseOutValidationError) as excinfo:
        validate_close_out(
            payload,
            closed_by="agent",
            auditor_verdict={"status": "incomplete", "integrity_status": "clean"},
        )
    assert "auditor" in str(excinfo.value).lower()


def test_auditor_conflict_leaves_the_run_incomplete_with_a_conflict_reason() -> None:
    payload = _valid_delivered_payload()
    with pytest.raises(CloseOutValidationError) as excinfo:
        validate_close_out(
            payload,
            closed_by="agent",
            auditor_verdict={"status": "blocked", "integrity_status": "clean"},
        )
    message = str(excinfo.value).lower()
    assert "conflict" in message


def test_delivered_agreeing_with_a_clean_auditor_verdict_is_accepted() -> None:
    payload = _valid_delivered_payload()
    record = validate_close_out(
        payload,
        closed_by="agent",
        auditor_verdict={"status": "complete", "integrity_status": "clean"},
    )
    assert record.outcome == "delivered"


def test_integrity_violation_blocks_delivery_even_with_a_complete_verdict() -> None:
    payload = _valid_delivered_payload()
    with pytest.raises(CloseOutValidationError):
        validate_close_out(
            payload,
            closed_by="agent",
            auditor_verdict={"status": "complete", "integrity_status": "violation"},
        )


def test_a_blocked_close_out_is_not_refused_by_the_auditor_verdict() -> None:
    """The auditor vetoes DELIVERY; it does not veto an honest blocked."""

    payload = _valid_delivered_payload()
    payload["outcome"] = "blocked"
    payload["artifact_refs"] = []
    record = validate_close_out(
        payload,
        closed_by="agent",
        auditor_verdict={"status": "incomplete", "integrity_status": "clean"},
    )
    assert record.outcome == "blocked"


# --------------------------------------------------------------------------
# 7. The legacy "done:" rationale is a MIGRATION HINT only.
# --------------------------------------------------------------------------


def test_legacy_done_rationale_is_parsed_as_a_derived_hint() -> None:
    parsed = parse_legacy_rationale("done: cogniziocompany/cognizioware-hydra#96")
    assert parsed is not None
    assert parsed.derived is True
    assert parsed.derived_from == "rationale_parse"
    assert parsed.outcome == "delivered"
    assert parsed.delivered is True


def test_legacy_done_rationale_is_never_delivered_without_an_artifact() -> None:
    """A transcribed artifact is marked derived, never presented as verified."""

    parsed = parse_legacy_rationale("done: cogniziocompany/cognizioware-hydra#96")
    assert parsed is not None
    assert parsed.derived is True
    assert parsed.derived_from == "rationale_parse"
    for ref in parsed.artifact_refs:
        assert ref.derived is True


def test_a_non_done_rationale_is_not_a_legacy_hint() -> None:
    assert parse_legacy_rationale("max_rounds_exhausted") is None
    assert parse_legacy_rationale("") is None
    assert parse_legacy_rationale(None) is None


def test_a_legacy_hint_is_not_a_valid_standalone_close_out() -> None:
    """A bare "done:" must not close a run on its own."""

    parsed = parse_legacy_rationale("done: cogniziocompany/cognizioware-hydra#96")
    assert parsed is not None
    with pytest.raises(CloseOutValidationError):
        validate_close_out(parsed.to_dict(), closed_by="agent")


def test_a_legacy_hint_cannot_be_closed_by_the_agent_path() -> None:
    """The agent path never sets delivered from a free-text rationale."""

    parsed = parse_legacy_rationale("done: cogniziocompany/cognizioware-hydra#96")
    assert parsed is not None
    with pytest.raises(CloseOutValidationError):
        validate_close_out(parsed.to_dict(), closed_by="agent")


# --------------------------------------------------------------------------
# 8. Persistence: the record is written next to report.json and mirrored
#    into the run status fields.
# --------------------------------------------------------------------------


def test_close_out_is_persisted_next_to_report_json(tmp_path: Path) -> None:
    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    (logs_dir / "report.json").write_text(json.dumps({"status": "cancelled"}), encoding="utf-8")
    record = validate_close_out(_valid_delivered_payload(), closed_by="agent")
    path = persist_close_out(logs_dir, record)
    assert path.name == "closeout.json"
    assert path.parent == logs_dir
    assert path.is_file()


def test_persisted_close_out_round_trips(tmp_path: Path) -> None:
    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    record = validate_close_out(_valid_delivered_payload(), closed_by="agent")
    persist_close_out(logs_dir, record)
    loaded = load_close_out(logs_dir)
    assert loaded is not None
    assert loaded.to_dict() == record.to_dict()


def test_close_out_is_mirrored_into_the_run_status_fields(tmp_path: Path) -> None:
    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    record = validate_close_out(_valid_delivered_payload(), closed_by="agent")
    report = {"status": "cancelled", "completion_satisfied": False}
    apply_close_out_to_report(report, record)
    assert report["outcome"] == "delivered"
    assert report["delivered"] is True
    assert report["outcome_reason"] == "PR opened and verified"
    assert report["closed_by"] == "agent"
    assert report["closed_by_identity"] == "manager"
    assert report["closed_at"] == 1762310000.0
    assert report["artifact_refs"][0]["url"].endswith("/pull/96")


def test_mirroring_does_not_touch_the_process_status(tmp_path: Path) -> None:
    """``status`` is the process lifecycle and must stay byte-identical."""

    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    record = validate_close_out(_valid_delivered_payload(), closed_by="agent")
    report = {"status": "cancelled", "completion_satisfied": False, "abort_reason": "max_rounds_exhausted"}
    apply_close_out_to_report(report, record)
    assert report["status"] == "cancelled"
    assert report["completion_satisfied"] is False
    assert report["abort_reason"] == "max_rounds_exhausted"


def test_reading_a_missing_close_out_returns_none(tmp_path: Path) -> None:
    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    assert read_close_out(logs_dir) is None


# --------------------------------------------------------------------------
# 9. Immutability: one close-out per run, amended only with actor + reason.
# --------------------------------------------------------------------------


def test_a_second_close_out_is_refused(tmp_path: Path) -> None:
    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    record = validate_close_out(_valid_delivered_payload(), closed_by="agent")
    persist_close_out(logs_dir, record)
    with pytest.raises(CloseOutError):
        persist_close_out(logs_dir, record)


def test_a_second_close_out_is_allowed_with_an_audited_amendment(tmp_path: Path) -> None:
    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    record = validate_close_out(_valid_delivered_payload(), closed_by="agent")
    persist_close_out(logs_dir, record)
    amended_payload = _valid_delivered_payload()
    amended_payload["outcome"] = "delivered_partial"
    amended_payload["outcome_reason"] = "one of two PRs merged"
    amended = validate_close_out(amended_payload, closed_by="agent")
    amendment = CloseOutAmendment(
        actor="overseer1",
        reason="second PR did not merge",
        at=1762311000.0,
    )
    persist_close_out(logs_dir, amended, amendment=amendment)
    loaded = load_close_out(logs_dir)
    assert loaded is not None
    assert loaded.outcome == "delivered_partial"
    assert loaded.amendment is not None
    assert loaded.amendment.actor == "overseer1"
    assert loaded.amendment.reason == "second PR did not merge"


def test_an_amendment_without_an_actor_is_refused(tmp_path: Path) -> None:
    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    record = validate_close_out(_valid_delivered_payload(), closed_by="agent")
    persist_close_out(logs_dir, record)
    amended_payload = _valid_delivered_payload()
    amended_payload["outcome"] = "delivered_partial"
    amended = validate_close_out(amended_payload, closed_by="agent")
    amendment = CloseOutAmendment(actor="", reason="because", at=1762311000.0)
    with pytest.raises(CloseOutError):
        persist_close_out(logs_dir, amended, amendment=amendment)


def test_an_amendment_without_a_reason_is_refused(tmp_path: Path) -> None:
    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    record = validate_close_out(_valid_delivered_payload(), closed_by="agent")
    persist_close_out(logs_dir, record)
    amended_payload = _valid_delivered_payload()
    amended_payload["outcome"] = "delivered_partial"
    amended = validate_close_out(amended_payload, closed_by="agent")
    amendment = CloseOutAmendment(actor="overseer1", reason="", at=1762311000.0)
    with pytest.raises(CloseOutError):
        persist_close_out(logs_dir, amended, amendment=amendment)


def test_the_original_close_out_is_kept_in_the_amendment_history(tmp_path: Path) -> None:
    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    record = validate_close_out(_valid_delivered_payload(), closed_by="agent")
    persist_close_out(logs_dir, record)
    amended_payload = _valid_delivered_payload()
    amended_payload["outcome"] = "delivered_partial"
    amended = validate_close_out(amended_payload, closed_by="agent")
    persist_close_out(
        logs_dir,
        amended,
        amendment=CloseOutAmendment(actor="overseer1", reason="partial", at=1762311000.0),
    )
    loaded = load_close_out(logs_dir)
    assert loaded is not None
    assert loaded.amendment is not None
    assert loaded.amendment.previous is not None
    assert loaded.amendment.previous.outcome == "delivered"


# --------------------------------------------------------------------------
# 10. The reference worked example, encoded so it cannot rot.
# --------------------------------------------------------------------------


def test_the_reference_worked_example_is_derived_not_observed() -> None:
    """Run 20261105T030448Z_3102627c predates the contract."""

    parsed = parse_legacy_rationale("done: cogniziocompany/cognizioware-hydra#96")
    assert parsed is not None
    assert parsed.derived is True
    assert parsed.derived_from == "rationale_parse"
    assert parsed.closed_by == "human"


def test_a_derived_record_cannot_assert_delivered_on_its_own() -> None:
    parsed = parse_legacy_rationale("done: cogniziocompany/cognizioware-hydra#96")
    assert parsed is not None
    with pytest.raises(CloseOutValidationError) as excinfo:
        validate_close_out(parsed.to_dict(), closed_by="human")
    # The refusal says WHY: a derived value is a migration hint, not a form.
    assert "derived" in str(excinfo.value).lower()


# --------------------------------------------------------------------------
# 11. The agent's proposal, and the shared validator in the run layer.
# --------------------------------------------------------------------------


_AGENT_PLAN_WITH_CLOSEOUT = """\
Work is done; the PR is open.

```closeout
outcome: delivered
reason: opened hydra#96 and verified the head sha
artifact: pr cogniziocompany/cognizioware-hydra 96 773be7e
```
"""


def test_the_agent_proposal_is_parsed_into_a_close_out_form() -> None:
    form = parse_agent_close_out_proposal(_AGENT_PLAN_WITH_CLOSEOUT)
    assert form is not None
    assert form["outcome"] == "delivered"
    assert form["outcome_reason"] == "opened hydra#96 and verified the head sha"
    assert form["artifact_refs"][0]["kind"] == "pr"
    assert form["artifact_refs"][0]["repo"] == "cogniziocompany/cognizioware-hydra"
    assert form["artifact_refs"][0]["ref"] == "96"
    assert form["artifact_refs"][0]["url"].endswith("/pull/96")
    assert form["artifact_refs"][0]["head_sha"] == "773be7e"
    assert form["closed_by"] == "agent"


def test_a_plan_without_a_close_out_proposes_nothing() -> None:
    assert parse_agent_close_out_proposal("Next: done") is None
    assert parse_agent_close_out_proposal("") is None
    assert parse_agent_close_out_proposal(None) is None


def test_the_agent_proposal_is_still_refused_without_an_artifact() -> None:
    """A proposal is not a close-out: the shared validator still refuses it."""

    plan = """\
```closeout
outcome: delivered
reason: trust me
```
"""
    form = parse_agent_close_out_proposal(plan)
    assert form is not None
    form["closed_at"] = 1762310000.0
    form["closed_by_identity"] = "manager"
    with pytest.raises(CloseOutValidationError):
        validate_close_out(form, closed_by="agent")


def test_the_agent_and_human_paths_reach_the_same_validator() -> None:
    """Both closers produce identical record shapes through ONE validator."""

    agent_form = parse_agent_close_out_proposal(_AGENT_PLAN_WITH_CLOSEOUT)
    assert agent_form is not None
    agent_form["closed_at"] = 1762310000.0
    agent_form["closed_by_identity"] = "manager"
    human_form = dict(agent_form)
    human_form["closed_by"] = "human"
    human_form["closed_by_identity"] = "hydrafleet-gateway"

    agent_record = validate_close_out(agent_form, closed_by="agent")
    human_record = validate_close_out(human_form, closed_by="human")
    assert agent_record.to_dict().keys() == human_record.to_dict().keys()
    assert agent_record.outcome == human_record.outcome == "delivered"
    assert agent_record.delivered is human_record.delivered is True


def test_the_manager_records_a_refused_close_out_as_incomplete(tmp_path: Path) -> None:
    """An invalid close-out must NOT be reported as completed."""

    from lh_harness.manager import _record_close_out

    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    report = {"status": "cancelled", "completion_satisfied": False}
    # A delivery with no artifact is refused by the validator.
    form = {
        "outcome": "delivered",
        "outcome_reason": "trust me",
        "artifact_refs": [],
        "closed_by": "agent",
        "closed_by_identity": "manager",
        "closed_at": 1762310000.0,
    }
    record = _record_close_out(
        logs_dir=logs_dir,
        form=form,
        closed_by="agent",
        identity="manager",
        rounds=[],
        report=report,
    )
    assert record is not None
    assert record.outcome == "incomplete"
    assert record.delivered is False
    # The run is NOT reported as completed.
    assert report["status"] == "cancelled"
    assert report["outcome"] == "incomplete"
    assert report["delivered"] is False
    assert "artifact" in report["outcome_reason"].lower()


def test_the_manager_records_a_missing_close_out_as_incomplete(tmp_path: Path) -> None:
    from lh_harness.manager import _record_close_out

    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    report = {"status": "cancelled"}
    record = _record_close_out(
        logs_dir=logs_dir,
        form=None,
        closed_by="agent",
        identity="manager",
        rounds=[],
        report=report,
    )
    assert record is not None
    assert record.outcome == "incomplete"
    assert report["outcome"] == "incomplete"
    assert report["delivered"] is False


def test_a_derived_record_can_be_persisted_as_a_migration_hint(tmp_path: Path) -> None:
    """A legacy run may carry a derived close-out, clearly marked as such."""

    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    parsed = parse_legacy_rationale("done: cogniziocompany/cognizioware-hydra#96")
    assert parsed is not None
    persist_close_out(logs_dir, parsed)
    loaded = load_close_out(logs_dir)
    assert loaded is not None
    assert loaded.derived is True
    assert loaded.derived_from == "rationale_parse"
    assert loaded.outcome == "delivered"


def test_the_manager_persists_a_valid_close_out_next_to_report_json(tmp_path: Path) -> None:
    from lh_harness.manager import _record_close_out

    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    report = {"status": "cancelled"}
    form = _valid_delivered_payload()
    record = _record_close_out(
        logs_dir=logs_dir,
        form=form,
        closed_by="agent",
        identity="manager",
        rounds=[],
        report=report,
    )
    assert record is not None
    assert record.outcome == "delivered"
    assert (logs_dir / "closeout.json").is_file()
    persisted = load_close_out(logs_dir)
    assert persisted is not None
    assert persisted.outcome == "delivered"
    assert report["delivered"] is True
    # The process lifecycle is untouched.
    assert report["status"] == "cancelled"


def test_the_human_path_produces_the_same_record_shape_as_the_agent(tmp_path: Path) -> None:
    """The human close path is not a second mechanism."""

    from lh_harness.manager import _record_close_out

    logs_dir = tmp_path / "lh_harness"
    logs_dir.mkdir()
    human_form = {
        "outcome": "delivered",
        "outcome_reason": "resolved gate 7112035f454d",
        "artifact_refs": [
            {
                "kind": "pr",
                "repo": "cogniziocompany/cognizioware-hydra",
                "ref": "96",
                "url": "https://github.com/cogniziocompany/cognizioware-hydra/pull/96",
                "head_sha": _PR_HEAD_SHA,
            }
        ],
        "closed_by": "human",
        "closed_by_identity": "hydrafleet-gateway",
        "closed_at": 1762310000.0,
    }
    report = {"status": "cancelled"}
    record = _record_close_out(
        logs_dir=logs_dir,
        form=human_form,
        closed_by="human",
        identity="hydrafleet-gateway",
        rounds=[],
        report=report,
    )
    assert record is not None
    assert record.outcome == "delivered"
    assert record.closed_by == "human"
    assert record.closed_by_identity == "hydrafleet-gateway"
    assert record.delivered is True
    # The same record shape as the agent path.
    agent_record = validate_close_out(_valid_delivered_payload(), closed_by="agent")
    assert record.to_dict().keys() == agent_record.to_dict().keys()
    assert report["delivered"] is True
