# Disk retention (CT110-DISK)

The harness ships a disk-retention subsystem that prunes five categories of
state when a node starts running low on disk:

1. **Run folders** older than `LH_HARNESS_DISK_RUN_MAX_AGE_DAYS` (default
   14 d).  Failed-run report and summary files are moved to
   `<runs-root>/_failed-reports/<run-id>/` *before* the folder is deleted.
2. **Parked work trees** idle more than
   `LH_HARNESS_DISK_WORKTREE_MAX_AGE_DAYS` (default 7 d).  Dirty trees are
   listed in the report and *not* deleted.
3. **npm and pip caches**, capped at
   `LH_HARNESS_DISK_{NPM,PIP,CACHE}_MAX_BYTES` (default 4 GiB each),
   pruning oldest entries first.
4. **Daily disk check**: warn at `LH_HARNESS_DISK_WARN_PCT` (default 80 %)
   and pause new launches at `LH_HARNESS_DISK_PAUSE_PCT` (default 90 %).
   The pause is sticky until a fresh check sees usage below
   `LH_HARNESS_DISK_RESUME_PCT` (default 85 %).
5. **Terminal-run clone workspaces** (easybutt0n-17): once *every* run and
   queue entry that names a working clone under the node work root is
   terminal in its own vocabulary — a run in `completed`, `failed`,
   `cancelled`, `blocked` or `incomplete`; a queue entry only in `done` or
   `failed` (a queue `blocked`/`pending`/`launched` entry is still parked
   work and keeps the clone) — and the clone has stayed that way for more
   than `LH_HARNESS_DISK_WORKSPACE_MAX_AGE_HOURS` (default 336 h = 14 days;
   `0` or a negative value disables this rule), the clone
   is a removal candidate.  **Opt-in**: nothing is deleted unless
   `LH_HARNESS_DISK_WORKSPACE_RETENTION_ENABLED` is `true` (default
   `false`).  See "Terminal-run clone workspaces" below.
6. **All thresholds are in the settings DB** (see
   `docs/settings-store.md`); no threshold is hard-coded outside the loader
   in `src/lh_harness/disk_retention/config.py`.  Each appears on the
   admin screen next to the other settings.

Every sweep is **dry-run by default**.  The first real prune is an
operator step, never a run:

```bash
# 1. See what would change (writes the report; touches no files):
lh-harness retention --runs-root /home/harness/work/q2-LongHorizon-Harness/.lh-harness/runs \
    --worktree-dirs /home/harness/work/q2-LongHorizon-Harness/.lh-harness/worktrees \
    --npm-cache ~/.npm/_cacache --pip-cache ~/.cache/pip \
    --report /tmp/retention-dryrun.json

# 2. Read the report, then re-run with --apply to actually move/delete:
lh-harness retention ... --apply
```

The launcher reads the same disk check (`/api/meta.disk.state` and
`/api/meta.disk.skip_reason`) and refuses new launches with the skip
reason `disk_full` while the disk is at or over `pause_pct`.  The
dashboard's "Disk" tile polls the same `/api/meta` payload, so the
launcher, the operator's CLI, and the web workbench all see the same
state.

## Settings reference

| Setting | Default | Meaning |
|---|---|---|
| `LH_HARNESS_DISK_RUN_MAX_AGE_DAYS` | 14 | Run-folder age threshold |
| `LH_HARNESS_DISK_WORKTREE_MAX_AGE_DAYS` | 7 | Parked work-tree idle threshold |
| `LH_HARNESS_DISK_CACHE_MAX_BYTES` | 4294967296 (4 GiB) | Umbrella cap; per-cache cap falls back to this |
| `LH_HARNESS_DISK_NPM_CACHE_MAX_BYTES` | (umbrella) | npm cache cap |
| `LH_HARNESS_DISK_PIP_CACHE_MAX_BYTES` | (umbrella) | pip cache cap |
| `LH_HARNESS_DISK_WARN_PCT` | 80 | Disk usage that triggers a warning |
| `LH_HARNESS_DISK_PAUSE_PCT` | 90 | Disk usage that pauses new launches |
| `LH_HARNESS_DISK_RESUME_PCT` | 85 | Disk usage under which launches auto-resume |
| `LH_HARNESS_DISK_WORKSPACE_RETENTION_ENABLED` | `false` | Explicit opt-in for terminal-run clone-workspace removal |
| `LH_HARNESS_DISK_WORKSPACE_MAX_AGE_HOURS` | 336 (14 d) | Age a terminal-run clone must exceed before it can be removed; `0` or negative disables the rule |

