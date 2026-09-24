"""Service-side task queue for fleet-triggered long-horizon runs.

Queue entries live as atomic JSON files under the configured runs root at
``queue/<queue_id>.json`` so they survive API restarts and remain visible to
any client with the bearer token.  The launcher (slice 2) evaluates capacity
from config and promotes the highest-priority eligible entry through the
same code path as ``POST /api/runs``.
"""

from __future__ import annotations

import json
import os
import re
import socket
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

# Body keys accepted by _normalize_request (task 233).  Anything else is
# rejected with the offending key names in the message instead of being
# dropped silently.  ``task_file`` and ``roles`` are alternative input keys
# for ``task`` and ``trio`` respectively.
KNOWN_REQUEST_KEYS = frozenset(
    {
        "name",
        "task",
        "task_file",
        "workspace",
        "max_rounds",
        "trio",
        "roles",
        "priority",
        "branch",
        "continue_branch",
        "base_check",
        "requested_by",
        "dedup_key",
    }
)


class UnknownQueueFieldError(ValueError):
    """An enqueue body carried keys outside ``KNOWN_REQUEST_KEYS``.

    Subclasses ``ValueError`` so every existing caller (both stores, the
    requeue path, the MCP tool) treats it as a validation failure; the REST
    route (server.py ``create_queue_entry``) re-raises it as HTTP 400 with
    the key names in the message.
    """

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


def _validate_trio(value: Any) -> str | None:
    if value is None:
        # Optional (task 233): the launcher resolves the trio itself and skips
        # an entry whose trio is not a configured trios key (launcher.py
        # _launch/_shadow_launch_decision "unknown trio ..."), so persisting
        # unset is safe -- no default is invented here.
        return ""
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("trio/roles must be a string")
    text = value.strip().lower()
    if not text:
        # An explicitly empty trio is the same "not supplied" state as an
        # omitted key (MCP clients may fill a declared default of "").
        return ""
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
    """Convert a POST /api/queue body into validated launch parameters.

    Unknown body keys are rejected (task 233) with an error naming every
    offending key -- they were previously dropped silently, which filed
    continuation tasks as fresh ones when ``continue_branch``/``branch`` went
    missing. ``KNOWN_REQUEST_KEYS`` is also the allowlist behind the MCP tool
    schema and the REST 400 mapping (server.py ``create_queue_entry``).
    """

    unknown = sorted(set(body) - KNOWN_REQUEST_KEYS)
    if unknown:
        raise UnknownQueueFieldError(
            "unknown field(s): " + ", ".join(unknown)
        )

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
    observe = queue.get("observe", False) if isinstance(queue, dict) else False
    if not isinstance(observe, bool):
        observe = False
    occupancy_ignore_dirty = (
        queue.get("occupancy_ignore_dirty", False) if isinstance(queue, dict) else False
    )
    if not isinstance(occupancy_ignore_dirty, bool):
        occupancy_ignore_dirty = False
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
            "auditor_mcp_profile": str(spec.get("auditor_mcp_profile", "")).strip() or None,
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
    return {
        "trios": normalized_trios,
        "capacity": normalized_capacity,
        "observe": observe,
        "occupancy_ignore_dirty": occupancy_ignore_dirty,
    }


def default_queue_config() -> dict[str, Any]:
    return queue_config_from_config({})


# ----------------------------------------------------------------------
# Cross-process launcher lease — task 173, scope 5 / migration doc §4.2
# ("single-orchestrator guarantee" at the file-store level).
#
# ``runs_root/queue/.lease`` holds one JSON object ``{pid, host, ts}`` naming
# the single launcher process that may run passes against this runs root.  It
# is created with O_EXCL (exactly one process can mint it), refreshed with a
# fresh ``ts`` on every pass the holder runs, and considered stale after
# ``LEASE_STALE_INTERVALS`` missed intervals (interval = the launcher's poll
# interval) so a crashed holder's lease is reclaimable.  A second launcher
# that finds a live lease it does not own logs and idles for that pass.
#
# Residual (documented, accepted): this is the file-store floor.  The
# unlink-and-O_EXCL reclaim has a tiny race window, and there is no fencing
# token on ``mark_launched``; the store's pending-guard and the eligibility
# gate still bound the damage of a lost race.  The Postgres backend mirrors
# the guarantee with a row lock instead (task 134).
# ----------------------------------------------------------------------

