"""MSCE experience layer, Phase 1: valued L1 trace persistence.

Phase 1 implements the paper's L1 trace memory for one harness run: every
round becomes a ``TraceUnit`` carrying a bounded (and later redacted) record
of what the roles did, a deterministic terminal reward split into
goal/process/satisfaction terms, a reflection weight derived from the
independent AuditReport, and a value backfilled with Eq. 2.

Slice 2 adds the pre-write choke points: ``redact`` strips credentials from
every text field before persistence (MSCE B.11 — the sink is shared), and
``tags`` derives the run's joinable identity (repo, branch, executor surface,
real tool names, error signature, per-role model trio) from the run's own
artifacts, read-only.

Explicitly out of scope for Phase 1 (``tasks/msce-experience-layer-2026-09-07.md``):
L2 induction, policy gain, L3 abstraction beyond the seeded defaults,
skill crystallization, retrieval into prompts, and direct memory-mcp writes.

This package must stay free of LLM calls and network access; everything here
is a pure function of data the run already produced.
"""

from __future__ import annotations

from .backfill import DEFAULT_GAMMA, backfill
from .redact import REDACTED, SECRET_ENV_VAR_NAMES, redact_text, redact_trace, redact_value
from .reflection import ALPHA_HIGH, ALPHA_LOW, ALPHA_MID, alpha
from .tags import (
    detect_branch,
    error_signature,
    next_step_tag,
    repo_from_owner,
    roles_from_owner,
    tool_names_from_round_dir,
)
from .reward import (
    WEIGHT_GOAL,
    WEIGHT_PROCESS,
    WEIGHT_SATISFACTION,
    RewardBreakdown,
    reward_breakdown,
    terminal_reward,
)
from .trace import (
    DEVICE_FIELD_NAMES,
    ROUTE_RATIONALE_MAX_CHARS,
    RewardTerms,
    RoleTrace,
    TraceCaps,
    TraceUnit,
    task_context_id,
    trace_unit_from_round,
)

__all__ = [
    "ALPHA_HIGH",
    "ALPHA_LOW",
    "ALPHA_MID",
    "DEFAULT_GAMMA",
    "DEVICE_FIELD_NAMES",
    "REDACTED",
    "ROUTE_RATIONALE_MAX_CHARS",
    "RewardBreakdown",
    "RewardTerms",
    "RoleTrace",
    "SECRET_ENV_VAR_NAMES",
    "TraceCaps",
    "TraceUnit",
    "WEIGHT_GOAL",
    "WEIGHT_PROCESS",
    "WEIGHT_SATISFACTION",
    "alpha",
    "backfill",
    "detect_branch",
    "error_signature",
    "next_step_tag",
    "redact_text",
    "redact_trace",
    "redact_value",
    "repo_from_owner",
    "reward_breakdown",
    "roles_from_owner",
    "task_context_id",
    "terminal_reward",
    "tool_names_from_round_dir",
    "trace_unit_from_round",
]
