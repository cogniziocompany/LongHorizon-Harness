#!/usr/bin/env python3
"""Cross-boundary validator for the fleet queue.

Checks the things a generic file-graph validator structurally CANNOT: references that
cross from a queue entry or task file out into Python source, the live gateway, or the
queue's own numbering. Every rule here exists because it caught a real defect.

COVERAGE DISCLAIMER: this validator proves that identifiers, task numbers, files and
env-var NAMES it can see actually exist. It does NOT validate prose semantics. Rule
``blocker-*`` can prove that a number referenced in a note exists or not; it cannot
know that a note saying "BLOCKED ON TASK 179" actually meant task 186. Likewise it
cannot judge whether a blocker is genuinely satisfied, only whether the target has
been launched. Nobody should assume more coverage than that.

Exit 0 = clean or WARN-only, non-zero only when an ERROR was found, so it can gate a
scheduled tick without blocking on advisories. Read-only; never writes to the queue
or to any queue entry, including seen-findings state (the caller diffs findings between
ticks if it wants "new only" reporting).

Console entry points::

    python -m lh_harness.validate_queue
    lh-validate-queue          (installed via [project.scripts])

``QUEUE_DIR`` and ``HARNESS_SRC`` override the paths below; the defaults point at the
in-repo layout (``.lh-harness/runs/queue`` beside this checkout) so a bare invocation
from the repository root does the right thing.

Scope-D additions (rules 6 and 7 below), with their severity rationale next to the
closest existing rules as the anchor:

- ``gateway-alias-exists``: an MCP alias named in a task file (or in an entry's
  explicit ``mcp_alias`` / ``gateway_alias`` / ``mcp_gateway_alias`` field) that is
  absent from the live gateway registry ``GET {gateway root}/v1/mcp/server`` is a
  cross-boundary reference to something that does not exist - the same shape as
  ``task-file-not-found`` and ``blocker-target-does-not-exist``, both ERROR already -
  and it needs a disposition (task 177 sat blocked for days on an alias that no
  longer existed), so it is **ERROR**. A registry that cannot be fetched is transient
  infrastructure noise, the same shape as ``env-check-skipped``, so
  ``gateway-registry-unavailable`` is **WARN** and never gates a tick. The gateway
  root derives from the in-repo harness source (see rule 6); the gateway-key env var
  is named but its VALUE is only ever a Bearer header - never read into findings,
  never printed. With no key configured the rule SKIPS ENTIRELY: no task-file scan,
  no HTTP request, no finding, no exit-code effect. The fetch itself is read-only
  (HTTP GET, no other verb, ever) and lives in one small swappable function so tests
  can substitute a mock registry; no test may contact the live gateway.
- ``open-asks-row-integrity``: every row of the queue's ``OPEN-ASKS.md`` table must
  carry BOTH a recommendation and a default-if-silent. LOOP-PROMPT's exact row schema
  is not present in this repository, so this rule implements the request's wording
  directly and says so (it is NOT a re-derivation of an unavailable file): when a
  header maps onto recommendation/default columns, an empty or dash-placeholder cell
  fails the row; when no such header can be mapped, the row must itself carry both
  the words the request names ("recommend" and "default-if-silent"). A row without
  either is a decision that cannot be resolved without operator input - it needs a
  disposition, like ``missing-required-field`` - so it is **ERROR**.

The coverage disclaimer above applies unchanged to both scope-D rules:
``gateway-alias-exists`` can prove an alias is absent from the registry; it cannot
tell whether the task file MEANT that alias. ``open-asks-row-integrity`` proves a
recommendation/default-if-silent CELL IS PRESENT and non-empty; it does not judge the
recommendation's soundness - presence, not quality.

The second output line is a stable, machine-readable summary::

    validate_queue findings: E errors, W warns

so the scheduled tick takes its finding COUNT from ``E + W`` in one clause (WARN is
advisory, ERROR needs a disposition).
"""
from __future__ import annotations
import json, re, sys, glob, os, collections
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

