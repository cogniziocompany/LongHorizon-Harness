"""fc-H3: the fleet channel client against an in-process fake fleet-admin.

The fake server speaks the wire contract (fleet-admin ``src/channel.js``):
it records the handshake headers, sends ``kind: http`` / ``ping`` frames and
reads the node's answers. A small threaded HTTP server stands in for the
node's own loopback web API.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import queue
import threading
import time
from typing import Any

import pytest

pytest.importorskip("websockets.asyncio.server")
from websockets.asyncio.server import serve

from lh_harness.fleet import channel as channel_mod
from lh_harness.fleet.channel import ChannelConfig, FleetChannel, backoff_delay, start_channel

from .test_channel_dispatch import _Api

KEY = "test-device-key"
TOKEN = "test-web-token"
NODE = "node-test"


# --- fakes ---------------------------------------------------------------------


class _Fleet:
    """Fake fleet-admin channel endpoint running its own loop in a thread."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.handshakes: list[dict[str, str]] = []
        self.received: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.connections: list[Any] = []
        self._connected = threading.Event()
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()
        self.port = 0

        async def handler(ws: Any) -> None:
            self.connections.append(ws)
            self._connected.set()
            async for message in ws:
                self.received.put(json.loads(message))

        def process_request(connection: Any, request: Any) -> Any:
            self.handshakes.append({k: v for k, v in request.headers.raw_items()})
            if self.refuse:
                return connection.respond(401, "bad signature\n")
            return None

        async def main() -> None:
            async with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
                self.port = server.sockets[0].getsockname()[1]
                self._stop = asyncio.Event()
                ready.set()
                await self._stop.wait()

        self.thread = threading.Thread(target=self.loop.run_until_complete, args=(main(),), daemon=True)
        self.thread.start()
        ready.wait(5)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/harness/channel"

    def wait_connections(self, count: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.connections) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(self.connections) >= count, f"expected {count} connection(s), saw {len(self.connections)}"

    def send(self, frame: dict[str, Any]) -> None:
        ws = self.connections[-1]
        asyncio.run_coroutine_threadsafe(ws.send(json.dumps(frame)), self.loop).result(5)

    def drop(self) -> None:
        ws = self.connections[-1]
        asyncio.run_coroutine_threadsafe(ws.close(), self.loop).result(5)

    def reply(self, match: Any, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                frame = self.received.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if match(frame):
                return frame
        raise AssertionError("no matching frame from the node")

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self._stop.set)
        self.thread.join(5)


@pytest.fixture()
def harness():
    api = _Api()
    fleet = _Fleet()
    node = FleetChannel(
        ChannelConfig(url=fleet.url, node=NODE, key=KEY),
        api_base=api.base,
        token=TOKEN,
        ping_interval=0.2,
        backoff_min=0.05,
        backoff_max=0.2,
    )
    node.start()
    fleet.wait_connections(1)
    yield fleet, api, node
    node.stop()
    fleet.close()
    api.close()


def _by_id(request_id: str):
    return lambda frame: frame.get("id") == request_id


# --- tests ------------------------------------------------------------------------


def test_handshake_headers_are_signed(harness) -> None:
    fleet, _, node = harness
    headers = {k.lower(): v for k, v in fleet.handshakes[0].items()}
    assert headers["x-fleet-host"] == NODE
    ts = int(headers["x-fleet-ts"])
    assert abs(time.time() - ts) <= 5
    expected = hmac.new(KEY.encode(), f"{NODE}.{ts}".encode(), hashlib.sha256).hexdigest()
    assert headers["x-fleet-signature"] == expected
    assert KEY not in json.dumps(fleet.handshakes)  # only the signature travels
    state = node.status()
    assert state["connected"] is True and state["since"]


def test_allowed_get_is_proxied_with_the_service_bearer(harness) -> None:
    fleet, api, _ = harness
    fleet.send({"id": "g1", "kind": "http", "method": "GET", "path": "/api/runs/latest", "query": {"limit": 5}, "body": None})
    reply = fleet.reply(_by_id("g1"))
    assert reply["status"] == 200
    assert reply["body"] == {"ok": True, "method": "GET", "path": "/api/runs/latest?limit=5"}
    assert api.requests[-1]["auth"] == f"Bearer {TOKEN}"


def test_disallowed_call_answers_403_over_the_channel(harness) -> None:
    fleet, api, _ = harness
    fleet.send({"id": "d1", "kind": "http", "method": "POST", "path": "/api/queue", "query": {}, "body": {}})
    assert fleet.reply(_by_id("d1")) == {"id": "d1", "status": 403, "body": {"error": "not allowed"}}
    assert api.requests == []


def test_post_stop_is_proxied_with_its_body(harness) -> None:
    fleet, api, _ = harness
    run_id = "20260930T024423Z_a42191e9"
    fleet.send({"id": "p1", "kind": "http", "method": "POST", "path": f"/api/runs/{run_id}/stop", "query": {}, "body": {"why": "deploy"}})
    reply = fleet.reply(_by_id("p1"))
    assert reply["status"] == 200
    assert api.requests[-1]["method"] == "POST"
    assert api.requests[-1]["path"] == f"/api/runs/{run_id}/stop"
    assert api.requests[-1]["body"] == {"why": "deploy"}
    assert api.requests[-1]["auth"] == f"Bearer {TOKEN}"


