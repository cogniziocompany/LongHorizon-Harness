from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..adapters.base import AgentAdapter
from ..auditor_agent import parse_audit_report
from ..environment.local import LocalEnvironment
from ..manager import _visible_output
from ..role_prompts import (
    MANAGER_NEXT_CLI,
    build_role_auditor_prompt,
    build_role_executor_prompt,
    build_role_final_response_prompt,
    build_role_manager_prompt,
    extract_role_manager_plan_text,
    extract_role_task_contract,
    extract_role_task_state,
    parse_role_manager_next_step,
)
from ..supervisor.control_bus import (
    CommandConflict,
    ControlBus,
    RevisionConflict,
    _append_jsonl,
    _atomic_bytes_write,
    _read_json_file,
    _read_jsonl,
)
from ..types import EpisodeBudget, EpisodeResult, ManagedRound
from .auth import canonical_body_digest

INTERACTIVE_STAGES = ("manager.plan", "executor.build", "auditor.verify", "final_response")
_STAGE_ROLES = {
    "manager.plan": "manager",
    "executor.build": "executor",
    "auditor.verify": "auditor",
    "final_response": "final_response",
}
_TERMINAL = {"completed", "incomplete", "failed", "cancelled", "superseded"}


class InteractionError(ValueError):
    def __init__(self, message: str, status_code: int = 409) -> None:
        self.status_code = status_code
        super().__init__(message)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _json_write(path: Path, value: dict[str, Any]) -> None:
    _atomic_bytes_write(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _records(path: Path) -> list[dict[str, Any]]:
    return _read_jsonl(path)


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InteractionError(f"local Git operation failed: {exc}", 422) from exc
    return result.stdout.strip()


def _safe_id(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise InteractionError(f"{field} is invalid", 422)
    return value


class _WorkspaceEnvironment(LocalEnvironment):
    def __init__(self, workspace: Path, tmp_dir: Path) -> None:
        super().__init__(str(tmp_dir))
        self.workspace = workspace

    async def exec(self, command: str, timeout: int = 30, tee_path: str | None = None):
        anchored = f"cd {shlex.quote(str(self.workspace))} && {command}"
        return await super().exec(anchored, timeout=timeout, tee_path=tee_path)


class LocalInteractiveService:
    """A loopback-only, four-stage fixture backed by real Harness adapters."""

    def __init__(
        self,
        root: str | Path,
        *,
        source_root: str | Path,
        adapter_factory: Callable[[str], AgentAdapter] | None = None,
        poll_interval: float = 0.05,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.source_root = Path(source_root).expanduser().resolve()
        self.runs_root = self.root / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.source_root.mkdir(parents=True, exist_ok=True)
        self.adapter_factory = adapter_factory
        self.poll_interval = max(0.01, poll_interval)
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}

    @staticmethod
    def manifest() -> dict[str, Any]:
        return {
            "schema_version": 1,
            "fixture": "interactive-office-local",
            "bmad_required": False,
            "stages": [
                {"id": stage, "role": _STAGE_ROLES[stage], "position": index}
                for index, stage in enumerate(INTERACTIVE_STAGES)
            ],
        }

    def list_runs(self) -> dict[str, Any]:
        runs = []
        for path in self.runs_root.iterdir():
            if path.is_symlink() or not path.is_dir():
                continue
            projection = self.projection(path.name)
            for attempt in projection["attempts"]:
                runs.append({
                    "run_id": path.name,
                    "attempt_id": attempt["attempt_id"],
                    "parent_attempt_id": attempt.get("parent_attempt_id"),
                    "status": attempt["status"],
                    "current_stage": projection["current_stage"] if attempt["attempt_id"] == projection["attempt_id"] else attempt.get("selected_stage"),
                    "revision": attempt["revision"],
                })
        return {"runs": sorted(runs, key=lambda item: (item["run_id"], item["attempt_id"]))}

    def _run_dir(self, run_id: str) -> Path:
        _safe_id(run_id, "run_id")
        path = self.runs_root / run_id
        if not path.is_dir() or path.is_symlink():
            raise InteractionError("run not found", 404)
        return path

    def _run_status(self, run_id: str) -> dict[str, Any]:
        return ControlBus(self._run_dir(run_id)).read_status()

    def _attempt_dir(self, run_id: str, attempt_id: str) -> Path:
        _safe_id(attempt_id, "attempt_id")
        path = self._run_dir(run_id) / "attempts" / attempt_id
        if not path.is_dir() or path.is_symlink():
            raise InteractionError("attempt not found", 404)
        return path

    def _attempt_status(self, run_id: str, attempt_id: str) -> dict[str, Any]:
        return ControlBus(self._attempt_dir(run_id, attempt_id)).read_status()

    def _update_attempt(self, run_id: str, attempt_id: str, update: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        bus = ControlBus(self._attempt_dir(run_id, attempt_id))
        def revisioned(current: dict[str, Any]) -> dict[str, Any]:
            updated = update(dict(current))
            updated["revision"] = int(current.get("revision") or 0) + 1
            updated["updated_at"] = time.time()
            return updated
        return bus.update_status(revisioned)

    def _update_run(self, run_id: str, update: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        return ControlBus(self._run_dir(run_id)).update_status(update)

    def _event(self, run_id: str, attempt_id: str, event: str, **payload: Any) -> dict[str, Any]:
        record = {
            "schema_version": 1,
            "event_id": f"evt-{uuid.uuid4().hex}",
            "event": event,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "ts": time.time(),
            **payload,
        }
        _append_jsonl(self._attempt_dir(run_id, attempt_id) / "control" / "events.jsonl", record)
        return record

    def _source_repo(self, source_repo: str | Path) -> Path:
        candidate = Path(source_repo).expanduser().resolve()
        try:
            candidate.relative_to(self.source_root)
        except ValueError as exc:
            raise InteractionError("source repository must be inside the isolated source root", 422) from exc
        if not candidate.is_dir():
            raise InteractionError("source repository not found", 422)
        return candidate

    def _prepare_worktree(self, source_repo: Path, run_id: str, attempt_id: str, source_commit: str) -> Path:
        workspace = self._run_dir(run_id) / "workspaces" / attempt_id
        if workspace.exists() or workspace.is_symlink():
            raise InteractionError("attempt workspace already exists")
        workspace.parent.mkdir(parents=True, exist_ok=True)
        _git(source_repo, "worktree", "add", "--detach", str(workspace), source_commit)
        return workspace

    def _checkpoint_workspace(self, workspace: Path, attempt_id: str) -> str:
        if not _git(workspace, "status", "--porcelain"):
            return _git(workspace, "rev-parse", "HEAD")
        suspicious = (".env", ".pem", ".key", "id_rsa", ".npmrc", ".pypirc", "secrets/")
        changed = _git(workspace, "status", "--porcelain").lower()
        if any(marker in changed for marker in suspicious):
            raise InteractionError("checkpoint contains a potential secret path; capture it explicitly or remove it", 422)
        _git(workspace, "add", "-A")
        staged = _git(workspace, "diff", "--cached", "--")
        secret_markers = ("begin private key", "anthropic_auth_token=", "openai_api_key=", "password=")
        if any(marker in staged.lower() for marker in secret_markers) or re.search(r"\bsk-[A-Za-z0-9_-]{16,}\b", staged, flags=re.IGNORECASE):
            _git(workspace, "reset")
            raise InteractionError("checkpoint diff contains potential secret material; no commit was created", 422)
        if not _git(workspace, "diff", "--cached", "--name-only"):
            return _git(workspace, "rev-parse", "HEAD")
        _git(workspace, "config", "user.email", "interactive-office@example.invalid")
        _git(workspace, "config", "user.name", "LH-Harness Executor checkpoint")
        _git(workspace, "commit", "-m", f"Checkpoint {attempt_id} Executor build")
        return _git(workspace, "rev-parse", "HEAD")

    def _next_attempt_id(self, run_id: str) -> str:
        attempts = self._run_dir(run_id) / "attempts"
        existing = {path.name for path in attempts.iterdir()} if attempts.is_dir() else set()
        index = len(existing) + 1
        while f"attempt-{index:03d}" in existing:
            index += 1
        return f"attempt-{index:03d}"

    def create_run(
        self,
        body: dict[str, Any],
        *,
        principal: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self.adapter_factory is None:
                raise InteractionError("no Harness adapter factory is configured", 501)
            run_id = _safe_id(str(body.get("run_id") or ""), "run_id")
            run_dir = self.runs_root / run_id
            fingerprint = _digest(body)
            if run_dir.exists() or run_dir.is_symlink():
                existing = ControlBus(run_dir).read_status()
                if idempotency_key and existing.get("idempotency_key") == idempotency_key and existing.get("request_digest") == fingerprint:
                    return {**self.projection(run_id), "idempotent": True}
                raise InteractionError("run already exists or idempotency key conflicts")
            task = str(body.get("task") or "").strip()
            criteria = body.get("approved_criteria")
            if not task or len(task) > 100_000:
                raise InteractionError("task is required and must be bounded", 422)
            if not isinstance(criteria, list) or not criteria or any(not isinstance(item, str) or not item.strip() for item in criteria):
                raise InteractionError("approved_criteria must be a non-empty string list", 422)
            criteria = [item.strip() for item in criteria]
            source_repo = self._source_repo(str(body.get("source_repo") or ""))
            if _git(source_repo, "status", "--porcelain"):
                raise InteractionError("source repository has ambiguous uncommitted state", 422)
            source_commit = _git(source_repo, "rev-parse", "HEAD")
            run_dir.mkdir(parents=True)
            attempt_id = "attempt-001"
            attempt_dir = run_dir / "attempts" / attempt_id
            attempt_dir.mkdir(parents=True)
            now = time.time()
            spec_revision = _digest({"task": task, "criteria": criteria})
            rubric_revision = _digest(criteria)
            binding = self._binding(
                body=body,
                principal=principal,
                run_id=run_id,
                attempt_id=attempt_id,
                stage="manager.plan",
                source_commit=source_commit,
                spec_revision=spec_revision,
                rubric_revision=rubric_revision,
            )
            estimate = {
                "schema_version": 1,
                "estimate_id": "estimate-001",
                "revision": 1,
                "parent_estimate_id": None,
                "parent_decision_id": None,
                "kind": "INITIAL",
                "run_id": run_id,
                "attempt_id": attempt_id,
                "selected_stage": "manager.plan",
                "request": task,
                "impact_analysis": "Initial bounded local fixture execution.",
                "effort": body.get("estimate") or {"unknown": True},
                "spec_revision": spec_revision,
                "rubric_revision": rubric_revision,
                "created_at": now,
                "binding": binding,
            }
            _append_jsonl(attempt_dir / "control" / "estimates.jsonl", estimate)
            workspace = self._prepare_worktree(source_repo, run_id, attempt_id, source_commit)
            status = {
                "schema_version": 1,
                "run_id": run_id,
                "attempt_id": attempt_id,
                "parent_attempt_id": None,
                "tenant_id": str(principal.get("tenant_id") or "local"),
                "project_id": str(body.get("project_id") or "interactive-office-local"),
                "authorization_epoch": int(principal.get("authorization_epoch") or 1),
                "status": "awaiting_decision",
                "current_stage": None,
                "selected_stage": "manager.plan",
                "effective_stage": None,
                "round": 1,
                "source_commit": source_commit,
                "source_repo": str(source_repo),
                "workspace": str(workspace),
                "task": task,
                "approved_criteria": criteria,
                "spec_revision": spec_revision,
                "rubric_revision": rubric_revision,
                "current_estimate_id": estimate["estimate_id"],
                "current_decision_id": None,
                "revision": 0,
                "writer_active": False,
                "outputs": {},
                "active_directives": [],
                "created_at": now,
            }
            ControlBus(attempt_dir).write_status(status)
            self._update_run(run_id, lambda _: {
                "schema_version": 1,
                "run_id": run_id,
                "current_attempt_id": attempt_id,
                "attempt_ids": [attempt_id],
                "created_at": now,
                "idempotency_key": idempotency_key,
                "request_digest": fingerprint,
            })
            self._event(run_id, attempt_id, "attempt_created", selected_stage="manager.plan", source_commit=source_commit)
            return self.projection(run_id)

    def _binding(
        self,
        *,
        body: dict[str, Any],
        principal: dict[str, Any],
        run_id: str,
        attempt_id: str,
        stage: str,
        source_commit: str,
        spec_revision: str,
        rubric_revision: str,
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "target": stage,
            "source_sha": source_commit,
            "spec_revision": spec_revision,
            "rubric_revision": rubric_revision,
            "principal": principal.get("sub"),
            "tenant_id": principal.get("tenant_id"),
            "credential_id": principal.get("credential_id"),
            "authorization_epoch": principal.get("authorization_epoch"),
            "policy": principal.get("policy"),
            "limits": body.get("limits") or {"stage_seconds": 300, "max_attempts": 20},
            "expires_at": float(body.get("expires_at") or (time.time() + 3600)),
            "canonical_argument_digest": canonical_body_digest(body),
        }

    def _commands(self, run_id: str, attempt_id: str, kind: str) -> list[dict[str, Any]]:
        return [item for item in ControlBus(self._attempt_dir(run_id, attempt_id)).commands() if item.get("kind") == kind]

    def _append_command(
        self,
        run_id: str,
        attempt_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        command_id: str | None,
        principal: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return ControlBus(self._attempt_dir(run_id, attempt_id)).append(
                kind,
                payload,
                created_by=str(principal.get("sub") or "interaction"),
                command_id=command_id,
                _return_replay=True,
            )
        except (CommandConflict, RevisionConflict) as exc:
            raise InteractionError(str(exc)) from exc

    def request_pause(self, run_id: str, *, principal: dict[str, Any], command_id: str | None = None) -> dict[str, Any]:
        attempt_id = str(self._run_status(run_id).get("current_attempt_id") or "")
        status = self._attempt_status(run_id, attempt_id)
        if status.get("status") in _TERMINAL:
            raise InteractionError("cannot pause a terminal attempt")
        action_id = command_id or f"hold-{uuid.uuid4().hex}"
        command = self._append_command(run_id, attempt_id, "hold", {"attempt_id": attempt_id}, command_id=action_id, principal=principal)
        receipt = ControlBus(self._attempt_dir(run_id, attempt_id)).receipt_for(str(command["command_id"]))
        return {
            "action_id": command["command_id"],
            "status": "paused" if status.get("status") == "paused" else "accepted",
            "deferred": not bool(receipt),
            "idempotent": bool(command.get("_idempotent_replay")),
            "receipt": receipt,
        }

    def request_resume(self, run_id: str, *, principal: dict[str, Any], command_id: str | None = None) -> dict[str, Any]:
        attempt_id = str(self._run_status(run_id).get("current_attempt_id") or "")
        if self._attempt_status(run_id, attempt_id).get("status") != "paused":
            raise InteractionError("attempt is not paused")
        action_id = command_id or f"resume-{uuid.uuid4().hex}"
        command = self._append_command(run_id, attempt_id, "resume_hold", {"attempt_id": attempt_id}, command_id=action_id, principal=principal)
        self._wake(run_id, attempt_id)
        return {"action_id": command["command_id"], "status": "accepted", "idempotent": bool(command.get("_idempotent_replay"))}

    def request_change(self, run_id: str, body: dict[str, Any], *, principal: dict[str, Any], command_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            attempt_id = str(self._run_status(run_id).get("current_attempt_id") or "")
            status = self._attempt_status(run_id, attempt_id)
            request = str(body.get("request") or "").strip()
            if not request:
                raise InteractionError("change request is required", 422)
            supplied = body.get("criteria")
            if not isinstance(supplied, list) or any(not isinstance(item, str) for item in supplied):
                raise InteractionError("criteria must be a string list", 422)
            approved = status.get("approved_criteria") or []
            matched = [item for item in supplied if item in approved]
            covered = bool(supplied) and len(matched) == len(supplied)
            classification = "IN_SCOPE_ADJUSTMENT" if covered else "OUT_OF_SCOPE"
            estimate_id = str(status.get("current_estimate_id") or "")
            decision = self._allowed_decision(run_id, attempt_id, estimate_id)
            if decision is None:
                raise InteractionError("current estimate has no allowed decision")
            payload = {**body, "attempt_id": attempt_id, "classification": classification}
            command = self._append_command(run_id, attempt_id, "scope_change", payload, command_id=command_id, principal=principal)
            existing = self._scope_for_command(run_id, attempt_id, str(command["command_id"]))
            if existing is not None:
                return existing
            if covered:
                directive_id = f"directive-{uuid.uuid4().hex}"
                record = {
                    "schema_version": 1,
                    "directive_id": directive_id,
                    "command_id": command["command_id"],
                    "classification": classification,
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "request": request,
                    "criteria_exact": matched,
                    "parent_estimate_id": estimate_id,
                    "parent_decision_id": decision["decision_id"],
                    "spec_revision": status["spec_revision"],
                    "rubric_revision": status["rubric_revision"],
                    "estimate_usage": 0,
                    "bmad_usage": 0,
                    "replan_usage": 0,
                    "created_at": time.time(),
                    "principal": principal,
                }
                _append_jsonl(self._attempt_dir(run_id, attempt_id) / "control" / "directives.jsonl", record)
                ControlBus(self._attempt_dir(run_id, attempt_id)).receipt(command, "accepted", result={"directive_id": directive_id})
                if status.get("status") in _TERMINAL:
                    self._update_attempt(run_id, attempt_id, lambda current: {**current, "status": "running"})
                    self._start(run_id, attempt_id, "executor.build")
                return record
            estimates = [
                record
                for attempt in self._lineage(run_id, attempt_id)
                for record in _records(self._attempt_dir(run_id, str(attempt["attempt_id"])) / "control" / "estimates.jsonl")
            ]
            revision = max((int(item.get("revision") or 0) for item in estimates), default=0) + 1
            selected = str(body.get("selected_stage") or "manager.plan")
            self._validate_stage(selected)
            new_estimate_id = f"estimate-{revision:03d}"
            missing = [item for item in supplied if item not in approved]
            record = {
                "schema_version": 1,
                "estimate_id": new_estimate_id,
                "revision": revision,
                "parent_estimate_id": estimate_id,
                "parent_decision_id": decision["decision_id"],
                "command_id": command["command_id"],
                "kind": "OUT_OF_SCOPE",
                "classification": classification,
                "run_id": run_id,
                "attempt_id": attempt_id,
                "selected_stage": selected,
                "request": request,
                "criteria_exact": matched,
                "criteria_missing": missing,
                "impact_analysis": str(body.get("impact_analysis") or f"Request adds criteria outside the approved boundary: {missing}"),
                "effort": body.get("estimate") or {"unknown": True},
                "spec_revision": _digest({"parent": status["spec_revision"], "request": request}),
                "rubric_revision": _digest([*approved, *missing]),
                "created_at": time.time(),
                "binding": self._binding(
                    body=body,
                    principal=principal,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    stage=selected,
                    source_commit=str(status["source_commit"]),
                    spec_revision=_digest({"parent": status["spec_revision"], "request": request}),
                    rubric_revision=_digest([*approved, *missing]),
                ),
            }
            _append_jsonl(self._attempt_dir(run_id, attempt_id) / "control" / "estimates.jsonl", record)
            ControlBus(self._attempt_dir(run_id, attempt_id)).receipt(command, "accepted", result={"estimate_id": new_estimate_id})
            return record

    def _scope_for_command(self, run_id: str, attempt_id: str, command_id: str) -> dict[str, Any] | None:
        for name in ("directives", "estimates"):
            for record in _records(self._attempt_dir(run_id, attempt_id) / "control" / f"{name}.jsonl"):
                if record.get("command_id") == command_id:
                    return record
        return None

    def _allowed_decision(self, run_id: str, attempt_id: str, estimate_id: str) -> dict[str, Any] | None:
        for attempt in reversed(self._lineage(run_id, attempt_id)):
            records = _records(self._attempt_dir(run_id, str(attempt["attempt_id"])) / "control" / "decisions.jsonl")
            for record in reversed(records):
                if record.get("estimate_id") == estimate_id and record.get("decision") == "allowed":
                    return record
        return None

    def decide(self, run_id: str, estimate_id: str, body: dict[str, Any], *, principal: dict[str, Any], command_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            current_attempt_id = str(self._run_status(run_id).get("current_attempt_id") or "")
            lineage = self._lineage(run_id, current_attempt_id)
            for attempt in lineage:
                prior_decisions = _records(self._attempt_dir(run_id, str(attempt["attempt_id"])) / "control" / "decisions.jsonl")
                if any(item.get("estimate_id") == estimate_id for item in prior_decisions):
                    raise InteractionError("estimate already has an append-only decision")
            owner_attempt_id = next(
                (
                    str(attempt["attempt_id"])
                    for attempt in reversed(lineage)
                    if any(
                        item.get("estimate_id") == estimate_id
                        for item in _records(self._attempt_dir(run_id, str(attempt["attempt_id"])) / "control" / "estimates.jsonl")
                    )
                ),
                None,
            )
            if owner_attempt_id is None:
                raise InteractionError("estimate not found", 404)
            attempt_id = owner_attempt_id
            choice = str(body.get("decision") or "")
            if choice not in {"allowed", "denied", "held"}:
                raise InteractionError("decision must be exactly allowed, denied, or held", 422)
            estimates = [item for item in _records(self._attempt_dir(run_id, attempt_id) / "control" / "estimates.jsonl") if item.get("estimate_id") == estimate_id]
            payload = {"estimate_id": estimate_id, "decision": choice, "reason": str(body.get("reason") or "")}
            command = self._append_command(run_id, attempt_id, "estimate_decision", payload, command_id=command_id, principal=principal)
            decision = {
                "schema_version": 1,
                "decision_id": f"decision-{uuid.uuid4().hex}",
                "command_id": command["command_id"],
                "estimate_id": estimate_id,
                "decision": choice,
                "reason": payload["reason"],
                "run_id": run_id,
                "attempt_id": attempt_id,
                "created_at": time.time(),
                "principal": principal,
            }
            _append_jsonl(self._attempt_dir(run_id, attempt_id) / "control" / "decisions.jsonl", decision)
            ControlBus(self._attempt_dir(run_id, attempt_id)).receipt(command, "applied", result={"decision_id": decision["decision_id"]})
            if choice == "denied":
                self._update_attempt(run_id, attempt_id, lambda current: {**current, "status": "cancelled", "current_decision_id": decision["decision_id"]})
            elif choice == "allowed":
                self._update_attempt(run_id, attempt_id, lambda current: {**current, "current_decision_id": decision["decision_id"]})
                if estimates[-1].get("kind") == "INITIAL":
                    self._update_attempt(run_id, attempt_id, lambda current: {**current, "status": "running"})
                    self._start(run_id, attempt_id, "manager.plan")
                else:
                    self._restart(run_id, attempt_id, estimates[-1], decision)
            return decision

    @staticmethod
    def _validate_stage(stage: str) -> None:
        if stage not in INTERACTIVE_STAGES:
            raise InteractionError(f"unsupported stage: {stage}", 422)

    def restart(self, run_id: str, attempt_id: str, body: dict[str, Any], *, principal: dict[str, Any], command_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            stage = str(body.get("stage") or "")
            self._validate_stage(stage)
            decision_id = str(body.get("decision_id") or "")
            decision = next((item for item in reversed(_records(self._attempt_dir(run_id, attempt_id) / "control" / "decisions.jsonl")) if item.get("decision_id") == decision_id and item.get("decision") == "allowed"), None)
            if decision is None:
                raise InteractionError("an allowed decision is required for restart")
            estimate = next((item for item in _records(self._attempt_dir(run_id, attempt_id) / "control" / "estimates.jsonl") if item.get("estimate_id") == decision.get("estimate_id")), None)
            if estimate is None or estimate.get("selected_stage") != stage:
                raise InteractionError("decision estimate does not match the restart target")
            expected = body.get("expected_revision")
            if isinstance(expected, bool) or not isinstance(expected, int) or expected != int(self._attempt_status(run_id, attempt_id).get("revision") or 0):
                raise InteractionError("stale attempt revision")
            payload = {"stage": stage, "decision_id": decision_id, "expected_revision": expected}
            command = self._append_command(run_id, attempt_id, "stage_restart", payload, command_id=command_id, principal=principal)
            existing = next((item for item in _records(self._attempt_dir(run_id, attempt_id) / "control" / "restarts.jsonl") if item.get("command_id") == command["command_id"]), None)
            if existing is not None:
                return existing
            return self._restart(run_id, attempt_id, estimate, decision, command=command)

    def _restart(self, run_id: str, attempt_id: str, estimate: dict[str, Any], decision: dict[str, Any], command: dict[str, Any] | None = None) -> dict[str, Any]:
        parent = self._attempt_status(run_id, attempt_id)
        if parent.get("status") not in _TERMINAL | {"paused", "awaiting_decision"} or self._writer_alive(attempt_id) and parent.get("status") != "paused":
            raise InteractionError("cannot restart while an attempt writer is active")
        for record in _records(self._attempt_dir(run_id, attempt_id) / "control" / "restarts.jsonl"):
            if record.get("decision_id") == decision["decision_id"]:
                raise InteractionError("decision already has a linked restart")
        workspace = Path(str(parent.get("workspace") or ""))
        if workspace.is_dir() and _git(workspace, "status", "--porcelain"):
            raise InteractionError("attempt has ambiguous uncommitted state", 422)
        stage = str(estimate["selected_stage"])
        new_attempt_id = self._next_attempt_id(run_id)
        restart_id = f"restart-{uuid.uuid4().hex}"
        downstream = INTERACTIVE_STAGES[INTERACTIVE_STAGES.index(stage):]
        restart = {
            "schema_version": 1,
            "restart_id": restart_id,
            "command_id": command.get("command_id") if command else None,
            "run_id": run_id,
            "parent_attempt_id": attempt_id,
            "attempt_id": new_attempt_id,
            "decision_id": decision["decision_id"],
            "estimate_id": estimate["estimate_id"],
            "selected_stage": stage,
            "invalidated_downstream_stages": list(downstream),
            "source_commit": parent["source_commit"],
            "spec_revision": estimate["spec_revision"],
            "rubric_revision": estimate["rubric_revision"],
            "created_at": time.time(),
            "state": "approved",
        }
        _append_jsonl(self._attempt_dir(run_id, attempt_id) / "control" / "restarts.jsonl", restart)
        if command is not None:
            ControlBus(self._attempt_dir(run_id, attempt_id)).receipt(command, "accepted", result={"restart_id": restart_id, "attempt_id": new_attempt_id})
        new_dir = self._run_dir(run_id) / "attempts" / new_attempt_id
        new_dir.mkdir(parents=True)
        source_repo = Path(str(parent["source_repo"]))
        new_workspace = self._prepare_worktree(source_repo, run_id, new_attempt_id, str(parent["source_commit"]))
        inherited = {
            key: value
            for key, value in (parent.get("outputs") or {}).items()
            if INTERACTIVE_STAGES.index(key) < INTERACTIVE_STAGES.index(stage)
        }
        task = str(parent["task"])
        if estimate.get("kind") == "OUT_OF_SCOPE":
            task = f"{task}\n\nApproved out-of-scope revision {estimate['estimate_id']}:\n{estimate['request']}"
        now = time.time()
        status = {
            **{key: value for key, value in parent.items() if key not in {"status", "outputs", "active_directives", "current_stage", "effective_stage", "selected_stage", "writer_active", "failure_reason"}},
            "run_id": run_id,
            "attempt_id": new_attempt_id,
            "parent_attempt_id": attempt_id,
            "restart_id": restart_id,
            "status": "running",
            "current_stage": None,
            "selected_stage": stage,
            "effective_stage": None,
            "workspace": str(new_workspace),
            "task": task,
            "current_estimate_id": estimate["estimate_id"],
            "current_decision_id": decision["decision_id"],
            "spec_revision": estimate["spec_revision"],
            "rubric_revision": estimate["rubric_revision"],
            "outputs": inherited,
            "active_directives": [],
            "revision": 0,
            "writer_active": False,
            "created_at": now,
        }
        ControlBus(new_dir).write_status(status)
        self._update_attempt(run_id, attempt_id, lambda current: {**current, "status": "superseded", "writer_active": False})
        self._update_run(run_id, lambda current: {**current, "current_attempt_id": new_attempt_id, "attempt_ids": [*current.get("attempt_ids", []), new_attempt_id]})
        self._event(run_id, new_attempt_id, "attempt_restarted", parent_attempt_id=attempt_id, restart_id=restart_id, selected_stage=stage)
        self._start(run_id, new_attempt_id, stage)
        return restart

    def post_turn(self, run_id: str, role: str, body: dict[str, Any], *, principal: dict[str, Any], command_id: str | None = None) -> dict[str, Any]:
        if role not in {"manager", "executor", "auditor"}:
            raise InteractionError("role is not conversation-enabled", 422)
        channel = str(body.get("channel") or "typed")
        if channel not in {"typed", "stt_finalized"}:
            raise InteractionError("turn channel must be typed or stt_finalized", 422)
        text = str(body.get("text") or "").strip()
        if not text or len(text) > 50_000:
            raise InteractionError("turn text is required and must be bounded", 422)
        attempt_id = str(self._run_status(run_id).get("current_attempt_id") or "")
        if self._attempt_status(run_id, attempt_id).get("status") not in {"running", "paused"}:
            raise InteractionError("attempt is not at a supported safe-turn boundary")
        conversation_id = str(body.get("conversation_id") or f"conversation:{run_id}:{attempt_id}:{role}")
        if len(conversation_id) > 256 or "\x00" in conversation_id:
            raise InteractionError("conversation_id is invalid", 422)
        turn_id = f"turn-{uuid.uuid4().hex}"
        context = body.get("context") if isinstance(body.get("context"), dict) else {}
        payload = {
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "role": role,
            "channel": channel,
            "text": text,
            "context": context,
        }
        command = self._append_command(run_id, attempt_id, "role_turn", payload, command_id=command_id, principal=principal)
        persisted = command.get("payload") or payload
        turn_id = str(persisted.get("turn_id"))
        existing = self._turn(run_id, attempt_id, turn_id)
        if existing is None:
            turn = {
                "schema_version": 1,
                **persisted,
                "message_id": f"message-{uuid.uuid4().hex}",
                "run_id": run_id,
                "attempt_id": attempt_id,
                "speaker": "customer",
                "principal": principal,
                "created_at": time.time(),
                "command_id": command["command_id"],
            }
            _append_jsonl(self._attempt_dir(run_id, attempt_id) / "control" / "turns.jsonl", turn)
            ControlBus(self._attempt_dir(run_id, attempt_id)).receipt(command, "accepted", result={"turn_id": turn_id})
        busy = self._writer_alive(attempt_id) and self._attempt_status(run_id, attempt_id).get("status") == "running"
        self._wake(run_id, attempt_id)
        return {
            "turn_id": turn_id,
            "conversation_id": persisted["conversation_id"],
            "status": "deferred" if busy else "accepted",
            "busy": busy,
            "idempotent": bool(command.get("_idempotent_replay")),
        }

    def _turn(self, run_id: str, attempt_id: str, turn_id: str) -> dict[str, Any] | None:
        return next((item for item in _records(self._attempt_dir(run_id, attempt_id) / "control" / "turns.jsonl") if item.get("turn_id") == turn_id), None)

    def set_customer_done(self, run_id: str, body: dict[str, Any], *, principal: dict[str, Any], command_id: str | None = None) -> dict[str, Any]:
        if body.get("done") is not True:
            raise InteractionError("customer DONE requires done=true", 422)
        return self._customer_feedback(run_id, {"done": True}, principal=principal, command_id=command_id)

    def set_customer_rating(self, run_id: str, body: dict[str, Any], *, principal: dict[str, Any], command_id: str | None = None) -> dict[str, Any]:
        stars = body.get("stars")
        if stars is not None and (isinstance(stars, bool) or not isinstance(stars, int) or not 1 <= stars <= 5):
            raise InteractionError("stars must be null or an integer from 1 to 5", 422)
        return self._customer_feedback(run_id, {"stars": stars}, principal=principal, command_id=command_id)

    def _customer_feedback(self, run_id: str, patch: dict[str, Any], *, principal: dict[str, Any], command_id: str | None) -> dict[str, Any]:
        attempt_id = str(self._run_status(run_id).get("current_attempt_id") or "")
        command = self._append_command(run_id, attempt_id, "customer_feedback", patch, command_id=command_id, principal=principal)
        existing = next((item for item in reversed(_records(self._attempt_dir(run_id, attempt_id) / "control" / "feedback.jsonl")) if item.get("command_id") == command["command_id"]), None)
        if existing is None:
            record = {
                "schema_version": 1,
                "feedback_id": f"feedback-{uuid.uuid4().hex}",
                "command_id": command["command_id"],
                "run_id": run_id,
                "attempt_id": attempt_id,
                **patch,
                "principal": principal,
                "created_at": time.time(),
            }
            _append_jsonl(self._attempt_dir(run_id, attempt_id) / "control" / "feedback.jsonl", record)
            ControlBus(self._attempt_dir(run_id, attempt_id)).receipt(command, "applied", result={"feedback_id": record["feedback_id"]})
            existing = record
        return existing

    def _lineage(self, run_id: str, attempt_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        current: str | None = attempt_id
        while current and current not in seen:
            seen.add(current)
            status = self._attempt_status(run_id, current)
            if not status:
                break
            result.append(status)
            current = str(status.get("parent_attempt_id") or "") or None
        result.reverse()
        return result

    def projection(self, run_id: str) -> dict[str, Any]:
        run = self._run_status(run_id)
        if not run:
            raise InteractionError("run not found", 404)
        current_attempt = str(run.get("current_attempt_id") or "")
        lineage = self._lineage(run_id, current_attempt)
        current = lineage[-1]
        events: list[dict[str, Any]] = []
        estimates: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        directives: list[dict[str, Any]] = []
        turns: list[dict[str, Any]] = []
        feedback: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        receipts: list[dict[str, Any]] = []
        restarts: list[dict[str, Any]] = []
        for attempt in lineage:
            attempt_id = str(attempt["attempt_id"])
            control = self._attempt_dir(run_id, attempt_id) / "control"
            events.extend(_records(control / "events.jsonl"))
            estimates.extend(_records(control / "estimates.jsonl"))
            decisions.extend(_records(control / "decisions.jsonl"))
            directives.extend(_records(control / "directives.jsonl"))
            turns.extend(_records(control / "turns.jsonl"))
            feedback.extend(_records(control / "feedback.jsonl"))
            artifacts.extend(_records(control / "artifacts.jsonl"))
            receipts.extend(ControlBus(self._attempt_dir(run_id, attempt_id)).receipts())
            restarts.extend(_records(control / "restarts.jsonl"))
        done = any(item.get("done") is True for item in feedback)
        stars: int | None = None
        for item in feedback:
            if "stars" in item:
                stars = item["stars"]
        usage = self._usage_projection(events)
        return {
            "schema_version": 1,
            "run_id": run_id,
            "tenant_id": current.get("tenant_id"),
            "project_id": current.get("project_id"),
            "authorization_epoch": current.get("authorization_epoch"),
            "status": current.get("status"),
            "failure_reason": current.get("failure_reason"),
            "current_stage": current.get("current_stage"),
            "selected_stage": current.get("selected_stage"),
            "effective_stage": current.get("effective_stage"),
            "attempt_id": current_attempt,
            "revision": current.get("revision"),
            "source_commit": current.get("source_commit"),
            "spec_revision": current.get("spec_revision"),
            "rubric_revision": current.get("rubric_revision"),
            "attempts": [
                {
                    "attempt_id": item.get("attempt_id"),
                    "parent_attempt_id": item.get("parent_attempt_id"),
                    "restart_id": item.get("restart_id"),
                    "status": item.get("status"),
                    "selected_stage": item.get("selected_stage"),
                    "effective_stage": item.get("effective_stage"),
                    "revision": item.get("revision"),
                    "source_commit": item.get("source_commit"),
                }
                for item in lineage
            ],
            "stages": [item for item in events if item.get("event") in {"stage_entry", "stage_exit", "checkpoint"}],
            "events": events,
            "estimates": estimates,
            "decisions": decisions,
            "directives": directives,
            "restarts": restarts,
            "receipts": receipts,
            "artifacts": artifacts,
            "turns": turns,
            "usage": usage,
            "customer": {"done": done, "stars": stars},
            "controls": {
                "can_pause": current.get("status") not in _TERMINAL,
                "can_resume": current.get("status") == "paused",
                "can_restart": current.get("status") in _TERMINAL | {"paused"},
            },
        }

    @staticmethod
    def _usage_projection(events: list[dict[str, Any]]) -> dict[str, Any]:
        by_role: dict[str, dict[str, Any]] = {}
        for event in events:
            if event.get("event") not in {"stage_exit", "role_turn_done"}:
                continue
            role = str(event.get("role") or "unknown")
            actual = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            bucket = by_role.setdefault(role, {"metered": False, "input_tokens": None, "output_tokens": None, "total_tokens": None, "cost": None, "unknown": ["input_tokens", "output_tokens", "total_tokens", "cost"]})
            known = [key for key in ("input_tokens", "output_tokens", "total_tokens", "cost") if actual.get(key) is not None]
            if known:
                bucket["metered"] = True
                bucket["unknown"] = [key for key in bucket["unknown"] if key not in known]
                for key in known:
                    bucket[key] = (bucket[key] or 0) + actual[key]
        return {"roles": by_role, "unmetered_is_unknown": True, "estimate_replan_bmad": {"estimate": 0, "replan": 0, "bmad": 0}}

    def artifact_content(self, run_id: str, artifact_id: str) -> dict[str, Any]:
        projection = self.projection(run_id)
        artifact = next((item for item in projection["artifacts"] if item.get("artifact_id") == artifact_id), None)
        if artifact is None:
            raise InteractionError("artifact not found", 404)
        path = self._attempt_dir(run_id, str(artifact["attempt_id"])) / str(artifact["path"])
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise InteractionError("artifact content is unavailable", 404) from exc
        if len(raw) > 8 * 1024 * 1024:
            raise InteractionError("artifact is too large", 413)
        if hashlib.sha256(raw).hexdigest() != artifact.get("sha256"):
            raise InteractionError("artifact revision does not match its manifest", 409)
        return {"artifact": artifact, "content": raw.decode("utf-8", errors="replace")}

    def evidence(self, run_id: str) -> dict[str, Any]:
        projection = self.projection(run_id)
        status = self._attempt_status(run_id, str(projection["attempt_id"]))
        workspace = Path(str(status["workspace"]))
        evidence: dict[str, Any] = {
            **projection,
            "workspace": {"commits": [], "files": [], "diff": "", "status": "unknown"},
        }
        try:
            evidence["workspace"] = {
                "status": _git(workspace, "status", "--porcelain"),
                "files": _git(workspace, "ls-files").splitlines(),
                "diff": _git(workspace, "diff", "HEAD", "--")[:2_000_000],
                "commits": _git(workspace, "log", "-20", "--pretty=format:%H%x09%s").splitlines(),
            }
        except InteractionError as exc:
            evidence["workspace"] = {"status": "unavailable", "error": str(exc), "commits": [], "files": [], "diff": ""}
        return evidence

    def workspace_file(self, run_id: str, name: str, expected_revision: str | None = None) -> dict[str, Any]:
        status = self._attempt_status(run_id, str(self._run_status(run_id).get("current_attempt_id") or ""))
        workspace = Path(str(status["workspace"])).resolve()
        if expected_revision is not None and expected_revision != _git(workspace, "rev-parse", "HEAD"):
            raise InteractionError("stale workspace revision")
        candidate = (workspace / name).resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise InteractionError("workspace file is outside its boundary", 422) from exc
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or any((workspace / part).is_symlink() for part in relative.parents if str(part) != "."):
            raise InteractionError("workspace file path is unsafe", 422)
        if candidate.is_symlink() or not candidate.is_file():
            raise InteractionError("workspace file not found", 404)
        raw = candidate.read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            raise InteractionError("workspace file is too large", 413)
        return {"path": name, "revision": _git(workspace, "rev-parse", "HEAD"), "content": raw.decode("utf-8", errors="replace")}

    def _writer_alive(self, attempt_id: str) -> bool:
        thread = self._threads.get(attempt_id)
        return bool(thread and thread.is_alive())

    def _start(self, run_id: str, attempt_id: str, stage: str) -> None:
        self._validate_stage(stage)
        with self._lock:
            for other_id, thread in self._threads.items():
                if other_id == attempt_id or not thread.is_alive():
                    continue
                other_status = self._attempt_status(run_id, other_id)
                if other_status.get("status") != "superseded":
                    raise InteractionError("another attempt writer is active")
            if self._writer_alive(attempt_id):
                return
            self._update_attempt(run_id, attempt_id, lambda current: {**current, "writer_active": True, "selected_stage": stage})
            thread = threading.Thread(target=self._thread_main, args=(run_id, attempt_id, stage), name=f"interactive-{attempt_id}", daemon=True)
            self._threads[attempt_id] = thread
            thread.start()

    def _wake(self, run_id: str, attempt_id: str) -> None:
        status = self._attempt_status(run_id, attempt_id)
        if status.get("status") == "running" and not self._writer_alive(attempt_id):
            self._start(run_id, attempt_id, str(status.get("current_stage") or status.get("selected_stage") or "manager.plan"))

    def wait_for_idle(self, run_id: str, timeout: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self._run_status(run_id)
            attempt_id = str(run.get("current_attempt_id") or "")
            thread = self._threads.get(attempt_id)
            if thread is not None:
                thread.join(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
            if not self._writer_alive(attempt_id):
                return self.projection(run_id)
        raise TimeoutError("interactive attempt did not become idle")

    def recover(self) -> None:
        if not self.runs_root.is_dir():
            return
        for run_dir in self.runs_root.iterdir():
            if run_dir.is_symlink() or not run_dir.is_dir():
                continue
            run = ControlBus(run_dir).read_status()
            attempt_id = str(run.get("current_attempt_id") or "")
            if not attempt_id:
                continue
            status = self._attempt_status(run_dir.name, attempt_id)
            if status.get("status") != "running":
                continue
            self._update_attempt(run_dir.name, attempt_id, lambda current: {**current, "writer_active": False})
            self._start(run_dir.name, attempt_id, str(status.get("current_stage") or status.get("selected_stage") or "manager.plan"))

    def _thread_main(self, run_id: str, attempt_id: str, stage: str) -> None:
        import asyncio

        try:
            asyncio.run(self._execute(run_id, attempt_id, stage))
        except _Superseded:
            pass
        except Exception as exc:
            try:
                self._event(run_id, attempt_id, "attempt_failed", error=str(exc))
                self._update_attempt(run_id, attempt_id, lambda current: {**current, "status": "failed", "failure_reason": str(exc)})
            except Exception:
                pass
        finally:
            try:
                self._update_attempt(run_id, attempt_id, lambda current: {**current, "writer_active": False})
            except Exception:
                pass

    async def _execute(self, run_id: str, attempt_id: str, selected_stage: str) -> None:
        index = INTERACTIVE_STAGES.index(selected_stage)
        while index < len(INTERACTIVE_STAGES):
            stage = INTERACTIVE_STAGES[index]
            await self._boundary(run_id, attempt_id, stage, "before_entry")
            if stage == "executor.build":
                self._apply_directives(run_id, attempt_id)
            await self._run_stage(run_id, attempt_id, stage)
            await self._boundary(run_id, attempt_id, stage, "after_exit")
            if stage != "manager.plan" and self._pending_directives(run_id, attempt_id):
                index = INTERACTIVE_STAGES.index("executor.build")
            else:
                index += 1
        status = self._attempt_status(run_id, attempt_id)
        audit_ref = (status.get("outputs") or {}).get("auditor.verify")
        audit_text = self._artifact_text(run_id, audit_ref) if audit_ref else ""
        audit = parse_audit_report(audit_text, 1, language="en")
        complete = audit.status == "complete" and audit.integrity_status == "clean" and audit.contract_audit_status == "aligned"
        final_status = "completed" if complete else "incomplete"
        self._update_attempt(run_id, attempt_id, lambda current: {**current, "status": final_status, "current_stage": None, "effective_stage": None, "completion_satisfied": complete})
        self._event(run_id, attempt_id, "attempt_done", status=final_status, completion_satisfied=complete)
        self._event(run_id, attempt_id, "customer_review_requested", status="unconfirmed", choices=["DONE", "change_requested", "on_hold"])

    async def _boundary(self, run_id: str, attempt_id: str, stage: str, boundary: str) -> None:
        bus = ControlBus(self._attempt_dir(run_id, attempt_id))
        holds = [item for item in bus.commands() if item.get("kind") == "hold"]
        resumes = [item for item in bus.commands() if item.get("kind") == "resume_hold"]
        latest_hold = max((float(item.get("created_at") or 0) for item in holds), default=0)
        latest_resume = max((float(item.get("created_at") or 0) for item in resumes), default=0)
        if latest_hold > latest_resume:
            status = self._attempt_status(run_id, attempt_id)
            checkpoint = {
                "stage": stage,
                "boundary": boundary,
                "round": status.get("round") or 1,
                "source_commit": status.get("source_commit"),
                "artifact_manifest": list((status.get("outputs") or {}).values()),
            }
            for command in holds:
                if bus.receipt_for(str(command["command_id"])) is None:
                    bus.receipt(command, "applied", message="safe role-boundary checkpoint", result=checkpoint)
            self._event(run_id, attempt_id, "checkpoint", **checkpoint)
            self._update_attempt(run_id, attempt_id, lambda current: {**current, "status": "paused", "current_stage": stage})
            while True:
                if str(self._run_status(run_id).get("current_attempt_id") or "") != attempt_id:
                    raise _Superseded()
                pending_resumes = [item for item in bus.pending() if item.get("kind") == "resume_hold"]
                if pending_resumes:
                    for command in pending_resumes:
                        bus.receipt(command, "applied", result={"stage": stage, "boundary": boundary, "attempt_id": attempt_id})
                    self._event(run_id, attempt_id, "hold_resumed", stage=stage, boundary=boundary)
                    self._update_attempt(run_id, attempt_id, lambda current: {**current, "status": "running"})
                    break
                await __import__("asyncio").sleep(self.poll_interval)
        await self._reply_to_turns(run_id, attempt_id)

    def _pending_directives(self, run_id: str, attempt_id: str) -> list[dict[str, Any]]:
        status = self._attempt_status(run_id, attempt_id)
        active = set(status.get("active_directives") or [])
        return [item for item in _records(self._attempt_dir(run_id, attempt_id) / "control" / "directives.jsonl") if item.get("directive_id") not in active]

    def _apply_directives(self, run_id: str, attempt_id: str) -> None:
        pending = self._pending_directives(run_id, attempt_id)
        if not pending:
            return
        def apply(current: dict[str, Any]) -> dict[str, Any]:
            outputs = dict(current.get("outputs") or {})
            for stage in ("auditor.verify", "final_response"):
                outputs.pop(stage, None)
            return {
                **current,
                "outputs": outputs,
                "active_directives": [*current.get("active_directives", []), *[item["directive_id"] for item in pending]],
            }
        self._update_attempt(run_id, attempt_id, apply)
        for directive in pending:
            self._event(run_id, attempt_id, "in_scope_adjustment_applied", directive_id=directive["directive_id"], estimate_usage=0, replan_usage=0, bmad_usage=0, invalidated_downstream_stages=["auditor.verify", "final_response"])

    async def _reply_to_turns(self, run_id: str, attempt_id: str) -> None:
        bus = ControlBus(self._attempt_dir(run_id, attempt_id))
        status = self._attempt_status(run_id, attempt_id)
        for command in list(bus.commands()):
            if command.get("kind") != "role_turn":
                continue
            payload = command.get("payload") or {}
            turn_id = str(payload.get("turn_id") or "")
            turns = _records(self._attempt_dir(run_id, attempt_id) / "control" / "turns.jsonl")
            if any(item.get("reply_to_turn_id") == turn_id for item in turns):
                continue
            receipt = bus.receipt_for(str(command["command_id"]))
            if receipt and receipt.get("status") in {"applied", "failed"}:
                continue
            role = str(payload.get("role") or "")
            prompt = (
                f"You are the configured {role} in a bounded local Harness run. Answer the canonical customer turn.\n"
                f"Run: {run_id}\nAttempt: {attempt_id}\nConversation: {payload.get('conversation_id')}\n"
                f"Object/artifact context: {json.dumps(payload.get('context') or {}, sort_keys=True)}\n"
                f"Customer: {payload.get('text')}"
            )
            try:
                result = await self._call_adapter(role, prompt, status, attempt_id, live_name=f"turn-{turn_id}.jsonl")
                text = _visible_output(result).strip()
                if result.status != "done" or not text:
                    raise InteractionError(f"role adapter returned no substantive reply: {result.error or result.status}")
                reply = {
                    "schema_version": 1,
                    "turn_id": f"turn-{uuid.uuid4().hex}",
                    "message_id": f"message-{uuid.uuid4().hex}",
                    "reply_to_turn_id": turn_id,
                    "conversation_id": payload["conversation_id"],
                    "role": role,
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "speaker": role,
                    "channel": "role_adapter",
                    "text": text,
                    "created_at": time.time(),
                }
                _append_jsonl(self._attempt_dir(run_id, attempt_id) / "control" / "turns.jsonl", reply)
                self._event(run_id, attempt_id, "role_turn_done", role=role, turn_id=turn_id, reply_turn_id=reply["turn_id"], usage=self._usage(result))
                bus.receipt(command, "applied", result={"turn_id": turn_id, "reply_turn_id": reply["turn_id"]})
            except Exception as exc:
                bus.receipt(command, "failed", message=str(exc), result={"turn_id": turn_id})

    async def _call_adapter(self, role: str, prompt: str, status: dict[str, Any], attempt_id: str, live_name: str) -> EpisodeResult:
        if self.adapter_factory is None:
            raise InteractionError("no Harness adapter factory is configured", 501)
        adapter = self.adapter_factory(role)
        if adapter is None:
            raise InteractionError(f"no Harness adapter is configured for {role}", 501)
        adapter.workspace_path = str(status["workspace"])
        env = _WorkspaceEnvironment(Path(str(status["workspace"])), self._attempt_dir(str(status["run_id"]), attempt_id) / "tmp")
        trajectory = self._attempt_dir(str(status["run_id"]), attempt_id) / "trajectories" / live_name
        return await adapter.run_episode(prompt, env, EpisodeBudget(max_duration_seconds=300), live_trajectory_path=str(trajectory))

    async def _run_stage(self, run_id: str, attempt_id: str, stage: str) -> None:
        status = self._attempt_status(run_id, attempt_id)
        role = _STAGE_ROLES[stage]
        execution_id = f"stage-{uuid.uuid4().hex}"
        entry = {
            "stage": stage,
            "role": role,
            "execution_id": execution_id,
            "round": status.get("round") or 1,
            "source_commit": status.get("source_commit"),
            "artifact_manifest": list((status.get("outputs") or {}).values()),
            "selected_stage": status.get("selected_stage"),
        }
        self._update_attempt(run_id, attempt_id, lambda current: {**current, "status": "running", "current_stage": stage, "effective_stage": stage})
        self._event(run_id, attempt_id, "stage_entry", **entry)
        prompt = self._stage_prompt(run_id, attempt_id, stage)
        result = await self._call_adapter(role, prompt, self._attempt_status(run_id, attempt_id), attempt_id, live_name=f"{execution_id}.jsonl")
        text = _visible_output(result).strip()
        if result.status != "done":
            raise InteractionError(f"{stage} adapter failed: {result.error or result.status}")
        if not text:
            raise InteractionError(f"{stage} adapter produced no readable output")
        if stage == "manager.plan":
            text = extract_role_manager_plan_text(text).strip()
            if parse_role_manager_next_step(text) != MANAGER_NEXT_CLI:
                raise InteractionError("local fixture requires Manager to emit exactly a CLI build plan; output: " + (text[:400] or "<empty>"))
        artifact = self._persist_artifact(run_id, attempt_id, stage, role, execution_id, text)
        def complete(current: dict[str, Any]) -> dict[str, Any]:
            outputs = {**current.get("outputs", {}), stage: artifact}
            updated = {**current, "outputs": outputs}
            if stage == "executor.build":
                workspace = Path(str(current["workspace"]))
                updated["source_commit"] = self._checkpoint_workspace(workspace, attempt_id)
                updated["worktree_dirty"] = bool(_git(workspace, "status", "--porcelain"))
            return updated
        updated = self._update_attempt(run_id, attempt_id, complete)
        self._event(
            run_id,
            attempt_id,
            "stage_exit",
            stage=stage,
            role=role,
            execution_id=execution_id,
            round=updated.get("round") or 1,
            source_commit=updated.get("source_commit"),
            artifact=artifact,
            usage=self._usage(result),
            status="completed",
        )

    def _stage_prompt(self, run_id: str, attempt_id: str, stage: str) -> str:
        status = self._attempt_status(run_id, attempt_id)
        outputs = status.get("outputs") or {}
        plan = self._artifact_text(run_id, outputs["manager.plan"]) if stage != "manager.plan" else ""
        executor_output = self._artifact_text(run_id, outputs["executor.build"]) if stage in {"auditor.verify", "final_response"} else ""
        auditor_report = self._artifact_text(run_id, outputs["auditor.verify"]) if stage == "final_response" else ""
        task_state = extract_role_task_state(plan, fallback="") if plan else ""
        task_contract = extract_role_task_contract(plan, fallback="") if plan else ""
        if stage == "manager.plan":
            return build_role_manager_prompt(task=str(status["task"]), rounds=[], round_index=1, task_state="", task_contract="", round_budget=1, language="en")
        if stage == "executor.build":
            prompt = build_role_executor_prompt(task=str(status["task"]), plan_text=plan, next_step="cli", task_state=task_state, task_contract=task_contract, related_auditor_reports="", workspace_path=str(status["workspace"]), language="en")
            directives = _records(self._attempt_dir(run_id, attempt_id) / "control" / "directives.jsonl")
            active = set(status.get("active_directives") or [])
            applicable = [item for item in directives if item.get("directive_id") in active]
            if applicable:
                prompt += "\n\nApproved in-scope adjustments (no replan or new estimate):\n" + "\n".join(f"- {item['request']} (exact criteria: {item['criteria_exact']})" for item in applicable)
            return prompt
        if stage == "auditor.verify":
            return build_role_auditor_prompt(task=str(status["task"]), plan_text=plan, executor_output=executor_output, next_step="cli", task_state=task_state, task_contract=task_contract, related_auditor_reports="", workspace_path=str(status["workspace"]), language="en")
        round_record = ManagedRound(round_index=1, next_step="cli", plan_text=plan, executor_output=executor_output, auditor_report=auditor_report, task_state=task_state, task_contract=task_contract)
        complete = parse_audit_report(auditor_report, 1, language="en").status == "complete"
        prompt = build_role_final_response_prompt(task=str(status["task"]), rounds=[round_record], status="complete" if complete else "incomplete", abort_reason="" if complete else "verification_incomplete", task_state=task_state, operator_instructions="", language="en")
        return prompt + "\n\nEnd by asking the customer to review this exact result and explicitly choose DONE, request changes, or hold. Never mark DONE or infer approval on the customer's behalf."

    def _artifact_text(self, run_id: str, reference: dict[str, Any]) -> str:
        path = self._attempt_dir(run_id, str(reference["attempt_id"])) / str(reference["path"])
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference.get("sha256"):
            raise InteractionError("input artifact revision does not match its manifest")
        return raw.decode("utf-8")

    def _persist_artifact(self, run_id: str, attempt_id: str, stage: str, role: str, execution_id: str, text: str) -> dict[str, Any]:
        raw = text.encode("utf-8")
        relative = Path("artifacts") / execution_id / f"{stage}.txt"
        path = self._attempt_dir(run_id, attempt_id) / relative
        _atomic_bytes_write(path, raw)
        artifact = {
            "schema_version": 1,
            "artifact_id": f"artifact-{uuid.uuid4().hex}",
            "run_id": run_id,
            "attempt_id": attempt_id,
            "stage": stage,
            "role": role,
            "execution_id": execution_id,
            "round": self._attempt_status(run_id, attempt_id).get("round") or 1,
            "path": relative.as_posix(),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "created_at": time.time(),
        }
        _append_jsonl(self._attempt_dir(run_id, attempt_id) / "control" / "artifacts.jsonl", artifact)
        return artifact

    @staticmethod
    def _usage(result: EpisodeResult) -> dict[str, Any]:
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        supplied = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
        return {
            key: supplied.get(key, metadata.get(key))
            for key in ("input_tokens", "output_tokens", "total_tokens", "cost")
            if supplied.get(key, metadata.get(key)) is not None
        }


class _Superseded(Exception):
    pass
