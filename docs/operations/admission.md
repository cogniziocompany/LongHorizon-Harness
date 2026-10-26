# Node-wide run admission

How the launcher decides whether THIS node may start one more run. Written
after the CT110 OOM incident of 2026-10-25: the queue's per-lane caps
(`queue_capacity` in the settings DB) summed to 20 while the fleet node
record advertises `maxConcurrentRuns 5`. The launcher admitted per lane
only, so 11-15 runs ran at once in a 6144 MB container, the OOM killer
took `lh-harness.service` at 22:27:29Z, and 13 launched runs ended
`worker disappeared without a final report`. Admission is now decided at
the NODE level, per launch cycle, under a lock, before any launch.

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
   `LH_HARNESS_RUN_STARTUP_MB`, default 600). The launcher reads
   `MemAvailable` from `/proc/meminfo`, bounded by the process's own cgroup
   `memory.max` when a finite one is present (the LXC case: the host-wide
   number can overstate what the container may touch). It refuses to launch
   when

   ```
   MemAvailable - LH_HARNESS_RUN_STARTUP_MB < LH_HARNESS_MEM_FLOOR_MB
   ```

   The startup allowance reserves the memory one starting run really
   consumes before its RSS settles. The refusal is recorded as the entry's
   latest skip reason starting with the marker `memory_headroom` (e.g.
   `memory_headroom: MemAvailable 1500 MB minus startup allowance 600 MB is
   below the 1024 MB floor`), and the entry stays pending. When no memory
   reading exists at all (a stripped container), this gate fails OPEN and
   the node cap alone bounds admission -- a missing probe must never freeze
   the queue.

All three limits hot-reload: the settings are read from the settings DB at
the start of every launch pass, so an operator change applies on the next
pass with no restart (same mechanism as the lane caps).

## The settings

Stored in the settings DB (admin screen, `/admin`), next to the lane caps
and the disk thresholds. Names, defaults, and meaning:

| Setting | Default | Meaning |
| --- | --- | --- |
| `LH_HARNESS_NODE_MAX_RUNS` | `5` | Node-wide cap on running + starting runs across all lanes |
| `LH_HARNESS_RUN_STARTUP_MB` | `600` | MB of MemAvailable reserved for one starting run |
| `LH_HARNESS_MEM_FLOOR_MB` | `1024` | Minimum MB that must remain, after the startup allowance, before a launch is admitted |

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
- `mem_available_mb` -- the honest headroom reading (host MemAvailable
  bounded by the cgroup limit when one applies), or `null` when unreadable.
- `blocked_reason` -- which gate refuses right now: `"node_max"`,
  `"memory_headroom"`, or `null` when the launcher is admitting. When the
  node is already OVER the cap at start (for example after a cap change),
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

## Raising caps safely

1. Check the current state first: `GET /api/meta` (`admission.running` +
   `admission.starting` vs `admission.node_max`, and
   `admission.mem_available_mb`).
2. Budget the memory, not just the runs. One run is roughly 300 MB RSS at
   steady state but STARTS hungrier; keep the floor high enough that the
   web process and the runs never meet. On a 6144 MB node: web ~0.9 GB,
   floor 1.0 GB, startup 0.6 GB leaves room for about 3-4 concurrent runs.
3. Raise `LH_HARNESS_NODE_MAX_RUNS` first (it takes effect on the next
   launch pass), then raise the lane caps. Never the reverse: lane caps
   above the node maximum are clamped by the node gate anyway, and a
   mismatch logs a one-time warning:
   `admission clamp: per-lane caps sum to N but the node maximum is M`.
4. Leave the memory settings alone unless the node's RAM actually changed.
   Lowering the floor to "get more runs in" is how the incident happened.
5. Watch the fleet window / dashboard: `blocked_reason` should return to
   `null` as runs drain, and `mem_available_mb` should never sit below
   floor + startup while launches are being admitted.

## Where the code lives

- `src/lh_harness/launcher.py` -- `Launcher._admit` (the one decision),
  `_admission_settings`, `_memory_headroom_reason`,
  `_reconcile_startup_state`, `_warn_on_caps_over_node_max`, and the
  module-level `read_mem_available_mb` probe.
- `src/lh_harness/settings_store.py` -- the three setting names in the
  catalog.
- `src/lh_harness/webapi/protocol.py` and `src/lh_harness/webapi/server.py`
  -- the `/api/meta` `admission` object.
- `tests/webapi/test_launcher_node_admission.py` -- the regression tests,
  including the exact incident shape (lane caps summing to 20 against
  node_max 5 can never admit more than 5).