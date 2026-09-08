"""Fleet reporter: push harness telemetry to fleet-admin over HMAC-signed HTTPS.

The reporter is a fail-open, side-car telemetry sink.  It is enabled only when
``LH_HARNESS_FLEET_URL`` is set.  All work is delegated to a single daemon
thread with a bounded queue so that network stalls or fleet-admin outages never
block a run.  Events are batched for 2 s, gzip-compressed, and POSTed with the
same HMAC-SHA256 + ``X-Fleet-Host`` + ``X-Fleet-Signature`` scheme used by the
device ``/checkin`` endpoint.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import logging
import os
import queue
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_ENV_URL = "LH_HARNESS_FLEET_URL"
_ENV_NODE = "LH_HARNESS_FLEET_NODE"
_ENV_KEY = "LH_HARNESS_FLEET_KEY"
_ENV_LABELS = "LH_HARNESS_FLEET_LABELS"

_BATCH_INTERVAL_SECONDS = 2.0
_HEARTBEAT_INTERVAL_SECONDS = 30.0
_MAX_QUEUE_SIZE = 10_000
_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = (0.0, 1.0, 2.0)
_WARN_ONCE_INTERVAL_SECONDS = 300.0
_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_TRAJECTORY_ROLES = (
    "manager",
    "executor",
    "auditor",
    "auditor_format_repair",
    "final_response",
)


@dataclass(frozen=True)
class EventEnvelope:
    """Public event shape sent to fleet-admin."""

    run_id: str
    type: str
    ts: float
    round: int | None = None
    role: str | None = None
    status: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_id": self.run_id,
            "type": self.type,
            "ts": self.ts,
            "payload": self.payload,
        }
        if self.round is not None:
            data["round"] = self.round
        if self.role is not None:
            data["role"] = self.role
        if self.status is not None:
            data["status"] = self.status
        if self.run_id and self.role:
            # Use the same session-id helper as the LLM/MCP path so fleet events
            # can be joined to Langfuse traces and run directories directly.
            from ..adapters.claude_code import episode_session_id

            round_tag = f"round_{self.round}" if self.round is not None else "round_unknown"
            data["session_id"] = episode_session_id(self.run_id, round_tag, self.role)
        return data


@dataclass
class _PendingItem:
    """Single work item for the reporter queue."""

    endpoint: str
    payload: Any
    gzip_body: bool = True


class FleetReporter:
    """Bounded-queue, daemon-thread reporter with HMAC-signed gzip POSTs.

    When ``LH_HARNESS_FLEET_URL`` is unset the reporter does nothing: creating
    an instance returns immediately and all public methods are no-ops.  This
    keeps the critical path byte-identical to a build without fleet reporting.
    """

    def __init__(
        self,
        *,
        fleet_url: str | None = None,
        node: str | None = None,
        key: str | None = None,
        labels: dict[str, str] | None = None,
        version: str = "unknown",
        capacity: int = 0,
    ) -> None:
        self._url = (fleet_url or os.environ.get(_ENV_URL) or "").rstrip("/")
        if not self._url:
            self._enabled = False
            self._node = ""
            self._key = ""
            self._labels: dict[str, str] = {}
            self._thread: threading.Thread | None = None
            self._queue: queue.Queue[_PendingItem | None] | None = None
            self._stop_event: threading.Event | None = None
            self._heartbeat_callback: Callable[[], tuple[list[dict[str, Any]], int, int, int]] | None = None
            return

        self._enabled = True
        self._node = node or os.environ.get(_ENV_NODE) or socket.gethostname()
        self._key = key or os.environ.get(_ENV_KEY) or ""
        raw_labels = labels or _parse_labels(os.environ.get(_ENV_LABELS, ""))
        self._labels = {str(k): str(v) for k, v in (raw_labels or {}).items()}
        self._version = version
        self._capacity = capacity
        self._queue: queue.Queue[_PendingItem | None] = queue.Queue(
            maxsize=_MAX_QUEUE_SIZE
        )
        self._stop_event = threading.Event()
        self._last_warned: float = 0.0
        self._warned_lock = threading.Lock()
        self._heartbeat_callback: Callable[[], tuple[list[dict[str, Any]], int, int, int]] | None = None
        self._heartbeat_interval = _HEARTBEAT_INTERVAL_SECONDS
        self._last_heartbeat = 0.0
        self._thread = threading.Thread(target=self._worker, name="fleet-reporter", daemon=True)
        self._thread.start()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def node(self) -> str:
        return self._node

    @property
    def labels(self) -> dict[str, str]:
        return dict(self._labels)

    def queue_event(self, envelope: EventEnvelope) -> None:
        """Enqueue one public event.  Never blocks the caller."""
        if not self._enabled or self._queue is None:
            return
        self._post("/harness/events", [envelope.to_dict()], gzip_body=True)

    def queue_heartbeat(
        self,
        runs: list[dict[str, Any]],
        active: int,
        cap: int,
        queue_len: int = 0,
    ) -> None:
        """Enqueue a periodic heartbeat describing this node."""
        if not self._enabled:
            return
        body = {
            "node": {
                "name": self._node,
                "version": self._version,
                "kind": self._labels.get("kind"),
                "labels": self._labels,
                "uiBaseUrl": "",
            },
            "runs": [
                {
                    "runId": run.get("id"),
                    "run_id": run.get("id"),
                    "status": run.get("status"),
                    "round": run.get("active_round"),
                    "activeRole": run.get("active_role"),
                    "model": run.get("model"),
                    "repo": run.get("repo"),
                    "workspace": run.get("workspace"),
                    "youtrackIssueId": run.get("youtrack_issue_id"),
                    "summary": {k: v for k, v in run.items() if k not in {"id", "status"}},
                }
                for run in runs
            ],
            "capacity": {"active": active, "cap": cap},
            "queueLen": queue_len,
        }
        self._post("/harness/heartbeat", body, gzip_body=True)

    def queue_round_content(
        self,
        run_id: str,
        round_index: int,
        artifacts: list[dict[str, Any]],
        trajectories: list[dict[str, Any]],
        report: dict[str, Any] | None = None,
    ) -> None:
        """Enqueue a complete round content payload."""
        if not self._enabled:
            return
        body = {
            "run_id": run_id,
            "runId": run_id,
            "round": round_index,
            "summary": {},
            "artifacts": artifacts,
            "trajectories": trajectories,
        }
        if report is not None:
            body["report"] = report
        self._post("/harness/rounds", body, gzip_body=True)

    def register_heartbeat(
        self,
        callback: Callable[[], tuple[list[dict[str, Any]], int, int, int]],
    ) -> None:
        """Register a callback that produces heartbeat data every 30 s.

        The callback must return ``(runs, active, cap, queue_len)``.  It is
        invoked on the reporter daemon thread; keep it fast and exception-free.
        """
        if not self._enabled:
            return
        self._heartbeat_callback = callback

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the worker to stop and drain the queue."""
        if not self._enabled or self._queue is None or self._stop_event is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive() and self._thread != threading.current_thread():
            self._thread.join(timeout=timeout)

    def _post(self, endpoint: str, payload: Any, gzip_body: bool = True) -> None:
        """Enqueue a POST payload without blocking."""
        if not self._enabled or self._queue is None:
            return
        try:
            self._queue.put_nowait(
                _PendingItem(endpoint=endpoint, payload=payload, gzip_body=gzip_body)
            )
        except queue.Full:
            self._warn_once("fleet reporter queue full; dropping telemetry batch")

    def _worker(self) -> None:
        """Daemon worker: batch, gzip, sign, and POST telemetry.

        Every code path inside the worker is guarded so an unexpected failure
        in the reporter cannot propagate into the run's own worker thread.
        Heartbeats share the same thread so the reporter never spawns more
        than one daemon.
        """
        assert self._queue is not None
        assert self._stop_event is not None
        pending: list[_PendingItem] = []
        last_flush = time.monotonic()
        self._last_heartbeat = time.monotonic()
        try:
            while not self._stop_event.is_set():
                try:
                    item = self._queue.get(timeout=0.1)
                except queue.Empty:
                    item = None
                if item is not None:
                    pending.append(item)
                now = time.monotonic()
                if pending and (
                    now - last_flush >= _BATCH_INTERVAL_SECONDS
                    or (item is None and self._stop_event.is_set())
                ):
                    try:
                        self._flush(pending)
                    except Exception:
                        logger.exception("fleet reporter flush failed; dropping batch")
                    pending = []
                    last_flush = now
                if (
                    self._heartbeat_callback is not None
                    and now - self._last_heartbeat >= self._heartbeat_interval
                ):
                    try:
                        runs, active, cap, queue_len = self._heartbeat_callback()
                        self.queue_heartbeat(runs, active, cap, queue_len)
                    except Exception:
                        logger.exception("fleet reporter heartbeat callback failed")
                    self._last_heartbeat = now
            # Drain any remaining work on stop without blocking callers.
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    pending.append(item)
            if pending:
                try:
                    self._flush(pending)
                except Exception:
                    logger.exception("fleet reporter final flush failed; dropping batch")
        except Exception:
            logger.exception("fleet reporter worker died unexpectedly")

    def _flush(self, items: list[_PendingItem]) -> None:
        """Send one or more queued items, grouped by endpoint."""
        by_endpoint: dict[str, list[Any]] = {}
        singles: list[_PendingItem] = []
        for item in items:
            if item.endpoint == "/harness/events":
                by_endpoint.setdefault(item.endpoint, []).extend(
                    item.payload if isinstance(item.payload, list) else [item.payload]
                )
            else:
                singles.append(item)
        for endpoint, payload in by_endpoint.items():
            self._send(endpoint, payload)
        for item in singles:
            self._send(item.endpoint, item.payload, gzip_body=item.gzip_body)

    def _send(self, endpoint: str, payload: Any, *, gzip_body: bool = True) -> None:
        """One HMAC-signed POST with bounded retry."""
        url = f"{self._url}{endpoint}"
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        original_size = len(body)
        if gzip_body:
            body = __import__("gzip").compress(body)
        attempt = 0
        last_error: Exception | None = None
        while attempt < _MAX_RETRIES:
            req = self._build_request(url, body, gzip_body=gzip_body, original_size=original_size)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read()
                return
            except urllib.error.HTTPError as exc:
                last_error = exc
                # 4xx client errors are not retried; payload is malformed or auth failed.
                if 400 <= exc.code < 500:
                    self._warn_once(f"fleet reporter rejected {endpoint}: HTTP {exc.code}")
                    return
            except Exception as exc:
                last_error = exc
            attempt += 1
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)])
        self._warn_once(
            f"fleet reporter could not POST {endpoint} after {attempt} attempts: {last_error}"
        )

    def _build_request(
        self,
        url: str,
        body: bytes,
        *,
        gzip_body: bool,
        original_size: int,
    ) -> urllib.request.Request:
        signature = hmac.new(
            self._key.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-Fleet-Host": self._node,
            "X-Fleet-Signature": signature,
            "Content-Type": "application/json",
            "X-Fleet-Body-Original-Length": str(original_size),
        }
        if gzip_body:
            headers["Content-Encoding"] = "gzip"
        return urllib.request.Request(url, data=body, headers=headers, method="POST")

    def _warn_once(self, message: str) -> None:
        now = time.monotonic()
        with self._warned_lock:
            if now - self._last_warned < _WARN_ONCE_INTERVAL_SECONDS:
                return
            self._last_warned = now
        logger.warning(message)


