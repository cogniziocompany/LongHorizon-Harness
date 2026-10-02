#!/usr/bin/env python3
"""Resume the runs a maintenance suspend parked on the CT110 harness service.

POST /api/maintenance/resume puts every manifest run back with
mode="continue"; this script then polls GET /api/runs until each resumed
run reports an ACTIVE lifecycle status, because the deploy's drain-clear
step must not fire while resumed runs are still terminal.  A run that does
not come back within --timeout-minutes fails this step BY NAME so the
failure path pins the exact run instead of a bare timeout.

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


# POST /api/maintenance/{suspend,resume} stops/starts every active run before it
# answers; on CT110 that took >15 s (2026-10-02 deploy 37062516845: suspend timed
# out client-side, landed server-side, and the empty-manifest resume stranded the
# runs). Wait long enough for the server to finish and write the manifest.
MAINTENANCE_POST_TIMEOUT = 120.0


def _resume(url: str, token: str, timeout: float = MAINTENANCE_POST_TIMEOUT) -> dict[str, object]:
    request = urllib.request.Request(
        url.rstrip("/") + "/api/maintenance/resume",
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _statuses(url: str, token: str, timeout: float = 60) -> dict[str, str] | None:
    """run id -> lifecycle status, or None when the poll itself failed."""
    request = urllib.request.Request(
        # Summary form, as in wait_zero_active.py: enough to name statuses,
        # small enough that the poll itself never becomes the bottleneck.
        url.rstrip("/") + "/api/runs?fields=summary",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"poll: GET /api/runs failed: {exc}", file=sys.stderr)
        return None
    runs = payload.get("runs") or []
    return {
        str(run["id"]): str(run.get("status", "")).strip().lower()
        for run in runs
        if isinstance(run, dict) and run.get("id")
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="base URL, e.g. http://192.168.21.168:8799")
    parser.add_argument("--interval-seconds", type=float, default=10,
                        help="delay between /api/runs polls (default 10)")
    parser.add_argument("--timeout-minutes", type=float, default=5)
    args = parser.parse_args()

    token = os.environ.get("CT110_API_TOKEN", "")
    if not token:
        print("::error::CT110_API_TOKEN is not set (ct110-prod environment secret missing?)",
              file=sys.stderr)
        return 1

    try:
        result = _resume(args.url, token)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"::error::POST /api/maintenance/resume failed: {exc}", file=sys.stderr)
        return 1

    run_ids: list[str] = []
    for entry in result.get("runs") or []:
        if not isinstance(entry, dict) or not entry.get("run_id"):
            continue
        run_id = str(entry["run_id"])
        run_ids.append(run_id)
        if entry.get("ok"):
            print(f"resume accepted: {run_id}")
        else:
            # The service keeps a failed entry in the manifest for an
            # operator retry; it still cannot come back on its own, so the
            # poll below times out on it by name.
            print(f"::warning::resume rejected {run_id}: {entry.get('error')}")
    if not run_ids:
        print("resume accepted: maintenance manifest was empty (nothing parked)")
        return 0

    deadline = time.monotonic() + args.timeout_minutes * 60
    pending = set(run_ids)
    while True:
        statuses = _statuses(args.url, token)
        if statuses is not None:
            back = {run_id for run_id in pending if statuses.get(run_id) in ACTIVE_STATUSES}
            for run_id in sorted(back):
                print(f"poll: {run_id} is active ({statuses[run_id]})")
            pending -= back
            if not pending:
                print(f"all {len(run_ids)} resumed run(s) active")
                return 0
            print(f"poll: waiting on {len(pending)} run(s): {' '.join(sorted(pending))}")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"::error::resumed run(s) did not come back within {args.timeout_minutes} "
                  f"minutes: {' '.join(sorted(pending))}", file=sys.stderr)
            return 1
        time.sleep(min(args.interval_seconds, remaining))


if __name__ == "__main__":
    sys.exit(main())
