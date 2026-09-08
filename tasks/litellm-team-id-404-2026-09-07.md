# LiteLLM: keys with an unregistered team_id 404 after 1.100.0 (2026-09-07)

**Source:** incident handoff from session bb0a0254 (Opus 5), relayed by Paxton 2026-09-07 ~23:40 PT. Severity P1 for affected tenants (orchestrator chat / LibreChat 100% down; gateway healthy).

## Live audit (overseer, CT202 postgres litellm_mcp_tools)
- 266 virtual keys; 55 carried a team_id with no `LiteLLM_TeamTable` row:
  - `litellm-dashboard` 51: LiteLLM UI login sessions (1-day expiry, default_user_id, 42 already expired). 1.100.0 RESERVES this team_id (`/team/new` returns 400 "reserved for LiteLLM UI dashboard sessions"). Not tenant keys.
  - `librechat` 3 (every LibreChat key), `advanced-paste-desktop` 1: real tenant keys, minted with a hand-set team_id that never had a team row. Pre-1.100 tolerated the dangling id; 1.100.0 enforces it at auth.
- CT204 UAT had the same two phantom teams (3 + 1) plus 38 dashboard sessions.

## Mitigation applied (additive, reversible, no restart)
- CT202: `POST /team/new` for `librechat` and `advanced-paste-desktop` (team_id = alias = name, metadata restored_by=overseer-2026-09-07). Probe key in team librechat: `GET /v1/models` 200. Orphan tenant keys remaining: 0.
- CT204: same two teams created. A scripting slip first created a team with an empty team_id on UAT; removed via `/team/delete` plus row delete (verified 0 rows). Probe 200; orphans 0.
- Scripts: `C:\tmp\ct202-teamaudit.sh`, `ct202-teamrestore.sh`, `ct204-teamrestore2.sh` (run via `pct exec` on 192.168.21.110).
- Not done: the 51 dashboard session keys stay (reserved id; most expired). Cleanup is part of the task below.

## Harness task (recurrence fix)
Queue entry `05g-litellm-team-integrity` (mcp-tools, kimi, 5 rounds, task text `C:\tmp\litellm-team-404-task.txt`): read-only audit script + idempotent restore script (refuses empty/reserved ids), e2e suite 39 team-integrity gate (`test:teams`), expired UI-session cleanup (dry-run default), S13 runbook + a PRE-UPGRADE audit step in the upgrade runbook. Lane deploys UAT then prod.

## Lessons
- Any LiteLLM upgrade: audit key-to-team integrity BEFORE pulling the image (becomes a runbook step via the task).
- Never build box scripts by sed-editing another script's loop; write the file whole. The empty-id team on UAT came from that.
