"""Service-side task queue for fleet-triggered long-horizon runs.

Queue entries live as atomic JSON files under the configured runs root at
``queue/<queue_id>.json`` so they survive API restarts and remain visible to
any client with the bearer token.  The launcher (slice 2) evaluates capacity
from config and promotes the highest-priority eligible entry through the
same code path as ``POST /api/runs``.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .supervisor.control_bus import _atomic_bytes_write
from .types import DEFAULT_MAX_ROUNDS, MAX_ROUNDS

try:  # Imported lazily so PgQueueStore is optional for the file store.
    from .pg_queue import PgQueueStore  # noqa: F401
except Exception:  # pragma: no cover - driver or schema missing
    PgQueueStore = None  # type: ignore[assignment]

_QUEUE_DIR = "queue"
_MAX_QUEUE_ID_CHARS = 128
_MAX_QUEUE_TASK_CHARS = 100_000
_MAX_QUEUE_REASON_CHARS = 4_000
_MAX_QUEUE_DEDUP_CHARS = 256
# ``blocked`` is the PC queue's fifth state: an entry is parked while it waits on
# something outside the launcher (a missing dependency, a gate, a resource). It
# is a non-terminal state -- a blocked entry is still parked work, so its dedup
# key stays "in use" and a fresh enqueue with the same key does not fork a second
# entry. It resumes through ``blocked`` -> ``pending`` (record_unblock) or
# ``blocked`` -> ``launched`` (mark_launched).
_VALID_STATUS = frozenset({"pending", "launched", "done", "failed", "blocked"})
# Non-terminal entries are still waiting to be, or being, launched, or parked. A
# dedup key is considered "in use" only while its entry is in one of these states;
# once an entry reaches a terminal state (``done``/``failed``) the key is free for
# a fresh (retry) entry.
_NON_TERMINAL_STATUS = frozenset({"pending", "launched", "blocked"})
_VALID_TRIOS = frozenset({"kimi", "qwen"})

# Allowed queue-entry state transitions. Keyed by (from, to); the value is the
# operation that performs the move. Read-only edges ("update (record_...)") are
# not a first-class QueueStore method: they are applied by the launcher by
# mutating the entry's status and calling ``update``. The terminal edges (done,
# failed) have no outgoing edge -- a terminal entry cannot leave its state.
_QUEUE_TRANSITIONS: dict[tuple[str, str], str] = {
    ("pending", "launched"): "mark_launched",
    ("pending", "done"): "mark_done",
    ("pending", "failed"): "mark_failed",
    ("pending", "blocked"): "update (record_block)",
    ("launched", "done"): "mark_done",
    ("launched", "failed"): "mark_failed",
    ("blocked", "pending"): "update (record_unblock)",
    ("blocked", "launched"): "mark_launched",
}


def _valid_transition(from_status: str, to_status: str) -> bool:
    """Return True if ``from_status`` -> ``to_status`` is an allowed transition."""
    return (from_status, to_status) in _QUEUE_TRANSITIONS


def _safe_queue_id(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if len(value) > _MAX_QUEUE_ID_CHARS:
        return False
    if value in {".", ".."} or "/" in value or "\\" in value:
        return False
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return False
    return True


def _queue_dir(runs_root: str | Path) -> Path:
    path = Path(runs_root).expanduser().resolve() / _QUEUE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _select_queue_store(
    runs_root: str | Path | None, project: dict[str, Any] | None
) -> QueueStore | PgQueueStore | None:
    """Pick the queue store backend.

    The file store is the default and stays hermetic. Only ``queue_backend="postgres"``
    plus a ``database_url`` in the project config reaches ``PgQueueStore``; every
    other configuration (including no config at all) yields the file-backed store.
    """
    if runs_root is None:
        return None
    if project is None:
        return QueueStore(runs_root)
    queue = project.get("queue", {})
    if not isinstance(queue, dict):
        return QueueStore(runs_root)
    backend = str(queue.get("backend", "file")).strip().lower()
    if backend not in {"file", "postgres"}:
        raise ValueError(f"unknown queue backend: {backend!r}")
    if backend != "postgres":
        return QueueStore(runs_root)
    database_url = str(queue.get("database_url", "")).strip()
    if not database_url:
        raise ValueError("queue_backend=postgres requires queue.database_url")
    return PgQueueStore(database_url)


def _queue_path(runs_root: str | Path, queue_id: str) -> Path:
    return _queue_dir(runs_root) / f"{queue_id}.json"


@dataclass
class QueueEntry:
    """One durable task waiting to be launched by the service."""

    queue_id: str
    name: str
    task: str
    workspace: str
    max_rounds: int
    trio: str
    priority: int
    requested_by: str
    # Workspace-guard continuation opt-in (task 201).  ``branch`` names a
    # specific branch to use as-is; ``continue_branch`` accepts whatever branch
    # the workspace currently has checked out.  When either is set the
    # launcher's workspace guard leaves the workspace untouched (mode
    # "continuation"): no relocation, no stash, and no open-PR refusal.  The
    # default (both unset) keeps the guard's full protection.
    branch: str = ""
    continue_branch: bool = False
    base_check: str = ""
    status: str = "pending"
    run_id: str | None = None
    reason: str | None = None
    skip_reasons: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    launched_at: float | None = None
    last_checked_at: float | None = None
    dedup_key: str | None = None
    # Retry/requeue fields
    retry_of: str | None = None
    attempt: int = 1
    failure_cause: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at
        data["updated_at"] = self.updated_at
        data["launched_at"] = self.launched_at
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueueEntry:
        kwargs: dict[str, Any] = {}
        for key in (
            "queue_id",
            "name",
            "task",
            "workspace",
            "max_rounds",
            "trio",
            "priority",
            "requested_by",
            "branch",
            "continue_branch",
            "base_check",
            "status",
            "run_id",
            "reason",
            "skip_reasons",
            "created_at",
            "updated_at",
            "launched_at",
            "last_checked_at",
            "dedup_key",
            "retry_of",
            "attempt",
            "failure_cause",
        ):
            if key in data:
                kwargs[key] = data[key]
        return cls(**kwargs)


def _now() -> float:
    return time.time()


def _validate_task(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("task must be a string")
    text = value.strip()
    if not text:
        raise ValueError("task is required")
    if len(text) > _MAX_QUEUE_TASK_CHARS:
        raise ValueError("task is too large")
    if "\x00" in text:
        raise ValueError("task contains a NUL byte")
    return text


def _validate_name(value: Any) -> str:
    if value is None:
        raise ValueError("name is required")
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("name must be a string")
    text = value.strip()
    if not text:
        raise ValueError("name is required")
    if len(text) > 256:
        raise ValueError("name is too long")
    if "\x00" in text:
        raise ValueError("name contains a NUL byte")
    return text


def _validate_workspace(value: Any) -> str:
    if value is None:
        raise ValueError("workspace is required")
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("workspace must be a string")
    text = value.strip()
    if not text:
        raise ValueError("workspace is required")
    if len(text) > 4096:
        raise ValueError("workspace is too long")
    if "\x00" in text:
        raise ValueError("workspace contains a NUL byte")
    return text


def _validate_trio(value: Any) -> str:
    if value is None:
        raise ValueError("trio/roles is required")
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("trio/roles must be a string")
    text = value.strip().lower()
    if text not in _VALID_TRIOS:
        raise ValueError(f"trio must be one of: {', '.join(sorted(_VALID_TRIOS))}")
    return text


def _validate_max_rounds(value: Any) -> int:
    if value is None:
        return DEFAULT_MAX_ROUNDS
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max_rounds must be an integer")
    if not 1 <= value <= MAX_ROUNDS:
        raise ValueError(f"max_rounds must be from 1 to {MAX_ROUNDS}")
    return value


def _validate_priority(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("priority must be an integer")
    return value


def _validate_requested_by(value: Any) -> str:
    if value is None:
        raise ValueError("requested_by is required")
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("requested_by must be a string")
    text = value.strip()
    if not text:
        raise ValueError("requested_by is required")
    if len(text) > 256:
        raise ValueError("requested_by is too long")
    return text


def _validate_base_check(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("base_check must be a string")
    return value.strip()


def _validate_dedup_key(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("dedup_key must be a string")
    text = value.strip()
    if not text:
        return None
    if len(text) > _MAX_QUEUE_DEDUP_CHARS:
        raise ValueError("dedup_key is too long")
    if "\x00" in text:
        raise ValueError("dedup_key contains a NUL byte")
    return text


def _validate_branch(value: Any) -> str:
    """Validate the continuation opt-in ``branch`` name (task 201)."""

    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("branch must be a string")
    text = value.strip()
    if not text:
        return ""
    if len(text) > 256:
        raise ValueError("branch is too long")
    if "\x00" in text or ".." in text:
        raise ValueError("branch contains an invalid sequence")
    if text.startswith("-"):
        raise ValueError("branch must not start with '-'")
    return text


def _validate_continue_branch(value: Any) -> bool:
    """Validate the continuation opt-in ``continue_branch`` flag (task 201)."""

    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError("continue_branch must be a boolean")
    return value


def _normalize_request(body: dict[str, Any]) -> dict[str, Any]:
    """Convert a POST /api/queue body into validated launch parameters."""

    task = body.get("task")
    task_file = body.get("task_file")
    if task is not None and task_file is not None:
        raise ValueError("supply task or task_file, not both")
    if task_file is not None:
        path = Path(str(task_file))
        if not path.is_file():
            raise ValueError("task_file does not exist")
        task = path.read_text(encoding="utf-8")
    task_text = _validate_task(task)

    trio = body.get("roles") if "roles" in body else body.get("trio")
    branch = _validate_branch(body.get("branch"))
    continue_branch = _validate_continue_branch(body.get("continue_branch"))
    if branch and continue_branch:
        # Both opt-ins name the same decision ("use the existing branch as-is")
        # at different specificity; accepting both would leave the launcher's
        # behaviour ambiguous, so the enqueue is rejected instead.
        raise ValueError("set branch or continue_branch, not both")
    return {
        "name": _validate_name(body.get("name")),
        "task": task_text,
        "workspace": _validate_workspace(body.get("workspace")),
        "max_rounds": _validate_max_rounds(body.get("max_rounds")),
        "trio": _validate_trio(trio),
        "priority": _validate_priority(body.get("priority")),
        "branch": branch,
        "continue_branch": continue_branch,
        "base_check": _validate_base_check(body.get("base_check")),
        "requested_by": _validate_requested_by(body.get("requested_by")),
        "dedup_key": _validate_dedup_key(body.get("dedup_key")),
    }


def queue_config_from_config(config: dict[str, Any]) -> dict[str, Any]:
    """Extract queue sections from a loaded project config."""

    queue = config.get("queue", {}) if isinstance(config, dict) else {}
    trios = queue.get("trios", {}) if isinstance(queue, dict) else {}
    capacity = queue.get("capacity", {}) if isinstance(queue, dict) else {}
    if not isinstance(trios, dict):
        trios = {}
    if not isinstance(capacity, dict):
        capacity = {}
    normalized_trios: dict[str, dict[str, Any]] = {}
    for name, spec in trios.items():
        if not isinstance(spec, dict):
            continue
        agent = str(spec.get("agent", "")).strip()
        model = str(spec.get("model", "")).strip() or None
        mcp_profile = str(spec.get("mcp_profile", "")).strip() or None
        if not agent:
            continue
        normalized_trios[name] = {
            "agent": agent,
            "model": model,
            "mcp_profile": mcp_profile,
        }
    for required in _VALID_TRIOS:
        if required not in normalized_trios:
            # Keep a safe fallback so the API can still report config shape.
            normalized_trios[required] = {
                "agent": "claude_code" if required == "kimi" else "codex",
                "model": None,
                "mcp_profile": None,
            }
    normalized_capacity = {
        "kimi_max": 3,
        "qwen_max": 1,
        "min_healthy_keys": 2,
        "key_health_url": "",
        "poll_seconds": 15,
        "max_retries": 2,
    }
    if isinstance(capacity.get("kimi_max"), int):
        normalized_capacity["kimi_max"] = max(0, capacity["kimi_max"])
    if isinstance(capacity.get("qwen_max"), int):
        normalized_capacity["qwen_max"] = max(0, capacity["qwen_max"])
    if isinstance(capacity.get("min_healthy_keys"), int):
        normalized_capacity["min_healthy_keys"] = max(0, capacity["min_healthy_keys"])
    if isinstance(capacity.get("key_health_url"), str):
        normalized_capacity["key_health_url"] = capacity["key_health_url"]
    if isinstance(capacity.get("poll_seconds"), (int, float)):
        normalized_capacity["poll_seconds"] = max(1.0, float(capacity["poll_seconds"]))
    if isinstance(capacity.get("max_retries"), int):
        # `requeue` reads this so a configured cap actually bounds retries;
        # dropping it here would silently reset every deployment to the default.
        normalized_capacity["max_retries"] = max(0, capacity["max_retries"])
    return {"trios": normalized_trios, "capacity": normalized_capacity}


def default_queue_config() -> dict[str, Any]:
    return queue_config_from_config({})


class QueueStore:
    """Atomic file-backed store for queue entries below a runs root."""

    def __init__(self, runs_root: str | Path, config: dict[str, Any] | None = None) -> None:
        self.runs_root = Path(runs_root).expanduser().resolve()
        self._root = _queue_dir(self.runs_root)
        self._config = config

    def _path(self, queue_id: str) -> Path:
        if not _safe_queue_id(queue_id):
            raise ValueError("invalid queue id")
        return self._root / f"{queue_id}.json"

    def _write(self, entry: QueueEntry) -> None:
        payload = json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
        _atomic_bytes_write(self._path(entry.queue_id), payload.encode("utf-8"))

    def create(self, body: dict[str, Any]) -> QueueEntry:
        """Create a queue entry, de-duplicating by ``dedup_key`` when supplied.

        Idempotent-enqueue semantics: when ``dedup_key`` is a non-empty string
        and a non-terminal entry (``pending`` or ``launched``) with the same key
        already exists, that existing entry is returned unchanged instead of
        creating a second one. Two orchestrators (or a retrying client) asking
        for the same work therefore resolve to a single entry, and the
        launcher's ``mark_launched`` pending-guard still ensures that entry is
        launched at most once. A key is freed once its entry reaches a terminal
        state (``done``/``failed``), so reusing the key afterwards creates a
        fresh entry -- a retry. Enqueues without a key behave exactly as before,
        each minting a unique ``q-<hex>`` id.

        Residual: the lookup is a read-then-write over the entry directory. It
        removes duplicate enqueues from sequential or retrying callers; a
        simultaneous cross-process race where two ``create`` calls both miss the
        lookup before either writes is a narrow window. The full
        single-orchestrator guarantee against that window is the lease proposed
        as new work in the migration plan (section 4), not implemented here.
        """
        params = _normalize_request(body)
        dedup_key = params.get("dedup_key")
        if dedup_key:
            existing = self._find_non_terminal_by_dedup(dedup_key)
            if existing is not None:
                return existing
        queue_id = f"q-{uuid.uuid4().hex[:16]}"
        now = _now()
        entry = QueueEntry(queue_id=queue_id, created_at=now, updated_at=now, **params)
        self._write(entry)
        return entry

    def _find_non_terminal_by_dedup(self, dedup_key: str) -> QueueEntry | None:
        """Return the non-terminal entry currently holding ``dedup_key``, if any."""
        for entry in self.list():
            if entry.dedup_key == dedup_key and entry.status in _NON_TERMINAL_STATUS:
                return entry
        return None

    def list(self) -> list[QueueEntry]:
        entries: list[QueueEntry] = []
        try:
            paths = list(self._root.iterdir())
        except OSError:
            return entries
        for path in paths:
            if not path.is_file() or path.suffix != ".json":
                continue
            entry = self._read_path(path)
            if entry is not None:
                entries.append(entry)
        entries.sort(key=lambda item: (-item.priority, item.created_at))
        return entries

    def get(self, queue_id: str) -> QueueEntry | None:
        path = self._path(queue_id)
        return self._read_path(path)

    def _read_path(self, path: Path) -> QueueEntry | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        try:
            entry = QueueEntry.from_dict(data)
        except (TypeError, ValueError):
            return None
        if entry.status not in _VALID_STATUS:
            # Log unknown status instead of dropping
            import logging
            logging.getLogger(__name__).warning(
                f"Ignoring queue entry {path.name} with unknown status: {entry.status}"
            )
            return None
        return entry

    def _is_valid_transition(self, from_status: str, to_status: str) -> bool:
        """Check if a status transition is valid according to the transition table."""
        return to_status in _VALID_TRANSITIONS.get(from_status, set())

    def update(self, entry: QueueEntry) -> QueueEntry:
        entry.updated_at = _now()
        self._write(entry)
        return entry

    def delete(self, queue_id: str) -> QueueEntry | None:
        entry = self.get(queue_id)
        if entry is None:
            return None
        try:
            self._path(queue_id).unlink()
        except OSError:
            return None
        return entry

    def set_priority(self, queue_id: str, priority: int) -> QueueEntry | None:
        entry = self.get(queue_id)
        if entry is None:
            return None
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError("priority must be an integer")
        entry.priority = priority
        return self.update(entry)

    def mark_launched(self, queue_id: str, run_id: str) -> QueueEntry | None:
        entry = self.get(queue_id)
        if entry is None:
            return None
        if entry.status != "pending":
            raise ValueError("entry is not pending")
        entry.status = "launched"
        entry.run_id = run_id
        entry.launched_at = _now()
        return self.update(entry)

    def mark_done(self, queue_id: str, *, reason: str | None = None) -> QueueEntry | None:
        entry = self.get(queue_id)
        if entry is None:
            return None
        if not self._is_valid_transition(entry.status, "done"):
            raise ValueError(f"invalid transition from {entry.status} to done")
        entry.status = "done"
        if reason is not None:
            entry.reason = reason[:_MAX_QUEUE_REASON_CHARS]
        return self.update(entry)

    def mark_failed(self, queue_id: str, reason: str) -> QueueEntry | None:
        entry = self.get(queue_id)
        if entry is None:
            return None
        if not self._is_valid_transition(entry.status, "failed"):
            raise ValueError(f"invalid transition from {entry.status} to failed")
        entry.status = "failed"
        entry.reason = reason[:_MAX_QUEUE_REASON_CHARS]
        return self.update(entry)

    def record_skip(self, queue_id: str, reason: str) -> QueueEntry | None:
        entry = self.get(queue_id)
        if entry is None:
            return None
        # Allow recording skips on pending and failed entries
        if entry.status not in ("pending", "failed"):
            return None
        entry.skip_reasons.append(str(reason)[:_MAX_QUEUE_REASON_CHARS])
        return self.update(entry)

    def requeue(self, queue_id: str, cause: str) -> QueueEntry | None:
        """Create a successor pending entry for a failed entry.

        Args:
            queue_id: The ID of the failed entry to retry
            cause: The failure cause that triggered the retry

        Returns:
            The new successor QueueEntry, or None if the original entry not found

        Raises:
            ValueError: If the original entry is not failed, or if attempt would exceed max_retries
        """
        entry = self.get(queue_id)
        if entry is None:
            return None

        # Load config to get max_retries. `queue_config_from_config` (and
        # `_flatten_queue_table` in config.py) both normalize [queue.capacity]
        # into a dict that always carries `max_retries`, so the default only
        # applies when the store was built with no config at all.
        max_retries = 2  # default
        capacity = self._config.get("capacity") if isinstance(self._config, dict) else None
        if isinstance(capacity, dict):
            max_retries = int(capacity.get("max_retries", 2))

        # An entry's own attempt counts toward the cap: original=attempt 1,
        # first retry=attempt 2, ... so the highest allowed attempt is
        # max_retries + 1.  Refuse when the successor would exceed that cap.
        # Checked before the status guard so a retry loop that keeps asking
        # after exhaustion learns it hit the cap, not just that the successor
        # is (still) pending.
        if entry.attempt > max_retries:
            raise ValueError(f"exceeded max_retries ({max_retries})")

        if entry.status != "failed":
            raise ValueError("can only requeue failed entries")

        # Create successor entry
        successor = QueueEntry(
            queue_id=f"q-{uuid.uuid4().hex[:16]}",
            name=entry.name,
            task=entry.task,
            workspace=entry.workspace,
            max_rounds=entry.max_rounds,
            trio=entry.trio,
            priority=entry.priority,
            requested_by=entry.requested_by,
            base_check=entry.base_check,
            status="pending",
            retry_of=entry.queue_id,
            attempt=entry.attempt + 1,
            failure_cause=cause,
            created_at=_now(),
            updated_at=_now(),
            dedup_key=None  # retries must not collide with original dedup_key
        )

        self._write(successor)
        return successor

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {status: 0 for status in _VALID_STATUS}
        for entry in self.list():
            counts[entry.status] = counts.get(entry.status, 0) + 1
        return counts
