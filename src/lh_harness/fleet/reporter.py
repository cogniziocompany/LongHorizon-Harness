"""Fleet reporter: push harness telemetry to fleet-admin over HMAC-signed HTTPS.

The reporter is a fail-open, side-car telemetry sink.  It is enabled only when
``LH_HARNESS_FLEET_URL`` is set.  All work is delegated to a single daemon
thread with a bounded queue so that network stalls or fleet-admin outages never
block a run.  Events are batched for 2 s, gzip-compressed, and POSTed with the
same HMAC-SHA256 + ``X-Fleet-Host`` + ``X-Fleet-Signature`` scheme used by the
device ``/checkin`` endpoint.
"""

from __future__ import annotations

import base64
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
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

_ENV_URL = "LH_HARNESS_FLEET_URL"
_ENV_NODE = "LH_HARNESS_FLEET_NODE"
_ENV_KEY = "LH_HARNESS_FLEET_KEY"
_ENV_LABELS = "LH_HARNESS_FLEET_LABELS"
_ENV_ALL = (_ENV_URL, _ENV_NODE, _ENV_KEY, _ENV_LABELS)
# Optional (intentionally NOT in _ENV_ALL): the public base URL of this node's
# Web console, reported as ``node.uiBaseUrl`` in the heartbeat so fleet-admin
# can deep-link from the fleet window to the node's own dashboard.  A missing
# or unset value reports "".
_ENV_UI_BASE_URL = "LH_HARNESS_FLEET_UI_BASE_URL"

