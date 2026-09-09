# Handoff: migrate the ORCHESTRATOR role from the PC launcher to the chat agent + harness service

## Handoff header

| Field | Value |
|---|---|
| Written | 2026-09-09 |
| Branch | `feat/orchestrator-migration` (created from `origin/main` at `a94008f`; not pushed) |
| Repo that owns it | `LongHorizon-Harness` — the queue, the service-side launcher and the run contract all live here (`src/lh_harness/queue.py`, `src/lh_harness/launcher.py`, `src/lh_harness/webapi/server.py`, `src/lh_harness/mcp_tools.py`) |
| Scope of this document | The plan only. The three secondary code/doc commits it references (idempotency key on enqueue, liveness signal, `docs/queue.md` 17-field contract) are **pending** and are described here, not implemented in this commit. |
| Verified-fact source | `round_001` auditor report (sole authority for repo-surface facts). Every file:line citation below is from `round_001` and was re-derived against the working tree. |

## Why this exists

Today's orchestrator is `launch_queue.py`, a polling script on a Windows workstation that reads JSON files from a local directory, decides a role/model trio, enforces a run cap, and drives the harness through its HTTP API. It is a stopgap. On 2026-09-08 it produced, in one day: three copies of itself running simultaneously and racing the same queue directory (a `pkill` from Git Bash does not kill Windows processes, so each "restart" stacked another); six runs killed at round zero because it had no idea a provider deployment was rate-limited and in cooldown; three hand-edits to the role/model dict as availability moved; and a queue that stops entirely whenever the workstation sleeps, reboots or loses its network. None of these are bugs in the script. They are the consequence of an orchestrator living outside the system it orchestrates.

The PC script itself is **not in this repo and was not read.** Every behaviour below is stated as a **contract the new owner must satisfy**, derived from this repo's API surface (`queue.py`, `launcher.py`, `webapi/server.py`, `mcp_tools.py`). The script is never cited as a source.

The target: the chat agent at `chat.easybutt0n.ai` becomes the enqueue surface, and the **harness service on CT110** owns launching, routing, budgets and reporting. The PC script is decommissioned. A key reframing this plan depends on: after migration the **orchestrator is the harness launcher on CT110, not the chat agent.** The chat agent is only an enqueue client. Therefore chat-agent downtime no longer stalls the queue — the always-on launcher keeps draining it. This decoupling is itself one of the migration's wins.

---

## 1. INVENTORY — every behaviour the PC launcher performs today, as contracts

Each row states the behaviour as a **contract** read from the repo surface, what breaks if it is lost, and the post-migration owner. Citations are `file:line` in `src/lh_harness/`.

### 1.1 Poll the queue for pending work on a fixed interval
- **Contract.** The owner must enumerate pending entries from the durable `QueueStore` on a fixed poll interval, in priority order, and evaluate each. Today: `Launcher._loop` polls every `poll_seconds` (default 15, `launcher.py:107`–`110`); `tick`→`_tick_sync`→`_run_pass` lists entries via `queue_store.list()` (`launcher.py:125`) and processes `status == "pending"` (`launcher.py:131`). `QueueStore.list()` sorts by `(-priority, created_at)` (`queue.py:325`).
- **Breaks if lost.** Pending work sits unseen; the queue stalls. (This is the workstation-sleep failure mode, relocated.)
- **Owner after migration.** Harness launcher on CT110 — **EXISTS TODAY.**

### 1.2 Decide the role/model trio
- **Contract.** The owner must resolve the entry's `trio` (one of `{"kimi","qwen"}`, `_VALID_TRIOS` `queue.py:28`) to an `(agent, model, mcp_profile)` triple from `[queue.trios.<name>]` config and bind it to the three roles. Today: `Launcher._launch` looks up `trios[entry.trio]` (`launcher.py:299`) and builds `role_configs` fanning the **same** `agent/model/mcp_profile` across `manager`, `executor`, `auditor` (`launcher.py:306`–`309`). `queue_config_from_config` (`queue.py:233`–`264`) normalises each trio to `{agent, model, mcp_profile}`. The `trio` name is chosen at **enqueue time** by the requester; the launcher does a static config lookup, not a dynamic per-role decision.
- **Breaks if lost.** Runs launch with no model, the wrong model, or all three roles pinned to one model when they should differ.
- **Owner after migration.** Static resolution = harness launcher — **EXISTS TODAY.** Dynamic per-role, per-availability routing = task 49 — **DOES NOT EXIST** (see §2).

### 1.3 Enforce a concurrency cap
- **Contract.** The owner must never exceed `{trio}_max` active runs per trio. Today: `_remaining_capacity` (`launcher.py:272`–`282`) computes per-trio remaining = `{trio}_max` minus launched-and-active count; `_run_pass` launches **at most one** entry per pass (the `launched` flag, `launcher.py:129`,`133`–`146`) and skips the rest with a capacity reason; `_check_eligibility` returns `"{trio} at capacity"` (`launcher.py:234`–`235`). Defaults `kimi_max=3`, `qwen_max=1` (`queue.py:266`–`267`).
- **Breaks if lost.** Over-subscription burns provider quota/key budget; runs contend and die at round zero.
- **Owner after migration.** Harness launcher — **EXISTS TODAY** (per-trio caps from `[queue.capacity]`).

