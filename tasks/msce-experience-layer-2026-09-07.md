# MSCE experience layer for LongHorizon-Harness (2026-09-07)

**Source:** planning session 173fa8a3 (Claude Code, PTAIT desk), plan file `C:\Users\PaxtonTait\.claude\plans\where-is-this-repo-sequential-moth.md`; paper `docs/handoffs/From Memory to Skills Evidence-Grounded2607.16621v1.pdf` (arXiv 2607.16621, committed with this file). Owner decision: deliverable = assessment + build Phase 1. Resume the planner for design questions: `claude --resume 173fa8a3-0591-4ef9-b9df-89d2e5a72d40 -p "<directive>"` from this repo.

## Trigger
Assessment verdict: the harness is a best-in-class within-run runtime (plan → act → verify → checkpoint with an independent auditor) and has none of MSCE's between-run layer. Today it sits at the paper's worst ablation: Vanilla Agent (memory MCP unused) or Flat Memory (hivemind ingests unvalued snapshots). Three findings drive the build: the L1 evidence already exists on disk per round and is discarded; the doctrine loop (run → lesson → `template:` commit) is MSCE run by hand at human speed; the auditor verdict lets α and R be computed deterministically from verified fields, stronger than the paper's self-reflection.

## Scorecard (summary)
L1 traces: substrate exists, unused · reflection ρ: auditor is better than the paper's · terminal R: present, categorical, discarded · value backfill: absent · L2 policies: real but hand-written (`TEMPLATE-overseer-hierarchy.md`) · gain/quorum: absent · L3: manual (`cognizioware-how-to.md`, memory files) · crystallization: absent (skills are hand-authored MCP prompts) · verification of promoted knowledge: absent (only run output is verified) · reliability/lifecycle: anecdotal · hierarchical retrieval: absent · a+/a- guidance: manual · failure burst: detected (`dashboard/rules.py` repeated_failure), not distilled · storage/privacy caps: present.

## Phase 1 scope (two harness tasks)
1. **This repo, task `16a-msce-experience-layer`** (workspace `/home/harness/work/LongHorizon-Harness`, kimi trio, 8 rounds, text `C:\tmp\msce-experience-task.txt`): package `src/lh_harness/experience/` (trace, reward, reflection, backfill, redact, tags, store, capture) wired once in `manager._run_impl` after `_final_report`, inside a never-fatal try/except. Writes ONE file, `role_orchestration/experience.jsonl`, in the run dir (a volume on every node; already walked by the ingester). Off by default (`LH_HARNESS_EXPERIENCE=1` / `[run] experience = true`). Reward = 0.45·goal + 0.30·process + 0.25·satisfaction with terms recorded separately; `user_cancelled` from the web = no signal. α deterministic from the AuditReport. Backfill Eq. 2 with γ = 0.9 (Table 5: α 0.7, R 0.8 ⇒ 0.776). `task_context_id` inherits across "successor to <run_id>" chains (B.2). Redaction before every write (B.11) because the sink is shared.
2. **cognizioware-hydra-fleet, task `16b-hivemind-experience-ingest`** (workspace `/home/harness/work/cognizioware-hydra-rsi`, kimi trio, 5 rounds, text `C:\tmp\msce-ingest-task.txt`): `scripts/hivemind-ingest.mjs` reads `experience.jsonl`, posts each trace as `remember_session` (source lh-harness, tier run) with R/V/α/terms/tags/task_context_id in `session_memories.metadata` — no CT103 schema change; existing content_hash dedupe. Plus a valued-recall helper for the first retrieval experiment (recall by repo with metadata.V > 0.5, by hand, before any prompt-slot work).

## Hard constraints
Never write inside a workspace (kb-hook.log voided every audit of run 13ccf895) · honour `LH_HARNESS_CLAUDE_ROLE` / `KB_HOOK_DISABLE` · default off, byte-identical runs when off · no network at finalization · no new LLM calls · POSIX tests only (108 win32 failures on record) · CT110 venv is root-owned: redeploy as root from `/home/harness/release-src`, check the import path before any harness-dev run · Docker node image rebuilt on corsairai300 after merge (no source bind mount).

## After completion (overseer)
Task 1: review → PR → merge → CT110 release deploy as root in an idle window (pause launcher) → flag-off run unchanged → flag-on 3-round run: one record per round, values consistent with the verdict, integrity clean every round → deliberately failed run yields negative values → rebuild the Hydra node image. Task 2: PR → merge → pull on the corsairai300 cron checkout → one manual ingest → `recall` experiment; record the result here.

## Explicitly not in Phase 1
L2 induction, policy gain, L3 abstraction, skill crystallization, retrieval into prompts, direct memory-mcp writes from the harness. Expect the gain to be cost and rediscovery reduction across runs, not +15 Pass@1 on an already-audited loop.

## Progress
- 2026-09-07 18:40 PT: task file created; queue entries 16a/16b added; PDF committed.
- 2026-09-08 (routing, task 16a runs after task 49 on this workspace): the run owner will carry `route` (see `docs/handoffs/dynamic-model-routing-handoff-2026-09-08.md`, "Interfaces other tasks read"). The L1 trace's agent/model per role must read `owner.route.bound.roles` when present and fall back to `owner.role_configs`; add optional `route_tier` per role and a redacted `route_rationale` (≤ 512 chars). The seeded L3 environment lists routing backends by NAME (`local-span`, `synthetic`, `ollama-cloud`) with their `max_concurrent`, never URLs. No import of `model_routing`; read the JSON.

### 2026-09-08 09:45 PT — the three levels exist per instance, seeded for now, and must be viewable
Paxton: every lh-harness instance carries all three MSCE levels from the start; we hardcode them for now and learn later; and the web UX must make
them accessible.
- **Task 16a (queued)** gained: a seeded-defaults module plus an optional `[experience]` override in the instance's own `.lh-harness/config.toml`.
  **L3 is genuinely populated now** — the fleet as data, not comments: which hosts exist and what each is for, what a head on each can run, and the
  standing constraints that already govern us (no host-level change without an explicit go; restart a runner from outside it; no e2e or QA on a dev
  box; prod goes through the lanes and gates decide; some hosts have no IPv6 egress; a lane compose resolving a stale image tag silently rolls a
  service back). **L2** starts empty but as a real addressable collection with its final shape. **L1** is per run, already specified. Every seeded
  item carries `origin: seeded|learned` and `seeded_at`, and a learned item supersedes a seeded one without erasing the record of what was seeded.
- **Read-only API** so a UI can render them: `GET /api/experience/environment`, `GET /api/experience/policies`, and
  `GET /api/experience/runs/{run_id}/traces` — same bearer boundary, paginated, redacted, stable ids. No write endpoints in this task.
- **Task 46 (Hydra, queued)** gained slice 9: per-node Environment disclosure (collapsed, origin badges), a Policies list with an honest empty
  state, and a per-run Traces view in the cockpit including the device/head/terminal and Hydra node where an execution happened.
- **Task 45 (window, running)** received the same read-only surfaces as an instruction; **task 44 (UX gate, queued)** now asserts they render, that
  the empty state is honest, that a 404 shows one quiet line rather than an empty list implying empty memory, and that ids and origin badges survive.
Redaction is unchanged and binding: the API must never return a value redaction would have stripped from a trace.
