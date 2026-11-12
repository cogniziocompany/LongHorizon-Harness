# easybutt0n-131 R8 D1 — CT111 ghost-capacity diagnosis and minimal source fix

Round: round_008 (edit episode). Branch `easybutt0n-131/ghost-capacity-d1`,
base `main`. This round does **not** push and does **not** open a PR; push and
the single PR are the next round's dominant change, preserving push-before-PR.

Dedup: `yt:easybutt0n-131`.

## 1. Base

| item | value |
| --- | --- |
| Base sha (audited, round_003, re-verified round_006) | `b4e86bf9bec4930207efe5730107e116c5dceb81` |
| `origin/main` at this episode's fetch probe | `b4e86bf9bec4930207efe5730107e116c5dceb81` (delta 0/0 — unmoved, nothing absorbed) |
| Branch | `easybutt0n-131/ghost-capacity-d1` |
| Commits beyond base at episode start | 0 |

The base is the FULL current LongHorizon-Harness `main` (merge of PR #198). The
one read-only fetch probe moved `origin/main` by zero commits, so no drift had
to be surfaced or absorbed.

Head sha is returned via the round output rather than baked into this file;
it is recomputable from the branch (`git rev-parse HEAD`) and the next round
records it for the PR.

## 2. Diagnosis — exact counter / resync source

**Counting site (the defect):**

- File: `src/lh_harness/fleet/reporter.py`
- Function: `FleetReporter.queue_heartbeat`
- Lines: 315–319 (the predicate is line 318)

```
315        for run in runs:
316            status = canonical_lifecycle_status(run.get("status"))
317            runs_by_status[status] = runs_by_status.get(status, 0) + 1
318            if status not in TERMINAL_STATUSES and not _is_ghost_capacity_row(run):
319                active_runs.append(run)
```

Before this change line 318 read `if status not in TERMINAL_STATUSES:`. This
loop is what decides which runs are serialized into the heartbeat `runs[]` —
the list the fleet plane (and the settings_store-facing aggregate) treats as
the node's active set. `capacity.active` on the heartbeat body
(`reporter.py:346`) is a separate, supervisor-owned process count; the
aggregate that inflated on CT111 is this non-terminal row count.

**Why it over-counts — the canonicalizer:**

- File: `src/lh_harness/supervisor/lifecycle.py`
- Function: `canonical_lifecycle_status`
- Line: 58 (defaults to `"idle"`); `TERMINAL_STATUSES` at line 24

`canonical_lifecycle_status` maps a blank/absent/unknown status to `"idle"`,
and `"idle"` is deliberately **not** in `TERMINAL_STATUSES`. So the single
predicate at line 318 counts any row whose status text is missing, blank, or
still `idle` as an active run — forever.

**Where those rows come from (the resync sources):**

- File: `src/lh_harness/supervisor/service.py`
  - `RunSupervisor.list_run_items`, lines 1832–1839 — reads the durable
    `control/status.json`; a terminal status is used directly, anything else
    goes through `_refresh`.
  - `RunSupervisor.list_run_summaries`, line 1732 —
    `status_name = str(status.get("status") or owner.get("status") or "idle")`,
    so a run with no durable status text is reported as `idle`.

### The two row classes (both empirically reproduced in-episode)

Reproduced by constructing real run directories under a temp runs root and
running the production row path (`RunSupervisor.list_run_items` →
`build_run_summary`, exactly what `webapi/server.py::_heartbeat` feeds the
reporter). Observed with the pre-fix predicate, 8 rows → 6 counted active:

| row | durable `status.json` | pre-fix canonical | pre-fix counted |
| --- | --- | --- | --- |
| `a-cancel-owner-state` (class a: cancel in `owner.json`) | `{"status":"idle","alive":false}` | `idle` | **yes** |
| `a-cancel-event-only` (class a: cancel only in event ledger) | `{"status":"idle","alive":false}` | `idle` | **yes** |
| `b-stale-no-status` (class b) | `{"status":"idle","alive":false}` | `idle` | **yes** |
| `b-stale-blank-status` (class b) | `{"status":"idle","alive":false}` | `idle` | **yes** |
| `b-stale-idle-dead` (class b) | `{"status":"idle","alive":false}` | `idle` | **yes** |
| `live-run` | `{"status":"running","alive":true}` | `running` | yes (correct) |
| `finished-run` | `{"status":"completed",...}` | `completed` | no (correct) |

Class (a): the operator's cancellation is durable in `control/owner.json`
(`state`) or in the role event ledger, but `control/status.json` was never
rewritten to a terminal spelling, so the row canonicalizes to `idle`.

Class (b): the stale row's control record still says `idle` with `alive:false`
— nothing durable claims a live worker — yet `idle` is non-terminal, so the
row is counted forever. This is the shape that lets a node with two real runs
advertise 7 (CT111, claude-code-teams PR 241, defect D1).

## 3. The fix

Minimal and confined to the traced counting path.

- `src/lh_harness/fleet/reporter.py`
  - New `_is_ghost_capacity_row` (line 754) — consults the run's durable
    control record via the new `_read_control_json` helper, which reuses the
    module's existing `O_NOFOLLOW` + single-hard-link open discipline from
    `_read_report`.
  - Line 318 gains `and not _is_ghost_capacity_row(run)`.

Exclusion happens **only on positive durable evidence**:

1. durable cancellation in `owner.json`/the last event → ghost (class a);
2. `alive` explicitly `false` with a non-active status → ghost (class b);
3. a terminal status in `status.json` → not a ghost (already excluded);
4. `owner.json` carrying a positive pid → liveness evidence, not a ghost;
5. absent `alive` / absent control record → **unchanged** behaviour.

Rule 5 is what keeps the documented invariant in the same function ("a
malformed record is reported, never silently dropped") and keeps stores
written before the liveness field existed behaving exactly as before. An
earlier draft of the fix treated a missing control record as a ghost; six
pre-existing tests failed and the draft was narrowed rather than the tests
being relaxed.

`maxConcurrentRuns` is not read, written, or otherwise touched. No capacity,
config, queue-behaviour, restart, or deploy surface is modified.

## 4. Tests

New tests in `tests/fleet/test_reporter.py`, all built from real supervisor
rows over a temp runs root:

- `test_heartbeat_excludes_intact_cancelled_rows_from_active` — class (a).
- `test_heartbeat_excludes_stale_missing_terminal_record_rows` — class (b).
- `test_heartbeat_active_count_matches_serialized_rows` — the aggregate vs
  serialized-list invariant behind D1.

All runs were executed in this episode, under bytecode/cache-disabling guards
(`PYTHONDONTWRITEBYTECODE=1`, `sys.dont_write_bytecode` from `tests/conftest.py`,
`-p no:cacheprovider`), scoped to `tests/fleet/test_reporter.py`. No full-suite
run.

Command (the repo's own `make test` shape; the dependency path is a shim over
the host venv that omits the installed `lh_harness` copy so `conftest.py`'s
TASK-211 src retarget is not stripped):

```
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/tmp/e131-r8/deps \
  /home/harness/venv/bin/python -m pytest tests/fleet/test_reporter.py \
  -p no:cacheprovider -q
```

| phase | when | result |
| --- | --- | --- |
| Baseline (pre-test, pre-fix) | before any edit | **34 passed** in 16.54s |
| Fail-first, class (a) | tests added, fix absent | **FAILED** — `assert 'ghost-cancel-owner-state' not in {…5 ghost ids…}` |
| Fail-first, class (b) | tests added, fix absent | **FAILED** — `assert 'ghost-stale-no-status' not in {…5 ghost ids…}` |
| Fail-first, invariant | tests added, fix absent | **FAILED** — `assert 6 == 1` (6 rows counted active, only 1 live) |
| Fail-first, whole file | tests added, fix absent | **3 failed, 34 passed** in 18.16s |
| Post-fix (first, too broad) | fix v1 | **6 failed, 31 passed** — narrowed instead of relaxing tests |
| **Post-fix (final)** | final fix | **37 passed** in 18.19s |

37 = the 34 pre-existing tests plus the 3 new ones, with no pre-existing test
changed.

## 5. Rollback

Revert the single PR:

```
git revert <merge-or-squash-sha-of-the-one-PR>
# or, before merge, simply delete the branch:
git push origin --delete easybutt0n-131/ghost-capacity-d1
```

The change is one source hunk (`reporter.py:318`) plus two new module-private
helpers and the new tests; no data migration, no persisted state, and no
configuration is involved, so reverting restores the previous aggregate
behaviour exactly.

## 6. Return package

| item | value |
| --- | --- |
| Base sha | `b4e86bf9bec4930207efe5730107e116c5dceb81` |
| Head sha | returned via round output (recomputable: `git rev-parse HEAD`); not required in this file |
| Branch | `easybutt0n-131/ghost-capacity-d1` |
| Test command | `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/tmp/e131-r8/deps /home/harness/venv/bin/python -m pytest tests/fleet/test_reporter.py -p no:cacheprovider -q` |
| Baseline / fail-first / post-fix | 34 passed / 3 failed, 34 passed / 37 passed |
| Diagnosis | `src/lh_harness/fleet/reporter.py::FleetReporter.queue_heartbeat` line 318; canonicalizer `src/lh_harness/supervisor/lifecycle.py::canonical_lifecycle_status` line 58 |
| Rollback | revert the one PR |
| Report path | `docs/reports/easybutt0n-131-ghost-capacity-d1-report.md` |

### Note on the test command's `PYTHONPATH`

The host interpreter has no `fastapi`; the project dependencies live in
`/home/harness/venv`, whose `site-packages` also contains an installed
`lh_harness` copy. `tests/conftest.py` (TASK 211) deliberately strips any
`sys.path` entry that is a `site-packages` containing `lh_harness`, so pointing
`PYTHONPATH` at that directory drops `fastapi` along with the stale copy.

`/tmp/e131-r8/deps` is therefore a directory of symlinks to every entry of that
`site-packages` **except** the `lh_harness` copies. `PYTHONPATH` points there,
so `conftest.py`'s strip pass finds nothing to remove and the src retarget runs
as intended. The tests import `lh_harness` only from this checkout's `src/`.

Equivalent recipe on a clean host:

```
python3 -m venv /tmp/e131-venv && /tmp/e131-venv/bin/pip install -e '.[test]'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  /tmp/e131-venv/bin/python -m pytest tests/fleet/test_reporter.py \
  -p no:cacheprovider -q
```

No file inside the repository was changed to make this work, and no cache or
bytecode artifact was written into the tree (verified: `git status`,
`git status --ignored`, `git clean -nxd` and
`git ls-files --others --ignored --exclude-standard` are all empty after the
final run).

## 7. Bounds observed

- No CT110/CT111 HTTP, SSH, `pct`, remote-exec, or gateway host access. No
  remote-pc/fleet tooling. Live node state was never needed: the mechanism is
  fully determined by repo source plus the supplied PR 241 evidence, and both
  row classes were reproduced locally.
- No restart, no cancel/queue/capacity change, no deploy, no merge, no
  secrets read or printed, no artificial load, no PASS/LIVE credit claimed.
- No writes to `ptait09`; CrashPlan and `terranas01` untouched.
- Harness-owned paths were never read, listed, or used.
- Not pushed, no PR opened this round.
