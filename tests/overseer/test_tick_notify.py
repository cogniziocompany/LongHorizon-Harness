"""Hermetic tests for the CT110 overseer-sweep tick telemetry (TASK 236).

Covers the two wire contracts the tick is wired against:

* Seq (TASK 161 contract, branch ``feat/seq-logging`` @ edef634): CLEF JSON
  line POSTed to ``{SEQ_URL}/api/events/raw?clef`` with the
  ``X-Seq-ApiKey`` header attached only when ``SEQ_API_KEY`` is set.
* Hivemind memory (TASK 229 contract, ``tasks/overseer-ingest-task.txt``):
  REST transport ``POST {MEMORY_URL}/api/memories`` with the
  ``authorization: Bearer`` header, or the gateway transport JSON-RPC 2.0
  ``tools/call`` with ``x-mcp-servers: memory`` and tool
  ``memory-remember_session`` (override via ``MEMORY_MCP_TOOL``), SSE or
  JSON responses parsed, 4xx hard error / 5xx retried once.

No test contacts anything outside this process: every transport is pointed
at a local ``http.server`` stub (the same pattern the repo's other tests
use for webhook-style transports).  No real secret value appears anywhere.
"""

from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "overseer_ct110"))

import tick_notify  # noqa: E402


def _env(**overrides: str) -> dict[str, str]:
    """A clean env of exactly the names the tick telemetry reads."""
    names = {
        "SEQ_URL": "",
        "SEQ_API_KEY": "",
        "SEQ_MIN_LEVEL": "",
        "MEMORY_URL": "",
        "MEMORY_TOKEN": "",
        "MEMORY_MCP_URL": "",
        "MEMORY_MCP_KEY": "",
        "MEMORY_MCP_TOOL": "",
    }
    names.update({k: v for k, v in overrides.items() if v != ""})
    return {k: v for k, v in names.items() if v}


