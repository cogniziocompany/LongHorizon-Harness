# Handoff: finish the Hydra fleet plane so it replaces the PC overseer and queue

Written by the overseer session `5321285c` on 2026-09-14 for whoever picks this up. Priority order from
Paxton, read off the Ship Plane system map: **(1) powerplatform to prod, (2) this.** Everything marked
"measured" was read from the live system today; the rest is labelled as estimate or decision.

## What "done" means, in one sentence
Nothing about launching, gating or reporting harness runs depends on a script or a file on the ptait09
workstation. Enqueue happens through Hydra or the chat agent, the CT110 launcher owns launches, the fleet
window shows every run, and the PC launcher is switched off and archived.

## Where it actually stands (measured 2026-09-14)

| piece | state | evidence |
|---|---|---|
| fleet.easybutt0n.ai (report view) | **live but empty** | HTTP 200, `<title>Fleet Window</title>`, served by Express behind Caddy on CT202; `GET /api/fleet/nodes` returns `[]` |
| why it is empty | harness heartbeat has never succeeded | every push is rejected HTTP 413; task 114 owns this; `fleet_ever_succeeded=false` on CT110 `/api/meta` |
| CT110 queue API | **deployed, nothing writes to it** | 22 endpoints incl. `POST/GET/DELETE /api/queue`, integer priority, `launched`/`done`/`failed` statuses; harness launcher class wired |
| PC launcher (`C:\tmp\launch_queue.py`) | **still the only orchestrator** | 800-line hand-edited script, CRLF, files entries to `done/` at launch (19 strandings in 4 days), no cross-process lock, restarted by hand 3 times today |
| Hydra fleet MCP (control) | **live for devices, absent for the queue** | measured on the gateway: alias `hydrafleet` serves 28 tools and `hydra` 7 (list_devices, get_device_health_history, create_session, destroy_session, send_input, read_screen, ...); none enqueues, launches, gates or reads harness runs. The orchestration half of the control plane does not exist yet |
| Hydra deploy path | **does not exist** | `.github/workflows` in cognizioware-hydra holds only `claude-code.yml` and `hydra-ci.yml`; deploys have been by hand |
| `feat/harness-fleet` @ 38e4184 | **stranded** | ahead 3 / behind 47 vs main, no PR; the control-plane work reached main via PR #18 on 09-08 |
| The migration plan | **written and on main** | `LongHorizon-Harness/docs/handoffs/orchestrator-migration-2026-09-08.md`, 404 lines: 15-row behaviour inventory, owner map, chat-agent safety contract, four unowned gaps, a 6-step cutover with rollbacks, decommission criteria |
| Queue contract for callers | **on main** | `LongHorizon-Harness/docs/queue.md`, 12,980 bytes |
| Two wrong claims of mine, now corrected | withdrawn | "fleet's DNS points at the wrong host" (it was the CT202 outage) and "Hydra runs on corsairai300:3000" (that port is powerplatform). Do not act on either. |

## The spec is written; the gaps are what is left
The migration plan already answers the hard questions. Its own owner map lists exactly what has **no
owner**, and that list is the work:

1. **Retry / requeue of a failed entry** — today a failed entry is terminal on CT110, and the PC script's
   "file to done/ at launch" behaviour is how 19 failures went invisible. This is the single change that ends
   the stranding class.
2. **Cross-process no-double-launch** — a lease or idempotency key on enqueue. The PC script ran three
   copies at once on 09-08.
3. **Orchestrator liveness signal** — `queue_len` is hard-coded to 0 in the heartbeat today; nobody can see
   the launcher is alive.
4. **Shared-infrastructure interlock** — a gate mid-flight must block a router restart. Proposed home: Hydra.
5. **Per-caller tool scoping at the harness** — `harness_resolve_gate` is callable by any bearer holder; the
   chat agent must never gain gate-bypass power.
6. **Chat-agent budget ceilings** — entries per hour, `max_rounds` clamp.

