# TASK — cognizioware-powerplatform: fix the `multistep/multi-webhook-crud` eval regression (prod eval gate 15/16)

**Why:** promotion run 16 (2026-09-06) deployed prod but the prod eval gate scored 15/16; the one red case is a real product regression (n8n QA verdict
FAIL after 3 attempts, exec 150601). Paxton's standing rule: prod promotions are auto-approved once the gates pass — this case is what keeps the gate red.
**Overseer role:** manager (no owner session for pp tonight). Task text `C:\tmp\webhook-crud-fix-task.txt` (investigate + fix in repo only; n8n read-only).
**Workspace:** CT110 `/home/harness/work/cognizioware-powerplatform-evalfix`, branch `fix/eval-multi-webhook-crud` rebased on origin/develop 476811f
(guest slice 1 + nav-archive + #76); untracked `.evalfix.env` (n8n read creds) and `.claude/settings.local.json` kept; budgets 600/900/1800.

## Progress
- 19:50 PT: launched as `20260907T014548Z_eeea214a` (all-kimi trio, 10 rounds). Earlier queued attempt cccbef69 died in the quota outage.
- After: review → PR → develop → dev lane → eval case re-run in the dev lane's eval suite → next promotion (pinned sha) clears the prod gate.
- 20:15 PT: 084159ae died in round 1 on the prax211 session limit (3 of 4 Ollama keys exhausted). Queued in the quota watcher's pending list; resumes automatically when ≥2 keys answer.
- 21:12 PT: promotion 34067791009 (guest slice 1) passed the PROD eval score gate at 100 — the multi-webhook-crud case did not fail this time, so the regression is intermittent rather than deterministic. Fix run stays queued (hardening + flake root cause), no longer blocking any gate.
- 00:05 PT (Sep 7): run 084159ae (resumed after quota) COMPLETE at round 9. Root cause: the eval's QA prompt allowed form-XML / App-tool
  verification of the option-set binding, which lags schema publication → intermittent `Agent stopped due to max iterations` → VERDICT FAIL
  (n8n executions cited in the diagnosis). Fix: catalog.mjs qaPrompt mandates the Dataverse Schema Tool checklist. 4 files +218/−1 (docs +
  one prompt line), no secrets, no workflow/n8n changes. Run closed via approval 97cdfe6c54ce; PR → develop opened. Proof = next pinned promotion.
- 00:15 PT: develop CI for d2e3784 green (dev lane deployed from ct210-pp). Pinned promotion of d2e3784 dispatched (promo-webhook.sh, prod auto-approval per the standing rule) — the eval gates on dev/uat/prod are the proof of the fix.
- 2026-09-07 13:20 PT: develop promotion 34094892708 re-run failed the dev primary eval gate again: multi-table-and-data-import and multi-webhook-crud both `build: NO_RESPONSE (1200s)`; verify skipped. Same signature as 07:39Z. n8n.easybutt0n.ai/healthz 200 and the dev backend health 200 at 13:22, so it is not a plain outage. This blocks every develop->prod promotion (incl. the guest slices), so 30-pp-eval-flakiness moved to 06b (launches next in the pp-evalfix workspace).
