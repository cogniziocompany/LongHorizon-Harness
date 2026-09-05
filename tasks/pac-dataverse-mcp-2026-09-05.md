# TASK — pac CLI MCP + Dataverse data MCP on the gateway, TRMS "four options" docs, Option-C release scripts

**Owner session:** Claude Code `2b006233-848f-40ac-9ade-cc5a41559271` (cwd `c:\Users\PaxtonTait\source\cognizioware-mcp-tools`).
It creates and monitors the harness run(s); the overseer schedules and supervises the session.
**Authoritative spec (read FIRST, follow exactly):** `C:\Users\PaxtonTait\.claude\plans\audit-its-capabilities-comparted-snappy-deer.md`
(WSL path `/mnt/c/Users/PaxtonTait/.claude/plans/audit-its-capabilities-comparted-snappy-deer.md`).
**Harness workspace (dedicated worktree — the main checkout is busy):** `/mnt/c/Users/PaxtonTait/source/cognizioware-mcp-tools-pac`
(branch `feat/pac-dataverse-mcp` from `main` @ 347e5b9). TRMS docs live at `/mnt/c/Users/PaxtonTait/source/repos/trms/docs`
(read/write for WP3; scripts for WP4 under `/mnt/c/Users/PaxtonTait/source/repos/trms/trms_cognizioware/e2e/scripts/release/`).

## Model trio (cloud-ollama standard)
manager `glm-5.3:cloud` · executor `kimi-k2.7-code:cloud` · auditor `kimi-k3:cloud`
(`glm-5.3-flash:cloud` is the router fallback only — never the executor seat; minimax retired).

## Launch payload
```json
{"model":"kimi-k2.7-code:cloud","agent":"claude_code",
 "workspace":"/mnt/c/Users/PaxtonTait/source/cognizioware-mcp-tools-pac",
 "roles":{"manager":{"agent":"claude_code","model":"glm-5.3:cloud"},
                 "executor":{"agent":"claude_code","model":"kimi-k2.7-code:cloud"},
                 "auditor":{"agent":"claude_code","model":"kimi-k3:cloud"}},
 "max_rounds":20,"task":"<task text below>"}
```
POST to the WSL workbench `http://127.0.0.1:8799/api/runs` (pipe JSON via stdin `--data-binary @-`).

## Task text (paste verbatim, one slice per round)
BUILD-ONLY, UAT-FIRST. Authoritative spec at the path above — read it FIRST. Work in this worktree only.
Sequence per spec §Sequencing: (1) WP1 spikes S0/S2/S3/S5 — S0 and S5 are runnable offline/locally (docker build
of `infrastructure/docker/pac-mcp`, tools/list, LiteLLM schema inspection); S2/S3 need Entra/PPAC actions — if creds
are absent record them as OPERATOR STEPS with exact commands and continue (never fabricate spike results; docs must
say "unverified" where the spec says so). (2) WP3 docs 3.1 + 3.2 then the 3.3 edits in the stated order, matching
the TRMS doc conventions exactly; never copy values from the gitignored `.mcp.json`. (3) WP4 Option-C release
scripts in the stated order; first real run of `export.ps1` against cognizio DEV only if creds exist, else dry-run.
(4) WP2 gateway commit 1 only: Dockerfiles/entrypoints, compose services, litellm-config aliases/entries with the
narrow access groups exactly as specified, `.env.example` names, e2e suites 29/30 (grace), suite 17 exclusions,
suite 20 matrix rows `'empty'`, docs mirror (§2.6). Before finishing: diff the live `/opt/cognizioware-mcp-tools/litellm-config.yaml`
on CT202 (read-only via ssh) vs repo and report live-only hunks (risk R1) — do not merge, do not deploy, do not touch
CT202 env. Hard rules: no `.github/workflows/` edits; no secrets committed; commit by EXPLICIT PATH (CRLF phantoms —
never `git add -A`); no push. Completion = commits + hashes per WP, spike results table (verified/unverified), and the
operator-steps list (CT202 env secrets, Entra app for Dataverse MCP, PPAC feature toggles, county device-code bootstrap).
Auditor: no git fetch.

## After completion (overseer-owned, mandatory per Paxton's directive)
1. Review commits; merge commit 1 to `main` via PR only after R1 hunks are ported.
2. Operator: put `PAC_DEV_*` / `DATAVERSE_DATA_*` into CT202 `mcp-tools.env` **before** merge.
3. Merge → LAN E2E gate → CT202 deploy (existing pipeline). UAT on cognizio DEV per spec §2.8.
4. **cognizioware-qa full e2e** (`qa-gate.yml` target=litellm, env=prod after deploy; plus the repo's own suites
   29/30/17/20/22 with `RUN_LIVE_REGRESSION=1`). If QA has no compatible suite for a piece, notify Paxton.
5. Commit 2 (matrix `'live'`, baseline, mustHave) → gate hard-fails on regression. County bootstrap last.

## Deployed (2026-09-05 ~20:55 PT)
#61 merged (74d278f). Its lane was "green" on 2026-09-05 17:5x but never deployed (deploy job defect, see ops task / mcp-tools #66-#68);
lane bded802 actually deployed pac-mcp, dataverse-data-mcp and monday-mcp to CT202 (all healthy). Note: suite numbers 29/30 were already
used by hydra-fleet/memory suites (the repo has other duplicate suite numbers too).