_LEASE_FILE = ".lease"
_LEASE_STALE_INTERVALS = 3


def _lease_path(runs_root: str | Path) -> Path:
    """Return ``runs_root/queue/.lease`` for a runs root."""

    path = Path(runs_root).expanduser().resolve() / _QUEUE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path / _LEASE_FILE


def _lease_stale_after(interval_seconds: float) -> float:
    """Seconds after which an unrefreshed lease is stale (3 missed intervals)."""

    return max(float(interval_seconds), 0.0) * _LEASE_STALE_INTERVALS


def read_lease(runs_root: str | Path) -> dict[str, Any] | None:
    """Read the current lease record, or None when absent/unreadable."""

    path = Path(runs_root).expanduser().resolve() / _QUEUE_DIR / _LEASE_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _lease_record(pid: int, host: str, ts: float) -> dict[str, Any]:
    return {"pid": int(pid), "host": str(host), "ts": float(ts)}


def _lease_held_by(record: dict[str, Any], pid: int, host: str) -> bool:
    try:
        return int(record.get("pid", -1)) == int(pid) and str(record.get("host", "")) == host
    except (TypeError, ValueError):
        return False


def _lease_stale(record: dict[str, Any], now: float, interval_seconds: float) -> bool:
    """True when ``record`` has not been refreshed for 3 missed intervals.

    An unparseable/missing ``ts`` reads as maximally stale: a lease whose
    timestamp cannot be trusted cannot defend its holder.
    """

    try:
        ts = float(record.get("ts"))
    except (TypeError, ValueError):
        return True
    return (now - ts) >= _lease_stale_after(interval_seconds)


