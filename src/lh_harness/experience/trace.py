"""L1 trace memory: one bounded, evidence-anchored unit per managed round.

A ``TraceUnit`` is the harness's instantiation of the paper's L1 step
``f(1) = (s, a, o, rho, V)``: the semantic state, the action, the observation
the harness fed back, the independent auditor's reflection, and (once the
terminal reward is backfilled) the step value ``V``.

Persistence follows MSCE B.11: long fields are truncated to bounded caps and
sensitive strings are removed before anything is written. Redaction itself
lives in :mod:`lh_harness.experience.redact` (slice 2) and is applied before
every write; the caps here bound how much text a trace can carry in the first
place.

The device dimension (Paxton 2026-09-08) adds ``device_id``, ``terminal_id``
and ``hydra_node`` to every trace that reached into the Hydra device fleet.
These are the *identical strings* Hydra and the fleet surfaces use — never
re-keyed, never truncated, and absent (not empty, not ``""``) when a round did
no remote execution. Phase 1 cannot populate them yet because the round data
the harness records does not identify which device a tool call executed on;
``capture.py`` (slice 3) only fills them if a future round record carries real
identifiers. Nothing in this module invents a value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# Field caps mirror the clipping budgets the roles already use
# (``HarnessConfig`` in lh_harness/types.py): the auditor's report budget for
# the reflection and the state summary it produced, the verified-context
# budget for what the run observed, and the history budget for the action
# record. ``TraceCaps.from_config`` reads the live values instead of assuming
# the defaults, so a configuration that tightens clipping tightens traces.
DEFAULT_AUDITOR_OUTPUT_CHARS = 24_000
DEFAULT_ROLE_VERIFIED_CONTEXT_CHARS = 60_000
DEFAULT_ROLE_HISTORY_CHARS = 100_000

# Paxton 2026-09-08: the run owner carries ``route``; the per-role agent/model
# read ``owner.route.bound.roles`` when present and fall back to
# ``owner.role_configs`` (slice 3 wiring). The rationale is bounded here and
# must be redacted (slice 2) before it is ever populated.
ROUTE_RATIONALE_MAX_CHARS = 512

_MAX_TOOL_NAMES = 64
_MAX_DOMAIN_TAGS = 16
_MAX_RUN_ID_CHARS = 128

# "successor to <run_id>" on the first task-text line inherits the previous
# run's task context (MSCE B.2 multi-episode handling).
_SUCCESSOR_RUN_ID_RE = re.compile(r"\bsuccessor\s+to\s+([^\s,;:)]+)")

DEVICE_FIELD_NAMES = ("device_id", "terminal_id", "hydra_node")


def _clip_text(text: str, max_chars: int) -> str:
    """Clip to ``max_chars`` keeping the head and tail.

    Mirrors the role prompts' ``_clip_preserve`` style (head + tail with an
    explicit truncation marker) with one difference required by MSCE B.11:
    persistence caps are hard, so the marker's own length is deducted and the
    clipped field never exceeds ``max_chars``.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    marker = f"\n\n...[truncated {len(text) - max_chars} chars; kept head and tail]...\n\n"
    budget = max_chars - len(marker)
    if budget < 2:
        return text[:max_chars]
    head_chars = max(1, int(budget * 0.65))
    tail_chars = max(1, budget - head_chars)
    clipped = (
        text[:head_chars].rstrip()
        + marker
        + text[-tail_chars:].lstrip()
    )
    return clipped[:max_chars] if len(clipped) > max_chars else clipped


