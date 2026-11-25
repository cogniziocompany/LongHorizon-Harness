#!/usr/bin/env python3
"""LongHorizon-Harness node work-tree retention (easybutt0n-304).

Runs INSIDE CT110 as root (pushed by scripts/corsairai300-pool-trim.sh through `pct exec 110`).
Default is a DRY RUN that only lists; `--apply` deletes.

Rule:
  1. Run records  /home/harness/work/.lh-harness/runs/<run id>: keep 30 days, then delete.
     Never delete a run that a queue entry still shows pending, launched or waiting.
  2. Workspace copies (top-level dirs in /home/harness/work): keep 14 days since last change, then delete.
     Never delete: a workspace a queue entry from the last 30 days names, a base workspace (BASE below),
     or a git copy with uncommitted changes or unpushed commits (those are FLAGGED, not deleted).
  3. Scratch, parked and loose files (_*, .scratch*, top-level files): keep 14 days, then delete.
Every pass prints one line per removed item (name, size, reason) and a total.
"""
import json, os, shutil, subprocess, sys, time

W = os.environ.get("LHH_WORK_ROOT", "/home/harness/work")
RUNS = os.path.join(W, ".lh-harness", "runs")
QUEUE = os.path.join(RUNS, "queue")
RUN_DAYS, WS_DAYS, QUEUE_LOOKBACK_DAYS = 30, 14, 30
BASE = {"cognizioware-mcp-tools", "LongHorizon-Harness", "claude-code-teams", "cognizioware-qa",
        "cognizioware", "powerplatform", "cognizioware-omnigent", "workspace"}
NEVER = {".lh-harness", ".git", ".cache", ".config", ".ssh", ".local", ".npm"}
NOW = time.time()
APPLY = "--apply" in sys.argv


def age_days(t):
    return (NOW - t) / 86400.0


def size_of(p):
    try:
        out = subprocess.run(["du", "-sb", p], capture_output=True, text=True, timeout=300).stdout
        return int(out.split()[0])
    except Exception:
        return 0


def human(n):
    for u in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.1f}T"


def queue_state():
    live_runs, named_ws = set(), set()
    if not os.path.isdir(QUEUE):
        return live_runs, named_ws
    for f in os.listdir(QUEUE):
        if not f.endswith(".json"):
            continue
        p = os.path.join(QUEUE, f)
        try:
            d = json.load(open(p))
        except Exception:
            continue
        st = str(d.get("status", "")).lower()
        rid = d.get("run_id") or d.get("runId") or ""
        if st in ("pending", "launched", "running", "waiting_approval", "starting") and rid:
            live_runs.add(str(rid))
        if age_days(os.path.getmtime(p)) <= QUEUE_LOOKBACK_DAYS or st in ("pending", "launched"):
            ws = d.get("workspace") or d.get("workspace_dir") or ""
            if ws:
                named_ws.add(os.path.basename(str(ws).rstrip("/")))
    return live_runs, named_ws


def last_change(p):
    """Newest of: the dir's own mtime, git HEAD/index mtime, newest file mtime (stops early)."""
    t = os.path.getmtime(p)
    for g in (".git/HEAD", ".git/index", ".git/logs/HEAD"):
        q = os.path.join(p, g)
        if os.path.exists(q):
            t = max(t, os.path.getmtime(q))
    if os.path.isfile(os.path.join(p, ".git")):  # worktree pointer
        t = max(t, os.path.getmtime(os.path.join(p, ".git")))
    cutoff = NOW - WS_DAYS * 86400
    if t < cutoff:
        r = subprocess.run(["find", p, "-xdev", "-newermt", f"@{int(cutoff)}", "-print", "-quit"],
                           capture_output=True, text=True, timeout=120)
        if r.stdout.strip():
            t = NOW
    return t


def git_unsafe(p):
    """Return a reason string if a git copy holds work that is not on its remote."""
    if not (os.path.isdir(os.path.join(p, ".git")) or os.path.isfile(os.path.join(p, ".git"))):
        return ""
    def g(*a):
        return subprocess.run(["git", "-C", p, *a], capture_output=True, text=True, timeout=60)
    s = g("status", "--porcelain", "--untracked-files=no")
    if s.returncode != 0:
        return "git-unreadable"
    if s.stdout.strip():
        return "uncommitted-changes"
    u = g("log", "--oneline", "--branches", "--not", "--remotes", "-n", "1")
    if u.stdout.strip():
        return "unpushed-commits"
    return ""


def main():
    live_runs, named_ws = queue_state()
    removed, flagged, total = [], [], 0
    # 1. run records
    if os.path.isdir(RUNS):
        for n in sorted(os.listdir(RUNS)):
            p = os.path.join(RUNS, n)
            if n == "queue" or not os.path.isdir(p) or not n[:2] == "20":
                continue
            if any(n.endswith(r) or r.endswith(n) or r == n for r in live_runs):
                continue
            if age_days(os.path.getmtime(p)) > RUN_DAYS:
                removed.append((p, "run-record>30d"))
    # 2 + 3. top level of the work tree
    for n in sorted(os.listdir(W)):
        p = os.path.join(W, n)
        if n in NEVER:
            continue
        if os.path.isfile(p) or os.path.islink(p):
            if age_days(os.lstat(p).st_mtime) > WS_DAYS:
                removed.append((p, "loose-file>14d"))
            continue
        if n in BASE or n in named_ws:
            continue
        if age_days(last_change(p)) <= WS_DAYS:
            continue
        why = git_unsafe(p)
        if why:
            flagged.append((p, why))
            continue
        kind = "scratch>14d" if n.startswith(("_", ".scratch")) else "workspace>14d"
        removed.append((p, kind))
    for p, why in removed:
        sz = size_of(p)
        total += sz
        print(f"{'DELETE' if APPLY else 'WOULD-DELETE'} {human(sz):>6} {why:<16} {p}")
        if APPLY:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
    for p, why in flagged:
        print(f"FLAG-KEEP {why:<20} {p}")
    print(f"TOTAL {'deleted' if APPLY else 'would delete'}: {len(removed)} items, {human(total)}; "
          f"flagged {len(flagged)}; protected live runs {len(live_runs)}, queue-named workspaces {len(named_ws)}")


if __name__ == "__main__":
    main()
