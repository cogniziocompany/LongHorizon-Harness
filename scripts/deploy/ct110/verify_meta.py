#!/usr/bin/env python3
"""Verify the restarted harness serves GET /api/meta AND runs the expected commit.

Why (deploy run 37162090599, b6f51f9 / LHH#97): the old verify step called
/api/meta exactly once, 1.3 s after ``systemctl restart``; uvicorn was not
listening yet, curl exited 7, and the deploy "rolled back".  The version check
before it could not tell the new code from the old either: every build is
0.1.7.  This script

  * polls /api/meta with a bounded wait (default 90 s, 5 s steps), so a
    service that is still binding its port is not a failure;
  * compares ``meta.build.commit`` (src/lh_harness/build_info.py, stamped by
    the deploy build job) with ``--expect-commit``: that proves the PROCESS
    answering is the target commit, not just that a package was installed.

Exit codes:
  0  /api/meta answered 200 and the commit matched (or no commit was expected)
  1  no 200 within the wait, wrong service, or a DIFFERENT commit is running
  3  /api/meta answered 200 but reports no build commit, and
     ``--allow-unknown-commit`` was given (a build that predates build info:
     the caller must report "commit unverified", never "verified")

Stdlib only (the lan-deploy runner is a bare Debian 12 box).  The bearer token
comes from the CT110_API_TOKEN environment variable, never an argument, and is
never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_COMMIT_UNKNOWN = 3


def _get_meta(url: str, token: str, timeout: float) -> tuple[int, dict[str, Any] | None, str]:
    """Return (http_status, parsed_body_or_None, error_text); status 0 = no HTTP answer."""
    request = urllib.request.Request(
        url.rstrip("/") + "/api/meta",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as exc:
        return exc.code, None, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return 0, None, f"no answer ({reason})"
    try:
        body = json.loads(raw)
    except ValueError:
        return status, None, "body is not JSON"
    return status, body if isinstance(body, dict) else None, ""


def _write_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def verify(
    url: str,
    token: str,
    *,
    expect_commit: str | None,
    allow_unknown_commit: bool,
    timeout_seconds: float,
    interval_seconds: float,
    request_timeout: float,
    get_meta: Callable[[str, str, float], tuple[int, dict[str, Any] | None, str]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    get_meta = get_meta or _get_meta
    deadline = clock() + max(0.0, timeout_seconds)
    attempt = 0
    while True:
        attempt += 1
        status, body, error = get_meta(url, token, request_timeout)
        if status == 200 and body is not None:
            if body.get("service") != "lh-harness":
                print(f"::error::/api/meta answered but service={body.get('service')!r}, expected 'lh-harness'",
                      file=sys.stderr)
                return EXIT_FAIL
            build = body.get("build") if isinstance(body.get("build"), dict) else {}
            commit = str(build.get("commit") or "").strip()
            version = str(build.get("version") or "unknown")
            _write_output("commit", commit or "unknown")
            _write_output("version", version)
            print(f"GET /api/meta -> 200 after {attempt} attempt(s); build.version={version} "
                  f"build.commit={commit or 'unknown'}")
            if not expect_commit:
                return EXIT_OK
            if commit == expect_commit:
                print(f"META_COMMIT_OK running commit {commit} == expected")
                return EXIT_OK
            if not commit:
                if allow_unknown_commit:
                    print(f"::warning::the running build reports no commit (it predates build info); "
                          f"cannot confirm it is {expect_commit}. Service is up; commit UNVERIFIED.")
                    return EXIT_COMMIT_UNKNOWN
                print(f"::error::the running build reports no commit; expected {expect_commit}. "
                      "Was the wheel built without the _build_info.json step?", file=sys.stderr)
                return EXIT_FAIL
            # A restarted service answering with another commit will not change
            # by waiting: fail now, by name.
            print(f"::error::the running service is commit {commit}, expected {expect_commit}",
                  file=sys.stderr)
            return EXIT_FAIL
        detail = error or f"HTTP {status}"
        remaining = deadline - clock()
        if remaining <= 0:
            print(f"::error::GET /api/meta never answered 200 within {timeout_seconds:g}s "
                  f"({attempt} attempts; last: {detail})", file=sys.stderr)
            return EXIT_FAIL
        print(f"attempt {attempt}: {detail}; retrying in {interval_seconds:g}s "
              f"({remaining:.0f}s left)")
        sleep(min(interval_seconds, max(remaining, 0.0)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="base URL, e.g. http://192.168.21.168:8799")
    parser.add_argument("--expect-commit", default="",
                        help="full commit sha the running service must report in meta.build.commit")
    parser.add_argument("--allow-unknown-commit", action="store_true",
                        help="exit 3 (not 1) when the running build reports no commit at all")
    parser.add_argument("--timeout-seconds", type=float, default=90)
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--request-timeout", type=float, default=10)
    args = parser.parse_args(argv)

    token = os.environ.get("CT110_API_TOKEN", "")
    if not token:
        print("::error::CT110_API_TOKEN is not set (ct110-prod environment secret missing?)", file=sys.stderr)
        return EXIT_FAIL
    return verify(
        args.url,
        token,
        expect_commit=args.expect_commit.strip() or None,
        allow_unknown_commit=args.allow_unknown_commit,
        timeout_seconds=args.timeout_seconds,
        interval_seconds=args.interval_seconds,
        request_timeout=args.request_timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
