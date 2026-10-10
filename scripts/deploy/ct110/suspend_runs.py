#!/usr/bin/env python3
"""Suspend every active run on the CT110 harness service (async flow).

POST /api/maintenance/suspend is asynchronous since LHH-SUSPEND-TIMEOUT
(the 2026-10-08 deploy-ct110 run 37737427710 failure: the old synchronous
handler blocked the request thread on per-run stops until this client's
120 s timeout fired mid-window, the deploy died, and a stale-manifest
resume a day later revived run 20261008T054411Z_4c9cce84).  The API now
sets the maintenance launch pause synchronously — so nothing new launches
from the moment suspend is requested — then answers 202 AT ONCE with a
suspend id, while a background job pauses each run at its next safe
checkpoint (bounded by LH_HARNESS_SUSPEND_RUN_GRACE_SECONDS per run and
LH_HARNESS_SUSPEND_DEADLINE_SECONDS overall, both server-side).

This script therefore only polls: GET /api/maintenance/suspend/{id} until
the suspend reaches a terminal state.

  * "suspended"  — every active run is parked in this suspend's manifest
                   (runs_root/queue/maintenance_manifest_<id>.json); the
                   launch pause stays set for the deploy window; exit 0.
  * "failed"     — the job hit an error or its deadline; the service has
                   ALREADY auto-resumed exactly the runs it paused and
                   cleared the launch pause (idempotent cleanup); exit 1
                   so the workflow's if: always() cleanup still runs.
  * deadline     — no terminal state within the local budget; exit 1.

The suspend id is printed and exported to $GITHUB_OUTPUT (when set) so
the resume step can key POST /api/maintenance/resume to THIS suspend's
manifest — an older suspend's manifest can never revive its runs.

Stdlib only: the lan-deploy runner is a bare Debian 12 box.

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


# The 202 answer comes back immediately (all run pausing happens in a
# background job server-side).  A POST that does not answer quickly means a
# wedged service, not a long-running suspend — do not resurrect the old
# 120 s client timeout here.
SUSPEND_POST_TIMEOUT = 15.0

# Overall local poll budget: comfortably larger than the server's default
# suspend deadline (120 s) plus its failure cleanup, so under normal
# conditions the API always reports its terminal state before this fires.
# Overridable with --timeout-seconds.
SUSPEND_POLL_BUDGET = 240.0

# Delay between GET /api/maintenance/suspend/{id} polls.
SUSPEND_POLL_INTERVAL = 5.0


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


def _post_suspend(url: str, token: str, reason: str, timeout: float = SUSPEND_POST_TIMEOUT) -> dict[str, object]:
    body = json.dumps({"reason": reason}).encode("utf-8")
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


def _get_status(url: str, token: str, status_path: str, timeout: float = SUSPEND_POST_TIMEOUT) -> dict[str, object]:
    request = urllib.request.Request(
        url.rstrip("/") + status_path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _emit_suspend_id(suspend_id: str) -> None:
    print(f"suspend id: {suspend_id}")
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    try:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"suspend_id={suspend_id}\n")
    except OSError as exc:
        # Not fatal: the deploy logs still carry the id above.
        print(f"::warning::could not write suspend_id to GITHUB_OUTPUT: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="base URL, e.g. http://ct110-host:8799")
    parser.add_argument("--reason", default="deploy maintenance window (fc-H4b)",
                        help="operator-facing reason recorded in the maintenance manifest")
    parser.add_argument("--timeout-seconds", type=float, default=SUSPEND_POLL_BUDGET,
                        help=f"overall poll budget (default {SUSPEND_POLL_BUDGET:.0f})")
    parser.add_argument("--interval-seconds", type=float, default=SUSPEND_POLL_INTERVAL,
                        help=f"delay between status polls (default {SUSPEND_POLL_INTERVAL:.0f})")
    args = parser.parse_args()

    token = os.environ.get("CT110_API_TOKEN", "")
    if not token:
        print("::error::CT110_API_TOKEN is not set (ct110-prod environment secret missing?)",
              file=sys.stderr)
        return 1

    try:
        result = _post_suspend(args.url, token, args.reason)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"::error::POST /api/maintenance/suspend failed: {_error_text(exc)}", file=sys.stderr)
        return 1

    suspend_id = result.get("suspend_id")
    if not isinstance(suspend_id, str) or not suspend_id:
        # The async contract guarantees an id; a 2xx without one means the
        # service behind --url predates the async suspend API — stop here
        # rather than pretend a window exists.
        print(f"::error::POST /api/maintenance/suspend returned no suspend id "
              f"(response: {json.dumps(result, sort_keys=True)}) — the service does not "
              f"speak the async suspend API", file=sys.stderr)
        return 1

    # Export BEFORE polling: even when the suspend later fails and this
    # script exits non-zero, the workflow's always() resume step can still
    # key its cleanup to this exact suspend id.
    _emit_suspend_id(suspend_id)
    status_path = result.get("status_url")
    if not isinstance(status_path, str) or not status_path.startswith("/api/"):
        status_path = f"/api/maintenance/suspend/{suspend_id}"
    deadline_seconds = result.get("deadline_seconds")

    deadline_mono = time.monotonic() + args.timeout_seconds
    while True:
        snapshot: dict[str, object] | None = None
        try:
            snapshot = _get_status(args.url, token, status_path)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                print(f"::error::GET {status_path}: unknown suspend id {suspend_id} "
                      f"(suspend record vanished mid-window)", file=sys.stderr)
                return 1
            print(f"::warning::poll GET {status_path} failed: {_error_text(exc)}", file=sys.stderr)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"::warning::poll GET {status_path} failed: {_error_text(exc)}", file=sys.stderr)

        if snapshot is not None:
            state = str(snapshot.get("state") or "")
            runs = [run for run in snapshot.get("runs") or [] if isinstance(run, dict)]
            if state == "suspended":
                paused = [str(run["run_id"]) for run in runs
                          if run.get("state") == "paused" and run.get("run_id")]
                if paused:
                    print(f"suspended {len(paused)} run(s): {' '.join(paused)}")
                else:
                    # Not an error: an idle service parks nothing, and the
                    # deploy proceeds as if the zero-active wait came up empty.
                    print("suspend accepted: no active runs to stop")
                pause = snapshot.get("launch_pause")
                if isinstance(pause, dict) and pause.get("enabled"):
                    print(f"maintenance launch pause held by {suspend_id} "
                          f"(resume step clears it after the deploy verifies)")
                return 0
            if state == "failed":
                # The service already rolled the window back: its _suspend_cleanup
                # resumed exactly the runs this suspend paused and cleared the
                # launch pause.  Surface the evidence and exit non-zero so the
                # workflow's always() cleanup/rollback path still runs.
                print(f"suspend {suspend_id} FAILED before every run was paused", file=sys.stderr)
                for error in snapshot.get("errors") or []:
                    if isinstance(error, dict):
                        print(f"::error::suspend error (run {error.get('run_id')}): "
                              f"{error.get('error')}", file=sys.stderr)
                for run in runs:
                    detail = f" ({run['detail']})" if run.get("detail") else ""
                    print(f"run {run.get('run_id')}: {run.get('state')}{detail}", file=sys.stderr)
                cleanup = [item for item in snapshot.get("cleanup") or [] if isinstance(item, dict)]
                resumed = [str(item.get("run_id")) for item in cleanup if item.get("ok")]
                stuck = [f"{item.get('run_id')}: {item.get('error')}" for item in cleanup
                         if not item.get("ok")]
                if cleanup:
                    print(f"service auto-resume (cleanup): ok=[{' '.join(resumed)}] "
                          f"failed=[{'; '.join(stuck)}]", file=sys.stderr)
                print(f"::error::suspend {suspend_id} did not complete; exiting non-zero so the "
                      f"deploy cleanup path runs (resume keyed to suspend id {suspend_id})",
                      file=sys.stderr)
                return 1
            if state == "resumed":
                print(f"::error::suspend {suspend_id} was resumed before this script finished "
                      f"polling; the deploy window is inconsistent — aborting", file=sys.stderr)
                return 1
            stopped = sum(1 for run in runs if run.get("state") == "stopping")
            paused_n = sum(1 for run in runs if run.get("state") == "paused")
            print(f"poll: suspend {suspend_id} state={state or 'unknown'} "
                  f"pausing={stopped} paused={paused_n}")

        remaining = deadline_mono - time.monotonic()
        if remaining <= 0:
            print(f"::error::suspend {suspend_id} did not reach a terminal state within "
                  f"{args.timeout_seconds:.0f}s (server deadline {deadline_seconds}s); exiting "
                  f"non-zero — the server-side job is still bounded by its own deadline and "
                  f"auto-resumes on expiry", file=sys.stderr)
            return 1
        time.sleep(min(args.interval_seconds, remaining))


if __name__ == "__main__":
    sys.exit(main())