@dataclass(frozen=True)
class TraceCaps:
    """Bounded-persistence limits for L1 trace fields."""

    state_summary_chars: int = DEFAULT_AUDITOR_OUTPUT_CHARS
    reflection_chars: int = DEFAULT_AUDITOR_OUTPUT_CHARS
    observation_chars: int = DEFAULT_ROLE_VERIFIED_CONTEXT_CHARS
    action_chars: int = DEFAULT_ROLE_HISTORY_CHARS
    route_rationale_chars: int = ROUTE_RATIONALE_MAX_CHARS
    max_tool_names: int = _MAX_TOOL_NAMES
    max_domain_tags: int = _MAX_DOMAIN_TAGS

    @classmethod
    def from_config(cls, config: Any) -> "TraceCaps":
        """Read the caps from the live ``HarnessConfig`` clipping budget."""
        return cls(
            state_summary_chars=int(getattr(config, "auditor_output_chars", DEFAULT_AUDITOR_OUTPUT_CHARS) or DEFAULT_AUDITOR_OUTPUT_CHARS),
            reflection_chars=int(getattr(config, "auditor_output_chars", DEFAULT_AUDITOR_OUTPUT_CHARS) or DEFAULT_AUDITOR_OUTPUT_CHARS),
            observation_chars=int(
                getattr(config, "role_verified_context_chars", DEFAULT_ROLE_VERIFIED_CONTEXT_CHARS)
                or DEFAULT_ROLE_VERIFIED_CONTEXT_CHARS
            ),
            action_chars=int(getattr(config, "role_history_chars", DEFAULT_ROLE_HISTORY_CHARS) or DEFAULT_ROLE_HISTORY_CHARS),
        )

    def cap_tool_names(self, names: Sequence[str]) -> tuple[str, ...]:
        cleaned = tuple(name for name in (str(item).strip() for item in names) if name)
        return cleaned[: max(0, int(self.max_tool_names))]

    def cap_domain_tags(self, tags: Sequence[str]) -> tuple[str, ...]:
        cleaned = tuple(tag for tag in (str(item).strip() for item in tags) if tag)
        return cleaned[: max(0, int(self.max_domain_tags))]


@dataclass(frozen=True)
class RoleTrace:
    """One role's agent/model identity for the round, plus optional routing.

    ``agent``/``model`` come from ``owner.route.bound.roles`` when the owner
    carries routing, otherwise from ``owner.role_configs`` (slice 3 wiring).
    ``route_tier`` and ``route_rationale`` are optional and stay ``None``
    until slice 3 reads them from the owner document; the rationale is
    redacted and capped before it is stored, never written raw.
    """

    role: str
    agent: str | None = None
    model: str | None = None
    route_tier: str | None = None
    route_rationale: str | None = None

    def __post_init__(self) -> None:
        if not str(self.role or "").strip():
            raise ValueError("RoleTrace.role must be a non-empty role name")
        object.__setattr__(self, "role", str(self.role).strip())
        for name in ("agent", "model", "route_tier"):
            value = getattr(self, name)
            if value is not None:
                text = str(value).strip()
                if not text:
                    # An identity that is known to exist but has no value is
                    # absent, not an empty string.
                    text = None
                object.__setattr__(self, name, text)
        if self.route_rationale is not None:
            object.__setattr__(
                self,
                "route_rationale",
                _clip_text(str(self.route_rationale), ROUTE_RATIONALE_MAX_CHARS),
            )

    def to_dict(self) -> dict[str, Any]:
        return _compact(
            {
                "role": self.role,
                "agent": self.agent,
                "model": self.model,
                "route_tier": self.route_tier,
                "route_rationale": self.route_rationale,
            }
        )


@dataclass(frozen=True)
class RewardTerms:
    """The three recorded reward components (spec: terms recorded separately)."""

    goal: float | None = None
    process: float | None = None
    satisfaction: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return _compact(
            {"goal": self.goal, "process": self.process, "satisfaction": self.satisfaction}
        )


