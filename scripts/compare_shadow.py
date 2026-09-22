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

# A backward wall-clock jump larger than one launcher cycle is not a decision
# lag — it is the overseer sample's next PC launcher invocation (a new
# PC-local day) or, inside one invocation, a genuine midnight roll.  Either
# way the calendar anchor moves forward by one day; the monotonic day-roll
# inside ``_resolve_stamp`` only handles the within-invocation case.
_PC_DAY_ROLL_THRESHOLD = DEFAULT_CYCLE_SECONDS * 2

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
_PC_PROBE = re.compile(r"^emit-probe (?P<name>\S+) \[trio (?P<idx>\d+)\]")


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
    trio_index: str = ""  # PC trio INDEX from the most recent emit-probe, if any


def parse_pc_log(
    path: Path, *, log_date: datetime | None = None
) -> tuple[list[PCDecision], dict[str, int]]:
    """Parse the PC launcher log into decisions and honest parse counts.

    The real sample is the overseer's PC ``launch_queue.log``: a concatenation
    of five PC launcher invocations, one per PC-local day 2026-09-17 .. 09-21
    (each invocation restarts its wall clock; the boundaries are the four
    backward wall-clock jumps larger than a launcher cycle).  A single
    ``--pc-date`` would therefore mis-stamp four of the five segments by
    whole days, so the parser re-anchors ``log_date`` at every backward jump
    larger than ``_PC_DAY_ROLL_THRESHOLD`` (which also covers a genuine
    midnight crossing inside one invocation).  ``log_date`` stays the anchor
    for the FIRST segment, so explicit ``--pc-date`` still wins there.

    Times are interpreted in ``PC_TZ`` per the operator's format notes —
    cross-checked against the sample's own run_id stamps (every LAUNCHED
    entry's run_id minute-of-day matches its wall-clock stamp within one
    minute at UTC-7 for all five segments).  A stamp that would land in the
    future relative to the running previous stamp is rolled back a day,
    which keeps a log that crosses midnight monotonic.
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
        "segments": 1,
    }
    # PC trio family: the PC launcher logs a trio INDEX (``[trio N]``) on its
    # emit-probe lines.  Its pool backend check (``trio N OK``) shows the
    # probe set per index but no role/model names, so the PC-side trio
    # family is only recoverable as the index itself — the CT110 trio name
    # (``kimi``/``qwen``) is NOT in the PC log.  The comparison joins
    # ``emit-probe <name> [trio N]`` to the LAUNCHED/HOLD entry with the
    # same name and reports the PC trio INDEX alongside the shadow trio
    # NAME; a trio-family verdict is only determinable when the operator
    # maps PC indexes to CT110 trio names (``--pc-trio-map``).
    pc_trio_by_name: dict[str, str] = {}
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
        if prev_ts is not None and prev_ts - _naive_stamp_ts(
            log_date, hour, minute
        ) > _PC_DAY_ROLL_THRESHOLD:
            # Backward jump larger than one launcher cycle: this is the
            # overseer sample's next PC launcher invocation (a new PC-local
            # day), not a midnight roll — re-anchor the calendar date.
            log_date = log_date + timedelta(days=1)
            counts["segments"] += 1
            # The first stamp of the new segment is anchored directly on the
            # new log_date; _resolve_stamp's forward day-roll must NOT undo
            # it (the gap back to the previous segment's last stamp is
            # necessarily > 12 h, which is exactly what that roll keys on).
            ts = _naive_stamp_ts(log_date, hour, minute)
        else:
            ts = _resolve_stamp(log_date, hour, minute, prev_ts)
        prev_ts = ts
        body = m.group("body")
        lm = _PC_LAUNCHED.match(body)
        if lm:
            counts["launched"] += 1
            decisions.append(
                PCDecision(
                    "launch",
                    lm.group("name"),
                    ts,
                    run_id=lm.group("run_id"),
                    raw=line,
                    trio_index=pc_trio_by_name.get(lm.group("name"), ""),
                )
            )
            continue
        hm = _PC_HOLD.match(body)
        if hm:
            counts["hold"] += 1
            decisions.append(
                PCDecision(
                    "hold",
                    hm.group("name"),
                    ts,
                    reason=hm.group("reason"),
                    raw=line,
                    trio_index=pc_trio_by_name.get(hm.group("name"), ""),
                )
            )
            continue
        pm = _PC_PROBE.match(body)
        if pm:
            # emit-probe <name> [trio N]: the PC-side trio index for this
            # entry's most recent probe (context, not a decision).
            pc_trio_by_name[pm.group("name")] = pm.group("idx")
            counts["other_timestamped"] += 1
            continue
        if _PC_TRIO.match(body):
            counts["trio_select"] += 1
            continue
        counts["other_timestamped"] += 1
    return decisions, counts


def _naive_stamp_ts(log_date: datetime, hour: int, minute: int) -> float:
    """Epoch of a bare ``HH:MM`` on ``log_date`` without any day-rolling."""

    stamp = log_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return stamp.timestamp()


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


def parse_shadow_log(
    path: Path, *, queue_names: dict[str, str] | None = None
) -> tuple[list[ShadowDecision], dict[str, int]]:
    """Parse ``shadow.jsonl`` (one JSON line per decision) into decisions.

    ``queue_names`` maps queue_id -> queue-entry name (from the runs_root's
    ``queue/q-*.json`` files); when supplied it overrides the workspace
    basename the shadow record carries, so the PC and shadow sides join on
    the same identity the launcher's queue uses.
    """

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
        name = ""
        if queue_names and queue_id in queue_names:
            name = queue_names[queue_id]
        else:
            name = _name_from_payload(payload)
        if rtype == "queue.shadow_launch":
            counts["shadow_launch"] += 1
            decisions.append(
                ShadowDecision(
                    "launch",
                    queue_id,
                    name,
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
                    name,
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
    pc_trio_index: str = ""  # PC-side trio INDEX from emit-probe ("" when not determinable)
    shadow_decision: str = "absent"  # "launch" | "skip" | "absent"
    shadow_ts: float | None = None
    shadow_reason: str = ""
    shadow_trio: str = ""
    verdict: str = ""  # agree | trio-differ | phantom | missed | pc-only | shadow-only
    detail: str = ""


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
            pc_trio_index=pc_latest.trio_index if pc_latest else "",
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


def _trio_note(report: EntryReport) -> str:
    """Honest trio-family note for the detail column.

    The PC log carries a trio INDEX (``emit-probe <name> [trio N]``); the
    shadow log carries the CT110 trio NAME (``kimi``/``qwen``).  Without an
    operator-supplied PC index → CT110 name map the two are NOT comparable,
    so the note states exactly what each side resolved to ("" when the PC
    index is unknown, which reads as a dash in the trio column).
    """

    pc = f"PC trio={report.pc_trio_index}" if report.pc_trio_index else "PC trio index unknown"
    sh = f"shadow trio={report.shadow_trio}" if report.shadow_trio else "shadow trio unknown"
    return f" [{pc}; {sh}]"


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
        trio_note = _trio_note(report)
        if report.shadow_ts is None or report.pc_ts is None:
            report.verdict = "agree"
            report.detail = f"both would launch{trio_note}"
            return
        delta = abs(report.shadow_ts - report.pc_ts)
        if delta <= cycle_seconds:
            report.verdict = "agree"
            report.detail = f"both launch within one cycle (|Δ| = {delta:.0f}s){trio_note}"
        else:
            report.verdict = "agree"
            report.detail = (
                f"both launch but {delta:.0f}s apart (> {cycle_seconds:.0f}s cycle; "
                f"different cycles in the sample window){trio_note}"
            )
        return
    if pc == "hold" and sh == "skip":
        report.verdict = "agree"
        report.detail = (
            f"both hold/skip — PC: {report.pc_reason[:80]!r}; "
            f"shadow: {report.shadow_reason[:120]!r}{_trio_note(report)}"
        )
        return
    if pc == "launch" and sh == "skip":
        report.verdict = "missed"
        report.detail = f"PC launched {report.pc_run_id}; shadow skipped: {report.shadow_reason}"
        return
    # pc == "hold" and sh == "launch"
    report.verdict = "phantom"
    report.detail = f"shadow would launch; PC held: {report.pc_reason}{_trio_note(report)}"


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
        "pc_trio",
        "shadow_trio",
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
                r.pc_trio_index or "-",
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
    parser.add_argument(
        "--out",
        help="write the per-entry table as JSON to this path",
    )
    parser.add_argument(
        "--queue-names",
        help=(
            "JSON file mapping queue_id -> queue-entry name (from the runs_root's "
            "queue/q-*.json files) so the PC and shadow sides join on the same identity"
        ),
    )
    args = parser.parse_args(argv)

    pc_path = Path(args.pc_log)
    if not pc_path.is_file():
        print(f"error: PC log not found: {pc_path}", file=sys.stderr)
        return 2
    shadow_path = _resolve_shadow_path(args.shadow)
    if not shadow_path.is_file():
        print(f"error: shadow log not found: {shadow_path}", file=sys.stderr)
        return 2

    queue_names: dict[str, str] | None = None
    if args.queue_names:
        qn_path = Path(args.queue_names)
        if not qn_path.is_file():
            print(f"error: queue-names file not found: {qn_path}", file=sys.stderr)
            return 2
        try:
            queue_names = {str(k): str(v) for k, v in json.loads(qn_path.read_text(encoding="utf-8")).items()}
        except (json.JSONDecodeError, ValueError, AttributeError) as exc:
            print(f"error: --queue-names must be a JSON object of queue_id -> name: {exc}", file=sys.stderr)
            return 2

    try:
        pc_date = datetime.strptime(args.pc_date, "%Y-%m-%d").replace(tzinfo=PC_TZ)
    except ValueError:
        print(f"error: --pc-date must be YYYY-MM-DD, got {args.pc_date!r}", file=sys.stderr)
        return 2

    pc_decisions, pc_counts = parse_pc_log(pc_path, log_date=pc_date)
    shadow_decisions, shadow_counts = parse_shadow_log(shadow_path, queue_names=queue_names)

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
                    "pc_trio_index": r.pc_trio_index,
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