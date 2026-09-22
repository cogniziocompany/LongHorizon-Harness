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
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .config import PROJECT_CONFIG_PATH, load_run_defaults
from .contention import ContentionGroup, detect_contention, groups_to_json
from .queue import QueueEntry, QueueStore, default_queue_config, queue_config_from_config
from .supervisor.lifecycle import ACTIVE_STATUSES, canonical_lifecycle_status
from .workspace_guard import WorkspaceBaseError, prepare_workspace_base, probe_open_pr_gh
from .workspace_identity import resolve_many

# ``httpx`` is already a transitive dependency of FastAPI/TestClient, but the
# launcher must not fail to import when it is absent.
try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore


_MAX_REASON_LEN = 4_000

# Failure causes that must never trigger a requeue.  Matched against the
# launch-failure exception text and the terminal run report's
# abort_reason/failure_reason.  Checked BEFORE the retryable patterns: a
# message that contains both (e.g. a transport error wrapping a validation
# sentence) stays non-retryable because the task itself is broken.
_NON_RETRYABLE_CAUSE_SIGNATURES = (
    # invalid task
    "task must be a string",
    "task is required",
    "task is too large",
    "contains a nul byte",
    # workspace missing / outside the boundary
    "invalid workspace path",
    "workspace must be inside",
    "workspace does not exist",
    # structural create_run rejections
    "agent must be",
    "max_rounds must be",
    "prompt_language must be",
    "model must be",
    "does not accept a reasoning effort",
    "run already exists",
    "cannot create runs",
    "idempotency-key",
    "request is already being created",
    # exhausted / human stop
    "max_retries",
    "exceeded max_retries",
    "max_rounds_exhausted",
    "needs_human_input",
    "user_cancelled",
    "manager_blocked",
    "worker_cancelled",
)

# Retryable causes, in the order they are tried: provider rate limiting,
# episode timeouts (executor/auditor), stalled-episode detection, and
# transport-class launch failures.
_RETRYABLE_CAUSE_SIGNATURES = (
    "provider_rate_limit",
    "429",
    "rate limit",
    "rate_limit",
    "too many requests",
    "provider_timeout",
    "timed out",
    "timeout",
    "provider_stall",
    "stalled_execution",
    "no_output_stall",
    "stalled episode",
)


def _classify_failure_cause(text: str) -> str | None:
    """Return a canonical retryable cause label for a failure, or None.

    ``None`` means the cause is explicitly non-retryable (invalid task,
    workspace missing, max_retries exhausted, human stop) or unclassifiable;
    the launcher must then let the entry stay failed instead of requeueing.
    """

    lowered = (text or "").lower()
    if not lowered:
        return None
    if any(sig in lowered for sig in _NON_RETRYABLE_CAUSE_SIGNATURES):
        return None
    for sig in _RETRYABLE_CAUSE_SIGNATURES:
        if sig in lowered:
            return "provider_rate_limit" if "rate" in sig or sig == "429" else (
                "provider_stall" if "stall" in sig or "no_output" in sig else "episode_timeout"
            )
    return None


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


# Occupancy probes (task 173, scope 4 / migration doc workspace-collision
# finding, 2026-09-14): an active run is not the only reason a workspace is
# unsafe to launch into.  A dirty tree means another task's uncommitted work
# sits in it, and a local branch carrying commits that never reached
# origin/main means another task's committed work does.  Both are OCCUPIED.
_GIT_OCCUPANCY_TIMEOUT = 30


def _git_occupancy(repo: Path, *args: str) -> str | None:
    """Run one read-only git probe in ``repo``; None on any failure."""

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_OCCUPANCY_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _dirty_workspace_occupied(workspace: str) -> bool:
    """True when ``git status --porcelain`` in the workspace is non-empty.

    Any failure (not a repo, git unavailable, timeout) reads as clean — the
    active-run rule and the workspace guard still protect the launch; this
    probe only ever adds a skip, never manufactures one out of an error.
    """

    repo = Path(workspace)
    if not repo.is_dir():
        return False
    status = _git_occupancy(repo, "status", "--porcelain")
    return bool(status)