_QUEUE_SUBDIR = "queue"
_REPO_DEFAULT_QUEUE = Path(__file__).resolve().parents[2] / ".lh-harness" / "runs" / _QUEUE_SUBDIR
_REPO_DEFAULT_HARNESS_SRC = Path(__file__).resolve().parents[1]

QUEUE = os.environ.get("QUEUE_DIR", str(_REPO_DEFAULT_QUEUE))
HARNESS_SRC = os.environ.get("HARNESS_SRC", str(_REPO_DEFAULT_HARNESS_SRC))
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
# Note: this proves only that the number exists; it cannot tell whether the prose
# intent of the note matches that number (see module docstring).
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
# Only ever compares NAMES; neither the source nor any task-file text is printed, so no
# secret VALUE can leak into findings - names only.
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

# ---- rule 6: MCP aliases named in task files must exist in the live gateway registry ----
# (commissioned for: task 177 sat blocked for days on an alias that no longer existed)
# Same cross-boundary shape as rules 2 and 3, but the boundary is the live gateway: a
# task file (or an entry's explicit alias field) that names an MCP alias must resolve
# against the registry at GET {gateway root}/v1/mcp/server. The gateway ROOT derives
# from the in-repo harness source - lh_harness/mcp_profiles.py owns the gateway base
# URL (MCP_PROD_GATEWAY_URL with its "/mcp/" path) and the two env-var NAMES
# (LH_HARNESS_MCP_GATEWAY_URL override, LH_HARNESS_MCP_GATEWAY_KEY for the Bearer key)
# - and the registry path itself was commissioned in the task statement (in-workspace
# corroboration: cognizioware-mcp-tools/.claude/skills/mcp-gateway-ops/SKILL.md:83).
# The KEY VALUE is only ever a Bearer header: never read into findings, never printed.
# No key -> the rule skips entirely (no high-cost task-file scan, no HTTP, no finding,
# no exit effect). Read-only GET is the only verb this module ever uses.
GATEWAY_REGISTRY_PATH = "v1/mcp/server"
GATEWAY_KEY_ENV = "LH_HARNESS_MCP_GATEWAY_KEY"    # NAME only: mirrors lh_harness.mcp_profiles
GATEWAY_URL_ENV = "LH_HARNESS_MCP_GATEWAY_URL"    # NAME only: mirrors lh_harness.mcp_profiles

def _gateway_registry_root() -> str | None:
    """Gateway proxy ROOT (scheme://netloc) derived from the in-repo gateway base URL.

    mcp_profiles' base ends in "/mcp/" (the Streamable-HTTP endpoint); the registry
    lives at the proxy root. Returns None when no usable root can be derived.
    """
    from . import mcp_profiles
    base = mcp_profiles._normalise_gateway_url(os.environ.get(GATEWAY_URL_ENV))
    try:
        parts = urlsplit(base)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

def _registry_aliases(payload) -> set[str]:
    """Alias names out of a registry payload (str|dict|list). Tolerant by design -
    the live registry shape is NOT asserted here; common field spellings
    (alias/aliases/server_name/name), a top-level ``data`` wrapper, and bare-string
    lists are all accepted. Names compare case-insensitively."""
    servers = payload.get("data", payload) if isinstance(payload, dict) else payload
    aliases = set()
    if isinstance(servers, list):
        for item in servers:
            if isinstance(item, str):
                if item.strip():
                    aliases.add(item.strip().lower())
            elif isinstance(item, dict):
                for field in ("alias", "aliases", "server_name", "name"):
                    value = item.get(field)
                    if isinstance(value, str) and value.strip():
                        aliases.add(value.strip().lower())
    return aliases

