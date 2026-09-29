#!/usr/bin/env python3
"""sdlc_export.py — record-store exporter and redactor (build sequence task 2).

Implements plan row 2 of
``docs/handoffs/PLAN-delivery-record-store-2026-09-29.md`` ("Exporter and
redactor: classify by the audited naming patterns, redact, hash, write a bundle
with a JSONL manifest. Tests use fixtures containing fake secrets.") against the
bundle format proposed in ``docs/design/sdlc-record-store.md`` §5.

Input is a COPY of the legacy overseer apparatus (the old queue folder tree
from the dev PC, per the plan's Context section) given as a command-line
argument.  No path is hard-coded anywhere in this file; the script performs
no network, database or service access.

Pipeline, per source file:

 1. classify by the audited naming patterns (see ``CLASSIFIERS`` below);
 2. files that ARE secrets (``.redis_pw``, ``.ops_ingest_key``, ``.tok*``,
    ``*_tnas_keys.txt`` and similar) are recorded by name, size and sha256 only
    — their body is never read into the bundle (plan Redaction rule 2);
 3. every other body is redacted (plan rule 1: matched secret values become
    ``[REDACTED:<kind>]``, the rest of the file is kept), sha256'd, and stored
    once in ``blobs/<sha256[:2]>/<sha256>`` — identical redacted bodies
    (the 508 ledger copies) collapse to one blob;
 4. the bundle is written as ``manifest.jsonl`` + ``redaction-report.jsonl`` +
    ``secrets-by-name.jsonl`` + ``blobs/`` (design §5), and then re-checked by a
    SECOND, independent scan that re-reads the finished bundle from disk.  Any
    hit fails the export with a non-zero exit (plan rule 4).

``--dry-run`` is the DEFAULT: files are classified, redaction is evaluated and
reported, but nothing is written except the report (stdout, or ``--report``
file).  A real bundle write requires the explicit ``--write`` flag.

The naming patterns encode the audit findings in the plan's Context section.
Where neither the plan nor the design spells out an exact filename grammar
(tick rows, run dumps, scratch scripts), the patterns below are derived from
the examples the audit names (``*-task.txt`` specs, ``LEDGER.md`` + copies,
``OPEN-ASKS.md``, ``HANDOFF-*.md``, the retired ``launch_queue.py.*`` /
``quota_resume*.py`` / watchdog scratch scripts) and are explicit, ordered and
easy to tune — they are rules, not hidden assumptions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Classes (plan Context / build sequence row 2, plus the design's plan/runbook
# artifact kinds, which the audit counts as core records of their own).
# ---------------------------------------------------------------------------

CLASS_SPEC = "spec"
CLASS_QUEUE_LIVE = "queue_entry_live"
CLASS_QUEUE_BLOCKED = "queue_entry_blocked"
CLASS_QUEUE_DONE = "queue_entry_done"
CLASS_LEDGER = "ledger"
CLASS_LEDGER_COPY = "ledger_copy"
CLASS_TICK_ROW = "tick_row"
CLASS_OPEN_ASK = "open_ask"
CLASS_HANDOFF = "handoff"
CLASS_PLAN = "plan"
CLASS_RUNBOOK = "runbook"
CLASS_RUN_DUMP = "run_dump"
CLASS_SCRATCH = "scratch"
CLASS_SECRET_FILE = "secret_file"
CLASS_OTHER = "other"

# Exporter class -> sdlc.artifact_kind enum value (design §3.2:
# spec, queue_entry, handoff, plan, runbook, scratch, patch, evidence,
# run_dump, log, script).  The enum has no ledger/open-ask kind: the ledger,
# its copies, exported tick rows, the open-asks document and unrecognised
# files are record-keeping narrative, so they map to 'log'.  The exporter
# `class` field stays authoritative for the loader (task 3); kind is
# best-effort only.
ARTIFACT_KIND = {
    CLASS_SPEC: "spec",
    CLASS_QUEUE_LIVE: "queue_entry",
    CLASS_QUEUE_BLOCKED: "queue_entry",
    CLASS_QUEUE_DONE: "queue_entry",
    CLASS_LEDGER: "log",
    CLASS_LEDGER_COPY: "log",
    CLASS_TICK_ROW: "log",
    CLASS_OPEN_ASK: "log",
    CLASS_HANDOFF: "handoff",
    CLASS_PLAN: "plan",
    CLASS_RUNBOOK: "runbook",
    CLASS_RUN_DUMP: "run_dump",
    CLASS_SCRATCH: "script",
    CLASS_OTHER: "log",
    CLASS_SECRET_FILE: None,  # never stored as a blob
}

# ---------------------------------------------------------------------------
# Secret-by-name files (plan Redaction rule 2): recorded by name, size and
# sha256 only; the body never enters the bundle.  The explicit list from the
# plan plus the obvious "and similar" credential-file shapes.
# ---------------------------------------------------------------------------

SECRET_FILE_EXACT = {
    ".redis_pw",
    ".ops_ingest_key",
    ".netrc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
SECRET_FILE_PREFIXES = (".tok",)          # plan: ".tok*"
SECRET_FILE_SUFFIXES = (
    ".tok",
    "_tnas_keys.txt",                      # plan
    "_keys.txt",
    ".pem",
    ".p12",
    ".pfx",
)


def is_secret_file(name: str) -> bool:
    low = name.lower()
    if low in SECRET_FILE_EXACT:
        return True
    if any(low.startswith(p) for p in SECRET_FILE_PREFIXES):
        return True
    return any(low.endswith(s) for s in SECRET_FILE_SUFFIXES)


# ---------------------------------------------------------------------------
# Classification rules.  First match wins.  Patterns operate on the path
# RELATIVE to the source dir, as posix text, and on the lowercase basename.
# ---------------------------------------------------------------------------

_LEDGER_BASE = re.compile(r"^ledger\.md$")
# Windows-copy and backup shapes observed for the ledger family:
#   LEDGER - Copy.md, LEDGER (2).md, LEDGER.md.bak, LEDGER.md.20260923,
#   ledger_old.md, ledger-backup.md …
_LEDGER_COPY = re.compile(r"^ledger([ ._(\-].*)?\.md(\..+)?$")
_TICK_ROW = re.compile(r"(?:^|[-_. ])tick[-_. ]?\d+", re.IGNORECASE)
_RUN_DUMP = re.compile(r"(?:^|[-_. ])run[-_. ]?dump", re.IGNORECASE)
_SCRATCH_SCRIPT = re.compile(
    r"^(launch_queue|quota_resume|spin_watchdog|watchdog)[-_.0-9a-z]*\.py(\..*)?$",
    re.IGNORECASE,
)


def classify(rel_path: str) -> str:
    parts = [p for p in rel_path.replace(os.sep, "/").split("/") if p]
    name = parts[-1]
    low = name.lower()
    low_parts = [p.lower() for p in parts[:-1]]

    if is_secret_file(name):
        return CLASS_SECRET_FILE
    if low.startswith("open-asks") or low.startswith("open_asks"):
        return CLASS_OPEN_ASK
    if _LEDGER_BASE.match(low):
        return CLASS_LEDGER
    if _LEDGER_COPY.match(low):
        return CLASS_LEDGER_COPY
    if _TICK_ROW.search(low):
        return CLASS_TICK_ROW
    if low.startswith("handoff-") or low.startswith("handoff_"):
        return CLASS_HANDOFF
    if low.startswith("plan-") or low.startswith("plan_"):
        return CLASS_PLAN
    if "runbook" in low:
        return CLASS_RUNBOOK
    if low.endswith("-task.txt") or low.endswith("-task.md"):
        return CLASS_SPEC
    # Queue entries: <queueid>-<tasknumber>-<slug>.json under a queue folder.
    # Live entries sit at the queue root; blocked/, done/ and imported/ carry
    # state variants (enqueue.py moves processed entries into queue/imported —
    # plan, "Removing ptait09 from the architecture").
    if low.endswith(".json") and "queue" in low_parts:
        q = low_parts.index("queue")
        tail = low_parts[q + 1 :]
        if "blocked" in tail:
            return CLASS_QUEUE_BLOCKED
        if "done" in tail or "imported" in tail:
            return CLASS_QUEUE_DONE
        return CLASS_QUEUE_LIVE
    if _RUN_DUMP.search(rel_path.replace(os.sep, "/")):
        return CLASS_RUN_DUMP
    if "scratch" in low_parts or _SCRATCH_SCRIPT.match(low):
        return CLASS_SCRATCH
    return CLASS_OTHER


_BASE_EXTS = (".md", ".txt", ".json", ".jsonl", ".py", ".ps1", ".sh", ".log", ".yaml", ".yml", ".html", ".csv")


def backup_suffix(name: str) -> str:
    """Design §3.4's backup_suffix: the BACKUP extension beyond the file's own
    (LEDGER.md.1 -> '.1'; notes.md.bak -> '.bak'; 'LEDGER (2).md' and plain
    'foo.json' -> '')."""
    low = name.lower()
    for ext in _BASE_EXTS:
        marker = ext + "."
        if marker in low:
            return low[low.index(marker) + len(ext) :]
        if low.endswith(ext):
            return ""
    m = re.search(r"\.(?:\d+|bak|backup|old|orig|save)$", low)
    return low[m.start() :] if m else ""


# ---------------------------------------------------------------------------
# Redaction rules (plan Redaction rule 1 + Verification item 2: harness token
# pattern, sk- literals, private-key headers).  `kind` is what lands in
# [REDACTED:<kind>]; VALUES never appear in any report, blob name or log.
# ---------------------------------------------------------------------------

# PEM-style private key block, replaced whole (header to footer) so no base64
# line inside it can shadow the other rules.
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----\s*",
    re.DOTALL,
)
# Header without a footer (truncated paste): still redact the header line.
_PRIVATE_KEY_HEADER = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[^\n]*")
# The CT110 harness API bearer credential, pinned by docs/SECRETS.md as a
# 48-char hex string.  48-hex has no benign lookalike (git SHAs are 40, sha256
# digests 64 — both documented non-secrets in SECRETS.md), so a bounded run of
# exactly 48 hex chars is the audit's "harness token pattern".
_HARNESS_TOKEN = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{48}(?![0-9a-fA-F])")
# sk- API key literals, >= 20 alnum chars so the documented benign 'sk-'
# lookalikes (branch labels like feat/task-sk-fleet-tools, the truncated
# 'sk-ant-api0...' mention in docs/SECRETS.md) do NOT match.
_SK_LITERAL = re.compile(r"sk-[A-Za-z0-9]{20,}")
# KEY/TOKEN/SECRET/PASSWORD-style assignment with a QUOTED literal value.
# Quoting is required so runtime wiring (os.environ[...], $VAR refs) is never
# mangled; placeholders are skipped in code below.
_CRED_NAME = r"[A-Za-z_][A-Za-z0-9_]*(?:_(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD)|API_KEY|AUTH_TOKEN)"
# Case-insensitive on the NAME: legacy scripts mix cases (`db_password:`).
_CRED_ASSIGN = re.compile(
    r"(?P<prefix>" + _CRED_NAME + r"\s*[=:]\s*)(?P<q>['\"])(?P<value>[^'\"\n]{6,})(?P=q)",
    re.IGNORECASE,
)

_PLACEHOLDER_VALUES = re.compile(
    r"(?i)^(?:x+|<.*>|\$\{?.*\}?|changeme|change[-_ ]?me|example(?:\.com)?|"
    r"your[-_ ].*|placeholder|redacted|\[redacted(?::[a-z_]+)?\]|todo|none|null)$"
)


def _redact_credential(match: re.Match, counts: dict) -> str:
    value = match.group("value")
    if _PLACEHOLDER_VALUES.match(value):
        return match.group(0)
    counts["credential"] = counts.get("credential", 0) + 1
    return match.group("prefix") + match.group("q") + "[REDACTED:credential]" + match.group("q")


def redact_body(text: str) -> tuple[str, dict]:
    """Return (redacted_text, per-kind counts).  Order matters: PEM blocks
    first so base64 lines cannot collide with the hex/token rules."""
    counts: dict = {}
    text, n = _PRIVATE_KEY_BLOCK.subn("[REDACTED:private_key]\n", text)
    if n:
        counts["private_key"] = n
    text, n = _PRIVATE_KEY_HEADER.subn("[REDACTED:private_key]", text)
    if n:
        counts["private_key"] = counts.get("private_key", 0) + n
    text, n = _HARNESS_TOKEN.subn("[REDACTED:harness_token]", text)
    if n:
        counts["harness_token"] = n
    text, n = _SK_LITERAL.subn("[REDACTED:sk_key]", text)
    if n:
        counts["sk_key"] = n
    text = _CRED_ASSIGN.sub(lambda m: _redact_credential(m, counts), text)
    return text, counts


# The second scan (plan rule 4) is INDEPENDENT of the redaction pass: it
# re-reads the finished bundle from disk with the same secret patterns and
# reports any hit as (relpath, kind, count) — never the value.  It is a
# pattern scan over everything written, not a check of the first pass's
# bookkeeping, so bookkeeping bugs cannot hide a leak from it.
def scan_for_secrets(data: bytes) -> list[tuple[str, int]]:
    text = data.decode("utf-8", errors="surrogateescape")
    findings: list[tuple[str, int]] = []
    for kind, pattern in (
        ("private_key", _PRIVATE_KEY_HEADER),
        ("harness_token", _HARNESS_TOKEN),
        ("sk_key", _SK_LITERAL),
    ):
        n = len(pattern.findall(text))
        if n:
            findings.append((kind, n))
    n_cred = 0
    for m in _CRED_ASSIGN.finditer(text):
        if not _PLACEHOLDER_VALUES.match(m.group("value")):
            n_cred += 1
    if n_cred:
        findings.append(("credential", n_cred))
    return findings


def second_scan(bundle_dir: Path) -> list[dict]:
    """Independent post-write scan over the finished bundle (plan rule 4)."""
    hits: list[dict] = []
    for root, _dirs, files in os.walk(bundle_dir):
        for fname in files:
            path = Path(root) / fname
            rel = path.relative_to(bundle_dir).as_posix()
            found = scan_for_secrets(path.read_bytes())
            for kind, count in found:
                hits.append({"path": rel, "kind": kind, "count": count})
    return hits


# ---------------------------------------------------------------------------
# Walk / export
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _task_ref_from_name(filename: str, cls: str) -> tuple[str, int | None, str]:
    """Best-effort (task_ref, task_number, slug) from the audited filename
    grammar.  Queue entries are <qid>-<number>-<slug>.json; specs are
    <ref>-<slug>-task.txt.  Pure-digit refs give a task_number; lettered early
    ids (05h3b, 125a, …) stay unmapped strings per design §3.1."""
    if cls.startswith("queue_entry"):
        stem = re.sub(r"\.json$", "", filename, flags=re.IGNORECASE)
        parts = stem.split("-", 2)
        if len(parts) == 3:
            _qid, num, slug = parts
            return num, int(num) if num.isdigit() else None, slug
        return "", None, stem
    if cls == CLASS_SPEC:
        core = re.sub(r"-task\.(txt|md)$", "", filename, flags=re.IGNORECASE)
        parts = core.split("-", 1)
        if len(parts) == 2:
            ref, slug = parts
            return ref, int(ref) if ref.isdigit() else None, slug
        return (core, int(core) if core.isdigit() else None, "")
    return "", None, ""


def walk_source(source: Path):
    for root, dirs, files in os.walk(source):
        dirs.sort()
        for fname in sorted(files):
            path = Path(root) / fname
            if path.is_symlink() or not path.is_file():
                continue
            yield path, path.relative_to(source).as_posix()


def build_records(source: Path):
    """Classify + redact every file under source.  Returns
    (manifest_lines, secret_records, report_lines); nothing is written."""
    manifest: list[dict] = []
    secrets: list[dict] = []
    report: list[dict] = []
    for path, rel in walk_source(source):
        cls = classify(rel)
        data = path.read_bytes()
        size = len(data)
        if cls == CLASS_SECRET_FILE:
            # Rule 2: name, size, hash only.  Body never enters the bundle.
            secrets.append(
                {
                    "path": rel,
                    "class": cls,
                    "size": size,
                    "sha256": _sha256(data),
                }
            )
            continue
        text = data.decode("utf-8", errors="surrogateescape")
        redacted, counts = redact_body(text)
        blob_bytes = redacted.encode("utf-8", errors="surrogateescape")
        digest = _sha256(blob_bytes)
        for kind, count in sorted(counts.items()):
            report.append({"file": rel, "kind": kind, "count": count})
        task_ref, task_number, slug = _task_ref_from_name(path.name, cls)
        manifest.append(
            {
                "artifact": {
                    "kind": ARTIFACT_KIND[cls],
                    "task_number": task_number,
                    "task_ref": task_ref,
                    "slug": slug,
                    "title": "",
                },
                "class": cls,
                "original_path": rel,
                "written_at": _iso_mtime(path),
                "backup_suffix": backup_suffix(path.name),
                "sha256": digest,
                "byte_size": size,
                "redactions": sum(counts.values()),
                "redaction_kinds": counts,
                "_blob_bytes": blob_bytes,  # internal; stripped before serializing
            }
        )
    return manifest, secrets, report


def write_bundle(bundle_dir: Path, manifest: list[dict], secrets: list[dict], report: list[dict]) -> tuple[int, int]:
    """Write blobs/ + manifest.jsonl + redaction-report.jsonl +
    secrets-by-name.jsonl (design §5).  Returns (files, distinct_blobs)."""
    blobs_dir = bundle_dir / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()
    for line in manifest:
        digest = line["sha256"]
        if digest in written:
            continue
        blob_path = blobs_dir / digest[:2] / digest
        if not blob_path.exists():  # dedup: identical bodies stored once
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            blob_path.write_bytes(line["_blob_bytes"])
        written.add(digest)

    def _dump(name: str, rows: list[dict], strip_internal: bool = False) -> None:
        with open(bundle_dir / name, "w", encoding="utf-8") as fh:
            for row in rows:
                row = {k: v for k, v in row.items() if not (strip_internal and k.startswith("_"))}
                fh.write(json.dumps(row, sort_keys=True) + "\n")

    _dump("manifest.jsonl", manifest, strip_internal=True)
    _dump("redaction-report.jsonl", report)
    _dump("secrets-by-name.jsonl", secrets)
    return len(manifest) + len(secrets), len(written)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# Distinct from argparse's usage-error exit (2) so a scan failure is
# unambiguous in automation.
EXIT_SCAN_FAILED = 3


def _print_report(report: list[dict], manifest: list[dict], secrets: list[dict]) -> None:
    class_counts: dict[str, int] = {}
    redaction_totals: dict[str, int] = {}
    for line in manifest:
        class_counts[line["class"]] = class_counts.get(line["class"], 0) + 1
    for row in report:
        redaction_totals[row["kind"]] = redaction_totals.get(row["kind"], 0) + row["count"]
    print("classification counts:")
    for cls in sorted(class_counts):
        print(f"  {cls}: {class_counts[cls]}")
    if secrets:
        print(f"  {CLASS_SECRET_FILE}: {len(secrets)} (name/size/sha256 only — no body)")
    print("redaction totals by kind:")
    if redaction_totals:
        for kind in sorted(redaction_totals):
            print(f"  {kind}: {redaction_totals[kind]}")
    else:
        print("  (none)")
    print("redaction report (file, kind, count — values are never shown):")
    for row in report:
        print(f"  {row['file']}\t{row['kind']}\t{row['count']}")
    if not report:
        print("  (no redactions)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export + redact a copy of the legacy overseer apparatus into a "
            "content-addressed bundle.  DRY-RUN IS THE DEFAULT: classification "
            "and redaction are reported, nothing is written but the report.  "
            "Pass --write for a real bundle write."
        )
    )
    parser.add_argument("source", nargs="?", help="source directory (a COPY of the legacy queue folder tree)")
    parser.add_argument("--out", help="bundle output directory (default: <source>.bundle next to the source)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=False, help="explicit dry run (this is already the default)")
    mode.add_argument("--write", action="store_true", help="actually write the bundle (an independent second scan runs afterwards; any hit fails the export)")
    parser.add_argument("--report", help="also write the redaction report (file/kind/count, never values) to this file")
    parser.add_argument("--rescan", metavar="BUNDLE", help="run ONLY the independent second scan over an existing bundle; exit non-zero on any hit")
    args = parser.parse_args(argv)

    if args.rescan:
        hits = second_scan(Path(args.rescan))
        if hits:
            print("SECOND SCAN FAILED — secret-shaped content in the finished bundle:", file=sys.stderr)
            for hit in hits:
                print(f"  {hit['path']}\t{hit['kind']}\t{hit['count']}", file=sys.stderr)
            return EXIT_SCAN_FAILED
        print("second scan: clean")
        return 0

    if not args.source:
        parser.error("a source directory argument is required (never hard-coded; pass the apparatus copy explicitly)")

    source = Path(args.source).resolve()
    if not source.is_dir():
        print(f"error: source is not a directory: {args.source}", file=sys.stderr)
        return 1

    out = Path(args.out).resolve() if args.out else source.parent / (source.name + ".bundle")
    try:
        out.relative_to(source)
        print("error: the bundle output must not live INSIDE the source directory", file=sys.stderr)
        return 1
    except ValueError:
        pass

    manifest, secrets, report = build_records(source)
    _print_report(report, manifest, secrets)

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            for row in report:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        print(f"report written: {args.report}")

    if not args.write:
        print("dry run (default): nothing written. Re-run with --write to produce the bundle.")
        return 0

    files, distinct = write_bundle(out, manifest, secrets, report)
    print(f"bundle written: {out} ({files} source files, {distinct} distinct redacted blobs)")

    # Plan rule 4: a second, independent scan over what is actually on disk.
    hits = second_scan(out)
    if hits:
        print("SECOND SCAN FAILED — export aborted, bundle is NOT trustworthy:", file=sys.stderr)
        for hit in hits:
            print(f"  {hit['path']}\t{hit['kind']}\t{hit['count']}", file=sys.stderr)
        return EXIT_SCAN_FAILED
    print("second scan: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
