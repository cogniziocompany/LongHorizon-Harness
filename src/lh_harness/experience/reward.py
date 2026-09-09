"""Terminal reward R for one run, split into goal/process/satisfaction terms.

The scorecard finding that drives this module: the harness already computes a
categorical final verdict and then discards it. ``terminal_reward`` turns the
final report, the approval ledger, the operator messages and the managed
rounds into a scalar R in [-1, 1] with each term recorded separately:

    R = 0.45 * goal + 0.30 * process + 0.25 * satisfaction

* ``goal`` — did the run achieve the task: final status, completion flag,
  abort reason, and the last non-synthetic auditor verdict triple.
* ``process`` — how the work was carried out: budget usage, role episodes
  that timed out, and rounds the harness spent repairing malformed reports.
* ``satisfaction`` — what the operator signalled: approval resolutions and
  their rationale text, plus operator instruction messages.

No-signal rule (spec): a ``user_cancelled`` abort whose provenance is the web
control plane (``created_by`` ``web``) carries no signal and returns ``None``.
Cancellations that the harness cannot attribute to the web wallboard keep a
computed value; the attribution evidence available today is the run's own
control-bus records (``kind`` ``stop``/``abort`` with ``created_by``). This
module never invents provenance: with no evidence either way it computes.

Everything here is deterministic — no LLM calls, no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..auditor_agent import parse_audit_report

WEIGHT_GOAL = 0.45
WEIGHT_PROCESS = 0.30
WEIGHT_SATISFACTION = 0.25

_CANCELLED_STATUSES = frozenset({"cancelled"})
_TERMINAL_CANCEL_COMMAND_KINDS = frozenset({"stop", "abort"})

# Base goal score by final report status (manager._final_report vocabulary).
_GOAL_STATUS_SCORES: dict[str, float] = {
    "complete": 1.0,
    "completed": 1.0,
    "incomplete": -0.5,
    "blocked": -0.75,
    "cancelled": -0.5,
    "failed": -1.0,
}
_GOAL_UNKNOWN_STATUS_SCORE = -0.5

# Refinement when the abort reason adds information the status does not
# already carry. ``provider_*`` failures are hard provider-side aborts.
_ABORT_REASON_ADJUSTMENTS: dict[str, float] = {
    "": 0.0,
    "user_cancelled": 0.0,
    "worker_cancelled": 0.0,
    "worker_exception": 0.0,
    "manager_blocked": 0.0,
    "needs_human_input": 0.25,
    "human_abort": 0.25,
    "max_rounds_exhausted": -0.25,
}

# Auditor verdict-triple refinements applied to the goal term.
_INTEGRITY_ADJUSTMENTS: dict[str, float] = {"violation": -0.25, "suspect": -0.1, "clean": 0.0}
_CONTRACT_ADJUSTMENTS: dict[str, float] = {
    "aligned": 0.0,
    "unknown": 0.0,
    "needs_revision": -0.15,
    "invalid": -0.25,
}
_AUDIT_STATUS_ADJUSTMENTS: dict[str, float] = {"blocked": -0.1}

# Process penalties, per round of each kind.
_TIMEOUT_PENALTY = 0.25
_FORMAT_REPAIR_PENALTY = 0.15

# Approval resolutions: the operator's chosen action is the satisfaction signal.
_APPROVAL_ACTION_SCORES: dict[str, float] = {
    "continue": 0.5,
    "approve": 0.5,
    "approved": 0.5,
    "resume": 0.5,
    "retry": -0.25,
    "stop": -0.5,
    "abort": -1.0,
    "deny": -1.0,
    "rejected": -1.0,
    "reject": -1.0,
    "blocked": -0.5,
    "unknown": 0.0,
    "": 0.0,
}
_UNRESOLVED_APPROVAL_PENALTY = -0.25
_MAX_KEYWORD_ADJUSTMENT = 0.2
_OPERATOR_MESSAGE_PENALTY = 0.1

_POSITIVE_KEYWORDS = (
    "good",
    "great",
    "perfect",
    "thanks",
    "thank you",
    "lgtm",
    "nice",
    "works",
    "correct",
    "verified",
)
_NEGATIVE_KEYWORDS = (
    "wrong",
    "incorrect",
    "failed",
    "broken",
    "regression",
    "revert",
    "bad",
)


@dataclass(frozen=True)
class RewardBreakdown:
    """The recorded reward components plus the scalar value.

    ``value``/terms are ``None`` when the run carries no signal (a
    ``user_cancelled`` abort attributed to the web control plane); ``reason``
    explains a ``None`` so a reader never has to guess why a trace is unvalued.
    """

    goal: float | None
    process: float | None
    satisfaction: float | None
    value: float | None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "process": self.process,
            "satisfaction": self.satisfaction,
            "value": self.value,
            "reason": self.reason,
        }


def terminal_reward(
    report: Mapping[str, Any] | None,
    approvals: Iterable[Any] | None,
    operator_messages: Iterable[Any] | None,
    rounds: Iterable[Any] | None,
    config: Any,
) -> float | None:
    """Terminal reward R in [-1, 1], or ``None`` for a no-signal run."""
    breakdown = reward_breakdown(report, approvals, operator_messages, rounds, config)
    return breakdown.value


def reward_breakdown(
    report: Mapping[str, Any] | None,
    approvals: Iterable[Any] | None,
    operator_messages: Iterable[Any] | None,
    rounds: Iterable[Any] | None,
    config: Any,
) -> RewardBreakdown:
    """Compute the three reward terms and the weighted scalar R."""
    data = dict(report) if isinstance(report, Mapping) else {}
    status = str(data.get("status") or "").strip().lower()
    abort_reason = str(data.get("abort_reason") or "").strip().lower()
    completion_satisfied = bool(data.get("completion_satisfied"))

    messages = [item for item in (operator_messages or []) if isinstance(item, Mapping)]

    if status in _CANCELLED_STATUSES or abort_reason == "user_cancelled":
        if _is_web_cancelled(data, messages):
            return RewardBreakdown(
                goal=None,
                process=None,
                satisfaction=None,
                value=None,
                reason="no_signal:user_cancelled_web",
            )

    goal = _goal_term(data, status, abort_reason, completion_satisfied, rounds)
    process = _process_term(data, rounds, config)
    satisfaction = _satisfaction_term(approvals, messages)
    value = round(
        WEIGHT_GOAL * goal + WEIGHT_PROCESS * process + WEIGHT_SATISFACTION * satisfaction, 6
    )
    value = _clamp(value, -1.0, 1.0)
    return RewardBreakdown(
        goal=round(goal, 6),
        process=round(process, 6),
        satisfaction=round(satisfaction, 6),
        value=value,
        reason="",
    )


def _is_web_cancelled(report: Mapping[str, Any], messages: Sequence[Mapping[str, Any]]) -> bool:
    """True only when the run's own records attribute the cancel to the web."""
    for key in ("created_by", "cancel_created_by", "abort_created_by"):
        if str(report.get(key) or "").strip().lower() == "web":
            return True
    for item in messages:
        created_by = str(item.get("created_by") or "").strip().lower()
        kind = str(item.get("kind") or item.get("event") or "").strip().lower()
        if created_by == "web" and kind in _TERMINAL_CANCEL_COMMAND_KINDS:
            return True
    return False