def _fetch_registry_aliases(root: str, key: str) -> set[str]:
    """Read-only GET of the MCP alias registry. SWAPPABLE: tests monkeypatch this
    function with a mock registry and no test may contact the live gateway. ``key``
    is only ever a Bearer header value here and is never printed."""
    import urllib.request
    req = urllib.request.Request(
        urljoin(root + "/", GATEWAY_REGISTRY_PATH),
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return _registry_aliases(json.load(resp))

_ALIAS_STOPWORDS = frozenset({
    # English function words likely to sit next to the word "alias" in prose, so an
    # ordinary sentence about aliases is not mistaken for NAMING one.
    "a", "an", "the", "and", "or", "on", "in", "of", "at", "to", "by", "as", "if",
    "is", "are", "was", "were", "be", "it", "all", "any",
    "from", "with", "into", "your", "its", "his", "her", "their",
    "for", "via", "per", "same", "such", "own", "new", "old",
    "like", "that", "this", "these", "those",
    "exists", "exist", "existing", "nonexistent", "was",
    "maps", "named", "name", "resolve", "resolves", "matches", "point", "points",
    "used", "uses", "must", "should", "needs",
})
ALIAS_AFTER_CUE = re.compile(r"(?:\b(?:mcp|gateway)\W{1,4})*alias\W{0,4}([a-z][a-z0-9_-]{2,})", re.I)
ALIAS_BEFORE_CUE = re.compile(r"\b([a-z][a-z0-9_-]{2,})\W{0,4}(?:\b(?:mcp|gateway)\W{1,4})+alias\b", re.I)

def _aliases_named(text: str) -> set[str]:
    """Alias tokens a task file names on the ``alias`` cue (both word orders, e.g.
    "gateway alias `lhharness`" and "the `lhharness` gateway alias")."""
    named = set()
    for pat in (ALIAS_AFTER_CUE, ALIAS_BEFORE_CUE):
        for m in pat.finditer(text):
            word = m.group(1).strip().strip("`'\"")
            if word and word.lower() not in _ALIAS_STOPWORDS and word.lower() not in ("alias", "aliases"):
                named.add(word)
    return named

def rule_gateway_aliases(ents):
    # SKIPS ENTIRELY without the gateway key - see module docstring. The key presence
    # check comes first so no task file is even scanned when the rule cannot run.
    from . import mcp_profiles
    key = mcp_profiles._gateway_key()
    if key is None:
        return
    root = _gateway_registry_root()
    if root is None:
        return
    named: dict[str, set[str]] = collections.defaultdict(set)
    for f, d in ents:
        base = os.path.basename(f)
        for field in ("mcp_alias", "gateway_alias", "mcp_gateway_alias"):
            value = d.get(field)
            if isinstance(value, str) and value.strip():
                named[value.strip()].add(base)
        tf = d.get("task_file")
        if not tf or not os.path.exists(tf): continue
        try: text = open(tf, encoding="utf-8", errors="replace").read()
        except Exception: continue
        for alias in _aliases_named(text):
            named[alias].add(base)
    if not named:
        return                                   # nothing to check -> still no HTTP
    try:
        registry = _fetch_registry_aliases(root, key)
    except Exception as e:
        # Transient infrastructure noise, same shape as env-check-skipped -> WARN,
        # so an unreachable gateway never gates a tick. Type name only: the
        # exception text may carry request detail we do not want in output.
        add("WARN", "gateway-registry-unavailable",
            f"cannot read MCP registry at {urljoin(root + '/', GATEWAY_REGISTRY_PATH)} "
            f"({type(e).__name__}); gateway-alias-exists check did not run")
        return
    for alias in sorted(named, key=str.lower):
        if alias.lower() not in registry:
            froms = ", ".join(sorted(named[alias], key=str.lower))
            add("ERROR", "gateway-alias-exists",
                f"MCP alias `{alias}` named by {froms} is not in the gateway "
                f"{urljoin(root + '/', GATEWAY_REGISTRY_PATH)} registry")

# ---- rule 7: every OPEN-ASKS row must carry a recommendation and a default-if-silent ----
# (commissioned with the wording of the task statement; the LOOP-PROMPT row schema is
# NOT present in this repository, so the definition below is the request's wording
# implemented directly - see module docstring. A row missing either half cannot be
# resolved without operator input, hence ERROR like missing-required-field.)
OPEN_ASKS_FILENAME = "OPEN-ASKS.md"
_OPEN_ASKS_RE = re.compile(r"\brecommend", re.I)
_DEFAULT_IF_SILENT_RE = re.compile(r"default[-\s]*if[-\s]*silent", re.I)
_SEPARATOR_CELL = re.compile(r":?-+:?")
_PLACEHOLDER_CELLS = frozenset({"-", "--", "---", "—", "?"})

def _table_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]

