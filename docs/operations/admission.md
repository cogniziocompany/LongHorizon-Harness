# Node-wide run admission

How the launcher decides whether THIS node may start one more run. Written
after the OOM incident in which the queue's per-lane caps (`queue_capacity`
in the settings DB) summed to far more than the fleet node record's
`maxConcurrentRuns`. The launcher admitted per lane only, so many more
runs ran at once than the node could hold; the OOM killer took
`lh-harness.service`, and every launched run ended `worker disappeared
without a final report`. Admission is now decided at the NODE level, per
launch cycle, under a lock, before any launch.

## Deployment boundary

This change is source only. It is not deployed, and it does not restart any
service. The live CT110 node keeps running the old launcher until the next
ordinary deploy of this code lands there, so **the node-wide cap and the
memory headroom gate are NOT active on CT110 yet** -- they take effect on
that node's next deploy, and only then (Paxton's go). Until that deploy,
CT110 still admits per lane only.

Nothing in this change touches a running node at merge time: no restart, no
kill of in-flight runs, no change to the live settings DB. The three
settings take their documented defaults on the first deploy; an operator
may then change them from the admin screen at runtime (they hot-reload).

## The three limits and how they compose

A launch must pass all three gates, checked in this order. The tighter one
always wins; no gate can override another.

1. **Lane caps** (per trio, `queue_capacity.max_concurrent` in the settings
   DB, hot-reloaded from `[queue.capacity]` in config.toml). One lane each.
   Their SUM may exceed the node maximum -- that is legal and expected --
   but it no longer decides anything by itself. A lane at its cap keeps
   reporting its own reason (`"<trio> at capacity"`).

2. **Node-wide admission cap** (`LH_HARNESS_NODE_MAX_RUNS`, default 5).
   Before any launch, the launcher counts **running + starting runs across
   ALL lanes** from the authoritative run store (the supervisor's live run
   set, plus launched queue entries whose run the store no longer sees --
   those hold a slot until the reconcile pass terminal-marks them). When
   `running + starting >= node_max`, nothing is admitted this cycle,
   whatever the lane caps allow. This decision is made once per launch
   cycle, under the launcher's admission lock, so two lanes can never both
   admit into the last slot.

3. **Memory headroom floor** (`LH_HARNESS_MEM_FLOOR_MB`, default 1024, and
   `LH_HARNESS_RUN_STARTUP_MB`, default 600). The launcher reads the memory
   HEADROOM -- what a new worker can still touch -- and refuses to launch
   when

   ```
   headroom_mb - LH_HARNESS_RUN_STARTUP_MB < LH_HARNESS_MEM_FLOOR_MB
   ```

   The headroom is NOT the cgroup's limit. A cgroup's limit is what it MAY
   hold; its headroom is what it has NOT yet used. Reading only
   `memory.max` therefore overstates the room a nearly full container has,
   which is exactly the shape that ends in an OOM kill. So the launcher
   computes, per cgroup level:

   ```
   remaining_mb = (memory.max - memory.current) / 1 MiB
   ```

   and takes the SMALLEST such remaining value over the container's own
   cgroup and EVERY ancestor cgroup that carries a finite `memory.max` --
   an ancestor's limit bounds this process too, so the tightest level is
   the real ceiling. That per-cgroup remaining is then bounded by the
   host's `MemAvailable` from `/proc/meminfo`, and the smaller of the two
   is the honest headroom:

   ```
   headroom_mb = min(host MemAvailable, tightest cgroup remaining)
   ```

   A cgroup level with no finite limit (`memory.max` absent, or the v2
   value `max`) does not bound the headroom and is skipped. The startup
   allowance reserves the memory one starting run really consumes before
   its RSS settles. The refusal is recorded as the entry's latest skip
   reason starting with the marker `memory_headroom` (e.g.
   `memory_headroom: headroom 1500 MB minus startup allowance 600 MB is
   below the 1024 MB floor`), and the entry stays pending. When no memory
   reading exists at all (a stripped container), this gate fails OPEN and
   the node cap alone bounds admission -- a missing probe must never freeze
   the queue.

   The refusal is written to the entry's `latest_skip_reason` field (and
   appended to its `skip_reasons` history). `latest_skip_reason` is the
   single field to read for "why did this entry not start": it is `None`
   on an entry that was never skipped, and it is what the queue API
   returns per entry. Both admission gates write it, so it carries
   `memory_headroom` when the memory gate refused and the node-capacity
   text when the node cap refused.

