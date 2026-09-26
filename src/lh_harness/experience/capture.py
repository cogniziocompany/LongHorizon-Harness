"""Single entry point that turns one finished run into persisted L1 traces.

``persist_run_experience`` runs at run finalization (manager ``_run_impl``,
immediately after the final report is built and written) and assembles the
whole MSCE Phase 1 pipeline in one place:

1. the terminal reward R, split into goal/process/satisfaction terms
   (:mod:`.reward`) from the final report, the approval ledger
   (``role_orchestration/approvals.jsonl``) and the operator control-bus
   commands (``control/commands.jsonl``);
2. a deterministic reflection weight alpha per round (:mod:`.reflection`)
   from each round's own AuditReport verdict triple;
3. the run's joinable identity (:mod:`.tags`) — repo, branch, per-role model
   trio — read from run-dir artifacts only (``control/owner.json``, the
   normalized per-round trajectories), never from the live workspace;
4. Eq. 2 value backfill (:mod:`.backfill`) when R carries signal;
5. rule-based redaction of every text field before write
   (:mod:`.redact`, MSCE B.11);
6. the append-only run-dir store (:mod:`.store`).

The gate is **OFF by default**: ``LH_HARNESS_EXPERIENCE=1`` (truthy values
only) or ``[run] experience = true`` in the instance config enables it, and
the env var wins when set so an operator can force a run off without editing
config. When disabled, nothing in this module is touched and run outputs are
byte-identical to a build without the layer.

Device dimension (Paxton 2026-09-08): ``device_id`` / ``terminal_id`` /
``hydra_node`` are left absent. The data the harness records per round today
(tool calls by name, not by execution target) does not identify *which* fleet
device an execution went through, and inventing a value would corrupt every
fleet join built on these ids. For capture to become possible, the harness
would have to record the Hydra exec binding (device/terminal id and node) on
each role episode — that is run-record work, not experience-layer work, and
is deliberately out of slice 3 scope.

Everything here is local-file work at finalization: no network, no LLM call,
no prompt or round-flow change. The manager wraps this entry point in a
never-fatal try/except; the function itself still refuses to raise on bad
run data (missing owner, malformed ledgers) so the failure surface stays
limited to genuine filesystem errors.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..supervisor.control_bus import _open_nofollow
from .backfill import DEFAULT_GAMMA, apply_backfill
from .redact import redact_trace
from .reflection import alpha
from .reward import reward_breakdown
from .store import (
    StoreStats,
    append_trace_records,
    experience_ledger_path,
)
from .tags import (
    detect_branch,
    error_signature,
    next_step_tag,
    repo_from_owner,
    roles_from_owner,
    tool_names_from_round_dir,
)
from .trace import RewardTerms, TraceCaps, trace_unit_from_round

# The feature gate. Env var wins over config when set (either direction), so a
# release window can force the layer off without editing instance config.
ENV_FLAG = "LH_HARNESS_EXPERIENCE"
_TRUTHY = {"1", "true", "yes", "on"}

# Bounded reads of run artifacts. approvals.jsonl and commands.jsonl are
# operator-facing ledgers and stay small; rounds.jsonl can carry large round
# records, so its bound mirrors the trajectory read bound in tags.py.
_APPROVALS_READ_MAX_BYTES = 4 * 1024 * 1024
_COMMANDS_READ_MAX_BYTES = 4 * 1024 * 1024
_ROUNDS_READ_MAX_BYTES = 24 * 1024 * 1024
_OWNER_READ_MAX_BYTES = 64 * 1024

# Log dir names that sit one level below the real run dir in the standard
# supervisor layout (mirrors run_boundary's canonical/legacy names).
_LOG_DIR_NAMES = frozenset({"lh_harness", "cua_harness", "logs"})

_MAX_RUN_ID_CHARS = 128


@dataclass(frozen=True)
class CaptureResult:
    """What one capture did. ``terminal_reward`` is ``None`` for a no-signal
    run (user_cancelled from the web); ``reason`` then names why."""

    run_id: str
    path: str
    rounds_captured: int
    written: int
    skipped_duplicates: int
    dropped: int
    file_capped: bool
    terminal_reward: float | None
    reward_reason: str


def experience_enabled(config: Any = None, *, env: Mapping[str, str] | None = None) -> bool:
    """Resolve the Phase 1 feature gate (default OFF).

    ``LH_HARNESS_EXPERIENCE`` wins when set to a non-empty value (truthy
    enables, anything else disables); otherwise the harness config's
    ``experience`` flag (from ``[run] experience = true``) decides.
    """
    source = env if env is not None else os.environ
    raw = source.get(ENV_FLAG)
    if raw is not None and str(raw).strip():
        return str(raw).strip().lower() in _TRUTHY
    return bool(getattr(config, "experience", False))


def run_dir_for_log_dir(log_dir: str | os.PathLike[str]) -> Path:
    """Best-effort supervisor run dir for a harness log dir.

    Standard layouts put the ledger at ``runs/<id>/lh_harness``; anything
    else (a standalone ``--log-dir``) is its own scope. Used by the manager
    wiring so capture still reads ``control/`` from the real run dir when the
    run is supervised.
    """
    path = Path(log_dir).expanduser().resolve(strict=False)
    if path.name in _LOG_DIR_NAMES:
        return path.parent
    return path


def persist_run_experience(
    rounds: Iterable[Any] | None,
    report: Mapping[str, Any] | None,
    config: Any,
    run_dir: str | os.PathLike[str],
    *,
    gamma: float = DEFAULT_GAMMA,
    occurred_at: str | None = None,
) -> CaptureResult:
    """Build, value, redact and persist this run's L1 traces.

    ``rounds`` are the managed round records (``ManagedRound``s or dicts);
    when ``None`` the durable ledger ``role_orchestration/rounds.jsonl`` is
    read instead, tolerating a malformed or truncated tail. ``report`` is the
    final report dict. Returns a :class:`CaptureResult`; only genuine
    filesystem errors propagate.
    """
    material = (
        [round_record for round_record in rounds]
        if rounds is not None
        else _read_jsonl_tolerant(
            _ledger_dir(config) / "rounds.jsonl", max_bytes=_ROUNDS_READ_MAX_BYTES
        )
    )
    report_data = dict(report) if isinstance(report, Mapping) else {}
    run_root = Path(run_dir).expanduser().resolve(strict=False)
    owner = _read_json_tolerant(
        run_root / "control" / "owner.json", max_bytes=_OWNER_READ_MAX_BYTES
    )
    run_id = _resolve_run_id(owner, run_root)
    task_text = str(report_data.get("task") or "")
    abort_reason = str(report_data.get("abort_reason") or "").strip()

    approvals = _read_jsonl_tolerant(
        _ledger_dir(config) / "approvals.jsonl", max_bytes=_APPROVALS_READ_MAX_BYTES
    )
    operator_messages = _operator_messages(
        _read_jsonl_tolerant(
            run_root / "control" / "commands.jsonl", max_bytes=_COMMANDS_READ_MAX_BYTES
        )
    )

    breakdown = reward_breakdown(report_data, approvals, operator_messages, material, config)

    owner_map = owner if isinstance(owner, Mapping) else {}
    workspace = str(owner_map.get("workspace") or "")
    repo = repo_from_owner(owner_map)
    roles = roles_from_owner(owner_map)
    branch = detect_branch(task_text, run_root)
    caps = TraceCaps.from_config(config)
    stamp = occurred_at or datetime.now(timezone.utc).isoformat()
    terms = RewardTerms(
        goal=breakdown.goal,
        process=breakdown.process,
        satisfaction=breakdown.satisfaction,
    )

    units = []
    last = len(material) - 1
    for index, round_record in enumerate(material):
        record = _round_mapping(round_record)
        try:
            round_index = int(record.get("round_index", 0) or 0)
        except (TypeError, ValueError):
            round_index = 0
        tool_names = tool_names_from_round_dir(
            _ledger_dir(config) / "rounds" / f"round_{round_index:03d}"
        )
        unit = trace_unit_from_round(
            record,
            run_id=run_id,
            task_text=task_text,
            reward_terms=terms,
            alpha=alpha(record),
            domain_tags=_domain_tags(repo, branch, record.get("next_step")),
            tool_names=tool_names,
            # A run-level provider abort signs the round where the run died;
            # mid-run rounds keep their own (or no) signature.
            error_signature=error_signature(
                record, abort_reason=abort_reason if index == last else ""
            ),
            roles=roles,
            workspace=workspace,
            repo=repo,
            branch=branch,
            occurred_at=stamp,
            caps=caps,
        )
        units.append(unit)

    if breakdown.value is not None and units:
        apply_backfill(units, breakdown.value, gamma)

    records: list[dict[str, Any]] = []
    for unit in units:
        payload = redact_trace(unit).to_dict()
        if breakdown.reason:
            payload["reward_reason"] = breakdown.reason
        records.append(payload)

    stats = append_trace_records(experience_ledger_path(_ledger_dir(config)), records)
    return CaptureResult(
        run_id=run_id,
        path=stats.path,
        rounds_captured=len(units),
        written=stats.written,
        skipped_duplicates=stats.skipped_duplicates,
        dropped=stats.dropped,
        file_capped=stats.file_capped,
        terminal_reward=breakdown.value,
        reward_reason=breakdown.reason,
    )


def _ledger_dir(config: Any) -> Path:
    return Path(getattr(config, "log_dir", ".")).expanduser().resolve(strict=False) / (
        "role_orchestration"
    )


def _resolve_run_id(owner: Any, run_root: Path) -> str:
    if isinstance(owner, Mapping):
        run_id = str(owner.get("run_id") or "").strip()
        if run_id:
            return run_id[:_MAX_RUN_ID_CHARS]
    return run_root.name[:_MAX_RUN_ID_CHARS] or "run"


def _operator_messages(commands: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Map control-bus commands to the reward module's message shape.

    The reward's web-cancel attribution needs the command's ``kind`` and
    ``created_by``; the satisfaction nudge reads the instruction text itself.
    The text is bounded here and redacted with the trace later.
    """
    messages: list[dict[str, Any]] = []
    for command in commands or ():
        if not isinstance(command, Mapping):
            continue
        kind = str(command.get("kind") or "").strip()
        if not kind:
            continue
        payload = command.get("payload") if isinstance(command.get("payload"), Mapping) else {}
        text = str(payload.get("instructions") or "")[:50_000]
        messages.append(
            {
                "kind": kind,
                "created_by": str(command.get("created_by") or ""),
                "text": text,
            }
        )
    return messages