def _goal_term(
    data: Mapping[str, Any],
    status: str,
    abort_reason: str,
    completion_satisfied: bool,
    rounds: Iterable[Any] | None,
) -> float:
    base = _GOAL_STATUS_SCORES.get(status, _GOAL_UNKNOWN_STATUS_SCORE)
    if completion_satisfied and status in {"complete", "completed"}:
        base = 1.0
    adjustment = _ABORT_REASON_ADJUSTMENTS.get(abort_reason)
    if adjustment is None:
        # Unrecognised reasons keep their prefix class: provider_* are hard
        # provider-side aborts, anything else is neutral.
        adjustment = -0.25 if abort_reason.startswith("provider_") else 0.0
    goal = base + adjustment
    goal += _verdict_adjustment(rounds)
    return _clamp(goal, -1.0, 1.0)


def _verdict_adjustment(rounds: Iterable[Any] | None) -> float:
    """Refine the goal with the last non-synthetic auditor verdict triple.

    A "complete" run whose final audit could not verify its own claims is
    worth slightly less than one the auditor confirmed; a violation or an
    invalid contract audit drags the goal down regardless of status.
    """
    total = 0.0
    for round_record in _latest_audit_rounds(rounds or ()):
        text = str(_round_mapping(round_record).get("auditor_report") or "").strip()
        if not text:
            continue
        audit = parse_audit_report(text, int(_round_mapping(round_record).get("round_index", 0) or 0))
        total += _INTEGRITY_ADJUSTMENTS.get(audit.integrity_status, 0.0)
        total += _CONTRACT_ADJUSTMENTS.get(audit.contract_audit_status, 0.0)
        total += _AUDIT_STATUS_ADJUSTMENTS.get(audit.status, 0.0)
        break
    return total


def _latest_audit_rounds(rounds: Iterable[Any]) -> Iterable[Any]:
    """The last round that carries a real auditor verdict (synthetic rounds skip)."""
    material = list(rounds or [])
    for round_record in reversed(material):
        mapping = _round_mapping(round_record)
        auditor_status = mapping.get("auditor_status")
        if isinstance(auditor_status, Mapping) and (
            auditor_status.get("invalid_plan") or auditor_status.get("invalid_completion")
        ):
            continue
        yield round_record


def _process_term(
    data: Mapping[str, Any], rounds: Iterable[Any] | None, config: Any
) -> float:
    material = list(rounds or [])
    # The report's ``rounds_run`` is the authoritative budget consumption the
    # spec names; the round list only backs it up when the report omits it.
    try:
        rounds_run = int(data.get("rounds_run"))
    except (TypeError, ValueError):
        rounds_run = -1
    if rounds_run < 0:
        rounds_run = len(material)
    try:
        max_rounds = int(data.get("max_rounds") or 0)
    except (TypeError, ValueError):
        max_rounds = 0
    if max_rounds <= 0:
        max_rounds = int(getattr(config, "max_total_episodes", 0) or 0) or max(1, rounds_run)
    budget_ratio = rounds_run / max(1, max_rounds)
    process = 1.0 - budget_ratio
    timeouts = sum(
        1
        for round_record in material
        for name in ("manager_status", "executor_status", "auditor_status")
        if _status_mapping(_round_mapping(round_record).get(name)).get("status") == "timeout"
    )
    repairs = sum(
        1
        for round_record in material
        if any(
            _status_mapping(_round_mapping(round_record).get("auditor_status")).get(key)
            for key in ("invalid_plan", "invalid_completion")
        )
    )
    process -= _TIMEOUT_PENALTY * timeouts
    process -= _FORMAT_REPAIR_PENALTY * repairs
    return _clamp(process, -1.0, 1.0)


def _satisfaction_term(
    approvals: Iterable[Any] | None, messages: Sequence[Mapping[str, Any]]
) -> float:
    resolutions = _latest_by_id(approvals or ())
    if not resolutions and not messages:
        return 0.0
    scores: list[float] = []
    for record in resolutions:
        resolved = str(record.get("status") or "").strip().lower() == "resolved"
        action = str(record.get("action") or "").strip().lower()
        extra_rounds = _as_int(record.get("extra_rounds"))
        if not resolved:
            # The run ended with an unanswered checkpoint.
            scores.append(_UNRESOLVED_APPROVAL_PENALTY)
            continue
        base = _APPROVAL_ACTION_SCORES.get(action)
        if base is None:
            base = _APPROVAL_ACTION_SCORES.get("unknown", 0.0)
        if extra_rounds and action in {"continue", "approve", "approved", "resume"}:
            # Granting extra rounds is a continue with more budget attached.
            base = max(base, _APPROVAL_ACTION_SCORES["continue"])
        rationale = " ".join(
            str(record.get(key) or "")
            for key in ("reason", "user_input", "message", "title")
        )
        base += _keyword_adjustment(rationale)
        scores.append(base)
    for message in messages:
        # An operator instruction is steering: the run needed a nudge, so each
        # message carries a small penalty, nudged by the message's own words.
        text = str(message.get("text") or message.get("instructions") or "")
        scores.append(_keyword_adjustment(text) - _OPERATOR_MESSAGE_PENALTY)
    if not scores:
        return 0.0
    mean = sum(scores) / len(scores)
    return _clamp(mean, -1.0, 1.0)


def _keyword_adjustment(text: str) -> float:
    """Rule-based sentiment nudge from operator rationale text.

    Deliberately crude and capped: it can move a satisfaction term by at most
    ``_MAX_KEYWORD_ADJUSTMENT`` and never invents a resolution that did not
    happen. No LLM call.
    """
    lowered = str(text or "").lower()
    adjustment = 0.0
    if any(keyword in lowered for keyword in _POSITIVE_KEYWORDS):
        adjustment += 0.1
    if any(keyword in lowered for keyword in _NEGATIVE_KEYWORDS):
        adjustment -= 0.1
    return max(-_MAX_KEYWORD_ADJUSTMENT, min(_MAX_KEYWORD_ADJUSTMENT, adjustment))


def _latest_by_id(records: Iterable[Any]) -> list[dict[str, Any]]:
    """Deduplicate approval records by id, keeping the latest occurrence.

    ``approvals.jsonl`` appends a pending record and later a resolved record
    for the same checkpoint; only the resolved (or latest) state matters.
    """
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        approval_id = str(record.get("approval_id") or "")
        if not approval_id:
            continue
        latest[approval_id] = dict(record)
    return list(latest.values())


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _round_mapping(record: Any) -> Mapping[str, Any]:
    if isinstance(record, Mapping):
        return record
    if hasattr(record, "__dataclass_fields__"):
        return {name: getattr(record, name) for name in record.__dataclass_fields__}
    state = getattr(record, "__dict__", None)
    if isinstance(state, Mapping):
        return state
    return {}


def _status_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
