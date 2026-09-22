#!/usr/bin/env python3
"""compare_shadow.py — PC launcher vs CT110 shadow-launcher agreement (task 173, scope 3).

Reads two decision streams and reports, per queue entry, whether the CT110
launcher in observe (shadow) mode would have made the same launch decision as
the PC launcher that is currently authoritative:

  1. the PC launcher's ``launch_queue.log`` (copied into this workspace by the
     overseer — the path is passed as a CLI argument, the file is NOT part of
     this repository), and
  2. ``shadow.jsonl`` — one JSON line per CT110 shadow decision, as produced by
     a real ``[queue] observe = true`` launcher run (pass the file or the
     runs_root that contains ``queue/shadow.jsonl``).

Per entry the tool classifies the agreement the migration doc's section 6
promotion evidence calls for:

  agree       — same launch/skip decision within one cycle (900 s)
  trio-differ — same decision but the resolved trio family differs
  phantom     — shadow said launch, the PC launcher did not
  missed      — the PC launcher launched, shadow skipped
  pc-only     — the PC launcher launched and shadow has no record of the entry
  shadow-only — shadow decided and the PC log has no trace of the entry

Both streams are only interpretable inside one poll cycle, so every judgement
carries the pair of timestamps that produced it (``--cycle`` seconds, default
900 = the launcher poll interval this comparison is defined against).

Exit status is 0 even when entries disagree (disagreement is the report's
content, not an error); 2 on usage/parse problems.

Usage::

    python3 scripts/compare_shadow.py \\
        --pc-log /path/to/launch_queue.log \\
        --shadow /path/to/runs/queue/shadow.jsonl \\
        [--out agreement.json]

``--shadow`` also accepts a runs_root directory (``<runs_root>/queue/shadow.jsonl``
is used), so the output of a local observe run can be compared directly.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zoneinfo
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# The launcher poll interval (``[queue.capacity] poll_seconds``) the PC
# launcher runs at; two decisions within one cycle describe the same queue
# state and are therefore comparable.
DEFAULT_CYCLE_SECONDS = 900

# The PC launcher logs bare wall-clock times (no date, no seconds) in its own
# local zone.  The overseer's PC fleet runs America/Los_Angeles: the sample's
# run_id UTC stamps (``20260917T103931Z`` on a ``03:39`` line) confirm UTC-7
# exactly for every LAUNCHED entry.  The shadow record's ``ts`` is a UTC epoch
# float.
PC_TZ = zoneinfo.ZoneInfo("America/Los_Angeles")

# Shadow trios are CT110 trio names; the PC launcher logs a trio INDEX for the
# same fleet roles.  Both sides describe a launch as "this trio is healthy and
# an entry may start", so a launch agrees when the shadow trio resolved at all
# (family agreement is reported separately per entry).
SHADOW_TRIO_FAMILY_DEFAULT = "kimi"

# PC line grammar: ``HH:MM`` + one space + body at top level, or ``HH:MM`` +
# three spaces + body on an indented continuation line.  CRLF terminators are
# stripped before matching.  Everything that is not LAUNCHED/HOLD/TRIO
# (``active=...`` status lines, emit-probe timings, SPAN/RECOVERY notes,
# pool-backend check banners, and un-timestamped traceback lines) is context
# only and never a decision.
_PC_LINE = re.compile(r"^(?P<hhmm>\d{2}:\d{2})(?P<sep> +)(?P<body>\S.*?)\s*$")
_PC_LAUNCHED = re.compile(r"^LAUNCHED (?P<name>\S+) (?P<run_id>\S+)$")
_PC_HOLD = re.compile(r"^HOLD (?P<name>\S+) - (?P<reason>.+)$")
_PC_TRIO = re.compile(r"^TRIO SELECT (?P<rest>.+)$")


# ---------------------------------------------------------------------------
# PC log parsing
# ---------------------------------------------------------------------------


@dataclass
class PCDecision:
    """One parsed PC launcher decision (a launch or a hold)."""

    kind: str  # "launch" | "hold"
    name: str
    ts: float  # epoch seconds, PC_TZ assumed on the bare HH:MM
    reason: str = ""
    run_id: str = ""
    raw: str = ""


def parse_pc_log(
    path: Path, *, log_date: datetime | None = None
) -> tuple[list[PCDecision], dict[str, int]]:
    """Parse the PC launcher log into decisions and honest parse counts.

    ``log_date`` pins the calendar date the bare ``HH:MM`` stamps belong to
    (default: 2026-09-18, the final day the sampled log covers).  Times are
    interpreted in ``PC_TZ`` per the operator's format notes — cross-checked
    against the run_id stamps (``20260917T103931Z`` = 03:39 local), which put
    the PC log at UTC-7 exactly.  A stamp that would land in the future
    relative to the log date is rolled back a day, which keeps a log that
    crosses midnight monotonic.
    """

    if log_date is None:
        log_date = datetime(2026, 9, 18, tzinfo=PC_TZ)
    raw = path.read_bytes().decode("utf-8", errors="replace")
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    decisions: list[PCDecision] = []
    counts = {
        "lines": len(raw.splitlines()),
        "timestamped": 0,
        "launched": 0,
        "hold": 0,
        "trio_select": 0,
        "other_timestamped": 0,
        "unparsed": 0,
    }
    prev_ts: float | None = None
    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            continue
        m = _PC_LINE.match(line)
        if m is None:
            # Un-timestamped continuation output (multi-line tool errors, pool
            # backend checks) carries no decision; counted, not a failure.
            counts["unparsed"] += 1
            continue
        counts["timestamped"] += 1
        hhmm = m.group("hhmm")
        try:
            hour, minute = int(hhmm[:2]), int(hhmm[3:])
        except ValueError:
            counts["unparsed"] += 1
            continue
        ts = _resolve_stamp(log_date, hour, minute, prev_ts)
        prev_ts = ts
        body = m.group("body")
        lm = _PC_LAUNCHED.match(body)
        if lm:
            counts["launched"] += 1
            decisions.append(
                PCDecision("launch", lm.group("name"), ts, run_id=lm.group("run_id"), raw=line)
            )
            continue
        hm = _PC_HOLD.match(body)
        if hm:
            counts["hold"] += 1
            decisions.append(
                PCDecision("hold", hm.group("name"), ts, reason=hm.group("reason"), raw=line)
            )
            continue
        if _PC_TRIO.match(body):
            counts["trio_select"] += 1
            continue
        counts["other_timestamped"] += 1
    return decisions, counts


def _resolve_stamp(log_date: datetime, hour: int, minute: int, prev_ts: float | None) -> float:
    """Turn a bare ``HH:MM`` into an epoch under PC_TZ, day-rolling at midnight."""

    stamp = log_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
    epoch = stamp.timestamp()
    # A log that crosses midnight rolls over; keep the sequence monotonic by
    # stepping back a day when the stamp would jump ahead of the previous one.
    if prev_ts is not None and epoch - prev_ts > 12 * 3600:
        epoch = (stamp - timedelta(days=1)).timestamp()
    return epoch


# ---------------------------------------------------------------------------
# Shadow log parsing
# ---------------------------------------------------------------------------


@dataclass
class ShadowDecision:
    """One parsed CT110 shadow decision from ``shadow.jsonl``."""

    kind: str  # "launch" | "skip"
    queue_id: str
    name: str  # the queue entry's name, when recoverable
    ts: float
    trio: str = ""
    reason: str = ""
    would_run_at: float | None = None


def parse_shadow_log(path: Path) -> tuple[list[ShadowDecision], dict[str, int]]:
    """Parse ``shadow.jsonl`` (one JSON line per decision) into decisions."""

    counts = {"lines": 0, "records": 0, "shadow_launch": 0, "shadow_skip": 0, "malformed": 0}
    decisions: list[ShadowDecision] = []
    raw = path.read_bytes().decode("utf-8", errors="replace")
    for lineno, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        counts["lines"] += 1
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            counts["malformed"] += 1
            continue
        if not isinstance(record, dict):
            counts["malformed"] += 1
            continue
        counts["records"] += 1
        rtype = record.get("type", "")
        # Records emitted inside a run's role_orchestration stream carry a
        # ``type`` at the top level; the shadow log stores the service-event
        # shape with ``type`` nested under ``payload``.  Accept both.
        if "type" not in record and isinstance(record.get("payload"), dict):
            rtype = str(record["payload"].get("type", rtype))
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        ts = float(record.get("ts", 0.0) or 0.0)
        queue_id = str(payload.get("queue_id", ""))
        if rtype == "queue.shadow_launch":
            counts["shadow_launch"] += 1
            decisions.append(
                ShadowDecision(
                    "launch",
                    queue_id,
                    _name_from_payload(payload),
                    ts,
                    trio=str(payload.get("trio", "")),
                    would_run_at=payload.get("would_run_at"),
                )
            )
            continue
        if rtype == "queue.shadow_skip":
            counts["shadow_skip"] += 1
            decisions.append(
                ShadowDecision(
                    "skip",
                    queue_id,
                    _name_from_payload(payload),
                    ts,
                    trio=str(payload.get("trio", "")),
                    reason=str(payload.get("reason", "")),
                )
            )
            continue
        counts["malformed"] += 1
    return decisions, counts


def _name_from_payload(payload: dict[str, Any]) -> str:
    """Best-effort queue-entry name from a shadow payload.

    The shadow record does not carry the entry name (only queue_id, trio,
    workspace); the compare tool accepts an optional ``--queue-names`` JSON
    mapping queue_id -> name so PC-side names can be joined.  Without it the
    workspace basename is the closest identity the shadow stream carries.
    """

    workspace = str(payload.get("workspace", "") or "")
    if workspace:
        return workspace.rstrip("/").rsplit("/", 1)[-1]
    return queue_id_or_empty(payload)


def queue_id_or_empty(payload: dict[str, Any]) -> str:
    return str(payload.get("queue_id", ""))


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------


@dataclass
class EntryReport:
    name: str
    pc_decision: str  # "launch" | "hold" | "absent"
    pc_ts: float | None
    pc_reason: str
    pc_run_id: str
    shadow_decision: str  # "launch" | "skip" | "absent"
    shadow_ts: float | None
    shadow_reason: str
    shadow_trio: str
    verdict: str  # agree | trio-differ | phantom | missed | pc-only | shadow-only
    detail: str


def _pc_decisions_by_name(decisions: list[PCDecision]) -> dict[str, list[PCDecision]]:
    grouped: dict[str, list[PCDecision]] = {}
    for d in decisions:
        grouped.setdefault(d.name, []).append(d)
    return grouped


def _shadow_decisions_by_name(decisions: list[ShadowDecision]) -> dict[str, list[ShadowDecision]]:
    grouped: dict[str, list[ShadowDecision]] = {}
    for d in decisions:
        if d.name:
            grouped.setdefault(d.name, []).append(d)
    return grouped


def compare_streams(
    pc: list[PCDecision],
    shadow: list[ShadowDecision],
    *,
    cycle_seconds: float = DEFAULT_CYCLE_SECONDS,
    pc_window: tuple[float, float] | None = None,
) -> list[EntryReport]:
    """Classify per-name agreement between the two decision streams.

    A name is judged by the decisions each side recorded for it inside (or
    nearest to) the comparison window:
    """

    reports: list[EntryReport] = []
    pc_by = _pc_decisions_by_name(pc)
    shadow_by = _shadow_decisions_by_name(shadow)

    def in_window(d: Any) -> bool:
        if pc_window is None:
            return True
        return pc_window[0] <= d.ts <= pc_window[1]

    names = sorted(set(pc_by) | set(shadow_by))
    for name in names:
        pc_events = [d for d in pc_by.get(name, []) if in_window(d)]
        sh_events = [d for d in shadow_by.get(name, []) if in_window(d)]
        pc_latest = _latest_final(pc_events)  # last hold or launch wins the window
        sh_latest = _latest_final(sh_events)
        pc_kind = pc_latest.kind if pc_latest else "absent"
        sh_kind = sh_latest.kind if sh_latest else "absent"
        report = EntryReport(
            name=name,
            pc_decision=pc_kind,
            pc_ts=pc_latest.ts if pc_latest else None,
            pc_reason=pc_latest.reason if pc_latest else "",
            pc_run_id=pc_latest.run_id if pc_latest else "",
            shadow_decision=sh_kind,
            shadow_ts=sh_latest.ts if sh_latest else None,
            shadow_reason=sh_latest.reason if sh_latest else "",
            shadow_trio=sh_latest.trio if sh_latest else "",
            verdict="",
            detail="",
        )
        _classify(report, cycle_seconds)
        reports.append(report)
    return reports


def _latest_final(events: list[Any]) -> Any | None:
    """The last decision for a name in the window (launch or hold/skip)."""

    if not events:
        return None
    return max(events, key=lambda d: d.ts)


def _classify(report: EntryReport, cycle_seconds: float) -> None:
    pc, sh = report.pc_decision, report.shadow_decision
    if pc == "absent" and sh == "absent":
        report.verdict = "shadow-only"
        report.detail = "no decision on either side in the window"
        return
    if pc == "absent":
        if sh == "launch":
            report.verdict = "phantom"
            report.detail = "shadow would launch; the PC log never decided this entry"
        else:
            report.verdict = "shadow-only"
            report.detail = f"shadow skip ({report.shadow_reason}); PC log has no trace"
        return
    if sh == "absent":
        if pc == "launch":
            report.verdict = "pc-only"
            report.detail = f"PC launched {report.pc_run_id}; no shadow record for this entry"
        else:
            report.verdict = "pc-only"
            report.detail = f"PC hold ({report.pc_reason}); no shadow record for this entry"
        return
    if pc == "launch" and sh == "launch":
        if report.shadow_ts is None or report.pc_ts is None:
            report.verdict = "agree"
            report.detail = "both would launch"
            return
        delta = abs(report.shadow_ts - report.pc_ts)
        if delta <= cycle_seconds:
            report.verdict = "agree"
            report.detail = f"both launch within one cycle (|Δ| = {delta:.0f}s)"
        else:
            report.verdict = "agree"
            report.detail = (
                f"both launch but {delta:.0f}s apart (> {cycle_seconds:.0f}s cycle; "
                "different cycles in the sample window)"
            )
        return
    if pc == "hold" and sh == "skip":
        report.verdict = "agree"
        report.detail = (
            f"both hold/skip — PC: {report.pc_reason[:80]!r}; "
            f"shadow: {report.shadow_reason[:120]!r}"
        )
        return
    if pc == "launch" and sh == "skip":
        report.verdict = "missed"
        report.detail = f"PC launched {report.pc_run_id}; shadow skipped: {report.shadow_reason}"
        return
    # pc == "hold" and sh == "launch"
    report.verdict = "phantom"
    report.detail = f"shadow would launch; PC held: {report.pc_reason}"


def _format_ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def render_table(reports: list[EntryReport]) -> str:
    headers = (
        "entry",
        "pc",
        "pc_time",
        "shadow",
        "shadow_time",
        "trio",
        "verdict",
        "detail",
    )
    rows = []
    for r in reports:
        rows.append(
            (
                r.name,
                r.pc_decision,
                _format_ts(r.pc_ts),
                r.shadow_decision,
                _format_ts(r.shadow_ts),
                r.shadow_trio or "-",
                r.verdict,
                r.detail,
            )
        )
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(c[: widths[i]].ljust(widths[i]) for i, c in enumerate(row)))
    return "\n".join(lines)


def summarize(reports: list[EntryReport]) -> str:
    counts: dict[str, int] = {}
    for r in reports:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    parts = []
    for verdict in ("agree", "trio-differ", "phantom", "missed", "pc-only", "shadow-only"):
        if counts.get(verdict):
            parts.append(f"{verdict}={counts[verdict]}")
    total = len(reports)
    agreed = counts.get("agree", 0)
    return f"entries={total}  " + "  ".join(parts) + f"  agreement={agreed}/{total}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_shadow_path(value: str) -> Path:
    path = Path(value)
    if path.is_dir():
        candidate = path / "queue" / "shadow.jsonl"
        if candidate.is_file():
            return candidate
        raise SystemExit(f"no shadow.jsonl under {candidate.parent}")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the PC launcher's launch_queue.log with the CT110 shadow "
            "launcher's shadow.jsonl (task 173 promotion evidence #1-#3)."
        )
    )
    parser.add_argument("--pc-log", required=True, help="path to the PC launcher's launch_queue.log")
    parser.add_argument(
        "--shadow",
        required=True,
        help="path to shadow.jsonl (or the runs_root that contains queue/shadow.jsonl)",
    )
    parser.add_argument(
        "--cycle",
        type=float,
        default=DEFAULT_CYCLE_SECONDS,
        help=f"poll cycle for 'same decision' (seconds, default {DEFAULT_CYCLE_SECONDS})",
    )
    parser.add_argument(
        "--pc-date",
        default="2026-09-18",
        help="calendar date the PC log's bare HH:MM stamps belong to (default 2026-09-18, the sample's final day)",
    )
    parser.add_argument("--out", help="write the per-entry table as JSON to this path")
    args = parser.parse_args(argv)

    pc_path = Path(args.pc_log)
    if not pc_path.is_file():
        print(f"error: PC log not found: {pc_path}", file=sys.stderr)
        return 2
    shadow_path = _resolve_shadow_path(args.shadow)
    if not shadow_path.is_file():
        print(f"error: shadow log not found: {shadow_path}", file=sys.stderr)
        return 2

    try:
        pc_date = datetime.strptime(args.pc_date, "%Y-%m-%d").replace(tzinfo=PC_TZ)
    except ValueError:
        print(f"error: --pc-date must be YYYY-MM-DD, got {args.pc_date!r}", file=sys.stderr)
        return 2

    pc_decisions, pc_counts = parse_pc_log(pc_path, log_date=pc_date)
    shadow_decisions, shadow_counts = parse_shadow_log(shadow_path)

    if pc_counts["launched"] == 0:
        print("error: no LAUNCHED lines parsed from the PC log — refusing to guess", file=sys.stderr)
        return 2
    if shadow_counts["records"] == 0:
        print("error: no shadow records parsed — refusing to guess", file=sys.stderr)
        return 2

    # The two samples are normally taken at different times (the PC log is a
    # retrospective sample; the shadow run is local).  Entry identity joins by
    # name, so no time window is imposed by default: every entry each side
    # decided is compared, and the "within one cycle" test still uses the
    # decision timestamps.  Pass --pc-date to pin the PC stamps' calendar date
    # when the sample needs exact cycle arithmetic.
    reports = compare_streams(pc_decisions, shadow_decisions, cycle_seconds=args.cycle)

    print("PC log parse counts:")
    for key, value in pc_counts.items():
        print(f"  {key}: {value}")
    print()
    print("shadow log parse counts:")
    for key, value in shadow_counts.items():
        print(f"  {key}: {value}")
    print()
    print(render_table(reports))
    print()
    print(summarize(reports))

    if args.out:
        out = {
            "pc_log": str(pc_path),
            "shadow_log": str(shadow_path),
            "cycle_seconds": args.cycle,
            "pc_parse_counts": pc_counts,
            "shadow_parse_counts": shadow_counts,
            "entries": [
                {
                    "name": r.name,
                    "pc_decision": r.pc_decision,
                    "pc_ts": r.pc_ts,
                    "pc_reason": r.pc_reason,
                    "pc_run_id": r.pc_run_id,
                    "shadow_decision": r.shadow_decision,
                    "shadow_ts": r.shadow_ts,
                    "shadow_reason": r.shadow_reason,
                    "shadow_trio": r.shadow_trio,
                    "verdict": r.verdict,
                    "detail": r.detail,
                }
                for r in reports
            ],
            "summary": summarize(reports),
        }
        Path(args.out).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())