def _domain_tags(repo: str, branch: str, next_step: Any) -> tuple[str, ...]:
    """Compact joinable labels derived from the same read-only tag sources."""
    tags: list[str] = []
    if repo:
        tags.append(f"repo:{repo}")
    if branch:
        tags.append(f"branch:{branch}")
    step = next_step_tag(next_step)
    if step:
        tags.append(f"next_step:{step}")
    return tuple(tags)


def _round_mapping(record: Any) -> Mapping[str, Any]:
    if isinstance(record, Mapping):
        return record
    fields = getattr(record, "__dataclass_fields__", None)
    if fields:
        return {name: getattr(record, name) for name in fields}
    return {}


def _read_json_tolerant(path: Path, *, max_bytes: int) -> dict[str, Any] | None:
    text = _read_text_nofollow(path, max_bytes)
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_jsonl_tolerant(path: Path, *, max_bytes: int) -> list[dict[str, Any]]:
    """All parseable records of a JSONL ledger; the tail may be truncated."""
    text = _read_text_nofollow(path, max_bytes)
    if not text:
        return []
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            # A writer cut mid-line leaves a truncated tail; skip it.
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _read_text_nofollow(path: Path, max_bytes: int) -> str:
    """Bounded read through the anchored no-follow open; missing → empty."""
    fd: int | None = None
    try:
        fd = _open_nofollow(path)
        data = bytearray()
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            data.extend(chunk)
            remaining -= len(chunk)
        return bytes(data[:max_bytes]).decode("utf-8", errors="replace")
    except OSError:
        return ""
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