@dataclass
class TraceUnit:
    """One per-round L1 trace (evidence layer of the MSCE memory hierarchy)."""

    run_id: str
    task_context_id: str
    round_index: int
    next_step: str = ""
    state_summary: str = ""
    action: str = ""
    observation: str = ""
    reflection: str = ""
    alpha: float | None = None
    value: float | None = None
    reward_terms: RewardTerms = field(default_factory=RewardTerms)
    evidence_id: str = ""
    domain_tags: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    error_signature: str | None = None
    workspace: str = ""
    repo: str = ""
    branch: str = ""
    roles: dict[str, RoleTrace] = field(default_factory=dict)
    occurred_at: str | None = None
    # Device dimension. ``None`` means the round did no remote execution;
    # empty strings are rejected so an absent dimension can never be
    # mistaken for a recorded one.
    device_id: str | None = None
    terminal_id: str | None = None
    hydra_node: str | None = None

    def __post_init__(self) -> None:
        self.run_id = str(self.run_id or "").strip()
        if not self.run_id:
            raise ValueError("TraceUnit.run_id must be a non-empty run id")
        self.task_context_id = str(self.task_context_id or "").strip()
        if not self.task_context_id:
            raise ValueError("TraceUnit.task_context_id must be a non-empty task context id")
        self.round_index = int(self.round_index)
        self.next_step = str(self.next_step or "").strip()
        self.state_summary = str(self.state_summary or "")
        self.action = str(self.action or "")
        self.observation = str(self.observation or "")
        self.reflection = str(self.reflection or "")
        self.evidence_id = str(self.evidence_id or "").strip()
        if isinstance(self.reward_terms, Mapping) and not isinstance(self.reward_terms, RewardTerms):
            self.reward_terms = RewardTerms(
                goal=_optional_float(self.reward_terms.get("goal")),
                process=_optional_float(self.reward_terms.get("process")),
                satisfaction=_optional_float(self.reward_terms.get("satisfaction")),
            )
        if self.roles:
            self.roles = {
                str(name): value
                if isinstance(value, RoleTrace)
                else _role_trace_from_mapping(name, value)
                for name, value in self.roles.items()
            }
        if self.alpha is not None:
            self.alpha = float(self.alpha)
        if self.value is not None:
            self.value = float(self.value)
        for name in DEVICE_FIELD_NAMES:
            value = getattr(self, name)
            if value is not None and not str(value).strip():
                raise ValueError(
                    f"TraceUnit.{name} must be absent (None) when the round did no "
                    "remote execution; empty strings are not device identities"
                )
        occurred = str(self.occurred_at).strip() if self.occurred_at is not None else ""
        self.occurred_at = occurred or None
        for name in ("workspace", "repo", "branch"):
            value = getattr(self, name)
            object.__setattr__(self, name, str(value or "").strip())
        self.domain_tags = _clean_tuple(self.domain_tags, _MAX_DOMAIN_TAGS)
        self.tool_names = _clean_tuple(self.tool_names, _MAX_TOOL_NAMES)
        if self.error_signature is not None and not str(self.error_signature).strip():
            self.error_signature = None
        else:
            self.error_signature = (
                str(self.error_signature).strip() if self.error_signature is not None else None
            )

    def to_dict(self) -> dict[str, Any]:
        """JSONL-ready record.

        Fields the run did not record are omitted rather than emitted as
        ``""``/``null`` so a reader can never mistake an absent dimension for
        a recorded one.
        """
        roles = {name: role.to_dict() for name, role in self.roles.items()}
        payload = {
            "run_id": self.run_id,
            "task_context_id": self.task_context_id,
            "round_index": self.round_index,
            "next_step": self.next_step,
            "state_summary": self.state_summary,
            "action": self.action,
            "observation": self.observation,
            "reflection": self.reflection,
            "alpha": self.alpha,
            "value": self.value,
            "reward_terms": self.reward_terms.to_dict(),
            "evidence_id": self.evidence_id,
            "domain_tags": list(self.domain_tags),
            "tool_names": list(self.tool_names),
            "error_signature": self.error_signature,
            "workspace": self.workspace,
            "repo": self.repo,
            "branch": self.branch,
            "roles": roles,
            "occurred_at": self.occurred_at,
            "device_id": self.device_id,
            "terminal_id": self.terminal_id,
            "hydra_node": self.hydra_node,
        }
        return _compact(payload)


def task_context_id(task_text: str, run_id: str) -> str:
    """Resolve the task-context id for a run.

    MSCE B.2 treats a follow-up request that continues a previous task as the
    same episode chain, so the first task-text line is scanned for
    ``successor to <run_id>``; when it matches, the referenced run id is
    inherited. Any other text keeps this run's own id.
    """
    text = str(task_text or "")
    first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
    match = _SUCCESSOR_RUN_ID_RE.search(first_line)
    if match:
        inherited = match.group(1).strip().strip(".,;")
        inherited = inherited[:_MAX_RUN_ID_CHARS]
        if inherited:
            return inherited
    return str(run_id or "").strip()


