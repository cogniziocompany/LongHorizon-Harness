"""Durable per-round checkpoints for an in-flight run.

A run records one checkpoint per finished round below
``<log_dir>/role_orchestration/rounds/round_NNN/checkpoint.json``.  The writer
lived in ``manager._record_round`` and the only reader was the fail-closed
validator in ``RunSupervisor._validate_latest_round_checkpoint``: validation
existed, but nothing turned a validated checkpoint back into the round state a
successor needs to resume at the last completed round.

This module is that primitive.  It is deliberately storage-agnostic — it takes
the role directory the manager already owns and knows nothing about a
supervisor, a worker command, or a launcher shape:

* :func:`build_round_checkpoint` / :func:`write_round_checkpoint` produce the
  same payload (and the same fingerprint) the manager writes, so the existing
  validator keeps accepting these files.
* :func:`read_last_completed_round` returns the latest completed round together
  with the round the successor should start at, or an explicit failure state —
  ``missing`` (nothing recorded yet), ``corrupt`` (unreadable, torn or
  tampered) or ``foreign`` (well-formed, but not a ``managed_round`` checkpoint
  of this schema, or stored under a round slot it does not describe).

The read path fails closed on purpose.  A successor that cannot prove which
rounds completed must fall back to a fresh start rather than silently replay or
skip work; see ``docs/design/recovery-slice-c.md`` §1.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from .supervisor.control_bus import (
    _atomic_bytes_write,
    _ensure_dir_nofollow,
    _open_nofollow,
)
from .types import MAX_ROUNDS, ManagedRound, RoleNextStep

# The writer and the reader must agree on these two labels: they are what makes
# a checkpoint in a run directory *this* kind of checkpoint rather than some
# other product's file that happens to be valid JSON.
MANAGED_ROUND_KIND = "managed_round"
ROUND_CHECKPOINT_SCHEMA_VERSION = 1

# ``types.RoleNextStep`` is the single source of truth for the route values; a
# checkpoint that carries anything else is rebuilt as ``invalid`` exactly as
# ``manager._managed_round_from_dict`` does for the ledger.
_ROUTE_VALUES = frozenset(get_args(RoleNextStep))

# Fields the fingerprint does not cover: they are produced by the writer, so a
# checksum that included them could never be recomputed by a reader.
_FINGERPRINT_EXCLUDED = frozenset({"schema_version", "checkpoint_kind", "recorded_at", "fingerprint"})

_MAX_CHECKPOINT_BYTES = 8 * 1024 * 1024
_ROUND_PREFIX = "round_"

CheckpointStatus = Literal["ok", "missing", "corrupt", "foreign"]


@dataclass(frozen=True)
class RoundCheckpointRead:
    """Outcome of reading the last completed round of one run.

    ``status`` is the only thing callers must branch on.

    * ``ok`` — ``round`` is the last completed round and ``resume_from_round``
      is that round's index; the successor continues at ``resume_from_round + 1``.
    * ``missing`` — no checkpoint has been written yet (a run that died in its
      first round, or a run directory that predates checkpoints).
    * ``corrupt`` — a checkpoint exists but is unreadable, torn, tampered, or
      the sequence of rounds has a hole, so the completed prefix cannot be
      trusted.
    * ``foreign`` — the file is a valid JSON object but not a checkpoint this
      reader owns (another ``checkpoint_kind`` or schema version), or it claims
      a round index other than the directory it sits in.
    """

    status: CheckpointStatus
    round: ManagedRound | None = None
    path: Path | None = None
    reason: str = ""
    resume_from_round: int = 0
    completed_rounds: tuple[int, ...] = ()

    @property
    def next_round(self) -> int:
        """The round a resumed successor executes next (``1`` when nothing is usable)."""

        return self.resume_from_round + 1

    @property
    def resumable(self) -> bool:
        return self.status == "ok"


def round_checkpoint_path(role_dir: Path, round_index: int) -> Path:
    """Return the local per-round checkpoint path used to resume a round."""

    return Path(role_dir) / "rounds" / f"round_{round_index:03d}" / "checkpoint.json"


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def checkpoint_fingerprint(checkpoint: dict[str, Any]) -> str:
    """Recompute the fingerprint over the canonical round record fields."""

    canonical_record = {
        key: value for key, value in checkpoint.items() if key not in _FINGERPRINT_EXCLUDED
    }
    return hashlib.sha256(_canonical(canonical_record)).hexdigest()


def build_round_checkpoint(
    record: ManagedRound,
    *,
    recorded_at: float | None = None,
) -> dict[str, Any]:
    """Build an independently verifiable checkpoint for one recorded round.

    The checkpoint contains the same durable fields as the ``rounds.jsonl``
    ledger plus a content hash so resume/continuation can detect a torn write.
    The hash covers only the record fields, so it can be recomputed from the
    file alone.
    """

    payload = asdict(record)
    payload["schema_version"] = ROUND_CHECKPOINT_SCHEMA_VERSION
    payload["checkpoint_kind"] = MANAGED_ROUND_KIND
    payload["recorded_at"] = time.time() if recorded_at is None else recorded_at
    payload["fingerprint"] = checkpoint_fingerprint(payload)
    return payload


def write_round_checkpoint(role_dir: Path, record: ManagedRound) -> Path:
    """Atomically persist the checkpoint for ``record`` and return its path."""

    path = round_checkpoint_path(role_dir, record.round_index)
    _ensure_dir_nofollow(path.parent)
    payload = build_round_checkpoint(record)
    _atomic_bytes_write(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    return path


def _read_checkpoint_bytes(path: Path) -> bytes:
    fd = _open_nofollow(path)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("checkpoint is not a private regular file")
        if metadata.st_size > _MAX_CHECKPOINT_BYTES:
            raise ValueError("checkpoint is too large to read safely")
        raw = os.read(fd, _MAX_CHECKPOINT_BYTES + 1)
        if len(raw) > _MAX_CHECKPOINT_BYTES:
            raise ValueError("checkpoint is too large to read safely")
        return raw
    finally:
        os.close(fd)


def _result(
    status: CheckpointStatus,
    *,
    reason: str,
    path: Path | None = None,
    record: ManagedRound | None = None,
    resume_from_round: int = 0,
    completed_rounds: tuple[int, ...] = (),
) -> RoundCheckpointRead:
    return RoundCheckpointRead(
        status=status,
        round=record,
        path=path,
        reason=reason,
        resume_from_round=resume_from_round,
        completed_rounds=completed_rounds,
    )


def _round_slots(rounds_dir: Path) -> list[tuple[int, Path]]:
    """Return ``(index, checkpoint path)`` for every readable round directory."""

    slots: list[tuple[int, Path]] = []
    for entry in rounds_dir.iterdir():
        if not entry.is_dir():
            continue
        suffix = entry.name.removeprefix(_ROUND_PREFIX)
        if suffix == entry.name or not suffix.isdecimal():
            continue
        index = int(suffix)
        if not 1 <= index <= MAX_ROUNDS:
            continue
        checkpoint_path = entry / "checkpoint.json"
        if checkpoint_path.is_file():
            slots.append((index, checkpoint_path))
    slots.sort(key=lambda item: item[0])
    return slots


def _parse_slot(index: int, path: Path) -> RoundCheckpointRead:
    """Validate one checkpoint file at round slot ``index``."""

    try:
        raw = _read_checkpoint_bytes(path)
    except (OSError, ValueError) as exc:
        return _result("corrupt", reason=f"unreadable: {exc}", path=path)
    try:
        checkpoint = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _result("corrupt", reason=f"malformed json: {exc}", path=path)
    if not isinstance(checkpoint, dict):
        return _result("corrupt", reason="checkpoint is not an object", path=path)

    kind = checkpoint.get("checkpoint_kind")
    if kind != MANAGED_ROUND_KIND:
        return _result("foreign", reason=f"unexpected checkpoint_kind {kind!r}", path=path)
    version = checkpoint.get("schema_version")
    if version != ROUND_CHECKPOINT_SCHEMA_VERSION:
        return _result("foreign", reason=f"unsupported schema_version {version!r}", path=path)

    stored_fingerprint = str(checkpoint.get("fingerprint") or "")
    if not stored_fingerprint:
        return _result("corrupt", reason="no fingerprint", path=path)
    if checkpoint_fingerprint(checkpoint) != stored_fingerprint:
        return _result(
            "corrupt",
            reason="fingerprint mismatch: checkpoint may be tampered or torn",
            path=path,
        )

    record = _managed_round_from_checkpoint(checkpoint)
    if record is None:
        return _result("corrupt", reason="round record is unusable", path=path)
    if record.round_index != index:
        return _result(
            "foreign",
            reason=(
                f"checkpoint describes round {record.round_index} "
                f"but is stored under round_{index:03d}"
            ),
            path=path,
        )
    return _result("ok", reason="", path=path, record=record, resume_from_round=index)


def _managed_round_from_checkpoint(payload: dict[str, Any]) -> ManagedRound | None:
    """Rebuild a :class:`ManagedRound` from a fingerprinted checkpoint payload."""

    index = payload.get("round_index")
    if not isinstance(index, int) or isinstance(index, bool) or not 1 <= index <= MAX_ROUNDS:
        return None

    def _text(key: str) -> str:
        value = payload.get(key)
        return value if isinstance(value, str) else ""

    def _status(key: str) -> dict[str, Any]:
        value = payload.get(key)
        return value if isinstance(value, dict) else {}

    refs = payload.get("related_report_refs")
    next_step = payload.get("next_step")
    try:
        return ManagedRound(
            round_index=index,
            next_step=next_step if next_step in _ROUTE_VALUES else "invalid",
            plan_text=_text("plan_text"),
            executor_output=_text("executor_output"),
            auditor_report=_text("auditor_report"),
            harness_feedback=_text("harness_feedback"),
            task_state=_text("task_state"),
            task_contract=_text("task_contract"),
            related_report_refs=[item for item in refs if isinstance(item, str)]
            if isinstance(refs, list)
            else [],
            manager_status=_status("manager_status"),
            executor_status=_status("executor_status"),
            auditor_status=_status("auditor_status"),
        )
    except (TypeError, ValueError):
        return None


def read_last_completed_round(role_dir: Path) -> RoundCheckpointRead:
    """Return the last completed round recorded under ``role_dir``.

    The completed rounds must form a contiguous run from round 1: the manager
    writes one checkpoint per finished round, so a hole means a file was lost or
    removed and the recorded prefix can no longer be trusted to describe the
    run.  Any doubtful state is returned as ``corrupt``/``foreign``; only an
    ``ok`` result may be used to resume.
    """

    role_dir = Path(role_dir)
    rounds_dir = role_dir / "rounds"
    try:
        if not rounds_dir.is_dir():
            return _result("missing", reason="no rounds directory", path=rounds_dir)
        slots = _round_slots(rounds_dir)
    except OSError as exc:
        return _result("corrupt", reason=f"cannot read rounds directory: {exc}", path=rounds_dir)
    if not slots:
        return _result("missing", reason="no round checkpoint recorded", path=rounds_dir)

    completed: list[int] = []
    latest: RoundCheckpointRead | None = None
    for index, path in slots:
        parsed = _parse_slot(index, path)
        if parsed.status != "ok":
            return RoundCheckpointRead(
                status=parsed.status,
                path=parsed.path,
                reason=f"round_{index:03d}: {parsed.reason}",
                completed_rounds=tuple(completed),
            )
        completed.append(index)
        latest = parsed

    expected = list(range(1, completed[-1] + 1))
    if completed != expected:
        missing_round = next(index for index in expected if index not in completed)
        return _result(
            "corrupt",
            reason=f"completed rounds are not contiguous; round_{missing_round:03d} is missing",
            path=rounds_dir,
            completed_rounds=tuple(completed),
        )

    assert latest is not None and latest.round is not None
    return RoundCheckpointRead(
        status="ok",
        round=latest.round,
        path=latest.path,
        reason="",
        resume_from_round=latest.resume_from_round,
        completed_rounds=tuple(completed),
    )
