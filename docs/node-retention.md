# Node work-tree retention (easybutt0n-304)

`lhh-retention.timer` runs `scripts/deploy/retention/lhh_retention.py`
(installed on the node at `/opt/lhh-retention/lhh_retention.py`) once a day
against the node's harness work root `/home/harness/work`. It is a janitor:
its job is to stop dead runs and abandoned copies from filling the disk, while
never touching anything the queue still needs or any copy that holds work that
is not saved anywhere else.

The same script is also the retention rule's source of truth: the ages and the
protected names below are read from it, not from this page. A dry run (the
default) only lists what it *would* do; `--apply` deletes.

## The rule, in plain words

1. **Run records: 30 days.** Each run's record directory under
   `/home/harness/work/.lh-harness/runs/<run id>` is kept for 30 days, then
   deleted. A run the queue still shows as pending, launched, running,
   starting or waiting for approval is never deleted.
2. **Workspace copies: 14 days since last change.** Each top-level directory
   under `/home/harness/work` is kept for 14 days since its last change
   (the newest of the directory's own mtime, its git `HEAD`/`index`/`reflog`
   mtimes, and any file inside it modified within the window), then deleted.
3. **Scratch, parked and loose files: 14 days.** Top-level entries whose names
   start with `_` or `.scratch` (scratch/parked trees) and top-level loose
   files are kept for 14 days, then deleted.

**Never deleted:**

- **Queue-named workspaces**: any workspace a queue entry from the last 30
  days names, and the base workspaces (`cognizioware-mcp-tools`,
  `LongHorizon-Harness`, `claude-code-teams`, `cognizioware-qa`,
  `cognizioware`, `powerplatform`, `cognizioware-omnigent`, `workspace`).
- **Live runs**: a run the queue still shows pending, launched, running,
  starting or waiting for approval.
- **Git copies with uncommitted or unpushed work**: a copy whose `git status`
  is dirty (uncommitted changes) or whose branches hold commits no remote has
  (`unpushed-commits`) is **never deleted — it is flagged** (`FLAG-KEEP`) so a
  human can decide. The same applies when git itself cannot be read
  (`git-unreadable`).

Only the git-unsafe copies produce `FLAG-KEEP` lines in the output; the
queue-named and live protections above apply silently, and the pass's total
line counts them (`protected live runs N, queue-named workspaces N`).

Also never touched, whatever their age: `.lh-harness`, `.git`, `.cache`,
`.config`, `.ssh`, `.local` and `.npm` at the top of the work root.

Every pass prints one line per item it removes (name, size, reason) plus one
flag line per flagged item, and ends with a total, e.g.:

```
TOTAL would delete: 3 items, 2.1G; flagged 1; protected live runs 0, queue-named workspaces 12
```

This is separate from the in-service disk-retention subsystem
(`docs/disk-retention.md`), which prunes run folders and caches when a node
runs *low on disk*; this rule is age-based and runs regardless of disk state.

## Schedule

`lhh-retention.timer` fires **daily at 09:47 UTC** (`OnCalendar=*-*-*
09:47:00 UTC`), with up to 10 minutes of randomised delay
(`RandomizedDelaySec=600`) so a fleet of nodes does not all sweep at the same
instant. If the node was down at the scheduled time, `Persistent=true` fires
the missed pass once when it comes back. The service is a `Type=oneshot`
`root` job (`lhh-retention.service`) that runs with `Nice=10` and
`IOSchedulingClass=idle` so the sweep never competes with real work.

Its journal is the audit trail for what was actually deleted:

```bash
journalctl -u lhh-retention.service
# one page per day, newest last:
journalctl -u lhh-retention.service --since today
```

The timer and units are installed and enabled on every node by the deploy
pipeline (`.github/workflows/deploy-ct110.yml`, steps
`Install node retention timer (easybutt0n-304)` and
`Read back retention timer state and dry-run total (easybutt0n-304)`).

## Dry-running by hand

The script is dry-run by default — no `--apply`, nothing is deleted. Run it
on the node as root (the same command the unit runs, minus `--apply`):

```bash
/usr/bin/python3 /opt/lhh-retention/lhh_retention.py
```

It prints one `WOULD-DELETE` line per candidate, `FLAG-KEEP` lines for
protected-but-dirty git copies, and the `TOTAL would delete: ...` summary line
(the deploy's read-back step prints exactly this line to the job log). To
point it somewhere else first (for example a copy of a work tree), set
`LHH_WORK_ROOT`:

```bash
LHH_WORK_ROOT=/tmp/work-copy /usr/bin/python3 /opt/lhh-retention/lhh_retention.py
```

Deleting happens only with the flag the timer's unit passes:

```bash
/usr/bin/python3 /opt/lhh-retention/lhh_retention.py --apply
```

To run the in-repo copy instead of the installed one, substitute
`scripts/deploy/retention/lhh_retention.py` for the `/opt` path (keep
`LHH_WORK_ROOT` pointed at the tree you mean, or it will sweep the real one).
