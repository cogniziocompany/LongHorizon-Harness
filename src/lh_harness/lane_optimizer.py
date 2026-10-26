"""tev1-powered lane optimizer (TEV1-LANE-OPTIMIZER-a, Paxton 2026-10-26).

Every overseer tick, the local tev1 model reads queue state plus provider plan
usage and SUGGESTS a lane/workspace rearrangement before the next tick;
Overseer1 reviews and applies.  This module is the suggester.  It is
SUGGEST-ONLY: it never moves, cancels or enqueues anything, never writes to
the queue store, the settings DB, or any workspace, and it never invokes the
apply path (``~/bin/q-move-ct111.sh`` is out of scope on purpose).  The only
outputs are JSON structures a human (or an audited automation) may act on.

WHY: on 2026-10-26 05:52Z, pending CT111 entries sat on the one full lane
(glm 5/5) while the z.ai (92% weekly unused) and Synthetic (99.8% unused)
lanes had cap 0, and two workspaces were blocked by dirty trees.  Nobody
flagged it.  This module flags exactly that shape every tick.

What is reused, per the build order:

* PR #162's tev1 client surface (``trio_router._call_systemone``,
  ``trio_router._resolve_settings``, the ``/v1/systemone`` payload shape and
  the ``TRIO_ROUTER_*`` constants).  No second tev1 client exists.
* ``SettingsStore.queue_trios()`` / ``queue_capacity_rows()`` for per-trio
  caps and enablement (the same rows the launcher's hot-reload reads).
* The launcher's skip-reason formats (``"<trio> at capacity"``,
  ``"workspace <ws> occupied: dirty tree ..."`` etc.) so the snapshot can
  read a blocked entry's last skip without re-probing workspaces.
* The launcher's peak-window helpers for the effective cap.
* The repo's redaction primitives (``settings_store._SECRET_NAME_RE``,
  ``safe_subprocess``'s deny lists) so serialized output can never carry env
  or key values.

Provider plan usage (ops_status_quota ``plan_usage[]``) is injected by the
caller as ``usage_rows`` — this harness-side module has no authorized route
to the fleet usage plane, and none was added.  With no usage rows the rules
degrade honestly: lanes without usage evidence are "unknown" and are never
move targets, and the output says so.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import trio_router as _trio_router
from .launcher import Launcher as _Launcher
from .queue import QueueEntry
from .queue import _QUEUE_DIR as _QUEUE_DIR_NAME
from .safe_subprocess import DENY_ENV_NAMES, DENY_ENV_PREFIXES
from .settings_store import _SECRET_NAME_RE as _SECRET_ENV_NAME_RE

logger = logging.getLogger(__name__)

# Thresholds from the task text (Paxton 2026-10-26 07:05Z): an idle PAID lane
# is a metered lane at <50% of its plan window; a lane at >=85% used is HOT
# and nothing may be suggested onto it.
IDLE_PAID_LANE_MAX_USED_PCT = 50.0
HOT_LANE_MIN_USED_PCT = 85.0

# Cap suggestions are raise-only and by one slot: the incident had usable paid
# lanes pinned at cap 0, so the deterministic action is "open one slot, let
# the next tick consume it".  Never a decrease, never a model-supplied jump.
CAP_RAISE_TO = 1

# The only workspace action this module may ever emit.  A dirty tree blocks a
# pending entry; the safe suggestion is a clone next to it — never stash,
# never reset, never discard someone else's uncommitted work.
WORKSPACE_ACTION_ADD_CLONE = "add_clone"

# Fields kept from a caller-injected provider usage row.  Everything else is
# dropped so a meter payload can never carry unexpected keys (and never a
# credential value) into the snapshot or the optimizer output.
USAGE_ROW_FIELDS = (
    "trio",
    "provider",
    "plan",
    "window",
    "used_pct",
    "reset_at",
    "observed_at",
    "observed_by",
)

_MAX_REASON_CHARS = 300
_VALID_QUEUE_STATUS = frozenset({"pending", "launched", "done", "failed", "blocked"})


# ------------------------------------------------------------- redaction --
def _secret_env_values() -> list[str]:
    """Env values that must never appear in optimizer output.

    Mirrors the repo's two existing secret-name conventions: the settings
    store's ``TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL`` regex and
    ``safe_subprocess``'s control-plane deny lists.  Values shorter than 4
    chars are ignored so trivial values (``"0"``, ``"on"``) do not shred
    ordinary output text.  Longest first so overlapping values redact the
    larger token.
    """

    values: list[str] = []
    for name, value in os.environ.items():
        if not value or len(value) < 4:
            continue
        if (
            _SECRET_ENV_NAME_RE.search(name)
            or name in DENY_ENV_NAMES
            or name.startswith(DENY_ENV_PREFIXES)
        ):
            values.append(value)
    values.sort(key=len, reverse=True)
    return values


def scrub_output(obj: Any) -> Any:
    """JSON-round-trip ``obj`` with every secret-shaped env value redacted.

    Idempotent and total: anything JSON-serializable goes in, the same
    structure comes out with sensitive substrings replaced by
    ``"[redacted]"``.  Non-serializable leaves degrade via ``default=str``
    and are then redacted as text.
    """

    text = json.dumps(obj, ensure_ascii=False, default=str)
    for value in _secret_env_values():
        if value in text:
            text = text.replace(value, "[redacted]")
    return json.loads(text)


# ------------------------------------------------------------ raw readers --
def _read_queue_entries(runs_root: str | Path) -> tuple[list[QueueEntry], bool]:
    """Load queue entries read-only (no mkdir, no write, no validation side effects).

    ``queue.QueueStore.__init__`` mkdirs its queue dir; this reader exists so
    the optimizer stays strictly read-only on a runs root that has no queue
    yet.  Entries with an unknown status are skipped exactly like the store's
    own :meth:`QueueStore._read_path`.
    """

    queue_dir = Path(runs_root).expanduser().resolve() / _QUEUE_DIR_NAME
    entries: list[QueueEntry] = []
    try:
        paths = sorted(queue_dir.iterdir())
    except OSError:
        return entries, False
    for path in paths:
        if not path.is_file() or path.suffix != ".json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            entry = QueueEntry.from_dict(data)
        except (TypeError, ValueError):
            continue
        if entry.status not in _VALID_QUEUE_STATUS:
            continue
        entries.append(entry)
    entries.sort(key=lambda item: (-item.priority, item.created_at))
    return entries, True


def _store_from(settings_db: Any) -> Any | None:
    """Normalize the ``settings_db`` argument to a duck-typed store, or ``None``.

    Accepts a ``SettingsStore``-like object (has ``queue_trios`` and
    ``queue_capacity_rows``), a filesystem path to a settings.db, or ``None``.
    The path form opens strictly read-only SQLite (``mode=ro``) — never the
    ``SettingsStore`` constructor, which chmods and opportunistically creates
    the file.  The queue tables are unencrypted, so a key is never needed for
    the reads this module performs.
    """

    if settings_db is None:
        return None
    if callable(getattr(settings_db, "queue_capacity_rows", None)):
        return settings_db
    if isinstance(settings_db, (str, Path)):
        path = Path(settings_db).expanduser()
        if not path.is_file():
            return None
        return _ReadOnlyQueueSettings(path)
    return None


class _ReadOnlyQueueSettings:
    """``queue_trios``/``queue_capacity_rows`` over a read-only SQLite handle.

    Returns the same dict shapes as ``SettingsStore`` so all downstream reuse
    (including ``trio_router``'s criteria builder, if pointed at it) behaves
    identically.  Missing optional columns degrade to the store's own
    defaults instead of raising.
    """

    def __init__(self, path: Path) -> None:
        import sqlite3

        self._conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row
        self._trio_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(queue_trio)").fetchall()
        }
        self._cap_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(queue_capacity)").fetchall()
        }

    def queue_trios(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM queue_trio ORDER BY name").fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            out.append(
                {
                    "name": record.get("name"),
                    "agent": record.get("agent"),
                    "model": record.get("model"),
                    "mcp_profile": record.get("mcp_profile"),
                    "enabled": bool(record.get("enabled", 1)),
                    "updated_by": record.get("updated_by"),
                    "updated_at": record.get("updated_at"),
                    "role_models_set": record.get("role_models") is not None
                    if "role_models" in self._trio_cols
                    else False,
                }
            )
        return out

    def queue_capacity_rows(self) -> dict[str, dict[str, Any]]:
        if "max_peak" in self._cap_cols:
            sel = "SELECT trio, max_concurrent, max_peak FROM queue_capacity ORDER BY trio"
        else:
            sel = "SELECT trio, max_concurrent, NULL AS max_peak FROM queue_capacity ORDER BY trio"
        rows = self._conn.execute(sel).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            record = dict(row)
            peak = record.get("max_peak")
            out[str(record["trio"])] = {
                "max": int(record["max_concurrent"]),
                "max_peak": int(peak) if peak is not None else None,
            }
        return out

    def metadata(self, name: str) -> dict[str, Any]:
        raise KeyError(name)


def _sanitize_usage_rows(usage_rows: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Keep only the known meter fields; coerce ``used_pct``; drop everything else."""

    out: list[dict[str, Any]] = []
    if not usage_rows:
        return out
    for row in usage_rows:
        if not isinstance(row, Mapping):
            continue
        clean: dict[str, Any] = {}
        for field in USAGE_ROW_FIELDS:
            value = row.get(field)
            if value is None:
                continue
            if field == "used_pct":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
            else:
                value = str(value)[:_MAX_REASON_CHARS]
            clean[field] = value
        lane = clean.get("trio") or clean.get("provider")
        if not lane or "used_pct" not in clean:
            continue
        out.append(clean)
    return out


# Lane-blocking skip-reason formats emitted by the launcher (launcher.py
# ``_check_eligibility``).  Matched as prefixes so the trailing detail (run
# id, git probe explanation) does not matter.
_CAPACITY_SKIP_SUFFIX = " at capacity"
_WORKSPACE_DIRTY_MARKERS = (
    "occupied: dirty tree",
    "occupied: checked-out branch",
)
_WORKSPACE_BUSY_MARKERS = (
    "has active run",
    "is reserved for a launch",
)


def _last_skip_reason(entry: QueueEntry) -> str | None:
    reasons = getattr(entry, "skip_reasons", None) or []
    if not reasons:
        return None
    return str(reasons[-1])[:_MAX_REASON_CHARS]


# ---------------------------------------------------------------- snapshot --
def build_snapshot(
    runs_root: str | Path,
    settings_db: Any,
    usage_rows: Iterable[Mapping[str, Any]] | None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Assemble the lane/workspace/usage view tev1 reasons over.

    Pure read: queue entry JSONs under ``<runs_root>/queue`` are parsed with
    ``QueueEntry.from_dict``; caps and trios come from
    ``SettingsStore.queue_capacity_rows()``/``queue_trios()`` (or the
    read-only SQLite twin when only a path is given); ``usage_rows`` is the
    caller-injected provider meter rows, sanitized to the known fields.

    Returns::

        {"trios": {name: {"cap", "cap_peak", "cap_effective", "active",
                          "pending", "enabled"}},
         "pending": [{"queue_id", "trio", "workspace", "priority",
                      "last_skip_reason"}],
         "workspaces": {name: {"state": "busy"|"dirty"|"free"}},
         "usage": [sanitized usage rows],
         "usage_available": bool,
         "peak_window": bool,
         "queue_available": bool,
         "settings_available": bool,
         "generated_at": epoch}
    """

    moment = float(time.time()) if now is None else float(now)
    entries, queue_available = _read_queue_entries(runs_root)
    store = _store_from(settings_db)
    settings_available = store is not None
    trios_rows: list[dict[str, Any]] = []
    cap_rows: dict[str, dict[str, Any]] = {}
    if store is not None:
        try:
            trios_rows = store.queue_trios()
        except Exception as exc:  # a broken store must never take the read down
            logger.warning("lane optimizer: queue_trios() failed: %s", type(exc).__name__)
            settings_available = False
        try:
            cap_rows = store.queue_capacity_rows()
        except Exception as exc:
            logger.warning("lane optimizer: queue_capacity_rows() failed: %s", type(exc).__name__)
            settings_available = False

    peak_window = _Launcher._in_capacity_peak_window(moment)

    enabled: dict[str, bool] = {}
    for row in trios_rows:
        name = str(row.get("name") or "").strip()
        if name:
            enabled[name] = bool(row.get("enabled", True))

    names = set(cap_rows) | {entry.trio for entry in entries if entry.trio} | set(enabled)
    trios: dict[str, dict[str, Any]] = {}
    for name in sorted(names):
        cap_row = cap_rows.get(name) or {}
        base = int(cap_row.get("max", 0) or 0)
        peak = cap_row.get("max_peak")
        effective = _Launcher._effective_cap(base, peak, moment)
        pending = sum(1 for entry in entries if entry.status == "pending" and entry.trio == name)
        active = sum(1 for entry in entries if entry.status == "launched" and entry.trio == name)
        trios[name] = {
            "cap": base,
            "cap_peak": int(peak) if peak is not None else None,
            "cap_effective": int(effective),
            "active": active,
            "pending": pending,
            # A trio named only by queue entries (no settings row) follows the
            # launcher's own rule: an unconfigured trio is "unknown" and
            # unschedulable, so it can never be a move target.
            "enabled": enabled.get(name, name not in enabled and name in cap_rows),
        }

    pending_entries: list[dict[str, Any]] = []
    workspaces: dict[str, dict[str, str]] = {}
    for entry in entries:
        if entry.status == "launched" and entry.workspace:
            ws = workspaces.setdefault(entry.workspace, {"state": "free"})
            ws["state"] = "busy"
    for entry in entries:
        if entry.status != "pending":
            continue
        reason = _last_skip_reason(entry)
        pending_entries.append(
            {
                "queue_id": entry.queue_id,
                "trio": entry.trio,
                "workspace": entry.workspace,
                "priority": entry.priority,
                "last_skip_reason": reason,
            }
        )
        if entry.workspace:
            ws = workspaces.setdefault(entry.workspace, {"state": "free"})
            if ws["state"] == "free" and reason:
                if any(marker in reason for marker in _WORKSPACE_BUSY_MARKERS):
                    ws["state"] = "busy"
                elif any(marker in reason for marker in _WORKSPACE_DIRTY_MARKERS):
                    ws["state"] = "dirty"

    usage = _sanitize_usage_rows(usage_rows)
    snapshot = {
        "trios": trios,
        "pending": pending_entries,
        "workspaces": workspaces,
        "usage": usage,
        "usage_available": bool(usage_rows),
        "peak_window": peak_window,
        "queue_available": queue_available,
        "settings_available": settings_available,
        "generated_at": moment,
    }
    return scrub_output(snapshot)


# --------------------------------------------------------------- usage map --
def _usage_by_lane(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``lane -> usage row``; caller may key a row by ``trio`` or ``provider``."""

    out: dict[str, dict[str, Any]] = {}
    for row in snapshot.get("usage") or ():
        lane = row.get("trio") or row.get("provider")
        if isinstance(lane, str) and lane.strip() and lane.strip() not in out:
            out[lane.strip()] = row
    return out


# --------------------------------------------------------------- the rules --
def rules_suggest(snapshot: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Deterministic plan from the snapshot; also the tev1 fallback.

    Rules (all additive, all suggest-only):

    * a pending entry whose lane is FULL (active >= effective cap, the same
      inequality the launcher uses) plus a lane that is enabled, has room
      now, and carries usage evidence under 50% used -> a move suggestion;
    * an enabled lane pinned at cap 0 that carries <50% usage evidence while
      backlog sits on a full lane -> a cap raise 0 -> 1 (never a decrease);
    * a pending entry blocked by a dirty tree or a no-upstream branch ->
      ``add_clone`` for that workspace, never stash/reset/discard;
    * a lane at >=85% used -> an alert and an exclusion: no moves onto it.

    Lanes with NO usage row are unknown — never move targets; the caller sees
    one honest ``usage_data_unavailable`` alert per such candidate lane.
    """

    moves: list[dict[str, Any]] = []
    cap_changes: list[dict[str, Any]] = []
    workspace_actions: list[dict[str, Any]] = []
    alerts: list[str] = []

    trios: Mapping[str, Mapping[str, Any]] = snapshot.get("trios") or {}
    usage_by_lane = _usage_by_lane(snapshot)

    if not snapshot.get("usage_available"):
        alerts.append(
            "provider usage unavailable: rules only move onto lanes with injected "
            "usage evidence under 50% used; cap raises and clone suggestions still apply"
        )

    hot_lanes: set[str] = set()
    for name in sorted(trios):
        row = usage_by_lane.get(name)
        if row is None:
            continue
        used = float(row.get("used_pct"))
        if used >= HOT_LANE_MIN_USED_PCT:
            hot_lanes.add(name)
            provider = row.get("provider") or name
            alerts.append(
                f"provider {provider} lane {name} at {used:.1f}% used (>= {HOT_LANE_MIN_USED_PCT:.0f}%): "
                "no moves onto this lane"
            )

    def lane_is_full(name: str) -> bool:
        lane = trios.get(name)
        if not lane:
            return True
        return int(lane.get("active", 0)) >= int(lane.get("cap_effective", 0))

    def lane_room(name: str) -> int:
        lane = trios[name]
        return int(lane.get("cap_effective", 0)) - int(lane.get("active", 0))

    def lane_is_idle_target(name: str) -> bool:
        lane = trios.get(name)
        if not lane or not lane.get("enabled") or name in hot_lanes:
            return False
        if lane_room(name) <= 0:
            return False
        row = usage_by_lane.get(name)
        if row is None:
            return False  # unknown usage -> not an "idle paid lane"
        return float(row.get("used_pct")) < IDLE_PAID_LANE_MAX_USED_PCT

    full_with_backlog = sorted(
        name for name, lane in trios.items() if int(lane.get("pending", 0)) > 0 and lane_is_full(name)
    )

    # Cap raises: enabled cold lanes pinned at cap 0 while backlog stalls.
    for name in sorted(trios):
        lane = trios[name]
        if not full_with_backlog:
            break
        if not lane.get("enabled") or name in hot_lanes:
            continue
        if int(lane.get("cap_effective", 0)) != 0:
            continue
        row = usage_by_lane.get(name)
        if row is None or float(row.get("used_pct")) >= IDLE_PAID_LANE_MAX_USED_PCT:
            continue
        blocked = ", ".join(full_with_backlog)
        cap_changes.append(
            {
                "trio": name,
                "from": 0,
                "to": CAP_RAISE_TO,
                "reason": (
                    f"{name} is an idle paid lane at {float(row.get('used_pct')):.1f}% used pinned at "
                    f"cap 0 while {blocked} holds pending entries; raise one slot for the next tick"
                )[:_MAX_REASON_CHARS],
            }
        )

    # Moves: backlog on a full lane -> room on a cold lane, cheapest first.
    room_reserved: dict[str, int] = {name: lane_room(name) for name in trios}
    for entry in snapshot.get("pending") or ():
        trio = str(entry.get("trio") or "")
        reason = entry.get("last_skip_reason")
        capacity_skipped = bool(
            reason and trio and str(reason).startswith(trio) and str(reason).endswith(_CAPACITY_SKIP_SUFFIX)
        )
        if not trio or not lane_is_full(trio) and not capacity_skipped:
            continue
        targets = sorted(
            (
                (float(usage_by_lane[name].get("used_pct")), name)
                for name in trios
                if name != trio and room_reserved.get(name, 0) > 0 and lane_is_idle_target(name)
            ),
        )
        if not targets:
            unknown = sorted(
                name
                for name in trios
                if name != trio
                and trios[name].get("enabled")
                and name not in hot_lanes
                and room_reserved.get(name, 0) > 0
                and name not in usage_by_lane
            )
            if unknown:
                alerts.append(
                    f"pending entry {entry.get('queue_id')} on full lane {trio}: no usage data for "
                    f"candidate lane(s) {', '.join(unknown)}; refusing to guess"
                )
            continue
        used, target = targets[0]
        room_reserved[target] -= 1
        workspace = str(entry.get("workspace") or "")
        full = trios[trio]
        moves.append(
            {
                "queue_id": str(entry.get("queue_id") or ""),
                "to_trio": target,
                "to_workspace": workspace,
                "reason": (
                    f"{trio} is full ({full.get('active')}/{full.get('cap_effective')} active/cap) with "
                    f"{full.get('pending')} pending; {target} is an idle paid lane at {used:.1f}% used"
                )[:_MAX_REASON_CHARS],
            }
        )

    # Workspace clones: a dirty tree blocks, it never gets discarded.
    for entry in snapshot.get("pending") or ():
        workspace = str(entry.get("workspace") or "")
        if not workspace:
            continue
        state = (snapshot.get("workspaces") or {}).get(workspace, {})
        if state.get("state") != "dirty":
            continue
        reason = str(entry.get("last_skip_reason") or "")
        if not any(marker in reason for marker in _WORKSPACE_DIRTY_MARKERS):
            continue
        workspace_actions.append(
            {
                "workspace": workspace,
                "action": WORKSPACE_ACTION_ADD_CLONE,
                "reason": (
                    f"pending entry {entry.get('queue_id')} is blocked: {reason}; clone the workspace "
                    "beside the dirty tree — never stash, reset, or discard uncommitted work"
                )[:_MAX_REASON_CHARS],
            }
        )

    return {
        "moves": moves,
        "cap_changes": cap_changes,
        "workspace_actions": workspace_actions,
        "alerts": alerts,
    }


# --------------------------------------------------------- the tev1 consult --
def _optimizer_payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """``/v1/systemone`` body for the lane plan: same envelope as PR #162's router."""

    text = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
    text = text[: _trio_router.TRIO_ROUTER_MAX_TASK_CHARS]
    return {
        "model": _trio_router.DEFAULT_TRIO_ROUTER_MODEL,
        "state": {"snapshot": text},
        "questions": {
            "plan": {
                "type": "json",
                "instructions": (
                    "Suggest a lane/workspace rearrangement for the next overseer tick. "
                    "Answer with one JSON object: "
                    '{"moves": [{"queue_id", "to_trio", "to_workspace", "reason"}], '
                    '"cap_changes": [{"trio", "from", "to", "reason"}], '
                    '"workspace_actions": [{"workspace", "action": "add_clone", "reason"}], '
                    '"alerts": [...]}. Suggest-only: never discard work; the only allowed '
                    "workspace action is add_clone; never move onto a lane at >=85% used."
                ),
            },
        },
    }


def _clean_reason(value: Any, fallback: str) -> str:
    text = str(value if value is not None else fallback).strip()
    text = " ".join(text.split())
    return (text or fallback)[:_MAX_REASON_CHARS]


def _coerce_model_plan(
    response: Any, snapshot: Mapping[str, Any]
) -> tuple[dict[str, list[dict[str, Any]]] | None, list[str]]:
    """Strictly validate tev1's answer; drop (never silently keep) unsafe items.

    Returns ``(plan, rejection_alerts)``.  ``plan`` is ``None`` when the
    answer could not be read at all — the caller then falls back to the
    deterministic rules.
    """

    rejects: list[str] = []
    if not isinstance(response, dict):
        return None, rejects
    answers = response.get("answers")
    if not isinstance(answers, dict):
        return None, rejects
    raw = answers.get("plan")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None, rejects
    if isinstance(raw, dict) and isinstance(raw.get("json"), (dict, str)):
        # EntelliJent-style typed answers wrap structured output in {"json": ...};
        # accept it only when it is itself the plan object.
        inner = raw["json"]
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except json.JSONDecodeError:
                return None, rejects
        raw = inner
    if not isinstance(raw, dict):
        return None, rejects

    trios: Mapping[str, Mapping[str, Any]] = snapshot.get("trios") or {}
    usage_by_lane = _usage_by_lane(snapshot)
    queue_ids = {str(item.get("queue_id")) for item in snapshot.get("pending") or ()}

    moves: list[dict[str, Any]] = []
    for item in raw.get("moves") if isinstance(raw.get("moves"), list) else ():
        if not isinstance(item, dict):
            continue
        queue_id = str(item.get("queue_id") or "").strip()
        to_trio = str(item.get("to_trio") or "").strip()
        if not queue_id or queue_id not in queue_ids:
            rejects.append("dropped model move: unknown queue_id")
            continue
        lane = trios.get(to_trio)
        if lane is None or not lane.get("enabled"):
            rejects.append(f"dropped model move onto unconfigured lane {to_trio}")
            continue
        usage = usage_by_lane.get(to_trio)
        if usage is not None and float(usage.get("used_pct")) >= HOT_LANE_MIN_USED_PCT:
            rejects.append(f"dropped model move onto hot lane {to_trio} (>=85% used)")
            continue
        if int(lane.get("active", 0)) >= int(lane.get("cap_effective", 0)):
            rejects.append(f"dropped model move onto full lane {to_trio}")
            continue
        moves.append(
            {
                "queue_id": queue_id,
                "to_trio": to_trio,
                "to_workspace": str(item.get("to_workspace") or "").strip(),
                "reason": _clean_reason(item.get("reason"), "tev1 lane rearrangement"),
            }
        )

    cap_changes: list[dict[str, Any]] = []
    for item in raw.get("cap_changes") if isinstance(raw.get("cap_changes"), list) else ():
        if not isinstance(item, dict):
            continue
        trio = str(item.get("trio") or "").strip()
        try:
            from_cap = int(item.get("from"))
            to_cap = int(item.get("to"))
        except (TypeError, ValueError):
            rejects.append("dropped model cap change: non-integer caps")
            continue
        if trio not in trios:
            rejects.append(f"dropped model cap change on unconfigured lane {trio}")
            continue
        if not (0 <= from_cap < to_cap):
            # Raise-only: the optimizer never suggests shrinking a lane.
            rejects.append(f"dropped model cap change on {trio}: only raises are allowed")
            continue
        cap_changes.append(
            {
                "trio": trio,
                "from": from_cap,
                "to": to_cap,
                "reason": _clean_reason(item.get("reason"), "tev1 cap raise"),
            }
        )

    workspace_actions: list[dict[str, Any]] = []
    for item in raw.get("workspace_actions") if isinstance(raw.get("workspace_actions"), list) else ():
        if not isinstance(item, dict):
            continue
        workspace = str(item.get("workspace") or "").strip()
        action = str(item.get("action") or "").strip()
        if action != WORKSPACE_ACTION_ADD_CLONE:
            # Hard refusal: the only safe workspace suggestion is a clone.
            rejects.append(
                f"dropped model workspace action {action or '<empty>'} on {workspace or '<unknown>'}: "
                "only add_clone is allowed — never stash, reset, or discard"
            )
            continue
        if not workspace:
            continue
        workspace_actions.append(
            {
                "workspace": workspace,
                "action": WORKSPACE_ACTION_ADD_CLONE,
                "reason": _clean_reason(item.get("reason"), "tev1 workspace clone"),
            }
        )

    alerts: list[str] = []
    for item in raw.get("alerts") if isinstance(raw.get("alerts"), list) else ():
        text = _clean_reason(item, "")
        if text:
            alerts.append(text)

    plan = {
        "moves": moves,
        "cap_changes": cap_changes,
        "workspace_actions": workspace_actions,
        "alerts": alerts,
    }
    return plan, rejects


def suggest(
    snapshot: Mapping[str, Any],
    model: str = "tev1",
    *,
    store: Any = None,
    http_call: Callable[..., Any] | None = None,
    timeout: float = _trio_router.TRIO_ROUTER_TIMEOUT_SECONDS,
    now: float | None = None,
) -> dict[str, Any]:
    """Return the tick's lane-optimizer suggestion as a JSON-safe dict.

    Contract shape (extra metadata keys ride alongside)::

        {"moves": [{queue_id, to_trio, to_workspace, reason}],
         "cap_changes": [{trio, from, to, reason}],
         "workspace_actions": [{workspace, action: "add_clone", reason}],
         "alerts": [...],
         "source": "tev1" | "rules",
         "model": str, "generated_at": float, "usage_available": bool}

    tev1 is consulted through PR #162's ``trio_router`` client when the
    router is enabled; any transport error, invalid answer, or refusal drops
    to the deterministic rules with an alert naming the fallback.  Model
    answers are re-validated against the same safety filters as the rules (no
    moves onto hot/full lanes, raises-only cap changes, add_clone-only
    workspace actions).  Output is scrubbed of every secret-shaped env value.
    """

    moment = float(time.time()) if now is None else float(now)
    settings = _trio_router._resolve_settings(store)
    rules_plan = rules_suggest(snapshot)

    envelope: dict[str, Any]
    source = "rules"
    extra_alerts: list[str] = []
    if not settings["enabled"]:
        extra_alerts.append("tev1 router disabled (TRIO_ROUTER_ENABLED unset): deterministic rules used")
    else:
        payload = _optimizer_payload(snapshot)
        caller = http_call if http_call is not None else _trio_router._call_systemone
        try:
            response = caller(settings["url"], settings["model"], payload, timeout=float(timeout))
        except Exception as exc:
            logger.warning("lane optimizer: tev1 consult failed; rules fallback: %s", exc)
            extra_alerts.append(
                f"tev1 unreachable ({type(exc).__name__}): deterministic rules fallback used"
            )
        else:
            plan, rejects = _coerce_model_plan(response, snapshot)
            if plan is None:
                extra_alerts.append("tev1 returned an unreadable plan: deterministic rules fallback used")
            else:
                source = "tev1"
                plan = dict(plan)
                plan["alerts"] = list(plan["alerts"]) + rejects
                rules_alerts = list(rules_plan["alerts"])
                keep_reject_alerts = [
                    alert
                    for alert in rules_alerts
                    if alert.startswith("provider ") or alert.startswith("provider usage unavailable")
                ]
                plan["alerts"] = keep_reject_alerts + plan["alerts"]
                rules_plan = plan

    envelope = {
        "moves": rules_plan["moves"],
        "cap_changes": rules_plan["cap_changes"],
        "workspace_actions": rules_plan["workspace_actions"],
        "alerts": list(rules_plan["alerts"]) + extra_alerts,
        "source": source,
        "model": settings["model"] if settings["enabled"] else model,
        "generated_at": moment,
        "usage_available": bool(snapshot.get("usage_available")),
    }
    return scrub_output(envelope)


__all__ = [
    "CAP_RAISE_TO",
    "HOT_LANE_MIN_USED_PCT",
    "IDLE_PAID_LANE_MAX_USED_PCT",
    "USAGE_ROW_FIELDS",
    "WORKSPACE_ACTION_ADD_CLONE",
    "build_snapshot",
    "rules_suggest",
    "scrub_output",
    "suggest",
]
