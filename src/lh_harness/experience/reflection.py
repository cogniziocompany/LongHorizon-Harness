"""Reflection weighting (the paper's alpha) for L1 traces.

MSCE Eq. 2 backfills each step value with a reflection weight alpha in [0, 1]:
faithful, concrete, causally informative reflections get high weight, while
empty, tautological, or unsupported ones get low weight. The paper estimates
alpha with an LLM scoring prompt; this harness does something stronger — the
reflection source is the independent auditor, and the weight is a *deterministic*
function of the AuditReport verdict triple (status, integrity, contract audit),
with no LLM call and no prompt change.

Mapping (deliberately simple and testable):

* ``ALPHA_HIGH`` — a clean parseable verdict whose constraints are verified:
  the control header parses, integrity is clean, and the contract audit is
  aligned. ``0.7`` keeps the harness numerically aligned with the paper's own
  Table 5 worked example.
* ``ALPHA_MID`` — a parseable verdict that could not fully verify the round
  (suspect integrity, unknown/needs-revision contract, or a blocked audit).
* ``ALPHA_LOW`` — reflections that must not be trusted: harness-synthesized
  format-repair feedback (``invalid_plan``/``invalid_completion``), episodes
  that timed out, integrity violations, invalid contract audits, and rounds
  that produced no auditor text at all.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..auditor_agent import parse_audit_report

ALPHA_HIGH = 0.7
ALPHA_MID = 0.5
ALPHA_LOW = 0.3

# Harness-synthesized feedback keys, mirroring manager._record_round usage.
_SYNTHETIC_AUDIT_KEYS = ("invalid_plan", "invalid_completion")
_TIMEOUT_STATUS = "timeout"


def alpha(round_record: Any) -> float:
    """Return the reflection weight for one managed round.

    ``round_record`` is a ``ManagedRound`` or its ``asdict`` form. The result
    is deterministic: identical input, identical weight.
    """
    record = _round_mapping(round_record)
    index = int(record.get("round_index", 0) or 0)
    auditor_status = _status_mapping(record.get("auditor_status"))

    # Synthetic repair feedback is written by the harness, not by the auditor:
    # it describes a malformed report, not the task.
    if any(auditor_status.get(key) for key in _SYNTHETIC_AUDIT_KEYS):
        return ALPHA_LOW

    # A role episode that timed out leaves the round's own output unverifiable.
    if any(
        _status_mapping(record.get(name)).get("status") == _TIMEOUT_STATUS
        for name in ("manager_status", "executor_status", "auditor_status")
    ):
        return ALPHA_LOW

    text = str(record.get("auditor_report") or "").strip()
    if not text:
        # No reflection available; the step should mostly inherit the value
        # from later steps (paper: empty reflections get low weights).
        return ALPHA_LOW

    audit = parse_audit_report(text, index)
    if audit.integrity_status == "violation" or audit.contract_audit_status == "invalid":
        return ALPHA_LOW
    if (
        audit.integrity_status == "clean"
        and audit.contract_audit_status == "aligned"
        and audit.status != "blocked"
    ):
        return ALPHA_HIGH
    return ALPHA_MID


def _round_mapping(record: Any) -> Mapping[str, Any]:
    if isinstance(record, Mapping):
        return record
    if hasattr(record, "__dataclass_fields__"):
        return {name: getattr(record, name) for name in record.__dataclass_fields__}
    state = getattr(record, "__dict__", None)
    if isinstance(state, Mapping):
        merged = dict(state)
        # Round records may expose fields as class attributes too.
        for name in _ROUND_FIELDS:
            if name not in merged:
                value = getattr(record, name, None)
                if value is not None:
                    merged[name] = value
        return merged
    return {}


_ROUND_FIELDS = (
    "round_index",
    "next_step",
    "plan_text",
    "executor_output",
    "auditor_report",
    "harness_feedback",
    "task_state",
    "task_contract",
    "manager_status",
    "executor_status",
    "auditor_status",
)


def _status_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}