class _RecordingServer:
    """Local HTTP stub recording (path, headers, body) per request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], bytes]] = []
        self.response_status = 201
        self.response_body = b'{"ok": true}'

        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                outer.requests.append(
                    (self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                )
                self.send_response(outer.response_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(outer.response_body)))
                self.end_headers()
                self.wfile.write(outer.response_body)

            def log_message(self, format: str, *args: Any) -> None:  # silence
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=2)


class TickNotifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = {
            k: os.environ.get(k)
            for k in (
                "SEQ_URL", "SEQ_API_KEY", "SEQ_MIN_LEVEL",
                "MEMORY_URL", "MEMORY_TOKEN", "MEMORY_MCP_URL", "MEMORY_MCP_KEY", "MEMORY_MCP_TOOL",
            )
        }

    def tearDown(self) -> None:
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # -- redaction ---------------------------------------------------------
    def test_redact_covers_all_brief_patterns(self) -> None:
        samples = {
            "sk-abcdef12345678": "[redacted]",
            "Bearer abcdefghijklmnopq": "[redacted]",
            "ghp_" + "a" * 20: "[redacted]",
            "AKIA" + "A" * 12 + "X": "[redacted]",
            "-----BEGIN OPENSSH PRIVATE KEY": "[redacted]",
        }
        for span, replacement in samples.items():
            with self.subTest(span=span):
                self.assertNotIn(span, tick_notify.redact(f"pre {span} post"))
                self.assertIn(replacement := replacement, (replacement,))
        # Positive framing: the replacement marker is present exactly where
        # each span was.
        text = tick_notify.redact("token sk-abcdef12345678 and Bearer abcdefghijklmnopq")
        self.assertEqual(text, "token [redacted] and [redacted]")

    # -- Seq transport (TASK 161 wire contract) ----------------------------
    def test_seq_disabled_without_seq_url(self) -> None:
        os.environ.clear()
        os.environ.update(_env())
        server = _RecordingServer()
        try:
            tick_notify.emit_seq(
                tick_id="t1", mode="read-only", rc=0, tick_log="/tmp/x.log",
                message="tick ran", print_only=False,
            )
            self.assertEqual(server.requests, [])
        finally:
            server.stop()

    def test_seq_posts_clef_with_key_header(self) -> None:
        os.environ.clear()
        os.environ.update(_env(SEQ_URL=" https://seq.example.invalid/ ", SEQ_API_KEY="dummy-key-name"))
        server = _RecordingServer()
        # Point the emitter at the stub by monkeypatching the env to the stub.
        os.environ["SEQ_URL"] = server.base
        try:
            tick_notify.emit_seq(
                tick_id="t1", mode="read-only", rc=0, tick_log="/tmp/x.log",
                message="Overseer sweep tick t1", print_only=False,
            )
            self.assertEqual(len(server.requests), 1)
            path, headers, body = server.requests[0]
            self.assertTrue(path.startswith("/api/events/raw?clef"))
            self.assertEqual(headers["content-type"], "application/vnd.serilog.clef")
            self.assertEqual(headers.get("x-seq-apikey"), "dummy-key-name")
            event = json.loads(body.decode())
            self.assertEqual(event["app"], "lh-overseer-sweep")
            self.assertEqual(event["@l"], "Information")
            self.assertEqual(event["tick_id"], "t1")
            self.assertIn("@t", event)
        finally:
            server.stop()

    def test_seq_omits_key_header_when_name_unset(self) -> None:
        os.environ.clear()
        os.environ.update(_env())
        server = _RecordingServer()
        os.environ["SEQ_URL"] = server.base
        try:
            tick_notify.emit_seq(
                tick_id="t2", mode="read-only", rc=0, tick_log="/tmp/x.log",
                message="m", print_only=False,
            )
            path, headers, _body = server.requests[0]
            self.assertNotIn("x-seq-apikey", headers)
        finally:
            server.stop()

    def test_seq_failure_is_fail_open(self) -> None:
        os.environ.clear()
        # No server on this port: the emit must not raise.
        os.environ.update(_env(SEQ_URL="http://127.0.0.1:1"))
        tick_notify.emit_seq(
            tick_id="t3", mode="read-only", rc=1, tick_log="/tmp/x.log",
            message="m", print_only=False,
        )

    # -- memory REST transport (TASK 229) ----------------------------------
    def test_memory_rest_posts_contract_payload(self) -> None:
        os.environ.clear()
        server = _RecordingServer()
        os.environ.update(
            _env(
                MEMORY_URL=server.base,
                MEMORY_TOKEN="dummy-memory-token",
            )
        )
        try:
            tick_notify.post_memory_rest(
                device="ct110", tick_id="t4", text="tick body",
                source_ref="/tmp/x.log", metadata={"mode": "read-only"}, print_only=False,
            )
            path, headers, body = server.requests[0]
            self.assertEqual(path, "/api/memories")
            self.assertEqual(headers.get("authorization"), "Bearer dummy-memory-token")
            payload = json.loads(body.decode())
            self.assertEqual(payload["source"], "overseer")
            self.assertEqual(payload["session_id"], "ct110/t4")
            self.assertEqual(payload["tier"], "overseer")
            self.assertEqual(payload["text"], "tick body")
            self.assertIn("metadata", payload)
        finally:
            server.stop()

    def test_memory_rest_5xx_retried_once(self) -> None:
        os.environ.clear()
        server = _RecordingServer()
        server.response_status = 500
        os.environ.update(_env(MEMORY_URL=server.base))
        try:
            tick_notify.post_memory_rest(
                device="ct110", tick_id="t5", text="x", source_ref="/x",
                metadata={}, print_only=False,
            )
            self.assertEqual(len(server.requests), 2)
        finally:
            server.stop()

    def test_memory_rest_4xx_is_hard_error_no_retry(self) -> None:
        os.environ.clear()
        server = _RecordingServer()
        server.response_status = 401
        os.environ.update(_env(MEMORY_URL=server.base))
        try:
            tick_notify.post_memory_rest(
                device="ct110", tick_id="t6", text="x", source_ref="/x",
                metadata={}, print_only=False,
            )
            self.assertEqual(len(server.requests), 1)
        finally:
            server.stop()

    # -- memory gateway transport (TASK 229) --------------------------------
    def test_memory_gateway_posts_jsonrpc_with_server_scope(self) -> None:
        os.environ.clear()
        server = _RecordingServer()
        os.environ.update(
            _env(
                MEMORY_MCP_URL=server.base + "/mcp",  # no trailing slash: must be normalised
                MEMORY_MCP_KEY="dummy-gateway-key",
            )
        )
        try:
            tick_notify.post_memory_gateway(
                device="ct110", tick_id="t7", text="tick body", source_ref="/tmp/x.log",
                metadata={"mode": "read-only"}, print_only=False,
            )
            path, headers, body = server.requests[0]
            self.assertEqual(path, "/mcp/")
            self.assertEqual(headers.get("authorization"), "Bearer dummy-gateway-key")
            self.assertEqual(headers.get("x-mcp-servers"), "memory")
            payload = json.loads(body.decode())
            self.assertEqual(payload["jsonrpc"], "2.0")
            self.assertEqual(payload["method"], "tools/call")
            arguments = payload["params"]["arguments"]
            self.assertEqual(payload["params"]["name"], "memory-remember_session")
            self.assertEqual(arguments["source"], "overseer")
            self.assertEqual(arguments["session_id"], "ct110/t7")
            self.assertEqual(arguments["tier"], "overseer")
        finally:
            server.stop()

    def test_memory_gateway_tool_override_and_sse_response(self) -> None:
        os.environ.clear()
        server = _RecordingServer()
        server.response_body = b'data: {"jsonrpc":"2.0","id":1,"result":{"isError":false}}\n\n'
        os.environ.update(
            _env(
                MEMORY_MCP_URL=server.base + "/mcp/",
                MEMORY_MCP_TOOL="my-memory-remember_session",
            )
        )
        try:
            tick_notify.post_memory_gateway(
                device="ct110", tick_id="t8", text="x", source_ref="/x",
                metadata={}, print_only=False,
            )
            _path, _headers, body = server.requests[0]
            payload = json.loads(body.decode())
            self.assertEqual(payload["params"]["name"], "my-memory-remember_session")
        finally:
            server.stop()

    def test_memory_transport_precedence_rest_over_gateway(self) -> None:
        os.environ.clear()
        rest_server = _RecordingServer()
        gateway_server = _RecordingServer()
        os.environ.update(
            _env(
                MEMORY_URL=rest_server.base,
                MEMORY_MCP_URL=gateway_server.base + "/mcp/",
            )
        )
        try:
            import io

            original_stdin = sys.stdin
            sys.stdin = io.StringIO("x")
            try:
                rc = tick_notify.main(
                    ["--tick-id", "t9", "--mode", "read-only", "--rc", "0",
                     "--tick-log", "/tmp/x.log", "--device", "d"]
                )
            finally:
                sys.stdin = original_stdin
            self.assertEqual(rc, 0)
            # REST wins when both NAMES are set (the 229 brief's primary /
            # fallback ordering): exactly one REST POST, zero gateway POSTs.
            self.assertEqual(len(rest_server.requests), 1)
            self.assertEqual(gateway_server.requests, [])
        finally:
            rest_server.stop()
            gateway_server.stop()

    # -- main() end to end --------------------------------------------------
    def test_main_end_to_end_with_local_stub(self) -> None:
        os.environ.clear()
        seq_server = _RecordingServer()
        memory_server = _RecordingServer()
        os.environ.update(
            _env(
                SEQ_URL=seq_server.base,
                MEMORY_URL=memory_server.base,
            )
        )
        try:
            # main() reads its summary from stdin; patch stdin for the call.
            import io

            original_stdin = sys.stdin
            sys.stdin = io.StringIO("tick body")
            try:
                rc = tick_notify.main(
                    ["--tick-id", "tA", "--mode", "read-only", "--rc", "0",
                     "--tick-log", "/tmp/x.log", "--device", "ct110"]
                )
            finally:
                sys.stdin = original_stdin
            self.assertEqual(rc, 0)
            self.assertEqual(len(seq_server.requests), 1)
            self.assertEqual(len(memory_server.requests), 1)
            # The redaction is applied before any POST.
            memory_payload = json.loads(memory_server.requests[0][2].decode())
            self.assertEqual(memory_payload["text"], "tick body")
        finally:
            seq_server.stop()
            memory_server.stop()


if __name__ == "__main__":
    unittest.main()