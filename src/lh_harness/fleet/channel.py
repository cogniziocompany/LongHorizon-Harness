"""Fleet channel: one outbound, signed WebSocket from this node to fleet-admin.

Task fc-H3 (fleet-channel plan, Paxton 2026-09-30). fleet-admin cannot reach
harness nodes, so on registration the node dials out to
``wss://<host of LH_HARNESS_FLEET_URL>/harness/channel`` and serves
allow-listed API calls over that one connection (no inbound ports, no tunnel
software).

fc-H3a added configuration and the signed handshake (URL + headers); fc-H3b..e
add the allow-listed request dispatcher, the connection loop with capped
exponential backoff, and the web-process wiring (``FleetChannel``,
``start_channel``).

Frames are JSON text. Down (server->node): {"id","kind":"http","method",
"path","query","body"}; up (node->server): {"id","status","body"}. Either side
may send {"kind":"ping","ts"}; the other answers {"kind":"pong","ts":<same>}.
Only the allow-list below is served, against this node's own loopback web API
with its service bearer; anything else answers 403 {"error":"not allowed"}.
Request timeout 30 s, response body cap 4 MB (413 over the cap).

Wire contract (identical text in fc-H3 and fc-F1): the node sends headers
X-Fleet-Host: <node name>, X-Fleet-Ts: <unix seconds>, X-Fleet-Signature: hex
HMAC-SHA256(device key from LH_HARNESS_FLEET_KEY, "<host>.<ts>"). The server
rejects |now-ts|>300, an unknown/inactive host and a bad signature.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit

logger = logging.getLogger(__name__)

ENV_URL = "LH_HARNESS_FLEET_URL"
ENV_NODE = "LH_HARNESS_FLEET_NODE"
ENV_KEY = "LH_HARNESS_FLEET_KEY"
ENV_CHANNEL = "LH_HARNESS_FLEET_CHANNEL"

CHANNEL_PATH = "/harness/channel"


@dataclass(frozen=True)
class ChannelConfig:
    """Where and as whom to dial. Never log ``key``."""

    url: str
    node: str
    key: str

    def __repr__(self) -> str:  # keep the key out of logs and tracebacks
        return f"ChannelConfig(url={self.url!r}, node={self.node!r}, key=<redacted>)"


def channel_url(fleet_url: str) -> str:
    """``wss://<host[:port]>/harness/channel`` for the configured fleet URL.

    Only the host (and an explicit port) of LH_HARNESS_FLEET_URL is used; its
    scheme and path are ignored, and the channel is always TLS.
    """

    parts = urlsplit((fleet_url or "").strip())
    if not parts.hostname:
        raise ValueError(f"{ENV_URL} has no host")
    host = parts.hostname
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    return f"wss://{host}{port}{CHANNEL_PATH}"


def sign(key: str, host: str, ts: int) -> str:
    """Hex HMAC-SHA256(key, "<host>.<ts>")."""

    message = f"{host}.{int(ts)}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def handshake_headers(node: str, key: str, now: float | None = None) -> dict[str, str]:
    """The three headers the node sends when it opens the channel."""

    ts = int(time.time() if now is None else now)
    return {
        "X-Fleet-Host": node,
        "X-Fleet-Ts": str(ts),
        "X-Fleet-Signature": sign(key, node, ts),
    }


def load_config(env: Mapping[str, str] | None = None) -> ChannelConfig | None:
    """Channel settings from the environment, or None when the channel is off.

    Off when LH_HARNESS_FLEET_URL is unset, when LH_HARNESS_FLEET_CHANNEL is
    "0", or when there is no device key to sign with. The node name follows the
    fleet reporter: LH_HARNESS_FLEET_NODE, else the hostname.
    """

    env = os.environ if env is None else env
    fleet_url = (env.get(ENV_URL) or "").strip()
    if not fleet_url or (env.get(ENV_CHANNEL) or "").strip() == "0":
        return None
    key = env.get(ENV_KEY) or ""
    if not key:
        return None
    node = env.get(ENV_NODE) or socket.gethostname()
    return ChannelConfig(url=channel_url(fleet_url), node=node, key=key)


# --- request dispatcher (fc-H3b) ---------------------------------------------

REQUEST_TIMEOUT_SECONDS = 30.0
MAX_BODY_BYTES = 4 * 1024 * 1024
# The fleet-admin server accepts one frame of the 4 MB body plus 64 KB envelope.
_MAX_FRAME_BYTES = MAX_BODY_BYTES + 60 * 1024
_MAX_DOWN_FRAME_BYTES = 1024 * 1024
_MAX_ID_CHARS = 128

# Run ids are harness-minted (``20260930T024423Z_a42191e9``); a stricter
# segment than the server's ``[^/]+`` keeps "."/".." and encoded separators out.
_RUN_ID = r"[A-Za-z0-9][A-Za-z0-9._:~-]{0,199}"
_ALLOW: dict[str, tuple[re.Pattern[str], ...]] = {
    "GET": (
        re.compile(r"^/api/(?:meta|queue|runs|runs/latest)$"),
        re.compile(rf"^/api/runs/{_RUN_ID}/(?:snapshot|events|status|latest)$"),
    ),
    "POST": (
        re.compile(rf"^/api/runs/{_RUN_ID}/(?:stop|resume|abort|instructions|time_limit)$"),
    ),
}

NOT_ALLOWED = {"error": "not allowed"}


def is_allowed(method: Any, path: Any) -> bool:
    """True only for the wire contract's allow-list (exact method + path)."""

    rules = _ALLOW.get(str(method or "").upper())
    return bool(rules) and isinstance(path, str) and any(rule.match(path) for rule in rules)


