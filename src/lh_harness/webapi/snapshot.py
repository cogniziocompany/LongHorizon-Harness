"""Project existing DashboardState data into the stable Web snapshot."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..dashboard.state import DashboardState
from ..supervisor.lifecycle import canonical_lifecycle_status
from .events import EventTailer


_PROVENANCE_FIELDS = (
    "agent",
    "model",
    "role_configs",
    "workspace",
    "max_rounds",
    "prompt_language",
    "mcp_profile",
    "mcp_profile_resolution",
)
_MAX_FINAL_RESPONSE_CHARS = 512 * 1024
# Mirrors agent_registry's rule; owner records are untrusted input here.
_REASONING_EFFORT_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def _final_response(
    raw: dict[str, Any],
    report: dict[str, Any],
    rounds: list[Any],
) -> str:
    """Select the durable user-facing reply without falling back to audit text."""

    candidates: list[Any] = [raw.get("final_response"), report.get("final_response")]
    candidates.extend(
        item.get("final_response")
        for item in reversed(rounds)
        if isinstance(item, dict)
    )
    for value in candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        return value[:_MAX_FINAL_RESPONSE_CHARS]
    return ""


def _provenance(*sources: dict[str, Any] | None) -> dict[str, Any]:
    """Select bounded, durable run provenance from newest-authority sources.

    Supervisor ``owner.json`` is the primary source for managed runs; report
    and start-event data are useful fallbacks for historical/legacy runs.  The
    projection deliberately omits malformed or overlong values so an
    agent-written metadata file cannot inflate a run summary response.
    """

    result: dict[str, Any] = {}
    limits = {"agent": 64, "model": 256, "workspace": 4096, "mcp_profile": 256}
    for field in _PROVENANCE_FIELDS:
        for source in sources:
            if not isinstance(source, dict) or field not in source:
                continue
            value = source.get(field)
            if field == "role_configs":
                cleaned = _safe_role_configs(value)
                if cleaned:
                    result[field] = cleaned
                    break
                continue
            if field == "mcp_profile_resolution":
                cleaned = _safe_mcp_profile_resolution(value)
                if cleaned:
                    result[field] = cleaned
                    break
                continue
            if field == "max_rounds":
                if isinstance(value, bool):
                    continue
                if isinstance(value, int) and 1 <= value <= 1_000_000:
                    result[field] = value
                    break
                # JSON written by older integrations occasionally encoded an
                # integer as a decimal string. Accept only canonical digits.
                if isinstance(value, str) and value.isdecimal():
                    parsed = int(value)
                    if 1 <= parsed <= 1_000_000:
                        result[field] = parsed
                        break
                continue
            if field == "prompt_language":
                if value in {"en", "zh"}:
                    result[field] = value
                    break
                continue
            if value is None and field == "model":
                # ``None`` is meaningful: the run followed the agent/provider
                # default rather than selecting a custom model.
                result[field] = None
                break
            if not isinstance(value, str):
                continue
            text = value.strip()
            if not text or len(text) > limits[field] or "\x00" in text:
                continue
            result[field] = text
            break
    return result


def _safe_role_configs(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for role in ("manager", "executor", "auditor"):
        raw = value.get(role)
        if not isinstance(raw, dict):
            continue
        agent = raw.get("agent")
        model = raw.get("model")
        if agent not in {"codex", "claude_code", "deepseek_harness", "opencode"}:
            continue
        if not isinstance(model, str) or not model.strip() or len(model.strip()) > 256 or "\x00" in model:
            continue
        result[role] = {"agent": agent, "model": model.strip()}
        effort = raw.get("reasoning_effort")
        if isinstance(effort, str) and _REASONING_EFFORT_RE.match(effort.strip()):
            result[role]["reasoning_effort"] = effort.strip()
    return result if len(result) == 3 else {}


_VALID_MCP_RESOLUTION_ROLES = frozenset(
    {
        "manager",
        "executor",
        "gui_executor",
        "cli_executor",
        "auditor",
        "gui_auditor",
        "cli_auditor",
        "final_response",
    }
)


def _safe_mcp_profile_resolution(value: object) -> dict[str, dict[str, Any]]:
    """Sanitize the per-role resolved MCP profile provenance record."""

    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for role, raw in value.items():
        if role not in _VALID_MCP_RESOLUTION_ROLES:
            continue
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        reason = raw.get("reason")
        if not isinstance(reason, str):
            reason = ""
        source = raw.get("source")
        if not isinstance(source, str):
            source = ""
        read_only = raw.get("read_only")
        if not isinstance(read_only, bool):
            read_only = None
        result[role] = {
            "name": name.strip(),
            "reason": reason.strip(),
            "source": source.strip(),
            "read_only": read_only,
        }
    return result


def _safe_preflight(value: object) -> dict[str, Any]:
    """Sanitize the workspace-preflight receipt carried by the start event.

    The preflight receipt is written by the manager and reaches the snapshot
    through the ``role_harness_start`` event.  It carries the five fields the
    overseer needs -- candidate SHA, env manifest, interpreter, collected
    count, and tool availability -- and every one of them is bounded before
    it is projected: the record is produced from a workspace the agent may
    influence, so an unbounded string here would let a run inflate its own
    summary response.
    """

    if not isinstance(value, dict):
        return {}

    def _text(raw: object, limit: int) -> str:
        if not isinstance(raw, str):
            return ""
        text = raw.strip()
        if "\x00" in text:
            return ""
        return text[:limit]

    candidate_sha = _text(value.get("candidate_sha"), 64)

    manifest_raw = value.get("env_manifest")
    manifest: dict[str, Any] = {}
    if isinstance(manifest_raw, dict):
        manifest["ecosystem"] = _text(manifest_raw.get("ecosystem"), 32)
        manifest["manifest_path"] = _text(manifest_raw.get("manifest_path"), 4096)
        requirements = manifest_raw.get("declared_test_requirements")
        if isinstance(requirements, list):
            manifest["declared_test_requirements"] = [
                _text(item, 128) for item in requirements[:64] if isinstance(item, str)
            ]
        lock_paths = manifest_raw.get("lock_paths")
        if isinstance(lock_paths, list):
            manifest["lock_paths"] = [
                _text(item, 4096) for item in lock_paths[:32] if isinstance(item, str)
            ]
        manifest["declared_no_tests"] = bool(manifest_raw.get("declared_no_tests"))

    interpreter_raw = value.get("interpreter")
    interpreter: dict[str, Any] = {}
    if isinstance(interpreter_raw, dict):
        interpreter["executable"] = _text(interpreter_raw.get("executable"), 4096)
        interpreter["version"] = _text(interpreter_raw.get("version"), 128)
        interpreter["source"] = _text(interpreter_raw.get("source"), 64)
        interpreter["shared_harness_venv"] = bool(
            interpreter_raw.get("shared_harness_venv")
        )

    collection_raw = value.get("collection")
    collected = 0
    if isinstance(collection_raw, dict):
        raw_count = collection_raw.get("collected")
        if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0:
            collected = raw_count

    tools_raw = value.get("tool_availability")
    tools: dict[str, Any] = {}
    if isinstance(tools_raw, dict):
        for name, entry in list(tools_raw.items())[:16]:
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            tools[_text(name, 64)] = {
                "available": bool(entry.get("available")),
                "usable": bool(entry.get("usable")),
                "path": _text(entry.get("path"), 4096),
                "alternate_route": _text(entry.get("alternate_route"), 2048),
            }

    # The provenance fields answer "where did these packages actually come
    # from?".  They are the evidence that the environment was workspace
    # isolated rather than resolving out of the shared harness venv.
    provenance_raw = value.get("provenance")
    provenance: dict[str, Any] = {}
    if isinstance(provenance_raw, dict):
        provenance["sys_executable"] = _text(provenance_raw.get("sys_executable"), 4096)
        provenance["repo_package_path"] = _text(
            provenance_raw.get("repo_package_path"), 4096
        )
        provenance["pytest_path"] = _text(provenance_raw.get("pytest_path"), 4096)
        sys_path = provenance_raw.get("sys_path")
        if isinstance(sys_path, list):
            provenance["sys_path"] = [
                _text(item, 4096) for item in sys_path[:64] if isinstance(item, str)
            ]
        else:
            provenance["sys_path"] = []
        provenance["pythonpath"] = _text(provenance_raw.get("pythonpath"), 4096)
        provenance["virtual_env"] = _text(provenance_raw.get("virtual_env"), 4096)
        provenance["pythonhome"] = _text(provenance_raw.get("pythonhome"), 4096)

    result: dict[str, Any] = {
        "candidate_sha": candidate_sha,
        "env_manifest": manifest,
        "interpreter": interpreter,
        "collected": collected,
        "tool_availability": tools,
        "provenance": provenance,
    }
    prerequisite = value.get("prerequisite")
    if isinstance(prerequisite, dict):
        result["prerequisite"] = {
            "code": _text(prerequisite.get("code"), 64),
            "message": _text(prerequisite.get("message"), 2048),
            "owner": _text(prerequisite.get("owner"), 128),
        }
    result["ok"] = bool(value.get("ok", True))
    return result


def _separate_claims(round_item: dict[str, Any]) -> dict[str, Any]:
    """Return a round projection that separates executor claims from auditor findings.

    The raw dashboard round blends executor output (the executor's claim about
    what it did) with the auditor report (the auditor's independent findings).
    The Web snapshot exposes these as distinct namespaces so clients can tell
    who asserted what, and adds a derived verdict when the auditor produced a
    finding.
    """

    if not isinstance(round_item, dict):
        return round_item
    result = dict(round_item)
    executor_output = result.get("executor_output") or ""
    auditor_report = result.get("auditor_report") or ""
    result["executor_claim"] = executor_output
    result["auditor_findings"] = auditor_report
    result["verdict"] = _derive_verdict(executor_output, auditor_report)
    return result


def _derive_verdict(executor_output: str, auditor_report: str) -> dict[str, Any]:
    """Derive a round-level verdict from executor claim and auditor findings.

    The auditor report is treated as structured text: a leading
    ``Status: <value>`` line is the authoritative signal.  Substring matching
    against freeform text is forbidden because it converts ``incomplete``,
    ``blocked`` and other status words into false acceptances.
    """

    report = (auditor_report or "").strip()
    claim = (executor_output or "").strip()
    if not report and not claim:
        return {"state": "pending", "summary": "no claim or findings recorded"}

    # Parse an explicit ``Status:`` line at the start of the report.
    status_value: str | None = None
    first_line = report.splitlines()[0] if report else ""
    if ":" in first_line:
        key, value = first_line.split(":", 1)
        if key.strip().lower() == "status":
            status_value = value.strip().lower()

    if status_value == "complete":
        return {"state": "accepted", "summary": "auditor accepted executor claim", "authority": "auditor"}
    if status_value in {"incomplete", "needs_revision", "blocked", "failed", "rejected"}:
        return {"state": "rejected", "summary": f"auditor report status is {status_value}", "authority": "auditor"}

    # No structured status line, but auditor text exists: keep the finding
    # separate from a pure executor claim rather than guessing.
    if report:
        return {"state": "pending", "summary": "auditor findings recorded without structured status", "authority": "auditor"}
    if claim:
        return {"state": "claimed", "summary": "executor produced a claim; no auditor findings yet", "authority": "executor"}
    return {"state": "pending", "summary": "no claim or findings recorded"}


def _status(raw: dict[str, Any], events: list[dict[str, Any]], approvals: list[dict[str, Any]]) -> str:
    report_status = raw.get("report", {}).get("status") if isinstance(raw.get("report"), dict) else None
    if report_status:
        return canonical_lifecycle_status(report_status)
    if any(item.get("status") == "pending" for item in approvals):
        return "waiting_approval"
    names = {str(item.get("event")) for item in events}
    if "role_harness_failed" in names:
        return "failed"
    if "role_harness_cancelled" in names:
        return "cancelled"
    if "role_harness_done" in names:
        return "completed"
    if events:
        return "running"
    return "idle"


def build_run_summary(item: dict[str, Any], *, state: DashboardState | None = None) -> dict[str, Any]:
    run_id = str(item.get("id") or "")
    status = str(item.get("status") or "")
    task = str(item.get("task") or "")
    provenance_sources: list[dict[str, Any] | None] = [item]
    if state is not None and (not status or not task):
        raw = state.snapshot()
        if not status:
            status = _status(raw, state.read_events(limit=5000), state.list_approvals())
        if not task:
            report = raw.get("report") if isinstance(raw.get("report"), dict) else {}
            task = str(raw.get("task") or report.get("task") or "")
    if state is not None and getattr(state, "runs_root", None) is not None:
        try:
            owner = state.control_bus.read_owner()
        except (OSError, ValueError, RuntimeError):
            owner = {}
        if isinstance(owner, dict):
            provenance_sources.insert(0, owner)
        try:
            report = state.read_report()
        except (OSError, ValueError, RuntimeError):
            report = {}
        provenance_sources.append(report if isinstance(report, dict) else None)
    summary = {
        "id": run_id,
        "task": task,
        "status": status or "unknown",
        "updated_at": item.get("mtime", 0.0),
        "log_dir": item.get("log_dir", ""),
    }
    # LHH-GATE-OWNER-NOTE: when the run is ``waiting_approval``, surface the
    # FIRST pending approval's owner + owner_note so the runs list can render
    # a "Decision: {owner}" badge without having to load the full snapshot.
    # Only set when a pending approval exists; absent values stay None so the
    # UI knows there is no gate to advertise.
    if state is not None and status == "waiting_approval":
        try:
            for approval in state.list_approvals():
                if approval.get("status") == "pending":
                    owner = approval.get("owner")
                    if owner:
                        summary["pending_decision_owner"] = str(owner)
                    owner_note = approval.get("owner_note")
                    if isinstance(owner_note, str) and owner_note.strip():
                        # Bounded so the summary row never carries an
                        # unbounded payload (the full note panel reads from
                        # the snapshot, not from this summary hint).
                        summary["pending_decision_owner_note"] = owner_note[:4000]
                    break
        except (OSError, ValueError, RuntimeError):
            pass
    summary.update(_provenance(*provenance_sources))
    if state is not None:
        try:
            owner = state.control_bus.read_owner()
            if isinstance(owner, dict) and owner.get("youtrack_issue_id"):
                summary["youtrack_issue_id"] = str(owner["youtrack_issue_id"])
        except (OSError, ValueError, RuntimeError):
            pass
    return summary


def build_snapshot(state: DashboardState, *, run_id: str | None = None) -> dict[str, Any]:
    raw = state.snapshot()
    effective_run_id = run_id or state.current_run_id or "local"
    # Read the event log through the same tailer used by REST/WS replay.  This
    # keeps event ids absolute when the dashboard snapshot is limited to the
    # last 200 records and isolates malformed records to a diagnostic warning.
    tailer = EventTailer(
        state.role_dir / "events.jsonl",
        run_id=effective_run_id,
    )
    envelopes = tailer.read(limit=200)
    events = [item.to_dict() for item in envelopes]
    legacy_events = raw.get("events") if isinstance(raw.get("events"), list) else []
    approvals = raw.get("approvals") if isinstance(raw.get("approvals"), list) else []
    operator_messages = raw.get("operator_messages") if isinstance(raw.get("operator_messages"), list) else []
    rounds = raw.get("rounds") if isinstance(raw.get("rounds"), list) else []
    status = canonical_lifecycle_status(_status(raw, legacy_events, approvals))
    report = raw.get("report") if isinstance(raw.get("report"), dict) else {}
    owner: dict[str, Any] = {}
    if getattr(state, "runs_root", None) is not None:
        try:
            candidate_owner = state.control_bus.read_owner()
            if isinstance(candidate_owner, dict):
                owner = candidate_owner
        except (OSError, ValueError, RuntimeError):
            owner = {}
    # A supervised worker owns the task before its first Manager report exists.
    # Project that durable value immediately so Web does not render an active
    # run as "no task selected" while the first round is still in progress.
    task = str(raw.get("task") or report.get("task") or owner.get("task") or "")
    final_response = _final_response(raw, report, rounds)
    start_payload: dict[str, Any] = {}
    for event in reversed(legacy_events):
        if not isinstance(event, dict) or event.get("event") != "role_harness_start":
            continue
        if isinstance(event.get("payload"), dict):
            start_payload = event["payload"]
        else:
            start_payload = event
        # Legacy event records store ``workspace_path`` rather than
        # ``workspace``. Normalize just this safe, bounded field.
        if "workspace" not in start_payload and "workspace_path" in start_payload:
            start_payload = {**start_payload, "workspace": start_payload.get("workspace_path")}
        break
    provenance = _provenance(owner, report, start_payload)
    # The workspace preflight receipt rides on the start event; the run
    # snapshot projects it so the overseer sees the same fields the run-dir
    # receipt file carries.
    # The ledger record is flat, but a legacy/legacy-shaped event may nest the
    # payload; accept both so the preflight is found either way.
    _preflight_raw = start_payload.get("workspace_preflight")
    if _preflight_raw is None:
        nested = start_payload.get("payload")
        if isinstance(nested, dict):
            _preflight_raw = nested.get("workspace_preflight")
    preflight = _safe_preflight(_preflight_raw)
    # The worker/adapter writes the authoritative resolved profile record after
    # it resolves against the gateway key and project config.  Merge it into
    # snapshot provenance when the file exists.
    if getattr(state, "runs_root", None) is not None:
        try:
            resolution_path = Path(state.runs_root) / effective_run_id / "harness" / "mcp_profile_resolution.json"
            if resolution_path.is_file():
                resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
                cleaned = _safe_mcp_profile_resolution(resolution)
                if cleaned:
                    provenance["mcp_profile_resolution"] = cleaned
        except (OSError, ValueError, RuntimeError):
            pass
    # ``active_*`` means work that can still change, never merely the newest
    # durable round.  A final Manager-only round is often appended after the
    # Auditor has already satisfied the task; falling back to that round made
    # clients render stale roles as active/waiting forever.
    active_round = None
    if status not in {"completed", "failed", "cancelled", "blocked", "incomplete"}:
        active_round = next(
            (
                item.get("round_index")
                for item in reversed(rounds)
                if isinstance(item, dict) and item.get("in_progress")
            ),
            None,
        )
    rounds = [_separate_claims(r) for r in rounds]
    active_role = None
    if active_round is not None:
        for item in reversed(rounds):
            if isinstance(item, dict) and item.get("round_index") == active_round and item.get("active_role"):
                active_role = item["active_role"]
                break
    completion_satisfied = report.get("completion_satisfied")
    if not isinstance(completion_satisfied, bool):
        completion_satisfied = None
    completion_authority = report.get("completion_authority")
    if completion_authority is not None:
        completion_authority = str(completion_authority)
    report_status = report.get("status")
    if report_status is not None:
        report_status = canonical_lifecycle_status(str(report_status))
    exit_code = report.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        exit_code = None
    failure_reason = report.get("failure_reason")
    if failure_reason is not None:
        failure_reason = str(failure_reason)
    return {
        "schema_version": 1,
        "run": {
            "id": effective_run_id,
            "status": canonical_lifecycle_status(status),
            "started_at": report.get("started_at"),
            "finished_at": report.get("finished_at"),
            "log_dir": raw.get("log_dir", ""),
            "completion_satisfied": completion_satisfied,
            "completion_authority": completion_authority,
            "report_status": report_status,
            "exit_code": exit_code,
            "failure_reason": failure_reason,
            "final_response": final_response,
            **provenance,
            **({"workspace_preflight": preflight} if preflight else {}),
        },
        "mission": {
            "task": task,
            "contract_path": "task_contract.txt",
            "plan_path": "manager_plan.txt",
            "verified_state_path": "task_state.txt",
            "report_path": "report.json",
        },
        "rounds": rounds,
        "active_round": active_round,
        "active_role": active_role,
        "events": events,
        "approvals": approvals,
        "operator_messages": operator_messages,
        "controls": {
            "can_inject": bool(raw.get("control_enabled")),
            # Approval/instruction control can attach directly to a worker,
            # while lifecycle signals require a RunSupervisor-owned process.
            # Supervised snapshots overlay this value in the API server.
            "can_abort": False,
            "can_resume": False,
        },
        "diagnostics": {
            "last_event_id": events[-1]["event_id"] if events else None,
            "event_count": len(events),
            "warnings": tailer.last_warnings,
            "cursor_gap": False,
            "resync_required": False,
        },
        "legacy": raw,
    }
