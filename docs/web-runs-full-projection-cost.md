# Full `GET /api/runs` projection: where the per-request work goes

easybutt0n-80, follow-up to #193.  #193 removed the quadratic *scan* (one
runs-root walk per run instead of one per listing).  This note maps what the
**full** projection — `GET /api/runs` with no `fields` parameter — still does
per request, on the code as merged at `b0942d1`.

Everything below is measured on a synthetic runs root in a temp directory via
`scripts/bench_runs_full_projection.py`.  No node, no CT111, no production run
directories were touched.

## The handler

`webapi/server.py:3447 runs()` takes the full branch when `fields` is absent
(`summary_mode` is False):

```python
result = []
contentions = _load_contentions(runs_root)          # one file read per request
for item in registry.run_items():                    # 1. supervisor listing
    if status_filter and ...: continue
    summary = build_run_summary(
        item, state=registry.state_for(item_run_id)  # 2. per-run registry state
    )                                                # 3. per-run projection
    result.append(_annotate_contention(summary, contentions))
```

`registry.run_items()` (`webapi/server.py:1092`) delegates to
`RunSupervisor.list_run_items()` (`supervisor/service.py:1768`).

## Per-run work, stage by stage

| Stage | Per run | What it touches |
| --- | --- | --- |
| `RunSupervisor.list_run_items()` | **1.83 ms** | full boundary walk, `status.json` refresh, `report.json`, `owner.json`, terminal-run park |
| `StateRegistry.state_for()` | 0.19 ms | one `os.stat` stamp check (the #193 TTL path) |
| `build_run_summary()` | 0.08 ms | `read_owner()` + `read_report()` + provenance projection |
| **Total warm** | **2.16 ms** | |

### 1. `list_run_items()` dominates — 85% of the request

Per run, it performs (`supervisor/service.py:1805-1846`):

* **A full boundary walk.** `safe_run_logs` / `safe_run_control` / `safe_run_role` /
  `safe_run_rounds` each call `safe_run_dir` + `_safe_child`, and every one of
  those does `is_symlink()` + `exists()` + `is_dir()` + `resolve()` +
  `relative_to()`.  Measured: **38.5 boundary-helper calls per run**, of which
  `safe_run_dir` alone is 15.2 and `_safe_child` 13.1.  `_safe_entry` is also
  called three times per run just to re-validate the directory it already had.
* **`self._refresh(run_id)`** (`supervisor/service.py:1501`): reads `status.json`
  under the process-wide `_lifecycle_lock`, then — for a live run — `_is_alive()`
  spawns a **`ps` subprocess per run** (`_ps_query`).  Measured on a live-shaped
  fixture: exactly **1.0 `ps` spawn per run**.
* **`_read_json(report.json)`** (capped at 8 MiB) and
  **`_read_json(owner.json)`** — two JSON parses per run.
* **`_saved_task_from_rounds()`** when the owner carries no task: an `openat`
  walk with `O_NOFOLLOW` per round component.
* **`_maybe_park_terminal_workspace()`** for every terminal run — measured at
  0.23 ms/run locally (it returns early once `parked_epoch` is set, but the
  first terminal observation of an unparked run runs real `git` commands).

### 2. `state_for()` is already bounded by #193

0.19 ms/run: one `os.stat` of the run directory (stamp `(st_mtime_ns, st_ino)`)
plus the per-run `DashboardState` cache.  **The scan is not implicated** — see
`scan_walks: 0` on a warm listing below.

### 3. `build_run_summary()` is cheap *unless* it takes the snapshot fallback

`snapshot.py:254`.  When the listing row already carries `task` and `status`
(production shape: `owner.json` always carries `task`, per
`supervisor/service.py:2270`), the row costs 0.08 ms.

When `task` or `status` is missing, it calls `state.snapshot()`, which reads
report rounds, round artifacts, `list_round_roles()` and
`_round_trajectory_sizes()` for **every round** — 2 trajectory probes per round
(`state.py:799-857`, each doing `is_symlink` + `resolve` + `relative_to` +
`open` + `fstat`).  That is what #193's shared scan cache was protecting: the
`snapshot()` → `list_runs()` walk.  With #193 in place the shared cache absorbs
it (`scan_walks: 0`), so the fallback costs round-count-proportional work but
**no additional runs-root walk**.

## Is the cost bounded per request?

No.  On a warm cache the request is **O(runs) with a ~2 ms constant** — nothing
is memoized across requests:

| N | warm full listing | per run | `list_run_items` | `state_for` | `build_run_summary` |
| --- | --- | --- | --- | --- | --- |
| 50 | 110 ms | 2.20 ms | 91.6 ms | 9.8 ms | 3.8 ms |
| 372 | 804 ms | 2.16 ms | 678.8 ms | 71.5 ms | 31.2 ms |

`build_run_summary` is called **once per run on every request**
(`build_summary_calls: 372`), `list_run_items` once per request, and the
boundary walk is re-performed from scratch for every run every time.  Nothing
carries a run's validated boundary, its parsed `report.json`, or its projected
summary between two listings.

The constant itself is not fixed either.  It scales with the run's on-disk
weight, because the per-run reads are unbounded by design:

* `report.json` size: 0.05 MiB → 2.27 ms/run, 1 MiB → 3.51 ms/run,
  4 MiB → 7.34 ms/run (N=372).
* Live runs vs finished runs: 2.16 ms/run finished, **5.41 ms/run live**
  (the `ps` subprocess in `_is_alive`), so a node with many live runs is the
  worst case.

At N=372 that is ~0.8 s of pure Python per listing on an idle dev box.  A node
at 253% CPU with several dashboard pollers re-issuing the full listing is
re-running that same O(N) boundary-walk + JSON-parse + subprocess fan-out on
every request, which matches the measured "first call 23.9 s, then two calls
over 60 s" symptom far better than a scan (the scan is cached and measured at
0 walks).

## Cold vs warm

The first listing after a process start is much worse: **4997 ms at N=372**
(13.4 ms/run) versus 804 ms warm.  Two effects:

* `DashboardState.snapshot()`/`_scan_runs()` populate the shared holder on the
  first cold listing; warm listings do `scan_walks: 0`.
* The queue launcher's background tick also calls `list_run_items()`
  (`launcher.py:1971 _active_runs`), so a cold listing can pay that walk a
  second time concurrently.

## Out of scope here

This note is the mapping only.  The bounded-work fix and its regression test
land as separate commits on the same PR.
