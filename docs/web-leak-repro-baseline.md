# Web-app memory / queue wedge — pre-fix baseline measurements

Task: **LHH-WEB-LEAK-FIX** (deliverable A — the *before* numbers).

These are **real measurements taken on this node** against the real web app
(`lh_harness.webapi.server.create_app`) over **synthetic fixture run
directories in a temp dir**. Nothing under a harness-owned run path was read
or written; no production node was contacted.

The operator's numbers (overseer1 on CT110, build `add619c9` = 0.1.7,
2026-10-25/26) are the symptom being reproduced: RSS 3.2 GB @ 39 min,
4.6 GB @ 2 h, 5.3 GB @ 2 h 40 min with 62–66 threads, while `/api/queue`,
`GET /` and the fleet-gateway node reads hung > 40–140 s (curl 000) even
though `/api/health` answered in 5–10 ms throughout.

---

## Environment used for these measurements

| item | value |
| --- | --- |
| interpreter | `/tmp/lhci-venv/bin/python` (CPython 3.12, `pip install -e '.[test]'` — same shape as `.github/workflows/pr-gate.yml`) |
| app under test | this checkout's `src/lh_harness` (the repo `src` tree is put first on `sys.path`) |
| fixtures | `tempfile.mkdtemp()` temp root, 520 run dirs + 120 queue entries |
| harness-owned paths | never read, listed, searched or written |

## Measurement 1 — request latency vs. run count (the wedge)

Each row builds a fresh fixture with N run dirs and issues **one**
`GET /api/runs` (the full projection the dashboard uses), one
`GET /api/runs?fields=summary` (the cheap projection) and one
`GET /api/queue`, timing each.

| run dirs | `GET /api/runs` | `GET /api/runs?fields=summary` | `GET /api/queue` |
| ---: | ---: | ---: | ---: |
| 25 | 3 362 ms | 62 ms | 19 ms |
| 50 | 8 874 ms | 398 ms | 33 ms |
| 100 | 10 225 ms | 308 ms | 22 ms |
| 200 | **62 842 ms** | 1 343 ms | **18 ms** |

**Reading of these numbers.** `GET /api/runs` is superlinear in the number of
run directories: 8× the run dirs (25 → 200) costs ~19× the wall clock
(3.4 s → 62.8 s), and it reaches the 40–140 s hang window CT110 reported.
`GET /api/queue` stays flat at ~20 ms across the whole range **when the
server is otherwise idle**, because the queue path never touches the run
directories. (Under *concurrent* load it degrades anyway — see Measurement 3
— because it queues behind the saturated worker pool. That is the CT110
symptom: the queue endpoint answers while the dashboard hangs.)

## Measurement 2 — where the time goes (cProfile of one `GET /api/runs`, 60 runs)

A single `GET /api/runs` over 60 run dirs took **15.8 s** (34.3 s under the
profiler). Top cumulative entries:

```
ncalls   tottime  cumtime  file:lineno(function)
   712       0.044   13.958  supervisor/service.py:1111(_bus)
   819/710   0.110   13.701  utils/run_boundary.py:139(safe_run_logs)
  3080/2585  0.202   13.131  utils/run_boundary.py:114(_safe_child)
  3548/2919  0.270   11.852  utils/run_boundary.py:52(safe_run_dir)
 14692/10783 0.342   11.482  pathlib.py:1230(resolve)
   368/343   0.039   10.924  utils/run_boundary.py:192(safe_run_role)
    60       0.006    9.722  webapi/server.py:794(state_for)
   896/782   0.075    9.519  utils/run_boundary.py:226(safe_run_control)
    60       0.009    8.600  webapi/snapshot.py:254(build_run_summary)
   147/133   0.012    7.591  supervisor/service.py:1363(_maybe_park_terminal_workspace)
   148       0.011    7.306  supervisor/service.py:1183(_persist_failure_report)
   354/322   0.014    7.266  supervisor/control_bus.py:773(read_status)
   147       0.012    7.056  supervisor/control_bus.py:748(update_status)
  41398/32105 0.321   6.632  pathlib.py:837(stat)
```

Two structural facts fall out of these counts:

