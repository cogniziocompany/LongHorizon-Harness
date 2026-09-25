"""Read-only overseer-state tools over the migrated apparatus archive (task 235).

Task 104b migrated the overseer apparatus off the PTAIT09 workstation into this
repository: ``tasks/`` (task briefs), ``queue/done/`` and ``queue/blocked/``
(queue-entry snapshots filed at launch), ``docs/LEDGER.md`` (the overseer tick
ledger), ``queue/OPEN-ASKS.md`` (the open-asks register), and
``docs/handoffs/HANDOFF-*.md`` (operator handoffs).  See
``README-OVERSEER-APPARATUS.md`` for the authoritative map.

Before that migration a chat client that needed the overseer's working state had
to read the fleet workstation filesystem directly (``C:/tmp``), which is what
task 204's chat session did.  These tools expose the same state through the
CT-hosted MCP surface so no filesystem read on a fleet host is ever needed.

All six handlers are READ-ONLY over the in-repo archive.  They never write,
they never reach a fleet host, they never read ``C:/tmp`` or equivalent, and
they do not need the deployment Postgres credentials.  The only credential the
call site may supply is the env-var NAME ``LH_HARNESS_APPARATUS_ROOT`` (an
override path); no value is stored or printed.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# Where the archive lives in a source checkout.  ``src/lh_harness/overseer_state.py``
# -> parents[0] = src/lh_harness, parents[1] = src, parents[2] = repo root.
_DEFAULT_ROOT = Path(__file__).resolve().parents[2]
_ENV_ROOT_NAME = "LH_HARNESS_APPARATUS_ROOT"

# Output bounds.  The queue archive contains one note that is ~60 kB, so a
# 64 kB cap preserves the large block reasons while keeping JSON responses
# bounded.
_MAX_NAME_CHARS = 256
_MAX_QUERY_CHARS = 256
_MAX_NOTE_CHARS = 64_000
_MAX_TASK_TEXT_CHARS = 100_000
_MAX_LEDGER_ROW_CHARS = 30_000
_MAX_HANDOFF_CHARS = 64_000
_MAX_LIST_LIMIT = 500
_DEFAULT_LIST_LIMIT = 100
_DEFAULT_LEDGER_LIMIT = 20

# Markdown-table cells can contain escaped pipes; parse them before splitting.
_MD_PIPE_RE = re.compile(r"(?<!\\)\|")


def resolve_overseer_root(root: str | Path | None = None) -> Path | None:
    """Return the apparatus root, or ``None`` if the archive is not present.

    Resolution order:

    1. The ``root`` argument if provided and valid.
    2. The ``LH_HARNESS_APPARATUS_ROOT`` environment variable if set and valid.
    3. This checkout's repository root if ``tasks/``, ``queue/`` and ``docs/``
       all exist under it.

    An explicit but invalid root returns ``None`` rather than falling back,
    so a misspelled env-var value surfaces as a configuration error.
    """
    if root is not None:
        return _validated_root(Path(root))
    env = os.environ.get(_ENV_ROOT_NAME)
    if env:
        validated = _validated_root(Path(env))
        if validated is not None:
            return validated
        # A set-but-invalid override is an error; do not silently ignore it.
        return None
    return _validated_root(_DEFAULT_ROOT)


def _validated_root(path: Path) -> Path | None:
    path = path.expanduser().resolve()
    if (
        path.is_dir()
        and (path / "tasks").is_dir()
        and (path / "queue").is_dir()
        and (path / "docs").is_dir()
    ):
        return path
    return None


def _unavailable_response() -> dict[str, Any]:
    return {
        "ok": False,
        "error": "overseer apparatus archive is not available (set LH_HARNESS_APPARATUS_ROOT or run from a source checkout containing tasks/, queue/, docs/)",
        "code": 501,
    }


def _bad_request(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message, "code": 400}


def _not_found(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message, "code": 404}


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **payload}


def _bounded_text(value: Any, *, field: str, max_chars: int, required: bool = False) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        raise ValueError(f"{field} must be a string")
    if required and not text.strip():
        raise ValueError(f"{field} is required")
    if len(text) > max_chars:
        raise ValueError(f"{field} is too long (max {max_chars} characters)")
    if "\x00" in text:
        raise ValueError(f"{field} contains a NUL byte")
    return text


def _bounded_int(value: Any, *, field: str, default: int, lo: int, hi: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if not (lo <= value <= hi):
        raise ValueError(f"{field} must be between {lo} and {hi}")
    return value


def _bounded_bool(value: Any, *, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _safe_stem(name: str) -> str | None:
    """Return a JSON file stem safe enough for directory listing, or None."""
    if not isinstance(name, str) or not name:
        return None
    if name in {".", ".."} or "/" in name or "\\" in name:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
        return None
    return name


def _load_json_records(root: Path, status: str | None = None) -> list[dict[str, Any]]:
    """Load all JSON records from ``queue/<status>/*.json``.

    Each returned dict is the stored JSON plus two synthetic keys:
    ``status`` (``done`` or ``blocked``) and ``record_name`` (file stem).
    """
    statuses: tuple[str, ...]
    if status == "done":
        statuses = ("done",)
    elif status == "blocked":
        statuses = ("blocked",)
    else:
        statuses = ("done", "blocked")

    records: list[dict[str, Any]] = []
    for st in statuses:
        dir_path = root / "queue" / st
        if not dir_path.is_dir():
            continue
        for path in sorted(dir_path.glob("*.json")):
            stem = path.stem
            if not _safe_stem(stem):
                continue
            try:
                with path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            data["status"] = st
            data["record_name"] = stem
            records.append(data)
    return records


def _resolve_task_text(root: Path, record: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return ``(task_text, resolved_path)`` for a queue record.

    ``task_file`` in the archive is often an absolute Windows path
    (``C:/tmp/<name>-task.txt``).  The 104b migration preserves the original
    value verbatim but also placed the brief in ``tasks/<basename>``.
    We resolve by basename only, so we never read outside the repo.
    """
    task_file = record.get("task_file")
    if not isinstance(task_file, str) or not task_file:
        return None, None
    basename = Path(task_file).name
    if not basename:
        return None, None
    candidate = root / "tasks" / basename
    if not candidate.is_file():
        # Some briefs use ``.md`` in the archive even though task_file says ``.txt``.
        md_candidate = root / "tasks" / f"{Path(basename).stem}.md"
        if md_candidate.is_file():
            candidate = md_candidate
        else:
            return None, None
    try:
        text = candidate.read_text(encoding="utf-8")
    except Exception:
        return None, None
    if len(text) > _MAX_TASK_TEXT_CHARS:
        text = text[: _MAX_TASK_TEXT_CHARS] + "\n[truncated by overseer_state]"
    return text, str(candidate.relative_to(root))


def _record_note(record: dict[str, Any]) -> str | None:
    note = record.get("note")
    if not isinstance(note, str) or not note:
        return None
    if len(note) > _MAX_NOTE_CHARS:
        return note[: _MAX_NOTE_CHARS] + "\n[truncated by overseer_state]"
    return note


def _matches_query(record: dict[str, Any], query: str) -> bool:
    q = query.lower()
    haystacks: list[str] = []
    for key in ("record_name", "name", "run_id", "task_file"):
        value = record.get(key)
        if isinstance(value, str):
            haystacks.append(value.lower())
    return any(q in hay for hay in haystacks)


def _sort_records_by_launched_at(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _key(r: dict[str, Any]) -> str:
        launched = r.get("launched_at")
        # ISO strings sort correctly; unknown entries sink to the bottom.
        if isinstance(launched, str):
            return launched
        return ""

    return sorted(records, key=_key, reverse=True)


def _entry_summary(record: dict[str, Any], include_note: bool = True) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "record_name": record["record_name"],
        "status": record["status"],
        "name": record.get("name"),
        "task_file": record.get("task_file"),
        "workspace": record.get("workspace"),
        "trio": record.get("trio"),
        "run_id": record.get("run_id"),
        "launched_at": record.get("launched_at"),
        "max_rounds": record.get("max_rounds"),
    }
    if include_note:
        note = _record_note(record)
        if note is not None:
            summary["note"] = note
    return summary


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def get_queue_entry(
    arguments: dict[str, Any],
    *,
    overseer_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return the full task text and note for one apparatus queue record.

    Argument ``name`` may be the record's file stem (e.g. ``9999h-168-harness-queue-cutover``),
    the entry's ``name`` field, or its ``run_id``.  The resolved task brief is
    read from ``tasks/<basename>`` in the repo; the original ``task_file`` path
    is never used to reach a fleet host.
    """
    root = resolve_overseer_root(overseer_root)
    if root is None:
        return _unavailable_response()

    try:
        query = _bounded_text(
            arguments.get("name"), field="name", max_chars=_MAX_NAME_CHARS, required=True
        )
    except ValueError as exc:
        return _bad_request(str(exc))

    records = _load_json_records(root)
    query_lower = query.lower()

    # Prefer exact matches (stem, name, run_id) over substring matches.
    exact = [
        r
        for r in records
        if r.get("record_name", "").lower() == query_lower
        or r.get("name", "").lower() == query_lower
        or r.get("run_id", "").lower() == query_lower
    ]
    if exact:
        # Return the newest exact match by launched_at; there should only be one.
        chosen = _sort_records_by_launched_at(exact)[0]
    else:
        matches = [r for r in records if _matches_query(r, query)]
        if not matches:
            return _not_found(
                f"no queue record matches {query!r}; use list_queue to browse records"
            )
        if len(matches) > 1:
            return _ok(
                {
                    "matched": len(matches),
                    "note": f"multiple records match {query!r}; pass the exact record_name to get one",
                    "entries": [_entry_summary(r, include_note=False) for r in matches[:20]],
                }
            )
        chosen = matches[0]

    task_text, task_path = _resolve_task_text(root, chosen)
    note = _record_note(chosen)
    entry = _entry_summary(chosen, include_note=False)
    result: dict[str, Any] = {
        "record_name": chosen["record_name"],
        "status": chosen["status"],
        "entry": entry,
    }
    if task_text is not None:
        result["task_text"] = task_text
        result["task_file_resolved"] = task_path
    if note is not None:
        result["note"] = note
    return _ok(result)


def list_queue(
    arguments: dict[str, Any],
    *,
    overseer_root: str | Path | None = None,
) -> dict[str, Any]:
    """List the migrated queue archive (done/blocked entries) with skip reasons.

    Skip and block reasons are carried verbatim in each entry's ``note`` field.
    Pass ``status`` to filter by ``done`` or ``blocked``, and ``limit`` to cap
    the returned list (newest launches first).
    """
    root = resolve_overseer_root(overseer_root)
    if root is None:
        return _unavailable_response()

    try:
        status = _bounded_text(
            arguments.get("status"), field="status", max_chars=32
        ).lower()
        limit = _bounded_int(
            arguments.get("limit"),
            field="limit",
            default=_DEFAULT_LIST_LIMIT,
            lo=1,
            hi=_MAX_LIST_LIMIT,
        )
        query = _bounded_text(
            arguments.get("query"), field="query", max_chars=_MAX_QUERY_CHARS
        )
    except ValueError as exc:
        return _bad_request(str(exc))

    if status and status not in {"done", "blocked", "all"}:
        return _bad_request("status must be one of: done, blocked, all")

    records = _load_json_records(root, status=None if status in ("", "all") else status)
    records = _sort_records_by_launched_at(records)
    if query:
        records = [r for r in records if _matches_query(r, query)]

    counts = {"done": 0, "blocked": 0, "total": 0}
    for r in records:
        counts["total"] += 1
        if r["status"] == "done":
            counts["done"] += 1
        elif r["status"] == "blocked":
            counts["blocked"] += 1

    entries = [_entry_summary(r, include_note=True) for r in records[:limit]]
    return _ok({"entries": entries, "counts": counts, "returned": len(entries)})


def get_task_history(
    arguments: dict[str, Any],
    *,
    overseer_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return all queue-archive runs matching a task number or name.

    ``task`` may be a task number (``204``), a name fragment
    (``env-access-token``), or a ``run_id``.
    """
    root = resolve_overseer_root(overseer_root)
    if root is None:
        return _unavailable_response()

    try:
        query = _bounded_text(
            arguments.get("task"), field="task", max_chars=_MAX_NAME_CHARS, required=True
        )
        limit = _bounded_int(
            arguments.get("limit"),
            field="limit",
            default=_DEFAULT_LIST_LIMIT,
            lo=1,
            hi=_MAX_LIST_LIMIT,
        )
    except ValueError as exc:
        return _bad_request(str(exc))

    records = _load_json_records(root)
    query_lower = query.lower()
    exact_run_id = [r for r in records if r.get("run_id", "").lower() == query_lower]
    if exact_run_id:
        matches = exact_run_id
    else:
        matches = [r for r in records if _matches_query(r, query)]

    matches = _sort_records_by_launched_at(matches)

    def _run_summary(r: dict[str, Any]) -> dict[str, Any]:
        summary = _entry_summary(r, include_note=False)
        note = _record_note(r)
        if note is not None:
            # History is a survey; keep the response compact but informative.
            if len(note) > 2_000:
                note = note[:2_000] + "\n[truncated; use get_queue_entry for the full note]"
            summary["note_excerpt"] = note
        return summary

    return _ok(
        {
            "task": query,
            "total": len(matches),
            "runs": [_run_summary(r) for r in matches[:limit]],
        }
    )


def read_ledger(
    arguments: dict[str, Any],
    *,
    overseer_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return recent rows from ``docs/LEDGER.md``.

    Each row is a ``##`` section (one tick).  Pass ``limit`` to cap the number
    of rows, and ``query`` to filter rows by substring.
    """
    root = resolve_overseer_root(overseer_root)
    if root is None:
        return _unavailable_response()

    try:
        limit = _bounded_int(
            arguments.get("limit"),
            field="limit",
            default=_DEFAULT_LEDGER_LIMIT,
            lo=1,
            hi=500,
        )
        query = _bounded_text(
            arguments.get("query"), field="query", max_chars=_MAX_QUERY_CHARS
        ).lower()
    except ValueError as exc:
        return _bad_request(str(exc))

    ledger_path = root / "docs" / "LEDGER.md"
    if not ledger_path.is_file():
        return _not_found("docs/LEDGER.md is not present in the apparatus archive")

    try:
        text = ledger_path.read_text(encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "error": f"could not read ledger: {exc}", "code": 500}

    # Split on section headings like "## TICK ..." or "## 2026-09-16 ...".
    heading_re = re.compile(r"(?m)^## ", re.MULTILINE)
    starts = [m.start() for m in heading_re.finditer(text)]
    if not starts:
        return _ok({"total_rows": 0, "rows": [], "returned": 0})

    starts.append(len(text))
    rows: list[dict[str, Any]] = []
    for i in range(len(starts) - 1):
        chunk = text[starts[i] : starts[i + 1]]
        # The heading line is the first line; the rest is the body.
        lines = chunk.splitlines()
        heading = lines[0].removeprefix("## ").strip() if lines else ""
        body = "\n".join(lines[1:]).strip()
        rows.append({"index": i + 1, "heading": heading, "body": body})

    total_rows = len(rows)

    if query:
        rows = [r for r in rows if query in r["heading"].lower() or query in r["body"].lower()]
    else:
        rows = rows[-limit:]

    rendered: list[dict[str, Any]] = []
    for r in rows[:limit]:
        body = r["body"]
        if len(body) > _MAX_LEDGER_ROW_CHARS:
            body = body[: _MAX_LEDGER_ROW_CHARS] + "\n[truncated by overseer_state]"
        rendered.append({"index": r["index"], "heading": r["heading"], "body": body})

    return _ok(
        {
            "total_rows": total_rows,
            "returned": len(rendered),
            "rows": rendered,
        }
    )


def list_open_asks(
    arguments: dict[str, Any],
    *,
    overseer_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return rows from ``queue/OPEN-ASKS.md``.

    By default only rows whose ``state`` column still looks open are returned.
    Pass ``include_closed: true`` to see closed/answered/superseded rows too.
    """
    root = resolve_overseer_root(overseer_root)
    if root is None:
        return _unavailable_response()

    try:
        include_closed = _bounded_bool(
            arguments.get("include_closed"),
            field="include_closed",
            default=False,
        )
        limit = _bounded_int(
            arguments.get("limit"),
            field="limit",
            default=_DEFAULT_LIST_LIMIT,
            lo=1,
            hi=_MAX_LIST_LIMIT,
        )
    except ValueError as exc:
        return _bad_request(str(exc))

    asks_path = root / "queue" / "OPEN-ASKS.md"
    if not asks_path.is_file():
        return _not_found("queue/OPEN-ASKS.md is not present in the apparatus archive")

    try:
        text = asks_path.read_text(encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "error": f"could not read open asks: {exc}", "code": 500}

    lines = text.splitlines()

    # Find the active-asks table: header row with exactly these seven columns.
    header_idx: int | None = None
    header_columns: tuple[str, ...] = (
        "id",
        "ask",
        "kind",
        "evidence",
        "recommended",
        "default if silent",
        "state",
    )
    for i, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        raw_cells = _split_table_cells(line)
        cells = [c.strip().lower().strip("*") for c in raw_cells]
        if tuple(cells[:7]) == header_columns:
            header_idx = i
            break

    if header_idx is None:
        return _not_found("could not find the open-asks table in queue/OPEN-ASKS.md")

    # Header note: the first non-empty line before the table, usually the
    # "Updated ..." summary line.
    updated_line = ""
    for line in reversed(lines[:header_idx]):
        stripped = line.strip()
        if stripped and not stripped.startswith("|") and not stripped.startswith("#"):
            updated_line = stripped
            break

    rows: list[dict[str, Any]] = []
    for line in lines[header_idx + 2 :]:
        if not line.startswith("|"):
            break
        if line.lstrip().startswith("|---"):
            continue
        cells = _split_table_cells(line)
        if len(cells) < 7:
            continue
        row = {
            "id": cells[0],
            "ask": cells[1],
            "kind": cells[2],
            "evidence": cells[3],
            "recommended": cells[4],
            "default_if_silent": cells[5],
            "state": cells[6],
        }
        rows.append(row)

    total_rows = len(rows)
    if not include_closed:
        closed_prefixes = ("closed", "answered", "done", "resolved", "superseded")
        rows = [
            r
            for r in rows
            if not r["state"].lower().lstrip("*").startswith(closed_prefixes)
        ]

    return _ok(
        {
            "updated_line": updated_line,
            "total_rows": total_rows,
            "open_rows": len(rows),
            "rows": rows[:limit],
        }
    )


def _split_table_cells(line: str) -> list[str]:
    """Split a markdown table row on unescaped pipes and strip cell text."""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    parts = _MD_PIPE_RE.split(line)
    return [p.replace("\\|", "|").strip() for p in parts]


def _handoff_date_from_stem(stem: str) -> str | None:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", stem)
    if m:
        return m.group(0)
    return None


def get_handoff(
    arguments: dict[str, Any],
    *,
    overseer_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return a handoff from ``docs/handoffs/HANDOFF-*.md``.

    With no ``name`` the most recent handoff (by filename date) is returned.
    With ``name`` the file is matched by exact filename, stem, or unique
    substring.
    """
    root = resolve_overseer_root(overseer_root)
    if root is None:
        return _unavailable_response()

    try:
        name = _bounded_text(arguments.get("name"), field="name", max_chars=_MAX_NAME_CHARS)
    except ValueError as exc:
        return _bad_request(str(exc))

    handoffs_dir = root / "docs" / "handoffs"
    if not handoffs_dir.is_dir():
        return _not_found("docs/handoffs/ is not present in the apparatus archive")

    files: list[tuple[Path, str, str | None]] = []
    for path in sorted(handoffs_dir.glob("*.md")):
        stem = path.stem
        if not _safe_stem(stem):
            continue
        files.append((path, stem, _handoff_date_from_stem(stem)))
    if not files:
        return _not_found("no handoff files found in docs/handoffs/")

    # Newest first: date descending, then filename descending for stable ties.
    files.sort(key=lambda t: (t[2] or "0000-00-00", t[1]), reverse=True)

    if not name:
        chosen = files[0]
    else:
        name_lower = name.lower()
        # Exact filename (with or without .md) or exact stem.
        exact = [
            f
            for f in files
            if f[1].lower() == name_lower
            or f[1].lower() == name_lower.removesuffix(".md")
            or f[0].name.lower() == name_lower
        ]
        if exact:
            chosen = exact[0]
        else:
            sub = [f for f in files if name_lower in f[1].lower()]
            if not sub:
                return _not_found(
                    f"no handoff matches {name!r}; use list_handoffs=True or pick from available"
                )
            if len(sub) > 1:
                return _ok(
                    {
                        "matched": len(sub),
                        "note": f"multiple handoffs match {name!r}; pass the exact filename",
                        "available": [f[1] for f in files[:50]],
                    }
                )
            chosen = sub[0]

    path, stem, date = chosen
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "error": f"could not read handoff: {exc}", "code": 500}

    if len(text) > _MAX_HANDOFF_CHARS:
        text = text[: _MAX_HANDOFF_CHARS] + "\n[truncated by overseer_state]"

    return _ok(
        {
            "handoff": {
                "name": stem,
                "file": str(path.relative_to(root)),
                "date": date,
                "text": text,
            },
            "available": [f[1] for f in files[:50]],
        }
    )
