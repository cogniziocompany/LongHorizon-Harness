"""Unit tests for the Seq logging handler (src/lh_harness/seq_logging.py)."""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from lh_harness import __version__
from lh_harness.seq_logging import (
    SeqLogHandler,
    _clef_level,
    install_seq_logging,
)
import lh_harness.seq_logging as seq_logging


class _StubSeqHandler(BaseHTTPRequestHandler):
    """Capture incoming gzip POSTs of newline-delimited CLEF."""

    posts: list[dict[str, Any]] = []
    attempts: int = 0
    fail_503_next: int = 0
    fail_400_next: int = 0

    def log_message(self, _format: str, *_args: Any) -> None:
        pass

    def do_POST(self) -> None:
        _StubSeqHandler.attempts += 1
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length)
        headers = dict(self.headers)
        if _StubSeqHandler.fail_400_next > 0:
            _StubSeqHandler.fail_400_next -= 1
            self.send_response(400)
            self.end_headers()
            return
        if _StubSeqHandler.fail_503_next > 0:
            _StubSeqHandler.fail_503_next -= 1
            self.send_response(503)
            self.end_headers()
            return
        encoding = ""
        for k, v in headers.items():
            if k.lower() == "content-encoding":
                encoding = v.lower()
        text = gzip.decompress(raw).decode("utf-8") if encoding == "gzip" else raw.decode("utf-8")
        events = [json.loads(line) for line in text.splitlines() if line.strip()]
        _StubSeqHandler.posts.append(
            {"path": self.path, "headers": headers, "events": events}
        )
        self.send_response(201)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"MinimumLevelAccepted":null}')


@pytest.fixture
def http_server():
    """Yield a local stub Seq server and its base URL."""
    _StubSeqHandler.posts.clear()
    _StubSeqHandler.attempts = 0
    _StubSeqHandler.fail_503_next = 0
    _StubSeqHandler.fail_400_next = 0
    server = HTTPServer(("127.0.0.1", 0), _StubSeqHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base
    server.shutdown()
    server.server_close()


_SEQ_ENV_VARS = (
    "SEQ_URL",
    "SEQ_API_KEY",
    "SEQ_MIN_LEVEL",
    "LH_HARNESS_FLEET_NODE",
    "LH_HARNESS_FLEET_LABELS",
)


def _setenv(**env: str | None) -> None:
    """Set seq-related env vars for the duration of a test (None clears)."""
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _reset_singleton() -> None:
    """Close and clear the singleton WITHOUT recreating it.

    get_seq_handler(reset=True) recreates a handler from ambient env, which
    would pre-populate the singleton with a disabled handler between tests.
    """
    handler = seq_logging._seq_handler
    if handler is not None:
        handler.close()
    seq_logging._seq_handler = None


@pytest.fixture(autouse=True)
def _isolate_handler():
    """Reset the handler singleton and stash env before each test."""
    original = {k: os.environ.get(k) for k in _SEQ_ENV_VARS}
    root = logging.getLogger()
    root_handlers_before = list(root.handlers)
    _reset_singleton()
    yield
    # Detach any Seq handlers the test attached to the root logger.
    for h in list(root.handlers):
        if h not in root_handlers_before and isinstance(h, SeqLogHandler):
            root.removeHandler(h)
    _reset_singleton()
    for k, v in original.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _record(
    message: str,
    level: int = logging.INFO,
    *,
    name: str = "test.seq",
    args: tuple = (),
    exc_info: Any = None,
    **extras: Any,
) -> logging.LogRecord:
    rec = logging.LogRecord(name, level, __file__, 10, message, args, exc_info)
    for k, v in extras.items():
        setattr(rec, k, v)
    return rec


def _header(headers: dict[str, str], name: str) -> str | None:
    """Case-insensitive header lookup (urllib normalizes header case)."""
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def _seq_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "lh_harness.seq_logging" and r.levelno >= logging.WARNING
    ]


def test_disabled_when_seq_url_unset(caplog):
    """Without SEQ_URL the handler is a no-op: no thread, emit() no-ops."""
    _setenv(SEQ_URL=None, SEQ_API_KEY=None, SEQ_MIN_LEVEL=None)
    with caplog.at_level(logging.WARNING, logger="lh_harness.seq_logging"):
        handler = SeqLogHandler()
    assert not handler.enabled
    assert handler._thread is None
    # Public methods must not raise and must not create any thread state.
    handler.emit(_record("nothing happens"))
    handler.stop()
    assert _seq_warnings(caplog) == []


