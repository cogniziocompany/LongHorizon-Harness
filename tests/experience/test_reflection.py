"""Reflection weights: deterministic alpha from the AuditReport verdict triple."""

from __future__ import annotations

import pytest

from lh_harness.experience import ALPHA_HIGH, ALPHA_LOW, ALPHA_MID, alpha

_CLEAN_REPORT = (
    "Status: complete\n"
    "Integrity: clean\n"
    "Contract audit: aligned\n"
    "\n"
    "Audit facts. The slice-1 modules exist and the Table 5 value matches.\n"
)
_SUSPECT_REPORT = (
    "Status: incomplete\n"
    "Integrity: suspect\n"
    "Contract audit: unknown\n"
    "\n"
    "Audit facts. The report could not fully verify the claims.\n"
)
_VIOLATION_REPORT = (
    "Status: incomplete\n"
    "Integrity: violation\n"
    "Contract audit: unknown\n"
    "\n"
    "Audit facts. The auditor observed a workspace mutation.\n"
)
_INVALID_CONTRACT_REPORT = (
    "Status: incomplete\n"
    "Integrity: clean\n"
    "Contract audit: invalid\n"
    "\n"
    "Audit facts. The round misread the contract.\n"
)


def _round(text: str = "", *, auditor_status: dict | None = None, **statuses) -> dict:
    record = {
        "round_index": 3,
        "next_step": "cli",
        "auditor_report": text,
        "auditor_status": auditor_status or {},
        "manager_status": {},
        "executor_status": {},
    }
    record.update(statuses)
    return record


def test_clean_aligned_verdict_gets_high_weight() -> None:
    assert alpha(_round(_CLEAN_REPORT)) == ALPHA_HIGH == 0.7


def test_high_alpha_matches_the_paper_table5_weight() -> None:
    # The harness's "verified" weight is the paper's own Table 5 alpha so the
    # worked example carries over unchanged.
    assert ALPHA_HIGH == 0.7


def test_degraded_verdict_gets_mid_weight() -> None:
    assert alpha(_round(_SUSPECT_REPORT)) == ALPHA_MID == 0.5
    assert (
        alpha(
            _round(
                "Status: incomplete\nIntegrity: clean\nContract audit: needs_revision\n\nAudit facts.\n"
            )
        )
        == ALPHA_MID
    )
    assert alpha(_round("Status: blocked\nIntegrity: clean\nContract audit: aligned\n\nAudit facts.\n")) == ALPHA_MID


def test_violation_and_invalid_contract_get_low_weight() -> None:
    assert alpha(_round(_VIOLATION_REPORT)) == ALPHA_LOW == 0.3
    assert alpha(_round(_INVALID_CONTRACT_REPORT)) == ALPHA_LOW


def test_synthetic_repair_rounds_get_low_weight() -> None:
    assert alpha(_round(_CLEAN_REPORT, auditor_status={"invalid_plan": True})) == ALPHA_LOW
    assert alpha(_round(_CLEAN_REPORT, auditor_status={"invalid_completion": True})) == ALPHA_LOW


def test_timeouts_get_low_weight() -> None:
    assert alpha(_round(_CLEAN_REPORT, executor_status={"status": "timeout"})) == ALPHA_LOW
    assert alpha(_round(_CLEAN_REPORT, manager_status={"status": "timeout"})) == ALPHA_LOW
    assert alpha(_round(_CLEAN_REPORT, auditor_status={"status": "timeout"})) == ALPHA_LOW


def test_missing_auditor_text_gets_low_weight() -> None:
    assert alpha(_round("")) == ALPHA_LOW


def test_synthetic_beats_clean_report() -> None:
    # A synthesized repair report must not inherit the clean verdict's weight.
    synthetic = _round(_CLEAN_REPORT, auditor_status={"invalid_completion": True})
    assert alpha(synthetic) == ALPHA_LOW
    assert alpha(synthetic) < alpha(_round(_SUSPECT_REPORT)) < alpha(_round(_CLEAN_REPORT))


def test_alpha_is_deterministic() -> None:
    record = _round(_CLEAN_REPORT)
    assert alpha(record) == alpha(record) == alpha(dict(record))


@pytest.mark.parametrize(
    "text",
    [
        _CLEAN_REPORT,
        _SUSPECT_REPORT,
        _VIOLATION_REPORT,
        _INVALID_CONTRACT_REPORT,
    ],
)
def test_alpha_accepts_dataclass_shaped_rounds(text: str) -> None:
    class _Stub:  # ManagedRound duck type
        round_index = 3
        manager_status = {}
        executor_status = {}
        auditor_status = {}
        auditor_report = text

    assert alpha(_Stub()) == alpha(_round(text))
