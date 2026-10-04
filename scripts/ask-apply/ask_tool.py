#!/usr/bin/env python3
"""Call one ask-store tool on a harness node as a signed caller (task A3d).

The overseer (Claude Code on VM 211) uses this to raise asks, declare fields
and apply sealed secrets: those tools need an HMAC caller identity, which an
LLM cannot compute through the gateway. Stdlib only.

    ask_tool.py raise_open_ask '{"id": "...", "ask": "...", "kind": "DECISION"}'
    ask_tool.py declare_ask_fields '{"id": "...", "fields": [...]}'
    ask_tool.py apply_ask_secret '{"id": "279-gateway-github-token", "field": "secret",
                                   "target": "ct202-mcp-tools-env:GITHUB_MCP_TOKEN"}'

Environment (names only; never pass values on the command line):
    LH_HARNESS_URL            base URL of the node, e.g. http://<ct110>:8799
    LH_HARNESS_WEB_TOKEN      the node's bearer
    LH_HARNESS_CALLER         caller name (default: overseer)
    LH_HARNESS_CALLER_<NAME>_SECRET   that caller's HMAC secret

It refuses respond_open_ask and clear_ask_secret: secrets are entered by a
person on the web page, never through this script.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

ALLOWED = {"raise_open_ask", "declare_ask_fields", "apply_ask_secret", "list_open_asks"}


def _secret_env(caller: str) -> str:
    return f"LH_HARNESS_CALLER_{caller.upper().replace('-', '_')}_SECRET"


def build_arguments(arguments: dict, caller: str, secret: str, now: float | None = None) -> dict:
    ts = str(int(time.time() if now is None else now))
    sig = hmac.new(secret.encode(), f"{caller}:{ts}".encode(), hashlib.sha256).hexdigest()
    return {**arguments, "caller": caller, "caller_ts": ts, "caller_sig": sig}


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ALLOWED:
        print(f"usage: {argv[0]} <{'|'.join(sorted(ALLOWED))}> '<json arguments>'", file=sys.stderr)
        return 2
    tool, raw = argv[1], argv[2]
    try:
        arguments = json.loads(raw)
    except ValueError as exc:
        print(f"arguments are not JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(arguments, dict):
        print("arguments must be a JSON object", file=sys.stderr)
        return 2
    base = os.environ.get("LH_HARNESS_URL", "").rstrip("/")
    token = os.environ.get("LH_HARNESS_WEB_TOKEN", "")
    caller = os.environ.get("LH_HARNESS_CALLER", "overseer")
    secret = os.environ.get(_secret_env(caller), "")
    missing = [n for n, v in (("LH_HARNESS_URL", base), ("LH_HARNESS_WEB_TOKEN", token), (_secret_env(caller), secret)) if not v]
    if missing:
        print("not set: " + ", ".join(missing), file=sys.stderr)
        return 2
    if tool != "list_open_asks":
        arguments = build_arguments(arguments, caller, secret)
    body = json.dumps({"arguments": arguments}).encode()
    req = urllib.request.Request(
        f"{base}/api/mcp/fleet/{tool}",
        data=body,
        method="POST",
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            print(resp.read().decode())
            return 0
    except urllib.error.HTTPError as exc:
        print(exc.read().decode(errors="replace"))
        return 1
    except urllib.error.URLError as exc:
        print(f"node unreachable: {exc.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
