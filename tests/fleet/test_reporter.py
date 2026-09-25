"""Unit tests for the fleet reporter core."""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import logging
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
    _collect_round_content,
    _parse_labels,
    _read_report,
    get_reporter,
    post_report,
    post_round_content,
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


def test_configured_true_when_all_env_present(_isolate_reporter, caplog):
    """All four LH_HARNESS_FLEET_* vars set -> configured, silent start."""
    _setenv("http://127.0.0.1:1", "cfg-node", "cfg-key", "kind=test")
    with caplog.at_level(logging.WARNING, logger="lh_harness.fleet.reporter"):
        reporter = get_reporter(reset=True)
    assert reporter.configured is True
    assert reporter.missing_env == ()
    warns = [
        r for r in caplog.records
        if r.name == "lh_harness.fleet.reporter" and r.levelno >= logging.WARNING
    ]
    assert warns == []
    state = reporter.registration_state()
    assert state == {"ever_succeeded": False, "last_ok": None, "last_error": None}


def test_configured_false_warns_all_four_when_none_present(_isolate_reporter, caplog):
    """No LH_HARNESS_FLEET_* vars set -> not configured, WARN names all four."""
    _setenv(None, None, None, None)
    with caplog.at_level(logging.WARNING, logger="lh_harness.fleet.reporter"):
        reporter = get_reporter(reset=True)
    assert reporter.configured is False
    assert set(reporter.missing_env) == {
        "LH_HARNESS_FLEET_URL",
        "LH_HARNESS_FLEET_NODE",
        "LH_HARNESS_FLEET_KEY",
        "LH_HARNESS_FLEET_LABELS",
    }
    warns = [
        r for r in caplog.records
        if r.name == "lh_harness.fleet.reporter" and r.levelno >= logging.WARNING
    ]
    assert len(warns) == 1
    message = warns[0].getMessage()
    for name in (
        "LH_HARNESS_FLEET_URL",
        "LH_HARNESS_FLEET_NODE",
        "LH_HARNESS_FLEET_KEY",
        "LH_HARNESS_FLEET_LABELS",
    ):
        assert name in message


def test_configured_false_warns_only_missing_when_partial(_isolate_reporter, caplog):
    """A partial set is a misconfiguration: WARN names exactly the missing vars,
    and never any configured VALUE (names only, never values)."""
    _setenv("http://127.0.0.1:1", None, "cfg-secret-key-value", None)
    with caplog.at_level(logging.WARNING, logger="lh_harness.fleet.reporter"):
        reporter = get_reporter(reset=True)
    assert reporter.configured is False
    assert set(reporter.missing_env) == {
        "LH_HARNESS_FLEET_NODE",
        "LH_HARNESS_FLEET_LABELS",
    }
    warns = [
        r for r in caplog.records
        if r.name == "lh_harness.fleet.reporter" and r.levelno >= logging.WARNING
    ]
    assert len(warns) == 1
    message = warns[0].getMessage()
    # Only the missing names appear.
    assert "LH_HARNESS_FLEET_NODE" in message
    assert "LH_HARNESS_FLEET_LABELS" in message
    # The present names and every configured value stay out of the log line.
    assert "LH_HARNESS_FLEET_URL" not in message
    assert "LH_HARNESS_FLEET_KEY" not in message
    assert "http://127.0.0.1:1" not in message
    assert "cfg-secret-key-value" not in message


def test_hmac_signature_correct(http_server, _isolate_reporter):
    """Requests carry HMAC-SHA256 over the JSON body (what fleet-admin verifies).

    fleet-admin inflates a gzip body before its HMAC check (express.json
    ``verify`` receives the decoded buffer), so the signature must cover the
    JSON bytes, not the compressed stream. A signature over the gzip bytes is
    exactly the bug that produced 9,098 ``bad signature`` rejections.
    """
    _setenv(http_server, "test-node", "secret-key", None)
    reporter = get_reporter(reset=True)
    assert reporter.enabled
    assert reporter.node == "test-node"

    reporter.queue_heartbeat([], 0, 0)
    reporter.stop(timeout=5.0)

    assert _StubHandler.requests, "request should have reached stub"
    req = _StubHandler.requests[0]
    assert req["path"] == "/harness/heartbeat"
    assert req["headers"].get("Content-Encoding") == "gzip"
    inflated = gzip.decompress(req["raw"])
    assert _hmac_match(inflated, req["headers"], "secret-key", "test-node")
    # And explicitly NOT over the wire bytes (the pre-fix behaviour).
    assert not _hmac_match(req["raw"], req["headers"], "secret-key", "test-node")


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