def _parse_labels(raw: str) -> dict[str, str]:
    """Parse ``kind=ct110,repo=...`` style labels."""
    labels: dict[str, str] = {}
    if not raw:
        return labels
    for part in raw.split(","):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key:
                labels[key] = value
    return labels


def _json_default(value: object) -> Any:
    """Fallback JSON serialization for unusual values."""
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def _safe_read_text(path: Any, max_bytes: int = _MAX_ARTIFACT_BYTES) -> dict[str, Any]:
    """Read a local file through no-follow, returning content or a truncation marker.

    If ``path`` is not a string/Path, or does not resolve safely, the file is
    omitted.  Files larger than ``max_bytes`` are never dropped silently: they
    are returned with ``{"truncated": true, "bytes": N}``.
    """

    try:
        target = Path(path)
    except (TypeError, ValueError):
        return {}
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return {}
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return {}
        size = int(metadata.st_size)
        remaining = max_bytes + 1
        data = bytearray()
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            data.extend(chunk)
            remaining -= len(chunk)
        too_large = len(data) > max_bytes
        raw = bytes(data[:max_bytes])
        if too_large:
            return {"truncated": True, "bytes": size}
        try:
            text = raw.decode("utf-8", errors="replace")
            try:
                return {"json": json.loads(text)}
            except (json.JSONDecodeError, TypeError, ValueError):
                return {"text": text}
        except (UnicodeDecodeError, TypeError, ValueError):
            return {"truncated": True, "bytes": size}
    except OSError:
        return {}
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _is_safe_name(name: str) -> bool:
    if not name or len(name) > 256:
        return False
    if name in {".", ".."} or "/" in name or "\\" in name:
        return False
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
        return False
    return True


