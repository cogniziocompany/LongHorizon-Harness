"""Background launcher that promotes queue entries into supervised runs.

The launcher is an asyncio task started with the Web API. It polls the
``QueueStore``, evaluates capacity from ``[queue.capacity]``, counts active runs
per trio, skips workspaces that already have an active run, and launches the
highest-priority eligible entry through the same ``supervisor.create_run`` path
used by ``POST /api/runs``.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .config import PROJECT_CONFIG_PATH, load_run_defaults
from .contention import ContentionGroup, detect_contention, groups_to_json
from .queue import QueueEntry, QueueStore, default_queue_config, queue_config_from_config
from .supervisor.lifecycle import ACTIVE_STATUSES, canonical_lifecycle_status
from .workspace_identity import resolve_many

# ``httpx`` is already a transitive dependency of FastAPI/TestClient, but the
# launcher must not fail to import when it is absent.
try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore


_MAX_REASON_LEN = 4_000


def _now() -> float:
    return time.time()


def _normalize_workspace(value: str) -> str:
    """Return a stable absolute form for workspace-path comparison."""

    return os.path.normcase(os.path.normpath(os.path.abspath(str(value or ""))))


def _read_report_json(path: Path) -> dict[str, Any]:
    """Load a report.json if it exists and is valid JSON."""

    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class Launcher:
    """Poll the queue and launch eligible entries through the supervisor."""

    def __init__(
        self,
        supervisor: Any,
        queue_store: QueueStore,
        *,
        poll_seconds: float = 15.0,
        queue_config: dict[str, Any] | None = None,
        min_emit_severity: str = "same_repo",
    ) -> None:
        self.supervisor = supervisor
        self.queue_store = queue_store
        self._config = queue_config or self._load_project_queue_config()
        self._poll_seconds = float(
            self._config.get("capacity", {}).get("poll_seconds", poll_seconds)
        )
        self._task: asyncio.Task | None = None
        self._stopping = False
        # One launcher instance must never launch two runs into the same
        # workspace across concurrent ticks.  This lock serializes the critical
        # section from eligibility check through store mark_launched.
        self._launch_lock = threading.Lock()
        self._contentions: dict[str, ContentionGroup] = {}
        self._min_emit_severity = min_emit_severity

    @staticmethod
    def _load_project_queue_config() -> dict[str, Any]:
        try:
            project = load_run_defaults(PROJECT_CONFIG_PATH)
            if isinstance(project.get("queue"), dict):
                return queue_config_from_config(project)
        except Exception:
            pass
        return default_queue_config()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None

    async def _loop(self) -> None:
        while not self._stopping:
            await self.tick()
            await asyncio.sleep(self._poll_seconds)

    async def tick(self) -> None:
        """Run one poll/launch pass, moving blocking work off the event loop."""

        await asyncio.to_thread(self._tick_sync)

    def _tick_sync(self) -> None:
        try:
            with self._launch_lock:
                self._run_pass()
        except Exception as exc:
            self._emit_service_event("queue.error", {"error": str(exc)[:200]})

    def _run_pass(self) -> None:
        entries = self.queue_store.list()
        active = self._active_runs()
        self._update_launched_entries(active)
        # Contention visibility is warn-only.  A git failure must never abort
        # the launch pass, so the bare except is deliberate.
        try:
            self._check_contention(active)
        except Exception as exc:
            self._emit_service_event("queue.error", {"error": f"contention check failed: {exc}"[:200]})
        capacities = self._remaining_capacity(active)
        launched = False
        for entry in entries:
            if entry.status != "pending":
                continue
            if launched:
                # Once we have launched one entry this pass, any later pending
                # entry in the same trio must be skipped with an up-to-date
                # capacity reason.
                skip_reason = self._capacity_reason(entry, capacities)
                if skip_reason is None:
                    skip_reason = "launch batch already consumed capacity"
                self._skip(entry, skip_reason)
                continue
            skip_reason = self._check_eligibility(entry, active, capacities)
            if skip_reason is None:
                self._launch(entry)
                launched = True
                capacities[entry.trio] = capacities.get(entry.trio, 0) - 1
            else:
                self._skip(entry, skip_reason)

    def _update_launched_entries(
        self, active: dict[str, dict[str, Any]]
    ) -> None:
        """Promote launched entries to done/failed once their run is terminal."""

        for entry in self.queue_store.list():
            if entry.status != "launched" or not entry.run_id:
                continue
            run_id = entry.run_id
            if run_id in active:
                continue
            status = self.supervisor.status(run_id)
            lifecycle = canonical_lifecycle_status(status.get("status"))
            if lifecycle in ACTIVE_STATUSES:
                continue
            run_status = status.get("status") or "unknown"
            # The supervisor exposes owner/status but not the manager report.
            # Read the durable audit result directly from the run directory.
            report = self._read_run_report(run_id)
            # A cancelled worker can still carry a successful manager audit.
            # Treat the queue entry as done when the run report is satisfied,
            # even if the operator stopped the process.
            completion_satisfied = report.get("completion_satisfied") is True
            if run_status == "completed" or completion_satisfied:
                reason = "run completed"
                if run_status != "completed":
                    reason = f"run {run_status} with completion satisfied"
                updated = self.queue_store.mark_done(
                    entry.queue_id, reason=reason
                )
                self._emit_run_event(
                    run_id,
                    "queue.done",
                    {
                        "queue_id": entry.queue_id,
                        "run_id": run_id,
                        "trio": entry.trio,
                        "workspace": entry.workspace,
                    },
                )
            else:
                updated = self.queue_store.mark_failed(
                    entry.queue_id, reason=f"run {run_status}"
                )
                self._emit_run_event(
                    run_id,
                    "queue.failed",
                    {
                        "queue_id": entry.queue_id,
                        "run_id": run_id,
                        "trio": entry.trio,
                        "workspace": entry.workspace,
                        "run_status": run_status,
                    },
                )
            if updated is not None:
                updated.last_checked_at = _now()
                self.queue_store.update(updated)

    def _read_run_report(self, run_id: str) -> dict[str, Any]:
        """Read the manager audit report for a run from durable storage."""

        runs_root = getattr(self.supervisor, "runs_root", None)
        if runs_root is None:
            return {}
        report_path = Path(runs_root) / run_id / "lh_harness" / "report.json"
        return _read_report_json(report_path)

    def _capacity_reason(self, entry: QueueEntry, capacities: dict[str, int]) -> str | None:
        if capacities.get(entry.trio, 0) <= 0:
            return f"{entry.trio} at capacity"
        return None

    def _check_eligibility(
        self,
        entry: QueueEntry,
        active: dict[str, dict[str, Any]],
        capacities: dict[str, int],
    ) -> str | None:
        capacity = self._config.get("capacity", {})
        if entry.trio == "kimi" and capacity.get("key_health_url"):
            min_healthy = int(capacity.get("min_healthy_keys", 2))
            if not self._key_health_ok(capacity["key_health_url"], min_healthy):
                return "key health insufficient"
        if capacities.get(entry.trio, 0) <= 0:
            return f"{entry.trio} at capacity"
        entry_workspace = _normalize_workspace(entry.workspace)
        for run_id, info in active.items():
            owner = info.get("owner", {})
            workspace = _normalize_workspace(owner.get("workspace", ""))
            if workspace and workspace == entry_workspace:
                return f"workspace {entry.workspace} has active run {run_id}"
        return None

    def _key_health_ok(self, url: str, min_healthy: int) -> bool:
        if not url:
            return True
        try:
            if httpx is not None:
                response = httpx.get(url, timeout=10)
                response.raise_for_status()
                data = response.json()
            else:
                import urllib.request

                with urllib.request.urlopen(url, timeout=10) as resp:
                    data = json.loads(resp.read())
        except Exception:
            return False
        healthy = 0
        if isinstance(data, dict):
            keys = data.get("healthy_keys")
        else:
            keys = None
        if isinstance(keys, list):
            healthy = sum(
                1
                for key in keys
                if isinstance(key, dict) and key.get("healthy") is True
            )
        return healthy >= min_healthy

    def _check_contention(
        self,
        active: dict[str, dict[str, Any]],
    ) -> None:
        """Detect workspace overlap among active runs and emit change events."""

        participants = {
            run_id: str(info.get("owner", {}).get("workspace", ""))
            for run_id, info in active.items()
        }
        # Drop runs with no workspace; they cannot contend.
        workspaces = [
            workspace for workspace in participants.values() if workspace
        ]
        identities = resolve_many(workspaces, budget_seconds=3.0)
        active_identities: dict[str, Any] = {}
        for run_id, workspace in participants.items():
            if not workspace:
                continue
            identity = identities.get(workspace)
            if identity is None:
                identity = identities.get(os.path.abspath(workspace))
            if identity is not None:
                active_identities[run_id] = identity

        groups = detect_contention(
            active_identities,
            min_emit_severity=self._min_emit_severity,
        )
        next_contentions: dict[str, ContentionGroup] = {
            group.contention_id: group for group in groups
        }
        previous_ids = set(self._contentions)
        next_ids = set(next_contentions)
        cleared = previous_ids - next_ids
        detected = next_ids - previous_ids

        for contention_id in cleared:
            group = self._contentions[contention_id]
            self._emit_contention(group, "fleet.contention.cleared")
        for contention_id in detected:
            group = next_contentions[contention_id]
            self._emit_contention(group, "fleet.contention.detected")

        self._contentions = next_contentions
        self._persist_contention(groups)

    def _persist_contention(self, groups: list[ContentionGroup]) -> None:
        """Write the current contention picture atomically for the read path."""

        runs_root = getattr(self.supervisor, "runs_root", None)
        if runs_root is None:
            return
        path = Path(runs_root) / "queue" / "contention.json"
        payload = json.dumps(
            {
                "ok": True,
                "available": True,
                "contentions": groups_to_json(groups),
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        self._atomic_write(path, payload.encode("utf-8"))

    def _emit_contention(self, group: ContentionGroup, event_type: str) -> None:
        """Emit one run event per member plus a service event."""

        for member in group.members:
            self._emit_run_event(
                member.run_id,
                event_type,
                {
                    "contention_id": group.contention_id,
                    "severity": group.severity,
                    "group_key": group.group_key,
                    "peers": [
                        {
                            "run_id": peer.run_id,
                            "workspace": peer.workspace,
                            "branch": peer.branch,
                        }
                        for peer in group.members
                        if peer.run_id != member.run_id
                    ],
                },
            )
        self._emit_service_event(
            event_type,
            {
                "contention_id": group.contention_id,
                "severity": group.severity,
                "group_key": group.group_key,
                "members": [
                    {"run_id": member.run_id, "workspace": member.workspace, "branch": member.branch}
                    for member in group.members
                ],
                "truncated": group.truncated,
            },
        )

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        from .supervisor.control_bus import _atomic_bytes_write

        try:
            _atomic_bytes_write(path, payload)
        except OSError:
            pass

    def _remaining_capacity(self, active: dict[str, dict[str, Any]]) -> dict[str, int]:
        capacity = self._config.get("capacity", {})
        trios = self._config.get("trios", {})
        counts: dict[str, int] = {name: 0 for name in trios}
        for entry in self.queue_store.list():
            if entry.status == "launched" and entry.run_id and entry.run_id in active:
                counts[entry.trio] = counts.get(entry.trio, 0) + 1
        return {
            name: max(0, int(capacity.get(f"{name}_max", 0)) - counts[name])
            for name in trios
        }

    def _active_runs(self) -> dict[str, dict[str, Any]]:
        """Return active runs keyed by run_id with owner and status."""

        result: dict[str, dict[str, Any]] = {}
        for item in self.supervisor.list_run_items():
            run_id = str(item.get("id") or "")
            if not run_id:
                continue
            status = self.supervisor.status(run_id)
            lifecycle = canonical_lifecycle_status(status.get("status"))
            if lifecycle in ACTIVE_STATUSES:
                result[run_id] = {"status": status, "owner": self.supervisor.owner(run_id)}
        return result

    def _launch(self, entry: QueueEntry) -> None:
        trio = self._config.get("trios", {}).get(entry.trio)
        if trio is None:
            self._skip(entry, f"unknown trio {entry.trio}")
            return
        agent = trio.get("agent", "codex")
        model = trio.get("model")
        mcp_profile = trio.get("mcp_profile")
        role_configs = {
            role: {"agent": agent, "model": model, "mcp_profile": mcp_profile}
            for role in ("manager", "executor", "auditor")
        }
        run_id: str | None = None
        try:
            created = self.supervisor.create_run(
                task=entry.task,
                agent=agent,
                model=model,
                role_configs=role_configs,
                workspace=entry.workspace,
                max_rounds=entry.max_rounds,
                prompt_language="en",
                mcp_profile=mcp_profile,
                base_check=entry.base_check or None,
            )
            run_id = str(created.get("id") or "")
        except Exception as exc:
            self.queue_store.mark_failed(entry.queue_id, f"launch failed: {exc}")
            self._emit_service_event(
                "queue.skipped",
                {
                    "queue_id": entry.queue_id,
                    "reason": f"launch failed: {exc}",
                },
            )
            return
        if not run_id:
            self.queue_store.mark_failed(entry.queue_id, "launch returned no run id")
            return
        launched = self.queue_store.mark_launched(entry.queue_id, run_id)
        self._emit_run_event(
            run_id,
            "queue.launched",
            {
                "queue_id": entry.queue_id,
                "run_id": run_id,
                "trio": entry.trio,
                "workspace": entry.workspace,
                "requested_by": entry.requested_by,
            },
        )
        if launched is not None:
            launched.last_checked_at = _now()
            self.queue_store.update(launched)

    def _skip(self, entry: QueueEntry, reason: str) -> None:
        updated = self.queue_store.record_skip(entry.queue_id, reason)
        self._emit_service_event(
            "queue.skipped",
            {
                "queue_id": entry.queue_id,
                "trio": entry.trio,
                "workspace": entry.workspace,
                "reason": reason,
            },
        )
        if updated is not None:
            updated.last_checked_at = _now()
            self.queue_store.update(updated)

    def _run_role_dir(self, run_id: str) -> Path | None:
        try:
            logs = self.supervisor._run_logs_dir(run_id)
        except Exception:
            return None
        return logs / "role_orchestration"

    def _emit_run_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        role_dir = self._run_role_dir(run_id)
        if role_dir is None:
            return
        record = {
            "schema_version": 2,
            "event_id": f"{run_id}:queue-{_now():.6f}",
            "type": event_type,
            "ts": _now(),
            "run_id": run_id,
            "payload": payload,
        }
        self._append_jsonl(role_dir / "events.jsonl", record)

    def _emit_service_event(self, event_type: str, payload: dict[str, Any]) -> None:
        record = {
            "schema_version": 2,
            "event_id": f"queue-{_now():.6f}",
            "type": event_type,
            "ts": _now(),
            "payload": payload,
        }
        self._append_jsonl(self.queue_store._root / "service_events.jsonl", record)

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
        except OSError:
            pass
