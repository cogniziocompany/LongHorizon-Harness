# Handoff: Paxton's decisions on "What is blocked — 2026-09-16 13:31 PT"

Written 2026-09-16 by session [fe2679] at Paxton's instruction; the decisions are HIS, relayed verbatim in substance.
ACK TOKEN for the bridge step: **PAX-DECISIONS-20260916-1331** — when a tick has read this file, write that token
into its ledger row and mark the OPEN-ASKS block below as consumed. That is how [fe2679] verifies the bridge works.

## Decisions, one per row of the report

1. **CT202 disk.** The row says both "I have not run it" and "both halves have now run" — restate it cleanly next tick.
   (a) Stop recording message bodies NOW: `store_prompts_in_spend_logs=false` + the 7-day retention (task 170; already
   decided under task 115 on 09-09). Apply as the DB setting, read it back. (b) The 25 GB reclaim (VACUUM FULL /
   pg_repack, whichever the table needs now that a mass delete created dead space) is APPROVED for the next zero-active
   window — the same window as 144 — after re-measuring disk margin. Not tonight. (c) The two-hourly export job stays
   ONLY until retention is proven live, then retires; the export family was deprecated 09-11 and must not become permanent.
2. **Synthetic.** No new spend. Decided 09-14: Synthetic CAP = 3, local lane carries the rest. Refresh the row: it still
   carries the 09-11 correction text and describes a two-slot fleet.
3. **Powerplatform setup defects.** Agreed as top of queue. Ship to develop, test on dev, then ONE promote to uat and
   prod per C:/tmp/PLAN-powerplatform-prod-chronology-2026-09-11.md. Confirm the Ruflo eval gate is green before the
   promote. Nothing needed from Paxton.
4. **Reviewers.** Correction 1: the workflow exists on FOUR repos (mcp-tools, mcp-cognizioware, LongHorizon-Harness,
   hydra), not two — re-measure with the contents API. Correction 2 / decision: do NOT install @claude as the primary
   reviewer on the remaining repos. The harness becomes the reviewer via the PR-review-gate plan
   (C:/tmp/PLAN-pr-review-gate-2026-09-15.md, tasks 185-188); @claude and Copilot are FALLBACK only, after two failed
   harness reviews. Re-scope tasks 130, 132, 149 accordingly. Holding the hydra reviewer merge for a quiet window: correct.
5. **65 review comments.** Nothing from Paxton. Continue.
6. **DNS test.** Fix the NAMING, not the check. Remove BOTH records (A and AAAA) in one edit, with a backup of the full
   local-DNS list first and a read-back of every other internal name afterwards. In a window, since the tool rewrites
   the whole list.
7. **Four abandoned PRs.** Close them, each with the reason attached.
8. **Local floor.** Recommendation accepted: local floor for manager and auditor only; executor fails fast.
9. **Old model tags.** `QA_LITELLM_MODELS = ornith-1.5:pool,kimi-k3:synthetic-anthropic` (one local, one Synthetic).
   Land PR #120 behind it.
10. **Eval environment.** Stays lowest. Nothing waits on it.

## Also from Paxton today (new work, queued by [fe2679] as task 182 — do not duplicate)
The overseer's communication surface moves off files and into a DB-backed web UX at fleet.easybutt0n.ai/overseer:
handoffs, asks/answers as a persisted chat-style session, tasks read/write, idempotent keys (repo, branch, overseer
session, lh-harness run ids, Langfuse trace ids), and migration of C:/tmp/queue + HANDOFF-*.md into the DB. Task text:
C:/tmp/overseer-web-ux-task.txt.
