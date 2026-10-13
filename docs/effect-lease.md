# Effect Lease — single-family writer enforcement at the harness boundary

**Status:** design + implementation proposal. The lease layer is **OFF by
default** in this PR. Nothing changes for the running VM211 deployment until
an operator flips `LH_HARNESS_EFFECT_LEASE_ENABLED` to `true` and performs
the first recorded handoff.

## 1. Problem

The six hosted runtimes come in three *families*:

| family      | primary     | pair        |
|------------|------------|------------|
| overseer    | overseer1  | overseer1-op  |
| infraops    | infraops1  | infraops1-op  |
| devspecops  | devspecops1 | devspecops1-op |

Paxton's design (via `codex@ptait09`): within a family, **exactly one runtime
may cause effects at a time**. A quota-exhausted or stalled holder must hand
the writer role to its pair.

Today the only artefact of that rule is an advisory file on VM211
(`agents/orchestrator/overseer-switch.json`, fields `holder`, `handoff`).
It is read by every agent and the per-process `defer_to_holder` logic uses it as
a hint, but **no mutating endpoint or MCP tool of the harness refuses a
call from the non-holder.** A prompt, a static key scope, and a JSON file
are not enforcement.

The harness is where gate resolutions, queue writes, run instructions, and
stops are accepted — that is the boundary we now close.

## 2. The model

### 2.1 Family

A **family** is a fixed string, one of `overseer`, `infraops`, `devspecops`.
A family contains two runtime ids (primary + pair). A runtime id is the
authenticated *caller name* resolved from the harness HMAC headers (REST) or
arguments (MCP) by `caller_auth.resolve_rest_caller` /
`resolve_mcp_caller`. The runtime id is **never** read from a self-declared
field on the request body.

### 2.2 Lease record (per family)

Stored durably in the harness settings DB. Exactly one record per family.

| field         | meaning                                                                                |
|--------------|---------------------------------------------------------------------------------------|
| `family`     | one of `overseer`, `infraops`, `devspecops`                                           |
| `holder`     | the runtime id that currently owns the writer role for this family                    |
| `epoch`      | integer `>= 1`, monotonically increasing per successful `acquire` or `takeover`; never reused |
| `acquired_at`| ISO-8601 UTC timestamp of the latest acquire/renew/takeover                           |
| `expires_at` | ISO-8601 UTC timestamp; a takeover is allowed after this moment if `reason == "expired"` |
| `reason`     | free-text tag (≤128 chars) the acquirer must set; e.g. `initial`, `renew`, `quota`, `timeout`, `operator` |

The lease is **per family, not per writer**: an `infraops1` host running
overseer-shaped work is NOT covered by an overseer lease. The mapping
runtime-id → family comes from the caller's configured `family` field
(`[callers] <name>.family`). The mapping is consulted only to decide
*which* lease applies; the *holder* of the lease is the runtime id itself.

### 2.3 Operations

All four operations are **compare-and-swap (CAS) on `epoch`**: the row
update includes a `WHERE epoch = :observed_epoch` clause so that two
concurrent CAS attempts cannot both succeed — exactly one wins.

| op         | a401 | effect                                                                                   | result |
|------------|------|----------------------------------------------------------------------------------------|--------|
| `acquire`  | family is unlocked (no row) OR row is expired                                       | insert row with `epoch = previous_epoch + 1` (or `1` if none); set `holder`, `acquired_at = now`, `expires_at = now + ttl`, `reason` |
| `renew`    | row exists AND `holder == self` AND `epoch == observed_epoch` AND `expires_at > now` | same holder; `epoch` unchanged; `acquired_at = now`; `expires_at = now + ttl` |
| `release`  | row exists AND `holder == self` AND `epoch == observed_epoch`                       | delete row (next acquire starts at `epoch = 1` again) |
| `takeover` | row exists AND caller is the *pair* of the current holder AND `epoch == observed_epoch` AND (`reason == "expired"` OR external evidence threshold met) | bump `epoch = epoch + 1`; set `holder = self`, `acquired_at = now`, `expires_at = now + ttl`, `reason` |

A `takeover` is the same SQL CAS as the others — it just gets to bump the
epoch past the previous holder rather than fail. Concurrent `takeover`
attempts race: one wins, the other gets 409 with the post-CAS epoch in the
response.

### 2.5 Default TTL

`ttl_seconds` defaults to `60`. The holder is expected to call `renew`
within `ttl_seconds` of `acquired_at`; on expiry, the pair may `takeover`
with `reason = "expired"`. There is no background sweeper: the lease state
is consulted at every CAS.

## 3. Takeover rules (quota + timeout fallback)