# Audited failure envelope for the old full-run-list heartbeat (round_001):
# 532 runs -> 5,469,390 bytes JSON / 1,698,083 bytes gzipped, ~10,280 B per run.
_AUDITED_GZIP_BYTES = 1_698_083
_HEARTBEAT_WIRE_BUDGET = 256 * 1024  # 256 KiB, ~15% of the audited gzipped size

_TERMINAL_STATUSES = ("completed", "failed", "cancelled", "blocked", "incomplete")


def _run_dict(i: int, status: str, *, fat: bool = False) -> dict[str, Any]:
    """Build one registry-style run summary as produced by ``build_run_summary``."""
    run = {
        "id": f"20260922T000000Z_{i:08d}",
        "status": status,
        "updated_at": float(i),
        "log_dir": f"/home/harness/work/runs/run-{i:08d}/lh_harness",
        "model": "kimi-k2.7-code:cloud",
        "repo": "LongHorizon-Harness",
        "workspace": "/home/harness/work",
    }
    if fat:
        # Terminal runs on a long-lived node carry large task summaries
        # (~10 KB each on CT110); the heartbeat must not depend on their size.
        run["task"] = f"task {i} " + "x" * 10_000
    else:
        run["task"] = f"task {i}"
    return run


def _store_of_532(fat_terminal: bool = True) -> list[dict[str, Any]]:
    """532 runs mirroring the audited CT110 store: 2 active, 530 terminal."""
    runs = [
        _run_dict(0, "running", fat=fat_terminal),
        _run_dict(1, "waiting_approval", fat=fat_terminal),
    ]
    for i in range(530):
        runs.append(_run_dict(i + 2, _TERMINAL_STATUSES[i % len(_TERMINAL_STATUSES)], fat=fat_terminal))
    return runs


def test_heartbeat_excludes_terminal_runs_and_reports_counts(
    http_server, _isolate_reporter
):
    """runs[] carries only non-terminal runs; the whole store is aggregated."""
    _setenv(http_server, "bound-node", "bound-key", "kind=ct110")
    reporter = get_reporter(version="1.1.0", capacity=5, reset=True)
    assert reporter.enabled

    store = _store_of_532()
    reporter.queue_heartbeat(store, active=2, cap=5, queue_len=0)
    reporter.stop(timeout=30.0)

    requests = [r for r in _StubHandler.requests if r["path"] == "/harness/heartbeat"]
    assert len(requests) == 1
    body = requests[0]["body"]

    # Only the two non-terminal runs are serialized.
    sent_runs = body["runs"]
    assert {r["runId"] for r in sent_runs} == {
        "20260922T000000Z_00000000",
        "20260922T000000Z_00000001",
    }
    statuses = {r["status"] for r in sent_runs}
    assert statuses == {"running", "waiting_approval"}
    assert statuses.isdisjoint(set(_TERMINAL_STATUSES))

    # Aggregates cover the entire store.
    assert body["runsTotal"] == 532
    assert body["runsByStatus"] == {
        "running": 1,
        "waiting_approval": 1,
        "completed": 106,
        "failed": 106,
        "cancelled": 106,
        "blocked": 106,
        "incomplete": 106,
    }
    assert body["runsTruncated"] is False

    # Fleet-plane contract fields are unchanged.
    assert body["node"]["name"] == "bound-node"
    assert body["capacity"] == {"active": 2, "cap": 5}
    assert body["queueLen"] == 0


