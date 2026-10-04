"""Fleet channel: one outbound, signed WebSocket from this node to fleet-admin.

Task fc-H3 (fleet-channel plan, Paxton 2026-09-30). fleet-admin cannot reach
harness nodes, so on registration the node dials out to
``wss://<host of LH_HARNESS_FLEET_URL>/harness/channel`` and serves
allow-listed API calls over that one connection (no inbound ports, no tunnel
software).

fc-H3a added configuration and the signed handshake (URL + headers); fc-H3b
adds the allow-listed request dispatcher (``dispatch_request``). fc-H3c..e add
the connection loop and the web-process wiring; until then nothing imports
this module.

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
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping
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
