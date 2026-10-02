"""Fleet channel: one outbound, signed WebSocket from this node to fleet-admin.

Task fc-H3 (fleet-channel plan, Paxton 2026-09-30). fleet-admin cannot reach
harness nodes, so on registration the node dials out to
``wss://<host of LH_HARNESS_FLEET_URL>/harness/channel`` and serves
allow-listed API calls over that one connection (no inbound ports, no tunnel
software).

This module is built in steps: fc-H3a adds configuration and the signed
handshake (URL + headers). Later steps add the request dispatcher, the
connection loop and the web-process wiring.

Wire contract (identical text in fc-H3 and fc-F1): the node sends headers
X-Fleet-Host: <node name>, X-Fleet-Ts: <unix seconds>, X-Fleet-Signature: hex
HMAC-SHA256(device key from LH_HARNESS_FLEET_KEY, "<host>.<ts>"). The server
rejects |now-ts|>300, an unknown/inactive host and a bad signature.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import time
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

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
