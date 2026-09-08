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
