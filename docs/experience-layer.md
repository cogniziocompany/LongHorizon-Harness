# Experience layer (MSCE Phase 1)

LongHorizon-Harness can persist **valued L1 traces** for every run: one bounded,
redacted record per managed round that carries what the roles did, a
deterministic terminal reward split into goal/process/satisfaction terms, a
reflection weight derived from the independent AuditReport, and a value
backfilled with Eq. 2 of *From Memory to Skills* (arXiv 2607.16621). The layer
is **off by default**; when it is off, runs are byte-identical to a build
without it.

This is Phase 1 only — valued L1 trace persistence. See
[What is not in Phase 1](#what-is-not-in-phase-1) below.

## Enabling the experience layer

Set the environment variable **or** the config flag on the instance:

| Knob | How to enable | Notes |
| --- | --- | --- |
| `LH_HARNESS_EXPERIENCE` | any truthy value (`1`, `true`, `yes`, `on`) | Wins over config **in both directions** when set to a non-empty value, so a release window can force the layer off without editing instance config. |
| `[run] experience = true` | in the instance's `.lh-harness/config.toml` | Validated by the same boolean validator as the other `[run]` toggles. |

When neither is set the layer is never touched: no extra files, no extra
events, and `report.json` stays byte-identical. Capture runs once at run
finalization, immediately after the final report is built and written, inside a
never-fatal try/except — a capture failure is logged as an
`experience_capture_failed` control-bus event and the run's own output is
unaffected. No network, no LLM calls, no prompt or round-flow changes.

## What is captured

One JSONL record per managed round, built **only** from data the run already
produced (round records, the final report, the approval ledger, operator
control-bus commands, `control/owner.json`, and the normalized per-round
trajectories):

| Field | Source | Meaning |
| --- | --- | --- |
| `run_id` / `round_index` / `evidence_id` | run ledger | The record's identity; also the dedupe key. |
| `task_context_id` | task text | `successor to <run_id>` on the first task-text line inherits that run's context (MSCE B.2 multi-episode chains); otherwise the run's own id. |
| `state_summary` / `action` / `observation` / `reflection` | round record | The manager's task state, the executor's output, the harness's feedback, and the auditor's report — each hard-capped (head+tail clipping) to the same budgets `HarnessConfig` uses for the roles. |
| `alpha` | the round's AuditReport | Reflection weight in {0.7, 0.5, 0.3} (see below). |
| `value` | Eq. 2 backfill | The round's step value `V` once the terminal reward is known. |
| `reward_terms` | final report + ledgers | The three recorded reward components `{goal, process, satisfaction}` (see below); absent fields on a no-signal run, with `reward_reason` naming why. |
| `next_step`, `domain_tags` | round record + owner | `gui`/`cli` step tag plus compact joinable labels (`repo:…`, `branch:…`, `next_step:…`). |
| `tool_names` | normalized `{role}_trajectory.jsonl` | Real `tool_use` names in first-appearance order. |
| `error_signature` | auditor report / abort reason | First `blockers[]` line, else a run-level `provider_*` abort reason. |
| `workspace`, `repo`, `branch` | `control/owner.json` | Repo is the workspace basename; branch comes from the task text or a **read-only** probe of a repo copy *inside the run dir* — never the live workspace. |
| `roles` | `control/owner.json` | Per-role `{agent, model, route_tier, route_rationale}` from `owner.route.bound.roles` when the run was routed, else `owner.role_configs`; the rationale is capped (≤ 512 chars) and redacted. |
| `occurred_at` | capture time | UTC ISO timestamp the same for all rounds of one capture. |
| `device_id`, `terminal_id`, `hydra_node` | *(absent in Phase 1)* | The Hydra device-fleet dimension, using the identical ids Hydra and the fleet surfaces use. **Not populated today**: the round data the harness records names tool calls, not the device a call executed on. These fields stay absent (not `""`) until the harness records the exec binding per role episode; nothing invents a value. |

Fields the run did not record are omitted from the output entirely, so a
missing dimension can never be mistaken for a recorded one.

## The value formula

The terminal reward is deterministic (no LLM call) and in **[-1, 1]**:

```
R = 0.45 * goal + 0.30 * process + 0.25 * satisfaction
```

- **goal** (0.45) — did the run achieve the task: final report status and
  `completion_satisfied`, the abort reason (`needs_human_input`/`human_abort`
  are mildly positive, `max_rounds_exhausted` and `provider_*` negative), and
  the last non-synthetic auditor verdict triple (integrity violation −0.25,
  invalid contract −0.25, …).
- **process** (0.30) — how the work was carried out: budget usage
  (`rounds_run / max_rounds`), −0.25 per timed-out role episode, −0.15 per
  round the harness spent repairing a malformed audit
  (`invalid_plan`/`invalid_completion`).
- **satisfaction** (0.25) — what the operator signalled: approval ledger
  resolutions (`approve`/`continue` +0.5, `deny`/`reject`/`abort` −1.0,
  expired −0.25), a small keyword nudge from their rationale text, and a small
  penalty per operator steering message.

**No-signal rule:** a `user_cancelled` abort attributed to the web control
plane (`created_by` `web` on the report or the cancel command) carries no
signal — the reward and all three terms are `None`, the record carries
`reward_reason: "no_signal:user_cancelled_web"`, and no values are backfilled.
Cancellations the records cannot attribute to the web keep a computed value;
the layer never invents provenance.

**Reflection weight α** (per round, from its own AuditReport):

- `0.7` (`ALPHA_HIGH`) — clean parseable verdict, integrity clean, contract
  aligned, not blocked.
- `0.5` (`ALPHA_MID`) — parseable verdict that could not fully verify the
  round (suspect integrity, unknown/needs-revision contract, blocked audit).
- `0.3` (`ALPHA_LOW`) — reflections that must not be trusted: synthetic
  format-repair feedback, timed-out episodes, integrity violations, invalid
  contract audits, or rounds with no auditor text at all.

**Value backfill (Eq. 2), γ = 0.9:**

```
V_t = α_t * R + (1 − α_t) * γ * V_{t+1},    V_terminal = R
```

The terminal round takes R exactly; earlier rounds blend R against the value of
the round that followed, so a trustworthy reflection lets a round keep more of
the terminal signal. α 0.7 and R 0.8 reproduce the paper's Table 5 value
0.776 exactly; this is covered by a unit test.

## Where it is written, and why the run dir

Exactly one file per run:

```
<run_dir>/logs/role_orchestration/experience.jsonl     # append-only JSONL
```

(within the standard supervisor layout, `<run_dir>/lh_harness/role_orchestration/`).

The run dir is the deliberate choice:

- **The hydra-fleet ingester already walks run dirs.** The ingester carries
  each trace into `hivemind_sessions.session_memories.metadata` (with R/V/α,
  the terms, tags, and `task_context_id`) — **no CT103 schema change**, and its
  existing content-hash dedupe does the rest. There is intentionally **no
  state-root store** and no second sink.
- **Never inside the workspace.** The workspace is what the task's agents
  write; the experience sink must not be reachable to them. Every write uses
  the anchored no-follow open pattern from the control bus, so a swapped
  symlink in the ledger path fails closed.
- **Append-only and bounded.** Lines are never rewritten; dedupe is on
  `(run_id, round_index)` plus a sha256 `content_hash`, so a double capture
  yields one record set. A single record above 512 KiB is dropped, and the
  file stops growing at 64 MiB (excess is counted, not spooled elsewhere).
  Malformed or truncated ledger tails are skipped, never fatal.

## Redaction

Every text field passes through rule-based redaction (MSCE B.11 — the sink is
shared) **before** the record is written: bearer credentials, `sk-*` keys,
GitHub token families, PEM private-key blocks, `password=` assignments,
unbroken hex ≥ 32 chars, and values of known credential env-var names are
masked with `***REDACTED***`. Hostnames, repo names, and device ids are fine,
tokens and keys are not. Redaction is deterministic (regex only), idempotent,
and applies to error signatures and route rationales too — a blocker or
rationale can quote a secret the auditor saw.

## What is not in Phase 1

Per `tasks/msce-experience-layer-2026-09-07.md`, none of the following is in
scope yet:

- **L2 policy induction** — generalizing recurring actions into policies, and
  the policy gain/quorum that decides promotion.
- **L3 environmental cognition** beyond the seeded defaults — learned fleet
  knowledge; L3 is seeded (hardcoded) for now and learned later.
- **Skill crystallization** — turning promoted policies into skills.
- **Retrieval into prompts** — no prompt slot, no recall on the generation
  path; nothing on the run path reads the ledger.
- **Direct memory-mcp writes** from the harness — traces reach hivemind only
  through the hydra-fleet ingester (task `16b-hivemind-experience-ingest`).
- **Device/head/terminal population** — the fields are declared for joinability
  but stay absent until the harness records the Hydra exec binding (device id,
  terminal/session id, Hydra node) on each role episode.

Expected gain from Phase 1 is cost and rediscovery reduction across runs, not
a Pass@1 bump on an already-audited loop.

## Reading a ledger record

A flag-on three-round run yields three lines, one per round. Each line is the
compacted trace (absent fields omitted); a reader can verify integrity by
checking that `round_index` values are contiguous, `value` decreases as you
walk away from the terminal round when α < 1, and the terminal round's `value`
equals the `reward_terms` combination
`round(0.45 · goal + 0.30 · process + 0.25 · satisfaction, 6)` clamped to
[-1, 1].