| fallback      | evidence the *pair* must present                                                  | who may take over                              |
|---------------|-------------------------------------------------------------------------------|------------------------------------------------|
| **quota**     | the holder's last `X-Run-Token-Remaining` or rate-limit response in the last 60 s was `<= 0` (recorded as `quota_event.jsonl` in the runs root by the holder itself; the pair reads it via a new MCP tool `harness_lease_quota_evidence`) | the *pair* runtime id only                     |
| **timeout**   | no `renew` from the holder within `2 * ttl_seconds` (`expires_at < now - ttl`) | the *pair* runtime id only                     |
| **expired**   | `now >= expires_at` and reason was `renew`                                     | the *pair*                                     |
| **operator**  | signed operator command (`LH_HARNESS_OPERATOR_SECRET` HMAC) recorded as `takeover(operator)` | any authenticated caller whose `family` matches |

The previous holder learns it lost the lease on its next `renew`: the CAS
fails (`epoch` changed), the response is `409 lease_lost`, and the holder
stops calling effects for that family. There is no push notification; the
holder's polling pattern (`acquire → renew → renew → release`) makes a
next-attempt refusal safe.

A takeover that happens for `quota` or `timeout` is logged with the
evidence path so an auditor can replay why the holder lost the lease.

## 4. What counts as an effect (lease-gated)

Every mutating endpoint and MCP tool of the harness is gated. The complete
list, anchored to file:line as of `175f8ec`:

### REST (under `app.post` / `app.put` / `app.delete` / `app.patch`)

- `src/lh_harness/webapi/server.py:1924` `POST /api/models/refresh`
- `src/lh_harness/webapi/server.py:1934` `POST /api/queue` (enqueue)
- `src/lh_harness/webapi/server.py:2034` `POST /api/queue/drain`
- `src/lh_harness/webapi/server.py:2248` `POST /api/maintenance/suspend`
- `src/lh_harness/webapi/server.py:2576` `POST /api/maintenance/resume`
- `src/lh_harness/webapi/server.py:2813` `DELETE /api/queue/{queue_id}` (cancel)
- `src/lh_harness/webapi/server.py:2875` `POST /api/mcp/fleet/{tool_name}`
- `src/lh_harness/webapi/server.py:2900` `POST /mcp` and `POST /mcp/`
- `src/lh_harness/webapi/server.py:2925` `POST /api/queue/{queue_id}/priority`
- `src/lh_harness/webapi/server.py:3140` `POST /api/runs`
- `src/lh_harness/webapi/server.py:3526` `POST /api/runs/{run_id}/rounds/{round_index}/artifact-chunks`
- `src/lh_harness/webapi/server.py:3576` `POST /api/runs/{run_id}/instructions` (send instructions)
- `src/lh_harness/webapi/server.py:3627` `POST /api/runs/{run_id}/approvals/{approval_id}/resolve` (resolve gate)
- `src/lh_harness/webapi/server.py:3673` `POST /api/runs/{run_id}/abort` (stop)
- `src/lh_harness/webapi/server.py:3684` `POST /api/runs/{run_id}/stop` (stop)
- `src/lh_harness/webapi/server.py:3695` `POST /api/runs/{run_id}/time_limit`
- `src/lh_harness/webapi/server.py:3759` `POST /api/runs/{run_id}/resume` (resume)
- `src/lh_harness/webapi/settings_routes.py:227` `PUT /api/admin/settings/{name}`
- `src/lh_harness/webapi/settings_routes.py:249` `DELETE /api/admin/settings/{name}`
- `src/lh_harness/webapi/settings_routes.py:309` `PUT /api/queue/trios`
- `src/lh_harness/webapi/settings_routes.py:425` `PUT /api/queue/capacity`
- `src/lh_harness/webapi/settings_routes.py:523` `PUT /api/queue/{queue_id}/model`

**Migrate**: `POST /api/runs/{run_id}/abort`, `…/stop`, `…/resume` are the
explicit migrate/stop/resume trio; ledger writes happen as a side-effect of
queue/traffic and are not their own endpoint.

### MCP (dispatched through `mcp_tools.dispatch`)

Every tool in `mcp_tools.TOOLS` that mutates state. The current list at
`175f8ec` (those marked *effect* in the design doc):

- `harness_enqueue_task`
- `harness_cancel_queue_entry`
- `harness_set_queue_priority`
- `harness_drain_queue`
- `harness_resolve_gate`
- `harness_send_instructions`
- `harness_stop_run` / `harness_abort_run` / `harness_resume_run`
- `harness_set_time_limit`
- `harness_put_setting` / `harness_delete_setting` (admin)
- `harness_set_trio` / `harness_set_capacity` (admin)

The dispatch wrapper (`src/lh_harness/mcp_tools.py:dispatch`) is the
chokepoint: every tool call passes through it.

### What stays lease-free (reads)

