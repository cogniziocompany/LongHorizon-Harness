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
promotion evidence calls for.  The buckets map onto cutover task 168's three
evidence items like this:

  agree             — same launch/skip decision for the entry (evidence item 1)
  trio-differ       — same decision but the resolved trio family differs
  phantom           — shadow said launch, the PC launcher did not (item 2)
  missed            — the PC launcher launched, shadow skipped (item 2)
  self-occupied     — the shadow's skip names the very run the PC launcher
                      created for this same entry: the shadow correctly
                      observing the PC's own launch, neither agree nor missed
  unobserved-race   — the PC launcher decided before the CT110 launcher's
                      first pass, so the shadow never observed the entry
                      pre-launch (item 3: unobservable, NOT a disagreement)
  pc-only           — the PC log decided the entry, the shadow has no record
                      of it at all (item 3: coverage gap)
  shadow-only       — shadow decided and the PC log has no trace (item 3)
  probe             — overseer/tick probe traffic (workspace names wrapped in
                      double underscores, e.g. ``__tick1558-...-not-exist__``);
                      never counted against evidence item 2

Task 222 judgement rule (replaces "latest shadow record wins"): the PC
decision is compared against the shadow's LATEST decision AT OR BEFORE the
PC's decision time — the PC launcher's log carries ``HH:MM`` only, so when the
run_id does not embed a UTC stamp the decision time is the minute stamp plus a
one-minute tolerance (the true second is unknown within that minute).  Shadow
records written AFTER the PC's launch describe the workspace the PC run itself
now occupies and must never flip an agreed launch into a "missed".

Queue-entry identity: the PC log's own ``shadow-filed <name>.json <queue_id>``
lines are the DURABLE queue-name map (the launcher deletes each
``runs_root/queue/q-*.json`` entry at launch, so those files do not survive
the window).  The map is derived from the PC log by default;
``--queue-names`` remains as an operator override.

Date anchoring: the PC log carries ``HH:MM`` only, so the FIRST line's date
must be exact or every stamp shifts by whole days.  ``--since TEXT`` together
with ``--since-date YYYY-MM-DD`` slices the log at a line the operator can
date (instead of hand-trimming the file) and anchors that line exactly.
Recommended PC-side fix (NOT made here): have the PC launcher print one full
ISO date line at startup and at each midnight roll so no anchor is needed.

Exit status is 0 even when entries disagree (disagreement is the report's
content, not an error); 2 on usage/parse problems.

Usage::

    python3 scripts/compare_shadow.py \\
        --pc-log /path/to/launch_queue.log \\
        --shadow /path/to/runs/queue/shadow.jsonl \\
        [--pc-date 2026-09-22 | --since "20:56 shadow-filed 9999zv-221" --since-date 2026-09-22] \\
        [--queue-names names.json] \\
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

# PC log stamps are ``HH:MM`` — the decision's true second is unknown but lies
# inside [stamp, stamp+60s).  A shadow decision inside that minute may equally
# have preceded the PC launch, so the "at or before the PC decision" test
# allows one minute of tolerance unless the run_id carries an exact UTC stamp.
_PC_STAMP_TOLERANCE_S = 60.0

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
# ``HH:MM shadow-filed <name>.json <queue_id>`` — the PC launcher logs one of
# these for every queue entry it files, and the launcher DELETES the API entry
# at launch, so this log line is the only durable queue_id -> name map.
_PC_SHADOW_FILED = re.compile(r"^shadow-filed\s+(?P<name>\S+?)(?:\.json)?\s+(?P<qid>\S+)\s*$")
# A run_id may embed the exact launch instant (``20260923T035722Z_d1d74675``).
_PC_RUN_STAMP = re.compile(r"^(?P<stamp>\d{8}T\d{6})Z")
# A shadow skip whose reason names the occupying run: the CT110 launcher's
# occupancy skip reads ``workspace <ws> has active run <run_id>``.
_SHADOW_SKIP_RUN = re.compile(r"has active run\s+(?P<run_id>\S+)")


# ---------------------------------------------------------------------------
# PC log parsing
# ---------------------------------------------------------------------------


class PCLogAnchorError(ValueError):
    """``--since`` text matched no line in the PC log."""


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
    # Exact decision instant recovered from the run_id's own UTC stamp
    # (``20260923T035722Z_d1d74675``), when the run_id carries one.  The log
    # line itself only has ``HH:MM``; this recovers the seconds.
    exact_ts: float | None = None


