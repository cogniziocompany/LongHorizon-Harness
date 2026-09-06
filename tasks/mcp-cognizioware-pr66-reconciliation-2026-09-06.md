# TASK — mcp-cognizioware: PR #66 reconciliation + admin UI loading states

**Owner session:** Claude Code `cec370bb-b46d-4605-9cb3-451a6db6d365` (cwd `c:\Users\PaxtonTait\source\mcp-cognizioware`, checked out on
`docs/youtrack-gap-analysis` — an older lineage; never diff against that working tree, always against `origin/develop`).
**Overseer role:** manager — worktree, run, review, Fix 1c (close #66) and Fix 2 (prod rebuild from develop) are overseer steps.
**Harness run:** `20260906T014303Z_1eeb4daf` (WSL workbench, 2026-09-06 01:43 PT, `:pool` trio, 14 rounds).
**Authoritative spec (read FIRST):** `C:\Users\PaxtonTait\.claude\plans\ut-the-tree-does-hazy-fox.md`
(WSL `/mnt/c/Users/PaxtonTait/.claude/plans/ut-the-tree-does-hazy-fox.md`).
**Harness workspace (WSL-created, LOCKED):** `/mnt/c/Users/PaxtonTait/source/mcp-cognizioware-recon` — branch `fix/prod-compose-env` from
`origin/develop` @ 8946eea3; the run creates `fix/admin-loading-states` from origin/develop in the same worktree for Fix 3.

## Why this supersedes the overseer's earlier PR #66
The CT100 prod tree that #66 carries was a STALE lineage. Measured against origin/develop: StripeService/IStripeService/ApplicationDbContext/
admin index.html byte-identical; net diff +1.1M lines of resurrected root cruft; `src/` net −7214 (would delete AccountTeardownService,
OnboardingProvisioningWorker, OnboardingSecurityService, ProvisioningClients, TokenAllowanceFlushWorker, OnboardingModels, three EF migrations,
the StripeWebhookController user-org linkage and `Configure<PartnerSettings>`). Only real content: prod compose/env wiring + one DTO.
**Do not merge #66.**

## Fix 2 verification (overseer, 2026-09-06 01:45 PT, read-only)
- `GET /api/subscription/status` on prod returns **503** with the LINDY secret and with a bad key → `PartnerSettings` unbound in the running image.
- Running image has none of develop's onboarding/teardown services (84 dlls, none matching).
- Box compose wires `Partner__ApiKey=${LINDY_WEBHOOK_SECRET}` (must become `${PARTNER_API_KEY}`; set that var on the box at redeploy).
→ Correct fix per plan: after Fix 1a merges, rebuild prod FROM DEVELOP source (not the box branch), `--no-deps`, postgres untouched, then verify:
  billing healthy, auth-config 5651b092… GUID scope, partner endpoint 401 bad key / 200 real key, Infra button present.

## Model trio
manager `kimi-k2.7-code:pool` · executor `kimi-k2.7-code:pool` · auditor `kimi-k3:pool` (API keys `roles` + `max_rounds`).

## Task text
`tasks/mcp-cognizioware-pr66-reconciliation-2026-09-06.task.txt` (verbatim). Fix 1a config-only branch; Fix 1b = do not add the DTO, record the
decision in a handoff doc; Fix 3 admin loading states on a second branch; no Program.cs/StripeService/Webhook/Migrations/.github changes; no
secrets; flag tracked `cloudflare/credentials.json`; no push; no PR #66 actions; no CT100 actions.

## After completion (post-processes)
1. Overseer review: `git diff --stat origin/develop..fix/prod-compose-env -- src/` empty; compose validates; loading-states diff limited to index.html.
2. Owner cec370bb (on go): push both branches; PRs → develop (`fix(prod-compose)…`, `feat(admin): loading states…`); browser check of Fix 3
   at Slow 3G per the plan; merge order 1a then 3.
3. Overseer Fix 1c: close PR #66 with the measured reasons and links to the replacement PRs; keep the branch.
4. Overseer Fix 2: after 1a merges → release fast-forward (uat), then prod rebuild from develop on CT100 with `PARTNER_API_KEY` set; verify; then
   the QA billing gates (uat, prod) must stay 100.

## Completed (2026-09-06 03:00 PT)
Run 1eeb4daf done in 4 rounds (one early audit violation, then clean): fix/prod-compose-env @ a54d29dd (compose + cloudflare/prod.config.yaml + docs/handoffs/pr66-reconciliation-2026-09-06.md; src/ diff empty) and fix/admin-loading-states @ 54632ed1 (index.html only). Owner cec370bb given GO (push, two PRs, Slow-3G check). Fix 1c DONE: #66 closed with measured reasons. Fix 2 pending the compose PR merge → prod rebuild from develop with PARTNER_API_KEY.
