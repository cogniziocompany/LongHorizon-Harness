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

from . import spec_stats as _spec_stats
from .supervisor.control_bus import _atomic_bytes_write
from .types import DEFAULT_MAX_ROUNDS, MAX_ROUNDS

_QUEUE_DIR = "queue"
_MAX_QUEUE_ID_CHARS = 128
_MAX_QUEUE_TASK_CHARS = 100_000
_MAX_QUEUE_REASON_CHARS = 4_000
_MAX_QUEUE_DEDUP_CHARS = 256
# ``spec_pending``: the entry has a spec file that is still a draft. The
# launcher never launches it; ``mark_spec_ready`` promotes it to ``pending``
# once the spec's frontmatter says ``status: ready-for-dev``.
_VALID_STATUS = frozenset({"spec_pending", "pending", "launched", "done", "failed"})
VALID_STATUSES: tuple[str, ...] = ("spec_pending", "pending", "launched", "done", "failed")
# Non-terminal entries are still waiting to be, or being, launched. A dedup key
# is considered "in use" only while its entry is in one of these states; once an
# entry reaches ``done``/``failed`` the key is free for a fresh (retry) entry.
_NON_TERMINAL_STATUS = frozenset({"spec_pending", "pending", "launched"})
_VALID_TRIOS = frozenset({"kimi", "qwen"})


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
    # Spec staging. ``spec_file`` is the BMAD-style spec the task is launched
    # from; its size is measured, never capped (see ``spec_stats``).
    spec_file: str | None = None
    spec_status: str | None = None
    spec_chars: int | None = None
    spec_tokens_est: int | None = None
    spec_exact: bool = False
    spec_measured_at: float | None = None
    spec_range_state: str | None = None

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
            "spec_file",
            "spec_status",
            "spec_chars",
            "spec_tokens_est",
            "spec_exact",
            "spec_measured_at",
            "spec_range_state",
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


def _validate_spec_file(value: Any) -> Path | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("spec_file must be a string")
    text = value.strip()
    if not text:
        return None
    if "\x00" in text:
        raise ValueError("spec_file contains a NUL byte")
    path = Path(text).expanduser()
    if not path.is_file():
        raise ValueError("spec_file does not exist")
    return path