def trace_unit_from_round(
    round_record: Any,
    *,
    run_id: str,
    task_text: str = "",
    task_context: str | None = None,
    reward_terms: RewardTerms | Mapping[str, Any] | None = None,
    alpha: float | None = None,
    value: float | None = None,
    domain_tags: Sequence[str] = (),
    tool_names: Sequence[str] = (),
    error_signature: str | None = None,
    roles: Mapping[str, RoleTrace] | None = None,
    workspace: str = "",
    repo: str = "",
    branch: str = "",
    evidence_id: str | None = None,
    occurred_at: str | None = None,
    caps: TraceCaps | None = None,
) -> TraceUnit:
    """Build one ``TraceUnit`` from a managed round record.

    ``round_record`` is a ``ManagedRound`` or its ``asdict`` form. The four
    trace texts are the harness's own per-round artifacts: the manager's
    running task state (semantic context), the executor's output (action),
    the harness feedback (observation), and the independent auditor's report
    (reflection — the harness's auditor is a stronger reflection source than
    the paper's self-reflection, per the assessment).
    """
    limits = caps or TraceCaps()
    record = round_record if isinstance(round_record, Mapping) else _dataclass_fields(round_record)
    context = task_context if task_context is not None else task_context_id(task_text, run_id)
    index = int(record.get("round_index", 0) or 0)
    if evidence_id is None:
        evidence_id = f"{run_id}:round_{index:03d}"
    terms: RewardTerms
    if isinstance(reward_terms, RewardTerms):
        terms = reward_terms
    elif isinstance(reward_terms, Mapping):
        terms = RewardTerms(
            goal=_optional_float(reward_terms.get("goal")),
            process=_optional_float(reward_terms.get("process")),
            satisfaction=_optional_float(reward_terms.get("satisfaction")),
        )
    else:
        terms = RewardTerms()
    return TraceUnit(
        run_id=run_id,
        task_context_id=context,
        round_index=index,
        next_step=str(record.get("next_step") or ""),
        state_summary=_clip_text(str(record.get("task_state") or ""), limits.state_summary_chars),
        action=_clip_text(str(record.get("executor_output") or ""), limits.action_chars),
        observation=_clip_text(str(record.get("harness_feedback") or ""), limits.observation_chars),
        reflection=_clip_text(str(record.get("auditor_report") or ""), limits.reflection_chars),
        alpha=alpha,
        value=value,
        reward_terms=terms,
        evidence_id=evidence_id,
        domain_tags=limits.cap_domain_tags(domain_tags),
        tool_names=limits.cap_tool_names(tool_names),
        error_signature=error_signature,
        workspace=workspace,
        repo=repo,
        branch=branch,
        roles=dict(roles or {}),
        occurred_at=occurred_at,
    )


def _dataclass_fields(record: Any) -> dict[str, Any]:
    if hasattr(record, "__dataclass_fields__"):
        return {name: getattr(record, name) for name in record.__dataclass_fields__}
    return {}


def _clean_tuple(values: Sequence[str], limit: int) -> tuple[str, ...]:
    cleaned: list[str] = []
    for value in values or ():
        text = str(value or "").strip()
        if text:
            cleaned.append(text)
        if len(cleaned) >= max(0, int(limit)):
            break
    return tuple(cleaned)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _role_trace_from_mapping(name: str, value: Any) -> RoleTrace:
    mapping = value if isinstance(value, Mapping) else {}
    return RoleTrace(
        role=name,
        agent=mapping.get("agent"),
        model=mapping.get("model"),
        route_tier=mapping.get("route_tier"),
        route_rationale=mapping.get("route_rationale"),
    )


def _compact(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None or an empty string (absent, not empty)."""
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, str) and not value:
            continue
        if isinstance(value, Mapping):
            nested = _compact(value)
            if nested:
                result[key] = nested
            continue
        result[key] = value
    return result
