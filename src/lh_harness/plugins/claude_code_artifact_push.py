"""Claude Code SessionEnd hook: export the current session as a ContextPacket.

Reference producer for LHH-SESSION-CONTEXT-ARTIFACTS.  The hook is opt-in
and lives outside the task-execution path; LH-Harness runs without the
plugin.  When the plugin is present, Claude Code invokes this script on
SessionEnd events; the script POSTs a small ContextPacket to
``/api/artifacts`` and prints the resulting ``artifact_id`` for the agent
to attach to the next queue entry.

The hook is a *reference*: it intentionally captures a minimal packet
(transcript tail + plan + files touched + links).  The lh-harness
artifact push CLI (cli.py ``lh-harness artifact push``) is the SDK/
producer that an external pipeline uses.

Environment:

* ``LH_HARNESS_BASE_URL`` -- base URL of the LH-Harness web app
  (e.g. ``http://lh-harness.local:8787``).  Required.
* ``LH_HARNESS_WEB_TOKEN`` -- bearer token.  Required.
* ``CLAUDE_SESSION_ID`` -- the local session id (set by the harness /
  Claude Code runtime that invokes the hook).
* ``CLAUDE_SESSION_REF`` -- short hex ref derived from the session id
  (6 chars of the id are enough when the id is UUID-shaped).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MAX_TRANSCRIPT_ROWS = 50
_MAX_EVIDENCE_BYTES = 16 * 1024


def _now_epoch() -> float:
    return datetime.now(tz=timezone.utc).timestamp()


def _bounded(value: str, *, limit: int) -> str:
    return value[:limit]


def build_packet(
    *,
    transcript: list[dict[str, Any]],
    plan: list[dict[str, Any]],
    files_touched: list[dict[str, Any]],
    links: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    title: str,
    summary: str,
    session_id: str,
    session_ref: str,
    host: str,
) -> dict[str, Any]:
    """Build a minimal ContextPacket from the producer's local context."""
    return {
        "schema_version": 1,
        "kind": "session_context",
        "title": _bounded(title or "session handoff", limit=512),
        "summary": _bounded(summary or "exported by Claude Code SessionEnd hook", limit=4096),
        "transcript_delta": transcript[-_MAX_TRANSCRIPT_ROWS:],
        "plan": plan,
        "decisions": [],
        "files_touched": files_touched,
        "links": links,
        "evidence": evidence,
        "producer": {
            "agent": "claude_code",
            "host": host,
            "session_id": session_id,
            "session_ref": session_ref,
            "captured_at": _now_epoch(),
        },
        "visibility": "private",
    }


def post_packet(packet: dict[str, Any], *, base_url: str, token: str) -> dict[str, Any]:
    """POST a packet to ``/api/artifacts`` and return the decoded response body."""
    url = base_url.rstrip("/") + "/api/artifacts"
    data = json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - URL is operator-supplied via env
            body = response.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        # Echo the body's detail field for the operator log; never the bearer.
        try:
            detail = exc.read().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            detail = ""
        raise SystemExit(f"artifact push failed: HTTP {exc.code}: {detail}") from exc


def _load_transcript(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    try:
        items = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(items, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "ts": float(item.get("ts") or 0),
                "role": str(item.get("role") or "system"),
                "text": str(item.get("text") or "")[:8192],
            }
        )
    return rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="lh-harness-claude-code-artifact-push",
        description="Export the current Claude Code session as a ContextPacket.",
    )
    parser.add_argument("--title", default="session handoff")
    parser.add_argument("--summary", default="exported by Claude Code SessionEnd hook")
    parser.add_argument(
        "--transcript-json",
        default=None,
        help="path to a JSON array of {ts,role,text} rows; bounded tail is sent",
    )
    parser.add_argument(
        "--plan-json",
        default=None,
        help="path to a JSON array of {id,text} plan rows",
    )
    parser.add_argument(
        "--files-json",
        default=None,
        help="path to a JSON array of {path,action,lines} file entries",
    )
    parser.add_argument(
        "--evidence-json",
        default=None,
        help="path to a JSON array of evidence blocks",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="print the would-be packet to stdout; do not POST",
    )
    args = parser.parse_args(argv)

    base_url = (os.environ.get("LH_HARNESS_BASE_URL") or "").strip()
    token = (os.environ.get("LH_HARNESS_WEB_TOKEN") or "").strip()
    if not args.print_only and (not base_url or not token):
        print(
            "LH_HARNESS_BASE_URL and LH_HARNESS_WEB_TOKEN are required to push",
            file=sys.stderr,
        )
        return 2

    packet = build_packet(
        transcript=_load_transcript(args.transcript_json),
        plan=json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
        if args.plan_json and Path(args.plan_json).is_file()
        else [],
        files_touched=json.loads(Path(args.files_json).read_text(encoding="utf-8"))
        if args.files_json and Path(args.files_json).is_file()
        else [],
        links=[],
        evidence=json.loads(Path(args.evidence_json).read_text(encoding="utf-8"))
        if args.evidence_json and Path(args.evidence_json).is_file()
        else [],
        title=args.title,
        summary=args.summary,
        session_id=os.environ.get("CLAUDE_SESSION_ID") or "unknown",
        session_ref=os.environ.get("CLAUDE_SESSION_REF") or "",
        host=os.environ.get("HOSTNAME") or "unknown",
    )

    if args.print_only:
        print(json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    response = post_packet(packet, base_url=base_url, token=token)
    print(json.dumps(response, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