def _query_string(query: Any) -> str | None:
    """``k=v&...`` for scalar (or list of scalar) values; None when malformed."""

    if query in (None, {}):
        return ""
    if not isinstance(query, dict):
        return None
    pairs: list[tuple[str, str]] = []
    for key, value in query.items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, bool):
                item = "true" if item else "false"
            if not isinstance(item, (str, int, float)):
                return None
            pairs.append((str(key), str(item)))
    return urlencode(pairs)


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call_loopback(
    api_base: str,
    method: str,
    path: str,
    query: str,
    body: Any,
    headers: Mapping[str, str],
    *,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    max_bytes: int = MAX_BODY_BYTES,
) -> tuple[int, Any]:
    """One blocking call to this node's own web API: ``(status, json body)``."""

    url = api_base.rstrip("/") + path + (f"?{query}" if query else "")
    data = None
    request_headers = {"Accept": "application/json", **headers}
    if method == "POST":
        data = json.dumps(body if body is not None else {}).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    try:
        response = _NO_PROXY_OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    except (TimeoutError, socket.timeout):
        return 504, {"error": "timeout"}
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            return 504, {"error": "timeout"}
        return 502, {"error": f"loopback api unreachable: {type(exc).__name__}"}
    with response:
        status = int(getattr(response, "status", None) or response.getcode() or 502)
        try:
            raw = response.read(max_bytes + 1)
        except (TimeoutError, socket.timeout):
            return 504, {"error": "timeout"}
    if len(raw) > max_bytes:
        return 413, {"error": f"response body over {max_bytes} bytes"}
    if not raw:
        return status, None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode("utf-8", errors="replace")