Plus one the plan does not cover because it predates the finding: **the launcher must treat a working tree
holding another task's uncommitted work as occupied**, not just a tree with a live run (three collisions on
2026-09-14 alone).

## Existing tasks that already own pieces (do not duplicate)
| task | owns | state |
|---|---|---|
| 114 | fleet heartbeat 413 → without it the report view stays empty | queued; its last run failed |
| 134 | Postgres-backed QueueStore, `blocked` status, transitions | requeued 16:20 after a token stall; relaunches now that runs hold `GH_TOKEN` |
| 167 | the `lh-orchestrator` skill in the gateway skills catalog (doctrine moves off the PC) | queued |
| 168 | the cutover itself: import entries, stop the PC launcher, rotate its bearer, point the overseer at the API | blocked on 134 + a CT103 database + a zero-run window |
| 140 | Fleet Chat (Open WebUI) as the overseer UI, via BMAD spec; spec committed as `d84b7a7` | requeued |
| 148a–e | the trio table on the fleet window | 3 queued, 2 blocked |
| 166 | Hydra deploy path + the stranded branch + PR triage (corrected today) | queued |
| 55 | the migration plan | delivered on main; its three "secondary commits" (idempotency key, liveness signal, queue.md) — check which landed before assuming |

## Effort, honestly
Estimate, not measurement. Sized in harness runs plus overseer hands-on time; a run is 6–8 rounds.

| step | what | effort | depends on |
|---|---|---|---|
| A | Confirm which of task 55's three secondary commits are on main; land the missing ones (idempotency key on enqueue, real `queue_len`, contract doc) | 1 run + review | nothing |
| B | Fix the heartbeat 413 (task 114) so the report view fills | 1 run + 1 deploy | nothing; highest value per hour |
| C | Retry/requeue + `launched`→`done`/`failed` promotion from the run report on CT110 (gap 1) | 1–2 runs | A |
| D | Postgres queue store (task 134) | 1–2 runs | CT103 database/role (Paxton) |
| E | Shadow-mode CT110 launcher: computes every decision, launches nothing, emits shadow events; compare against the PC launcher for a week | 1 run + 7 days observation | A, C |
| F | Cutover (task 168): import active/blocked entries with int priorities, stop the PC script, rotate its bearer, doctrine points at the API | overseer, one quiet window | D, E green for the window |
| G | Hydra side: a deploy workflow or runbook, retire or carry the stranded branch, triage the 4 open PRs (task 166) | 1 run + hands-on | nothing |
| H | Control plane: gate interlock in Hydra, per-caller tool scoping, chat-agent ceilings (gaps 4–6) | 2–3 runs | F for the interlock to matter |
| I | Fleet Chat as the operator UI (task 140), the `lh-orchestrator` skill (task 167) | 2 runs | F |

Critical path: **A → C → E (7-day shadow) → F.** Roughly two to three weeks of calendar time, most of it
the shadow window, with about eight to ten harness runs and three or four hands-on windows. B and G run in
parallel from day one. H and I follow the cutover.

The shadow window is not padding: the plan's own decommission criteria require the CT110 launcher to agree
with the PC launcher's decisions across a CT110 reboot and a network blip before the PC script is switched
off, and the reason is that the PC script has silently stopped twice this week.

## Hard rules for whoever does this
- Read `orchestrator-migration-2026-09-08.md` §3 before touching the chat agent: it is the safety boundary
  (what it may call, what it may never call, human-only actions as a list).
- Never rebase `feat/harness-fleet`; diff it against main and carry per path or retire it.
- A CT110 harness deploy restarts the service and kills every in-flight run; only in a zero-active window.
- Env var names only; no bearer values in docs or commits; the PC launcher embeds a CT110 bearer that must
  be rotated at cutover, not copied.
- Report the fleet window as empty until `/api/fleet/nodes` is non-empty; do not insert a host row to fake it.

## Verification for "done"
1. `GET /api/fleet/nodes` non-empty and the wallboard lists runs whose ids match `GET /api/runs` (compare
   the lists).
