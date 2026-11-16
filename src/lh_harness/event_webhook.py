"""Outbound run-event webhook: push terminal run events to a configured endpoint.

A fail-open, side-car notification sink, enabled only when
``LH_HARNESS_EVENT_WEBHOOK_URL`` is set.  All delivery work is delegated to a
single daemon thread with a bounded queue so that a slow, hanging, or absent
receiver can never block a run.  One POST is sent per matching event, with an
optional HMAC-SHA256 signature over the exact body bytes.

The payload is ids-and-status only (event, event_id, ts, node, run_id, queue_id,
name, status, failure_class, approval_id, approval_title, requester): no task
text, no final response, no model output, no environment, no token ever leaves
the process through this module.  Call sites build the body; this module owns
delivery only.

Delivery is at-least-once: a worker that dies between a successful POST and the
next dequeue would resend.  Receivers dedupe on ``event_id``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# Names registered in the settings DB catalog (settings_store.CATALOG).
# ``apply_startup_settings`` copies DB values over the environment at service
# start, so ``os.environ`` is the read path like every other LH_HARNESS_* option.
SETTING_URL = "LH_HARNESS_EVENT_WEBHOOK_URL"
SETTING_SECRET = "LH_HARNESS_EVENT_WEBHOOK_SECRET"
SETTING_EVENTS = "LH_HARNESS_EVENT_WEBHOOK_EVENTS"

# The four terminal events the harness already produces on its internal event
# stream (webapi/events.py EVENT_TYPE_MAP); the default allowlist matches the
# spec: failures, cancellations, completions, and operator gates.
DEFAULT_EVENTS = "run.failed,run.cancelled,run.completed,operator.approval.pending"

# Transport limits.  The queue is bounded; when it is full the OLDEST entry is
# dropped (and counted) so a burst never blocks the run loop and never grows
# without limit.
MAX_QUEUE_SIZE = 1000
REQUEST_TIMEOUT_SECONDS = 5.0  # connect + read, per attempt
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (1.0, 4.0)
ALLOWED_SCHEMES = ("http", "https")
SIGNATURE_HEADER = "X-LH-Signature"
USER_AGENT = "lh-harness-event-webhook/1"


def parse_events(raw: str | None) -> frozenset[str]:
    """Parse the comma-separated event allowlist (whitespace-tolerant, deduped)."""

    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


class EventWebhook:
    """Bounded-queue, daemon-thread POST transport for run events.

    An unset or empty URL leaves the hook inert: the constructor runs one
    boolean check, creates no thread, no queue, and no socket, and every public
    method is a no-op.  This keeps the flag-off critical path byte-identical to
    a build without the webhook.
    """

    def __init__(self, *, url: str | None = None, secret: str | None = None,
                 events: str | None = None, environ: Mapping[str, str] | None = None) -> None:
        # The single flag-off gate comes first: when the URL is unset or empty
        # nothing else runs - no second env read, no lock, no event, no queue,
        # no thread, no socket.  ``_dropped`` is read even while off so callers
        # never need an ``enabled`` guard for the counters.
        self._dropped = 0
        self._delivered = 0
        self._given_up = 0
        self._enabled = False
        self._url = ""
        self._secret = ""
        self._events = frozenset()
        self._queue = None
        self._thread = None
        self._stop = None
        self._enqueue_lock = None
        raw_url = (os.environ if environ is None else environ).get(SETTING_URL, "")
        url = raw_url if url is None else url
        if not url:
            return
        env = os.environ if environ is None else environ
        self._url = url
        self._secret = (secret if secret is not None else env.get(SETTING_SECRET, "")) or ""
        self._events = parse_events(
            events if events is not None else env.get(SETTING_EVENTS, "")
        ) or parse_events(DEFAULT_EVENTS)
        self._stop = threading.Event()
        self._enqueue_lock = threading.Lock()
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            # One refusal line, scheme only: the URL itself may carry a query
            # string, which never appears in logs.
            logger.warning(
                "event webhook disabled: %r is not an http(s) URL (LH_HARNESS_EVENT_WEBHOOK_URL "
                "must start with http:// or https://)",
                scheme or "(no scheme)",
            )
            return
        self._enabled = True
        self._queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self._thread = threading.Thread(
            target=self._worker, name="lh-event-webhook", daemon=True
        )
        self._thread.start()
        logger.info(
            "event webhook enabled: POSTing %s to a %s endpoint",
            ", ".join(sorted(self._events)) or "(no events)",
            scheme,
        )

    # ------------------------------------------------------------ properties --
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def url(self) -> str:
        return self._url

    @property
    def events(self) -> frozenset[str]:
        return self._events

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def delivered(self) -> int:
        return self._delivered

    @property
    def given_up(self) -> int:
        return self._given_up

    # ---------------------------------------------------------------- seam ----
    def wants(self, event_type: str) -> bool:
        """True when the hook is on and ``event_type`` is in the allowlist."""

        return self._enabled and event_type in self._events

    def emit(self, event_type: str, body: Mapping[str, Any]) -> bool:
        """Enqueue one event for delivery; never raises, never blocks.

        Returns True when the event was accepted (queued or dropped-into), False
        when the hook is off or the event is not allowlisted.  ``body`` is
        serialized here, on the caller's thread, so the worker only ever sees
        bytes: the exact bytes it signs are the exact bytes it sends.
        """

        if not self.wants(event_type):
            return False
        try:
            payload = json.dumps(dict(body), ensure_ascii=False, default=str).encode("utf-8")
            self._enqueue(payload)
            return True
        except Exception:  # noqa: BLE001 - delivery must never hurt a run
            logger.exception("event webhook: enqueue failed for %s", event_type)
            return False

    def _enqueue(self, payload: bytes) -> None:
        # Only reachable while enabled: ``emit`` gates on ``wants``, which is
        # False until the constructor has built the queue.
        if self._queue is None or self._enqueue_lock is None:
            return
        with self._enqueue_lock:
            try:
                self._queue.put_nowait(payload)
                return
            except queue.Full:
                pass
            # Bounded queue, oldest-first eviction: a burst that outruns the
            # worker sheds the earliest queued event, never the newest, and is
            # counted so the operator can see the loss.
            try:
                self._queue.get_nowait()
            except queue.Empty:  # pragma: no cover - only under a concurrent drain
                pass
            self._dropped += 1
            self._queue.put_nowait(payload)

    # -------------------------------------------------------------- worker ----
    def _worker(self) -> None:
        # The worker exists only while enabled, so the queue is not None here;
        # the guards keep a mis-ordered ``stop`` from crashing the thread.
        if self._queue is None or self._stop is None:
            return
        while not self._stop.is_set():
            try:
                payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if payload is None:
                break
            try:
                self._deliver(payload)
            except Exception:  # noqa: BLE001 - the worker must survive anything
                logger.exception("event webhook: delivery raised past its guards")

    def _deliver(self, payload: bytes) -> None:
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self._secret:
            signature = hmac.new(self._secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
            headers[SIGNATURE_HEADER] = f"sha256={signature}"
        request = urllib.request.Request(self._url, data=payload, headers=headers, method="POST")
        reason = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    response.read()
                self._delivered += 1
                return
            except urllib.error.HTTPError as exc:
                reason = f"HTTP {exc.code}"
                # A 4xx is the receiver refusing this payload; retrying the
                # same bytes cannot change the answer.
                if 400 <= exc.code < 500:
                    break
            except Exception as exc:  # noqa: BLE001 - class name only, never the message
                reason = type(exc).__name__
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS[attempt - 1])
        event_id = self._event_id_of(payload)
        self._given_up += 1
        # ONE line, and only safe fields: event_id, attempt count, and the HTTP
        # status or exception class.  Never the URL query string, never the
        # secret, never the body.
        logger.warning(
            "event webhook: gave up on event_id=%s after %d attempt(s): %s",
            event_id, attempt, reason or "unknown",
        )

    @staticmethod
    def _event_id_of(payload: bytes) -> str:
        try:
            value = json.loads(payload.decode("utf-8")).get("event_id")
            return str(value) if value is not None else "?"
        except Exception:  # noqa: BLE001 - a malformed body must not break logging
            return "?"

    # --------------------------------------------------------------- stop -----
    def stop(self, timeout: float = 1.0) -> None:
        """Signal the worker to exit (tests and shutdown only)."""

        if self._stop is not None:
            self._stop.set()
        if self._queue is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:  # pragma: no cover - stop beats a saturated queue
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)


_singleton: EventWebhook | None = None
_singleton_lock = threading.Lock()


def get_event_webhook(environ: Mapping[str, str] | None = None) -> EventWebhook:
    """Return the process-wide hook, constructing it on first call.

    Called once at service start, after ``apply_startup_settings`` has applied
    DB values over the environment.  When the URL is unset the constructor does
    one boolean check and no more, so this call is inert by construction.
    """

    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = EventWebhook(environ=environ)
        return _singleton


def reset_event_webhook() -> None:
    """Drop the process-wide hook (tests only)."""

    global _singleton
    with _singleton_lock:
        hook, _singleton = _singleton, None
    if hook is not None:
        hook.stop()


__all__ = [
    "ALLOWED_SCHEMES",
    "DEFAULT_EVENTS",
    "EventWebhook",
    "MAX_ATTEMPTS",
    "MAX_QUEUE_SIZE",
    "REQUEST_TIMEOUT_SECONDS",
    "RETRY_BACKOFF_SECONDS",
    "SETTING_EVENTS",
    "SETTING_SECRET",
    "SETTING_URL",
    "SIGNATURE_HEADER",
    "get_event_webhook",
    "parse_events",
    "reset_event_webhook",
]
