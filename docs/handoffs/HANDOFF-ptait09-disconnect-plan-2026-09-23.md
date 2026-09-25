# Plan: disconnect PTAIT09 and C:\tmp\queue — run the fleet from CT infra, driven from chat.easybutt0n.ai
Written 2026-09-23 by the interactive session [61855af8] at Paxton's request. Every row below was **measured today**
unless marked otherwise. Goal (Paxton): a user controls the Hydra + lh-harness combo from chat.easybutt0n.ai; the
queue, launcher, overseer and state live on CT infra; PTAIT09 and `C:\tmp\queue` can be switched off without anything
stopping. Every piece of work has an owning queue task (completed, in progress, or queued today).

## 1. What PTAIT09 still does for the fleet (measured 2026-09-23)
| # | Dependency on PTAIT09 | If PTAIT09 went off today | CT-side replacement | Owner | State |
|---|---|---|---|---|---|
| 1 | **PC launcher** `C:\tmp\launch_queue.py` — the ONLY thing that launches runs | nothing launches | CT110 launcher (deployed, currently `observe: True`) | **168** cutover | blocked on the 7-day shadow window (~2026-09-30); Paxton's last gate cleared 09-23 |
| 2 | **Queue state** `C:\tmp\queue\*.json`, `blocked\`, `done\` (~268 entries) | queue lost | CT110 queue API + Postgres store on CT103 (`lh_harness`, provisioned and end-to-end proven 09-21) | **168** (import step) | migrations 001/002 and `queue_backend=postgres` still to apply |
| 3 | **Overseer brain**: scheduled task `LH-Overseer-Sweep` → `overseer_tick_hidden.vbs` → `overseer_tick.ps1` → `claude -p` + `LOOP-PROMPT.md`, every 5 min | no ticks, no gates resolved, no merges | headless tick on a CT, using the `ANTHROPIC_*` creds already in CT110's secrets env | **104** (released today) | was paused "until Paxton says go" — Paxton said go 2026-09-23 |
| 4 | **Overseer documents**: `LEDGER.md`, `OPEN-ASKS.md`, `LOOP-PROMPT.md`, `STRANDING-DISPOSITION.md`, `C:\tmp\HANDOFF-*.md`, `C:\tmp\*-task.txt` | institutional memory lost | versioned repo + DB (overseer web UX at fleet.easybutt0n.ai/overseer) | **104** + **197** | 197 delivered 182a/b; 182c/d outcome **unverified** |
| 5 | **Ship Plane** `C:\tmp\ship-plane.html`, republished by an interactive session | no status page | hosted page rendered from the DB | **104** (folded in) | not started |
| 6 | **Harness deploys** `C:\tmp\optimistic_deploy_harness.py` (streams `deploy-harness-v2.sh` into CT110 over ssh) | CT110 can't be upgraded | GitHub Actions workflow on the LAN runner (CT210) | **224** (queued today) | not started |
| 7 | **Hivemind ingest** task `Cognizioware Hivemind Overseer Ingest` → `overseer-session-ingest.mjs` (reads PTAIT09 Claude transcripts) | ingest stops — fine once the overseer no longer runs here | moves with the overseer (#3) | **104** | — |
| 8 | Launcher helpers `spin_watchdog.py` (running), `quota_resume2` | nothing, once #1 retires | retire with the PC launcher | **168** | — |
| 9 | **Chat's tool list includes the PTAIT09 runner** (`/opt/fleet-chat/mcpo.config.json` on corsairai300: `ptait09 → https://ptait09.easybutt0n.ai/mcp`) | a dead tool entry in chat, harmless | remove the entry | checklist D6 | — |
| 10 | **Credentials used from PTAIT09**: `gh` as prax211 (ticks merge PRs), `~/.ssh/id_ed25519_proxmox`, the claude subscription login, the gateway key in `~/.claude.json` | ticks can't merge or reach hosts | CT110 `GH_TOKEN` (fine-grained PAT: contents/PRs/actions R+W, statuses R), the gateway `ssh` MCP server (holds the proxmox key group), harness `ANTHROPIC_*` | **104** | the PAT and ssh already exist CT-side |
| 11 | Stray scheduled task `CodexDeployReminder-CT202` (a MessageBox) | nothing | disable it | checklist D7 | — |

## 2. Chat control today — what works, what doesn't
**Already wired (measured):** chat.easybutt0n.ai = Open WebUI + mcpo on corsairai300 (both up 2 weeks). mcpo's
`cognizioware` entry calls the gateway with `x-mcp-servers: memory,hydrafleet,ssh,cloudflare,n8n,graphify`, so the chat
can already call `hydrafleet-enqueue_task`, `list_fleet_runs`, `get_run_snapshot`, `resolve_run_gate`, `stop_run`,
`resume_run` and the rest of the 29 tools. The `lh-orchestrator` skill (task 167) is in the gateway skills catalog,
so the chat model can load the operating doctrine.