For `LH_HARNESS_DISK_WORKSPACE_MAX_AGE_HOURS`, a setting of `0` (or any
negative value) disables the rule: no clone is ever old enough, so the
sweep selects nothing and removes nothing even with enforcement on.  For
`LH_HARNESS_DISK_CACHE_MAX_BYTES` (and the per-cache caps), `0` or
negative means "no cap".  This does **not** hold for
`LH_HARNESS_DISK_RUN_MAX_AGE_DAYS` or
`LH_HARNESS_DISK_WORKTREE_MAX_AGE_DAYS`: those are plain age thresholds,
so `0` (or a negative) makes every run folder / parked work tree that is
not live eligible — set them to a positive number.  Every setting is
editable from the admin screen; the names match the `SettingSpec` rows
added to `src/lh_harness/settings_store.py:CATALOG`.

## Terminal-run clone workspaces

A run's *working clone* is the directory the supervisor resolves for the
run under the node work root (`--workspace-root`, e.g.
`/home/harness/work/<workspace>/`).  It is **not** run evidence; nothing
under `.lh-harness/runs/` is ever a candidate, and neither is a workspace
named by a queue entry in any non-terminal queue state (`pending`,
`launched` or `blocked`).

The selection rule is the pure function
`lh_harness.disk_retention.workspaces.should_retain_workspace(path,
states, age_seconds, max_age_seconds)`.  It selects a clone only when
**all** of the following hold; anything else keeps it:

* no path component is `.lh-harness`;
* at least one run or queue entry names the clone (an orphan is kept —
  we do not know what it is);
* every referencing run and queue entry is terminal **in its own
  vocabulary**.  A run is judged by the supervisor lifecycle states
  (`completed`, `failed`, `cancelled`, `blocked`, `incomplete`); a queue
  entry only through the explicit allowlist `done`/`failed`
  (`QUEUE_TERMINAL_STATUSES`).  The two vocabularies share the word
  `blocked` with opposite meanings — a *run* `blocked` is terminal, a
  *queue* `blocked` is still parked work (`queue._NON_TERMINAL_STATUS`) —
  so references are tagged by source and never conflated.  A run in
  `running`, `waiting_approval`, `queued` or `pending`, a queue entry in
  `pending`/`launched`/`blocked`, an unknown state, a mix of terminal and
  non-terminal, all keep it;
* the clone's age is strictly greater than the threshold (equal keeps).

The age is measured from the most recent `finished_at` of a referencing
terminal run (the run's own terminal-state timestamp in `status.json`),
falling back to the clone directory's mtime when no run left one.  The
source is reported per entry as `age_source`.

Only direct children of the work root are visited; symlinks are never
followed or removed.  The launcher runs the sweep once per scheduling
pass **only while the flag is set** (with it off the launcher never
touches the work root); the operator CLI lists the same candidates with
`du`-style sizes regardless of the flag:

```bash
# List candidates with sizes; removes nothing regardless of the flag:
lh-harness retention --runs-root <runs-root> --work-root /home/harness/work \
    --report /tmp/retention-dryrun.json

# Only with BOTH --apply and LH_HARNESS_DISK_WORKSPACE_RETENTION_ENABLED=true
# are candidates removed; otherwise the pass is identical to the dry-run:
lh-harness retention --runs-root <runs-root> --work-root /home/harness/work --apply
```

Report states: `selectable` (candidate; would be / was removed),
`removed`, `kept_harness_owned`, `kept_no_references`,
`kept_active_reference`, `kept_recent`, `kept_uncommitted`, `skipped`,
`error`.

A terminal-and-old clone is a candidate only when it is *proved* clean and
pushed (easybutt0n-304).  The sweep runs three read-only git probes into
the clone (`git --no-optional-locks status --porcelain`, `git
--no-optional-locks rev-list --count HEAD --not --remotes`, and `git
--no-optional-locks rev-parse --abbrev-ref --symbolic-full-name
@{upstream}`, each with a 10 s timeout) and keeps the clone with the
reason `kept_uncommitted` — listed in the dry-run, never removed under
enforcement — when *any* of these holds: the tree has uncommitted changes
(tracked or untracked); HEAD has a commit no remote branch carries; the
checked-out branch has no upstream (a detached HEAD included, since
"pushed" cannot then be proved); any git probe fails or times out; or the
directory is not a git repository.  A probe that cannot tell never reads
as clean.

Enabling this on a node is a tooled-seat action: overseer1 or
overseer1-op enables it after review, dry-run first; a run never enables it.

## State machine

| Disk usage | `state` in `/api/meta.disk` | Launcher behaviour |
|---|---|---|
| `< resume_pct` | `ok` | Launches allowed |
| `>= warn_pct` and `< pause_pct` | `warn` | Launches allowed; a `disk.warning` event is emitted |
| `>= pause_pct` | `paused` | Launches refused with `skip_reason: disk_full` |
| Was `paused`, now `< resume_pct` | `resumed` | Launches allowed again |

## Tests

`tests/test_disk_retention.py` exercises every rule with mock
filesystems and a fake settings DB; `tests/test_workspace_retention.py`
covers the terminal-run clone-workspace predicate, dry-run, enforcement
gate and settings defaults.  Run with:

```bash
pytest tests/test_disk_retention.py tests/test_workspace_retention.py -q
```
