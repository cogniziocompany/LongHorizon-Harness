# TASK — Hivemind: overseer-session ingester (Claude Code transcripts → memory-mcp, source "overseer")

**Asked by Paxton 2026-09-05 19:35 PT:** "did you finish this … if not create an lh-harness task where you manage it and get it live asap".
Status before this task: NOT built. The hivemind pipeline (`cognizioware-hydra-fleet/scripts/hivemind-ingest.mjs`, cron on corsairai300)
captures harness runs + fleet activity only; nothing reads `~/.claude/projects/<slug>/*.jsonl`.
**Overseer role:** manager — worktree, run, review, then the live wiring (env file with the memory key, Scheduled Task on PTAIT09, later ptait-desk03).
**Repo:** cogniziocompany/cognizioware-hydra, branch `feat/overseer-session-ingest` from origin/main @ c7651d1 (PR #4 merge).
**Harness workspace (WSL-created, LOCKED):** `/mnt/c/Users/PaxtonTait/source/cognizioware-hydra-overseer`.
**Task text:** overseer scratchpad `overseer-ingest-task.txt` (verbatim in the run).

## Verified facts fed to the run
- Transcript record types/fields (user, assistant, ai-title, pr-link, last-prompt); content = string or block list; most user records are
  tool_result-only (209 of 222 in the sample session) → a "turn" needs a text block.
- memory-mcp REST `POST /api/memories` (Bearer MEMORY_TOKEN; required source/session_id/text; content-hash dedupe). CT202 :3120 is NOT
  reachable from PTAIT09 (TCP fails, ping works), so the ingester also speaks the LiteLLM MCP gateway (`x-mcp-servers: memory`,
  tool `remember_session`), which is reachable from anywhere.
- tier is a free string server-side → tier "overseer".

## Model trio
manager `kimi-k2.7-code:pool` · executor `kimi-k2.7-code:pool` · auditor `kimi-k3:pool` (API keys `roles` + `max_rounds`).

## After completion (overseer)
1. Review: `node --test`, dry-run payload sanity, no secrets, diff limited to scripts/** design/** README.md.
2. Push branch, PR → main, merge (the hydra repo has no deploy lane for scripts; the orchestrator on corsairai300 is a tarball deploy and
   does not need this change).
3. Live: write `%LOCALAPPDATA%\cognizioware\overseer-ingest.env` on PTAIT09 with MEMORY_MCP_URL + a gateway key allowed on the `memory`
   server (hive-mind key), run once with `--dry-run`, then install the Scheduled Task; verify with `recall` that an overseer memory comes back.
   Then ptait-desk03.

## Completed — LIVE on PTAIT09 (2026-09-05 20:25 PT)
Run 19d5e76c: 5 commits (1d328cf ingester · 7ae4620 tests · 3204a41 scheduler install · f7e471e docs · 7d4ea70 repairs), 8 files +1042; node:test 11/11
(run the FILE, not the directory, on Windows node); dry-run payloads well-formed. Merged as hydra PR #6. First live run exposed four gateway defects,
fixed by the overseer in PR #7: MEMORY_MCP_URL must keep its trailing slash (`/mcp/`; the gateway 307s `/mcp` and drops the POST body), the tool is
`memory-remember_session` behind the gateway (server-alias prefix; `MEMORY_MCP_TOOL` overrides), JSON-RPC errors/`result.isError` now fail the post,
and the transcript offset no longer advances past failed posts.
Live wiring: install checkout `%LOCALAPPDATA%\cognizioware\hydra` (main), env file `%LOCALAPPDATA%\cognizioware\overseer-ingest.env`
(MEMORY_MCP_URL + MEMORY_MCP_KEY = LiteLLM virtual key alias `overseer-ingest`, mcp_access_groups [hive-mind], no models), per-user Scheduled Task
"Cognizioware Hivemind Overseer Ingest" every 5 min (registered with Register-ScheduledTask; the repo's install ps1 has `#requires -RunAsAdministrator`
and could not be used from a non-elevated shell — follow-up). Verified: `memory-recall` with source=overseer returns ptait09 session turns.
Follow-ups: (a) drop the admin requirement from install-overseer-ingest.ps1; (b) the "No memory transport configured" warning prints before `--env-file`
is loaded (cosmetic); (c) ptait-desk03 install.