_BATCH_INTERVAL_SECONDS = 2.0
_HEARTBEAT_INTERVAL_SECONDS = 30.0
_MAX_QUEUE_SIZE = 10_000
_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = (0.0, 1.0, 2.0)
_WARN_ONCE_INTERVAL_SECONDS = 300.0
_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
# Heartbeat bounding: the heartbeat sends only non-terminal runs (the ones a
# remote operator can act on) plus aggregate counts for the rest.  The active
# list itself is hard-capped so a node with a very large live backlog stays
# well under any plausible proxy body limit for the heartbeat route.
_MAX_ACTIVE_RUNS_PER_HEARTBEAT = 200
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
# Over the inline cap a run file is no longer skipped: it is uploaded as a
# sequence of independently gzip-compressed base64 chunks (one POST per chunk)
# so the whole transcript still reaches fleet-admin.  Two settings bound that
# path (both live in the settings DB catalog; ``apply_startup_settings`` copies
# DB values over the environment at service start, so ``os.environ`` is the
# read path like every other LH_HARNESS_* option):
# - ``LH_HARNESS_MAX_ARTIFACT_BYTES``: inline cap; default keeps the historic
#   8 MB behavior byte-identical until configured.
# - ``LH_HARNESS_MAX_TRANSCRIPT_BYTES``: absolute hard cap for the chunked
#   path; anything larger keeps the old ``{truncated: true}`` marker.
_MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
_ENV_MAX_ARTIFACT_BYTES = "LH_HARNESS_MAX_ARTIFACT_BYTES"
_ENV_MAX_TRANSCRIPT_BYTES = "LH_HARNESS_MAX_TRANSCRIPT_BYTES"
# Raw bytes per chunk.  At 512 KiB the gzip+base64 envelope stays under the
# 1 MB JSON control-body limit of every JSON ingest route.
_UPLOAD_CHUNK_BYTES = 512 * 1024
# Server route that consumes chunked artifact/transcript uploads.
# This must match ``/api/runs/{run_id}/rounds/{round_index}/artifact-chunks``
# in ``src/lh_harness/webapi/server.py``.
_CHUNK_ENDPOINT = "/api/runs/{run_id}/rounds/{round_index}/artifact-chunks"
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
        env_values = {
            _ENV_URL: fleet_url or os.environ.get(_ENV_URL),
            _ENV_NODE: node or os.environ.get(_ENV_NODE),
            _ENV_KEY: key or os.environ.get(_ENV_KEY),
            _ENV_LABELS: labels if labels else _parse_labels(os.environ.get(_ENV_LABELS, "")),
        }
        # An absent URL disables the reporter entirely; anything short of all
        # four variables is a misconfiguration that must be LOUD so an
        # unregistered node can never masquerade as an idle one.  Names only,
        # never values.
        self._missing_env = [name for name in _ENV_ALL if not env_values.get(name)]
        self._configured = not self._missing_env
        if self._missing_env:
            logger.warning(
                "fleet reporter configuration incomplete; missing environment "
                "variables: %s (this node will NOT register with any fleet; "
                "set them in the service EnvironmentFile and restart the service)",
                ", ".join(self._missing_env),
            )
        self._url = (env_values[_ENV_URL] or "").rstrip("/")
        self._ui_base_url = (os.environ.get(_ENV_UI_BASE_URL) or "").rstrip("/")
        self._ever_succeeded = False
        self._last_attempt_ok: bool | None = None
        self._last_attempt_error: str | None = None
        self._attempt_lock = threading.Lock()
        if not self._url:
            self._enabled = False
            self._node = ""
            self._key = ""
            self._labels: dict[str, str] = {}
            self._thread: threading.Thread | None = None
            self._queue: queue.Queue[_PendingItem | None] | None = None
            self._stop_event: threading.Event | None = None
            self._heartbeat_callback: Callable[
                [],
                tuple[list[dict[str, Any]], int, int, int]
                | tuple[list[dict[str, Any]], int, int, int, float | None, dict[str, Any] | None],
            ] | None = None
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
        self._heartbeat_callback: Callable[
            [],
            tuple[list[dict[str, Any]], int, int, int]
            | tuple[list[dict[str, Any]], int, int, int, float | None, dict[str, Any] | None],
        ] | None = None
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

    @property
    def configured(self) -> bool:
        """True only when all four LH_HARNESS_FLEET_* variables were present and
        non-empty at construction.  False means this node cannot be registered."""
        return self._configured

    @property
    def missing_env(self) -> tuple[str, ...]:
        """Names of the LH_HARNESS_FLEET_* variables missing at construction."""
        return tuple(self._missing_env)

    def registration_state(self) -> dict[str, Any]:
        """Snapshot of registration history for /api/meta exposure.

        ``ever_succeeded`` flips True on the first successful POST and stays
        True; ``last_ok``/``last_error`` describe the most recent attempt
        (None before the first attempt completes).
        """
        with self._attempt_lock:
            return {
                "ever_succeeded": self._ever_succeeded,
                "last_ok": self._last_attempt_ok,
                "last_error": self._last_attempt_error,
            }

    def _record_registration_attempt(self, *, success: bool, error: str | None) -> None:
        with self._attempt_lock:
            self._last_attempt_ok = success
            self._last_attempt_error = error
            if success:
                self._ever_succeeded = True

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
        launcher_tick_at: float | None = None,
        lease_holder: dict[str, Any] | None = None,
    ) -> None:
        """Enqueue a periodic heartbeat describing this node.

        The heartbeat carries only the node's *non-terminal* runs in
        ``runs[]`` — the ones a remote operator can act on — plus aggregate
        counts for the whole store (``runsTotal``, ``runsByStatus``) and a
        ``runsTruncated`` flag when the active list itself is capped.  A
        long-lived node accumulates hundreds of completed runs whose per-run
        summaries dominate the payload (measured ~10 KB per run; a 532-run
        store produced a 5.47 MB body that the fleet plane rejected with
        HTTP 413), so the full run list must never be sent.

        ``node.uiBaseUrl`` reports the optional
        ``LH_HARNESS_FLEET_UI_BASE_URL`` (trailing slash stripped, ``""``
        when unset); each run row additionally carries ``round``,
        ``activeRole``, and a bounded ``lastEvent`` projection with
        ``lastEventAgeSeconds`` derived from the run's last durable event.
        """
        if not self._enabled:
            return
        # Imported lazily: supervisor/__init__ eagerly pulls in control_bus,
        # which imports this module, so a module-level import would create a
        # circular import for anything loading fleet.reporter first.
        from lh_harness.supervisor.lifecycle import (
            TERMINAL_STATUSES,
            canonical_lifecycle_status,
        )
        # Aggregate over the whole run list first: every run is counted by
        # status, but only non-terminal runs are serialized into ``runs[]``.
        # Unknown/blank statuses canonicalize to "idle" (non-terminal), so a
        # malformed record is reported, never silently dropped.
        runs_total = len(runs)
        runs_by_status: dict[str, int] = {}
        active_runs: list[dict[str, Any]] = []
        for run in runs:
            status = canonical_lifecycle_status(run.get("status"))
            runs_by_status[status] = runs_by_status.get(status, 0) + 1
            if status not in TERMINAL_STATUSES:
                active_runs.append(run)
        # Hard cap on the active list itself (most recent first by the
        # summary's ``updated_at``; the raw registry ``mtime`` is the
        # fallback).  Live runs are bounded in practice by the node's
        # capacity; the cap keeps a pathological store or a misbehaving
        # heartbeat callback from re-inflating the payload.
        runs_truncated = len(active_runs) > _MAX_ACTIVE_RUNS_PER_HEARTBEAT
        if runs_truncated:
            active_before_cap = len(active_runs)
            active_runs = sorted(
                active_runs,
                key=lambda r: r.get("updated_at", r.get("mtime", 0)),
                reverse=True,
            )[:_MAX_ACTIVE_RUNS_PER_HEARTBEAT]
            logger.info(
                "fleet reporter heartbeat: capping active runs from "
                f"{active_before_cap} to {_MAX_ACTIVE_RUNS_PER_HEARTBEAT} most recent; "
                "aggregate counts still cover every run"
            )
        # Serialize run rows only after the cap so the tail read (one bounded
        # seek-from-end per run) happens at most 200 times per heartbeat.
        # ``round``/``activeRole``/``lastEvent`` are derived from the run's
        # last durable event via the same ``EventTailer.read_last`` reader the
        # /api/runs/{id}/latest liveness route uses; run summaries carry no
        # active-round/active-role fields of their own.
        now = time.time()
        serialized_runs = [
            _heartbeat_run_row(run, now=now) for run in active_runs
        ]
        body = {
            "node": {
                "name": self._node,
                "version": self._version,
                "kind": self._labels.get("kind"),
                "labels": self._labels,
                "uiBaseUrl": self._ui_base_url,
            },
            "runs": serialized_runs,
            "capacity": {"active": active, "cap": cap},
            "queueLen": queue_len,
            # Launcher liveness (task 173, scope 6): the lease's last refresh
            # and its holder.  Both are None when no lease exists, which is the
            # fleet window's "no launcher" signal -- so the block is always
            # present and never omitted.
            "liveness": {
                "launcher_tick_at": launcher_tick_at,
                "lease_holder": lease_holder,
            },
            "runsTotal": runs_total,
            "runsByStatus": dict(sorted(runs_by_status.items())),
            "runsTruncated": runs_truncated,
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
        """Enqueue a complete round content payload.

        Content dicts produced by the chunked path carry only the upload
        metadata plus a private ``_chunk_source`` key (the local file path).
        That key is stripped from the inline payload here and its file is
        re-read slice by slice: one gzip+base64 envelope is produced, queued,
        and released before the next slice is read, so neither the transcript
        nor all chunk envelopes ever exist in the reporter process at once.
        Chunks target the same
        ``/api/runs/{run_id}/rounds/{round_index}/artifact-chunks`` route the
        webapi server exposes so the round body stays small and no single
        request approaches the ingest body limit.
        """
        if not self._enabled:
            return
        for kind, items, name_key in (
            ("artifact", artifacts, "name"),
            ("trajectory", trajectories, "file"),
        ):
            for item in items:
                content = item.get("content") if isinstance(item, dict) else None
                if not isinstance(content, dict):
                    continue
                source = content.pop("_chunk_source", None)
                if not source:
                    continue
                envelope_base: dict[str, Any] = {
                    "run_id": run_id,
                    "runId": run_id,
                    "round": round_index,
                    "kind": kind,
                    "name": item.get(name_key),
                    "bytes": content.get("bytes"),
                    "sha256": content.get("sha256"),
                    "encoding": content.get("encoding"),
                    "chunk_count": content.get("chunks"),
                }
                if kind == "trajectory":
                    envelope_base["role"] = item.get("role")
                emitted = 0
                for index, data in enumerate(_iter_chunk_payloads(source)):
                    envelope = dict(envelope_base)
                    envelope["chunk_index"] = index
                    envelope["data"] = data
                    self._post_chunk_envelope(run_id, round_index, envelope)
                    # Release the chunk body before the next slice is read so
                    # only one envelope is alive at a time.
                    emitted = index + 1
                    envelope = None
                    data = None
                expected = content.get("chunks")
                if isinstance(expected, int) and emitted != expected:
                    logger.warning(
                        "<ORGANIZATION_OCKAH_50> reporter chunk upload for %s round %d emitted "
                        "%d of %d chunks for %s; the file changed during upload",
                        run_id,
                        round_index,
                        emitted,
                        expected,
                        item.get(name_key),
                    )
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

    def _post_chunk_envelope(self, run_id: str, round_index: int, envelope: dict[str, Any]) -> None:
        """Queue one chunk envelope POST to the server's artifact-chunk route.

        Sent with ``gzip_body=False``: the envelope's chunk payload is already
        individually gzipped inside the JSON, so a second compression layer
        would only burn CPU.
        """

        endpoint = _CHUNK_ENDPOINT.format(
            run_id=urllib.parse.quote(str(run_id), safe=""),
            round_index=round_index,
        )
        self._post(endpoint, envelope, gzip_body=False)

    def register_heartbeat(
        self,
        callback: Callable[
            [],
            tuple[list[dict[str, Any]], int, int, int]
            | tuple[list[dict[str, Any]], int, int, int, float | None, dict[str, Any] | None],
        ],
    ) -> None:
        """Register a callback that produces heartbeat data every 30 s.

        The callback must return ``(runs, active, cap, queue_len)`` or, when
        the node exposes a launcher lease, the extended
        ``(runs, active, cap, queue_len, launcher_tick_at, lease_holder)``.  It
        is invoked on the reporter daemon thread; keep it fast and
        exception-free.
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
                        result = self._heartbeat_callback()
                        if len(result) == 6:
                            runs, active, cap, queue_len, launcher_tick_at, lease_holder = result
                            self.queue_heartbeat(
                                runs, active, cap, queue_len,
                                launcher_tick_at=launcher_tick_at,
                                lease_holder=lease_holder,
                            )
                        else:
                            runs, active, cap, queue_len = result
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
        # Sign the JSON bytes, never the compressed wire bytes: fleet-admin's
        # express.json ``verify`` hands the HMAC the INFLATED body, so a
        # signature over the gzip stream can never match (9,098 consecutive
        # ``harness_bad_sig`` rejections for ct110 before this was measured,
        # 2026-09-22; proven by a probe that signed plaintext and got 200).
        sign_bytes = body
        if gzip_body:
            body = __import__("gzip").compress(body)
        attempt = 0
        last_error: Exception | None = None
        while attempt < _MAX_RETRIES:
            req = self._build_request(
                url, body, sign_bytes=sign_bytes, gzip_body=gzip_body, original_size=original_size
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read()
                self._record_registration_attempt(success=True, error=None)
                return
            except urllib.error.HTTPError as exc:
                last_error = exc
                # 4xx client errors are not retried; payload is malformed or auth failed.
                if 400 <= exc.code < 500:
                    self._warn_once(f"fleet reporter rejected {endpoint}: HTTP {exc.code}")
                    self._record_registration_attempt(success=False, error=f"HTTP {exc.code}")
                    return
            except Exception as exc:
                last_error = exc
            attempt += 1
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)])
        self._warn_once(
            f"fleet reporter could not POST {endpoint} after {attempt} attempts: {last_error}"
        )
        self._record_registration_attempt(success=False, error=str(last_error))

    def _build_request(
        self,
        url: str,
        body: bytes,
        *,
        sign_bytes: bytes | None = None,
        gzip_body: bool,
        original_size: int,
    ) -> urllib.request.Request:
        signature = hmac.new(
            self._key.encode("utf-8"),
            body if sign_bytes is None else sign_bytes,
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


def _tail_last_event(run_id: str, log_dir: Any) -> Any | None:
    """Read the run's last durable event via the fc-H1a seek-from-end tailer.

    Returns ``None`` for missing/invalid inputs, an unreadable ledger, or any
    unexpected failure — the heartbeat must never die on one run's event log.
    The reader is ``EventTailer.read_last`` (the same bounded 64 KiB
    seek-from-end tail read the ``/api/runs/{id}/latest`` liveness route
    uses), so a heartbeat over a 100 MB event log stays a tail read.

    The import is lazy for the same reason the supervisor.lifecycle import in
    :meth:`FleetReporter.queue_heartbeat` is lazy: webapi.events imports
    supervisor.control_bus, which imports this module — a module-level import
    here would be circular.
    """
    if not run_id or not isinstance(log_dir, str) or not log_dir:
        return None
    try:
        from ..webapi.events import EventTailer
    except Exception:
        return None
    role_root = Path(log_dir)
    canonical = role_root / "role_orchestration" / "events.jsonl"
    legacy = role_root / "role_management" / "events.jsonl"
    try:
        # Same canonical/legacy selection as DashboardState._role_dir.
        path = canonical if canonical.exists() or not legacy.exists() else legacy
    except OSError:
        path = canonical
    try:
        events = EventTailer(path, run_id=run_id).read_last(1)
    except Exception:
        return None
    return events[-1] if events else None


def _heartbeat_run_row(run: dict[str, Any], *, now: float) -> dict[str, Any]:
    """Serialize one run summary into a heartbeat row with liveness identity.

    ``round`` and ``activeRole`` come from the run's last durable event: run
    summaries (``build_run_summary``) carry no active-round/active-role fields
    of their own, so reading the event tail is what lets the fleet window show
    what a live run is actually doing.  ``lastEvent`` is a small
    ``{id, type, ts}`` projection plus ``lastEventAgeSeconds``, keeping the
    row bounded regardless of the event ledger's size.
    """
    run_id = run.get("id")
    last_event = _tail_last_event(str(run_id or ""), run.get("log_dir"))
    last_event_block: dict[str, Any] | None = None
    last_event_age: float | None = None
    if last_event is not None:
        last_event_block = {
            "id": last_event.event_id,
            "type": last_event.type,
            "ts": last_event.ts,
        }
        if last_event.ts and last_event.ts > 0:
            last_event_age = max(0.0, round(now - last_event.ts, 3))
    return {
        "runId": run_id,
        "run_id": run_id,
        "status": run.get("status"),
        "round": last_event.round if last_event is not None else None,
        "activeRole": last_event.role if last_event is not None else None,
        "model": run.get("model"),
        "repo": run.get("repo"),
        "workspace": run.get("workspace"),
        "youtrackIssueId": run.get("youtrack_issue_id"),
        "lastEvent": last_event_block,
        "lastEventAgeSeconds": last_event_age,
        "summary": {k: v for k, v in run.items() if k not in {"id", "status"}},
    }


def _json_default(value: object) -> Any:
    """Fallback JSON serialization for unusual values."""
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def _bounded_int_setting(name: str, default: int) -> int:
    """Read an integer byte bound from the environment.

    ``apply_startup_settings`` copies settings-DB values over the environment
    at service start, so this is the settings-DB read path used by every other
    ``LH_HARNESS_*`` option.  Missing, unparsable, or absurd values fall back
    to ``default`` so a bad setting can never disable the bound.
    """

    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("ignoring unparsable %s=%r; using default %d", name, raw, default)
        return default
    if not 1 <= value <= 1024 * 1024 * 1024:
        logger.warning("ignoring out-of-range %s=%r; using default %d", name, raw, default)
        return default
    return value


def _chunk_upload_content(path: Path) -> dict[str, Any]:
    """Read ``path`` in bounded slices and build the chunked-upload metadata record.

    No chunk payload is buffered here.  This pass walks the file in
    ``_UPLOAD_CHUNK_BYTES`` slices to compute ``bytes``/``sha256``/``chunks``
    incrementally — every chunk envelope must carry the full-file digest to
    satisfy the artifact-chunk route — and attaches a private ``_chunk_source``
    key with the local path.  The payloads themselves are streamed one slice at
    a time by ``_iter_chunk_payloads`` during ``queue_round_content``, which
    pops ``_chunk_source`` so it is never serialized into the round body, so
    the whole transcript never accumulates in the reporter process.

    The receiver still reassembles by concatenating per-chunk decompressions
    and verifies ``sha256`` over the result.
    """

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return {}
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return {}
        digest = hashlib.sha256()
        read_total = 0
        chunks = 0
        while True:
            slice_bytes = os.read(fd, _UPLOAD_CHUNK_BYTES)
            if not slice_bytes:
                break
            digest.update(slice_bytes)
            read_total += len(slice_bytes)
            chunks += 1
        if read_total == 0:
            return {}
        return {
            "chunked": True,
            "bytes": read_total,
            "sha256": digest.hexdigest(),
            "encoding": "gzip+base64",
            "chunks": chunks,
            "_chunk_source": str(path),
        }
    except OSError:
        return {}
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _iter_chunk_payloads(path: Path | str) -> Iterator[str]:
    """Stream ``path`` and yield one gzip+base64 chunk payload at a time.

    The file is opened ``O_RDONLY|O_NOFOLLOW`` with the same regular-file and
    single-link checks as ``_chunk_upload_content`` and read in bounded
    ``_UPLOAD_CHUNK_BYTES`` slices; each slice is compressed and encoded
    individually and yielded before the next slice is read, so only one raw
    slice, one compressed body, and one encoded payload exist in memory at any
    time.  An open or read failure ends the stream early;
    ``queue_round_content`` logs the resulting shortfall against the chunk
    count the metadata pass computed.
    """

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return
        while True:
            try:
                slice_bytes = os.read(fd, _UPLOAD_CHUNK_BYTES)
            except OSError:
                return
            if not slice_bytes:
                return
            yield base64.b64encode(gzip.compress(slice_bytes)).decode("ascii")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


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
    artifact or trajectory up to the configured inline cap
    (``LH_HARNESS_MAX_ARTIFACT_BYTES``, default ``_MAX_ARTIFACT_BYTES``) is read
    inline.  Larger files up to the hard cap (``LH_HARNESS_MAX_TRANSCRIPT_BYTES``,
    default ``_MAX_TRANSCRIPT_BYTES``) upload as gzip+base64 chunk records instead
    of being dropped; beyond the hard cap they keep the explicit
    ``{truncated: true, bytes: N}`` marker.
    """

    inline_cap = _bounded_int_setting(_ENV_MAX_ARTIFACT_BYTES, _MAX_ARTIFACT_BYTES)
    hard_cap = _bounded_int_setting(_ENV_MAX_TRANSCRIPT_BYTES, _MAX_TRANSCRIPT_BYTES)
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
        content = _safe_read_text(target, max_bytes=inline_cap)
        if (
            content.get("truncated")
            and isinstance(content.get("bytes"), int)
            and content["bytes"] <= hard_cap
        ):
            uploaded = _chunk_upload_content(target)
            if uploaded:
                content = uploaded
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