def test_install_noops_when_seq_url_unset():
    """install_seq_logging mirrors the fleet reporter: disabled without SEQ_URL."""
    _setenv(SEQ_URL=None)
    root = logging.getLogger()
    before = list(root.handlers)
    assert install_seq_logging() is None
    assert list(root.handlers) == before


def test_enabled_with_url_only_sends_without_api_key(http_server, caplog):
    """URL only (no key): events ship unauthenticated, no config warning.

    Per the 2026-09-14 amendment the dev Seq server accepts unauthenticated
    ingestion, so a missing SEQ_API_KEY must never block or warn.
    """
    _setenv(SEQ_URL=http_server, SEQ_API_KEY=None)
    with caplog.at_level(logging.WARNING, logger="lh_harness.seq_logging"):
        handler = SeqLogHandler()
        assert handler.enabled
        handler.emit(_record("hello seq", logging.INFO, name="my.mod"))
        handler.stop(timeout=5.0)
    assert _seq_warnings(caplog) == []
    assert len(_StubSeqHandler.posts) == 1
    post = _StubSeqHandler.posts[0]
    assert post["path"] == "/api/events/raw?clef"
    assert _header(post["headers"], "X-Seq-ApiKey") is None
    assert post["events"][0]["@m"] == "hello seq"


def test_api_key_present_sends_x_seq_apikey_header(http_server):
    """When a key IS present it is attached to every batch."""
    _setenv(SEQ_URL=http_server, SEQ_API_KEY="test-key-123")
    handler = SeqLogHandler()
    assert handler.enabled
    handler.emit(_record("keyed event"))
    handler.stop(timeout=5.0)
    assert len(_StubSeqHandler.posts) == 1
    assert _header(_StubSeqHandler.posts[0]["headers"], "X-Seq-ApiKey") == "test-key-123"


def test_clef_level_mapping():
    """Every Python level maps to the closest Seq (Serilog) level."""
    assert _clef_level(logging.DEBUG) == "Debug"
    assert _clef_level(logging.INFO) == "Information"
    assert _clef_level(logging.WARNING) == "Warning"
    assert _clef_level(logging.ERROR) == "Error"
    assert _clef_level(logging.CRITICAL) == "Fatal"
    # Boundaries/custom levels fall to the closest lower bucket.
    assert _clef_level(0) == "Verbose"
    assert _clef_level(15) == "Debug"
    assert _clef_level(35) == "Warning"


def test_clef_payload_shape_and_enrichment(http_server):
    """One record becomes one compact CLEF object with standard properties."""
    _setenv(
        SEQ_URL=http_server,
        SEQ_API_KEY=None,
        LH_HARNESS_FLEET_NODE="test-node",
        LH_HARNESS_FLEET_LABELS="kind=test,repo=LongHorizon-Harness",
    )
    handler = SeqLogHandler()
    assert handler.enabled
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        exc_info = sys.exc_info()
    handler.emit(
        _record(
            "run %s failed",
            logging.ERROR,
            name="lh_harness.manager",
            args=("run-1",),
            exc_info=exc_info,
            run_id="run-1",
            weird=object(),  # not JSON-serializable: must be coerced to str
        )
    )
    handler.stop(timeout=5.0)

    assert len(_StubSeqHandler.posts) == 1
    assert len(_StubSeqHandler.posts[0]["events"]) == 1
    ev = _StubSeqHandler.posts[0]["events"][0]
    assert ev["@t"].endswith("Z")
    assert ev["@l"] == "Error"
    assert ev["@m"] == "run run-1 failed"
    assert ev["@mt"] == "run %s failed"
    assert ev["logger"] == "lh_harness.manager"
    assert ev["app"] == "lh-harness"
    assert ev["version"] == __version__
    assert ev["node"] == "test-node"
    # Fleet labels are reused, never a second labelling scheme.
    assert ev["kind"] == "test"
    assert ev["repo"] == "LongHorizon-Harness"
    # Stack traces are searchable via @x.
    assert "ValueError: boom" in ev["@x"]
    # Extra record attributes become CLEF properties; unsafe values stringify.
    assert ev["run_id"] == "run-1"
    assert isinstance(ev["weird"], str)


def test_batching_single_gzip_post(http_server):
    """Records emitted within the batch window ship as one gzipped NDJSON POST."""
    _setenv(SEQ_URL=http_server, SEQ_API_KEY=None)
    handler = SeqLogHandler()
    for i in range(5):
        handler.emit(_record(f"event {i}"))
    handler.stop(timeout=5.0)

    assert len(_StubSeqHandler.posts) == 1
    post = _StubSeqHandler.posts[0]
    assert _header(post["headers"], "Content-Encoding") == "gzip"
    assert _header(post["headers"], "Content-Type") == "application/vnd.serilog.clef"
    events = post["events"]
    assert len(events) == 5
    assert [ev["@m"] for ev in events] == [f"event {i}" for i in range(5)]