def _cell(cells: list[str], i: int | None) -> str:
    if i is None or i >= len(cells):
        return ""
    value = cells[i].strip()
    return "" if value in _PLACEHOLDER_CELLS else value

def rule_open_asks_integrity():
    path = os.path.join(QUEUE, OPEN_ASKS_FILENAME)
    if not os.path.exists(path):
        return                                   # no OPEN-ASKS file -> nothing to check
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except Exception as e:
        add("WARN", "open-asks-unreadable",
            f"{OPEN_ASKS_FILENAME} could not be read ({type(e).__name__}); row integrity check did not run")
        return
    table_lines = [ln for ln in lines if ln.lstrip().startswith("|")]
    if not table_lines:
        return                                   # prose-only file: no table rows to check
    header = _table_cells(table_lines[0])
    rec_col = next((i for i, c in enumerate(header) if _OPEN_ASKS_RE.search(c)), None)
    def_col = next((i for i, c in enumerate(header) if _DEFAULT_IF_SILENT_RE.search(c)), None)
    if rec_col is None and def_col is None:
        # Definition fallback (request's wording, verbatim): without a mappable
        # header there is no header row at all - every table line is a row and the
        # only observable of "carries a recommendation and a default-if-silent" is
        # the row carrying BOTH named markers in its own text.
        for ln in table_lines:
            cells = _table_cells(ln)
            if all(_SEPARATOR_CELL.fullmatch(c) or not c for c in cells):
                continue
            if len(cells) < 2:
                continue                           # a single-cell divider line is not a row
            if not (_OPEN_ASKS_RE.search(ln) and _DEFAULT_IF_SILENT_RE.search(ln)):
                add("ERROR", "open-asks-row-missing-recommendation-or-default",
                    f"{OPEN_ASKS_FILENAME}: row `{ln.strip()[:60]}` cannot be mapped to "
                    "recommendation/default-if-silent columns and does not carry both itself")
        return
    if rec_col is None or def_col is None:
        missing = "recommendation" if rec_col is None else "default-if-silent"
        add("ERROR", "open-asks-missing-column",
            f"{OPEN_ASKS_FILENAME}: table header has no {missing} column")
        return
    for ln in table_lines[1:]:
        cells = _table_cells(ln)
        if all(_SEPARATOR_CELL.fullmatch(c) or not c for c in cells):
            continue                               # markdown header separator row
        preview = ln.strip()[:60]
        if not _cell(cells, rec_col):
            add("ERROR", "open-asks-missing-recommendation",
                f"{OPEN_ASKS_FILENAME}: row `{preview}` has no recommendation")
        if not _cell(cells, def_col):
            add("ERROR", "open-asks-missing-default-if-silent",
                f"{OPEN_ASKS_FILENAME}: row `{preview}` has no default-if-silent")

def main() -> int:
    ents = list(entries())
    known = rule_unique_numbers(ents)
    rule_task_file_exists(ents)
    rule_blocker_refs(ents, known)
    rule_env_names(ents)
    rule_schema(ents)
    rule_gateway_aliases(ents)
    rule_open_asks_integrity()
    n_err = sum(1 for s, _, _ in findings if s == "ERROR")
    n_warn = sum(1 for s, _, _ in findings if s == "WARN")
    print(f"validate_queue: {len(ents)} live entries checked ({QUEUE})")
    print(f"validate_queue findings: {n_err} errors, {n_warn} warns")
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
