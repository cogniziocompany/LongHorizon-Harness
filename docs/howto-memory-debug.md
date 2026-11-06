# How to: live memory debug for the web process

Ticket: `easybutt0n-17`. Slice: `LHH-WEB-LEAK-FIX c3`.

The CT110 web process (`lh_harness web`, 526 run dirs, pollers every few
seconds) grows from **~0.8 GB RSS at start** to **>3 GB within ~70 min** and
is then killed by an RSS guard. The earlier slices did not stop it:

* LHH#175 (c1) bounded the per-run state cache and added the RSS watchdog;
* LHH#179 cached the dashboard runs scan behind a TTL and a lock;
* LHH#178 (c2a) measured the leak off-node and found no reproduction.

This how-to is the **live measurement** that costs **no restart**: with the
debug flag on, the running web process reports what is growing, in place, on
the node where it grows.

Everything described here is **opt-in**. With the flag unset the routes do
not exist (they answer `404`), and `tracemalloc` is never started, so the
default deployment pays nothing.

---

## 1. Enable

Set the flag **in the environment of the web process** and restart the web
process once to pick it up. (The flag is read at process startup; the
routes themselves re-read it on every request.)

```bash
# on the node running the web process
export LH_HARNESS_MEM_DEBUG=1
export LH_HARNESS_WEB_TOKEN='<the existing bearer>'
lh-harness web --runs-root /path/to/runs --host 0.0.0.0 --port 8799
```

Only the exact value `1` enables the feature. `0`, `true`, `yes` and the
empty string all leave it **off**.

At startup the process now calls `tracemalloc.start(25)` — 25 frames of
traceback depth — and logs one line:

```
memory debug enabled: tracemalloc started with 25 frames
```

> **Cost.** `tracemalloc` slows allocations and keeps its own bookkeeping in
> the process. That is fine for a diagnostic run on a node whose memory
> growth you are already investigating, and it is the reason the feature is
> opt-in. Turn it off when you are done (step 4).

The API keeps its **existing bearer boundary**: every route below needs the
same `Authorization: Bearer <token>` header as every other `/api/` route.
Without it the routes answer `401`.

## 2. Call twice, ten minutes apart

Take a tracemalloc snapshot, wait, take a second one. The snapshots are held
**in memory only** (never written to disk) and at most **3** are retained.

```bash
BASE='http://127.0.0.1:8799'
AUTH="Authorization: Bearer ${LH_HARNESS_WEB_TOKEN}"

# snapshot #1
curl -sS -X POST "$BASE/api/debug/memory/snapshot" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{}'

# ... let the web process run for ~10 minutes ...

# snapshot #2
curl -sS -X POST "$BASE/api/debug/memory/snapshot" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{}'
```

> The `POST` needs `Content-Type: application/json`: the web API's
> middleware answers `415` to a `POST` without it.

Each snapshot call answers with the snapshot count and the current tracemalloc
totals:

```json
{
  "taken_at": 1793000000.0,
  "snapshots_held": 2,
  "snapshot_cap": 3,
  "current_bytes": 812345678,
  "peak_bytes": 900123456
}
```

You can also look at the live picture at any time, without snapshotting:

```bash
curl -sS "$BASE/api/debug/memory" -H "$AUTH" | python3 -m json.tool
```

## 3. Read the diff

```bash
curl -sS "$BASE/api/debug/memory/diff" -H "$AUTH" | python3 -m json.tool
```

The diff compares the **latest two** snapshots and returns the **top 30
growth entries** — the allocation sites whose size grew the most between
snapshot #1 and snapshot #2, largest growth first:

```json
{
  "older_taken_at": 1793000000.0,
  "newer_taken_at": 1793000600.0,
  "interval_seconds": 600.0,
  "top_growth": [
    {
      "size_diff": 524354,
      "count_diff": 2,
      "traceback": [
        "src/lh_harness/supervisor/control_bus.py:492",
        "src/lh_harness/supervisor/service.py:1724",
        "src/lh_harness/webapi/server.py:3052",
        "..."
      ],
      "location": "src/lh_harness/supervisor/control_bus.py:492"
    }
  ]
}
```

`size_diff` is the number of **bytes that grew** at that site and
`count_diff` the number of **blocks**. The `traceback` is the allocation
call chain, innermost frame first, bounded to **6 frames**; `location` is the
innermost frame on its own.

**What to look for:** the site with the largest positive `size_diff` that
persists across several diffs is the structure that grows. A site that
appears once with a positive diff and then returns to zero was transient
work, not a leak.

The live report (`GET /api/debug/memory`) carries more context:

| key | meaning |
|---|---|
| `rss_bytes` | this process's resident set size (from `/proc/self/status`) |
| `tracemalloc.current_bytes` | bytes currently tracked by `tracemalloc` |
| `tracemalloc.peak_bytes` | peak tracked bytes since the process started |
| `tracemalloc.top_allocations` | top **30** allocation groups by size, each with its count and a depth-**6** `file:line` traceback |
| `gc.count` | `gc.get_count()` — the per-generation collection counters |
| `gc.tracked_objects` | `len(gc.get_objects())` |
| `gc.type_buckets` | the tracked objects bucketed by type, top **15** types |
| `caches.dashboard_runs_scan_cache_len` | length of the `DashboardState` runs-scan cache |
| `caches.state_registry_entries` | entries in the per-run state registry |
| `caches.snapshot_cache_entries` | entries in the per-run snapshot cache |
| `snapshots_held` / `snapshot_cap` | how many snapshots are retained (max **3**) |

## 4. Disable

Remove the flag and restart the web process:

```bash
unset LH_HARNESS_MEM_DEBUG
```

After the restart the routes answer `404` again and `tracemalloc` is no
longer running, so the process pays no tracing overhead.

---

## Boundaries (what this feature deliberately does not do)

* **No restart is needed to measure.** Only the initial enable and the final
  disable require a restart; every measurement in between is live.
* **Snapshots are in memory only.** Nothing is written to disk: the process
  being measured is the one short of memory, and a diagnostic must not add
  durable state of its own.
* **Overhead is bounded.** 25 traceback frames at startup, 6 frames in each
  rendered traceback, 30 allocation groups, 15 gc type buckets, 30 diff
  entries, 3 retained snapshots.
* **The routes are invisible without the flag** — `404`, not `403`, so a
  scanner cannot learn that a diagnostic endpoint exists.
* **The bearer boundary is unchanged.** The routes inherit the single
  `/api/` bearer middleware; there is no bypass.
* **No secrets are reported.** The payload carries sizes, counts and
  `file:line` paths — never file contents, tokens or environment values.

## Troubleshooting

| symptom | meaning |
|---|---|
| `404` on every memory route | `LH_HARNESS_MEM_DEBUG` is not `1` in the web process environment |
| `401` | missing or wrong bearer; send `Authorization: Bearer <token>` |
| `415` on the snapshot `POST` | missing `Content-Type: application/json` |
| `409` on the snapshot route | `tracemalloc` is not running — the flag was set after the process started, so restart it |
| `409` on the diff route | fewer than two snapshots; `POST` the snapshot route twice |
| `tracemalloc.tracing` is `false` | `tracemalloc` was stopped, or the process started without the flag |
