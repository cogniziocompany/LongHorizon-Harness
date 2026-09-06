# TASK — n8n → code (LangGraph JS) orchestration: Step 0, land the canonical spec (docs only)

**Owner session:** Claude Code `62e850d6-2a00-4933-a033-b41c3b3e4376` (cwd `C:\Users\PaxtonTait\source\cognizioware-powerplatform`, branch ci/env-promotion).
**Overseer role:** manager — created the worktree, launched and monitors the run, reviews, and gives the owner the go to push/PR.
**Harness run:** `20260906T013415Z_c1538905` (WSL workbench, 2026-09-06 01:34 PT, `:pool` trio, 14 rounds).
**Authoritative spec (read FIRST):** `C:\Users\PaxtonTait\.claude\plans\we-want-to-swithc-wild-crab.md`
(WSL `/mnt/c/Users/PaxtonTait/.claude/plans/we-want-to-swithc-wild-crab.md`) — Step 0 is committed by the run itself as
`design/orchestrator/orchestration-migration-audit-2026-09-05.md` + the handoff v2.
**Harness workspace (WSL-created, LOCKED):** `/mnt/c/Users/PaxtonTait/source/cognizioware-powerplatform-orch` — branch `docs/orchestrator-reconciled-v2`
from `origin/develop` @ e119e98 (develop already contains ci/env-promotion, so "on ci/env-promotion" in the plan is satisfied by targeting develop).
**Design inputs (commits reachable in the repo):** feat/code-first-orchestrator 50e98ff 1fa3cb4 6d2ec44; design/n8n-code-orchestration-20260905 42a532b 6bc6729.
**Scope of this task:** Step 0 only. Phases A–G/E/F are separate tasks, each gated on the previous phase's exit criteria; the harness/eval lane
work (Phase B) must keep `EVAL_ENGINE=n8n` green while `langgraph` comes up, and the promotion lane (promote.yml) is the cutover vehicle in F.

## Model trio
manager `kimi-k2.7-code:pool` · executor `kimi-k2.7-code:pool` · auditor `kimi-k3:pool` (API keys `roles` + `max_rounds`).

## Task text
See `scratchpad/orch-task.txt` in the overseer session (pasted verbatim into the run): cherry-pick the three code-first commits, checkout the
6bc6729 handoff directory (no .gitignore/kb-hook changes), write the migration audit from the plan appendix, revise the handoff to v2 with every
section the plan lists (Decision/Supersedes, §2 baseline + n8n scope table, §2.4 coupling inventory, §4 G6/G7, §6, §7, §9, §10, §12 Phases,
§13, Appendix A pins, separation table), add the two pointer paragraphs, verify `git diff --stat` touches only `design/**` and
`node tests/ci/workflow-guards.mjs` passes. Docs only; explicit-path commits; no push.

## After completion (post-processes)
1. Overseer: review the diff (design/** only), the handoff v2 sections, and the audit; confirm workflow-guards output.
2. Owner 62e850d6 (on go): push `docs/orchestrator-reconciled-v2`, open PR → develop titled
   `docs(orchestrator): reconcile code-first handoff v2 with TRMS delivery contract and migration audit`; docs-only, so the dev-lane rollout is a no-op.
3. Then plan the Phase A task (verify and pin: live n8n inventory, gateway ACL/fallback tests, TRMS baseline decisions with Paxton, package pins)
   as its own task file; Phase A has user-decision inputs (baseline-manifest dispositions) that need Paxton before launch.

## Completed (2026-09-06 03:00 PT)
Run c1538905 done in 4 rounds, three clean audits: 968e802 0bad583 (cherry-picks/6bc6729 dir) · fed0a59 audit · d895341 handoff v2 · a965d7b summary; 10 files +3435, design/** only, all listed sections present. Owner 62e850d6 given GO to push + PR → develop, then draft the Phase A task (needs Paxton decisions).