def parse_pc_log(
    path: Path,
    *,
    log_date: datetime | None = None,
    since_text: str | None = None,
    since_date: datetime | None = None,
) -> tuple[list[PCDecision], dict[str, int], dict[str, str]]:
    """Parse the PC launcher log into decisions, honest parse counts, and the
    queue-name map taken from the log's own ``shadow-filed`` lines.

    The real sample is the overseer's PC ``launch_queue.log``: a concatenation
    of PC launcher invocations, one per PC-local day (each invocation restarts
    its wall clock; the boundaries are the backward wall-clock jumps larger
    than a launcher cycle).  ``log_date`` stays the anchor for the FIRST
    segment, so explicit ``--pc-date`` still wins there — the operator MUST
    get that first date right, and ``--since TEXT --since-date YYYY-MM-DD``
    is the safe way to do it: everything before the first line containing
    TEXT is dropped (no more hand-trimming) and that line is anchored on
    ``since_date`` exactly.  Later segments still re-anchor at every backward
    jump larger than ``_PC_DAY_ROLL_THRESHOLD`` (which also covers a genuine
    midnight crossing inside one invocation).

    Times are interpreted in ``PC_TZ`` per the operator's format notes —
    cross-checked against the sample's own run_id stamps (every LAUNCHED
    entry's run_id minute-of-day matches its wall-clock stamp within one
    minute at UTC-7).  A stamp that would land in the future relative to the
    running previous stamp is rolled back a day, which keeps a log that
    crosses midnight monotonic.

    Returns ``(decisions, counts, queue_names)`` where ``queue_names`` maps
    queue_id -> queue-entry name from the log's ``shadow-filed`` lines — the
    durable identity join between the two streams (the ``q-*.json`` API
    entries are deleted at launch and do not survive the shadow window).
    """

    if log_date is None:
        log_date = datetime(2026, 9, 18, tzinfo=PC_TZ)
    if since_text is not None:
        if since_date is None:
            raise PCLogAnchorError("--since requires --since-date")
        log_date = since_date
    raw = path.read_bytes().decode("utf-8", errors="replace")
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    decisions: list[PCDecision] = []
    counts = {
        "lines": len(raw.splitlines()),
        "timestamped": 0,
        "launched": 0,
        "hold": 0,
        "shadow_filed": 0,
        "trio_select": 0,
        "other_timestamped": 0,
        "unparsed": 0,
        "before_since": 0,
        "segments": 1,
    }
    # queue_id -> queue-entry name, from the PC log's own shadow-filed lines
    # (durable across the window, unlike the deleted API entry files).
    queue_names: dict[str, str] = {}
    # PC trio family: the PC launcher logs a trio INDEX (``[trio N]``) on its
    # emit-probe lines.  Its pool backend check (``trio N OK``) shows the
    # probe set per index but no role/model names, so the PC-side trio
    # family is only recoverable as the index itself — the CT110 trio name
    # (``kimi``/``qwen``) is NOT in the PC log.  The comparison joins
    # ``emit-probe <name> [trio N]`` to the LAUNCHED/HOLD entry with the
    # same name and reports the PC trio INDEX alongside the shadow trio
    # NAME; a trio-family verdict is only determinable when the operator
    # maps PC indexes to CT110 trio names (``--pc-trio-map``, operator-supplied);
    # until that map is available the PC trio family is not determinable and is
    # reported honestly.
    pc_trio_by_name: dict[str, str] = {}
    prev_ts: float | None = None
    seen_since = since_text is None
    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if not seen_since:
            if since_text in line:
                seen_since = True
            else:
                counts["before_since"] += 1
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
            run_id = lm.group("run_id")
            decisions.append(
                PCDecision(
                    "launch",
                    lm.group("name"),
                    ts,
                    run_id=run_id,
                    raw=line,
                    exact_ts=_run_id_stamp_ts(run_id),
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
        sm = _PC_SHADOW_FILED.match(body)
        if sm:
            # ``shadow-filed <name>.json <queue_id>``: the durable queue-name
            # map source (the q-*.json API entry is deleted at launch).
            counts["shadow_filed"] += 1
            queue_names[sm.group("qid")] = sm.group("name")
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
    if not seen_since:
        raise PCLogAnchorError(f"--since text matched no line in the PC log: {since_text!r}")
    return decisions, counts, queue_names


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


def _run_id_stamp_ts(run_id: str) -> float | None:
    """Exact UTC epoch a run_id embeds (``20260923T035722Z_d1d74675``), or None."""

    m = _PC_RUN_STAMP.match(run_id)
    if m is None:
        return None
    try:
        return datetime.strptime(m.group("stamp"), "%Y%m%dT%H%M%S").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except ValueError:
        return None


def pc_compare_ts(decision: PCDecision) -> float:
    """The PC decision's compare instant.

    The run_id's own UTC stamp when it has one (exact); otherwise the minute
    stamp plus ``_PC_STAMP_TOLERANCE_S``, because a bare ``HH:MM`` line only
    pins the decision to a minute.
    """

    if decision.exact_ts is not None:
        return decision.exact_ts
    return decision.ts + _PC_STAMP_TOLERANCE_S


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
    workspace: str = ""
    would_run_at: float | None = None


def parse_shadow_log(
    path: Path, *, queue_names: dict[str, str] | None = None
) -> tuple[list[ShadowDecision], dict[str, int]]:
    """Parse ``shadow.jsonl`` (one JSON line per decision) into decisions.

    ``queue_names`` maps queue_id -> queue-entry name.  By default the tool
    derives it from the PC log's ``shadow-filed`` lines (the durable source);
    a ``--queue-names`` file overrides it.  Without any map the workspace
    basename the shadow record carries is the closest identity the shadow
    stream carries.
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
                    workspace=str(payload.get("workspace", "") or ""),
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
                    workspace=str(payload.get("workspace", "") or ""),
                )
            )
            continue
        counts["malformed"] += 1
    return decisions, counts


def _name_from_payload(payload: dict[str, Any]) -> str:
    """Best-effort queue-entry name from a shadow payload.

    The shadow record does not carry the entry name (only queue_id, trio,
    workspace).  The compare tool derives the queue_id -> name map from the
    PC log's ``shadow-filed`` lines by default and accepts ``--queue-names``
    as an operator override; without either, the workspace basename is the
    closest identity the shadow stream carries.
    """

    workspace = str(payload.get("workspace", "") or "")
    if workspace:
        return _workspace_basename(workspace)
    return queue_id_or_empty(payload)


def _workspace_basename(workspace: str) -> str:
    return workspace.rstrip("/").rsplit("/", 1)[-1]


def queue_id_or_empty(payload: dict[str, Any]) -> str:
    return str(payload.get("queue_id", ""))


def _is_probe_name(name: str) -> bool:
    """Overseer/tick probe identity: a name (or workspace basename) wrapped
    in double underscores, e.g. ``__tick1558-workspace-does-not-exist__``."""

    return len(name) >= 5 and name.startswith("__") and name.endswith("__")


def _run_id_from_skip_reason(reason: str) -> str:
    """The occupying run_id inside the CT110 occupancy skip reason, or ''."""

    m = _SHADOW_SKIP_RUN.search(reason)
    return m.group("run_id") if m else ""


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
    shadow_decision: str = "absent"  # "launch" | "skip" | "absent" (the JUDGED basis decision)
    shadow_ts: float | None = None
    shadow_reason: str = ""
    shadow_trio: str = ""
    # Count of later shadow skips whose occupying run is the PC's own run for
    # this entry (expected shadow-mode output after the PC launch; context).
    self_occupied_after: int = 0
    probe: bool = False
    verdict: str = ""  # agree | trio-differ | phantom | missed | self-occupied | unobserved-race | pc-only | shadow-only | probe
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

    A name is judged by the PC decision and the shadow's latest decision AT
    OR BEFORE the PC decision time (task 222): the CT110 launcher re-decides
    every pending entry each pass, so once the PC has launched, every later
    shadow record for that entry is the shadow observing the PC's own run
    (a self-occupied skip) — judging on the latest record would score the
    shadow's correct observation as a missed launch.
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
        report = EntryReport(
            name=name,
            pc_decision=pc_latest.kind if pc_latest else "absent",
            pc_ts=pc_latest.ts if pc_latest else None,
            pc_reason=pc_latest.reason if pc_latest else "",
            pc_run_id=pc_latest.run_id if pc_latest else "",
            pc_trio_index=pc_latest.trio_index if pc_latest else "",
            probe=_is_probe_name(name)
            or bool(pc_latest and _is_probe_name(pc_latest.name))
            or any(_is_probe_name(_workspace_basename(d.workspace)) for d in sh_events),
        )
        if report.probe:
            report.verdict = "probe"
            report.detail = (
                "overseer/tick probe (workspace name wrapped in double underscores, "
                "e.g. __tick1558-workspace-does-not-exist__): synthetic probe traffic, "
                "excluded from every agreement denominator; never evidence item 1 or 2"
            )
            reports.append(report)
            continue
        _classify(
            report,
            cycle_seconds,
            pc_latest=pc_latest,
            sh_events=sh_events,
            pc_cmp=pc_compare_ts(pc_latest) if pc_latest else None,
        )
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


def _classify(
    report: EntryReport,
    cycle_seconds: float,
    *,
    pc_latest: Any | None,
    sh_events: list[Any],
    pc_cmp: float | None,
) -> None:
    pc, sh = report.pc_decision, report.shadow_decision

    # Later shadow skips whose occupying run is the PC's own run for this
    # entry: the shadow observing the PC's launch, never a disagreement.
    if pc_latest is not None and pc_latest.run_id:
        report.self_occupied_after = sum(
            1
            for d in sh_events
            if d.kind == "skip"
            and pc_cmp is not None
            and d.ts > pc_cmp
            and _run_id_from_skip_reason(d.reason) == pc_latest.run_id
        )

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

    # The PC decided.  Judge against the shadow's latest decision AT OR
    # BEFORE the PC decision time (within the tolerance of the PC log's
    # HH:MM stamp) — never against the latest record overall, which is
    # always post-launch and self-occupied.
    basis: Any | None = None
    if pc_cmp is not None:
        at_or_before = [d for d in sh_events if d.ts <= pc_cmp]
        basis = max(at_or_before, key=lambda d: d.ts) if at_or_before else None
    if basis is not None:
        report.shadow_decision = basis.kind
        report.shadow_ts = basis.ts
        report.shadow_reason = basis.reason
        report.shadow_trio = basis.trio
    elif sh_events:
        # The PC decided before the CT110 launcher's first pass for this
        # entry: the shadow never observed the entry pre-launch, so agreement
        # is unobservable.  (217-clawdtalk on the real window: the shadow's
        # only records are the PC run's own occupancy.)
        report.verdict = "unobserved-race"
        report.detail = (
            f"PC {pc} at {_format_ts(report.pc_ts)} precedes the CT110 launcher's first "
            f"pass for this entry ({len(sh_events)} later shadow record(s), "
            f"{report.self_occupied_after} naming the PC run's own occupancy) — "
            "evidence item 3: UNOBSERVED, not a disagreement; excluded from the "
            "item-1 agreement denominator"
        )
        return

    sh = report.shadow_decision
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
        delta = abs(report.shadow_ts - pc_compare_ts(pc_latest))
        if delta <= cycle_seconds:
            report.verdict = "agree"
            report.detail = f"both launch within one cycle (|Δ| = {delta:.0f}s){trio_note}"
        else:
            report.verdict = "agree"
            report.detail = (
                f"both launch but {delta:.0f}s apart (> {cycle_seconds:.0f}s cycle; "
                f"different cycles in the sample window){trio_note}"
            )
        if report.self_occupied_after:
            report.detail += (
                f" ({report.self_occupied_after} later self-occupied skip(s) after the "
                "launch ignored — the PC run's own occupancy)"
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
        occupier = _run_id_from_skip_reason(report.shadow_reason)
        if pc_latest is not None and pc_latest.run_id and occupier == pc_latest.run_id:
            report.verdict = "self-occupied"
            report.detail = (
                f"PC launched {report.pc_run_id}; the shadow's skip names that same run "
                f"({report.shadow_reason[:120]}) — the shadow observing the PC's own "
                "launch, neither agree nor missed"
            )
            return
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


_VERDICT_ORDER = (
    "agree",
    "trio-differ",
    "phantom",
    "missed",
    "self-occupied",
    "unobserved-race",
    "pc-only",
    "shadow-only",
    "probe",
)

# Buckets that CAN agree or disagree: the observable-agreement denominator.
_OBSERVABLE_VERDICTS = ("agree", "trio-differ", "phantom", "missed")


def summarize(reports: list[EntryReport]) -> str:
    counts = verdict_counts(reports)
    parts = [f"{verdict}={counts[verdict]}" for verdict in _VERDICT_ORDER if counts.get(verdict)]
    agreed = counts.get("agree", 0)
    observable = sum(counts.get(v, 0) for v in _OBSERVABLE_VERDICTS)
    line = f"entries={len(reports)}  " + "  ".join(parts)
    line += f"  observable-agreement={agreed}/{observable}"
    return line


def verdict_counts(reports: list[EntryReport]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in reports:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    return counts


# Per-bucket meaning for cutover task 168's three evidence items, so the
# day-7 report is readable without the task text:
#   item 1 — launch-decision agreement (the CT110 launcher would have made
#            the same call as the PC launcher)
#   item 2 — disagreement evidence (the CT110 launcher would have diverged)
#   item 3 — observability/coverage accounting (what was observable at all)
EVIDENCE_LEGEND: tuple[tuple[str, str], ...] = (
    (
        "agree",
        "evidence item 1 (launch-decision agreement) — counts FOR promotion: the CT110 "
        "launcher would have made the same call as the PC launcher.",
    ),
    (
        "trio-differ",
        "evidence item 1 (partial) — same launch/skip decision but a different trio family; "
        "report the family delta at day 7.",
    ),
    (
        "phantom",
        "evidence item 2 (disagreement) — the CT110 launcher would have launched where the "
        "PC launcher held; a day-7 promotion blocker until explained.",
    ),
    (
        "missed",
        "evidence item 2 (disagreement) — the PC launcher launched where the CT110 launcher "
        "would have skipped; a day-7 promotion blocker until explained.",
    ),
    (
        "self-occupied",
        "NEITHER evidence item 1 nor 2 — the shadow's skip names the very run the PC launcher "
        "created for this same entry, i.e. the shadow correctly observing the PC's own launch "
        "(expected shadow-mode output after every agreed launch). Never score as missed; "
        "counted under item 3 observability.",
    ),
    (
        "unobserved-race",
        "evidence item 3 (observability) — the PC launcher decided before the CT110 launcher's "
        "first pass, so the entry's pre-launch state was never observed and agreement is "
        "UNOBSERVABLE, not a disagreement. Evidence item 3 must list it as unobserved: it "
        "neither supports nor opposes item 1 and is excluded from the observable-agreement "
        "denominator. Do not re-run the tool expecting it to resolve; the race is a property "
        "of the two launchers' independent clocks.",
    ),
    (
        "pc-only",
        "evidence item 3 (coverage gap) — the PC log decided this entry and the shadow stream "
        "has no record for it at all; not agreement evidence, list at day 7 as unobserved.",
    ),
    (
        "shadow-only",
        "evidence item 3 (coverage gap) — the shadow decided this entry and the PC log has no "
        "trace of it; not disagreement evidence, list at day 7 as unobserved.",
    ),
    (
        "probe",
        "overseer/tick probe traffic (workspace name wrapped in double underscores, e.g. "
        "__tick1558-workspace-does-not-exist__) — synthetic probe workspaces, never a real "
        "queue entry; excluded from every agreement denominator and NEVER counted against "
        "evidence item 2.",
    ),
)


def print_legend(counts: dict[str, int]) -> None:
    """Print what each verdict bucket means for evidence items 1-3."""

    print("verdict buckets -> cutover task 168 evidence items (items: 1 launch-agreement, "
          "2 disagreement, 3 observability):")
    for verdict, text in EVIDENCE_LEGEND:
        print(f"  {verdict} ({counts.get(verdict, 0)}): {text}")


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
        default="2026-09-17",
        help=(
            "calendar date the PC log's FIRST segment belongs to "
            "(default 2026-09-17: the real overseer sample's first PC-local day, "
            "verified entry-by-entry against every LAUNCHED run_id's own UTC stamp). "
            "The log carries HH:MM only, so this MUST be exact — prefer --since."
        ),
    )
    parser.add_argument(
        "--since",
        help=(
            "substring of a PC log line the operator can date; the log is sliced at "
            "that line (everything before it is dropped) and the line is anchored on "
            "--since-date exactly — no hand-trimming, no silent day roll"
        ),
    )
    parser.add_argument(
        "--since-date",
        help="YYYY-MM-DD the --since anchor line belongs to (PC-local calendar date)",
    )
    parser.add_argument(
        "--out",
        help="write the per-entry table as JSON to this path",
    )
    parser.add_argument(
        "--queue-names",
        help=(
            "OVERRIDE only: JSON file mapping queue_id -> queue-entry name. By default "
            "the map is derived from the PC log's own 'shadow-filed <name>.json "
            "<queue_id>' lines (the durable source — the runs_root's queue/q-*.json API "
            "entries are DELETED at launch and do not survive the shadow window)"
        ),
    )
    args = parser.parse_args(argv)

    if args.since and not args.since_date:
        print("error: --since requires --since-date YYYY-MM-DD (the anchor line's date)", file=sys.stderr)
        return 2
    if args.since_date and not args.since:
        print("error: --since-date requires --since", file=sys.stderr)
        return 2

    pc_path = Path(args.pc_log)
    if not pc_path.is_file():
        print(f"error: PC log not found: {pc_path}", file=sys.stderr)
        return 2
    shadow_path = _resolve_shadow_path(args.shadow)
    if not shadow_path.is_file():
        print(f"error: shadow log not found: {shadow_path}", file=sys.stderr)
        return 2

    queue_names_override: dict[str, str] | None = None
    if args.queue_names:
        qn_path = Path(args.queue_names)
        if not qn_path.is_file():
            print(f"error: queue-names file not found: {qn_path}", file=sys.stderr)
            return 2
        try:
            queue_names_override = {
                str(k): str(v) for k, v in json.loads(qn_path.read_text(encoding="utf-8")).items()
            }
        except (json.JSONDecodeError, ValueError, AttributeError) as exc:
            print(f"error: --queue-names must be a JSON object of queue_id -> name: {exc}", file=sys.stderr)
            return 2

    since_date: datetime | None = None
    if args.since_date:
        try:
            since_date = datetime.strptime(args.since_date, "%Y-%m-%d").replace(tzinfo=PC_TZ)
        except ValueError:
            print(f"error: --since-date must be YYYY-MM-DD, got {args.since_date!r}", file=sys.stderr)
            return 2

    try:
        pc_date = datetime.strptime(args.pc_date, "%Y-%m-%d").replace(tzinfo=PC_TZ)
    except ValueError:
        print(f"error: --pc-date must be YYYY-MM-DD, got {args.pc_date!r}", file=sys.stderr)
        return 2

    try:
        pc_decisions, pc_counts, queue_names = parse_pc_log(
            pc_path,
            log_date=pc_date,
            since_text=args.since,
            since_date=since_date,
        )
    except PCLogAnchorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # --queue-names is an override: the PC log's shadow-filed lines are the
    # default source of the queue_id -> name map.
    queue_names_source = (
        f"pc-log shadow-filed lines ({len(queue_names)} entries)"
        if queue_names
        else "none (falling back to shadow workspace basenames)"
    )
    if queue_names_override is not None:
        queue_names_source = "--queue-names override file"
        queue_names = queue_names_override

    shadow_decisions, shadow_counts = parse_shadow_log(shadow_path, queue_names=queue_names or None)

    if pc_counts["launched"] == 0:
        print("error: no LAUNCHED lines parsed from the PC log — refusing to guess", file=sys.stderr)
        return 2
    if shadow_counts["records"] == 0:
        print("error: no shadow records parsed — refusing to guess", file=sys.stderr)
        return 2

    # The two samples are normally taken at different times (the PC log is a
    # retrospective sample; the shadow run is local).  Entry identity joins by
    # name, so no time window is imposed by default: every entry each side
    # decided is compared, and each PC decision is judged against the shadow's
    # latest decision at-or-before the PC's decision time.  Pass --since with
    # --since-date to pin the PC stamps' calendar date to a known dated line
    # (instead of hand-trimming the file and trusting --pc-date).
    reports = compare_streams(pc_decisions, shadow_decisions, cycle_seconds=args.cycle)

    print("PC log parse counts:")
    anchor = (
        f"--since anchor {args.since!r} on {args.since_date}"
        if args.since
        else f"--pc-date {args.pc_date} (first segment)"
    )
    print(f"  anchor: {anchor}")
    for key, value in pc_counts.items():
        print(f"  {key}: {value}")
    print(f"  queue_names source: {queue_names_source}")
    print()
    print("shadow log parse counts:")
    for key, value in shadow_counts.items():
        print(f"  {key}: {value}")
    print()
    print(render_table(reports))
    print()
    counts = verdict_counts(reports)
    print(summarize(reports))
    print()
    print_legend(counts)

    if args.out:
        out = {
            "pc_log": str(pc_path),
            "shadow_log": str(shadow_path),
            "cycle_seconds": args.cycle,
            "pc_parse_counts": pc_counts,
            "shadow_parse_counts": shadow_counts,
            "queue_names_source": queue_names_source,
            "queue_names": queue_names,
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
                    "self_occupied_after": r.self_occupied_after,
                    "probe": r.probe,
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