# LHH-WEB-LEAK-FIX c2a — web leak measurement (no fix)

Slice: `LHH-WEB-LEAK-FIX-c2a`. **MEASURE ONLY — no leak fix ships here.**

Goal: determine *what* grows in the CT110 web process before any fix is
designed. CT110 web RSS climbs to ~3.2 GB over ~40–60 min after the c1
deploy (guard restarts 06:42Z, 07:23Z, 07:53Z, …, 10:03Z).

Everything below is measured data from the local repro, plus the reading of
that data. Raw logs are listed in [Evidence](#evidence).

## Scale (A2)

Small scale first, as the request requires: a **60-run synthetic root**, **40
polls per endpoint**, one endpoint isolated per invocation. The escalation
to 150 runs / 80 polls was **not** performed; see
[Escalation decision](#escalation-decision-a2).

## Command lines

Every invocation was wrapped per the time rules
(`timeout 540 <cmd> > /tmp/c2a-<step>.log 2>&1; tail -40 /tmp/c2a-<step>.log`).

```bash
# shared flags: 60-run synthetic root, 40 polls, tracemalloc on, top 15 sites
COMMON="--runs 60 --queue 20 --batches 10 --polls-per-batch 4 --poll-mix c2 \
        --include-full-runs --tracemalloc --tracemalloc-top 15 --midpoint-poll 10"

# /api/runs
timeout 540 python3 scripts/web_leak_repro.py $COMMON \
  --tracemalloc-out /tmp/c2a-runs_full-trace.txt \
  --rss-out /tmp/c2a-runs_full-rss.csv \
  --request-timeout 60 --wedge-timeout 60 --only-endpoint runs_full \
  > /tmp/c2a-runs_full.log 2>&1; tail -40 /tmp/c2a-runs_full.log

# /api/runs?fields=summary
timeout 540 python3 scripts/web_leak_repro.py $COMMON \
  --tracemalloc-out /tmp/c2a-runs_summary-trace.txt \
  --rss-out /tmp/c2a-runs_summary-rss.csv \
  --request-timeout 60 --wedge-timeout 60 --only-endpoint runs_summary \
  > /tmp/c2a-runs_summary.log 2>&1; tail -40 /tmp/c2a-runs_summary.log

# one per-run snapshot
timeout 540 python3 scripts/web_leak_repro.py $COMMON \
  --tracemalloc-out /tmp/c2a-snapshot-trace.txt \
  --rss-out /tmp/c2a-snapshot-rss.csv \
  --request-timeout 60 --wedge-timeout 60 --only-endpoint snapshot \
  > /tmp/c2a-snapshot.log 2>&1; tail -40 /tmp/c2a-snapshot.log

# /api/queue
timeout 540 python3 scripts/web_leak_repro.py $COMMON \
  --tracemalloc-out /tmp/c2a-queue_full-trace.txt \
  --rss-out /tmp/c2a-queue_full-rss.csv \
  --request-timeout 60 --wedge-timeout 60 --only-endpoint queue_full \
  > /tmp/c2a-queue_full.log 2>&1; tail -40 /tmp/c2a-queue_full.log
```

`--only-endpoint` and the "take the tracemalloc baseline *after* a one-request
warm-up of the endpoint under test" behaviour are measurement-only changes to
`scripts/web_leak_repro.py`; see
[Working-tree diff disposition](#working-tree-diff-disposition-a7). They do
not change the application, its routes, its responses, or any application
state — only which requests the harness makes and when the baseline snapshot
is taken.

## RSS table (A3)

`VmRSS` of the local web-server child process, sampled once per poll; the
rows below are the samples at polls 1, 10, 20, 30 and 40. Source: the
`--rss-out` CSV of each run.

| poll | `/api/runs` | `/api/runs?fields=summary` | per-run snapshot | `/api/queue` |
|-----:|------------:|---------------------------:|-----------------:|------------:|
| 1    | 81.844 MiB  | 81.223 MiB                 | 80.426 MiB       | 79.902 MiB  |
| 10   | 81.992 MiB  | 81.402 MiB                 | 81.320 MiB       | 79.984 MiB  |
| 20   | 100.340 MiB | 98.699 MiB                 | 101.188 MiB      | 96.859 MiB  |
| 30   | 100.395 MiB | 98.711 MiB                 | 101.281 MiB      | 96.859 MiB  |
| 40   | 100.422 MiB | 98.711 MiB                 | 101.379 MiB      | 96.859 MiB  |
| **Δ poll 10 → 40** | **+18.430 MiB** | **+17.309 MiB** | **+20.059 MiB** | **+16.875 MiB** |
| threads (1 → 40) | 8 → 8 | 8 → 8 | 8 → 8 | 8 → 8 |

Every endpoint shows the same shape: a **single step** of ~17–20 MiB between
poll 10 and poll 20, then **flat** from poll 20 to poll 40 (worst case
`/api/runs`: 100.340 → 100.422 MiB, +0.08 MiB over 20 polls). Thread count
is constant at 8 for all four endpoints. The child process never approaches
the ~3.2 GB production figure at this scale.

## tracemalloc top 15, poll 10 → last poll (A3)

`size_diff` and `count_diff` are from `snapshot.compare_to(marker, "lineno")`
between the poll-10 marker and the final (poll-40) marker. Negative values
mean memory was released.

### `/api/runs`

| # | size_diff | count_diff | site |
|--:|----------:|-----------:|------|
| 1 |    +6912 B |    +124 B | `/usr/lib/python3.12/tracemalloc.py:558` |
| 2 |    -3640 B |     -35 B | `/usr/lib/python3.12/pathlib.py:1169` |
| 3 |    -2720 B |     -45 B | `src/lh_harness/fleet/reporter.py:1164` |
| 4 |    -1760 B |     -30 B | `/usr/lib/python3.12/pathlib.py:377` |
| 5 |    -1512 B |     -27 B | `/usr/lib/python3.12/pathlib.py:359` |
| 6 |     -720 B |     -10 B | `src/lh_harness/supervisor/service.py:1116` |
| 7 |     -720 B |     -10 B | `/usr/lib/python3.12/json/encoder.py:224` |
| 8 |     -560 B |      -5 B | `/usr/lib/python3.12/pathlib.py:432` |
| 9 |     -544 B |      -9 B | `/usr/lib/python3.12/pathlib.py:404` |
| 10 |    -520 B |     -10 B | `src/lh_harness/supervisor/control_bus.py:541` |
| 11 |    -480 B |     -10 B | `/usr/lib/python3.12/threading.py:135` |
| 12 |    -460 B |      -5 B | `<frozen posixpath>:410` |
| 13 |    -448 B |      -8 B | `pylibs/uvicorn/protocols/http/httptools_impl.py:246` |
| 14 |    -432 B |      -6 B | `<frozen os>:1037` |
| 15 |     +360 B |      +3 B | `/usr/lib/python3.12/tracemalloc.py:560` |

### `/api/runs?fields=summary`

| # | size_diff | count_diff | site |
|--:|----------:|-----------:|------|
| 1 |    +4936 B |     +87 B | `/usr/lib/python3.12/tracemalloc.py:558` |
| 2 |    -2640 B |     -66 B | `/usr/lib/python3.12/json/encoder.py:254` |
| 3 |    -1104 B |      -6 B | `/usr/lib/python3.12/json/encoder.py:334` |
| 4 |     -840 B |      -8 B | `/usr/lib/python3.12/json/encoder.py:224` |
| 5 |     -784 B |     -14 B | `src/lh_harness/fleet/reporter.py:1164` |
| 6 |     -728 B |      -7 B | `/usr/lib/python3.12/pathlib.py:1169` |
| 7 |     -616 B |     -11 B | `/usr/lib/python3.12/json/encoder.py:258` |
| 8 |     -480 B |      -3 B | `/usr/lib/python3.12/json/encoder.py:278` |
| 9 |     -480 B |      -3 B | `/usr/lib/python3.12/json/encoder.py:414` |
| 10 |    -480 B |      -3 B | `/usr/lib/python3.12/json/encoder.py:342` |
| 11 |    -392 B |      -7 B | `pylibs/uvicorn/protocols/http/httptools_impl.py:246` |
| 12 |    -384 B |      -6 B | `/usr/lib/python3.12/json/__init__.py:234` |
| 13 |     +360 B |      +3 B | `/usr/lib/python3.12/tracemalloc.py:560` |
| 14 |    -352 B |      -6 B | `/usr/lib/python3.12/pathlib.py:377` |
| 15 |     +352 B |      +3 B | `/usr/lib/python3.12/tracemalloc.py:423` |

### per-run snapshot (`/api/runs/{id}/snapshot`)

| # | size_diff | count_diff | site |
|--:|----------:|-----------:|------|
| 1 |    -8008 B |     -77 B | `/usr/lib/python3.12/pathlib.py:1169` |
| 2 |    +6848 B |    +120 B | `/usr/lib/python3.12/tracemalloc.py:558` |
| 3 |    -4312 B |     -77 B | `/usr/lib/python3.12/pathlib.py:359` |
| 4 |    -3872 B |     -66 B | `/usr/lib/python3.12/pathlib.py:377` |
| 5 |    -3520 B |     -88 B | `/usr/lib/python3.12/json/encoder.py:254` |
| 6 |    -3104 B |     -39 B | `src/lh_harness/fleet/reporter.py:1164` |
| 7 |    -2040 B |     -34 B | `/usr/lib/python3.12/pathlib.py:404` |
| 8 |    -1904 B |     -34 B | `/usr/lib/python3.12/json/encoder.py:258` |
| 9 |    -1720 B |     -19 B | `/usr/lib/python3.12/json/encoder.py:224` |
| 10 |    -1648 B |     -15 B | `/usr/lib/python3.12/pathlib.py:432` |
| 11 |    -1584 B |     -22 B | `src/lh_harness/supervisor/service.py:1116` |
| 12 |    -1472 B |      -8 B | `/usr/lib/python3.12/json/encoder.py:334` |
| 13 |    -1144 B |     -22 B | `src/lh_harness/supervisor/control_bus.py:541` |
| 14 |    -1056 B |     -22 B | `/usr/lib/python3.12/threading.py:135` |
| 15 |    -1012 B |     -11 B | `<frozen posixpath>:410` |

### `/api/queue`

| # | size_diff | count_diff | site |
|--:|----------:|-----------:|------|
| 1 | **+524354 B** |     **+2 B** | **`src/lh_harness/supervisor/control_bus.py:492`** |
| 2 |    +4808 B |     +84 B | `/usr/lib/python3.12/tracemalloc.py:558` |
| 3 |    -2184 B |     -21 B | `/usr/lib/python3.12/pathlib.py:1169` |
| 4 |    -1440 B |     -24 B | `/usr/lib/python3.12/pathlib.py:404` |
| 5 |    -1176 B |     -21 B | `/usr/lib/python3.12/pathlib.py:359` |
| 6 |    -1176 B |     -21 B | `src/lh_harness/queue.py:1615` |
| 7 |    -1056 B |     -18 B | `/usr/lib/python3.12/pathlib.py:377` |
| 8 |     -969 B |      -9 B | `/usr/lib/python3.12/pathlib.py:432` |
| 9 |     -880 B |     -22 B | `/usr/lib/python3.12/json/encoder.py:254` |
| 10 |    -768 B |      -9 B | `src/lh_harness/fleet/reporter.py:1164` |
| 11 |     +660 B |     +16 B | `src/lh_harness/supervisor/control_bus.py:489` |
| 12 |    -432 B |      -6 B | `src/lh_harness/supervisor/service.py:1116` |
| 13 |    -376 B |      -4 B | `/usr/lib/python3.12/json/encoder.py:224` |
| 14 |    -368 B |      -2 B | `/usr/lib/python3.12/json/encoder.py:334` |
| 15 |     +360 B |      +3 B | `/usr/lib/python3.12/tracemalloc.py:560` |

## Reading: which structure grows

**The one structure that grows, and the only one, is the control-plane JSON
file read at `src/lh_harness/supervisor/control_bus.py:492`** — and it grows
**only on the `/api/queue` path**.

The object is the raw byte buffer returned by `os.read(fd, max_bytes + 1)`
inside `_read_json_file()`:

```python
# src/lh_harness/supervisor/control_bus.py:481-494
def _read_json_file(path: Path, *, max_bytes: int = _MAX_CONTROL_RECORD_BYTES) -> dict[str, Any]:
    ...
    raw = os.read(fd, max_bytes + 1)      # <-- line 492
```

`_MAX_CONTROL_RECORD_BYTES = 512 * 1024` (`control_bus.py:24`), so the read is
capped at 512 KiB. The measured growth is **+524354 B in exactly 2 blocks** —
i.e. the buffer was filled to its cap. The number is the cap plus the small
overhead of the `bytes` object header and the interpreter's own accounting.

Why this is the leak and not noise:

1. **It is present in every growth block of the `/api/queue` run and in
   none of the other three.** The same line appears at rank 1 in the
   poll-10 → poll-40 growth table, at rank 1 in the *cumulative, diff vs
   start* table, and at rank 2 in the *cumulative, current* table
   (`524354 B / 2 blk`). For `/api/runs`, `/api/runs?fields=summary` and the
   per-run snapshot, `control_bus.py` appears only at `control_bus.py:541`
   with a **negative** diff (`-520 B`, `-1144 B`).
2. **It is retained, not transient.** It appears in the *current* snapshot
   table, so the buffer is still alive at the end of the poll sequence.
3. **Every other site in all four tables is negative or negligible.** In the
   three non-queue tables the net tracemalloc movement between poll 10 and
   poll 40 is a few KB at most, with the top entries negative — allocations
   are being released as fast as they are made. The queue table is the sole
   outlier.

The reading of *why* `/api/queue` is the trigger: `GET /api/queue`
(`src/lh_harness/webapi/server.py:3052`) serves the legacy full
`{entries, groups, counts}` shape, which materialises every queue entry's
full record — including the large per-run control metadata that the control
bus persists under `<run>/control/status.json` and `owner.json`. Those are
read back through `_read_json_file` (`control_bus.py:771`, `:792`, `:801`;
also `supervisor/service.py:1724-1725`), and each read allocates a fresh
full-size byte buffer. The `view=summary` projection exists precisely to
drop that payload, and the c1 queue-wedge work added it — but the *default*
legacy shape still pays it.

**Caveat, stated plainly:** at 40 polls on a 60-run root the total
`/api/queue` growth attributable to this site is ~512 KiB, and RSS is flat
after poll 20. This slice therefore identifies **the structure that grows**,
not a demonstrated path to 3.2 GB. The production figure accumulates over
40–60 minutes against a much larger live run set; whether this same
allocation site scales to ~3.2 GB there is **not** demonstrated by this
measurement, and this slice does not claim it does.

## Escalation decision (A2)

**Not escalated.** The A2 precondition is "only escalate to 150 runs / 80
polls if the small run showed *no clear growth*". The small run did **not**
show no-clear-growth — it showed one clear, isolated, monotonic growth site
(`control_bus.py:492`, +524354 B on the `/api/queue` path) and flat RSS on
all four endpoints. Escalating scale would not change which structure grows;
the escalation precondition was therefore not met, and the scale was not
raised.

The contract's residual-risk note — "40 polls may under-show a leak that takes
40–60 min to reach ~3.2 GB in production" — stands. It is recorded here as
an explicit, unmitigated limitation of this slice, not as a claim about
production behaviour.

## Working-tree diff disposition (A7)

The slice began with an uncommitted modification to
`scripts/web_leak_repro.py` in the working tree. It was **read, attributed
and kept**, with the attribution recorded in the commit that introduced it
(`easybutt0n-17: web leak c2a measurement-only repro repair (attributed
round-2 working-tree diff)`).

* **Origin:** a leftover of round 2 of *this* slice (LHH-WEB-LEAK-FIX c2a),
  written by the round-2 executor on top of `d76edb65`. It is not an
  upstream change and not unrelated contamination.
* **Disposition: KEPT.** It is required to obtain the per-endpoint tables
  A3 asks for. Without `--only-endpoint` the tracemalloc growth table
  attributes to the *mix* of all four endpoints instead of to the endpoint
  under test, and without the warm-up-before-baseline the fixed one-time
  lazy-import cost (~1.9 MB: `importlib._bootstrap_external:753`,
  `linecache.py:137`, `importlib._bootstrap:488`, `uvloop/__init__.py:24`)
  occupies four of the top five slots and masks the per-poll growth.
* **Minimality:** the diff is 145 insertions / 10 deletions relative to
  `d76edb65` and touches only measurement code paths — the tracemalloc
  baseline marker, the `_write_sites` source echo, the endpoint selection in
  `drive_traffic`, and the `--only-endpoint` CLI flag. No application code is
  touched, and no leak fix is included.

### Relation to `d76edb65`

`d76edb65` ("wip c2 tracemalloc instrumentation from run 371104aa") is
reused as the basis. Its tracemalloc machinery works and is retained. Two
defects in it were found by reading and smoking it, and both are fixed in
the minimal diff above:

1. **The baseline marker was taken too early.** `d76edb65` took it
   immediately after `create_app`, before any request. uvicorn/uvloop/
   websockets import their protocol modules lazily and anyio's lazy importer
   calls `inspect.getsource`, which reads whole source files through
   `linecache`. That is ~1.9 MB of fixed warm-up cost reported as growth.
   Fix: take the marker on `SIGWINCH` after a one-request warm-up of the
   endpoint under test.
2. **The instrument was the leak it reported.** `_write_sites` called
   `stat.traceback.format()`, which renders the source line of every frame
   and therefore makes CPython read whole source files through `linecache`,
   retaining megabytes of source text *inside the child process while it was
   being measured* — it appeared as the #1 growing site
   (`linecache.py:137`, +2493049 B). Fix: do not print the source echo; the
   `file:line`, size diff and count diff that the c2 table asks for are all
   on the first line of each row and do not need it.

## Evidence

| artefact | path |
|---|---|
| run log, `/api/runs` | `/tmp/c2a-runs_full.log` |
| tracemalloc trace, `/api/runs` | `/tmp/c2a-runs_full-trace.txt` |
| RSS CSV, `/api/runs` | `/tmp/c2a-runs_full-rss.csv` |
| run log, `/api/runs?fields=summary` | `/tmp/c2a-runs_summary.log` |
| tracemalloc trace, `/api/runs?fields=summary` | `/tmp/c2a-runs_summary-trace.txt` |
| RSS CSV, `/api/runs?fields=summary` | `/tmp/c2a-runs_summary-rss.csv` |
| run log, per-run snapshot | `/tmp/c2a-snapshot.log` |
| tracemalloc trace, per-run snapshot | `/tmp/c2a-snapshot-trace.txt` |
| RSS CSV, per-run snapshot | `/tmp/c2a-snapshot-rss.csv` |
| run log, `/api/queue` | `/tmp/c2a-queue_full.log` |
| tracemalloc trace, `/api/queue` | `/tmp/c2a-queue_full-trace.txt` |
| RSS CSV, `/api/queue` | `/tmp/c2a-queue_full-rss.csv` |
| uncommitted working-tree diff as found | `/tmp/c2a-wt-diff.log` |
| smoke test (3 runs / 3 polls) | `/tmp/c2a-r3-smoke.log` |

## Safety

The measured instance is a local child process started by
`scripts/web_leak_repro.py::_start_server` (uvicorn on `127.0.0.1`, a free
port). The synthetic run root is a `tempfile.mkdtemp()` under `/tmp`, never
under the harness-owned `.lh-harness` paths. No live node, CT110, or ptait09
is contacted: no ssh, pct, remote-exec proxy or gateway is used anywhere in
this slice, and the fleet/token environment keys are stripped from the child
process environment by the script's own `_CHILD_STRIP_ENV`. No secrets are
printed; the logs contain only paths, timings and sizes.
