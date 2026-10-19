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
5. **All thresholds are in the settings DB** (see
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

A setting of `0` (or negative for the cache cap) disables the rule that
owns it.  Every setting is editable from the admin screen; the names
match the `SettingSpec` rows added to
`src/lh_harness/settings_store.py:CATALOG`.

## State machine

| Disk usage | `state` in `/api/meta.disk` | Launcher behaviour |
|---|---|---|
| `< resume_pct` | `ok` | Launches allowed |
| `>= warn_pct` and `< pause_pct` | `warn` | Launches allowed; a `disk.warning` event is emitted |
| `>= pause_pct` | `paused` | Launches refused with `skip_reason: disk_full` |
| Was `paused`, now `< resume_pct` | `resumed` | Launches allowed again |

## Tests

`tests/test_disk_retention.py` exercises every rule with mock
filesystems and a fake settings DB.  Run with:

```bash
pytest tests/test_disk_retention.py -q
```
