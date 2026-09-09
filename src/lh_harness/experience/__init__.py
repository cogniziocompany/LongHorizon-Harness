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

Slice 3 persists the result: ``store`` writes one append-only
``role_orchestration/experience.jsonl`` inside the run dir (content-hash
dedupe on (run_id, round_index), size caps), and ``capture`` is the single
finalization entry point wired into ``manager._run_impl`` — off by default,
gated by ``LH_HARNESS_EXPERIENCE=1`` or ``[run] experience = true``.

Slice 5 seeds the levels every instance carries from the start (Paxton
2026-09-08): ``seed`` ships L3 (the device fleet as stable knowledge —
hosts, standing constraints, routing backends by name) as data, and L2 as
an empty but already addressable collection in its final shape, with an
optional ``[experience]`` override in the instance's ``.lh-harness/config.toml``.
L1 stays per run. Every seeded item carries ``origin``/``seeded_at`` and is
redacted before it leaves the module.

Explicitly out of scope for Phase 1 (``tasks/msce-experience-layer-2026-09-07.md``):
L2 induction, policy gain, L3 abstraction beyond the seeded defaults,
skill crystallization, retrieval into prompts, and direct memory-mcp writes.

This package must stay free of LLM calls and network access; everything here
is a pure function of data the run already produced.
"""

from __future__ import annotations

from .backfill import DEFAULT_GAMMA, backfill
from .capture import (
    ENV_FLAG,
    CaptureResult,
    experience_enabled,
    persist_run_experience,
    run_dir_for_log_dir,
)
from .redact import REDACTED, SECRET_ENV_VAR_NAMES, redact_text, redact_trace, redact_value
from .reflection import ALPHA_HIGH, ALPHA_LOW, ALPHA_MID, alpha
from .store import (
    EXPERIENCE_FILENAME,
    StoreStats,
    append_trace_records,
    content_hash,
    experience_ledger_path,
)
from .tags import (
    detect_branch,
    error_signature,
    next_step_tag,
    repo_from_owner,
    roles_from_owner,
    tool_names_from_round_dir,
)
from .seed import (
    ENVIRONMENT_KINDS,
    ORIGIN_LEARNED,
    ORIGIN_SEEDED,
    POLICY_ITEM_SCHEMA,
    SEEDED_AT,
    SUMMARY_MAX_CHARS,
    SeededLevels,
    empty_policy_collection,
    load_seeded_levels,
    supersede,
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
    "ENVIRONMENT_KINDS",
    "ENV_FLAG",
    "EXPERIENCE_FILENAME",
    "ORIGIN_LEARNED",
    "ORIGIN_SEEDED",
    "POLICY_ITEM_SCHEMA",
    "REDACTED",
    "ROUTE_RATIONALE_MAX_CHARS",
    "SEEDED_AT",
    "SUMMARY_MAX_CHARS",
    "CaptureResult",
    "RewardBreakdown",
    "RewardTerms",
    "RoleTrace",
    "SECRET_ENV_VAR_NAMES",
    "SeededLevels",
    "StoreStats",
    "TraceCaps",
    "TraceUnit",
    "WEIGHT_GOAL",
    "WEIGHT_PROCESS",
    "WEIGHT_SATISFACTION",
    "alpha",
    "append_trace_records",
    "backfill",
    "content_hash",
    "detect_branch",
    "empty_policy_collection",
    "error_signature",
    "experience_enabled",
    "experience_ledger_path",
    "load_seeded_levels",
    "next_step_tag",
    "persist_run_experience",
    "redact_text",
    "redact_trace",
    "redact_value",
    "repo_from_owner",
    "reward_breakdown",
    "roles_from_owner",
    "run_dir_for_log_dir",
    "supersede",
    "task_context_id",
    "terminal_reward",
    "tool_names_from_round_dir",
    "trace_unit_from_round",
]
