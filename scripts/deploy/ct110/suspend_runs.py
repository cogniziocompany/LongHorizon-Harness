#!/usr/bin/env python3
"""Stop (suspend) every active run on the CT110 harness service (fc-H4b).

A deploy restarts lh-harness.service and kills every in-flight run; waiting
for a natural zero-active window can take forever when runs are long.  With
the queue drain already set (set_drain.py --enable, required — the API
refuses a suspend with 409 while the queue is undrained), this script stops
every ACTIVE run through POST /api/maintenance/suspend.  The service parks
each stopped run in runs_root/queue/maintenance_manifest.json so
resume_runs.py can put every one of them back (mode=continue) once the
deploy verifies.  Cost: a suspended run's round in progress is redone
after resume.

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
        url.rstrip("/") + "/api/maintenance/suspend",
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="base URL, e.g. http://192.168.21.168:8799")
    parser.add_argument("--reason", default="deploy maintenance window (fc-H4b)",
                        help="operator-facing reason recorded in the maintenance manifest")
    args = parser.parse_args()

    token = os.environ.get("CT110_API_TOKEN", "")
    if not token:
        print("::error::CT110_API_TOKEN is not set (ct110-prod environment secret missing?)",
              file=sys.stderr)
        return 1

    try:
        result = _post(args.url, token, {"reason": args.reason})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"::error::POST /api/maintenance/suspend failed: {exc}", file=sys.stderr)
        return 1

    stopped = result.get("stopped") or []
    stopped_ids = [str(item["run_id"]) for item in stopped
                   if isinstance(item, dict) and item.get("run_id")]
    if stopped_ids:
        print(f"suspended {len(stopped_ids)} run(s): {' '.join(stopped_ids)}")
    else:
        # Not an error: an idle service parks nothing, and the deploy
        # proceeds exactly as if the zero-active wait had come up empty.
        print("suspend accepted: no active runs to stop")
    for error in result.get("errors") or []:
        if isinstance(error, dict):
            print(f"::warning::suspend could not stop {error.get('run_id')}: {error.get('error')}")
    manifest = result.get("manifest", {})
    print(f"suspend write accepted: {json.dumps(manifest, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