**Three gaps before this is safe and useful:**
1. **Entries enqueued from chat land on CT110, and CT110 is in observe mode — it records decisions and launches
   nothing.** Until 168 flips it, chat-queued work only runs if the PC launcher happens to mirror it.
   **168 is the critical path for chat control, not only for the disconnect.**
2. **One shared key for every chat user** — anyone on the chat can resolve gates and stop runs. Per-user keys and the
   chat/operator split are **177 → mcp-tools #169 (OPEN)**, now designed: two `hydrafleet` registrations with different
   `allowed_tools`. Harness-side caller scoping and budget ceilings are **174 → LHH #36 (OPEN)**.
3. **The Fleet Chat UX spec is unreviewed** — **140 → BMAD_Cognizioware #2, OPEN since 2026-09-15**: per-user key entry,
   stored against the login, passed per request. Nothing gets built until it is reviewed.

## 3. The powerplatform "dev lane from chat" flow, end to end
chat: "fix X in powerplatform" → `hydrafleet-enqueue_task` (workspace cognizioware-powerplatform) → CT110 launcher
(after 168) → run branches from `origin/develop`, pushes, opens a PR to `develop` → harness review (**186 → LHH #23**,
`run_kind=review`; GitHub App `cognizioware-lh-reviewer` installed 09-21) → merge → `promote.yml` deploys **dev only**.
`develop` never reaches uat/prod on its own (release → uat and main → prod stay gated), so dev keeps iterating
while prod testing continues, and nothing started from chat can reach prod.

**Open question — who merges once PTAIT09 is gone.** Today, scheduled ticks merge as prax211. Options: the CT-hosted
overseer (row 3) using the CT110 PAT, or docs-only auto-merge via the PR gate (185 → mcp-tools #172) plus the merge
doctrine (188). **Recommended:** the CT overseer merges `develop`-only PRs after a green harness review; anything
aimed at `release` or `main` stays human.

## 4. Order of work (critical path first)
1. **Now, in parallel, no window needed:** merge mcp-tools **#169** (177) and LHH **#36** (174); review **BMAD #2** (140);
   run tasks **104** and **224** (code + PRs only).
2. **Shadow window** runs to ~2026-09-30, scored with the task-222 fixes (LHH #44, merged). Read the verdict using the
   `shadow-more-conservative` bucket — task 204 is the one real disagreement so far, and CT110 was right.
3. **Zero-run window (overseer):** apply migrations 001/002 on `lh_harness`; set `queue_backend=postgres` + `database_url`;
   restart the harness service (workers then also get `LH_HARNESS_DB_PASSWORD`, `SEQ_READ_KEY_PP_PROD`, `PP_DEV_E2E_*`);
   import the active and blocked entries; **flip `observe` off**.
4. **Stand up the CT overseer** (104): same LOOP-PROMPT doctrine, reading and writing the DB instead of `C:\tmp`, same
   5-minute cadence. Run it alongside the PTAIT09 tick for 24h in read-only mode, compare the two ledgers, then
   disable `LH-Overseer-Sweep`.
5. **Disconnect checklist** (section 5).

## 5. Disconnect checklist — do not unplug until every line reads back
- **D1** `GET /api/queue/config` on CT110 shows `observe: false`, and a real entry enqueued **from chat** launched on CT110.
- **D2** No `launch_queue.py` process on PTAIT09 for 24h, and nothing stranded (validator: 0 ERROR).
- **D3** The CT overseer has written ≥ 288 consecutive tick rows (24h) to the DB ledger; `LH-Overseer-Sweep` disabled.
- **D4** `C:\tmp\queue` exported to the DB and to a read-only archive (zip + checksum on the NAS); CT110's queue count
  matches the export.
- **D5** A harness deploy has run through the new CI workflow (224), not `optimistic_deploy_harness.py`.
- **D6** `ptait09` removed from `/opt/fleet-chat/mcpo.config.json` (backup first), mcpo restarted, chat still lists the
  hydrafleet tools.
- **D7** PTAIT09 scheduled tasks `LH-Overseer-Sweep`, `Cognizioware Hivemind Overseer Ingest`, `CodexDeployReminder-CT202`
  **disabled, not deleted** (reversible); `spin_watchdog.py` stopped.
- **D8** Rotate the CT110 bearer that `launch_queue.py` embeds (168 already requires this).

**Rollback at any step:** re-enable the scheduled task, set `observe: true`, restart `launch_queue.py`. Nothing in this
plan deletes the PTAIT09 copy until D1–D8 all pass.
