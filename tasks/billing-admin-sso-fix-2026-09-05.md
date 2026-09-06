# TASK — Fix Entra SSO sign-in on https://mcp-cognizioware.easybutt0n.ai/admin/ (AADSTS90009 / 50011 / 403 chain)

**Owner session:** Claude Code `e6483165-afce-4196-96aa-47a9c1f6627d` (cwd `c:\Users\PaxtonTait\source\mcp-cognizioware`).
It creates and monitors the harness run; the overseer schedules, runs the QA gate, and executes the deploy.
**Authoritative spec (read FIRST, follow exactly):** `C:\Users\PaxtonTait\.claude\plans\https-mcp-cognizioware-easybutt0n-ai-adm-toasty-nova.md`
(WSL `/mnt/c/Users/PaxtonTait/.claude/plans/https-mcp-cognizioware-easybutt0n-ai-adm-toasty-nova.md`).
**Harness workspace (dedicated worktree, WSL-form pointers):** `/mnt/c/Users/PaxtonTait/source/mcp-cognizioware-sso`
— branch `fix/admin-sso-spa-client-id` from `origin/develop` @ 44c4ccc2 (= the exact commit prod runs). The main checkout is on
`docs/youtrack-gap-analysis` with heavy unrelated changes — never build the fix there. Windows git shows "not a git repository" in the
worktree until the run ends; use WSL git.

## Model trio (cloud-ollama standard)
manager `glm-5.3:cloud` · executor `kimi-k2.7-code:cloud` · auditor `kimi-k3:cloud`

## Launch payload
```json
{"model":"kimi-k2.7-code:cloud","agent":"claude_code",
 "workspace":"/mnt/c/Users/PaxtonTait/source/mcp-cognizioware-sso",
 "roles":{"manager":{"agent":"claude_code","model":"glm-5.3:cloud"},
                 "executor":{"agent":"claude_code","model":"kimi-k2.7-code:cloud"},
                 "auditor":{"agent":"claude_code","model":"kimi-k3:cloud"}},
 "max_rounds":10,"task":"<task text below>"}
```
POST to `http://127.0.0.1:8799/api/runs` (WSL workbench), JSON via stdin `--data-binary @-`.

## Task text (paste verbatim, one slice per round)
BUILD-ONLY. Read the spec at the path above FIRST; work only in this worktree (branch fix/admin-sso-spa-client-id off origin/develop @ 44c4ccc2).
Root cause is settled (see spec Context): prod .env hands the admin SPA the YouTrack app id 240a59df (no SPA redirect, no app roles); the correct
registration is mcp-cognizioware-prod 5651b092-e9ac-4595-b112-083df3648633; and develop lacks main's df6f8adc (GUID-form scope). Implement spec
steps 1-5 exactly: (1) EntraIdSettings.cs: AdminSpaClientId + EffectiveAdminSpaClientId (fallback to ClientId so dev/uat lanes keep working);
(2) AdminUiController.cs auth-config: clientId = EffectiveAdminSpaClientId, apiScope = "{spaClientId}/.default" (GUID form), loginEnabled =
tenant + effective id present; (3) Program.cs AddJwtBearer ValidAudiences = distinct non-empty set of Audience, ClientId, api://{ClientId},
EffectiveAdminSpaClientId, api://{EffectiveAdminSpaClientId} (v1 tokens from sts.windows.net already covered by ValidIssuers; API-key bearer
paths unaffected); (4) compose wiring — one line `- EntraId__AdminSpaClientId=${ADMIN_SPA_CLIENT_ID:-}` next to EntraId__Audience in
docker-compose.prod.yml, docker-compose.dev.yml, docker-compose.uat.yml, infrastructure/docker-compose.yml, infrastructure/docker-compose.uat.yml,
infrastructure/docker-compose.prod.yml, infrastructure/docker-compose.v3-uat.yml, k8s/deployment.yaml (optional env entry), and
`ADMIN_SPA_CLIENT_ID=` with a comment in .env.example / uat.env template if tracked; (5) tests — AdminUiControllerTests: set → uses it; unset →
falls back to ClientId; loginEnabled false when tenant missing (follow existing Controllers/*ControllerTests.cs style). Run `dotnet build` and
`dotnet test` for McpCognizioware.BillingService.tests and quote the output (new + existing green). Commit with the conventional message
`fix(admin): use dedicated Entra SPA client id for admin SSO (falls back to ClientId)` by EXPLICIT PATH (never git add -A); no push.
OUT OF SCOPE (operator/overseer, spec steps 7-8): any change on CT100 (.env, compose on the box, image build, compose up), pushes, PRs, memory
housekeeping. Never write real client secrets anywhere. Completion = commit hash + test output + the exact deploy checklist from spec step 7
restated with the values to use (ADMIN_SPA_CLIENT_ID=5651b092-e9ac-4595-b112-083df3648633; AZURE_CLIENT_ID/SECRET/AUDIENCE untouched) and the
rollback line. Auditor: no git fetch.

## After completion (overseer-owned post-processes)
1. Push `fix/admin-sso-spa-client-id`; open PR to `develop` (and note df6f8adc on main is superseded).
2. **cognizioware-qa gate — compatibility notice:** QA has no suite for the billing admin SPA. Minimum gate before prod: an `oidc`-style read-only
   smoke (GET /api/v1/admin/auth-config returns clientId 5651b092… + apiScope GUID-form + loginEnabled true; GET /admin/ serves the SPA; the MSAL
   authorize redirect carries client_id=5651b092…) run against the UAT lane (billing-uat-lane-app-1 on CT100) first. Add it as `qa-gate` target
   `billing` if Paxton wants it permanent; otherwise run it ad hoc and record it.
3. Deploy to CT100 per spec step 7 (UAT lane first, then prod compose project mcp-cognizioware-prod): append `ADMIN_SPA_CLIENT_ID=5651b092-…` to
   /opt/mcp-cognizioware/.env, sed the compose line after EntraId__Audience (box compose has a local Stripe__WebhookSecret mod — do not check out
   over it), build/pull the image, `docker compose -f docker-compose.prod.yml up -d mcp-billing-service`, wait healthy. Rollback = retag
   ghcr…billing-service:sha-44c4ccc → mcp-billing-service:latest + compose up + remove the two env lines.
4. Verify per spec: auth-config JSON, container logs show `/api/v1/admin/config/me - 200` after Paxton signs in as paxton@cognizio.company (Global
   Admin badge), no new JWT audience/issuer failures, API-key paths unchanged. Then update memory `billing-service-deploy-path.md`.

- 2026-09-06 01:45 PT: Paxton signed in to prod /admin/ — Global Admin badge + Infra link confirmed. SSO task COMPLETE (prod branch reconciliation #66 remains for the owner).
