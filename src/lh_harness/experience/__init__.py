"""MSCE experience layer, Phase 1: valued L1 trace persistence.

Phase 1 implements the paper's L1 trace memory for one harness run: every
round becomes a ``TraceUnit`` carrying a bounded (and later redacted) record
of what the roles did, a deterministic terminal reward split into
goal/process/satisfaction terms, a reflection weight derived from the
independent AuditReport, and a value backfilled with Eq. 2.

Explicitly out of scope for Phase 1 (``tasks/msce-experience-layer-2026-09-07.md``):
L2 induction, policy gain, L3 abstraction beyond the seeded defaults,
skill crystallization, retrieval into prompts, and direct memory-mcp writes.

This package must stay free of LLM calls and network access; everything here
is a pure function of data the run already produced.
"""

from __future__ import annotations

from .backfill import DEFAULT_GAMMA, backfill
from .reflection import ALPHA_HIGH, ALPHA_LOW, ALPHA_MID, alpha
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
    "ROUTE_RATIONALE_MAX_CHARS",
    "RewardBreakdown",
    "RewardTerms",
    "RoleTrace",
    "TraceCaps",
    "TraceUnit",
    "WEIGHT_GOAL",
    "WEIGHT_PROCESS",
    "WEIGHT_SATISFACTION",
    "alpha",
    "backfill",
    "reward_breakdown",
    "task_context_id",
    "terminal_reward",
    "trace_unit_from_round",
]
