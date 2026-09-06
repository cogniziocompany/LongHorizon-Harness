# TASK — mcp-cognizioware: YouTrack integration Phase 1 (BillingService fast-option closure)

**Asked by Paxton 2026-09-05 23:05 PT** as part of: "create an lh-harness task for cognizioware-hydra … ship plan reporting … using mcp-cognizioware
youtrack integration so both ai agents and users can comment, tickets CRUD, record time sheets … tracked in appropriate places in youtrack … related
integration with dynamics … admin parts of the power platform customers go through mcp-cognizioware api … sync with our company's CRM sales
department for accounts and contacts with opportunities and leads … tie the timesheet youtrack integration from the other end of the business flow."
Split by the overseer into (a) this concrete build (the gap analysis already specifies it) and (b) the integration spec run
(`ship-plan-integration-spec-2026-09-05.md`).
**Authoritative spec:** `design docs/youtrack-integration-gap-analysis.md` (mcp-cognizioware branch docs/youtrack-gap-analysis @ 4a9824f0 — NOT on
develop; copied into the worktree untracked, committed by the run in slice 1). Sections 1.2, 2, 3 (gaps a–e, verdict "BillingService is the
backbone"), 6 Phase 1, 7, 8.2.
**Overseer role:** manager — worktree, run, review, PR → develop, dev lane → uat (release fast-forward) → prod rebuild (mcp-cognizioware lanes).
**Repo / branch:** cogniziocompany/mcp-cognizioware, `feat/youtrack-phase1` from origin/develop @ 880f3d9f.
**Harness workspace (WSL-created, LOCKED):** `/mnt/c/Users/PaxtonTait/source/mcp-cognizioware-youtrack`.
**Harness run:** `20260906T061801Z_fc60d514` (WSL, `:pool` trio, 14 rounds). **Task text:** `C:\tmp\youtrack-phase1-task.txt` (verbatim).

## Slices
1. Gap (a) author/config binding from configuration + readiness probe `GET /api/billing/youtrack/readiness`
2. Gap (b) comments + issue CRUD/query + work-item queries in YouTrackService, unit tests with fake handler
3. Gap (c) AI-vs-human hours: work-item type + optional custom field; legacy metadata blob behind a flag (default off)
4. Gap (d) real `youtrack:*` MCP tools (create/get/query/update issue, add/get comments, log_time, get_work_items) with agent attribution
5. Gap (e) `POST /api/youtrack/events` receiver (shared-secret Bearer, LindyWebhookAuth pattern), `youtrack_events` table via dual-write, kill switch `YOUTRACK_EVENTS_ENABLED` (default off → 503)
6. Verification + `design docs/youtrack-phase1-handback.md`

Scope locks: CE plugin untouched; D365CeService not used; Phase 0 rotation dropped per Paxton (no new secrets, config-only); no Stripe/billing meter changes.

## Model trio
manager `kimi-k2.7-code:pool` · executor `kimi-k2.7-code:pool` · auditor `kimi-k3:pool`.

## After completion (overseer)
1. Review: build/test evidence, no secrets, flag defaults, endpoint/tool table.
2. PR → develop, merge → dev lane; set `YouTrack__*` config on the dev box (CT100 dev :20252) from existing values; readiness probe green; then release fast-forward (uat) and prod rebuild with the gates at 100.
3. Hand the endpoint/tool table to the spec run's consumers (Hydra ship-plan module).

## Completed (2026-09-06 01:55 PT)
Run fc60d514 used all 14 rounds but delivered all six slices: 7194dde8 config-bound author + readiness · 5b90b060 comments/issue CRUD/work items ·
f41786a0 AI-hours convention (flagged) · df998bb4 real youtrack MCP tools · b431add9 events receiver + kill switch · f21f4a06 handback; 19 files +6900
(3 new test files: 1742 lines). Handback evidence: dotnet build 0 errors; tests 195 passed / 9 DataIntegrity need Postgres; sandbox integration NOT RUN.
Overseer check: hard-coded ai-dev01 default removed from YouTrackSettings/Program (config only). Round-limit gate resolved "stop". PR → develop: mcp-cognizioware #70.
Next: dev lane deploy → set YouTrack__DefaultUserEmail/DefaultProjectIssueId/ProjectId (+ AiWorkItemType, WebhookSecret) on CT100 dev, check
GET /api/billing/youtrack/readiness, then release fast-forward (uat) and prod rebuild with the billing QA gates at 100. RP-01 (Hydra events bridge) can now be launched.
Prod eval note (pp): multi-webhook-crud@prod fails QA after 3 attempts inside n8n (exec 150601, 17 min, "Respond Fail") — a product regression for the pp owner, not a runner issue.