All `GET` endpoints under `/api/`, every `harness_list_*` and
`harness_get_*` MCP tool, every WebSocket `snapshot`/`events` stream, and
every read-only `/admin` page. A reader never holds a lease.

## 5. Caller identity

The runtime id the gate sees is `caller_auth.resolve_rest_caller` /
`resolve_mcp_caller` — i.e. the verified HMAC identity, exactly as the
gating path already does for `_scoped_caller`. A request body field like
`{"runtime_id": "overseer1"}` is **never** consulted; the only inputs to
the lease gate are the headers (REST) or arguments (MCP) that the existing
identity path already validates.

If the verified caller is `anon` (no valid HMAC), the lease gate is not
reached — the existing 401 / 403 path fires first. The lease layer sits
*after* identity, not before it.

## 6. Headers / arguments the gate expects on the wire

REST mutating endpoints (the list in §4) require two new headers:

- `X-Harness-Lease-Family` — the family the call is acting in (e.g. `overseer`)
- `X-Harness-Lease-Epoch` — the epoch the caller believes the holder has

MCP tools require two new arguments with the same names. Both are validated
by a length and shape check (`family` ∈ `{overseer, infraops, devspecops}`,
`epoch` ∈ positive int).

The runtime id is **not** sent; it is the verified caller. An attacker that
presents a valid HMAC for `overseer1` cannot present a lease header for
`infraops` and have it accepted, because the family's lease row has
`holder = infraops1`, not `overseer1` — the gate compares verified caller
against the row's holder.

## 7. Outcomes

A gate check has three outcomes:

| outcome           | condition                                                                                              | HTTP / JSON-RPC shape                                           |
|-------------------|--------------------------------------------------------------------------------------------------------|----------------------------------------------------------------|
| **accept**        | caller is the row's holder AND `epoch` matches AND `expires_at > now` AND action id not previously accepted | `200 OK` body `{ok, action_id, family, holder, epoch, accepted_at}` |
| **accept-idem**   | action id was previously accepted by the same caller for the same effect                              | `200 OK` body = the *original* receipt (same `action_id`, same `accepted_at`) |
| **deny**          | any other state                                                                                       | `409 Conflict` body `{error, code, family, observed_holder, observed_epoch, reason}` |

The accept-deny audit row is `caller_audit.jsonl` with the existing format
extended by `{family, observed_epoch, observed_holder, reason}`.

## 8. Action-id idempotency

The `action_id` is a UUID v4 the gate computes on the first accept and
records in a separate `effect_action` table keyed by `(family, caller,
action_id)`. A retry that supplies the same `(family, caller, action_id)`
returns the original receipt without executing the effect again. A
different `action_id` from the same caller is a new effect.

`action_id` is taken from the existing `Idempotency-Key` header (REST) and
the existing `idempotency_key` argument (MCP), not from a new field. The
harness already plumbs those values through `_bounded_command_id` /
`command_id`; the lease layer reads the same field.

The action table is `effect_lease_action(action_id, family, caller,
original_receipt_json, accepted_at)`; unique on `(family, caller,
action_id)`. Reads of this table do not need a lease.

## 9. Settings flag — default OFF

The whole gate is gated on `LH_HARNESS_EFFECT_LEASE_ENABLED` (bool, default
`false`). When the flag is `false`:

- the gate's HTTP middleware is *not* registered
- the MCP dispatch wrapper skips the lease check
- the effect_action table is not consulted

The harness's pre-lease behaviour is preserved bit-for-bit. Existing test
suite must stay green with the flag off.

When the flag flips to `true`, every mutating endpoint and MCP tool starts
enforcing. The first renewed guest of an empty lease table starts with
`epoch = 1` and `reason = "initial"`.

## 10. Migration note

The VM211 switch file (`agents/orchestrator/overseer-switch.json`,
fields `holder`, `handoff`) maps onto the first lease record as follows:

| VM211 field          | first lease record                                   |
|----------------------|------------------------------------------------------|
| `holder = "overseer1"` (today's value) | `family = "overseer"`, `holder = "overseer1"`, `epoch = 1`, `reason = "initial"` |
| `handoff = ""`        | (nothing — the file stays advisory only)             |

The first acquire happens when an operator flips `LH_HARNESS_EFFECT_LEASE_ENABLED`
to `true` *and* runs the holder's bootstrap acquire with reason `initial`.
Until then, **nothing is switched on** and VM211 overseer1 stays the
de-facto writer (the same as today). The VM211 switch file is **not**
modified by this PR and stays a separate advisory channel.

## 11. Out of scope

- The PR does not change the runtime id ↔ family mapping table.
- The PR does not deploy, restart, or merge anything.
- The PR does not touch `ptait09` or any deployment on VM211.
- The PR does not introduce new env files; the flag is stored in the
  app-embedded settings DB.