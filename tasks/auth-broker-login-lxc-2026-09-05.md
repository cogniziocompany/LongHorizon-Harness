# TASK — Non-MFA login LXC (CT208 cognizioware-auth-broker) + MSAL token broker for Dataverse/pac MCP auth

**Owner session:** Claude Code `d33948bb-ce8c-4f3c-8080-47d7552f62c7` (cwd `c:\Users\PaxtonTait\source\cognizioware-mcp-tools`).
It creates and monitors the harness run; the overseer schedules and supervises the session.
**Authoritative spec (read FIRST, follow exactly):** `C:\Users\PaxtonTait\.claude\plans\why-arent-we-using-velvety-anchor.md`
(WSL `/mnt/c/Users/PaxtonTait/.claude/plans/why-arent-we-using-velvety-anchor.md`).
**Harness workspace (dedicated worktree, WSL-form pointers):** `/mnt/c/Users/PaxtonTait/source/cognizioware-mcp-tools-broker`
— branch `feat/auth-broker-login-lxc` based on `feat/pac-dataverse-mcp` @ e1455fd (plan execution-order step 1, user-confirmed base).
Windows git shows "not a git repository" there until the run ends; use WSL git.

## Model trio (cloud-ollama standard)
manager `glm-5.3:cloud` · executor `kimi-k2.7-code:cloud` · auditor `kimi-k3:cloud`

## Launch payload
```json
{"model":"kimi-k2.7-code:cloud","agent":"claude_code",
 "workspace":"/mnt/c/Users/PaxtonTait/source/cognizioware-mcp-tools-broker",
 "roles":{"manager":{"agent":"claude_code","model":"glm-5.3:cloud"},
                 "executor":{"agent":"claude_code","model":"kimi-k2.7-code:cloud"},
                 "auditor":{"agent":"claude_code","model":"kimi-k3:cloud"}},
 "max_rounds":18,"task":"<task text below>"}
```
POST to `http://127.0.0.1:8799/api/runs` (WSL workbench), JSON via stdin `--data-binary @-`.

## Task text (paste verbatim, one slice per round)
BUILD-ONLY, UAT-FIRST. Read the spec at the path above FIRST; work only in this worktree. Deliver plan section 5 exactly:
(1) infrastructure/docker/msal-broker/{Dockerfile,package.json,src/index.js,README.md} — generalize infrastructure/docker/ms365-mcp/src/token-manager.js + token-store.js (MSAL, AES-256-GCM store, requireApiKey) plus e2e/lib/msal-auth.js for client-credential; auth order ROPC (public client — NEVER send client_secret on the refresh grant, AADSTS700025) then silent refresh then SPN; MCP streamableHTTP :3125 with tools auth_status and auth_get_token(resource) and REST /token and /health on :3126; resources dataverse and foundry scopes.
(2) infrastructure/auth-broker/docker-compose.yml (msal-broker, playwright-login :3127 headless isolated, lh-harness) + auth-broker.env.example (placeholders only) + create-lxc.sh (pct create/config for CT208 on ptait01: nesting, static IP, onboot=1; SMALL footprint — CT202 20 GiB + CT207 32 GiB already committed) — the script is written, NOT executed.
(3) infrastructure/lh-harness/episodes/login-and-prove.toml (+ config.toml overrides) implementing section 4: auth_status -> auth_get_token(dataverse) -> Playwright login vs TRMS DEV (reuse qa1-autonomous-login.js logic) -> screenshot to /opt/auth-broker/proof/ -> Web API RetrieveTotalRecordCount; auditor asserts expiry > 50 min, PNG > 20 KB, HTTP 200.
(4) infrastructure/docker/pac-mcp/entrypoint.sh username/password seed path (keep the SPN path primary; note the pac --username/--password deprecation).
(5) infrastructure/litellm-config.yaml entry auth_broker_mcp (alias auth, access_groups ["auth-broker"] ONLY — never repo-tools/dev-tools), infrastructure/docker-compose.yml extra_hosts fail-fast entry, infrastructure/.env.example names.
(6) e2e/suites/31-auth-broker.test.js (soft-fail off-LAN; auth_status reachable, token expiry, proof PNG listing, repo-tools key excluded) + e2e/package.json script test:auth-broker; suite 17 exclusion + suite 20 matrix row empty.
(7) ms365 token store to Postgres: infrastructure/docker/ms365-mcp/src/pg-token-store.js, migrate-token-store.mjs, compose env TOKEN_STORE_PG_CONNECTION (section 2b) — code + migration only, no live migration.
(8) Docs: design docs/handoff-docs/auth-broker-login-lxc-handoff.md (plan header + runbook), token-store-admin-handoff.md, design docs/claims365-lab-auth-broker-audit.md (section 6 — read-only survey of /mnt/c/Users/PaxtonTait/source/claims365-lab; NO edits to that repo), CLAUDE.md infra table row, secrets runbook entry in design docs/mcp-placeholder-services-audit-and-plan.md.
Unit-test the broker offline (mocked MSAL); if a local gitignored env with ai-dev01 creds exists you MAY run one real ROPC acquisition and quote only account + expiry (never the token or password) — otherwise record it as an operator step.
HARD RULES: no .github/workflows edits; no secrets committed; no host/LXC/CT202 changes (creating CT208, compose up, CT202 env fills, redeploys are operator steps listed in the handoff); commit by EXPLICIT PATH (CRLF phantoms — never git add -A); no push.
Completion = commits + hashes per deliverable, test output quoted, operator-steps checklist (create-lxc, auth-broker.env, Pi-hole dns.hosts + Caddy route for auth-broker.lan.easybutt0n.ai, CT202 mcp-tools.env PAC_DEV_* / PAC_DEV_USERNAME/PASSWORD, LXC firewall allow-list CT202 + ptait09). Auditor: no git fetch.

