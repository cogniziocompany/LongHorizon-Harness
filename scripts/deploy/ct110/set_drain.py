#!/usr/bin/env python3
"""Set or clear the queue drain flag on the CT110 harness service (TASK 242).

A deploy restarts lh-harness.service, so the runner first sets the drain
flag (POST /api/queue/drain) to stop NEW queue launches while live runs
finish; wait_zero_active.py then finds the window much sooner because the
three kimi slots no longer refill.  The flag persists across the restart,
so after the deploy we clear it explicitly.

Stdlib only: the lan-deploy runner is a bare Debian 12 box.

Reads the bearer token from the CT110_API_TOKEN environment variable
(the ct110-prod environment secret, never from an argument).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _post(url: str, token: str, payload: dict[str, object], timeout: float = 15) -> dict[str, object]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + "/api/queue/drain",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _meta(url: str, token: str, timeout: float = 15) -> dict[str, object]:
    request = urllib.request.Request(
        url.rstrip("/") + "/api/meta",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="base URL, e.g. http://192.168.21.168:8799")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable", action="store_true", help="set the drain flag (stops new launches)")
    group.add_argument("--disable", action="store_true", help="clear the drain flag (resume launching)")
    parser.add_argument("--reason", default="deploy maintenance window (TASK 242)",
                        help="operator-facing reason recorded with the flag (enable only)")
    parser.add_argument("--verify-state", choices=("enabled", "disabled"),
                        help="after the write, verify /api/meta reports this drain state")
    parser.add_argument("--verify-attempts", type=int, default=1)
    parser.add_argument("--verify-interval-seconds", type=float, default=5)
    args = parser.parse_args()

    token = os.environ.get("CT110_API_TOKEN", "")
    if not token:
        print("::error::CT110_API_TOKEN is not set (ct110-prod environment secret missing?)",
              file=sys.stderr)
        return 1

    payload: dict[str, object] = {"enabled": bool(args.enable)}
    if args.enable:
        payload["reason"] = args.reason
    try:
        result = _post(args.url, token, payload)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"::error::POST /api/queue/drain failed: {exc}", file=sys.stderr)
        return 1
    print(f"drain write accepted: {json.dumps(result.get('drain', {}), sort_keys=True)}")

    if not args.verify_state:
        return 0

    want_enabled = args.verify_state == "enabled"
    for attempt in range(1, max(args.verify_attempts, 1) + 1):
        try:
            meta = _meta(args.url, token)
            drain = meta.get("drain")
            if isinstance(drain, dict) and bool(drain.get("enabled")) is want_enabled:
                print(f"meta drain verified: {json.dumps(drain, sort_keys=True)}")
                return 0
            print(f"attempt {attempt}: meta drain={drain!r}, want enabled={want_enabled}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"attempt {attempt}: GET /api/meta failed: {exc}")
        if attempt < max(args.verify_attempts, 1):
            import time

            time.sleep(args.verify_interval_seconds)
    print("::error::/api/meta did not report the expected drain state", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())