### 1.4 Refuse to launch two runs into one working tree
- **Contract.** The owner must refuse to launch an entry whose workspace matches any active run's workspace, and the eligibility-check-through-`mark_launched` window must be atomic. Today: `_check_eligibility` normalises the entry workspace and every active run's workspace and returns `"workspace {entry.workspace} has active run {run_id}"` on collision (`launcher.py:236`–`241`). An in-process `threading.Lock` (`_launch_lock`, `launcher.py:78`) serialises that critical section **within one launcher process**.
- **Breaks if lost.** Two runs write into the same git working tree → corrupted branches, racing commits, lost work.
- **Owner after migration.** Harness launcher (hard refusal) — **EXISTS TODAY.** Workspace-contention *fleet warnings* = task 48 — **IN FLIGHT.** The cross-process guarantee (so two orchestrators can't both pass eligibility) — **DOES NOT EXIST** (see §4).

### 1.5 Pass `max_rounds` and the `agent`/`mcp_profile` per role into the run
- **Contract.** The owner must pass `entry.max_rounds` (bounded `1..MAX_ROUNDS`, `_validate_max_rounds` `queue.py:167`–`174`; `MAX_ROUNDS=1000` `types.py:40`, `DEFAULT_MAX_ROUNDS=25` `types.py:41`) and the resolved per-role `agent/model/mcp_profile` into `supervisor.create_run`. Today: `_launch` calls `create_run(..., role_configs=role_configs, max_rounds=entry.max_rounds, mcp_profile=mcp_profile, base_check=entry.base_check or None, prompt_language="en", ...)` (`launcher.py:312`–`322`).
- **Breaks if lost.** Runs launch with the default round budget (25) instead of the requested one, or with the wrong agent/profile, violating per-run budgets or MCP-profile separation.
- **Owner after migration.** Harness launcher — **EXISTS TODAY.**

### 1.6 Move an entry to a terminal state
- **Contract.** The owner must move `launched` entries to `done` or `failed` based on the run's terminal status, reading the durable audit report (not just process liveness). Today: `_update_launched_entries` (`launcher.py:150`–`207`) reconciles launched entries each tick; `mark_done` (`queue.py:386`) on `completed` or `completion_satisfied`; `mark_failed` (`queue.py:395`) otherwise. `_VALID_STATUS = {pending, launched, done, failed}` (`queue.py:27`).
- **Breaks if lost.** Entries stick in `launched` forever; capacity accounting (`_remaining_capacity` counts `launched`-and-active) never frees; the queue head-blocks.
- **Owner after migration.** Harness launcher — **EXISTS TODAY.**

### 1.7 Record the run id against the entry
- **Contract.** The owner must record the `run_id` on the entry atomically with the `pending`→`launched` transition, before any further launch. Today: `mark_launched` (`queue.py:375`–`384`) sets `run_id` and `launched_at` and **raises `ValueError("entry is not pending")` if the entry is not pending** (`queue.py:379`–`380`); called from `_launch` after `create_run` returns a run id (`launcher.py:337`).
- **Breaks if lost.** The link between queue entry and run is lost; reconciliation (`_update_launched_entries`) can't promote the entry; restart re-launches an already-running entry.
- **Owner after migration.** Harness launcher + `QueueStore.mark_launched` — **EXISTS TODAY.**

### 1.8 Retry or requeue a failed entry
- **Contract.** The owner must, on a recoverable launch failure, re-create the work as a new pending entry recording what it retries and which attempt it is, bounded by a configurable cap. Today: the launcher only `mark_failed` (`launcher.py:191`,`325`,`335`) — a failed entry is **terminal**; `record_skip` (`queue.py:403`) appends a reason but keeps the entry `pending` for transient skips, not for hard failures. **There is no re-enqueue.** The dynamic-routing design (task 49, binding decision #2: "bounded relaunch for a refusal before round zero") specifies this exactly — mark the offending model, fail the original with the reason, create a new pending entry recording the retry-of and attempt number, cap default 3, every attempt binding a differing route. That design is **not implemented.**
- **Breaks if lost.** A transient provider refusal (rate limit, cooldown, quota) permanently consumes the entry; an operator must manually re-enqueue. This is the round-zero-kill failure mode's second half.
- **Owner after migration.** **DOES NOT EXIST.** Proposed by task 49 design only.

### 1.9 Survive a restart without losing or double-launching work
- **Contract.** The owner must persist entries durably across API restarts, never re-launch a non-pending entry, and reconcile `launched` entries against active runs on restart. Today: entries are atomic JSON files under `runs_root/queue/` (`_QUEUE_DIR="queue"` `queue.py:23`; `_queue_path` `queue.py:49`–`50`; `_write` via `_atomic_bytes_write` `queue.py:301`–`303`, import `queue.py:20`). The `mark_launched` pending-guard (`queue.py:379`–`380`) prevents re-launching an already-launched entry. On restart the launcher re-evaluates `launched` entries vs. active runs (`_update_launched_entries`). **However:** the only serialization is the in-process `threading.Lock` (`launcher.py:78`); enqueue mints a fresh `q-{uuid.uuid4().hex[:16]}` every time with **no idempotency key** (`queue.py:307`); there is **no cross-process lock or lease.** So a single service instance survives restart cleanly, but two concurrent orchestrators can double-launch.
- **Breaks if lost.** Lost work (entries vanish), or duplicated work (two runs into one task/workspace). The PC script's triple-copy race is the live instance of the latter.
- **Owner after migration.** Durable storage + single-instance no-re-launch — **EXISTS TODAY.** Cross-process no-double-launch — **DOES NOT EXIST** (see §4; the idempotency-key secondary commit is the floor).

### 1.10 Gate `kimi` launches on key health
- **Contract.** The owner must refuse to launch a `kimi` entry when fewer than `min_healthy_keys` healthy keys are reported. Today: `_check_eligibility` (`launcher.py:230`–`233`) calls `_key_health_ok` (`launcher.py:244`–`270`) which GETs `key_health_url`, counts `healthy_keys` with `healthy==true`, and requires `>= min_healthy_keys` (default 2, `queue.py:268`). `qwen` has **no** key-health gate.
- **Breaks if lost.** `kimi` launches into a provider with no healthy Ollama keys → round-zero kills. (This gate is the partial mitigation for the Ollama-key-rate-limit subset of the 2026-09-08 kills; see §7.)
- **Owner after migration.** Harness launcher — **EXISTS TODAY** (`kimi` only; static URL heuristic, not general availability awareness).

### 1.11 Honour priority ordering
- **Contract.** Pending entries must be evaluated highest-priority-first, ties broken by `created_at`. Today: `QueueStore.list()` sorts by `(-priority, created_at)` (`queue.py:325`); `set_priority` (`queue.py:366`); `POST /api/queue/{id}/priority` (`server.py:1099`, pending-only, 409 otherwise).
- **Breaks if lost.** Low-value work jumps the queue; urgent work starves.
- **Owner after migration.** `QueueStore` + launcher — **EXISTS TODAY.**

### 1.12 Record why an entry was skipped (observability)
- **Contract.** The owner must record a skip reason (capacity, workspace, key health, batch-consumed) on the entry and emit a service event, so the fleet can see head-blocking. Today: `record_skip` appends to `skip_reasons` (`queue.py:403`–`410`); `_skip` emits `queue.skipped` to `service_events.jsonl` (`launcher.py:353`–`366`,`397`).
- **Breaks if lost.** Silent head-blocking; operators cannot tell capacity-skip from a stuck launcher.
- **Owner after migration.** Harness launcher — **EXISTS TODAY.**

### 1.13 Allow only pending entries to be removed
- **Contract.** Only `pending` entries may be deleted; launched/done/failed are immutable history. Today: `DELETE /api/queue/{id}` returns 409 for non-pending (`server.py:1057`–`1069`).
- **Breaks if lost.** An in-flight entry is deleted mid-run → orphaned run, lost reconciliation.
- **Owner after migration.** `QueueStore` + REST — **EXISTS TODAY.**

### 1.14 Validate and shape the entry at enqueue
- **Contract.** The enqueue surface must validate `name/task/workspace/trio/max_rounds/priority/base_check/requested_by`, accept `task` or `task_file` (not both), and reject malformed entries with 422. Today: `_normalize_request` (`queue.py:206`–`230`); `POST /api/queue` returns 422 on `ValueError` (`server.py:1024`–`1027`); MCP `harness_enqueue_task` reuses the same `QueueStore.create` (`mcp_tools.py:181`–`198`).
- **Breaks if lost.** Malformed or oversized entries reach the launcher; capacity/eligibility misbehave.
- **Owner after migration.** `QueueStore` + REST + MCP — **EXISTS TODAY.**

### 1.15 Promote terminal state from the durable run report
- **Contract.** Terminal promotion must read the durable audit report, so a cancelled worker that still satisfied completion marks `done`, not `failed`. Today: `_read_run_report` reads `runs_root/{run_id}/lh_harness/report.json` (`launcher.py:209`–`216`); `completion_satisfied` drives `mark_done` (`launcher.py:172`–`179`).
- **Breaks if lost.** A successful audit is masked as a failure; the queue and the fleet disagree on outcome.
- **Owner after migration.** Harness launcher — **EXISTS TODAY.**

---

## 2. OWNER MAP — who owns each row after migration, and whether it exists

Statuses are exactly the three the brief requires: **EXISTS TODAY**, **IN FLIGHT**, **DOES NOT EXIST.** Rows with no owner are called out explicitly — they are the migration's real work.

| # | Behaviour (§1) | Post-migration owner | Status |
|---|---|---|---|
| 1.1 | Poll queue for pending work | Harness launcher (`launcher.py:107`–`148`) | **EXISTS TODAY** |
| 1.2 | Decide trio — static resolve | Harness launcher (`launcher.py:299`–`309`, `queue.py:233`–`264`) | **EXISTS TODAY** |
| 1.2 | Decide trio — dynamic per-role/per-availability routing | Dynamic routing, task 49 | **DOES NOT EXIST** |
| 1.3 | Per-trio concurrency cap | Harness launcher (`launcher.py:234`–`235`,`272`–`282`) | **EXISTS TODAY** |
| 1.4 | One-run-per-working-tree (hard refusal) | Harness launcher (`launcher.py:236`–`241`) | **EXISTS TODAY** |
| 1.4 | One-run-per-working-tree (cross-process guarantee) | — | **DOES NOT EXIST** |
| 1.4 | Workspace-contention fleet warnings | Task 48, PR #8 open (`launcher.py`,`mcp_tools.py`,`server.py`; **not** `queue.py`) | **IN FLIGHT** |
| 1.5 | Pass `max_rounds` + per-role agent/profile | Harness launcher (`launcher.py:306`–`322`) | **EXISTS TODAY** |
| 1.6 | Move entry to terminal state | Harness launcher (`launcher.py:150`–`207`) | **EXISTS TODAY** |
| 1.7 | Record run id against entry | `QueueStore.mark_launched` (`queue.py:375`–`384`) | **EXISTS TODAY** |
| 1.8 | Retry/requeue a failed entry | — (proposed in task 49 design, decision #2) | **DOES NOT EXIST** |
| 1.9 | Restart survival — durable + single-instance no-re-launch | `QueueStore` atomic files + `mark_launched` pending-guard | **EXISTS TODAY** |
| 1.9 | Restart survival — cross-process no-double-launch | — (idempotency key is the floor; lease is the strong form) | **DOES NOT EXIST** |
| 1.10 | `kimi` key-health gate | Harness launcher (`launcher.py:230`–`233`,`244`–`270`) | **EXISTS TODAY** |
| 1.11 | Priority ordering | `QueueStore.list` (`queue.py:325`) | **EXISTS TODAY** |
| 1.12 | Skip-reason observability | Harness launcher (`queue.py:403`–`410`) | **EXISTS TODAY** |
| 1.13 | Pending-only delete | `DELETE /api/queue/{id}` (`server.py:1057`) | **EXISTS TODAY** |
| 1.14 | Enqueue validation | `QueueStore.create` + `_normalize_request` (`queue.py:206`,`305`) | **EXISTS TODAY** |
| 1.15 | Terminal promotion from run report | Harness launcher (`launcher.py:209`–`216`) | **EXISTS TODAY** |
| — | Fleet visibility of queue + launcher liveness | Fleet reporter + fleet window (merged 2026-09-08; gated on env `LH_HARNESS_FLEET_URL`, `server.py:641`) | **EXISTS TODAY** (but heartbeat reports `queue_len=0` hardcoded, `server.py:672`, and no launcher-tick timestamp — see §4) |
| — | Orchestrator liveness signal (real queue depth + launcher tick) | — | **DOES NOT EXIST** (pending secondary commit) |
| — | Single-orchestrator guarantee (cross-process) | — | **DOES NOT EXIST** (pending secondary commit + proposed lease) |
| — | Chat-agent enqueue identity + tool scoping | Hydra + LiteLLM gateway alias `lhharness`, access group `fleet-runners` (`docs/queue.md:12`,`62`) | **EXISTS TODAY** at the gateway; per-caller tool scoping **DOES NOT EXIST** at the harness (see §3, §4) |
| — | Control plane / sole write surface | Hydra | **EXISTS TODAY** (as the control plane) |

### Honest corrections to the original brief

- **Task 49 is NOT "in flight."** `round_001` audited the local branch `feat/dynamic-model-routing` and found it **empty of code — design docs only** (`docs/handoffs/dynamic-model-routing-handoff-2026-09-08.md`, with binding decisions dated 2026-09-08 but no implementation). The owner map therefore marks dynamic routing **DOES NOT EXIST**, not IN FLIGHT. The brief's "not merged, in flight" wording is incorrect and is not repeated as fact anywhere in this plan.
- **Task 48 IS real, open code** (PR #8). It touches `launcher.py`, `mcp_tools.py`, `server.py` but **not** `queue.py`. So `queue.py` is collision-safe for the idempotency-key secondary commit; `server.py` and `launcher.py` are contested and any liveness/lease change there must be minimal and must declare the collision.

### Rows with NO owner (the migration's real work)
1.8 retry/requeue · 1.9 cross-process no-double-launch · orchestrator liveness signal · single-orchestrator lease · per-caller tool scoping at the harness · shared-infrastructure interlock · chat-agent per-hour ceiling and `max_rounds` clamp. These are expanded in §3 and §4.

---

## 3. THE CHAT AGENT'S CONTRACT — the safety boundary

The chat agent is an AI holding an API key that can cause code to be written, tests to run, and deployments to happen. This section is written **as if a confused or adversarial caller is on the other end.** Every "may" is the complete list; every "may never" is enforced by something other than the agent's good behaviour.

### 3.1 What the chat agent MAY call
- `POST /api/queue` — enqueue a run. Equivalently MCP tool `harness_enqueue_task` (`mcp_tools.py:181`), gateway alias `lhharness`.
- `GET /api/queue` (optionally `?status=…`) — read the queue. Equivalently `harness_list_queue` (`mcp_tools.py:201`).
- `GET /api/runs/{run_id}/status` — read a run's status. Equivalently `harness_run_status` (`mcp_tools.py:217`).
- `GET /api/queue/config` — read the effective `[queue.*]` trios/capacity (`server.py:1116`), read-only, so the agent knows which trios exist and what `max_rounds` bounds are.
- **"Set a route" — DOES NOT EXIST today and is NEW WORK.** When dynamic routing (task 49) lands, a route override becomes a **field on the queue entry** set through the API/Hydra/chat (task 49 design, decision #4: "an override is a field on the queue entry, never code"). Until it exists, the chat agent may **only** choose `trio ∈ {kimi, qwen}` at enqueue; it may not name a model per role. This plan flags the route-setting capability as new work; it is not assumed present.

### 3.2 What the chat agent may NEVER call
These endpoints/tools exist on the harness today; the chat agent is forbidden them by policy, and that policy must be **enforced at the gateway access group**, not by the agent's restraint (see §3.4):
- `POST /api/runs/{run_id}/abort` (`server.py:1544`) — never abort another owner's run.
- `POST /api/runs/{run_id}/stop` (`server.py:1554`) / `POST /api/runs/{run_id}/resume` (`server.py:1564`) — never stop or resume another owner's run.
- `POST /api/runs/{run_id}/approvals/{approval_id}/resolve` (`server.py:1502`) and MCP tool `harness_resolve_gate` (`mcp_tools.py:240`) — **never resolve an approval gate.** Gates decide, not the orchestrator (see §3.6).
- `POST /api/runs` (`server.py:1139`) — never create a run directly; the chat agent enqueues, and the launcher decides when to launch. Direct run creation bypasses capacity, eligibility, and the one-run-per-working-tree rule.
- Any mutation of `[queue.capacity]` budgets (`kimi_max`, `qwen_max`, `min_healthy_keys`) or trio model mappings — there is no runtime endpoint for this (config is file-based, `PROJECT_CONFIG_PATH`); the chat agent must not edit config or deploy. Budget changes are a human action (§3.5).
- Any deploy endpoint — production deploys go through deployment lanes, never the queue (`mcp_tools.py:35`–`36`).
- `DELETE /api/queue/{id}` on an entry it did not create — the chat agent may delete only its own `pending` entries (identified by `requested_by`); deleting another owner's work is a human action.

### 3.3 Key scoping and access groups (env NAMES only — no credential values)
- The chat agent authenticates with the same bearer-token boundary as all `/api/*` (`mcp_tools.py:150`–`152` auth parity: `auth_token == request_token`). The token itself is referenced **by name only** — its value lives in env on the router/gateway boxes, never in this repo or this plan.
- The chat agent reaches the queue through the LiteLLM MCP gateway alias **`lhharness`**, access group **`fleet-runners`** (`docs/queue.md:12`,`62`; `mcp_tools.py:3`–`4`). The gateway scope must grant `fleet-runners` only the enqueue/list/status tools — **not** `harness_resolve_gate`, not run-control (`abort`/`stop`/`resume`), not `POST /api/runs`.
- The chat agent must **not** hold the `audit` MCP profile (`mcp_profiles.py:34`, read-only audit tooling) nor any run-control scope. It uses the `default`-class fleet access via the gateway. Conflating profiles weakens the auditor lock and MCP-profile separation, which this plan does not touch.
- Env names referenced (values never): the gateway/API key material in env on the router boxes; `LH_HARNESS_FLEET_URL` (`server.py:641`) for the fleet reporter; `ANTHROPIC_BASE_URL` (points every agent at the LiteLLM router).

### 3.4 The harness does NOT restrict which caller may call which tool today — this is a gap
`mcp_tools.dispatch` checks **only** the bearer token, not the caller's identity or access group (`mcp_tools.py:150`–`162`). `harness_resolve_gate` is in the manifest (`mcp_tools.py:120`) and is dispatched to any authenticated caller. **Therefore the "never call" list in §3.2 is currently enforceable only at the gateway**, by not exposing `harness_resolve_gate` and the run-control endpoints to the `fleet-runners` access group. That gateway scoping is **NEW WORK** — the harness has no per-caller tool restriction today. Until it lands, the chat agent must not be given the raw bearer token; it must go through the gateway with a scoped access group. This plan does not weaken any existing protection; it calls out that the protection is currently gateway-side, not harness-side.

### 3.5 Hard ceilings so a loop cannot spend the fleet's budget
Both are **NEW WORK** (the harness enforces neither today):
- **Per-hour entry ceiling.** A hard cap on entries the chat agent may create per rolling hour, keyed by `requested_by`. Enforce at `POST /api/queue`: maintain a rolling-hour count per `requested_by` and return **429** over the cap. Proposed default: **10 entries/hour per `requested_by`** (to be set in config, not hardcoded). The PC script had no such cap; a confused agent loop could flood the queue.
- **Maximum `max_rounds`.** Today `_validate_max_rounds` allows `1..1000` (`queue.py:167`–`174`). The chat agent must be clamped **below** `MAX_ROUNDS`. Proposed chat-agent ceiling: **`max_rounds ≤ 50`** (NEW WORK: a per-caller clamp at enqueue, configured separately from the global `MAX_ROUNDS=1000`). A request above the clamp is rejected with 422, not silently truncated.

Both ceilings must be enforced server-side at the enqueue boundary, never trusted to the agent. Combined with the idempotency key (§4.2), a retry loop that re-sends the same logical request is de-duplicated, and a divergent loop is throttled to the per-hour ceiling.

### 3.6 Gates decide, not the orchestrator — the chat agent must never acquire gate-bypass power
- Production deploys through the deployment lanes are **already auto-approved by standing decision** (`mcp_tools.py:35`–`36`). "Auto-approved" means the **gate's policy auto-resolves** — it does **not** mean the orchestrator skips the gate. The chat agent must not acquire the power to bypass, pre-resolve, or suppress a gate.
- The chat agent must **never** call `harness_resolve_gate` / `POST /api/runs/{id}/approvals/{id}/resolve`. Resolving a gate (continue / stop / extra_rounds / user instruction) is a human-or-lane-policy decision. Even an auto-approved prod lane is gate-mediated; the chat agent's role ends at enqueue.
- This is stated as a hard rule, not a principle: the gateway scope in §3.4 is the enforcement. If the gateway cannot currently hide `harness_resolve_gate` from `fleet-runners`, that is a **blocker** on promoting the chat agent to authoritative (§5), not a relaxation.

### 3.7 Actions that still require a human (a list, not a principle)
1. Resolving any approval gate (continue / stop / extra_rounds / user instruction).
2. Aborting or stopping another owner's run.
3. Resuming another owner's run.
4. Changing `[queue.capacity]` budgets or trio model mappings (config edit + deploy).
5. Restarting a shared infrastructure dependency (LiteLLM router, Ollama, DB) while a gate is mid-flight — until the §4.1 interlock exists, a human must check for in-flight gates first.
6. Deploying outside the standing-auto-approved prod lanes (emergency / non-lane deploys).
7. Editing MCP profiles or the auditor lock.
8. Deleting another owner's queue entry.
9. Pulling/loading a local model (task 49 design, decision #3: never pull; a host-level change requires explicit human go).
10. Decommissioning the PC launcher (the final cutover decision, §6).

---

## 4. GAPS WITH NO OWNER

### 4.1 Shared-infrastructure interlock
**What happened.** On 2026-09-08 the prod LiteLLM router (`https://litellm.easybutt0n.ai`, the router every harness agent's `ANTHROPIC_BASE_URL` points at) was restarted during a promotion's eval gate. The restart poisoned the router's retries mid-gate. Dynamic routing (task 49) handles **model availability** but has **no concept of "a gate is running, do not touch this dependency."** Nothing today prevents an operator or agent from restarting a shared dependency while a gate is mid-flight.

**Proposed home: Hydra**, as the control plane and the only write surface. The harness knows which runs have open gates (approvals surface in the run snapshot, `server.py:687`–`695`); Hydra is the natural place to hold the "do not touch" lock because it already mediates writes and fleet operations.

**What the check looks like (NEW WORK).** Before restarting a shared dependency, Hydra runs a pre-flight: it queries the harness for in-flight gates — runs whose snapshot contains an unresolved approval (status `waiting_approval` or an open approval_id) — that depend on that dependency (router, Ollama endpoint, DB). If the set is non-empty, Hydra **refuses the restart** (or requires an explicit, recorded override that names who overrode and which gates were at risk). Concretely:
- A read-only harness endpoint or snapshot filter exposing "runs with open gates" (the data already exists in the snapshot's `approvals` list; a thin `GET /api/fleet/in_flight_gates` aggregator is NEW WORK).
- A Hydra-side rule keyed by dependency that blocks restart while the set is non-empty, with a recorded override.
This is labelled **new work** — neither the harness endpoint nor the Hydra rule exists today. It does not weaken any protection; it adds one.

### 4.2 Single-orchestrator guarantee
**The PC script had none, and ran three times at once.** Whatever replaces it must make concurrent orchestrators **impossible or harmless** — enforced mechanically, not by convention.

**Audited truth.** Today the only serialization is an in-process `threading.Lock` (`launcher.py:78`) — it protects one launcher process's ticks, not two processes. Enqueue mints a fresh uuid with no idempotency (`queue.py:307`). `POST /api/runs` (create_run) **does** take an `Idempotency-Key` header and raises `IdempotencyConflict`→409 on reuse (`server.py:1180`,`1182`; `supervisor/service.py:403`,`1406`,`1448`) — but `POST /api/queue` does **not** (`server.py:1020`–`1028`). So the harness already has an idempotency convention; the queue enqueue path simply doesn't use it.

**Proposal — make concurrent orchestrators harmless (floor) and then impossible (ceiling):**
- **Floor (harmless): idempotency key on enqueue.** Accept an `Idempotency-Key` header on `POST /api/queue`, validate it with the existing `_bounded_command_id`, and reject/de-duplicate a duplicate key — mirroring `create_run` exactly. Two orchestrators sending the same logical request then produce one entry, not two. This is the **pending secondary commit** (see Appendix A.1). It touches `queue.py` (collision-safe per `round_001` — task 48 does not touch `queue.py`) and a thin header read in `server.py` (contested — keep to reading the header and passing it through, no eligibility-gate change). The `mark_launched` pending-guard (`queue.py:379`–`380`) already prevents re-launching a launched entry, so idempotency on enqueue + the pending-guard together make double-launch of the *same* entry impossible.
- **Ceiling (impossible): a launcher lease.** A lease record (file or DB row under `runs_root/queue/`) owned by exactly one launcher process, with a TTL shorter than `poll_seconds` and a fencing token. A second launcher that starts sees the lease held and stands down (or alarms). Every `create_run`/`mark_launched` carries the fencing token so a stale launcher's write is rejected. This makes a second authoritative launcher impossible, not just harmless. **NEW WORK**; it touches `launcher.py` and therefore **collides with task 48** — it must be narrow (a lease check at tick start + a fencing token on writes) and must not touch the eligibility gate (`launcher.py:223`–`242`).

The guarantee is "harmless via idempotency (this branch) + impossible via lease (new work, task-48-colliding)." Convention ("only run one launcher") is explicitly rejected as the enforcement.

### 4.3 Queue durability and ownership
- **Where entries live after migration.** Entries are atomic JSON files under `runs_root/queue/` on **CT110** (`queue.py:43`–`50`,`301`–`303`), durable across API restarts and visible to any bearer-holding client. They are no longer files on a sleepable workstation. The runs root on CT110 is durable storage, backed up as part of the node, not a single PC's disk.
- **In-flight entries during cutover.** Entries the PC script already POSTed to the harness are already in the `QueueStore` (durable). Entries that exist **only** in the PC script's local directory (not yet POSTed) are the cutover risk: they must be drained/forwarded into the `QueueStore` during the shadow period (§5 Step 3), so none are orphaned. After the drain, the PC's local directory is archived (§6), not trusted as a source of truth.
- **No-double-launch property.** Three layers: (a) `mark_launched` pending-guard (`queue.py:379`–`380`) — a launched entry cannot be re-launched; (b) idempotency key on enqueue (§4.2 floor) — the same logical request yields one entry; (c) the eligibility check + proposed lease (§4.2 ceiling) — two orchestrators cannot both pass eligibility for the same workspace. Today only (a) exists.

### 4.4 Who watches the watcher
**If the orchestrator is down, work stops silently.** Define the signal that says it is alive and where it surfaces.

**Audited truth.** The fleet reporter registers a heartbeat callback `_heartbeat` (`server.py:655`–`676`) that returns `(runs, active, active_cap, queue_len)` — but `queue_len` is **hardcoded 0** (`server.py:672`, comment: "The queue length is not tracked by the supervisor; report 0"), and there is **no launcher-tick timestamp** surfaced. The reporter is gated on env `LH_HARNESS_FLEET_URL` (`server.py:641`) and merged to main 2026-09-08. So a heartbeat exists, but it does not actually say whether the launcher is ticking or how deep the queue is.

**Proposal (pending secondary commit, see Appendix A.2).** Extend the signal minimally:
- The launcher writes a **last-tick timestamp** each tick. To avoid editing contested `launcher.py`/`server.py` logic, write it to a **new file** `runs_root/queue/launcher_heartbeat.json` (timestamp + tick count + fencing-token/lease owner) — an additive write, not a change to the eligibility gate or the `_heartbeat` callback's existing return shape beyond reading this file.
- `_heartbeat` returns the **real** `queue_len` from `queue_store.counts()` (`queue.py:412`) instead of the hardcoded 0, plus the launcher's last-tick timestamp.
- The fleet window surfaces: queue depth, launcher last-tick age, and lease owner. **"Orchestrator alive"** = last-tick age `< 2 × poll_seconds` AND queue depth reflects reality. **"Orchestrator stale/dead"** = last-tick age `>= 2 × poll_seconds` → the fleet window shows a degraded/stale state and alerts a human.

**Reframing (a migration win).** Because the orchestrator is the always-on CT110 launcher, not the chat agent, **chat-agent downtime does not stop work** — the launcher keeps draining the queue. The liveness signal watches the *launcher*, which is the thing that can actually stall. This is the direct fix for "queue stops whenever the workstation sleeps."

### 4.5 Additional gaps the inventory surfaces
- **No per-caller tool scoping at the harness (§3.4).** `harness_resolve_gate` is callable by any authenticated caller. Gateway scoping is new work and a blocker on chat-agent promotion.
- **No retry/requeue (§1.8).** Failed entries are terminal; transient provider refusals consume entries permanently until task 49's bounded relaunch is built.
- **No chat-agent budget ceilings (§3.5).** No per-hour entry cap, no per-caller `max_rounds` clamp. Both new work.

---

## 5. CUTOVER SEQUENCE — ordered, each step with its own rollback, executable during normal operation

No big-bang switch. The new orchestrator runs **observed but not authoritative** during a shadow period, and is promoted only on named evidence. The migration is executable during normal operation because each step is additive or reversible; no step requires a quiet window.

### Step 0 — Archive + baseline (read-only)
- Archive the PC script, its companion watchers, their config (role/model dict, capacity, poll interval, queue-directory path, API base URL), and a sample of the PC's local queue-directory entries (see §6 archive-first list).
- Record baseline: `QueueStore.counts()`, PC local-queue depth, time-to-launch, and the 2026-09-08 six-failure signatures (so recurrence is detectable).
- **Rollback.** N/A (read-only).

### Step 1 — Deploy the additive safety floor (no behaviour change)
- Merge the three pending secondary commits (Appendix A): idempotency key on `POST /api/queue`, liveness signal (real `queue_len` + launcher tick), and `docs/queue.md` 17-field contract. The PC script keeps running unchanged; it does not yet send an `Idempotency-Key`, so behaviour is identical.
- **Rollback.** Revert the three commits. No data impact (idempotency only affects callers that send the key).

### Step 2 — Stand up the harness launcher in OBSERVE (shadow) mode
- The harness launcher ticks and computes the **full** decision (which entry, which trio, capacity, key-health, workspace-collision) and emits **shadow** events (`queue.shadow_launch` / `queue.shadow_skip`) — but does **not** call `create_run`. The PC script remains the sole authoritative launcher. **Both orchestrators run; the new one is observed, not authoritative.**
- Observe-mode is a narrow, additive launcher flag that short-circuits **before** `_launch` (`launcher.py:298`); it does **not** touch the eligibility gate (`launcher.py:223`–`242`) or the one-run-per-working-tree rule. It is **NEW WORK** and **collides with task 48** (touches `launcher.py`) — kept to a mode flag, declared here, not implemented in this commit.
- **Rollback.** Stop the launcher / flip observe off. The PC script is unaffected.

### Step 3 — Dual-source the queue (drain the PC's local queue)
- The chat agent (and/or Hydra) begins enqueuing real work into the `QueueStore` via `POST /api/queue`. The PC script is reconfigured to **enqueue** (`POST /api/queue`) instead of launching from its local directory, and/or a one-shot forwarder drains the PC's local directory into the `QueueStore`. The launcher (still in observe) now shadows **all** entries. The PC script still launches anything it still owns; the launcher observes.
- Every forwarded entry carries an `Idempotency-Key` derived from the PC's local entry id, so a forwarder re-run cannot duplicate.
- **Rollback.** Stop forwarding; the PC script resumes its local-directory launching; stop chat-agent enqueues.

### Step 4 — Promote the launcher to authoritative (the handoff)
- **Promotion evidence (all must hold over the shadow window, e.g. 7 days):**
  1. **Decision agreement.** The shadow launcher's would-launch decisions agree with the PC script's actual launches on every entry: same entry selected, same trio, same skip reasons. Zero unexplained divergence.
  2. **Zero phantom launches.** The shadow launcher created no runs during the shadow period.
  3. **Zero missed launches.** No entry the PC launched that the shadow would have skipped without a recorded capacity/workspace/key reason; and no entry the shadow would have launched that the PC dropped.
  4. **Liveness through the PC's failure modes.** The launcher tick signal stayed green (age `< 2 × poll_seconds`) across at least one CT110 reboot and one network blip — proving the workstation-sleep/reboot/network stall is gone.
  5. **No double-launch.** The idempotency key rejected every duplicate enqueue as expected; the `mark_launched` pending-guard held; zero duplicate runs.
  6. **Queue latency.** Time-to-launch under the (shadow) launcher is ≤ the PC baseline.
- When the evidence holds: enable `create_run` in the launcher (flip observe off) **and simultaneously** stop the PC script from launching (it enqueues only, like the chat agent). The launcher is now the sole launcher; the PC script and the chat agent are both enqueue clients.
- **Rollback.** Disable `create_run` in the launcher (back to observe); the PC script resumes launching. Reverting is one flag flip because the PC script and the launcher are both still present.

### Step 5 — Decommission (see §6)
- Once Step 4 is stable for the criteria window, stop the PC script entirely. The chat agent + Hydra are the enqueue sources; the CT110 launcher is the orchestrator. Archive (§6).

---

## 6. DECOMMISSION CRITERIA — when the PC scripts are switched off for good

The PC scripts (`launch_queue.py` and its companion watchers) are switched off **only when all of the following hold for the criteria window (proposed 7 days)** after Step 4 promotion:

1. The harness launcher has been the sole authoritative launcher for the window with **zero rollback** to the PC script.
2. Shadow + authoritative decision agreement held throughout (§5 Step 4 evidence #1) with no unexplained divergence.
3. The launcher liveness signal was green for the entire window, **including across at least one CT110 reboot and one network blip** — the workstation-bound failure modes are proven gone.
4. **Zero double-launch** incidents: idempotency rejected duplicates as expected; the pending-guard held; no two runs into one workspace from the launcher.
5. The chat agent and/or Hydra were the sole enqueue sources for the final period; the PC script enqueued nothing (or was off).
6. The 2026-09-08 failure signatures did not recur for the causes this migration owns: no concurrent-orchestrator race; no queue stall from a sleepable/rebootable/network-bound host; no hand-edits to a role/model dict (the trio is config-driven, changed via a documented config edit + deploy, not a script edit). (Note: round-zero kills from provider cooldown are **not** closed by this migration — see §7 — so their non-recurrence is **not** a decommission criterion; it depends on task 49.)
7. All in-flight entries from the PC's local queue were drained into the `QueueStore` and reached a terminal state (`done`/`failed`); none orphaned in the PC's local directory.

### Archive-first list (so the behaviour contract is not lost with the scripts)
Archived **before** the scripts are deleted, in this order:
1. `launch_queue.py` and every companion watcher script, with their config files (the role/model dict, capacity settings, poll interval, queue-directory path, API base URL).
2. A sample of the PC's local queue-directory entries (the JSON shape the PC used) — the entry contract the PC fulfilled, recoverable.
3. The PC script's launch/decision log for the shadow period — the ground truth the shadow launcher was compared against (§5 evidence #1).
4. The trio/model mapping as of cutover — the final state of the hand-edited dict, so the static `[queue.trios]` config the launcher now uses is traceable to the PC's last decisions.
5. This migration plan and `docs/queue.md` (the contract the chat agent codes against).
6. The idempotency-key, liveness-signal, and lease design notes (§4).

---

## 7. What this migration does NOT fix — honest residual risk (the six-failures answer)

The six run failures of 2026-09-08 are the **six runs killed at round zero** because the orchestrator had no idea a provider deployment was rate-limited and in cooldown. Asked honestly which of those six would still have happened after *this* migration:

**Not "none."** This migration relocates the orchestrator to the always-on CT110 launcher, adds the idempotency key, the liveness signal, and the queue contract. It fixes the *other* 2026-09-08 failures — the triple-copy race (idempotency + single launcher on CT110), the workstation-sleep/reboot/network stalls (always-on launcher), the hand-edits to the role/model dict (config-driven trio), and double-launch (idempotency + pending-guard). But the round-zero kills are an **availability-awareness** failure, and this migration does **not** add availability awareness.

The harness launcher today decides launches from: the static trio config (`kimi`/`qwen` → fixed `agent/model/mcp_profile`), per-trio capacity caps, the one-run-per-working-tree rule, and — for `kimi` only — the `key_health_url` gate (`launcher.py:230`–`233`, requires `>= min_healthy_keys`). That gate **would** catch the Ollama-key-rate-limit subtype (six of seven keys rate-limited → one healthy `< 2` → "key health insufficient" skip, not a kill), so the subset of the six driven by Ollama key rate-limit would likely **not** have been killed — they would have been skipped and held pending. But the gate is `kimi`-only and is a key-count heuristic; it does **not** detect a provider deployment in cooldown (the brief's stated cause), the auditor's monthly cap (task 49 design doc: three runs lost to `provider_provider_error` before round zero), Synthetic quota exhaustion, or any non-Ollama refusal. Those subtypes would **still die at round zero.**

The work that would actually prevent the round-zero kills — dynamic routing's "bounded relaunch for a refusal before round zero" on a differing route (task 49, decision #2), plus a run that "cannot start must say why" — is **unbuilt (DOES NOT EXIST)**, and the failed-entry behaviour today is terminal (`mark_failed`, no re-enqueue, §1.8). So at minimum the three auditor-monthly-cap runs the design doc names would still have happened, and the provider-deployment-cooldown runs would still have happened. The migration makes such failures **visible sooner** (the launcher records a failure reason; terminal promotion reads the run report, so a pre-round failure now leaves a recorded reason rather than an empty run) — but visibility is not prevention.

**Honest answer:** a meaningful fraction of the six — at minimum the three auditor-cap runs and any provider-deployment-cooldown runs — would still have happened. The migration fixes the orchestration-location and concurrency failures; it explicitly does **not** fix the availability-blindness failure, which is task 49's to own and is unbuilt. Claiming "none" would hide that task 49 is the real owner of this failure and is not yet built.

---

## Appendix A — Pending secondary commits (referenced, not implemented in this commit)

These three are the secondary deliverables. They are described here so later rounds implement them as narrow, individually-auditable commits on this branch. None is implemented by this commit.

### A.1 Idempotency key on queue entry creation (with duplicate-rejection tests)
- Accept an `Idempotency-Key` header on `POST /api/queue` (`server.py:1020`), validate via the existing `_bounded_command_id`, and reject/de-duplicate a duplicate — mirroring `create_run` (`server.py:1180`,`1182`; `supervisor/service.py:403`,`1448`).
- Touches `queue.py` (collision-safe — task 48 does not touch `queue.py`) and a thin header read in `server.py` (contested with task 48 — pass-through only, no eligibility-gate change).
- Tests: a duplicate enqueue with the same key is rejected/de-duplicated; distinct keys produce distinct entries; the `mark_launched` pending-guard still prevents re-launch. Hermetic `make test` (loopback `TestClient`, no live calls).

### A.2 Orchestrator liveness signal
- Launcher writes `runs_root/queue/launcher_heartbeat.json` (timestamp + tick count + lease owner) each tick — additive file write, **not** an edit to the eligibility gate.
- `_heartbeat` (`server.py:655`–`676`) returns real `queue_len` from `queue_store.counts()` (`queue.py:412`) instead of the hardcoded 0 (`server.py:672`), plus the last-tick timestamp.
- Touches `server.py` (contested with task 48) — kept to reading the heartbeat file and replacing the `0` literal; declared collision, kept narrow.

### A.3 `docs/queue.md` — the 17-field queue entry contract
- Extends the existing `docs/queue.md` (which today lists only the 8 enqueue request fields) to enumerate **all 17** `QueueEntry` fields (`queue.py:57`–`73`): `queue_id`, `name`, `task`, `workspace`, `max_rounds`, `trio`, `priority`, `requested_by`, `base_check`, `status`, `run_id`, `reason`, `skip_reasons`, `created_at`, `updated_at`, `launched_at`, `last_checked_at` — each with type, default, validation rule, and who writes it. This is the interface the chat agent codes against.

---

## Appendix B — Verified facts relied upon (from `round_001`, re-derived against the working tree)

- `QueueEntry` dataclass has **17 fields** at `queue.py:53`–`73`; `_VALID_STATUS = {pending, launched, done, failed}` (`queue.py:27`); `_VALID_TRIOS = {kimi, qwen}` (`queue.py:28`); `_QUEUE_DIR = "queue"` (`queue.py:23`).
- `QueueStore.create` mints `q-{uuid.uuid4().hex[:16]}` at `queue.py:307` with **no idempotency, dedup, lease, or lock** anywhere in `queue.py`.
- `mark_launched` (`queue.py:375`–`384`) raises if the entry is not pending (`queue.py:379`–`380`); `mark_done` (`queue.py:386`), `mark_failed` (`queue.py:395`), `record_skip` (`queue.py:403`), `set_priority` (`queue.py:366`), `counts` (`queue.py:412`).
- `launcher.py`: in-process-only `_launch_lock = threading.Lock()` at `launcher.py:78`; `_run_pass` `launcher.py:124`; `_update_launched_entries` `launcher.py:150`; `_read_run_report` `launcher.py:209`; `_check_eligibility` `launcher.py:223` with the workspace-collision message at `launcher.py:241`; `_key_health_ok` `launcher.py:244`; `_remaining_capacity` `launcher.py:272`; `_launch` `launcher.py:298` (carries `role_configs` and `max_rounds=entry.max_rounds`, `launcher.py:318`).
- `webapi/server.py`: `POST /api/queue` via `create_queue_entry` at `server.py:1020` (no idempotency); `GET /api/queue` `server.py:1030`; `DELETE /api/queue/{id}` `server.py:1057`; `POST /api/queue/{id}/priority` `server.py:1099`; `GET /api/queue/config` `server.py:1116`; `POST /api/runs` `server.py:1139` (takes `Idempotency-Key` at `server.py:1180`, `IdempotencyConflict`→409 at `server.py:1182`); `POST /api/runs/{id}/approvals/{id}/resolve` `server.py:1502`; `POST /api/runs/{id}/abort` `server.py:1544`; `stop` `server.py:1554`; `resume` `server.py:1564`; `_heartbeat` callback `server.py:655`–`676` with `queue_len` hardcoded 0 at `server.py:672`, gated on env `LH_HARNESS_FLEET_URL` at `server.py:641`.
- `mcp_tools.py`: exactly four tools — `harness_enqueue_task` (`:154`), `harness_list_queue` (`:156`), `harness_run_status` (`:158`), `harness_resolve_gate` (`:160`); `dispatch` checks only the bearer token (`:150`–`152`), no per-caller scoping; `_resolve_gate` ASCII-only enforcement (`:254`).
- `types.py`: `MAX_ROUNDS = 1000` (`:40`), `DEFAULT_MAX_ROUNDS = 25` (`:41`); `_validate_max_rounds` `1..MAX_ROUNDS` (`queue.py:172`).
- `mcp_profiles.py`: `audit` profile (`:34`, read-only), `default` profile (`:39`); `_default_profile_for_role` → auditor→`audit` (`:263`), else `default` (`:266`).
- `supervisor/service.py`: `class IdempotencyConflict` (`:403`); `create_run` idempotency (`:1406`,`:1448`).
- Fleet reporter merged to main 2026-09-08; gateway alias `lhharness`, access group `fleet-runners` (`docs/queue.md:12`,`62`).
- Task 48 (workspace contention): real, open PR; touches `launcher.py`, `mcp_tools.py`, `server.py`; **not** `queue.py` — so `queue.py` is collision-safe, the other three are not.
- Task 49 (dynamic routing): **EMPTY local branch — design docs only** (`docs/handoffs/dynamic-model-routing-handoff-2026-09-08.md`); no implementation. Marked **DOES NOT EXIST**, correcting the original brief's "in flight" wording.
- Hermetic test target: `make test` using loopback `TestClient`, no live gateway/API calls.
- Protections preserved unchanged by this plan: the auditor lock (`audit` MCP profile), MCP profiles, per-run budgets (`max_rounds` `1..1000`), and the one-run-per-working-tree rule (`launcher.py:236`–`241`). Nothing in this plan weakens any of them.
