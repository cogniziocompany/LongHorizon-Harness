"""Seq logging: ship Python logging records to a Seq server as CLEF events.

Seq logging is a fail-open, side-car telemetry sink, designed to mirror the
fleet reporter (``fleet/reporter.py``).  It is enabled only when ``SEQ_URL``
is set.  ``SEQ_API_KEY`` is strictly optional: the dev Seq server accepts
unauthenticated ingestion, so when the key is absent events are sent without
an ``X-Seq-ApiKey`` header and no warning is emitted; when present it is
attached to every batch.  All work is delegated to a single daemon thread
with a bounded queue so that network stalls or Seq outages never block the
emitting thread.  Events are batched for 2 s, gzip-compressed, and POSTed to
the Seq raw CLEF ingestion endpoint (``/api/events/raw?clef``).

Environment variables (mirror the ``LH_HARNESS_FLEET_*`` pattern):

  ``SEQ_URL``       (none - disabled)   Seq server origin, e.g. https://seq.example.com
  ``SEQ_API_KEY``   (none - optional)   Seq ingestion API key (X-Seq-ApiKey header)
  ``SEQ_MIN_LEVEL`` INFO                Minimum Python logging level shipped to Seq
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .fleet.reporter import _parse_labels

logger = logging.getLogger(__name__)

_ENV_URL = "SEQ_URL"
_ENV_API_KEY = "SEQ_API_KEY"
_ENV_MIN_LEVEL = "SEQ_MIN_LEVEL"

_BATCH_INTERVAL_SECONDS = 2.0
_MAX_QUEUE_SIZE = 10_000
_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = (0.0, 1.0, 2.0)
_WARN_ONCE_INTERVAL_SECONDS = 300.0

# Standard LogRecord attributes; anything else on the record is emitted as an
# extra CLEF property.
_STANDARD_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__)


def _clef_level(levelno: int) -> str:
    """Map a Python logging level to the closest Seq (Serilog) level."""
    if levelno >= logging.CRITICAL:
        return "Fatal"
    if levelno >= logging.ERROR:
        return "Error"
    if levelno >= logging.WARNING:
        return "Warning"
    if levelno >= logging.INFO:
        return "Information"
    if levelno >= logging.DEBUG:
        return "Debug"
    return "Verbose"


def _parse_min_level(raw: Any) -> int:
    """Resolve ``SEQ_MIN_LEVEL`` to a logging level number (default INFO)."""
    if isinstance(raw, int):
        return raw
    text = str(raw or "INFO").strip().upper()
    value = getattr(logging, text, None)
    return value if isinstance(value, int) else logging.INFO


def _safe_property(value: Any) -> Any:
    """Coerce an extra record attribute into a JSON-safe CLEF property."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        # Strict dumps: only return values that are JSON-serializable as-is;
        # anything else (custom objects, sets, ...) is coerced to a string so
        # every CLEF property is safe before it ever reaches the worker.
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class SeqLogHandler(logging.Handler):
    """Bounded-queue, daemon-thread logging handler that ships CLEF to Seq.

    When ``SEQ_URL`` is unset the handler does nothing: creating an instance
    returns immediately and ``emit()`` is a no-op.  ``SEQ_API_KEY`` is
    optional: when set it is sent as the ``X-Seq-ApiKey`` header; when absent
    events are sent without one (the dev Seq instance accepts unauthenticated
    ingestion) and no configuration warning is raised.
    """

    def __init__(
        self,
        *,
        seq_url: str | None = None,
        api_key: str | None = None,
        min_level: str | int | None = None,
        node: str | None = None,
        labels: dict[str, str] | None = None,
        version: str | None = None,
    ) -> None:
        super().__init__()
        self._url = (seq_url or os.environ.get(_ENV_URL) or "").strip().rstrip("/")
        self._api_key = api_key if api_key is not None else (os.environ.get(_ENV_API_KEY) or "")
        self._min_level = _parse_min_level(
            min_level if min_level is not None else os.environ.get(_ENV_MIN_LEVEL)
        )
        self.setLevel(self._min_level)
        raw_labels = (
            labels
            if labels is not None
            else _parse_labels(os.environ.get("LH_HARNESS_FLEET_LABELS", ""))
        )
        self._labels = {str(k): str(v) for k, v in (raw_labels or {}).items()}
        self._node = node or os.environ.get("LH_HARNESS_FLEET_NODE") or socket.gethostname()
        self._version = version or __version__
        self._last_warned = 0.0
        self._warned_lock = threading.Lock()
        self._queue: queue.Queue[dict[str, Any] | None] | None = None
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        if not self._url:
            self._enabled = False
            return
        # SEQ_API_KEY carries no enable/disable weight: when present it is
        # sent as X-Seq-ApiKey, when absent batches go out unauthenticated
        # (the dev Seq instance accepts anonymous ingestion — see
        # docs/seq-logging.md).  Neither path warns on its own.
        self._enabled = True
        self._queue = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._worker, name="seq-log-handler", daemon=True)
        self._thread.start()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def emit(self, record: logging.LogRecord) -> None:
        """Enqueue one CLEF event.  Never raises, never blocks the caller."""
        if not self._enabled or self._queue is None:
            return
        try:
            event = self._record_to_clef(record)
        except Exception:
            self._warn_once("seq log failed to format a record as CLEF; dropping event")
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._warn_once("seq log queue full; dropping log events")

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the worker to stop and drain the queue."""
        if not self._enabled or self._queue is None or self._stop_event is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._stop_event.set()
        if (
            self._thread is not None
            and self._thread.is_alive()
            and self._thread != threading.current_thread()
        ):
            self._thread.join(timeout=timeout)

    def close(self) -> None:
        try:
            self.stop()
        finally:
            super().close()

    def _record_to_clef(self, record: logging.LogRecord) -> dict[str, Any]:
        """Convert one LogRecord into a compact CLEF object."""
        event: dict[str, Any] = {
            "@t": _utc_iso(record.created),
            "@l": _clef_level(record.levelno),
            "@m": record.getMessage(),
            "logger": record.name,
            "app": "lh-harness",
            "version": self._version,
            "node": self._node,
        }
        if isinstance(record.msg, str) and record.msg != event["@m"]:
            event["@mt"] = record.msg
        for key, value in self._labels.items():
            event.setdefault(key, value)
        if record.exc_info:
            try:
                event["@x"] = logging.Formatter().formatException(record.exc_info)
            except Exception:
                event["@x"] = str(record.exc_info[1])
        elif record.exc_text:
            event["@x"] = record.exc_text
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key.startswith("@") or key in event:
                continue
            event[key] = _safe_property(value)
        return event

    def _worker(self) -> None:
        """Daemon worker: batch, gzip, and POST CLEF events.

        Every code path inside the worker is guarded so an unexpected failure
        in the handler cannot propagate into the process.
        """
        assert self._queue is not None
        assert self._stop_event is not None
        pending: list[dict[str, Any]] = []
        last_flush = time.monotonic()
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
                        logger.exception("seq log flush failed; dropping batch")
                    pending = []
                    last_flush = now
            # Drain any remaining events on stop without blocking callers.
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
                    logger.exception("seq log final flush failed; dropping batch")
        except Exception:
            logger.exception("seq log worker died unexpectedly")

    def _flush(self, events: list[dict[str, Any]]) -> None:
        """One gzip-compressed POST of newline-delimited CLEF, bounded retry."""
        assert self._url
        url = f"{self._url}/api/events/raw?clef"
        body = "\n".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)
            for event in events
        ).encode("utf-8")
        payload = gzip.compress(body)
        headers = {
            "Content-Type": "application/vnd.serilog.clef",
            "Content-Encoding": "gzip",
        }
        if self._api_key:
            headers["X-Seq-ApiKey"] = self._api_key
        attempt = 0
        last_error: Exception | None = None
        while attempt < _MAX_RETRIES:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read()
                return
            except urllib.error.HTTPError as exc:
                last_error = exc
                # 4xx client errors are not retried; payload is malformed or auth failed.
                if 400 <= exc.code < 500:
                    self._warn_once(f"seq log ingestion rejected the batch: HTTP {exc.code}")
                    return
            except Exception as exc:
                last_error = exc
            attempt += 1
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)])
        self._warn_once(
            f"seq log could not POST events after {attempt} attempts: {last_error}"
        )

    def _warn_once(self, message: str) -> None:
        now = time.monotonic()
        with self._warned_lock:
            if now - self._last_warned < _WARN_ONCE_INTERVAL_SECONDS:
                return
            self._last_warned = now
        logger.warning(message)


_seq_handler: SeqLogHandler | None = None
_init_lock = threading.Lock()


def get_seq_handler(
    *,
    seq_url: str | None = None,
    api_key: str | None = None,
    min_level: str | int | None = None,
    node: str | None = None,
    labels: dict[str, str] | None = None,
    version: str | None = None,
    reset: bool = False,
) -> SeqLogHandler | None:
    """Return the singleton Seq log handler, creating it on first call."""
    global _seq_handler
    if reset:
        if _seq_handler is not None:
            _seq_handler.close()
        _seq_handler = None
    if _seq_handler is None:
        with _init_lock:
            if _seq_handler is None:
                _seq_handler = SeqLogHandler(
                    seq_url=seq_url,
                    api_key=api_key,
                    min_level=min_level,
                    node=node,
                    labels=labels,
                    version=version,
                )
    return _seq_handler


def install_seq_logging(
    *,
    target: logging.Logger | None = None,
    reset: bool = False,
    **handler_kwargs: Any,
) -> SeqLogHandler | None:
    """Attach the Seq log handler to the root logger when ``SEQ_URL`` is set.

    No-ops identically to the fleet reporter when ``SEQ_URL`` is unset, and
    returns ``None`` whenever the handler is not enabled.  Attaching to the
    root logger means uvicorn/FastAPI access and error logs flow to Seq too.
    """
    if not (os.environ.get(_ENV_URL) or "").strip():
        return None
    handler = get_seq_handler(reset=reset, **handler_kwargs)
    if handler is None or not handler.enabled:
        return None
    target_logger = target if target is not None else logging.getLogger()
    if handler not in target_logger.handlers:
        target_logger.addHandler(handler)
    return handler
