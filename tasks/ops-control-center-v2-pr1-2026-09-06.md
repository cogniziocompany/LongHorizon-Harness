# TASK — Ops Control Center v1.1 → v2, PR 1 of 5 (read-only revisions: role tiering, discovery targets, GET /api/status)

**Owner session:** Claude Code `6803faad-e27d-43c0-8150-328e549e1fb1` (cwd `c:\Users\PaxtonTait\source\cognizioware-mcp-tools`, stale branch — do not build there).
**Overseer role:** manager — worktree, run, review, merge (lane auto-deploys to CT202), then PRs 2-5 as follow-up runs.
**Harness run:** `20260906T021714Z_d2fd4759` (WSL workbench, 2026-09-06 02:17 PT, `:pool` trio, 16 rounds).
**Authoritative spec (read FIRST):** `C:\Users\PaxtonTait\.claude\plans\c-users-paxtontait-source-claims365-lab-snoopy-rossum.md` (revised same day: v1.1 → v2).
**Harness workspace (WSL-created, LOCKED):** `/mnt/c/Users/PaxtonTait/source/cognizioware-mcp-tools-opsv2` — branch `feat/ops-control-center-v2` from `origin/main` @ d55d6df
(v1 + #69 #71 #72 #73 already in). Live facts baked into the task text: email arrives in X-Forwarded-User, roles in X-Forwarded-Groups; enforcement ON; Caddy
`@keyed` routes key-gated API paths straight to the app; ingest key header is X-Ops-Ingest-Key.

## Scope of this run (section 8, PR 1 only)
§1 identity/roles + ActionGuard + ConfirmActionModal + audit record (NO actions wired) · §2 TargetsComposer + `--targets` + compose.overrides + Caddyfile :ro
mount + gateway.expected_servers · §3 GET /api/status(*) only (ops-mcp is PR 4) + Caddy @keyed + SKIP_AUTH_ROUTES + suite 33 cases · handoff addendum.
Verification via docker build + local compose smoke (no dotnet on the WSL box). Task text: `ops-control-center-v2-pr1-2026-09-06.task.txt`.

## Follow-ups (each its own run after the previous PR deploys)
PR 2 v2 actions (rw socket proxy profile, ActionGuard-gated restart/CI dispatch/DDNS) — deploy with OPS_ACTIONS_ENABLED=false, then hand-flip
COMPOSE_PROFILES=ops-actions + OPS_ACTIONS_ENABLED=true in mcp-tools.env and mirror non-secret defaults in compose same day · PR 3 Settings + Cloud +
Langfuse feed (OPS_SETTINGS_KEY, OPS_LITELLM_CLOUD_KEY mint, Langfuse pk/sk via UI) · PR 4 ops-mcp in-app /mcp + litellm-config ops_mcp + baseline
bump + suite 34 · PR 5 docs. Manual runbook items listed in plan §8.

## After completion (post-processes)
1. Overseer review (no action endpoints; /api/status has no secret-looking strings; discovery rows match live containers) → merge → lane → verify
   `curl -H "X-Ops-Ingest-Key: $K" https://ops.easybutt0n.ai/api/status` live and the role chip in the footer.
2. Owner 6803faad: nothing until PR 2 is scheduled; report any deviation from the plan.
