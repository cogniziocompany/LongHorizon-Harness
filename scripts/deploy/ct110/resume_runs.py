#!/usr/bin/env python3
"""Resume the runs a maintenance suspend parked on the CT110 harness service.

POST /api/maintenance/resume consumes ONLY the manifest of the suspend id it
is given (LHH-SUSPEND-TIMEOUT): this script must name this deploy's suspend
id via --suspend-id (exported by suspend_runs.py to $GITHUB_OUTPUT), so a
resume can never act on an older suspend's stale manifest — the 2026-10-08
mechanism that revived run 20261008T054411Z_4c9cce84 a day after its own
suspend died.  The service refuses a provenance-free resume outright, and
clears the maintenance launch pause on every accepted call (only while the
flag still names this suspend).

Resume itself is idempotent end to end: runs already active count as
success server-side, the manifest is deleted once every run resumed, and a
repeat call against the same suspend id finds an empty/absent manifest and
is a no-op.

After the POST, this script polls GET /api/runs until each resumed run
reports an ACTIVE lifecycle status, because the deploy's drain-clear step
must not fire while resumed runs are still terminal.  A run that does not
come back within --timeout-minutes fails this step BY NAME so the failure
path pins the exact run instead of a bare timeout.

Active statuses come from src/lh_harness/supervisor/lifecycle.py
(ACTIVE_STATUSES) — re-declared here because the lan-deploy runner is a
bare Debian 12 box, so no third-party (or harness-package) imports are
allowed.  Stdlib only.

Reads the bearer token from the CT110_API_TOKEN environment variable
(the ct110-prod environment secret, never from an argument).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ACTIVE_STATUSES = {"creating", "starting", "running", "waiting_approval", "stopping"}


# POST /api/maintenance/resume resumes every manifest run synchronously
# inside the request, so keep a generous per-request budget (it always was
# 120 s on CT110 and was never the bottleneck — the suspend side was).
RESUME_POST_TIMEOUT = 120.0

# Must match the server-side acceptance rule for suspend ids (they feed a
# manifest filename): refuse locally instead of round-tripping a 422.
_SUSPEND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _suspend_id_arg(value: str) -> str:
    if value and not _SUSPEND_ID_RE.match(value):
        raise argparse.ArgumentTypeError(
            "suspend id has an invalid form (a path-safe token is expected)")
    return value


def _error_text(exc: BaseException) -> str:
    """Best-effort one-line description, surfacing the API's detail field."""

    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (OSError, ValueError):
            body = None
        if isinstance(body, dict) and isinstance(body.get("detail"), str):
            return f"HTTP {exc.code}: {body['detail']}"
        return f"HTTP {exc.code}: {exc.reason}"
    return str(exc)


def _resume(url: str, token: str, suspend_id: str, timeout: float = RESUME_POST_TIMEOUT) -> dict[str, object]:
    request = urllib.request.Request(
        url.rstrip("/") + "/api/maintenance/resume",
        data=json.dumps({"suspend_id": suspend_id}).encode("utf-8"),
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
    parser.add_argument("--url", required=True, help="base URL, e.g. http://ct110-host:8799")
    parser.add_argument("--suspend-id", required=True, type=_suspend_id_arg,
                        help="suspend id this deploy started (suspend_runs.py exports it to "
                             "$GITHUB_OUTPUT); resume consumes only this suspend's manifest")
    parser.add_argument("--interval-seconds", type=float, default=10,
                        help="delay between /api/runs polls (default 10)")
    parser.add_argument("--timeout-minutes", type=float, default=5)
    args = parser.parse_args()

    if not args.suspend_id:
        # The deploy's suspend step never produced an id (e.g. the POST
        # itself failed): there is no window to close, and resolving to
        # whatever manifest happens to exist is the structural stale-revival
        # path — refuse to guess, succeed as a no-op cleanup.
        print("resume: no suspend id supplied (suspend never started); nothing to resume")
        return 0

    token = os.environ.get("CT110_API_TOKEN", "")
    if not token:
        print("::error::CT110_API_TOKEN is not set (ct110-prod environment secret missing?)",
              file=sys.stderr)
        return 1

    print(f"resuming runs parked by suspend {args.suspend_id}")
    try:
        result = _resume(args.url, token, args.suspend_id)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"::error::POST /api/maintenance/resume failed for suspend "
              f"{args.suspend_id}: {_error_text(exc)}", file=sys.stderr)
        return 1

    # Provenance check: the service must have acted on exactly the suspend
    # this deploy started — never silently accept an answer about another.
    answered = result.get("suspend_id")
    if isinstance(answered, str) and answered and answered != args.suspend_id:
        print(f"::error::resume answered for suspend {answered!r}, expected "
              f"{args.suspend_id!r} — refusing to trust this response", file=sys.stderr)
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
        print(f"resume accepted: no parked runs for suspend {args.suspend_id} "
              f"(manifest empty — a repeated resume is a no-op)")
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