def _normalize_request(body: dict[str, Any]) -> dict[str, Any]:
    """Convert a POST /api/queue body into validated launch parameters.

    ``spec_file`` adds the spec-staging stage: the entry is created as
    ``spec_pending`` (unless the spec is already ``ready-for-dev``), its size is
    measured, and ``task`` may be omitted because the spec body becomes the task
    at ``mark_spec_ready`` time. ``task``/``task_file`` alongside ``spec_file``
    is allowed and kept as the original prose until the spec replaces it.
    """

    task = body.get("task")
    task_file = body.get("task_file")
    if task is not None and task_file is not None:
        raise ValueError("supply task or task_file, not both")
    if task_file is not None:
        path = Path(str(task_file))
        if not path.is_file():
            raise ValueError("task_file does not exist")
        task = path.read_text(encoding="utf-8")

    spec_path = _validate_spec_file(body.get("spec_file"))
    spec_fields: dict[str, Any] = {}
    status = "pending"
    if spec_path is not None:
        spec_text = spec_path.read_text(encoding="utf-8")
        spec_status = _spec_stats.spec_status_from_text(spec_text) or _spec_stats.SPEC_STATUS_DRAFT
        measure = _spec_stats.measure_text(spec_text)
        _, spec_body = _spec_stats.parse_frontmatter(spec_text)
        if spec_status == _spec_stats.SPEC_STATUS_READY:
            task = spec_body
        elif task is None:
            # Placeholder until the spec is ready; never launched in this state.
            task = spec_body
            status = "spec_pending"
        else:
            status = "spec_pending"
        spec_fields = {
            "spec_file": str(spec_path),
            "spec_status": spec_status,
            "spec_chars": measure.chars,
            "spec_tokens_est": measure.tokens_est,
            "spec_exact": measure.exact,
            "spec_measured_at": measure.measured_at,
        }
    task_text = _validate_task(task)

    trio = body.get("roles") if "roles" in body else body.get("trio")
    return {
        "name": _validate_name(body.get("name")),
        "task": task_text,
        "workspace": _validate_workspace(body.get("workspace")),
        "max_rounds": _validate_max_rounds(body.get("max_rounds")),
        "trio": _validate_trio(trio),
        "priority": _validate_priority(body.get("priority")),
        "base_check": _validate_base_check(body.get("base_check")),
        "requested_by": _validate_requested_by(body.get("requested_by")),
        "dedup_key": _validate_dedup_key(body.get("dedup_key")),
        "status": status,
        **spec_fields,
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
    return {
        "trios": normalized_trios,
        "capacity": normalized_capacity,
        "spec_stats": _spec_stats.spec_stats_config_from_config(config),
    }


def default_queue_config() -> dict[str, Any]:
    return queue_config_from_config({})


class QueueStore:
    """Atomic file-backed store for queue entries below a runs root."""

    def __init__(self, runs_root: str | Path, *, spec_stats_config: dict[str, Any] | None = None) -> None:
        self.runs_root = Path(runs_root).expanduser().resolve()
        self._root = _queue_dir(self.runs_root)
        self.spec_stats_config = dict(spec_stats_config or _spec_stats.DEFAULT_SPEC_STATS_CONFIG)

    # -- spec staging -----------------------------------------------------

    @property
    def spec_stats_path(self) -> Path:
        return self._root / _spec_stats.SPEC_STATS_FILENAME

    def spec_stats(self) -> _spec_stats.SpecStats:
        return _spec_stats.load_stats(self.spec_stats_path)

    def record_spec_finished(self, key: str, tokens_est: int) -> _spec_stats.SpecStats:
        return _spec_stats.record_finished(
            self.spec_stats_path, key, tokens_est, config=self.spec_stats_config
        )

    def measure_spec(self, queue_id: str, *, force: bool = False) -> QueueEntry | None:
        """Re-measure an entry's spec when the file changed since last measure."""

        entry = self.get(queue_id)
        if entry is None or not entry.spec_file:
            return entry
        path = Path(entry.spec_file)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return entry
        if not force and entry.spec_measured_at is not None and mtime <= entry.spec_measured_at:
            return entry
        text = path.read_text(encoding="utf-8")
        measure = _spec_stats.measure_text(text)
        entry.spec_status = _spec_stats.spec_status_from_text(text) or _spec_stats.SPEC_STATUS_DRAFT
        entry.spec_chars = measure.chars
        entry.spec_tokens_est = measure.tokens_est
        entry.spec_exact = measure.exact
        entry.spec_measured_at = measure.measured_at
        entry.spec_range_state = _spec_stats.classify(measure.tokens_est, self.spec_stats())
        return self.update(entry)

    def read_spec(self, queue_id: str) -> dict[str, Any] | None:
        """Return the spec body, frontmatter, measurement, and current stats."""

        entry = self.measure_spec(queue_id)
        if entry is None:
            return None
        if not entry.spec_file:
            raise ValueError("entry has no spec_file")
        try:
            text = Path(entry.spec_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"spec_file unreadable: {exc}") from exc
        frontmatter, body = _spec_stats.parse_frontmatter(text)
        stats = self.spec_stats()
        return {
            "queue_id": entry.queue_id,
            "spec_file": entry.spec_file,
            "frontmatter": frontmatter,
            "body": body,
            "chars": entry.spec_chars,
            "tokens_est": entry.spec_tokens_est,
            "exact": entry.spec_exact,
            "measured_at": entry.spec_measured_at,
            "range_state": _spec_stats.classify(entry.spec_tokens_est, stats),
            "stats": stats.public(),
        }

    def mark_spec_ready(self, queue_id: str) -> QueueEntry | None:
        """Promote ``spec_pending`` -> ``pending`` once the spec is ready-for-dev.

        The spec body replaces ``task`` so the launcher path is unchanged from
        here on, and the spec's size is recorded into the expected-range
        distribution.
        """

        entry = self.get(queue_id)
        if entry is None:
            return None
        if entry.status not in {"spec_pending", "pending"}:
            raise ValueError(f"cannot mark spec ready for status {entry.status}")
        if not entry.spec_file:
            raise ValueError("entry has no spec_file")
        path = Path(entry.spec_file)
        if not path.is_file():
            raise ValueError("spec_file does not exist")
        text = path.read_text(encoding="utf-8")
        status = _spec_stats.spec_status_from_text(text)
        if status != _spec_stats.SPEC_STATUS_READY:
            raise ValueError(
                f"spec status is {status or 'missing'!r}; set frontmatter status: {_spec_stats.SPEC_STATUS_READY}"
            )
        _, body = _spec_stats.parse_frontmatter(text)
        entry.task = _validate_task(body)
        measure = _spec_stats.measure_text(text)
        entry.spec_status = status
        entry.spec_chars = measure.chars
        entry.spec_tokens_est = measure.tokens_est
        entry.spec_exact = measure.exact
        entry.spec_measured_at = measure.measured_at
        stats = self.record_spec_finished(entry.queue_id, measure.tokens_est)
        entry.spec_range_state = _spec_stats.classify(measure.tokens_est, stats)
        entry.status = "pending"
        return self.update(entry)

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
        if entry.spec_tokens_est is not None:
            entry.spec_range_state = _spec_stats.classify(entry.spec_tokens_est, self.spec_stats())
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
            return None
        return entry

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
        entry.status = "done"
        if reason is not None:
            entry.reason = reason[:_MAX_QUEUE_REASON_CHARS]
        return self.update(entry)

    def mark_failed(self, queue_id: str, reason: str) -> QueueEntry | None:
        entry = self.get(queue_id)
        if entry is None:
            return None
        entry.status = "failed"
        entry.reason = reason[:_MAX_QUEUE_REASON_CHARS]
        return self.update(entry)

    def record_skip(self, queue_id: str, reason: str) -> QueueEntry | None:
        entry = self.get(queue_id)
        if entry is None:
            return None
        if entry.status not in {"pending", "spec_pending"}:
            return None
        entry.skip_reasons.append(str(reason)[:_MAX_QUEUE_REASON_CHARS])
        return self.update(entry)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {status: 0 for status in _VALID_STATUS}
        for entry in self.list():
            counts[entry.status] = counts.get(entry.status, 0) + 1
        return counts
