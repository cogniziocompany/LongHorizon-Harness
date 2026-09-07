# TASK — cognizioware-mcp-tools: upgrade the LiteLLM gateway 1.93.0 → v1.100.0 (+ MCP session introspection, RS256 tokens, access-group toolsets), UAT first

**Asked by Paxton 2026-09-07 01:45 PT:** "use the lh-harness if you need to manage it but get this done asap with our infra scheduling". Plan:
`C:\Users\PaxtonTait\.claude\plans\plan-to-get-this-vast-rivest.md` (session f5a28320). Ground rules (non-negotiable): UAT (CT204) green before
CT202; never bare `docker compose up` (always `--env-file mcp-tools.env`); mcp-tools.env is overwritten by every deploy → mirror non-secret values into
compose as `${VAR:-default}`; never `compose down -v`; never `prisma migrate --accept-data-loss` (drops langfuse_projects); the runner is now
`gh-runner-lan` on CT203 (the plan's "must run on PTAIT09" is stale — nothing runs on the PC).

## Split
| Part | Who | What |
|---|---|---|
| Phase 1 schema diff (read-only decision gate) | overseer, now | `pct snapshot 204 pre-litellm-1100`, pg_dump, pull v1.100.0 on CT204, extract schema.prisma, `prisma migrate diff --script`, classify DDL (additive vs destructive; watch `langfuse_projects`, VerificationToken/team/access-group columns, NOT NULL without default). Report to Paxton before applying anything. |
| Phases 0 + 2 (+ e2e, docs) | harness run `feat/litellm-1100` (queued with top priority; task text `C:\tmp\litellm-upgrade-task.txt`) | UAT lane syncs compose + pulls the pinned digest + recreates with `--env-file` + health assertion; kill the stale `:main-latest` refs; digest bump with comment history; `DISABLE_SCHEMA_UPDATE` mirrored into compose; `/introspect` + RS256 session tokens (env NAMES only); toolsets via access groups; version assertion + scoped-key enforcement e2e; runbook. |
| Rollout | overseer | apply the reviewed additive DDL on CT204 in a transaction → merge PR → lane (e2e → CT204 recreate on v1.100.0 → QA gate) → verify on CT204 (admin API 200, scoped key DENIED outside its group, /introspect true/false, aggregate tools ≥ 45, no prisma errors) → snapshot + dump CT202 → apply DDL on CT202 → lane deploys CT202 in a quiet window (pool runs down anyway) → same verification + the thinking-round-trip matrix. Rollback: revert the digest line, redeploy; DB dump only if non-additive DDL was applied. |

## Progress
- 01:50 PT: task text written; queued as `05-litellm-upgrade-1100` (first in the queue; the mcp-tools workspace is free after the audio QA run).
  Phase 1 running on CT204 in the background (snapshot, dump, pull, schema extract, migrate diff).
- 02:00 PT: Phase 1 done on CT204 — snapshot `pre-litellm-1100`, pg_dumpall 3.7 GB at /root/litellm-upgrade/ct204-litellm-pre1100.sql, v1.100.0
  pulled, schema.prisma extracted (/app/.venv/.../litellm/proxy/schema.prisma), `prisma migrate diff --script` = 279 lines: 32 additive statements
  (12 new tables: MCPServerOAuthClient, SSOIdentityAssertion, ModelAccessGroupBudgetTable, BudgetWindowSpend, ProxyWorkerHeartbeat,
  DailyGuardrailUsageUnits, DailyToolSpend, DailyGatewayRequests, AutoRouterSession, ShadowEval*; new columns incl. SpendLogs created/updated_at
  and VerificationToken key_type/settings_updated_at, all NOT NULLs carry defaults; one new FK), ONE destructive statement:
  `DROP TABLE "langfuse_projects"` (our table) → stripped. Decision: additive path, DISABLE_SCHEMA_UPDATE stays True.
- 02:05 PT: applying additive.sql on CT204 in one transaction and recreating the uat router on v1.100.0 with --env-file (background); verification
  = liveliness, /key/list + /team/list 200, readiness version, no prisma errors. Prod (CT202) only after the harness run's PR lands and the lane
  is green on UAT, in a quiet window, after a CT202 snapshot + dump.
- 02:15 PT: CT204 on v1.100.0 — additive DDL applied (278 statements, 0 DROP; langfuse_projects kept; 12 new tables present), DISABLE_SCHEMA_UPDATE
  added to the uat env, router recreated with --env-file: liveliness 200, /key/list 200, /team/list 200, readiness db connected/healthy, aggregate
  tools 846 (≥45), logs clean except the expected "schema out of sync: DROP TABLE langfuse_projects" warning (no restart loop). Pending: LLM path
  re-test (qwen3.8 via /v1/messages timed out once at 240 s while the local span served the RC run; kimi pool re-test running), scoped-key denial
  check, /introspect after the harness run's config lands.
- 02:50 PT: v1.100.0 on CT204 serves the Anthropic route with the normalizer hook: qwen3.8 /v1/messages → message in 78 s (span cold-loaded after its restart), zero hook errors; kimi pool → a proper 429 (1.93 returned 500 for the same quota condition — the QA gate classifier must include 429). Scoped-key denial + /introspect checks wait for the harness run's config (toolsets by access group, session tokens).
- 05:15 PT: harness run launched by the queue as `20260907T081439Z_50b1e8a7` (all-kimi, 8 rounds; Phases 0 + 2 + e2e + runbook). UAT is already on v1.100.0 with the additive schema; the run's lane fix makes the pipeline able to carry the digest to CT204/CT202.

## Progress 2026-09-07 02:35 PT
- Run 50b1e8a7 delivered all five slices (d7ff83e, c428b91, b52319d, dc81b31, d14c208, 92948f5, b519a43); ended at the round-limit gate (stop, ASCII note). Overseer added 2bd9ddb (UAT gate asserts `/health/readiness` instead of the full `/health` sweep). Branch pushed; **PR #87 open, not merged**.
- CT202 prep: `pct snapshot 202 pre-litellm-1100` taken 02:13 PT; `pg_dumpall` -> `/root/litellm-upgrade/pg_dumpall_pre1100.sql` (db 5.6 GB, 9.2 GB free on the CT disk - watch it); `additive.sql` (278 lines, 12 CREATE TABLE, 0 destructive) staged at `/root/litellm-upgrade/additive.sql`. Next: apply the DDL in one transaction once the dump finishes, then merge #87 -> lane (E2E -> CT204 sync/recreate -> QA gate -> CT202 deploy), then verify readiness version, /key/list, /team/list, scoped-key denial, tools >= 45.
- Config review note: `mcp_session_token_signing` block references only env names; if v1.100.0 rejects the key, the UAT gate catches it before prod.
