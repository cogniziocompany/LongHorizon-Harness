"""Run-dir store for L1 experience traces (append-only JSONL).

The store writes exactly one file, ``role_orchestration/experience.jsonl``,
inside the run's own ledger directory — the same directory that already holds
``rounds.jsonl`` and ``report.json``. There is deliberately no state-root
store: the hydra-fleet ingester already walks run dirs, so per-run placement
is what carries each trace to ``hivemind_sessions.session_memories.metadata``
with no schema change. The workspace (the directory the task's agents write
into) is never a write target of this module; every record goes through the
anchored no-follow open pattern from the control bus, so a swapped symlink in
the ledger path fails closed instead of writing somewhere unexpected.

Durability rules (task spec, slice 3):

* **Append-only.** Existing lines are never rewritten, truncated, or
  reordered. A resumed or double capture recomputes the same records and the
  dedupe key keeps the ledger at one record set.
* **Dedupe on (run_id, round_index).** Each record also carries a
  ``content_hash`` (sha256 over the canonical JSON of the trace payload) so
  the downstream ingester can content-dedupe without parsing semantics.
* **Size caps.** A single record above ``MAX_RECORD_BYTES`` is dropped
  (the harness's trace caps make this a guard, not a normal path), and the
  file as a whole stops growing at ``MAX_FILE_BYTES`` — excess records are
  counted as dropped, never spooled elsewhere.
* **Tolerant reads.** The existing ledger is scanned with a bounded
  no-follow read; malformed or truncated lines (a finalization cut mid-line)
  are skipped, never fatal.

Records must already be redacted (:mod:`lh_harness.experience.redact`) and
valued (:mod:`lh_harness.experience.backfill`) before they reach this module:
the store is a persistence choke point, not a semantics layer.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..supervisor.control_bus import (
    _ensure_dir_fd_nofollow,
    _open_nofollow,
    _open_private_regular_at,
)

EXPERIENCE_FILENAME = "experience.jsonl"

# Mirrors the control-bus record budget (`_MAX_CONTROL_RECORD_BYTES`): trace
# fields are already capped by TraceCaps, so anything larger is corruption or
# a bug and is dropped rather than persisted.
MAX_RECORD_BYTES = 512 * 1024
# The file cap keeps one run's ledger bounded on shared fleet volumes. The
# ingester dedupes downstream, so dropping the tail here never loses the
# run's early (usually most informative) rounds.
MAX_FILE_BYTES = 64 * 1024 * 1024
# Bounded scan of the existing ledger, sized to the file cap plus slack for
# an in-flight append.
_SCAN_MAX_BYTES = MAX_FILE_BYTES + 1024 * 1024

_DEDUPE_KEY_FIELDS = ("run_id", "round_index")


@dataclass(frozen=True)
class StoreStats:
    """What one append call did, for the capture result and diagnostics."""

    path: str
    written: int = 0
    skipped_duplicates: int = 0
    dropped: int = 0
    file_capped: bool = False


def experience_ledger_path(role_dir: str | os.PathLike[str]) -> Path:
    """The single store file below a run's ``role_orchestration`` ledger."""
    return Path(role_dir) / EXPERIENCE_FILENAME


def content_hash(record: Mapping[str, Any]) -> str:
    """sha256 over the canonical JSON of one record (hash field excluded)."""
    payload = {key: value for key, value in dict(record).items() if key != "content_hash"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def dedupe_key(record: Mapping[str, Any]) -> tuple[str, int] | None:
    """The (run_id, round_index) identity of one record, or ``None``."""
    run_id = str(record.get("run_id") or "").strip()
    if not run_id:
        return None
    try:
        round_index = int(record.get("round_index"))
    except (TypeError, ValueError):
        return None
    return (run_id, round_index)


def append_trace_records(
    path: str | os.PathLike[str],
    records: Iterable[Mapping[str, Any]],
) -> StoreStats:
    """Append new trace records to the run-dir ledger.

    Callers pass plain dict payloads (``TraceUnit.to_dict()`` output plus any
    transport metadata). A ``content_hash`` is attached to any record that
    lacks one, computed over the redacted payload so identical captures hash
    identically. Records whose (run_id, round_index) key already exists — or
    that have no valid key — are not written.
    """
    target = Path(path)
    existing_size, existing_keys, tail_needs_newline = _scan_existing(target)
    lines: list[bytes] = []
    written = 0
    skipped = 0
    dropped = 0
    size_after = existing_size
    file_capped = existing_size >= MAX_FILE_BYTES
    for record in records or ():
        if not isinstance(record, Mapping):
            dropped += 1
            continue
        key = dedupe_key(record)
        if key is None:
            dropped += 1
            continue
        if key in existing_keys:
            skipped += 1
            continue
        payload = dict(record)
        payload.setdefault("content_hash", content_hash(payload))
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        if len(line) > MAX_RECORD_BYTES:
            dropped += 1
            continue
        if tail_needs_newline:
            # A writer cut mid-line leaves an unterminated tail. Terminate it
            # before appending so the torn record never glues onto a new one.
            line = b"\n" + line
            tail_needs_newline = False
        if size_after + len(line) > MAX_FILE_BYTES:
            file_capped = True
            dropped += 1
            continue
        lines.append(line)
        existing_keys.add(key)
        size_after += len(line)
        written += 1
    if lines:
        _append_lines_nofollow(target, lines)
    return StoreStats(
        path=str(target),
        written=written,
        skipped_duplicates=skipped,
        dropped=dropped,
        file_capped=file_capped,
    )


def _scan_existing(path: Path) -> tuple[int, set[tuple[str, int]], bool]:
    """Return (current size, dedupe keys, unterminated tail) reading the ledger defensively.

    A symlinked ledger path fails the no-follow open and is treated as
    unreadable (zero size, no keys): the subsequent anchored append then also
    fails closed, so a swapped ledger can never be written through.
    """
    fd: int | None = None
    try:
        fd = _open_nofollow(path)
        metadata = os.fstat(fd)
        if not stat_is_regular(metadata):
            return (0, set(), False)
        size = int(metadata.st_size)
        remaining = size + 1
        bounded = min(remaining, _SCAN_MAX_BYTES)
        data = bytearray()
        while bounded > 0:
            chunk = os.read(fd, min(bounded, 1024 * 1024))
            if not chunk:
                break
            data.extend(chunk)
            bounded -= len(chunk)
        body = bytes(data)
        keys: set[tuple[str, int]] = set()
        for raw in body.split(b"\n"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                parsed = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                # A truncated tail line is skipped, never fatal.
                continue
            if isinstance(parsed, dict):
                key = dedupe_key(parsed)
                if key is not None:
                    keys.add(key)
        unterminated = bool(body) and not body.endswith(b"\n")
        return (size, keys, unterminated)
    except OSError:
        return (0, set(), False)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def stat_is_regular(metadata: os.stat_result) -> bool:
    import stat as stat_module

    return stat_module.S_ISREG(metadata.st_mode)


def _append_lines_nofollow(path: Path, lines: list[bytes]) -> None:
    """Append pre-serialised records under the anchored no-follow pattern."""
    parent_fd: int | None = None
    fd: int | None = None
    try:
        parent_fd = _ensure_dir_fd_nofollow(path.parent)
        fd = _open_private_regular_at(parent_fd, path.name, os.O_WRONLY | os.O_APPEND)
        handle = os.fdopen(fd, "ab")
        fd = None
        with handle:
            for line in lines:
                handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass
