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
import time
from pathlib import Path
from typing import Any

from .config import PROJECT_CONFIG_PATH, load_run_defaults
from .queue import QueueEntry, QueueStore, default_queue_config, queue_config_from_config
from .supervisor.lifecycle import ACTIVE_STATUSES, canonical_lifecycle_status

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

    return os.path.normpath(os.path.abspath(str(value or "")))


class Launcher:
    """Poll the queue and launch eligible entries through the supervisor."""

    def __init__(
        self,
        supervisor: Any,
        queue_store: QueueStore,
        *,
        poll_seconds: float = 15.0,
        queue_config: dict[str, Any] | None = None,
    ) -> None:
        self.supervisor = supervisor
        self.queue_store = queue_store
        self._config = queue_config or self._load_project_queue_config()
        self._poll_seconds = float(
            self._config.get("capacity", {}).get("poll_seconds", poll_seconds)
        )
        self._task: asyncio.Task | None = None
        self._stopping = False

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
            self._run_pass()
        except Exception as exc:
            self._emit_service_event("queue.error", {"error": str(exc)[:200]})

    def _run_pass(self) -> None:
        entries = self.queue_store.list()
        active = self._active_runs()
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