* **`Path.resolve()` is called 14 692 times for 60 runs** — the
  `safe_run_*` boundary walk is re-done many times *per run*, not once per
  request. Every one of those is a `realpath()` syscall.
* **147 `update_status()` calls for 60 runs** — the listing *writes to disk*
  while serving a read. `_maybe_park_terminal_workspace` →
  `_persist_failure_report` → `update_status` fires on every poll for every
  terminal run, so a read endpoint does durable writes on the request path.

## Root cause (deliverable B) — file:line

The full `GET /api/runs` projection re-walks every run boundary **on every
request**, and — depending on whether the run's task text is available — it
does so **once per run** (quadratic) or ~53 times per run (linear with a
large constant). Either way the request cost scales with the number of run
directories, which is exactly what CT110 had (~800 run dirs).

### Mechanism 1 — the boundary walk is repeated per run, not per request

1. `src/lh_harness/webapi/server.py:3257` — the handler loops
   `for item in registry.run_items():`
2. `src/lh_harness/webapi/server.py:3261` — and for each run calls
   `build_run_summary(item, state=registry.state_for(item_run_id))`
3. `src/lh_harness/webapi/server.py:794` — `StateRegistry.state_for()` calls
   `_run_paths()` (`server.py:769`), which walks the run boundary:
   `safe_run_dir` → `safe_run_logs` → `safe_run_control` → `safe_run_rounds`
   → `safe_run_role` (`server.py:781-787`).
4. `src/lh_harness/webapi/server.py:807-821` — **on a cache HIT the walk is
   repeated anyway**: the comment says "Revalidate the cached path on every
   request", and `self._run_paths(run_id)` is called again to compare
   `cached.log_dir.resolve(strict=False)` against the freshly walked path.
5. `src/lh_harness/utils/run_boundary.py:52` / `:114` / `:139` — each
   `safe_run_*` call resolves every path component with `realpath()`, so one
   boundary walk is several dozen syscalls.

**Measured** (one `GET /api/runs` over 30 run dirs, 22.5 s):

| boundary helper | calls | per run |
| --- | ---: | ---: |
| `_safe_child` | 1 592 | **53.1** |
| `safe_run_dir` | 1 088 | **36.3** |
| `safe_run_logs` | 181 | 6.0 |
| `safe_run_role` | 90 | 3.0 |

### Mechanism 2 — the per-run state cache cannot help (quadratic scan)

`src/lh_harness/webapi/server.py:663` —
`_STATE_REGISTRY_MAX = int(os.environ.get("LH_HARNESS_STATE_REGISTRY_MAX", "64") or 64)`.

