"""Unit tests for the fleet reporter core."""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import os
import queue
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from lh_harness.dashboard.state import ApprovalOption, DashboardState
from lh_harness.fleet.reporter import (
    FleetReporter,
    _parse_labels,
    get_reporter,
    wrap_event_sink,
    wrap_status_writer,
)
from lh_harness.supervisor.control_bus import ControlBus


class _StubHandler(BaseHTTPRequestHandler):
    """Capture incoming HMAC-signed gzip POSTs."""

    requests: list[dict[str, Any]] = []
    fail_next: int = 0
    last_path: str = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length)
        encoding = self.headers.get("content-encoding", "")
        body = gzip.decompress(raw) if encoding == "gzip" else raw
        _StubHandler.last_path = self.path
        if _StubHandler.fail_next > 0:
            _StubHandler.fail_next -= 1
            self.send_response(503)
            self.end_headers()
            return
        _StubHandler.requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "raw": raw,
                "body": json.loads(body.decode("utf-8")),
            }
        )
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


@pytest.fixture
def http_server():
    """Yield a local HTTP server and its base URL."""
    _StubHandler.requests.clear()
    _StubHandler.fail_next = 0
    _StubHandler.last_path = ""
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base
    server.shutdown()
    server.server_close()


def _hmac_match(raw_body: bytes, headers: dict[str, str], key: str, node: str) -> bool:
    """Verify HMAC-SHA256 over the raw request body matches the header."""
    assert headers.get("X-Fleet-Host") == node
    signature = headers.get("X-Fleet-Signature") or headers.get("x-fleet-signature")
    expected = hmac.new(key.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return signature == expected


def _getenv() -> dict[str, str | None]:
    """Return the original fleet env values so tests can restore them."""
    return {
        "LH_HARNESS_FLEET_URL": os.environ.get("LH_HARNESS_FLEET_URL"),
        "LH_HARNESS_FLEET_NODE": os.environ.get("LH_HARNESS_FLEET_NODE"),
        "LH_HARNESS_FLEET_KEY": os.environ.get("LH_HARNESS_FLEET_KEY"),
        "LH_HARNESS_FLEET_LABELS": os.environ.get("LH_HARNESS_FLEET_LABELS"),
    }


def _setenv(url: str | None, node: str | None, key: str | None, labels: str | None) -> None:
    """Set fleet env vars for the duration of a test."""
    env = {
        "LH_HARNESS_FLEET_URL": url,
        "LH_HARNESS_FLEET_NODE": node,
        "LH_HARNESS_FLEET_KEY": key,
        "LH_HARNESS_FLEET_LABELS": labels,
    }
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(autouse=True)
def _isolate_reporter():
    """Reset the reporter singleton and stash env before each test."""
    original = _getenv()
    get_reporter(reset=True)
    yield original
    for k, v in original.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_reporter(reset=True)


def test_disabled_when_url_unset(_isolate_reporter):
    """Without LH_HARNESS_FLEET_URL the reporter is a no-op."""
    _setenv(None, None, None, None)
    get_reporter(reset=True)
    reporter = FleetReporter()
    assert not reporter.enabled
    assert reporter.node == ""
    # Public methods must not raise and must not create any thread state.
    from lh_harness.fleet.reporter import EventEnvelope as _EventEnvelope

    reporter.queue_event(_EventEnvelope(run_id="r", type="t", ts=0.0))
    reporter.queue_heartbeat([], 0, 0)
    reporter.queue_round_content("r", 1, [], [], None)
    reporter.stop()


def test_hmac_signature_correct(http_server, _isolate_reporter):
    """Requests carry HMAC-SHA256 over the raw (gzipped) body."""
    _setenv(http_server, "test-node", "secret-key", None)
    reporter = get_reporter(reset=True)
    assert reporter.enabled
    assert reporter.node == "test-node"

    reporter.queue_heartbeat([], 0, 0)
    reporter.stop(timeout=5.0)

    assert _StubHandler.requests, "request should have reached stub"
    req = _StubHandler.requests[0]
    assert req["path"] == "/harness/heartbeat"
    assert _hmac_match(req["raw"], req["headers"], "secret-key", "test-node")
    assert req["headers"].get("Content-Encoding") == "gzip"


def test_batching_groups_events(http_server, _isolate_reporter):
    """Events are batched into one POST within ~2 seconds."""
    _setenv(http_server, "batch-node", "batch-key", None)
    reporter = get_reporter(reset=True)

    class _Env:
        run_id: str = "r"
        type: str = "run.started"
        ts: float = 1.0

    def _make():
        env = _Env()
        env.ts = time.time()
        return env

    sink = wrap_event_sink(lambda e: None)
    for _ in range(5):
        env = _make()
        sink({"run_id": "run-1", "type": "run.started", "ts": env.ts})
    time.sleep(2.5)
    reporter.stop(timeout=5.0)

    event_requests = [r for r in _StubHandler.requests if r["path"] == "/harness/events"]
    assert len(event_requests) == 1
    body = event_requests[0]["body"]
    assert isinstance(body, list)
    assert len(body) == 5
    assert all(ev["run_id"] == "run-1" for ev in body)


def test_non_blocking_on_failure(_isolate_reporter, monkeypatch):
    """A permanently unreachable fleet URL must not block the caller."""
    _setenv("http://127.0.0.1:1", "bad-node", "bad-key", None)
    reporter = get_reporter(reset=True)
    assert reporter.enabled

    from lh_harness.fleet.reporter import EventEnvelope as _EventEnvelope

    start = time.monotonic()
    for _ in range(100):
        reporter.queue_event(_EventEnvelope(run_id="r", type="t", ts=0.0))
    elapsed = time.monotonic() - start
    # Enqueue should be instant; retries happen on the daemon thread.
    assert elapsed < 1.0

    # Allow the worker a chance to retry and drop.
    time.sleep(5.0)
    reporter.stop(timeout=2.0)


def test_labels_parsing_and_heartbeat_shape(http_server, _isolate_reporter):
    """Optional labels are parsed and heartbeat has the documented shape."""
    _setenv(http_server, "label-node", "label-key", "kind=ct110,repo=LongHorizon-Harness")
    reporter = get_reporter(version="0.1.0", capacity=3, reset=True)
    assert reporter.labels == {"kind": "ct110", "repo": "LongHorizon-Harness"}

    reporter.queue_heartbeat(
        [
            {
                "id": "run-42",
                "status": "running",
                "active_round": 2,
                "active_role": "executor",
                "model": "kimi-k2.7-code:cloud",
                "repo": "LongHorizon-Harness",
                "workspace": "/home/harness/work",
                "youtrack_issue_id": "MCP-123",
            }
        ],
        active=1,
        cap=3,
        queue_len=0,
    )
    reporter.stop(timeout=5.0)

    req = _StubHandler.requests[0]
    body = req["body"]
    assert body["node"]["name"] == "label-node"
    assert body["node"]["version"] == "0.1.0"
    assert body["node"]["labels"] == {"kind": "ct110", "repo": "LongHorizon-Harness"}
    assert body["capacity"] == {"active": 1, "cap": 3}
    assert body["queueLen"] == 0
    runs = body["runs"]
    assert len(runs) == 1
    assert runs[0]["runId"] == "run-42"
    assert runs[0]["status"] == "running"
    assert runs[0]["round"] == 2
    assert runs[0]["activeRole"] == "executor"
    assert runs[0]["youtrackIssueId"] == "MCP-123"


def test_event_hook_trims_transcripts(_isolate_reporter):
    """Public event payloads are trimmed, never carrying transcripts."""
    _setenv("http://127.0.0.1:1", "trim-node", "trim-key", None)
    reporter = get_reporter(reset=True)
    captured: list[dict[str, Any]] = []
    original = {"run_id": "r", "type": "round.executor.completed", "ts": 1.0}

    # Replace the worker so we can inspect what is queued without a server.
    def _capture(endpoint: str, payload: Any, *, gzip_body: bool = True) -> None:
        captured.append({"endpoint": endpoint, "payload": payload})

    reporter._post = _capture  # type: ignore[method-assign]

    sink = wrap_event_sink(lambda e: None)
    sink(
        {
            **original,
            "round": 3,
            "role": "executor",
            "status": "completed",
            "payload": {
                "transcript": "secret thinking text",
                "artifact_count": 2,
                "final_response": "large response" * 1000,
            },
        }
    )

    assert len(captured) == 1
    events = captured[0]["payload"]
    assert isinstance(events, list) and len(events) == 1
    ev = events[0]
    assert ev["type"] == "round.executor.completed"
    assert ev["round"] == 3
    assert ev["role"] == "executor"
    assert "transcript" not in ev["payload"]
    assert ev["payload"]["artifact_count"] == 2


def test_status_writer_emits_run_status(_isolate_reporter):
    """Wrapping write_status emits a run.status event."""
    _setenv("http://127.0.0.1:1", "status-node", "status-key", None)
    reporter = get_reporter(reset=True)
    captured: list[dict[str, Any]] = []

    def _capture(endpoint: str, payload: Any, *, gzip_body: bool = True) -> None:
        captured.append({"endpoint": endpoint, "payload": payload})

    reporter._post = _capture  # type: ignore[method-assign]

    calls: list[dict[str, Any]] = []
    wrapped = wrap_status_writer(calls.append)
    wrapped({"run_id": "run-99", "status": "running", "round": 5, "active_role": "manager"})

    assert len(calls) == 1
    assert calls[0]["status"] == "running"
    status_events = [c for c in captured if c["endpoint"] == "/harness/events"]
    assert len(status_events) == 1
    ev = status_events[0]["payload"][0]
    assert ev["type"] == "run.status"
    assert ev["run_id"] == "run-99"
    assert ev["status"] == "running"
    assert ev["round"] == 5
    assert ev["role"] == "manager"


def test_heartbeat_scheduling(http_server, _isolate_reporter):
    """Heartbeat callback is invoked on the reporter thread every interval."""
    _setenv(http_server, "hb-node", "hb-key", "kind=local")
    reporter = get_reporter(version="0.2.0", capacity=2, reset=True)
    reporter._heartbeat_interval = 0.3

    calls: list[tuple[list[dict[str, Any]], int, int, int]] = []

    def _heartbeat() -> tuple[list[dict[str, Any]], int, int, int]:
        calls.append(([], 1, 2, 0))
        return [], 1, 2, 0

    reporter.register_heartbeat(_heartbeat)
    time.sleep(1.0)
    reporter.stop(timeout=5.0)

    heartbeat_requests = [r for r in _StubHandler.requests if r["path"] == "/harness/heartbeat"]
    assert len(heartbeat_requests) >= 2
    body = heartbeat_requests[0]["body"]
    assert body["node"]["name"] == "hb-node"
    assert body["node"]["version"] == "0.2.0"
    assert body["capacity"] == {"active": 1, "cap": 2}
    assert body["queueLen"] == 0
    assert len(calls) >= 2


def test_manager_append_event_reaches_fleet(http_server, _isolate_reporter, tmp_path):
    """Calling manager._append_event sends a public EventEnvelope to fleet."""
    _setenv(http_server, "mgr-node", "mgr-key", None)
    get_reporter(version="0.3.0", capacity=1, reset=True)

    from lh_harness.manager import _append_event

    role_dir = tmp_path / "run-mgr" / "lh_harness" / "role_orchestration"
    role_dir.mkdir(parents=True)
    events_path = role_dir / "events.jsonl"
    _append_event(
        events_path,
        "manager_round_start",
        {"round": 7, "role": "manager", "prompt_chars": 120},
    )
    reporter = get_reporter()
    reporter.stop(timeout=5.0)

    event_requests = [r for r in _StubHandler.requests if r["path"] == "/harness/events"]
    assert len(event_requests) == 1
    body = event_requests[0]["body"]
    assert len(body) == 1
    ev = body[0]
    assert ev["run_id"] == "run-mgr"
    assert ev["type"] == "manager_round_start"
    assert ev["round"] == 7
    assert ev["role"] == "manager"
    assert ev["status"] == "running"
    assert ev["payload"].get("prompt_chars") == 120


def test_control_bus_status_event(_isolate_reporter):
    """ControlBus.write_status emits a run.status event when fleet is enabled."""
    _setenv("http://127.0.0.1:1", "bus-node", "bus-key", None)
    reporter = get_reporter(reset=True)
    captured: list[dict[str, Any]] = []

    def _capture(endpoint: str, payload: Any, *, gzip_body: bool = True) -> None:
        captured.append({"endpoint": endpoint, "payload": payload})

    reporter._post = _capture  # type: ignore[method-assign]

    bus = ControlBus("/tmp/fleet-test-control-bus")
    bus.write_status({"run_id": "run-bus", "status": "stopping", "round": 3, "active_role": "auditor"})

    status_events = [c for c in captured if c["endpoint"] == "/harness/events"]
    assert len(status_events) == 1
    ev = status_events[0]["payload"][0]
    assert ev["type"] == "run.status"
    assert ev["run_id"] == "run-bus"
    assert ev["status"] == "stopping"
    assert ev["round"] == 3
    assert ev["role"] == "auditor"


def test_gate_approval_events(tmp_path, _isolate_reporter):
    """Creating and resolving an approval emits approval_created/resolved events."""
    _setenv("http://127.0.0.1:1", "gate-node", "gate-key", None)
    reporter = get_reporter(reset=True)
    captured: list[dict[str, Any]] = []

    def _capture(endpoint: str, payload: Any, *, gzip_body: bool = True) -> None:
        captured.append({"endpoint": endpoint, "payload": payload})

    reporter._post = _capture  # type: ignore[method-assign]

    run_dir = tmp_path / "run-gate"
    log_dir = run_dir / "lh_harness"
    role_dir = log_dir / "role_orchestration"
    role_dir.mkdir(parents=True)
    state = DashboardState(str(log_dir), control_enabled=True)
    approval = state.create_approval(
        title="Continue?",
        options=[ApprovalOption(value="continue", label="Continue")],
        context={"trigger": "end_of_round", "round_index": 4},
    )
    state.resolve_approval(
        approval.approval_id,
        action="continue",
        reason="proceed",
    )
    # The command is written synchronously; the worker loop applies it via
    # _apply_pending_resolutions.  Trigger that here so the resolved event
    # is emitted in this test.
    state._apply_pending_resolutions()

    events = [c for c in captured if c["endpoint"] == "/harness/events"]
    assert len(events) == 2
    created = events[0]["payload"][0]
    assert created["type"] == "approval_created"
    assert created["run_id"] == "run-gate"
    assert created["round"] == 4
    assert created["payload"].get("trigger") == "end_of_round"
    assert created["payload"].get("approval_id") == approval.approval_id
    resolved = events[1]["payload"][0]
    assert resolved["type"] == "approval_resolved"
    assert resolved["payload"].get("action") == "continue"


def test_parse_labels():
    assert _parse_labels("kind=ct110,repo=foo") == {"kind": "ct110", "repo": "foo"}
    assert _parse_labels("") == {}
    assert _parse_labels("  a = b , c=d  ") == {"a": "b", "c": "d"}
    assert _parse_labels("no-equals") == {}
