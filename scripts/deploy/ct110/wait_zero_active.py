#!/usr/bin/env python3
"""Wait for a COUNTED zero-active-run window on the CT110 harness service.

A deploy restarts lh-harness.service and kills every in-flight run, so the
runner must observe several consecutive polls of GET /api/runs with zero
runs in an ACTIVE lifecycle state before it is allowed to touch the host
(one passing poll can be luck between a run ending and the next launching).
The inner deploy script re-checks idleness inside CT110 immediately before
the restart, so a launch racing this window still aborts safely.

Active statuses come from src/lh_harness/supervisor/lifecycle.py
(ACTIVE_STATUSES).  Stdlib only: the lan-deploy runner is a bare Debian 12
box, so no third-party imports are allowed here.

Reads the bearer token from the CT110_API_TOKEN environment variable
(the ct110-prod environment secret, never from an argument).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ACTIVE_STATUSES = {"creating", "starting", "running", "waiting_approval", "stopping"}


def count_active(url: str, token: str) -> int:
    """Return the number of active runs, or -1 when the count is unknowable."""
    request = urllib.request.Request(
        url.rstrip("/") + "/api/runs",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"poll: GET /api/runs failed: {exc}", file=sys.stderr)
        return -1
    runs = payload.get("runs") or []
    return sum(
        1
        for run in runs
        if str(run.get("status", "")).strip().lower() in ACTIVE_STATUSES
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="base URL, e.g. http://192.168.21.168:8799")
    parser.add_argument("--consecutive", type=int, default=3,
                        help="consecutive zero-active polls required (default 3)")
    parser.add_argument("--interval-seconds", type=int, default=15)
    parser.add_argument("--timeout-minutes", type=float, default=30)
    args = parser.parse_args()

    token = os.environ.get("CT110_API_TOKEN", "")
    if not token:
        print("::error::CT110_API_TOKEN is not set (ct110-prod environment secret missing?)",
              file=sys.stderr)
        return 1

    deadline = time.monotonic() + args.timeout_minutes * 60
    zero_streak = 0
    while True:
        active = count_active(args.url, token)
        if active == 0:
            zero_streak += 1
            print(f"poll: 0 active runs ({zero_streak}/{args.consecutive} consecutive)")
            if zero_streak >= args.consecutive:
                print(f"zero-active window confirmed: {args.consecutive} consecutive polls")
                return 0
        elif active > 0:
            zero_streak = 0
            print(f"poll: {active} active run(s) — window not open; waiting")
        else:
            zero_streak = 0
            print("poll: count unknowable — NOT treating as a safe window; waiting")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"::error::no zero-active window within {args.timeout_minutes} minutes "
                  f"(last count: {active}) — refusing to deploy into live runs", file=sys.stderr)
            return 1
        time.sleep(min(args.interval_seconds, remaining))


if __name__ == "__main__":
    sys.exit(main())