def _unpushed_branch_occupied(workspace: str) -> bool:
    """True when the checked-out branch has no upstream AND carries commits
    not on origin's default branch (origin/main in the task's wording).

    The 2026-09-14 collisions were all "a run started on top of another task's
    unpushed branch work".  A branch with an upstream is not occupied by this
    rule even when it is ahead of that upstream — the open-PR probe and the
    ahead-of-upstream relocation in ``prepare_workspace_base`` cover those at
    launch time.  Anything unreadable reads as clean: this probe only ever
    adds a skip, never manufactures one out of an error.
    """

    repo = Path(workspace)
    if not repo.is_dir():
        return False
    if _git_occupancy(repo, "rev-parse", "--is-inside-work-tree") != "true":
        return False
    branch = _git_occupancy(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch == "HEAD":
        # Detached HEAD: no branch work to protect beyond the dirty-tree rule.
        return False
    upstream = _git_occupancy(repo, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}")
    if upstream:
        return False
    origin_main = _git_occupancy(
        repo, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main"
    )
    if not origin_main:
        # No origin/main ref: commits-not-on-origin/main is undecidable here
        # (no remote, or an unusual remote layout); the launch-time guard still
        # runs, so this stays fail-open rather than blocking every workspace.
        return False
    count = _git_occupancy(repo, "rev-list", "--count", f"refs/remotes/origin/main..{branch}")
    return bool(count and count != "0")


class Launcher:
    """Poll the queue and launch eligible entries through the supervisor."""

    def __init__(
        self,
        supervisor: Any,
        queue_store: QueueStore,
        *,
        poll_seconds: float = 15.0,
        queue_config: dict[str, Any] | None = None,
        probe_open_pr: "Callable[[Path, str], str | None] | None" = probe_open_pr_gh,
        min_emit_severity: str = "same_repo",
    ) -> None:
        self.supervisor = supervisor
        self.queue_store = queue_store
        self._config = queue_config or self._load_project_queue_config()
        # Observe (shadow) mode — task 173 / migration §5 Step 2: when true the
        # launcher computes the full launch decision but starts NOTHING; it
        # appends one shadow record per decision to
        # runs_root/queue/shadow.jsonl and leaves every entry pending. The PC
        # launcher stays the sole authoritative launcher while this is on.
        self._observe = bool(self._config.get("observe", False))
        self._poll_seconds = float(
            self._config.get("capacity", {}).get("poll_seconds", poll_seconds)
        )
        self._task: asyncio.Task | None = None
        self._stopping = False
        # Workspace branch guard hook: returns a description of an OPEN PR on
        # the given branch, or None.  DEFAULT-ON: production launches probe
        # origin for a colliding OPEN PR with no flag or config (deliverable 3
        # must hold in the default configuration).  Pass ``probe_open_pr=None``
        # only to disable the probe explicitly (tests and offline runs).
        self.probe_open_pr = probe_open_pr
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
                if self._observe:
                    self._shadow_skip(entry, skip_reason)
                    continue
                self._skip(entry, skip_reason)
                continue
            skip_reason = self._check_eligibility(entry, active, capacities)
            if skip_reason is None:
                if self._observe:
                    # Observe (shadow) mode, task 173 / migration §5 Step 2:
                    # the full decision (eligibility, capacity, key health and
                    # now the trio resolve) is computed, then — immediately
                    # before the launch point, never inside the eligibility
                    # gate — the pass short-circuits and records what WOULD
                    # have happened.  The entry stays pending; create_run is
                    # never called.  The batch bookkeeping still runs so later
                    # entries see exactly the skip reasons a real launch would
                    # have produced.
                    self._shadow_launch_decision(entry)
                    launched = True
                    capacities[entry.trio] = capacities.get(entry.trio, 0) - 1
                    continue
                self._launch(entry)
                launched = True
                capacities[entry.trio] = capacities.get(entry.trio, 0) - 1
            elif self._observe:
                self._shadow_skip(entry, skip_reason)
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
                cause = self._reconcile_failure_cause(run_id, f"run {run_status}")
                updated = self.queue_store.mark_failed(
                    entry.queue_id, reason=cause[:_MAX_REASON_LEN]
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
                # Retry only genuinely retryable backend faults; human stops,
                # max_rounds and invalid tasks stay failed with no successor.
                if updated is not None and self._is_retryable_cause(cause):
                    self._handle_retry(updated, cause)
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
        # Occupancy (task 173, scope 4): beyond an active run, a workspace is
        # OCCUPIED when another task's uncommitted work sits in it (dirty
        # tree) or when the checked-out branch carries unpushed work with no
        # upstream to push it to.  Each skip reason names which condition
        # fired.  ``occupancy_ignore_dirty`` (per-environment overseer
        # override) disables only these two probes; the active-run rule above
        # always applies.  A continuation entry (task 201 ``branch`` /
        # ``continue_branch``) explicitly owns the workspace it names, dirty
        # branch included, so the two dirty-work probes do not apply to it.
        if not bool(self._config.get("occupancy_ignore_dirty", False)) and not bool(
            getattr(entry, "branch", "") or getattr(entry, "continue_branch", False)
        ):
            if _dirty_workspace_occupied(entry.workspace):
                return (
                    f"workspace {entry.workspace} occupied: dirty tree "
                    "(git status --porcelain non-empty)"
                )
            if _unpushed_branch_occupied(entry.workspace):
                return (
                    f"workspace {entry.workspace} occupied: checked-out branch "
                    "has no upstream and carries commits not on origin/main"
                )
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
        # Workspace branch guard: never launch onto another task's branch
        # (measured defect, 2026-09-16: runs 7784478f under PR #108 and
        # 96563c4c under PR #154).  A non-default checked-out branch must not
        # be used as-is; the run gets a base cut fresh from origin's default,
        # and another task's uncommitted/unpushed work is never destroyed.
        #
        # Continuation opt-in (task 201): an entry with ``branch`` or
        # ``continue_branch`` set is a continuation task that owns the branch
        # it names or finds checked out; the guard honours that with mode
        # "continuation" — no relocation, no stash, no open-PR refusal.  The
        # default (neither set) keeps the guard's full protection.
        continuation = bool(getattr(entry, "branch", "") or getattr(entry, "continue_branch", False))
        try:
            base = prepare_workspace_base(
                entry.workspace,
                run_label=f"{entry.trio}-{uuid.uuid4().hex[:8]}",
                base_root=getattr(self.supervisor, "workspace_root", None),
                probe_open_pr=self.probe_open_pr,
                continuation=continuation,
                requested_branch=getattr(entry, "branch", "") or "",
            )
        except WorkspaceBaseError as exc:
            reason = str(exc)[:_MAX_REASON_LEN]
            # A guard refusal is retryable, not terminal (task 201): a dirty
            # tree or an OPEN PR on the branch is often transient (the other
            # task merges, the tree is cleaned), so the entry stays pending
            # with the reason recorded in skip_reasons and is re-attempted on
            # the next poll instead of becoming a dead row needing hand
            # requeue.  The same choice as the other skip paths in this
            # launcher; nothing else about the entry changes.
            updated = self.queue_store.record_skip(entry.queue_id, f"workspace base refused: {reason}")
            self._emit_service_event(
                "queue.skipped",
                {
                    "queue_id": entry.queue_id,
                    "trio": entry.trio,
                    "workspace": entry.workspace,
                    "reason": f"workspace base refused: {reason}",
                },
            )
            if updated is not None:
                updated.last_checked_at = _now()
                self.queue_store.update(updated)
            return
        workspace = str(base.workspace if base.mode == "worktree" else entry.workspace)
        try:
            created = self.supervisor.create_run(
                task=entry.task,
                agent=agent,
                model=model,
                role_configs=role_configs,
                workspace=workspace,
                max_rounds=entry.max_rounds,
                prompt_language="en",
                mcp_profile=mcp_profile,
                base_check=entry.base_check or None,
                workspace_base_mode=base.mode,
                workspace_base_summary=base.summary(),
            )
            run_id = str(created.get("id") or "")
        except Exception as exc:
            failure_reason = f"launch failed: {exc}"
            updated = self.queue_store.mark_failed(entry.queue_id, failure_reason)
            self._emit_service_event(
                "queue.skipped",
                {
                    "queue_id": entry.queue_id,
                    "reason": failure_reason,
                },
            )
            # Only transport-class launch failures requeue; a create_run
            # validation error (invalid task, workspace outside the root)
            # would reproduce identically on a successor, so it stays failed.
            if updated is not None and self._is_retryable_cause(failure_reason):
                self._handle_retry(updated, failure_reason)
            return
        if not run_id:
            failure_reason = "launch returned no run id"
            updated = self.queue_store.mark_failed(entry.queue_id, failure_reason)
            # Handle retry for retryable causes
            if updated is not None and self._is_retryable_cause(failure_reason):
                self._handle_retry(updated, failure_reason)
            return
        launched = self.queue_store.mark_launched(entry.queue_id, run_id)
        self._emit_run_event(
            run_id,
            "queue.launched",
            {
                "queue_id": entry.queue_id,
                "run_id": run_id,
                "trio": entry.trio,
                "workspace": workspace,
                "requested_by": entry.requested_by,
                "workspace_base": base.summary(),
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

    # ------------------------------------------------------------------
    # Observe (shadow) mode — task 173 / migration §5 Step 2.
    #
    # Shadow decisions are recorded BOTH as durable service events (the fleet
    # event stream) AND as one JSON line each in runs_root/queue/shadow.jsonl
    # (the comparison stream scripts/compare_shadow.py reads).  The entry
    # stays pending in every path; no run is created and no capacity is
    # permanently consumed beyond the in-pass batch bookkeeping.
    # ------------------------------------------------------------------

    def _shadow_launch_decision(self, entry: QueueEntry) -> None:
        """Record a would-launch decision without calling create_run.

        Resolves the trio exactly as ``_launch`` would (so an unknown trio is
        a shadow skip, not a crash), then emits ``queue.shadow_launch`` with
        the would-be launch fields.  The entry is left pending.
        """

        trio = self._config.get("trios", {}).get(entry.trio)
        if trio is None:
            self._shadow_skip(entry, f"unknown trio {entry.trio}")
            return
        agent = trio.get("agent", "codex")
        model = trio.get("model")
        mcp_profile = trio.get("mcp_profile")
        roles = {
            role: {"agent": agent, "model": model, "mcp_profile": mcp_profile}
            for role in ("manager", "executor", "auditor")
        }
        self._shadow_record(
            entry,
            "queue.shadow_launch",
            trio=entry.trio,
            roles=roles,
            would_run_at=_now(),
        )

    def _shadow_skip(self, entry: QueueEntry, reason: str) -> None:
        self._shadow_record(entry, "queue.shadow_skip", reason=reason)

    def _shadow_record(
        self,
        entry: QueueEntry,
        event_type: str,
        *,
        reason: str | None = None,
        trio: str | None = None,
        roles: dict[str, Any] | None = None,
        would_run_at: float | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "queue_id": entry.queue_id,
            "trio": trio if trio is not None else entry.trio,
            "workspace": entry.workspace,
        }
        if roles is not None:
            payload["roles"] = roles
        if reason is not None:
            payload["reason"] = reason
        if would_run_at is not None:
            payload["would_run_at"] = would_run_at
        self._emit_service_event(event_type, payload)
        self._append_shadow_log(event_type, payload)

    def _append_shadow_log(self, event_type: str, payload: dict[str, Any]) -> None:
        """Append one shadow decision line to runs_root/queue/shadow.jsonl."""

        append = getattr(self.queue_store, "append_shadow_record", None)
        if append is None:
            # A store without the shadow log (e.g. the PG backend before its
            # mirror lands) still emits the service event; nothing is lost
            # from the fleet event stream.
            return
        append(
            {
                "schema_version": 2,
                "type": event_type,
                "ts": _now(),
                "payload": payload,
            }
        )

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
        # A store without a file root (the PG backend) has no service event
        # log to append to; the launcher stays read-only there rather than
        # aborting the pass.
        root = getattr(self.queue_store, "_root", None)
        if root is None:
            return
        record = {
            "schema_version": 2,
            "event_id": f"queue-{_now():.6f}",
            "type": event_type,
            "ts": _now(),
            "payload": payload,
        }
        self._append_jsonl(Path(root) / "service_events.jsonl", record)

    def _is_retryable_cause(self, cause: str) -> bool:
        """True when a failure cause should spawn a retry (successor entry).

        Retryable: provider rate limiting (429/rate limit), executor/auditor
        episode timeouts, stalled-episode detection (provider_stall), and
        "launch failed" transport errors.  NOT retryable: invalid task,
        workspace missing, max_retries exhausted, human stop -- anything the
        requeue would just repeat forever.  `_classify_failure_cause` checks
        the non-retryable signatures first, so a transport message that merely
        wraps a validation error stays failed.
        """
        return _classify_failure_cause(cause) is not None

    def _reconcile_failure_cause(self, run_id: str, reason: str) -> str:
        """Build the cause text for a terminated run from its durable report.

        The report's abort_reason/failure_reason carry the actionable cause
        (provider_rate_limit, provider_stall, episode timeout); the plain
        "run <status>" text alone cannot distinguish a retryable backend
        fault from a human stop.
        """

        report = self._read_run_report(run_id)
        parts = [str(report.get("abort_reason") or "").strip()]
        parts.append(str(report.get("failure_reason") or "").strip())
        signals = report.get("runtime_signals")
        if isinstance(signals, list):
            for item in signals:
                if isinstance(item, dict):
                    label = str(item.get("signal") or "").strip()
                    if label:
                        parts.append(label)
        parts.append(reason)
        return " | ".join(part for part in parts if part)

    def _handle_retry(self, failed_entry: QueueEntry, cause: str) -> None:
        """Create a successor entry for a retryably-failed entry.

        The successor inherits the original's task/workspace/trio/priority and
        attempt+1, with dedup_key cleared (a retry must not collide with the
        original's dedup id).  A refused requeue (cap reached) is logged as a
        service event, never raised into the launcher tick.
        """
        try:
            successor = self.queue_store.requeue(failed_entry.queue_id, cause)
            if successor is not None:
                # Emit queue.requeued event
                self._emit_service_event(
                    "queue.requeued",
                    {
                        "original": failed_entry.queue_id,
                        "successor": successor.queue_id,
                        "cause": cause,
                        "attempt": successor.attempt,
                    },
                )
        except ValueError as exc:
            # Log but don't fail the launcher tick
            self._emit_service_event(
                "queue.requeue_error",
                {
                    "queue_id": failed_entry.queue_id,
                    "error": str(exc)[:200],
                },
            )

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