def test_4xx_not_retried(http_server, caplog):
    """A 4xx rejection is not retried and warns exactly once."""
    _StubSeqHandler.fail_400_next = 10
    handler = SeqLogHandler(seq_url=http_server)
    handler._last_warned = -1e9  # guarantee the first warning fires
    before = _StubSeqHandler.attempts
    with caplog.at_level(logging.WARNING, logger="lh_harness.seq_logging"):
        handler._flush([{"@m": "rejected"}])
    assert _StubSeqHandler.attempts - before == 1
    warns = _seq_warnings(caplog)
    assert len(warns) == 1
    assert "400" in warns[0].getMessage()
    handler.stop(timeout=5.0)


def test_5xx_retried_until_success(http_server, monkeypatch):
    """Transient 5xx responses are retried with the bounded backoff."""
    monkeypatch.setattr(seq_logging, "_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    _StubSeqHandler.fail_503_next = 2
    handler = SeqLogHandler(seq_url=http_server)
    before = _StubSeqHandler.attempts
    handler._flush([{"@m": "eventually"}])
    assert _StubSeqHandler.attempts - before == 3
    assert len(_StubSeqHandler.posts) == 1
    assert _StubSeqHandler.posts[0]["events"][0]["@m"] == "eventually"
    handler.stop(timeout=5.0)


def test_retry_gives_up_after_max_attempts(http_server, caplog, monkeypatch):
    """A permanently failing Seq is dropped after _MAX_RETRIES attempts."""
    monkeypatch.setattr(seq_logging, "_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    _StubSeqHandler.fail_503_next = 10
    handler = SeqLogHandler(seq_url=http_server)
    handler._last_warned = -1e9
    before = _StubSeqHandler.attempts
    with caplog.at_level(logging.WARNING, logger="lh_harness.seq_logging"):
        handler._flush([{"@m": "doomed"}])
    assert _StubSeqHandler.attempts - before == seq_logging._MAX_RETRIES
    warns = _seq_warnings(caplog)
    assert len(warns) == 1
    assert "could not POST events" in warns[0].getMessage()
    handler.stop(timeout=5.0)


def test_queue_full_warns_once_and_drops(caplog, monkeypatch):
    """A full queue drops events and warns once, never blocking the caller."""
    monkeypatch.setattr(seq_logging, "_MAX_QUEUE_SIZE", 2)

    def _stalled_worker(self: SeqLogHandler) -> None:
        # Prevent the worker from draining so the queue actually fills.
        if self._stop_event is not None:
            self._stop_event.wait(timeout=5.0)

    monkeypatch.setattr(SeqLogHandler, "_worker", _stalled_worker)
    handler = SeqLogHandler(seq_url="http://127.0.0.1:1")
    assert handler.enabled
    assert handler._queue is not None
    handler._last_warned = -1e9

    with caplog.at_level(logging.WARNING, logger="lh_harness.seq_logging"):
        for i in range(20):
            handler.emit(_record(f"flood {i}"))

    assert handler._queue.qsize() == 2  # extras were dropped, never raised
    warns = [r for r in _seq_warnings(caplog) if "queue full" in r.getMessage()]
    assert len(warns) == 1
    handler.stop(timeout=5.0)


def test_min_level_filters_records(http_server):
    """SEQ_MIN_LEVEL controls the handler level; lower records are dropped."""
    _setenv(SEQ_URL=http_server, SEQ_API_KEY=None, SEQ_MIN_LEVEL="WARNING")
    handler = SeqLogHandler()
    assert handler.level == logging.WARNING
    test_logger = logging.getLogger("test.seq.minlevel")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)
    test_logger.propagate = False
    try:
        test_logger.info("below the bar")
        test_logger.debug("way below the bar")
        test_logger.warning("over the bar")
    finally:
        test_logger.removeHandler(handler)
    handler.stop(timeout=5.0)

    assert len(_StubSeqHandler.posts) == 1
    events = _StubSeqHandler.posts[0]["events"]
    assert len(events) == 1
    assert events[0]["@m"] == "over the bar"
    assert events[0]["@l"] == "Warning"


def test_install_attaches_singleton_to_root(http_server):
    """install_seq_logging attaches one singleton handler to the root logger."""
    _setenv(SEQ_URL=http_server, SEQ_API_KEY=None)
    root = logging.getLogger()
    first = install_seq_logging()
    assert first is not None and first.enabled
    assert first in root.handlers
    second = install_seq_logging()
    assert second is first
    assert root.handlers.count(first) == 1
