#!/usr/bin/env python3
"""Cross-boundary validator for the C:/tmp/queue fleet queue.

Checks the things a generic file-graph validator structurally CANNOT: references that
cross from a queue entry or task file out into Python source, the live gateway, or the
queue's own numbering. Every rule here exists because it caught a real defect.

Exit 0 = clean, 1 = findings. Read-only; never writes.
"""
from __future__ import annotations
import json, re, sys, glob, os, collections

QUEUE = os.environ.get("QUEUE_DIR", "C:/tmp/queue")
HARNESS_SRC = os.environ.get("HARNESS_SRC", "C:/Users/PaxtonTait/source/LongHorizon-Harness/src")
findings: list[tuple[str, str, str]] = []          # (severity, rule, message)
def add(sev, rule, msg): findings.append((sev, rule, msg))

def entries():
    """Live queue entries only: active band + blocked/. done/ is an upper bound, not state."""
    for f in glob.glob(f"{QUEUE}/*.json") + glob.glob(f"{QUEUE}/blocked/*.json"):
        if ".bak" in f or ".superseded" in f or ".requeued" in f or ".released" in f:
            continue
        try:
            yield f, json.load(open(f, encoding="utf-8"))
        except Exception as e:
            add("ERROR", "unparseable-entry", f"{os.path.basename(f)}: {e}")

TASKNUM = re.compile(r"^[0-9a-z]+-(\d+)([a-z]?)-")

# ---- rule 1: task-number uniqueness (caught: two entries both numbered 208) ----
def rule_unique_numbers(ents):
    seen = collections.defaultdict(list)
    for f, d in ents:
        m = TASKNUM.match(os.path.basename(f))
        if m: seen[m.group(1) + m.group(2)].append(os.path.basename(f))
    for num, files in sorted(seen.items()):
        if len(files) > 1:
            add("ERROR", "duplicate-task-number", f"task {num} used by {len(files)} live entries: {', '.join(files)}")
    return {n for n in seen}

# ---- rule 2: task_file must exist (caught: task_file missing after a hand-edit) ----
def rule_task_file_exists(ents):
    for f, d in ents:
        tf = d.get("task_file")
        if d.get("task_source") == "task_file" or tf:
            if not tf: add("ERROR", "missing-task-file-field", os.path.basename(f))
            elif not os.path.exists(tf): add("ERROR", "task-file-not-found", f"{os.path.basename(f)} -> {tf}")

# ---- rule 3: blocker references must resolve to a task that exists SOMEWHERE ----
# A launched task lives in done/, so done/ counts as "exists". Two distinct signals:
#   dangling  = number exists nowhere -> typo or mislabel (ERROR)
#   satisfied = number exists only in done/ -> blocker may be cleared (WARN, verify)
def rule_blocker_refs(ents, live_nums):
    done_nums = set()
    for f in glob.glob(QUEUE + "/done/*.json"):
        if ".bak" in f:
            continue
        m = TASKNUM.match(os.path.basename(f))
        if m:
            done_nums.add(m.group(1) + m.group(2))
    pat = re.compile(r"BLOCKED ON(?:[^.\n]{0,40}?)TASK\s+(\d+)", re.I)
    for f, d in ents:
        for num in set(pat.findall(d.get("note", ""))):
            if num in live_nums:
                continue
            base = os.path.basename(f)
            if num in done_nums:
                add("WARN", "blocker-may-be-satisfied",
                    base + " says BLOCKED ON TASK " + num + "; that task has already been launched "
                    "(it is in done/). Verify whether the blocker is cleared and release the entry if so")
            else:
                add("ERROR", "blocker-target-does-not-exist",
                    base + " says BLOCKED ON TASK " + num + ", but no queue entry with that number "
                    "exists anywhere - typo, renumbering, or a mislabelled blocker")

# ---- rule 4: env-var names in task files must exist in the harness source ----
# (caught: a runbook said LH_HARNESS_DATABASE_URL; the code reads LH_HARNESS_DB_PASSWORD)
def rule_env_names(ents):
    src = ""
    for p in glob.glob(f"{HARNESS_SRC}/**/*.py", recursive=True):
        try: src += open(p, encoding="utf-8", errors="replace").read()
        except Exception: pass
    if not src:
        add("WARN", "env-check-skipped", f"no harness source at {HARNESS_SRC}; env-name check did not run")
        return
    known = set(re.findall(r"[\"']([A-Z][A-Z0-9_]{4,})[\"']", src))
    seen = set()
    for f, d in ents:
        tf = d.get("task_file")
        if not tf or not os.path.exists(tf): continue
        try: text = open(tf, encoding="utf-8", errors="replace").read()
        except Exception: continue
        for name in set(re.findall(r"\bLH_HARNESS_[A-Z0-9_]+\b", text)):
            if name not in known and name not in seen:
                seen.add(name)
                add("WARN", "env-name-not-in-source",
                    f"{os.path.basename(tf)} references {name}, which appears in no harness .py source file")

# ---- rule 5: fields the launcher actually reads ----
def rule_schema(ents):
    required = ("name", "workspace", "max_rounds")
    for f, d in ents:
        for k in required:
            if k not in d: add("ERROR", "missing-required-field", f"{os.path.basename(f)} has no '{k}'")
        if d.get("trio") not in (None, "kimi", "qwen", "degraded"):
            add("ERROR", "unknown-trio", f"{os.path.basename(f)} trio={d.get('trio')!r} (launcher knows kimi|qwen|degraded)")
        if "name" in d and os.path.basename(f) != d["name"] + ".json":
            add("WARN", "name-filename-mismatch", f"{os.path.basename(f)} declares name={d['name']!r}")
        if d.get("continue_branch") and not d.get("branch"):
            add("ERROR", "continuation-without-branch", f"{os.path.basename(f)} sets continue_branch but no branch")

def main() -> int:
    ents = list(entries())
    known = rule_unique_numbers(ents)
    rule_task_file_exists(ents)
    rule_blocker_refs(ents, known)
    rule_env_names(ents)
    rule_schema(ents)
    print(f"validate_queue: {len(ents)} live entries checked ({QUEUE})")
    if not findings:
        print("  clean - no findings"); return 0
    for sev in ("ERROR", "WARN"):
        rows = [x for x in findings if x[0] == sev]
        if rows:
            print(f"\n  {sev} ({len(rows)}):")
            for _, rule, msg in rows: print(f"    [{rule}] {msg}")
    return 1 if any(s == "ERROR" for s, _, _ in findings) else 0

if __name__ == "__main__":
    sys.exit(main())