async def dispatch_request(
    frame: Mapping[str, Any],
    *,
    api_base: str,
    headers: Mapping[str, str],
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Answer one down ``kind: http`` frame with its up frame.

    The allow-list is checked first: a refused call never reaches the API.
    The loopback call runs in a worker thread so the channel loop keeps
    answering pings while it waits.
    """

    request_id = frame.get("id")
    if not isinstance(request_id, str) or len(request_id) > _MAX_ID_CHARS:
        request_id = "" if request_id is None else str(request_id)[:_MAX_ID_CHARS]
    method = str(frame.get("method") or "").upper()
    path = frame.get("path")
    if not is_allowed(method, path):
        return {"id": request_id, "status": 403, "body": NOT_ALLOWED}
    query = _query_string(frame.get("query"))
    if query is None:
        return {"id": request_id, "status": 400, "body": {"error": "query must map names to scalar values"}}
    try:
        status, body = await asyncio.wait_for(
            asyncio.to_thread(
                call_loopback, api_base, method, str(path), query, frame.get("body"), dict(headers), timeout=timeout
            ),
            timeout=timeout + 1,
        )
    except asyncio.TimeoutError:
        status, body = 504, {"error": "timeout"}
    return {"id": request_id, "status": status, "body": body}


# --- connection loop (fc-H3c/d) ----------------------------------------------

PING_INTERVAL_SECONDS = 20.0
BACKOFF_MIN_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
_MAX_INFLIGHT = 8


def backoff_delay(attempt: int, *, low: float = BACKOFF_MIN_SECONDS, high: float = BACKOFF_MAX_SECONDS) -> float:
    """Capped exponential backoff with jitter: within [low, high]."""

    ceiling = min(high, low * (2 ** max(0, attempt)))
    return max(low, random.uniform(ceiling / 2, ceiling))


def _ws_connect(url: str, headers: dict[str, str]) -> Any:
    """``websockets`` client connect across the >=13 and legacy (12) APIs."""

    options = {"max_size": _MAX_DOWN_FRAME_BYTES, "ping_interval": None, "open_timeout": 20}
    try:
        from websockets.asyncio.client import connect
    except ImportError:  # websockets 12
        from websockets.legacy.client import connect  # type: ignore[no-redef]

        return connect(url, extra_headers=headers, **options)
    return connect(url, additional_headers=headers, **options)


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat()


class FleetChannel:
    """Background client: dial fleet-admin, serve allow-listed calls, reconnect.

    Runs its own asyncio loop in a daemon thread so the web server's loop is
    never blocked. ``status()`` feeds /api/meta.
    """

    def __init__(
        self,
        config: ChannelConfig,
        *,
        api_base: str,
        token: str | None = None,
        caller_headers: Callable[[], dict[str, str]] | None = None,
        ping_interval: float = PING_INTERVAL_SECONDS,
        backoff_min: float = BACKOFF_MIN_SECONDS,
        backoff_max: float = BACKOFF_MAX_SECONDS,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.config = config
        self.api_base = api_base
        self._token = token
        self._caller_headers = caller_headers
        self.ping_interval = ping_interval
        self.backoff_min = backoff_min
        self.backoff_max = backoff_max
        self.request_timeout = request_timeout
        self._lock = threading.Lock()
        self._connected = False
        self._since: float | None = None
        self._last_error: str | None = None
        self.connects = 0
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None

    # -- state ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """``{connected, since, last_error}``; ``since`` is the last state change."""

        with self._lock:
            return {"connected": self._connected, "since": _iso(self._since), "last_error": self._last_error}

    def _set_state(self, connected: bool, error: str | None = None) -> None:
        with self._lock:
            if connected != self._connected or self._since is None:
                self._since = time.time()
            self._connected = connected
            if connected:
                self.connects += 1
            if error is not None:
                self._last_error = error[:300]

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._thread_main, name="lh-fleet-channel", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None:
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:  # loop already closed
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:  # pragma: no cover - the loop itself never raises
            logger.exception("fleet channel thread crashed")

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        attempt = 0
        while not self._stop.is_set():
            opened_at: float | None = None
            try:
                opened_at = await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set_state(False, f"{type(exc).__name__}: {exc}")
            else:
                self._set_state(False)
            if self._stop.is_set():
                break
            # A connection that stayed up resets the backoff; a node refused
            # at the handshake (or dropped at once) keeps backing off.
            if opened_at is not None and time.monotonic() - opened_at >= self.backoff_max:
                attempt = 0
            delay = backoff_delay(attempt, low=self.backoff_min, high=self.backoff_max)
            attempt += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        self._set_state(False)

    async def _session(self) -> float | None:
        """One connection: returns when it closes (monotonic open time)."""

        headers = handshake_headers(self.config.node, self.config.key)
        async with _ws_connect(self.config.url, headers) as ws:
            opened_at = time.monotonic()
            self._set_state(True)
            logger.info("fleet channel connected to %s as %s", self.config.url, self.config.node)
            send_lock = asyncio.Lock()
            inflight = asyncio.Semaphore(_MAX_INFLIGHT)
            last_seen = [time.monotonic()]
            tasks: set[asyncio.Task[Any]] = set()

            async def send(frame: dict[str, Any]) -> None:
                text = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
                if len(text.encode("utf-8")) > _MAX_FRAME_BYTES:
                    text = json.dumps({"id": frame.get("id"), "status": 413, "body": {"error": "response body over 4 MB"}})
                async with send_lock:
                    await ws.send(text)

            async def serve(frame: dict[str, Any]) -> None:
                async with inflight:
                    reply = await self.handle_request(frame)
                try:
                    await send(reply)
                except Exception:
                    pass  # the reader sees the close and ends the session

            async def pinger() -> None:
                while True:
                    await asyncio.sleep(self.ping_interval)
                    if time.monotonic() - last_seen[0] > 3 * self.ping_interval:
                        self._set_state(True, "no frame from fleet-admin for 3 ping intervals; reconnecting")
                        await ws.close()
                        return
                    await send({"kind": "ping", "ts": int(time.time())})

            async def stopper() -> None:
                assert self._stop is not None
                await self._stop.wait()
                await ws.close()

            background = [asyncio.create_task(pinger()), asyncio.create_task(stopper())]
            try:
                async for message in ws:
                    last_seen[0] = time.monotonic()
                    try:
                        frame = json.loads(message)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(frame, dict):
                        continue
                    kind = frame.get("kind")
                    if kind == "ping":
                        await send({"kind": "pong", "ts": frame.get("ts")})
                    elif kind == "http":
                        task = asyncio.create_task(serve(frame))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
            finally:
                for task in [*background, *tasks]:
                    task.cancel()
            return opened_at

    async def handle_request(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Answer one ``kind: http`` frame with the service bearer (+ caller)."""

        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._caller_headers is not None:
            try:
                headers.update(self._caller_headers())
            except Exception:
                logger.exception("fleet channel caller headers failed")
        return await dispatch_request(frame, api_base=self.api_base, headers=headers, timeout=self.request_timeout)


# --- web-process wiring (fc-H3e) ---------------------------------------------

_CHANNEL: FleetChannel | None = None
_CHANNEL_LOCK = threading.Lock()


def get_channel() -> FleetChannel | None:
    return _CHANNEL


def loopback_api_base(bind_host: str, port: int) -> str:
    """URL of this process's own web API as seen from inside the node."""

    host = (bind_host or "").strip() or "127.0.0.1"
    if host in {"0.0.0.0", "::", "localhost"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{int(port)}"


def start_channel(
    *,
    api_base: str,
    token: str | None,
    caller_headers: Callable[[], dict[str, str]] | None = None,
    env: Mapping[str, str] | None = None,
) -> FleetChannel | None:
    """Start the process-wide channel when configured; None when it is off.

    Off unless LH_HARNESS_FLEET_URL is set, LH_HARNESS_FLEET_CHANNEL is not
    "0" and LH_HARNESS_FLEET_KEY is present (``load_config``). Idempotent.
    """

    global _CHANNEL
    config = load_config(env)
    if config is None:
        return None
    with _CHANNEL_LOCK:
        if _CHANNEL is None:
            _CHANNEL = FleetChannel(config, api_base=api_base, token=token, caller_headers=caller_headers)
            _CHANNEL.start()
        return _CHANNEL


def stop_channel() -> None:
    global _CHANNEL
    with _CHANNEL_LOCK:
        channel, _CHANNEL = _CHANNEL, None
    if channel is not None:
        channel.stop()