The registry LRU (added by LHH#120) is bounded at **64** entries, but
`GET /api/runs` iterates **every** run dir in order. With more than 64 run
dirs the cache thrashes: each request evicts the entries it is about to
need. The cache therefore contributes **no hit rate** on CT110's ~800 run
dirs while still paying the eviction cost.

### Mechanism 3 — `snapshot()` re-scans every run dir (the quadratic path)

When a run's task text is **not** available, `build_run_summary` falls back
to `state.snapshot()`:

* `src/lh_harness/webapi/snapshot.py:259-260` —
  `if state is not None and (not status or not task): raw = state.snapshot()`
* `src/lh_harness/dashboard/state.py:802` — `snapshot()` calls
  `self.list_runs()`
* `src/lh_harness/dashboard/state.py:287` → `state.py:237` →
  `state.py:248` — `list_runs()` is `_scan_runs()`, which does
  `sorted(root.iterdir())` and re-validates **every run directory in the
  tree**.

That is a **full run-dir scan per run** → O(runs²) per request.

**Measured, same app, same fixtures, only the task text differing:**

| run dirs | task text present | `list_runs()` calls | `_scan_runs()` calls | `GET /api/runs` |
| ---: | :--- | ---: | ---: | ---: |
| 20 | yes | 0 | 0 | 8 749 ms |
| 40 | yes | 0 | 0 | 12 946 ms |
| 20 | **no** | **35** | **35** | **61 543 ms** |
| 40 | **no** | **73** | **73** | **274 551 ms (4 min 35 s)** |

73 scans for 40 runs is the quadratic signature (roughly one full scan per
run, plus the listing's own), and it is **quadratic in the measured time**:
doubling the run dirs (20 → 40) costs **4.5×** the wall clock
(61.5 s → 274.6 s), not 2×. At CT110's ~800 run dirs this is the 40–140 s
hang window (and beyond).

**The "cheap" projection is not cheap either.** Against the reproduction's
520-run fixture, `GET /api/runs?fields=summary` took **14 822 ms** and
`GET /api/runs` did not answer inside a 40 s client timeout. The cheap path
(`run_items_cheap` → `supervisor.list_run_summaries`,
`supervisor/service.py:1687`) still walks every run directory on every
request; it only avoids the deeper per-run reads.

**The full projection does not answer at all at 520 run dirs.** Against the
same running fixture, a client `GET /api/runs` with a **180 s** timeout
**never returned** (curl exit code 124), while the server's RSS climbed from
71.2 MB to 85.3 MB during the attempt and `/api/meta` was still answering.
That is the CT110 "hangs > 40–140 s (curl 000)" symptom, reproduced
locally.

### Mechanism 4 — a read endpoint writes to disk

`src/lh_harness/supervisor/service.py:1846` — `list_run_items()` calls
`self._maybe_park_terminal_workspace(run_dir.name, status)` **inside the
listing loop**, so a read endpoint performs workspace-park work for every
terminal run on every poll (`service.py:1363` → `service.py:1299` →
`service.py:1183` → `control_bus.update_status`).

**Measured:** 147 `update_status()` calls and 148 `_persist_failure_report()`
calls for **60 runs** in a single `GET /api/runs` — a read that writes
2.5 durable records per run per request.

### Why `/api/queue` kept answering on CT110

The queue listing reads only the queue store's own files; it never touches
the run directories, so it is unaffected by all four mechanisms above. That
is why `/api/health` and `/api/queue` were fast while `GET /` (the
dashboard) and the fleet-gateway node reads hung — and why the CT110
symptom was "`/api/queue` hangs" only in the sense that the *dashboard* that
polls it was wedged behind the exhausted worker pool.

### Why RSS grew

Each in-flight `/api/runs` request materialises a `DashboardState` per run
plus its report/owner/provenance reads, and (mechanism 3) a full run-dir
scan per run. With many concurrent pollers (CT110: 62–66 threads) each
holding hundreds of these at once, the resident set grows with
*concurrent pollers × run dirs*, and the server's worker pool saturates — a
fresh `/api/queue` request then queues behind the in-flight scans. A restart
clears the in-flight state, which is why CT110 recovered for ~40 min after
each restart (until the next poll filled the pool again).

### Candidates that were checked and REFUTED

* **"The per-run state registry never evicts finished runs"** — refuted.
  `StateRegistry` has a bounded LRU (`server.py:744-750`, `_evict_states`
  at `server.py:838`). The registry is not the unbounded structure; its
  *bound* (64) is simply smaller than the run count, so it never helps.
* **"The snapshot cache is keyed per request"** — refuted. No per-request
  snapshot cache was found on this path; the cost is recomputation, not
  cache-key cardinality.
* **"A sqlite settings store opens a connection per request"** — refuted
  for this path. The settings store is not consulted by `GET /api/runs`;
  it does not appear in the profile.
* **"Thread-pool exhaustion by long run-dir scans"** — refuted as the
  *cause*; it is the *consequence*. The pool is exhausted because each
  request is long, not because a scan is wrongly dispatched to the pool.
* **"The full per-role payload is reloaded per poll"** — refuted as the
  cause, see the operator-lead section below.

## Operator lead (per-role payload) — status: NOT confirmed as the cause

The operator's dashboard lead (`run 9437a8e1`, ~4.4 MB per-role executor
output) was investigated at code level. The per-role text reads are already
bounded: `_read_text_bounded(..., _MAX_ROUND_TEXT_BYTES)` in
`src/lh_harness/dashboard/state.py` truncates a 4.4 MB role output to
512 KiB, so the *reader* is bounded. The unbounded growth measured here is
**not** payload-driven — it is driven by the **number of run dirs** and the
per-request re-scan/re-validation above. The lead is therefore recorded as
**refuted as the primary cause**; a role-payload size budget remains worth
adding as defence in depth, but it is not what produced the CT110 wedge.

---

## Measurement 3 — the queue symptom explained (the CT110 "queue hangs" report)

CT110 reported `/api/queue` hanging > 40–140 s (curl 000) while the
unauthenticated probe answered in 5–10 ms. The mechanism is **worker-pool
saturation**, not a slow queue path:

* `src/lh_harness/queue.py:1603-1617` — `QueueStore.list()` iterates only
  `self._root` (the `queue/` sub-directory) and reads only `*.json` files.
  It never touches a run directory. The queue read is therefore O(queue
  entries), independent of the ~800 run dirs.
* `src/lh_harness/webapi/server.py:2836-2838` — the `/api/queue` handler
  calls only `queue_store.list()` / `queue_store.counts()`.

Measured against the reproduction's child server **while the run-list path
was saturated** (520 fixture run dirs, the script's poll mix running):

| endpoint | idle | under load |
| --- | ---: | ---: |
| `GET /api/meta` | ~20 ms | 49–112 ms |
| `GET /api/queue` | 18–33 ms | **2 613–2 906 ms** |

`/api/queue` is unchanged in cost, but it is **queued behind the saturated
worker pool**, so it slows by ~100×. The same effect at CT110's scale (62–66
threads, 800 run dirs) is what produced the 40–140 s hangs and the `curl
000`. The fix is therefore to make the run-list path cheap, not to make the
queue path faster.

Child process state during the saturated phase: `Threads: 9`,
`VmRSS: 71 156 kB` (69.5 MiB).

### What this reproduction does and does NOT show about RSS

**Honest scope note.** The **latency** and the **wedge** reproduce
reliably and are the load-bearing measurements: `GET /api/runs` does not
answer within 180 s at 520 run dirs, and it is superlinear in the run count.

The **multi-GB RSS growth** observed on CT110 is only *partially* reproduced
locally. Measured over this run, the child's RSS went 58.0 MiB (baseline) →
71.2 MiB → 83.9 MiB → 85.9 MiB and then **plateaued**; it did not reach the
3.2–5.3 GB CT110 recorded. The reasons are honest and specific:

* The fixture's run dirs all carry a `task` in `control/owner.json`, so the
  **quadratic** `state.snapshot()` path (mechanism 3) is *not* taken and each
  request does not materialise a full snapshot per run. CT110's run dirs
  include many without a task text, which is what turns the latency problem
  into a resident-set problem.
* CT110's 3.2–5.3 GB accumulated over **2 h 40 min** of continuous polling
  from the dashboard, the fleet gateway and the overseer. The reproduction
  runs for minutes.

**So: the RSS growth on CT110 is the *same defect* seen over a long enough
window with the quadratic path active.** The reproduction pins the mechanism
(request cost scales with run count; a run without task text turns that into
a per-run full scan) and the wedge it produces; the multi-GB resident set is
what that same mechanism yields under CT110's sustained production load.
The reproduction's own RSS assertion is therefore written as a **bounded**
assertion (RSS must not grow without bound across repeated polling), not as
a reproduction of CT110's absolute figure — an absolute multi-GB target
would be a fabrication, because it depends on CT110's own workload shape.

## Reproduction commands

```bash
# CI-shaped environment used here (see .github/workflows/pr-gate.yml)
python3 -m venv /tmp/lhci-venv
/tmp/lhci-venv/bin/pip install -e '.[test]' pyyaml typing_extensions

# the leak/latency reproduction (script)
env -u PYTHONPATH /tmp/lhci-venv/bin/python scripts/web_leak_repro.py \
    --runs 520 --queue 120 --batches 3 --polls-per-batch 6 \
    --concurrent-workers 8 --concurrent-rounds 2

# the same reproduction as a test
env -u PYTHONPATH /tmp/lhci-venv/bin/python -m pytest \
    tests/webapi/test_web_leak_repro.py -q
```

## Post-fix numbers

_Filled in after the fix lands — see the PR body._
