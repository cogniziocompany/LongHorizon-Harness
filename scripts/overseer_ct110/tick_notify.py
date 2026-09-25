#!/usr/bin/env python3
"""Per-tick telemetry sink for the CT110 overseer sweep (TASK 236).

Two fail-open sinks, both wired by env-var NAME only, no values baked in.

Seq logging mirrors the wire contract of ``src/lh_harness/seq_logging.py`` on
branch ``feat/seq-logging`` @ edef634 (TASK 161):
  * CLEF JSON lines POSTed to ``{SEQ_URL}/api/events/raw?clef``
  * ``X-Seq-ApiKey`` header attached only when the NAME ``SEQ_API_KEY`` is
    set (missing key = unauthenticated, the dev server accepts it)
  * env names: SEQ_URL / SEQ_API_KEY / SEQ_MIN_LEVEL

Hivemind ingest mirrors the contract of ``tasks/overseer-ingest-task.txt``
(TASK 229 material, the overseer-session ingester brief).  It posts a single
overseer turn per tick into the memory MCP via two possible transports:
  1. REST: ``POST {MEMORY_URL}/api/memories``, Bearer ``MEMORY_TOKEN``.
  2. Gateway: JSON-RPC 2.0 ``tools/call`` to ``{MEMORY_MCP_URL}`` (must
     keep trailing slash; the gateway 307s ``/mcp`` and drops the POST body),
     headers ``authorization: Bearer {MEMORY_MCP_KEY}`` and
     ``x-mcp-servers: memory``; tool name ``memory-remember_session``
     (override via ``MEMORY_MCP_TOOL``).  Both required-fields check matches
     the brief: source, session_id, text.

All network calls are fail-open: a Seq outage or an unreachable memory
server is logged locally (stderr) but the script exits 0, so the tick itself
is never aborted by telemetry.  A 4xx from the memory transport is a hard
error for that record (logged); 5xx/timeout is retried once.

The only secrets that may appear on stdin are redacted before any POST using
the exact regex from the task 229 brief.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# Redaction (verbatim from tasks/overseer-ingest-task.txt, 2026-09-05)
# ---------------------------------------------------------------------------
_REDACT_RE = re.compile(
    r"(sk-[A-Za-z0-9]{8,}|Bearer [A-Za-z0-9._-]{12,}|ghp_[A-Za-z0-9]{20,}|"
    r"AKIA[0-9A-Z]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY)",
    re.IGNORECASE,
)
_REDACT_REPLACEMENT = "[redacted]"

# Seq endpoint contract (matches src/lh_harness/seq_logging.py on feat/seq-logging).
_SEQ_URL_ENV = "SEQ_URL"
_SEQ_API_KEY_ENV = "SEQ_API_KEY"
_SEQ_MIN_LEVEL_ENV = "SEQ_MIN_LEVEL"
_SEQ_LEVEL_DEFAULT = "Information"

# Memory ingest contract (matches tasks/overseer-ingest-task.txt).
_MEMORY_REST_URL_ENV = "MEMORY_URL"
_MEMORY_REST_TOKEN_ENV = "MEMORY_TOKEN"
_MEMORY_GATEWAY_URL_ENV = "MEMORY_MCP_URL"
_MEMORY_GATEWAY_KEY_ENV = "MEMORY_MCP_KEY"
_MEMORY_GATEWAY_TOOL_ENV = "MEMORY_MCP_TOOL"
_MEMORY_DEFAULT_TOOL = "memory-remember_session"

# HTTP retry/backoff policy for memory transport only (Seq is best-effort
# single shot, matching the Python handler's own "_MAX_RETRIES = 3" policy
# but implemented here as one retry; a full retry queue is not worth adding to
# a wrapper script).
_RETRY_BACKOFF = (0.0, 1.0)


def redact(text: str) -> str:
    """Redact credential-shaped spans before any network POST."""
    return _REDACT_RE.sub(_REDACT_REPLACEMENT, text)


def _utc_now() -> str:
    return datetime.datetime.now(tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _clef_level(level: str) -> str:
    # The only levels the tick emits are Information (success path) and Error
    # (config failure); the source contract supports the full Serilog ladder.
    return level if level in {"Verbose", "Debug", "Information", "Warning", "Error", "Fatal"} else "Information"


def emit_seq(
    *,
    tick_id: str,
    mode: str,
    rc: int,
    tick_log: str,
    message: str,
    print_only: bool,
) -> None:
    """Best-effort single CLEF event to Seq (fail-open)."""
    seq_url = (os.environ.get(_SEQ_URL_ENV) or "").strip().rstrip("/")
    if not seq_url:
        return

    raw_level = (os.environ.get(_SEQ_MIN_LEVEL_ENV) or _SEQ_LEVEL_DEFAULT).strip()
    # Validate/uppercase the level; any invalid value falls back to Information.
    level = _clef_level(raw_level.capitalize())

    # Use the package version if available; otherwise a fixed marker for the
    # tick implementation.  This keeps the CLEF event consistent with the
    # ``app``/``version`` convention used by src/lh_harness/seq_logging.py.
    version = _tick_version()

    event: dict[str, Any] = {
        "@t": _utc_now(),
        "@l": level,
        "@m": message,
        "app": "lh-overseer-sweep",
        "version": version,
        "logger": "scripts.overseer_ct110.tick_notify",
        "tick_id": tick_id,
        "tick_mode": mode,
        "tick_rc": rc,
        "tick_log": tick_log,
    }
    line = json.dumps(event, default=str) + "\n"

    if print_only:
        print("[seq print-only]", line.strip())
        return

    url = f"{seq_url}/api/events/raw?clef"
    request = urllib.request.Request(
        url,
        data=line.encode("utf-8"),
        headers={
            "Content-Type": "application/vnd.serilog.clef",
            "Content-Length": str(len(line)),
        },
        method="POST",
    )
    api_key = (os.environ.get(_SEQ_API_KEY_ENV) or "").strip()
    if api_key:
        request.add_header("X-Seq-ApiKey", api_key)

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            _ = response.read()
    except urllib.error.HTTPError as exc:
        print(f"seq warn: POST {url} returned HTTP {exc.code} ({exc.reason})", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - fail-open by design
        print(f"seq warn: POST {url} failed: {exc}", file=sys.stderr)


def _tick_version() -> str:
    """Return a version string tied to the package if we can import it."""
    try:
        from lh_harness import __version__  # type: ignore[import-untyped]
        return str(__version__)
    except Exception:  # noqa: BLE001
        return "0.1.7-task236"


def _post_request(url: str, body: bytes, headers: dict[str, str], timeout: float = 30.0) -> urllib.request.addinfourl:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    return urllib.request.urlopen(request, timeout=timeout)


def post_memory_rest(
    *,
    device: str,
    tick_id: str,
    text: str,
    source_ref: str,
    metadata: dict[str, Any],
    print_only: bool,
) -> None:
    memory_url = (os.environ.get(_MEMORY_REST_URL_ENV) or "").strip().rstrip("/")
    if not memory_url:
        return
    token = (os.environ.get(_MEMORY_REST_TOKEN_ENV) or "").strip()

    payload = {
        "source": "overseer",
        "session_id": f"{device}/{tick_id}",
        "text": text,
        "source_ref": source_ref,
        "device": device,
        "repo": "LongHorizon-Harness",
        "tier": "overseer",
        "metadata": metadata,
    }
    body = json.dumps(payload, default=str).encode("utf-8")

    if print_only:
        print("[memory rest print-only]", body.decode("utf-8"))
        return

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    if token:
        headers["authorization"] = f"Bearer {token}"

    attempts = 0
    while attempts < 2:
        try:
            with _post_request(f"{memory_url}/api/memories", body, headers) as response:
                _ = response.read()
            return
        except urllib.error.HTTPError as exc:
            print(
                f"memory rest warn: POST {memory_url}/api/memories returned HTTP {exc.code} ({exc.reason})",
                file=sys.stderr,
            )
            if exc.code < 500:
                return  # 4xx is a hard error for this record, do not retry.
            attempts += 1
            if attempts < 2:
                time.sleep(_RETRY_BACKOFF[attempts])
        except Exception as exc:  # noqa: BLE001
            print(f"memory rest warn: attempt {attempts + 1} failed: {exc}", file=sys.stderr)
            attempts += 1
            if attempts < 2:
                time.sleep(_RETRY_BACKOFF[attempts])


def post_memory_gateway(
    *,
    device: str,
    tick_id: str,
    text: str,
    source_ref: str,
    metadata: dict[str, Any],
    print_only: bool,
) -> None:
    gateway_url = (os.environ.get(_MEMORY_GATEWAY_URL_ENV) or "").strip()
    if not gateway_url:
        return
    # The live wiring from PTAIT09 (tasks/overseer-session-ingest-2026-09-05.md)
    # found that the gateway requires a trailing slash: ``/mcp/``; ``/mcp``
    # 307s and drops the POST body.  Normalise it here.
    if not gateway_url.endswith("/"):
        gateway_url += "/"
    gateway_key = (os.environ.get(_MEMORY_GATEWAY_KEY_ENV) or "").strip()
    tool = (os.environ.get(_MEMORY_GATEWAY_TOOL_ENV) or _MEMORY_DEFAULT_TOOL)

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": {
                "source": "overseer",
                "session_id": f"{device}/{tick_id}",
                "text": text,
                "tier": "overseer",
                "device": device,
                "repo": "LongHorizon-Harness",
                "source_ref": source_ref,
                "metadata": metadata,
            },
        },
    }
    body = json.dumps(payload, default=str).encode("utf-8")

    if print_only:
        print("[memory gateway print-only]", body.decode("utf-8"))
        return

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Content-Length": str(len(body)),
    }
    if gateway_key:
        headers["authorization"] = f"Bearer {gateway_key}"
    headers["x-mcp-servers"] = "memory"

    attempts = 0
    while attempts < 2:
        try:
            with _post_request(gateway_url, body, headers) as response:
                raw = response.read()
            # The gateway can return either a plain JSON body or an SSE frame.
            _parse_gateway_response(raw)
            return
        except urllib.error.HTTPError as exc:
            print(
                f"memory gateway warn: POST {gateway_url} returned HTTP {exc.code} ({exc.reason})",
                file=sys.stderr,
            )
            if exc.code < 500:
                return
            attempts += 1
            if attempts < 2:
                time.sleep(_RETRY_BACKOFF[attempts])
        except Exception as exc:  # noqa: BLE001
            print(f"memory gateway warn: attempt {attempts + 1} failed: {exc}", file=sys.stderr)
            attempts += 1
            if attempts < 2:
                time.sleep(_RETRY_BACKOFF[attempts])


def _parse_gateway_response(raw: bytes) -> None:
    """Best-effort parse of a gateway JSON-RPC/SSE response.

    The contract only requires us to detect hard errors.  A clean response
    (HTTP 200/202) is accepted; an ``isError`` field is logged.
    """
    if not raw:
        return
    text = raw.decode("utf-8", errors="replace")
    # If the response contains SSE framing, take the first ``data:`` line.
    if "data:" in text:
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return  # malformed body is accepted as a warning, not a tick abort.

    if not isinstance(data, dict):
        return
    if data.get("error"):
        print(f"memory gateway warn: JSON-RPC error: {data['error']}", file=sys.stderr)
    result = data.get("result")
    if isinstance(result, dict) and result.get("isError"):
        print(f"memory gateway warn: result.isError set: {result.get('content')}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Per-tick Seq + hivemind notifications for the CT110 overseer sweep.",
    )
    parser.add_argument("--tick-id", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--rc", type=int, required=True)
    parser.add_argument("--tick-log", required=True)
    parser.add_argument("--device", default=os.environ.get("HOSTNAME", "").lower())
    parser.add_argument("--print-only", action="store_true", help="render payloads, post nothing")
    args = parser.parse_args(argv)

    # Read the tick summary from stdin and redact it before any POST.
    raw_summary = sys.stdin.read()
    summary = redact(raw_summary)

    metadata: dict[str, Any] = {
        "mode": args.mode,
        "rc": args.rc,
        "tick_log": args.tick_log,
    }

    seq_configured = bool(os.environ.get(_SEQ_URL_ENV))
    memory_rest_configured = bool(os.environ.get(_MEMORY_REST_URL_ENV))
    memory_gateway_configured = bool(os.environ.get(_MEMORY_GATEWAY_URL_ENV))

    if args.print_only:
        print(
            f"[tick-notify print-only] sinks: seq={seq_configured}, "
            f"memory_rest={memory_rest_configured}, memory_gateway={memory_gateway_configured}"
        )

    # Seq event (fail-open).
    seq_message = summary[:1000]  # CLEF message field bounded like the handler does.
    emit_seq(
        tick_id=args.tick_id,
        mode=args.mode,
        rc=args.rc,
        tick_log=args.tick_log,
        message=seq_message,
        print_only=args.print_only,
    )

    # Memory ingest of this tick (fail-open).  REST transport takes precedence
    # when both are configured (matches the 229 brief: MEMORY_URL primary,
    # MEMORY_MCP_URL fallback when MEMORY_URL is unset).
    post_memory_rest(
        device=args.device,
        tick_id=args.tick_id,
        text=summary,
        source_ref=args.tick_log,
        metadata=metadata,
        print_only=args.print_only,
    )
    if not memory_rest_configured:
        post_memory_gateway(
            device=args.device,
            tick_id=args.tick_id,
            text=summary,
            source_ref=args.tick_log,
            metadata=metadata,
            print_only=args.print_only,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())