def test_heartbeat_payload_far_below_failure_envelope(
    http_server, _isolate_reporter
):
    """A 532-run store of terminal runs must stay far under the audited envelope.

    The old full-list heartbeat measured 5,469,390 B JSON / 1,698,083 B gzipped
    and was rejected with HTTP 413.  With terminal runs aggregated the wire
    payload must be small regardless of how large the terminal summaries are.
    """
    _setenv(http_server, "small-node", "small-key", None)
    reporter = get_reporter(version="1.1.0", capacity=5, reset=True)

    store = _store_of_532(fat_terminal=True)
    reporter.queue_heartbeat(store, active=2, cap=5, queue_len=0)
    reporter.stop(timeout=30.0)

    requests = [r for r in _StubHandler.requests if r["path"] == "/harness/heartbeat"]
    assert len(requests) == 1
    req = requests[0]
    body = req["body"]

    assert body["runsTotal"] == 532
    assert len(body["runs"]) == 2
    assert len(body["runs"]) <= 200
    # Far below the audited failure envelope: gzipped wire bytes, and the
    # decompressed JSON the fleet plane would parse.
    assert len(req["raw"]) < _HEARTBEAT_WIRE_BUDGET
    assert len(json.dumps(body).encode("utf-8")) < _HEARTBEAT_WIRE_BUDGET
    # Sanity anchor against the measured rejection: >8x smaller, not marginally.
    assert len(req["raw"]) * 8 < _AUDITED_GZIP_BYTES


def test_heartbeat_caps_active_runs_with_truncation_flag(
    http_server, _isolate_reporter, caplog
):
    """More than 200 active runs: keep the 200 most recent, flag truncation."""
    _setenv(http_server, "cap-node", "cap-key", None)
    reporter = get_reporter(version="1.1.0", capacity=5, reset=True)

    store = [_run_dict(i, "running") for i in range(250)]
    with caplog.at_level(logging.INFO, logger="lh_harness.fleet.reporter"):
        reporter.queue_heartbeat(store, active=5, cap=5, queue_len=0)
    reporter.stop(timeout=30.0)

    requests = [r for r in _StubHandler.requests if r["path"] == "/harness/heartbeat"]
    assert len(requests) == 1
    body = requests[0]["body"]

    sent_runs = body["runs"]
    assert len(sent_runs) == 200
    assert body["runsTruncated"] is True
    # The kept runs are the most recent by updated_at: ids 50..249.
    kept_ids = {int(r["runId"].split("_")[1]) for r in sent_runs}
    assert kept_ids == set(range(50, 250))
    # Aggregates still describe every run, not just the capped page.
    assert body["runsTotal"] == 250
    assert body["runsByStatus"] == {"running": 250}

    truncation_logs = [
        r for r in caplog.records
        if r.name == "lh_harness.fleet.reporter" and "capping active runs" in r.getMessage()
    ]
    assert truncation_logs, "active-run capping should be logged at INFO"


def test_heartbeat_counts_unknown_status_as_non_terminal(
    http_server, _isolate_reporter
):
    """Blank/unknown statuses are reported, not dropped: they count and stay in runs[]."""
    _setenv(http_server, "unknown-node", "unknown-key", None)
    reporter = get_reporter(version="1.1.0", capacity=2, reset=True)

    store = [
        {"id": "run-known", "status": "completed", "updated_at": 3.0},
        {"id": "run-blank", "status": "", "updated_at": 2.0},
        {"id": "run-odd", "status": "weird_future_state", "updated_at": 1.0},
    ]
    reporter.queue_heartbeat(store, active=0, cap=2, queue_len=0)
    reporter.stop(timeout=30.0)

    requests = [r for r in _StubHandler.requests if r["path"] == "/harness/heartbeat"]
    assert len(requests) == 1
    body = requests[0]["body"]
    assert body["runsTotal"] == 3
    # "completed" is terminal and excluded; the unknowns canonicalize to
    # non-terminal statuses and are serialized.
    sent_ids = {r["runId"] for r in body["runs"]}
    assert sent_ids == {"run-blank", "run-odd"}
    assert body["runsByStatus"]["completed"] == 1
    assert body["runsByStatus"]["idle"] == 1
    assert body["runsByStatus"]["weird_future_state"] == 1
    assert body["runsTruncated"] is False


