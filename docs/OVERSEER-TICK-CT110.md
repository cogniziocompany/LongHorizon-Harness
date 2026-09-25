# CT110 OVERSEER TICK ADAPTER — how LOOP-PROMPT.md is executed from CT110 (TASK 236)

This document is the adapter between the authoritative LOOP-PROMPT doctrine
(`docs/LOOP-PROMPT.md`, unchanged) and the CT110 home of the overseer sweep.
The doctrine itself is NOT rewritten: every state, rule, step and
prohibition in it still applies verbatim.  What changes is only **where
things live** — this file maps each `C:/tmp`-era surface in the doctrine to
its CT110 counterpart.  Where this file and the doctrine disagree, the
doctrine wins on behavior and this file wins on location.

Written for the systemd tick (`lh-overseer-sweep.timer` +
`lh-overseer-sweep.service` in `packaging/`, entrypoint
`scripts/overseer_ct110/tick.sh`) that replaces the PTAIT09 scheduled-task
chain (`LH-Overseer-Sweep` → `overseer_tick_hidden.vbs` →
`overseer_tick.ps1` → `claude -p`, every 5 minutes).

## 0. Actor identity and ancestry (replaces the winpid test)

The doctrine's "DECIDE WHICH ACTOR YOU ARE BY ANCESTRY, NEVER BY FEEL"
section resolves ancestry through `Win32_Process` on PTAIT09.  On CT110 the
equivalent test is systemd cgroup ancestry, run before STEP 0:

```bash
cat /proc/$$/cgroup          # must end in lh-overseer-sweep.service
systemctl status "$$"        # must show the tick unit as your cgroup
```

- Parent chain `systemd → lh-overseer-sweep.service → tick.sh → claude` =>
  **YOU ARE THE SCHEDULED TICK**.  Your authority is exactly
  `LH_OVERSEER_TICK_MODE` as passed to your process (read the env var, never
  assume): in `read-only` you observe and report ONLY; in `act` you act.
- Anything else (an interactive shell, a manual `claude -p`, a dev session)
  => you are an interactive session and inherit the doctrine's interactive
  rules: act only on Paxton's explicit requests, write the ledger row first.
- `LH_OVERSEER_TICK_ID` in your environment is the tick identity stamped
  into Seq and the hivemind ingest for this invocation; quote it in the
  ledger row.

## 1. State surface map (the C:/tmp → CT110 dictionary)

| Doctrine says | On CT110 it is |
|---|---|
| `C:/tmp/queue/LOOP-PROMPT.md` | `docs/LOOP-PROMPT.md` in the repo checkout (authoritative; `README-OVERSEER-APPARATUS.md:14`) |
| `C:/tmp/queue/LEDGER.md` | The tick ledger on the CT110 harness API + Postgres (`harness` schema on CT103, task 235 surface; ledger rows via the API/MCP surface below). Until the DB ledger UI (197) serves writes, each tick appends its row through the task 235 API and mirrors it to `docs/LEDGER.md` in the checkout via the normal repo PR flow. |
| `C:/tmp/queue/OPEN-ASKS.md` | `queue/OPEN-ASKS.md` in the repo checkout (authoritative archive). New rows go through the task 235 API; the repo file is updated by the normal PR flow. |
| `C:/tmp/queue/done/*.json`, `blocked/` | The `harness.queue` Postgres store (migrations 001–003) via `GET /api/queue` (states `pending`, `launched`, `done`, `failed`, `blocked`). The `done/`-is-an-upper-bound lesson (RULE 2) reads as: `launched`/`done` are launch-time records; verify actual state with `GET /api/runs/<id>/status`. |
| `C:/tmp/*-task.txt` | `tasks/*-task.txt` in the repo checkout (authoritative briefs) |
| `C:/tmp/HANDOFF-*.md` | `docs/handoffs/HANDOFF-*.md` in the repo checkout |
| `GET /api/runs` (PTAIT09-launched runs) | Same route, same host: `GET $CT110_WEB_URL/api/runs` with the `LH_HARNESS_WEB_TOKEN` bearer. All doctrine RULE 1 lessons about this endpoint (`{"runs":[...]}` shape, `id` not `run_id`, no bare curl, snapshot path, approvals in `approvals[]`) apply verbatim. |
| Ship Plane republish | The hosted Ship Plane page (task 104, folded in). A scheduled tick in read-only mode never republishes; in act mode the doctrine's skip-line applies ('republish pending, interactive') unless the hosted page is writable by the tick. |
| `C:/tmp/overseer_tick.log` / `overseer_ticks/` | `journalctl -u lh-overseer-sweep.service` and `/home/harness/.overseer-sweep/tick.log` + `ticks/<tick-id>.log` |
| `C:/tmp/overseer_tick.lock` (age-based, 55-min TTL) | `flock -n -E 0 /home/harness/.overseer-sweep/tick.lock` in the unit (kernel flock; releases on process death, no stale state) + systemd's own no-overlap oneshot semantics |
| Seq (doctrine has no sink) | Per-tick CLEF event via `scripts/overseer_ct110/tick_notify.py` (TASK 161 wire contract; env names `SEQ_URL`/`SEQ_API_KEY`/`SEQ_MIN_LEVEL`) |
| hivemind ingest (task 229) | One `remember_session` post per tick via `tick_notify.py` (env names `MEMORY_URL`/`MEMORY_TOKEN` or `MEMORY_MCP_URL`/`MEMORY_MCP_KEY`, tool `memory-remember_session`, trailing-slash lesson preserved) |