All three limits hot-reload: the settings are read from the settings DB at
the start of every launch pass, so an operator change applies on the next
pass with no restart (same mechanism as the lane caps).

## The settings

Stored in the settings DB (admin screen, `/admin`), next to the lane caps
and the disk thresholds. Names, defaults, and meaning:

| Setting | Default | Meaning |
| --- | --- | --- |
| `LH_HARNESS_NODE_MAX_RUNS` | `5` | Node-wide cap on running + starting runs across all lanes |
| `LH_HARNESS_RUN_STARTUP_MB` | `600` | MB of headroom reserved for one starting run |
| `LH_HARNESS_MEM_FLOOR_MB` | `1024` | Minimum MB of headroom that must remain, after the startup allowance, before a launch is admitted |

A blank, unparseable, or zero-or-below value keeps the default; the gates
never take the launcher down over one bad row.

## `/api/meta` fields

`GET /api/meta` now carries an `admission` object with exactly five keys:

```json
{
  "admission": {
    "node_max": 5,
    "running": 4,
    "starting": 1,
    "mem_available_mb": 1910.5,
    "blocked_reason": "node_max"
  }
}
```

- `node_max` -- the effective node-wide cap this cycle.
- `running` / `starting` -- the cross-lane counts the decision used
  (`starting` is the creating/starting window where RSS grows fastest).
- `mem_available_mb` -- the honest headroom reading: the host's
  `MemAvailable` bounded by the tightest per-cgroup REMAINING memory
  (`memory.max - memory.current`, taken over the container's own cgroup and
  every ancestor cgroup with a finite max), or `null` when unreadable.
- `blocked_reason` -- which gate refuses right now: `"node_max"`,
  `"memory_headroom"`, `"inventory_unavailable"`, or `null` when the
  launcher is admitting. When the node is already OVER the cap at start
  (for example after a cap change),
  the launcher kills nothing and admits nothing until the count drops
  below it; this field is how an operator sees that state.

## Reconciliation and release

The count is not launcher bookkeeping -- it is re-read from the
authoritative run store on every pass, so a worker exiting or crashing
releases its slot on the next pass with no explicit "release" call. At
service start, the launcher reconciles once from live processes and run
dirs before its first pass. A launched entry whose run vanished in a crash
is marked failed with the same crash-naming reason the supervisor's own
reconciliation uses (`worker disappeared without a final report`); the
launcher never requeues such an orphan itself -- the existing `retry_of`
retry logic alone decides that, exactly as for any other failure.

## When the live-run inventory cannot be read

The count in the node gate comes from an enumeration of the live runs. If
that enumeration FAILS -- the shared supervisor boundary cannot list the
runs it is supposed to be counting -- the count is unknown, not zero. An
unknown inventory is NOT an empty node: treating a failed enumeration as
"no runs are running" would let the node over-admit into the last slot,
which is exactly the failure the node cap exists to prevent.

So on such a failure:

- The last known admission state is KEPT, not reset. The `/api/meta`
  `running` and `starting` counts do not drop to zero and back up to a
  stale value; they hold the last successful reading.
- Admission is BLOCKED, and the block is reported as
  `"inventory_unavailable"` in `/api/meta`'s `admission.blocked_reason`.
- No run is launched and nothing is killed. The blocked entries stay
  pending, with the refusal recorded as their latest skip reason.
- The block clears by itself: as soon as an enumeration succeeds, the next
  pass makes a normal admission decision again. There is no manual reset.

Both launch paths report this the same way. The shared supervisor boundary
refuses first, and the queue launcher sees that refusal and refuses the
same way, so the queue API and `POST /api/runs` never disagree about
whether the node is admitting.

## What a refusal does to the entry

A refusal is NOT a failure. Nothing was started, so the entry is not
failed, does not consume one of its retries, and is retried on a later
pass. The refusal is recorded as the entry's latest skip reason, so the
entry's history shows why it did not start -- with the `memory_headroom`
marker when the memory gate refused, the node-capacity text when the
node cap refused, and the `inventory_unavailable` marker when the live-run
inventory could not be read at all.

The same holds for the direct API path: `POST /api/runs` answers **429**
when the shared boundary refuses, not 400 and not 500. The request was
well formed; the node was simply at capacity or short on memory, so the
caller should retry later.

## Where the decision is enforced (two layers, one cap)

The node cap is enforced twice, and both layers read the SAME three
settings, so they can never disagree about the limit:

1. **The launcher**, once per launch cycle, in `Launcher._admit`: it
   counts running + starting runs across all lanes from the supervisor's
   live run set plus the launched queue entries whose run the store no
   longer sees, and refuses the whole cycle when the count has reached
   the cap. One decision per cycle, under the launcher's admission lock,
   so two lanes can never both admit into the last slot.

2. **The supervisor**, at the shared launch boundary, in
   `RunSupervisor.create_run`: every launch path -- the launcher's
   `_launch` AND the API's `POST /api/runs` -- funnels through
   `NodeAdmission.reserve_startup`, which takes a PENDING STARTUP
   reservation under its own lock before any worker process is spawned.
   The reservation is released when the worker is published into the live
   supervision window, when the start fails, and when the worker exits
   or crashes, so a slot is never leaked.

The second layer is the backstop for the first: a launch that did not
come through the queue launcher (a direct API call) still has to pass
the same cap, and a run in its startup window -- before the launcher can
see it -- still holds its slot.

## Raising caps safely

1. Check the current state first: `GET /api/meta` (`admission.running` +
   `admission.starting` vs `admission.node_max`, and
   `admission.mem_available_mb`).
2. Budget the memory, not just the runs. One run is roughly 300 MB RSS at
   steady state but STARTS hungrier; keep the floor high enough that the
   web process and the runs never meet. Work it out from the node's own
   headroom -- the smaller of the host's `MemAvailable` and the tightest
   cgroup REMAINING (`memory.max - memory.current`): subtract the web
   process's share, then subtract the floor and the startup allowance, and
   divide what is left by the per-run cost to get a safe concurrent-run
   count.
3. Raise `LH_HARNESS_NODE_MAX_RUNS` first (it takes effect on the next
   launch pass), then raise the lane caps. Never the reverse: lane caps
   above the node maximum are clamped by the node gate anyway, and a
   mismatch logs a one-time warning:
   `admission clamp: per-lane caps sum to N but the node maximum is M`.
4. Leave the memory settings alone unless the node's RAM actually changed.
   Lowering the floor to "get more runs in" is how the incident happened.
5. Watch the fleet window / dashboard: `blocked_reason` should return to
   `null` as runs drain, and `mem_available_mb` should never sit below
   floor + startup while launches are being admitted. A
   `blocked_reason` of `inventory_unavailable` is not a capacity problem:
   it means the live-run count could not be read, and it clears on its own
   once an enumeration succeeds.

## Where the code lives

- `src/lh_harness/launcher.py` -- `Launcher._admit` (the one decision),
  `_admission_settings`, `_memory_headroom_reason`,
  `_supervisor_inventory_reason` (the `inventory_unavailable` block),
  `_reconcile_startup_state`, `_warn_on_caps_over_node_max`, and the
  module-level `read_mem_available_mb` probe.
- `src/lh_harness/supervisor/admission.py` -- the shared boundary:
  `NodeAdmission.reserve_startup` (the reservation and the two gates),
  `release_startup` / `publish` / `observe` / `forget` (the slot's life
  cycle), `reconcile` (the count rebuilt from live runs), the
  `/api/meta` snapshot, `cgroup_headroom_mb` / `read_cgroup_headroom_mb`
  (the per-cgroup REMAINING memory and the ancestor walk),
  `read_mem_available_mb` (the host reading bounded by that remaining),
  and the `INVENTORY_UNAVAILABLE_BLOCKED_REASON` marker.
- `src/lh_harness/supervisor/service.py` -- the reservation taken in
  `create_run`, published when the worker comes up, and released when it
  exits or crashes.
- `src/lh_harness/settings_store.py` -- the three setting names in the
  catalog.
- `src/lh_harness/queue.py` and `src/lh_harness/pg_queue.py` -- the
  entry's `latest_skip_reason` field (the file store and the Postgres
  column), written by `record_skip` wherever the refusal is recorded.
- `src/lh_harness/webapi/protocol.py` and `src/lh_harness/webapi/server.py`
  -- the `/api/meta` `admission` object.
- `tests/webapi/test_launcher_node_admission.py` -- the launcher-side
  regression tests, including the exact incident shape (lane caps summing
  past the node maximum can never admit more than node_max).
- `tests/supervisor/test_supervisor_admission.py` -- the shared-boundary
  tests: the reservation is atomic under the lock (two racing launches
  cannot both take the last slot), the memory gate refuses, the slot is
  released on a failed start and on worker exit, and the count is
  reconciled from the live run dirs at construction.