## After completion (overseer-owned post-processes)
1. PR feat/auth-broker-login-lxc -> main (stacked on the pac PR #61; merge order pac -> broker).
2. Operator: create CT208 with create-lxc.sh (ptait01 pct), install Docker, place auth-broker.env, compose up; DNS + Caddy; run the login-and-prove episode inside the LXC; collect PNG proof + auth_status.
3. cognizioware-qa gate before any CT202 deploy (target=litellm on UAT) + npm run test:auth-broker, suites 17/20/23/29 from the LAN runner; then merge -> CI lane (QA gate sits between UAT and prod) -> CT202 env fills -> pac-mcp restart -> verification per the plan. If QA lacks a compatible check, notify Paxton.

## Completion (2026-09-05 ~11:45 PT)
Runs: 2a38855a (failed r7-8, glm-5.3 manager thinking-block) → 50a5c48c (died: prax211 Ollama quota) → `20260905T101725Z_2b308880` on the
`:pool` trio completed all eight deliverables (12 rounds, clean audits). Branch `feat/auth-broker-login-lxc`: 23f4f05 c6f29ad f8bc0ff (1) ·
c42b824 (2) · 1139e5b (3) · 46a39dd (4) · aa7e651 441b825 (5) · b6e8091 (6, suite 31) · 465f9eb (7) · f0e6ab3 (8) · 11faf4b kb-hook (overseer).
Review: 31 files, no `.github` changes, YAML parses, placeholders only. Owner d33948bb given GO to push + PR stacked on pac #61.
Operator steps (CT208 create, auth-broker.env, DNS/Caddy route, CT202 env fills) wait for Paxton's go.

PR: mcp-tools **#65** `feat/auth-broker-login-lxc` → `feat/pac-dataverse-mcp` (stacked on #61; retarget to main after #61 merges).

## Operator log (2026-09-05, overseer)
- CT208 `cognizioware-auth-broker` on ptait01 at **192.168.21.166** (`.163` is the pp dev host). Needed: Pi-hole resolver via `pct set --nameserver`
  + restart (template inherits the host's Tailscale DNS), and `firewall=1` removed from net0 (blocked all traffic; CT202 runs without it).
  create-lxc.sh fixed on the branch (3ed462e). Docker 29.8 / compose v5.5.
- `/opt/auth-broker/auth-broker.env` (600 root) with tenant, public client, ai-dev01 ROPC account, dataverse URL, broker token, cache key.
  Repo contexts under `/opt/auth-broker/repo/infrastructure/`. `msal-broker` healthy on :3125/:3126; real ROPC token for dataverse acquired;
  token store persists after `chown node:node /data` (EACCES before).
- Pi-hole `192.168.21.161 auth-broker.lan.easybutt0n.ai`; Caddy route on the branch (8dbb9d1) → 192.168.21.166:3125; CT202 mcp-tools.env
  got AUTH_BROKER_TOKEN, AUTH_BROKER_MCP_URL, PAC_DEV_* (username/password fallback; SPN ids still blank).
- Gaps left on PR #65: `lh-harness` service has no Dockerfile; `playwright-login` Dockerfile patch no longer matches playwright/mcp:latest.
- Deployed: #65 merged (5b995dd), lane 33988761927 green. Caddy route works (https://auth-broker.lan.easybutt0n.ai/mcp → 401 without token).
  LiteLLM could not reach it: compose `extra_hosts` hardcodes `auth-broker.lan.easybutt0n.ai:127.0.0.1` (fail-fast) → parametrized as
  `${AUTH_BROKER_HOST_IP:-127.0.0.1}` (#69) with AUTH_BROKER_HOST_IP=192.168.21.161 in mcp-tools.env; lane 21073a0 applies it.
- Lane 21073a0 (#69) green: LiteLLM resolves auth-broker.lan → 192.168.21.161 and lists auth-auth_status / auth-auth_get_token; CT208 broker reachable through Caddy. Broker task COMPLETE except the two build gaps (lh-harness Dockerfile, playwright-login patch) and the SPN ids.
