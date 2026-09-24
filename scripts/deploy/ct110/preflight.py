#!/usr/bin/env python3
"""Preflight gate for the CT110 deploy (runs on the lan-deploy runner).

Validates, BEFORE anything touches CT110:
  1. the requested ref (tag vX.Y.Z or sha) resolves to a commit;
  2. for a version tag, the pyproject version at that ref equals the tag
     (same guard release.yml applies to PyPI releases);
  3. the default branch's tip is green: every check run completed with a
     non-failing conclusion, and any legacy commit statuses are success.
     This repo currently has NO push-triggered CI on main (release.yml runs
     on tags only), so a main tip with zero check runs is accepted with a
     loud warning instead of a silent pass.

Writes ``sha`` and ``version`` to $GITHUB_OUTPUT.

Environment: GH_TOKEN (the workflow's GITHUB_TOKEN), GH_REPO (owner/name),
TARGET_REF (the requested tag or sha).  Stdlib only — the runner cannot be
assumed to have `gh` installed (template lesson: grep LAN workflows for
host-only tooling before relying on it).
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
FAILING = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
PASSING = {"success", "neutral", "skipped"}


def get(path: str) -> dict:
    request = urllib.request.Request(
        API + path,
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        print(f"::error::GET {path} -> HTTP {exc.code}: {body}", file=sys.stderr)
        raise SystemExit(1) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"::error::GET {path} failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def output(name: str, value: str) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
        fh.write(f"{name}={value}\n")


def main() -> int:
    repo = os.environ["GH_REPO"]
    ref = os.environ["TARGET_REF"].strip()
    if not ref:
        print("::error::target_ref input is empty", file=sys.stderr)
        return 1

    quoted = urllib.parse.quote(ref, safe="")
    commit = get(f"/repos/{repo}/commits/{quoted}")
    sha = commit.get("sha") or ""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        print(f"::error::ref {ref!r} did not resolve to a commit sha", file=sys.stderr)
        return 1
    print(f"ref {ref!r} resolves to {sha}")

    contents = get(f"/repos/{repo}/contents/pyproject.toml?ref={sha}")
    text = base64.b64decode(contents["content"]).decode("utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        print("::error::could not read project.version from pyproject.toml", file=sys.stderr)
        return 1
    version = match.group(1)
    print(f"pyproject version at {sha[:12]}: {version}")

    if re.fullmatch(r"v\d+\.\d+\.\d+.*", ref):
        tag_version = ref[1:]
        if tag_version != version:
            print(f"::error::tag {ref} does not match pyproject version {version}",
                  file=sys.stderr)
            return 1
        print(f"tag {ref} == pyproject version {version}")

    default_branch = get(f"/repos/{repo}")["default_branch"]
    branch_sha = get(f"/repos/{repo}/commits/{urllib.parse.quote(default_branch, safe='')}")["sha"]
    check_runs = get(f"/repos/{repo}/commits/{branch_sha}/check-runs?per_page=100")
    runs = check_runs.get("check_runs") or []
    if not runs:
        # Real repo fact on 2026-09-24: no workflow triggers on push to main.
        print(f"::warning::no check runs on {default_branch} tip {branch_sha[:12]} — "
              "this repo has no push-triggered CI on main; treating 'main green' as "
              "'nothing is red'. Wire push CI and this gate starts enforcing real green.")
    else:
        pending = [r["name"] for r in runs if r.get("status") != "completed"]
        failed = [r["name"] for r in runs
                  if r.get("status") == "completed" and r.get("conclusion") not in PASSING]
        if pending:
            print(f"::error::{default_branch} has in-flight checks: {pending}", file=sys.stderr)
            return 1
        if failed:
            print(f"::error::{default_branch} is RED: {failed}", file=sys.stderr)
            return 1
        print(f"{default_branch} tip checks green ({len(runs)} completed, none failing)")

    status = get(f"/repos/{repo}/commits/{branch_sha}/status")
    if status.get("total_count", 0) > 0 and status.get("state") != "success":
        print(f"::error::{default_branch} combined status is {status.get('state')!r}",
              file=sys.stderr)
        return 1

    output("sha", sha)
    output("version", version)
    print(f"preflight OK: sha={sha} version={version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
