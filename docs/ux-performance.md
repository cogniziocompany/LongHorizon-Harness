# Web UX snapshot performance

The slice-4 work introduced server-side caching for the `/api/runs/{id}/snapshot` endpoints so the dashboard no longer rebuilds the full run tree on every poll. This page documents the before/after latency measured with `scripts/bench_snapshot.py`.

## Methodology

Benchmark command:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
    .venv-dev/bin/python scripts/bench_snapshot.py --rounds=16 --iterations=20
```

The script builds a synthetic run with 16 rounds and 800 events, then measures:

* `full snapshot (cached path)` — repeated `GET /api/runs/{id}/snapshot`
* `summary snapshot (cached path)` — repeated `GET /api/runs/{id}/snapshot?fields=summary`
* `full snapshot (first-touch / uncached)` — a fresh app with an empty cache
* `summary snapshot (first-touch / uncached)` — a fresh app with an empty cache

**Before state** was captured by checking out commit `720a1a2` (the parent of the slice-4 commit `10663aa`) into a temporary directory, restoring `.venv-dev` and the benchmark script, and running the same command. This gives the old full-rebuild path.

**After state** is the current `feat/queue-e2e-and-docs` tree.

## Results

| Metric | Before (ms) | After (ms) | Change |
|---|---|---|---|
| full snapshot p50 | 35.8 | 4.1 | -88.5 % |
| full snapshot p95 | 37.2 | 4.9 | -86.8 % |
| summary snapshot p50 | 35.7 | 2.7 | -92.4 % |
| summary snapshot p95 | 36.8 | 3.6 | -90.2 % |
| full first-touch p50 | 35.9 | 37.5 | +4.5 % |
| summary first-touch p50 | 35.6 | 2.8 | -92.1 % |

All numbers were produced on the same machine and venv (Python 3.12.3, local SSD).

## Interpretation

* Cached full snapshots dropped from ~36 ms to ~4 ms. The old code rebuilt the entire run tree every poll; the new path stores the parsed state and only invalidates when the run changes.
* Cached summary snapshots dropped to ~2–3 ms, which is the common dashboard poll path. The dashboard now stays responsive during long runs.
* First-touch full latency is essentially unchanged (~37 ms). That cost is unavoidable on initial load because the same filesystem walk still happens once.
* First-touch summary latency also dropped to ~3 ms because the summary branch now short-circuits before the expensive full build.

The script also asserts the cached summary path stays under 300 ms; it passes.

## Reproduce

For the current tree (after):

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
    .venv-dev/bin/python scripts/bench_snapshot.py
```

For the old full-rebuild baseline (before):

```bash
TMP=$(mktemp -d)
git clone --depth 2 file:///path/to/repo "$TMP"
cd "$TMP"
git checkout 720a1a2
# copy .venv-dev and scripts/bench_snapshot.py from the current tree
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
    .venv-dev/bin/python scripts/bench_snapshot.py
```