def test_ping_is_answered_and_node_pings(harness) -> None:
    fleet, _, _ = harness
    fleet.send({"kind": "ping", "ts": 1234})
    assert fleet.reply(lambda f: f.get("kind") == "pong") == {"kind": "pong", "ts": 1234}
    ping = fleet.reply(lambda f: f.get("kind") == "ping", timeout=2)
    assert isinstance(ping["ts"], int)


def test_reconnects_after_server_drop(harness) -> None:
    fleet, _, node = harness
    fleet.drop()
    fleet.wait_connections(2)
    deadline = time.monotonic() + 5
    while not node.status()["connected"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert node.status()["connected"] is True
    assert node.connects >= 2
    assert len(fleet.handshakes) >= 2
    fleet.send({"id": "after", "kind": "http", "method": "GET", "path": "/api/meta", "query": {}, "body": None})
    assert fleet.reply(_by_id("after"))["status"] == 200


def test_refused_handshake_records_error_and_keeps_retrying() -> None:
    fleet = _Fleet(refuse=True)
    node = FleetChannel(
        ChannelConfig(url=fleet.url, node=NODE, key=KEY), api_base="http://127.0.0.1:9", backoff_min=0.05, backoff_max=0.1
    )
    node.start()
    try:
        deadline = time.monotonic() + 5
        while len(fleet.handshakes) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(fleet.handshakes) >= 3
        state = node.status()
        assert state["connected"] is False
        assert "401" in (state["last_error"] or "")
        assert KEY not in (state["last_error"] or "")
    finally:
        node.stop()
        fleet.close()


def test_disabled_unless_configured(monkeypatch) -> None:
    assert start_channel(api_base="http://127.0.0.1:1", token=None, env={}) is None
    base = {"LH_HARNESS_FLEET_URL": "https://fleet.example", "LH_HARNESS_FLEET_KEY": KEY}
    assert start_channel(api_base="http://127.0.0.1:1", token=None, env={**base, "LH_HARNESS_FLEET_CHANNEL": "0"}) is None
    assert start_channel(api_base="http://127.0.0.1:1", token=None, env={"LH_HARNESS_FLEET_URL": "https://fleet.example"}) is None
    assert channel_mod.get_channel() is None


def test_meta_reports_channel_off_and_app_never_dials_without_bind_port(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from lh_harness.webapi import server as server_mod
    from lh_harness.webapi.server import create_app

    # The reporter is not under test and must not dial out either.
    monkeypatch.setattr(server_mod, "_maybe_start_fleet_reporter", lambda *a, **k: None)
    # Even with the fleet env set, an app built without bind_port (tests,
    # embedded dashboards) must not start the channel.
    monkeypatch.setenv("LH_HARNESS_FLEET_URL", "https://fleet.invalid")
    monkeypatch.setenv("LH_HARNESS_FLEET_KEY", KEY)
    monkeypatch.setenv("LH_HARNESS_FLEET_NODE", NODE)
    root = tmp_path / "runs"
    root.mkdir()
    app = create_app(runs_root=root)
    assert channel_mod.get_channel() is None
    meta = TestClient(app).get("/api/meta").json()
    assert meta["fleet_channel_connected"] is False
    assert meta["fleet_channel_since"] is None
    assert meta["fleet_channel_last_error"] is None


def test_meta_reports_a_live_channel(harness, tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from lh_harness.webapi.server import create_app

    _, _, node = harness
    monkeypatch.setattr(channel_mod, "_CHANNEL", node)
    root = tmp_path / "runs"
    root.mkdir()
    meta = TestClient(create_app(runs_root=root)).get("/api/meta").json()
    assert meta["fleet_channel_connected"] is True
    assert meta["fleet_channel_since"]


# --- unit ---------------------------------------------------------------------------


def test_channel_signs_as_the_configured_caller(monkeypatch) -> None:
    """With [callers] scoping on, proxied run-control calls carry a caller
    identity the server's own verifier accepts."""

    from lh_harness.caller_auth import resolve_rest_caller
    from lh_harness.webapi import server as server_mod

    specs = {"fleet-channel": {"secret_env": "LH_HARNESS_CALLER_FLEET_CHANNEL", "rest_run_control": True}}
    monkeypatch.setenv("LH_HARNESS_CALLER_FLEET_CHANNEL", "caller-secret-for-tests")
    monkeypatch.setenv("LH_HARNESS_FLEET_CHANNEL_CALLER", "fleet-channel")
    captured: dict[str, Any] = {}

    def fake_start(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(server_mod, "start_channel", fake_start)
    assert server_mod._maybe_start_fleet_channel("tok", "0.0.0.0", 8799, specs) is True
    assert captured["api_base"] == "http://127.0.0.1:8799"
    assert captured["token"] == "tok"
    headers = captured["caller_headers"]()
    assert resolve_rest_caller({k.lower(): v for k, v in headers.items()}, specs) == "fleet-channel"

    monkeypatch.delenv("LH_HARNESS_FLEET_CHANNEL_CALLER")
    assert server_mod._maybe_start_fleet_channel("tok", "127.0.0.1", 8799, specs) is True
    assert captured["caller_headers"] is None


def test_backoff_is_capped_with_jitter() -> None:
    for attempt in range(12):
        delay = backoff_delay(attempt)
        assert 1.0 <= delay <= 60.0
    assert backoff_delay(10) >= 30.0  # at the cap: jitter stays in its upper half