## 2. Read/write mechanics on CT110

- **Reads** go through the CT110 API (`$CT110_WEB_URL`, default
  `http://192.168.21.168:8799`) with the `LH_HARNESS_WEB_TOKEN` bearer:
  `GET /api/runs`, `GET /api/runs/{id}/snapshot`, `GET /api/runs/{id}/status`,
  `GET /api/queue`, `GET /api/queue/config`, and the task 235 MCP surface
  (`GET /api/mcp/fleet/tools`, `POST /api/mcp/fleet/{tool_name}` — the six
  read-only overseer-state tools, incl. `read_ledger`).  The doctrine's
  RULE 1 read-back lessons transfer as-is.
- **Writes** (gate resolves, requeues, instruction injections) are **act
  mode only**: `POST /api/runs/{id}/approvals/{approval_id}/resolve`,
  `POST /api/runs/{id}/instructions`, queue updates.  In read-only mode the
  tick reports the gate in its ledger row instead of resolving it — a
  read-only tick that found a gate writes "gate found, not resolved
  (read-only tick)" and stops there.
- **The repo checkout is read by the tick, written only through git.**  The
  tick works in `/home/harness/work/LongHorizon-Harness` and uses plain
  `gh`/`git` with `GH_TOKEN` (env-var NAME, value from the EnvironmentFile).
  Doctrine rule honored: always pass
  `--repo cogniziocompany/LongHorizon-Harness` explicitly (two-remotes trap).

## 3. Credentials (env-var NAMES only — never values)

All supplied through `/home/harness/.overseer-sweep-secrets.env`
(EnvironmentFile of `lh-overseer-sweep.service`; operator-written on CT110,
never committed; registry in `docs/SECRETS.md`):

| Env NAME | Used for |
|---|---|
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` | the headless `claude -p` model credential (BASE_URL points at the LiteLLM router; the agent speaks only the Anthropic route) |
| `GH_TOKEN` | `gh` CLI for branch push / PR open / review reads |
| `LH_HARNESS_WEB_TOKEN` | bearer for the CT110 harness API |
| `LH_HARNESS_MCP_GATEWAY_URL` / `LH_HARNESS_MCP_GATEWAY_KEY` / `LH_HARNESS_MCP_GATEWAY_HEADERS_JSON` | the gateway MCP wiring (same names as `src/lh_harness/mcp_profiles.py`); the gateway `ssh` MCP server — which holds the proxmox key group per the disconnect plan — is reached under its alias name `ssh` through this key when, and only when, the doctrine's STEP 4 requires host-level probes |
| `SEQ_URL` / `SEQ_API_KEY` / `SEQ_MIN_LEVEL` | per-tick Seq logging |
| `MEMORY_URL` / `MEMORY_TOKEN`, or `MEMORY_MCP_URL` / `MEMORY_MCP_KEY` (+ optional `MEMORY_MCP_TOOL`) | hivemind ingest of its own ticks |
| `CT110_WEB_URL`, `LH_OVERSEER_SWEEP_HOME`, `LH_HARNESS_APPARATUS_ROOT`, `LH_OVERSEER_REPO_ROOT` | non-secret topology overrides |

No secret value may ever be committed, echoed, or written into a repo file.
A missing credential degrades behavior (e.g. no `SEQ_URL` ⇒ no Seq event,
exactly the seq-logging fail-open rule; no `LH_HARNESS_WEB_TOKEN` ⇒ the tick
records `no-credential` and does not run `claude` blind).

## 4. Single-instance and overlap rules

- `lh-overseer-sweep.service` is a oneshot: systemd never runs two instances
  concurrently, and `flock -n -E 0` makes an overlapping fire exit 0 (a
  logged skip, not a failed unit).  There is no age-based TTL to reason
  about; the kernel releases the lock on process death.
- The doctrine's "A second tick would also show its own START with no END"
  observation translates to: two `TICK ... START` lines for different
  tick-ids in the same minute in the journal is a defect to report, never
  the expected state.
- The `Persistent=true` timer fires at most ONE catch-up tick after idle —
  never a burst.

## 5. Rollout ownership (out of scope for the repo work)

1. Merge this branch (PR stays open until reviewed; DO NOT MERGE was the
   task-236 rule for the *implementation round*).
2. Deploy through the task 224 pipeline (the units stage ships the two
   units; the deploy itself never enables them).
3. Paxton enables the timer (read-only): `systemctl enable --now
   lh-overseer-sweep.timer`.
4. 24h read-only shadow alongside the PTAIT09 sweep; ≥ 288 tick rows
   (disconnect checklist D3); compare the two ledgers.
5. Flip to act mode (`LH_OVERSEER_TICK_MODE=act` +
   `LH_OVERSEER_TICK_ACT_CONFIRM=YES`), THEN disable `LH-Overseer-Sweep` on
   PTAIT09 (`Disable-ScheduledTask -TaskName LH-Overseer-Sweep`, reversible
   with `Enable-ScheduledTask`).  Never both acting at once.