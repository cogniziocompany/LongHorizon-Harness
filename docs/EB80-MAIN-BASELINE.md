# easybutt0n-80: main-baseline evidence (round 3)

Measured BEFORE any fix exists, against origin/main at 03704b23ff66d5a02b7da13be5e2da669f3722e5,
running only this module: `tests/webapi/test_runs_listing_scale_budget.py`.

## Fixture scale

    N=200 run dirs, 25,000 files, 383,385,791 bytes (~366 MB), build ~0.5 s,
    entirely under pytest tmp storage (never the real runs dir).

Shape: 60 ACTIVE (status=running, managed=false, blank report status) +
140 terminal (completed/failed, report carries its status so
report_status/completion_satisfied derive as on a real node). owner.json
carries NO task text and report.json none either, which is exactly the
historical/imported shape that makes the full listing fall back to
_saved_task_from_rounds (a walk of the round directories) per request.

## Command

    env -u PYTHONPATH <repo venv> python -m pytest \
        tests/webapi/test_runs_listing_scale_budget.py -v --tb=short

## Result on main: 2 failed, 4 passed (stable across 3 consecutive runs)

FAILED test_expired_cache_listing_refreshes_incrementally
    the expired-cache listing re-read 402 per-run control records although
    nothing changed (a fresh scan of N=200 runs re-reads ~400); the refresh
    must be incremental, keyed on each run's own status stamp

FAILED test_twenty_parallel_latest_calls_share_one_refresh
    20 concurrent /api/runs/latest requests enumerated the runs root 20
    times; they must share one refresh

PASSED test_runs_latest_within_budget_and_no_round_artifact_reads
PASSED test_summary_listing_within_budget_shape_and_no_round_artifact_reads
PASSED test_full_listing_within_budget_and_no_round_artifact_reads
PASSED test_status_change_is_visible_after_incremental_refresh

## Wall timings on main (same fixture, standalone probe, warm page cache)

    /api/runs/latest          cold ~334 ms   warm ~134 ms   (all 200 active)
                               cold ~ 50 ms   warm ~  37 ms   (60 active)
    /api/runs?fields=summary  cold ~ 18 ms   warm ~ 16 ms
    /api/runs (full)          cold ~1.4-2.0 s warm ~0.40-0.45 s

    8 concurrent GET /api/runs: worst request 4.15 s  (fleet pattern)
    20 concurrent GET /api/runs/latest: worst request 1.01 s, wall 1.02 s

## Honest reading of the baseline

- The two FAILED cases are deterministic, structural reds on main:
  every expired-cache listing re-reads both control records of every run
  (402 reads at N=200, per request, forever), and concurrent /latest
  calls do not share one refresh (each of the 20 enumerates the runs
  root itself).  These are the wedge mechanism from CT111 (GIL-bound
  scanning under a routine poll), independent of disk speed, so they are
  the primary acceptance reds.
- The single-request wall budgets pass on the development machine used
  for this baseline (fast NVMe, warm page cache): main's single-request
  walk cost at N=200 is below 500 ms warm / 3 s cold here.  On CT111-class
  storage under load the same walks measured 25-45 s (the incident).
  The wall assertions stay in the module as the budget the fix must hold
  on every machine; the structural assertions are the ones that are red
  on main everywhere.
- Profile of one cold full listing on main (cProfile, same fixture):
  704 fsync calls (workspace-park bookkeeping, one-shot per run), plus
  the safe_run_* realpath storm (28k _joinrealpath calls).  After parks
  settle, a warm full listing still re-derives every row per request,
  and the full path also walks the round directories of every run whose
  records carry no task text (200 walks per listing on this fixture).
- Method note: the benchmark client is intentionally NOT entered as a
  TestClient context manager.  Entering starts the app lifespan and with
  it the fleet-reporter thread, whose own background listings race the
  request-window counters and made the walk/read assertions flaky (1-19
  stray walks per run).  The plain construction matches the other webapi
  tests in this repo.

## What was measured vs guessed

Measured: everything above (fixture scale, pass/fail per test, wall
timings, concurrency latencies, fsync/pathlib profile, rounds-walk
counts). Guessed: nothing in this note. CT111 facts (196 dirs, 10.8 GB,
35,220 files, 25-45 s timeouts, ~30 s poll cadence) are sizing context
from the request, not re-measured here.