2. An entry enqueued through Hydra or the chat tool appears in `GET /api/queue` and launches from CT110.
3. No PC launcher process; its bearer returns 401; `C:\tmp\queue` is a read-only archive.
4. A forced 429 on round 1 leaves the entry `failed` with the cause, never `done`.
5. The liveness signal shows real queue depth and a launcher tick on the fleet window.

---
## CORRECTIONS appended 2026-09-14 by session [fe2679] after reading the repos (Paxton asked for the audit; his decisions are marked)

| row above | correction | evidence |
|---|---|---|
| Step A "confirm which of 55's secondary commits landed" | ALL THREE are on origin/main; Step A is done | 0de7b2d dedup_key (queue.py L79, L350-364; a BODY field, not a header), 3efb79b real queue_len (server.py L674-679), ca4670c docs/queue.md 12,980 b |
| "Hydra deploy path does not exist" | hydra-ci.yml has a `deploy` job on the self-hosted hydra-host runner (rsync to /opt/cognizioware-hydra, compose build/up, health, smoke, qa dispatch) | .github/workflows/hydra-ci.yml L110+ |
| "feat/harness-fleet @ 38e4184 stranded" | merge-base with main is the branch's own commit fbd150d: its control-plane content is in main. Only delta = PR #5 (device-token), CONFLICTING. **Paxton: close #5 as superseded; close the branch.** | git merge-base; PR list |
| "Hydra fleet MCP: none enqueues, launches, gates or reads runs" | hydrafleet (28) already has list_fleet_runs, create_fleet_run, get_run_snapshot, send_run_instructions, resolve_run_gate, stop_run, resume_run, get_fleet_capacity, get_run_activity; fleet-scheduler consumes the LHH queue contract. **Only enqueue is missing** (task 176) | orchestrator/src/mcp.js L122+, fleet-scheduler.js L62-69, fleet-routes.js L277 |
| "interlock: proposed home Hydra" | a per-node guard exists (409 on non-terminal runs, force-bypassable) at fleet-routes.js L94-95; extend it to pending gates and remove force for infra restarts (task 176) | |
| "heartbeat 413; task 114 owns it" | root cause: fleet-admin/src/server.js L40 whitelists only /harness/rounds for the 10 MB parser; heartbeat inherits 1 MB. Reporter side: runs = every run ever (server.py L664), summary duplicated (reporter.py L250), _MAX_PAYLOAD_BYTES unused (L44), 413 swallowed (L408-414). **114 is in done/ with run 476970f4 and no later ledger row — verify or requeue** | |
| Launcher on CT110 | real (launcher.py L298-322); NO shadow mode; NO retry (mark_failed terminal); occupancy = live-run path string only, no dirty-tree check; in-process lock only; _VALID_STATUS lacks `blocked`; mcp dispatch = one bearer, no caller scoping; gateway intended-key-matrix.yaml L19 gives EXECUTOR keys hydra+hydrafleet | tasks 172-174, 177 |
| Task 168 text | lacks the shadow phase that task 55 mandates; amended today | |

**Paxton's decisions 2026-09-14:** gate resolution from Fleet Chat only after per-user keys (one LiteLLM virtual key per user; harness-chat vs harness-operator groups; key entered once, persisted per login, default harness-chat, accept/use step) — tasks 140 + 177, golden scenario in 113; all fleet-plane tasks append AFTER every powerplatform task; shadow window has no fixed length, promote on the six evidence items with the reboot and blip exercised deliberately; PR #5 closed as superseded.

**Queued today (after pp, 9999l–q):** 172 launcher-terminal-and-retry (active), 173 launcher-shadow-and-occupancy (blocked on 172), 174 harness-tool-scoping-and-ceilings (blocked on 134), 175 fleet-admin-deploy-path (active), 176 hydra-enqueue-and-interlock (active), 177 gateway-caller-scoping (active). Amended: 166, 168, 167, 169 (sampling size), 113, 140.
Corrected critical path: 114 → 172 → 173 (observe on) → evidence → 168 cutover; 175/177/166 in parallel from day one; 174/176 after 134/cutover.