def _create_lease_excl(path: Path, record: dict[str, Any]) -> None:
    """Create the lease file with O_EXCL so exactly one contender wins."""

    payload = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def acquire_lease(
    runs_root: str | Path,
    *,
    interval_seconds: float = 15.0,
    pid: int | None = None,
    host: str | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Take or refresh the launcher lease for ``runs_root``.

    Returns the caller's lease record when this process may run the pass
    (freshly minted, refreshed because it already owned the lease, or
    reclaimed from a stale holder), and ``None`` when a live lease owned by
    another process blocks it — the caller must log and idle.

    Refreshes rewrite the record atomically (``_atomic_bytes_write``); only a
    first take or a stale reclaim goes through O_EXCL, so exactly one
    contender can mint a lease out of nothing.
    """

    path = _lease_path(runs_root)
    effective_pid = os.getpid() if pid is None else int(pid)
    effective_host = socket.gethostname() if host is None else str(host)
    now_ts = time.time() if now is None else float(now)
    record = _lease_record(effective_pid, effective_host, now_ts)

    existing = read_lease(runs_root)
    if existing is not None and not _lease_stale(existing, now_ts, interval_seconds):
        if _lease_held_by(existing, effective_pid, effective_host):
            # Ours: refresh the timestamp for this pass.
            _atomic_bytes_write(path, json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            return record
        return None

    if existing is None:
        # No readable lease: mint one with O_EXCL.  Losing the race to a
        # simultaneous contender is re-read and re-evaluated once.
        try:
            _create_lease_excl(path, record)
            return record
        except FileExistsError:
            existing = read_lease(runs_root)
            if existing is not None and not _lease_stale(existing, now_ts, interval_seconds):
                return None

    # Stale (or unreadable-timestamp) lease: reclaim it.  Only unlink when the
    # record on disk is still stale at the moment of the unlink, so a holder
    # that managed a refresh in between keeps its lease; the O_EXCL re-create
    # serializes two simultaneous reclaimers.
    current = read_lease(runs_root)
    if current is not None and not _lease_stale(current, now_ts, interval_seconds):
        return None
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # Un-unlinkable (permissions, read-only root): refuse rather than
        # assume exclusivity we could not establish.
        return None
    try:
        _create_lease_excl(path, record)
        return record
    except FileExistsError:
        return None
    except OSError:
        return None


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
        return (from_status, to_status) in _QUEUE_TRANSITIONS

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

    def record_block(self, queue_id: str) -> QueueEntry | None:
        """Transition an entry from pending to blocked.

        Args:
            queue_id: The ID of the entry to block

        Returns:
            The updated QueueEntry, or None if the entry not found
            or if the transition is invalid
        """
        entry = self.get(queue_id)
        if entry is None:
            return None
        if not self._is_valid_transition(entry.status, "blocked"):
            return None
        entry.status = "blocked"
        return self.update(entry)

    def record_unblock(self, queue_id: str) -> QueueEntry | None:
        """Transition an entry from blocked to pending.

        Args:
            queue_id: The ID of the entry to unblock

        Returns:
            The updated QueueEntry, or None if the entry not found
            or if the transition is invalid
        """
        entry = self.get(queue_id)
        if entry is None:
            return None
        if not self._is_valid_transition(entry.status, "pending"):
            return None
        entry.status = "pending"
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

        # Create successor entry. The continuation opt-ins (branch /
        # continue_branch) carry over (task 233): a retried continuation
        # task stays a continuation task, not a fresh one.
        successor = QueueEntry(
            queue_id=f"q-{uuid.uuid4().hex[:16]}",
            name=entry.name,
            task=entry.task,
            workspace=entry.workspace,
            max_rounds=entry.max_rounds,
            trio=entry.trio,
            priority=entry.priority,
            requested_by=entry.requested_by,
            branch=entry.branch,
            continue_branch=entry.continue_branch,
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

    # ------------------------------------------------------------------
    # Shadow (observe) log — task 173.
    #
    # One JSON line per shadow decision, appended to
    # ``runs_root/queue/shadow.jsonl``, rotated daily: once the current day
    # rolls over, the previous day's full file is renamed to
    # ``shadow-<YYYYMMDD>.jsonl`` (UTC, the log is a fleet-wide evidence
    # stream, not a local one) and a fresh ``shadow.jsonl`` starts. The
    # rename happens on append by whichever launcher process observes the
    # day change; the only failure mode is an extra record on the old file,
    # never data loss.
    # ------------------------------------------------------------------

    _SHADOW_LOG = "shadow.jsonl"

    def _rotate_shadow_log(self, today: str) -> None:
        """Rename yesterday's ``shadow.jsonl`` to ``shadow-<day>.jsonl``."""

        current = self._root / self._SHADOW_LOG
        if not current.exists():
            return
        stamp = time.strftime("%Y%m%d", time.gmtime(os.path.getmtime(current)))
        if stamp == today:
            return
        rotated = self._root / f"shadow-{stamp}.jsonl"
        if rotated.exists():
            # A rotated file for that day already exists: append instead of
            # clobbering an evidence stream.
            with current.open("rb") as src, rotated.open("ab") as dst:
                dst.write(src.read())
            current.unlink()
            return
        os.replace(current, rotated)

    def append_shadow_record(self, record: dict[str, Any]) -> None:
        """Append one shadow decision as a single JSON line, rotating daily."""

        today = time.strftime("%Y%m%d", time.gmtime())
        try:
            self._rotate_shadow_log(today)
            line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            path = self._root / self._SHADOW_LOG
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
        except OSError:
            pass

    def read_shadow_records(self, since: float | None = None) -> list[dict[str, Any]]:
        """Return shadow records with ``ts >= since`` (today's + rotated files).

        Rotated files are named ``shadow-<YYYYMMDD>.jsonl`` and are ordered
        oldest-first by name; today's ``shadow.jsonl`` is last.
        """

        records: list[dict[str, Any]] = []
        candidates = sorted(self._root.glob("shadow-*.jsonl")) + [self._root / self._SHADOW_LOG]
        for path in candidates:
            if not path.is_file():
                continue
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        if not isinstance(record, dict):
                            continue
                        if since is not None and float(record.get("ts", 0.0)) < since:
                            continue
                        records.append(record)
            except OSError:
                continue
        return records