def _collect_round_content(
    runs_root: str | Path,
    run_dir: str | Path,
    round_index: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect artifacts and role trajectories for one round.

    All filesystem access is routed through ``safe_run_rounds`` / ``safe_run_role``
    boundary helpers so the walk never escapes the validated run directory.  Each
    artifact or trajectory is capped at ``_MAX_ARTIFACT_BYTES``; larger files are
    represented by an explicit ``{truncated: true, bytes: N}`` marker.
    """

    artifacts: list[dict[str, Any]] = []
    trajectories: list[dict[str, Any]] = []
    try:
        from ..utils.run_boundary import safe_run_rounds

        rounds_dir = safe_run_rounds(runs_root, run_dir, allow_missing=True)
    except Exception:
        logger.exception("fleet round content walk failed")
        return artifacts, trajectories
    if rounds_dir is None:
        return artifacts, trajectories
    try:
        round_dir = rounds_dir / f"round_{round_index:03d}"
        # Validate the round directory is a real child of the validated rounds root.
        resolved_rounds = rounds_dir.resolve(strict=False)
        resolved_round = round_dir.resolve(strict=False)
        if resolved_round.parent != resolved_rounds or not resolved_round.is_dir():
            return artifacts, trajectories
    except (OSError, RuntimeError, ValueError):
        return artifacts, trajectories

    try:
        entries = sorted(round_dir.iterdir())
    except OSError:
        entries = []

    seen_trajectory_files: set[str] = set()
    for entry in entries:
        name = entry.name
        if not _is_safe_name(name):
            continue
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
        except OSError:
            continue
        target = entry.resolve()
        try:
            target.relative_to(resolved_round)
        except (ValueError, OSError, RuntimeError):
            continue
        content = _safe_read_text(target, max_bytes=_MAX_ARTIFACT_BYTES)
        if not content:
            continue
        for role in _TRAJECTORY_ROLES:
            base = f"{role}_raw_trajectory"
            normalized = f"{role}_trajectory.jsonl"
            if name.startswith(base) or name == normalized:
                seen_trajectory_files.add(name)
                trajectories.append(
                    {
                        "role": role,
                        "file": name,
                        "content": content,
                    }
                )
                break
        else:
            # Anything that is not a recognized trajectory file is treated as a
            # round artifact (plan text, screenshots, metadata, etc.).
            artifacts.append(
                {
                    "name": name,
                    "content": content,
                }
            )

    return artifacts, trajectories


def post_round_content(
    runs_root: str | Path,
    run_dir: str | Path,
    round_index: int,
) -> None:
    """Queue a round content payload to fleet-admin when configured.

    Safe to call from any thread; no-ops when fleet reporting is disabled.
    """
    reporter = get_reporter()
    if reporter is None or not reporter.enabled:
        return
    run_id = str(Path(run_dir).name)
    artifacts, trajectories = _collect_round_content(runs_root, run_dir, round_index)
    _safe_call(reporter.queue_round_content, run_id, round_index, artifacts, trajectories)


def _read_report(log_dir: Path) -> dict[str, Any] | None:
    """Read logs/report.json if it exists and is safe JSON."""

    try:
        target = Path(log_dir) / "report.json"
        fd = os.open(
            target,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError:
        return None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return None
        data = os.read(fd, _MAX_ARTIFACT_BYTES + 1)
        if len(data) > _MAX_ARTIFACT_BYTES:
            return {"truncated": True, "bytes": int(metadata.st_size)}
        text = data.decode("utf-8", errors="replace")
        return json.loads(text)
    except Exception:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def post_report(
    runs_root: str | Path,
    run_dir: str | Path,
) -> None:
    """Queue the final logs/report.json to fleet-admin when configured.

    Safe to call from any thread; no-ops when fleet reporting is disabled.
    """
    reporter = get_reporter()
    if reporter is None or not reporter.enabled:
        return
    try:
        from ..utils.run_boundary import safe_run_logs

        log_dir = safe_run_logs(runs_root, run_dir, allow_missing=True)
    except Exception:
        log_dir = None
    if log_dir is None:
        return
    report = _read_report(log_dir)
    if report is None:
        return
    run_id = str(Path(run_dir).name)
    _safe_call(reporter.queue_round_content, run_id, 0, [], [], report)


_reporter: FleetReporter | None = None
_init_lock = threading.Lock()


def get_reporter(
    *,
    fleet_url: str | None = None,
    node: str | None = None,
    key: str | None = None,
    labels: dict[str, str] | None = None,
    version: str = "unknown",
    capacity: int = 0,
    reset: bool = False,
) -> FleetReporter | None:
    """Return the singleton reporter, creating it on first call if configured."""
    global _reporter
    if reset:
        if _reporter is not None:
            _reporter.stop()
        _reporter = None
    if _reporter is None:
        with _init_lock:
            if _reporter is None:
                _reporter = FleetReporter(
                    fleet_url=fleet_url,
                    node=node,
                    key=key,
                    labels=labels,
                    version=version,
                    capacity=capacity,
                )
    return _reporter


def _safe_call(func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Call a function and swallow every exception so the reporter cannot
    raise into the run's own worker thread."""
    try:
        func(*args, **kwargs)
    except Exception:
        logger.exception("fleet reporter hook failed; dropping telemetry")


def wrap_event_sink(
    sink: Callable[[dict[str, Any]], None]
) -> Callable[[dict[str, Any]], None]:
    """Wrap a public event dict sink so it is also sent to fleet-admin."""

    def _wrapped(envelope: dict[str, Any]) -> None:
        sink(envelope)
        reporter = get_reporter()
        if reporter is None or not reporter.enabled:
            return
        event = EventEnvelope(
            run_id=str(envelope.get("run_id") or "local"),
            type=str(envelope.get("type") or "unknown"),
            ts=float(envelope.get("ts") or time.time()),
            round=_optional_int(envelope.get("round")),
            role=envelope.get("role"),
            status=envelope.get("status"),
            payload=_trim_payload(envelope.get("payload")),
        )
        _safe_call(reporter.queue_event, event)

    return _wrapped


def wrap_status_writer(write_status: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    """Wrap ControlBus.write_status to emit a ``run.status`` event."""

    def _wrapped(status: dict[str, Any]) -> None:
        write_status(status)
        reporter = get_reporter()
        if reporter is None or not reporter.enabled:
            return
        run_id = status.get("run_id") or "local"
        event = EventEnvelope(
            run_id=str(run_id),
            type="run.status",
            ts=time.time(),
            round=_optional_int(status.get("round")),
            role=status.get("active_role"),
            status=status.get("status"),
            payload={k: v for k, v in status.items() if k not in {"run_id", "round", "active_role", "status"}},
        )
        _safe_call(reporter.queue_event, event)

    return _wrapped


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_RESERVED_EVENT_KEYS = frozenset(
    {
        "schema_version",
        "event_id",
        "event",
        "type",
        "ts",
        "timestamp",
        "run_id",
        "round",
        "round_index",
        "round_no",
        "round_number",
        "role",
        "role_name",
        "agent_role",
        "status",
    }
)


def _record_to_envelope(record: dict[str, Any]) -> EventEnvelope:
    """Convert a manager events.jsonl record into a public EventEnvelope."""
    if not isinstance(record, dict):
        record = {}
    event_id = str(record.get("event_id") or "")
    run_id = str(record.get("run_id") or _run_id_from_event_id(event_id))
    raw_type = str(record.get("event") or record.get("type") or "unknown")
    ts_raw = record.get("ts", record.get("timestamp", 0.0))
    try:
        ts = float(ts_raw)
    except (TypeError, ValueError):
        ts = 0.0
    round_number = next(
        (
            _optional_int(record.get(key))
            for key in ("round", "round_index", "round_no", "round_number")
            if record.get(key) is not None
        ),
        None,
    )
    role = (
        record.get("role")
        or record.get("role_name")
        or record.get("agent_role")
        or _infer_role(raw_type)
    )
    status = record.get("status") or _infer_status(raw_type)
    payload = {k: v for k, v in record.items() if k not in _RESERVED_EVENT_KEYS}
    return EventEnvelope(
        run_id=run_id,
        type=raw_type,
        ts=ts,
        round=round_number,
        role=role,
        status=status,
        payload=_trim_payload(payload),
    )


def _run_id_from_event_id(event_id: str) -> str:
    if ":" in event_id:
        return event_id.split(":", 1)[0]
    return "local"


def _infer_role(event_name: str) -> str | None:
    dotted = f".{event_name}."
    for role in (
        "manager",
        "auditor_format_repair",
        "auditor",
        "executor",
        "final_response",
    ):
        if f".{role}." in dotted or event_name.startswith(f"{role}."):
            return role
    return None


def _infer_status(event_name: str) -> str | None:
    if event_name.endswith("_start"):
        return "running"
    if event_name.endswith("_done"):
        return "completed"
    if event_name.endswith("_cancelled"):
        return "cancelled"
    if event_name.endswith("_failed"):
        return "failed"
    if event_name.endswith("_created"):
        return "pending"
    if event_name.endswith("_resolved"):
        return "completed"
    return None


def post_event_record(record: dict[str, Any]) -> None:
    """Queue one manager events.jsonl record to fleet-admin as a public envelope.

    Safe to call from any thread; no-ops when fleet reporting is disabled.
    """
    reporter = get_reporter()
    if reporter is None or not reporter.enabled:
        return
    event = _record_to_envelope(record)
    _safe_call(reporter.queue_event, event)


def _trim_payload(payload: Any) -> dict[str, Any]:
    """Strip transcripts and large nested objects from event payloads."""
    if not isinstance(payload, dict):
        return {}
    drop_keys = {
        "transcript",
        "trajectory",
        "thinking",
        "raw_thinking",
        "messages",
        "prompt",
        "final_response_full",
        "executor_output",
        "auditor_report",
        "harness_feedback",
        "task_state",
        "task_contract",
        "plan_text",
    }
    trimmed: dict[str, Any] = {}
    for key, value in payload.items():
        if key in drop_keys:
            continue
        if isinstance(value, (dict, list)) and _approx_size(value) > 4096:
            trimmed[key] = {"_summary": f"{type(value).__name__} omitted ({_approx_size(value)} bytes)"}
        else:
            trimmed[key] = value
    return trimmed


def _approx_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return 0