def test_round_content_path_traversal(tmp_path):
    """Round content collection must stay inside the validated run directory."""
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "run-ok"
    log_dir = run_dir / "lh_harness"
    role_dir = log_dir / "role_orchestration"
    rounds_dir = role_dir / "rounds"
    round_dir = rounds_dir / "round_001"
    round_dir.mkdir(parents=True)

    # A symlink inside the rounds directory pointing outside the run dir must not
    # be followed.  The same is true for a file whose path escapes via traversal.
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    symlink_escape = round_dir / "escape.txt"
    try:
        symlink_escape.symlink_to(outside)
    except OSError:
        # Some test environments lack symlink support; skip traversal-via-link.
        pass

    (round_dir / "manager_plan.txt").write_text("plan", encoding="utf-8")
    (round_dir / "executor_raw_trajectory.jsonl").write_text(
        json.dumps({"step": 1}), encoding="utf-8"
    )
    traversal_file = round_dir / "..%2f..%2foutside.txt"
    traversal_file.write_text("bad name but inside", encoding="utf-8")

    artifacts, trajectories = _collect_round_content(runs_root, run_dir, 1)
    names = {a["name"] for a in artifacts}
    assert "manager_plan.txt" in names
    assert "escape.txt" not in names
    assert len(trajectories) == 1
    assert trajectories[0]["role"] == "executor"


def test_truncation_marker(tmp_path):
    """Files larger than the 8 MB cap are represented by a truncation marker."""
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "run-big"
    log_dir = run_dir / "lh_harness"
    role_dir = log_dir / "role_orchestration"
    rounds_dir = role_dir / "rounds"
    round_dir = rounds_dir / "round_001"
    round_dir.mkdir(parents=True)

    big = round_dir / "big.bin"
    big.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    (round_dir / "manager_plan.txt").write_text("small", encoding="utf-8")

    artifacts, _ = _collect_round_content(runs_root, run_dir, 1)
    by_name = {a["name"]: a["content"] for a in artifacts}
    assert by_name["big.bin"] == {"truncated": True, "bytes": 8 * 1024 * 1024 + 1}
    assert by_name["manager_plan.txt"]["text"] == "small"


def test_report_push(http_server, _isolate_reporter, tmp_path):
    """post_report reads logs/report.json and queues it as a round payload."""
    _setenv(http_server, "report-node", "report-key", None)
    get_reporter(version="0.4.0", capacity=1, reset=True)

    runs_root = tmp_path / "runs"
    run_dir = runs_root / "run-report"
    log_dir = run_dir / "lh_harness"
    (log_dir / "role_orchestration" / "rounds").mkdir(parents=True)
    report = {"schema_version": 2, "status": "complete", "task": "done"}
    (log_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8"
    )

    post_report(runs_root, run_dir)
    reporter = get_reporter()
    reporter.stop(timeout=5.0)

    round_requests = [r for r in _StubHandler.requests if r["path"] == "/harness/rounds"]
    assert len(round_requests) == 1
    body = round_requests[0]["body"]
    assert body["run_id"] == "run-report"
    assert body["report"] == report


def test_round_content_push_hook(http_server, _isolate_reporter, tmp_path):
    """post_round_content sends a complete round payload to fleet-admin."""
    _setenv(http_server, "round-node", "round-key", None)
    get_reporter(version="0.4.0", capacity=1, reset=True)

    runs_root = tmp_path / "runs"
    run_dir = runs_root / "run-round"
    log_dir = run_dir / "lh_harness"
    role_dir = log_dir / "role_orchestration"
    rounds_dir = role_dir / "rounds"
    round_dir = rounds_dir / "round_002"
    round_dir.mkdir(parents=True)
    (round_dir / "manager_plan.txt").write_text("plan text", encoding="utf-8")
    (round_dir / "executor_raw_trajectory.jsonl").write_text(
        json.dumps({"step": 1, "thinking": "deep thought"}),
        encoding="utf-8",
    )
    (round_dir / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")

    post_round_content(runs_root, run_dir, 2)
    reporter = get_reporter()
    reporter.stop(timeout=5.0)

    round_requests = [r for r in _StubHandler.requests if r["path"] == "/harness/rounds"]
    assert len(round_requests) == 1
    body = round_requests[0]["body"]
    assert body["run_id"] == "run-round"
    assert body["round"] == 2
    artifact_names = {a["name"] for a in body["artifacts"]}
    assert "manager_plan.txt" in artifact_names
    assert "screenshot.png" in artifact_names
    trajectory_roles = {t["role"] for t in body["trajectories"]}
    assert "executor" in trajectory_roles
