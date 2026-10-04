"""fc-H3b: the fleet channel's allow-listed request dispatcher.

``dispatch_request`` turns one down ``kind: http`` frame into its up frame by
calling this node's own loopback web API. A small threaded HTTP server stands
in for that API and records what reached it.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from lh_harness.fleet.channel import call_loopback, dispatch_request, is_allowed

TOKEN = "test-web-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class _Api:
    """The node's loopback web API: records requests, answers JSON."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                api.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "body": json.loads(raw) if raw else None,
                    }
                )
                status = 200
                if self.path.startswith("/api/runs/slow/"):
                    time.sleep(1.0)
                if self.path.startswith("/api/runs/big/"):
                    payload = b'"' + b"x" * 2048 + b'"'
                elif self.path.startswith("/api/runs/missing/"):
                    status, payload = 404, b'{"detail":"run not found"}'
                else:
                    payload = json.dumps({"ok": True, "method": self.command, "path": self.path}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _answer
            do_POST = _answer
            do_DELETE = _answer

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def api():
    server = _Api()
    yield server
    server.close()


def _dispatch(api: _Api, frame: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(dispatch_request(frame, api_base=api.base, headers=AUTH, **kwargs))


def test_allow_list_matches_the_wire_contract() -> None:
    for path in ("/api/meta", "/api/queue", "/api/runs", "/api/runs/latest"):
        assert is_allowed("GET", path)
        assert not is_allowed("POST", path)
    for tail in ("snapshot", "events", "status", "latest"):
        assert is_allowed("get", f"/api/runs/r-1/{tail}")
        assert not is_allowed("POST", f"/api/runs/r-1/{tail}")
    for tail in ("stop", "resume", "abort", "instructions", "time_limit"):
        assert is_allowed("POST", f"/api/runs/20260930T024423Z_a42191e9/{tail}")
        assert not is_allowed("GET", f"/api/runs/r-1/{tail}")
    assert not is_allowed("GET", "/api/runs/./status")
    assert not is_allowed("POST", "/api/runs/a/b/stop")
    assert not is_allowed("PUT", "/api/meta")
    assert not is_allowed("GET", None)


def test_allowed_get_is_proxied_with_bearer_and_query(api: _Api) -> None:
    reply = _dispatch(api, {"id": "g1", "kind": "http", "method": "GET", "path": "/api/runs/latest", "query": {"limit": 5, "all": True}, "body": None})
    assert reply["id"] == "g1"
    assert reply["status"] == 200
    assert reply["body"]["path"] == "/api/runs/latest?limit=5&all=true"
    assert api.requests[-1]["auth"] == f"Bearer {TOKEN}"


def test_post_stop_is_proxied_with_its_body(api: _Api) -> None:
    run_id = "20260930T024423Z_a42191e9"
    reply = _dispatch(api, {"id": "p1", "kind": "http", "method": "POST", "path": f"/api/runs/{run_id}/stop", "query": {}, "body": {"why": "deploy"}})
    assert reply["status"] == 200
    assert api.requests[-1] == {"method": "POST", "path": f"/api/runs/{run_id}/stop", "auth": f"Bearer {TOKEN}", "body": {"why": "deploy"}}


def test_api_error_status_is_passed_through(api: _Api) -> None:
    reply = _dispatch(api, {"id": "m1", "kind": "http", "method": "GET", "path": "/api/runs/missing/status"})
    assert reply == {"id": "m1", "status": 404, "body": {"detail": "run not found"}}


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/settings"),
        ("GET", "/api/runs/r1/rounds/1/artifacts"),
        ("POST", "/api/runs/r1/snapshot"),
        ("GET", "/api/runs/r1/stop"),
        ("DELETE", "/api/queue/q-1"),
        ("POST", "/api/queue"),
        ("GET", "/api/runs/../meta"),
        ("POST", "/api/runs/%2e%2e/stop"),
        ("GET", "/api/meta?x=1"),
        ("GET", "/mcp"),
    ],
)
def test_disallowed_calls_answer_403_and_never_reach_the_api(api: _Api, method: str, path: str) -> None:
    reply = _dispatch(api, {"id": "d1", "kind": "http", "method": method, "path": path, "query": {}, "body": None})
    assert reply == {"id": "d1", "status": 403, "body": {"error": "not allowed"}}
    assert api.requests == []


def test_malformed_query_is_refused(api: _Api) -> None:
    reply = _dispatch(api, {"id": "q1", "kind": "http", "method": "GET", "path": "/api/meta", "query": {"x": {"nested": 1}}})
    assert reply["status"] == 400
    assert api.requests == []


def test_body_over_cap_is_413(api: _Api) -> None:
    status, body = call_loopback(api.base, "GET", "/api/runs/big/snapshot", "", None, AUTH, max_bytes=100)
    assert status == 413 and "error" in body


def test_slow_api_times_out_504(api: _Api) -> None:
    reply = _dispatch(api, {"id": "t1", "kind": "http", "method": "GET", "path": "/api/runs/slow/status"}, timeout=0.2)
    assert reply == {"id": "t1", "status": 504, "body": {"error": "timeout"}}


def test_unreachable_api_is_502() -> None:
    status, body = call_loopback("http://127.0.0.1:9", "GET", "/api/meta", "", None, {}, timeout=2)
    assert status in (502, 504) and "error" in body
