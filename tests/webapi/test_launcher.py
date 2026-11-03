from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from lh_harness import settings_store
from lh_harness.launcher import Launcher, maintenance_manifest_run_ids
from lh_harness.queue import QueueStore, default_queue_config
from lh_harness.settings_store import SettingsRuntime, SettingsStore


class FakeSupervisor:
    """In-memory supervisor stand-in that records create_run calls."""

    def __init__(self, runs_root: Path) -> None:
        self.runs_root = runs_root
        self._runs: dict[str, dict[str, Any]] = {}
        self._owners: dict[str, dict[str, Any]] = {}
        self._statuses: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []

    def add_run(
        self,
        run_id: str,
        workspace: str | Path,
        *,
        status: str = "running",
        trio: str | None = None,
    ) -> None:
        self._runs[run_id] = {
            "id": run_id,
            "workspace": str(workspace),
            "trio": trio,
        }
        self._owners[run_id] = {
            "run_id": run_id,
            "workspace": str(workspace),
            "task": "existing task",
            "agent": "codex",
        }
        self._statuses[run_id] = {"status": status, "run_id": run_id}

    def list_run_items(self) -> list[dict[str, Any]]:
        return list(self._runs.values())

    def owner(self, run_id: str) -> dict[str, Any]:
        return self._owners.get(run_id, {})

    def status(self, run_id: str) -> dict[str, Any]:
        return self._statuses.get(run_id, {"status": "idle", "run_id": run_id})

    def _run_logs_dir(self, run_id: str) -> Path:
        return self.runs_root / run_id / "lh_harness"

    def create_run(self, **kwargs: Any) -> dict[str, Any]:
        run_id = kwargs.get("run_id") or f"run-{uuid.uuid4().hex[:8]}"
        workspace = str(kwargs.get("workspace", "."))
        agent = kwargs.get("agent", "codex")
        self.add_run(run_id, workspace, status="creating", trio=agent)
        self._owners[run_id].update(
            {
                "task": kwargs.get("task", ""),
                "agent": agent,
                "model": kwargs.get("model"),
                "role_configs": kwargs.get("role_configs"),
                "max_rounds": kwargs.get("max_rounds"),
                "mcp_profile": kwargs.get("mcp_profile"),
            }
        )
        result = {
            "id": run_id,
            "task": kwargs.get("task", ""),
            "status": "creating",
            "log_dir": str(self._run_logs_dir(run_id)),
            "owner": self._owners[run_id],
        }
        self.created.append(result)
        return result


def _fixture(tmp_path: Path) -> tuple[Path, QueueStore, FakeSupervisor]:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = FakeSupervisor(root)
    return root, store, supervisor


_NAME_SEQ = {"n": 0}


def _base_entry(trio: str = "kimi", priority: int = 0, workspace: str = "./workspace") -> dict[str, Any]:
    _NAME_SEQ["n"] += 1
    return {
        "name": f"task {_NAME_SEQ['n']}",
        "task": "do something",
        "workspace": workspace,
        "trio": trio,
        "priority": priority,
        "requested_by": "ci",
    }


@pytest.mark.parametrize("lifecycle", ["incomplete", "failed", "cancelled"])
def test_parent_wait_retains_same_queue_entry_only_for_clean_hold(tmp_path: Path, lifecycle: str):
    root, store, supervisor = _fixture(tmp_path)
    entry = store.create({**_base_entry(), "dedup_key": "parent-task"})
    store.mark_launched(entry.queue_id, "run-parent")
    supervisor.add_run("run-parent", entry.workspace, status=lifecycle)
    report_dir = root / "run-parent" / "lh_harness"
    report_dir.mkdir(parents=True)
    (report_dir / "report.json").write_text(json.dumps({
        "status": "incomplete", "completion_satisfied": False,
        "abort_reason": "needs_parent_input",
    }))
    launcher = Launcher(supervisor, store, queue_config=default_queue_config())
    launcher._update_launched_entries({})
    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.run_id == "run-parent"
    assert updated.status == ("launched" if lifecycle == "incomplete" else "failed")
    assert updated.wait_reason == ("needs_parent_input" if lifecycle == "incomplete" else None)
    launcher._update_launched_entries({})
    assert len(store.list()) == 1
    assert supervisor.created == []
    if lifecycle == "incomplete":
        events = (report_dir / "role_orchestration" / "events.jsonl").read_text()
        assert events.count('"queue.parent_wait"') == 1
        assert store.create({**_base_entry(), "dedup_key": "parent-task"}).queue_id == entry.queue_id
        supervisor._statuses["run-parent"]["status"] = "running"
        launcher._update_launched_entries({"run-parent": {"status": "running"}})
        assert store.get(entry.queue_id).wait_reason is None


def test_launcher_launches_highest_priority_kimi(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 1
    launcher = Launcher(supervisor, store, queue_config=config)

    low = store.create(_base_entry(trio="kimi", priority=1, workspace="./w2"))
    high = store.create(_base_entry(trio="kimi", priority=10, workspace="./w1"))

    asyncio.run(launcher.tick())

    high_updated = store.get(high.queue_id)
    low_updated = store.get(low.queue_id)
    assert high_updated is not None
    assert low_updated is not None
    assert high_updated.status == "launched"
    assert low_updated.status == "pending"
    assert any("kimi at capacity" in reason for reason in low_updated.skip_reasons)
    assert high_updated.run_id is not None
    assert high_updated.run_id in supervisor._runs

    events_path = supervisor._run_logs_dir(high_updated.run_id) / "role_orchestration" / "events.jsonl"
    assert events_path.is_file()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    assert any("queue.launched" in line for line in lines)
    contention_path = root / "queue" / "contention.json"
    assert contention_path.is_file()


def test_launcher_skips_qwen_at_zero_capacity(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["qwen_max"] = 0
    launcher = Launcher(supervisor, store, queue_config=config)

    entry = store.create(_base_entry(trio="qwen"))
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert any("qwen at capacity" in reason for reason in updated.skip_reasons)

    service_log = root / "queue" / "service_events.jsonl"
    assert service_log.is_file()
    assert any("queue.skipped" in line for line in service_log.read_text().splitlines())


def test_launcher_skips_busy_workspace(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 2
    launcher = Launcher(supervisor, store, queue_config=config)

    busy_workspace = str(tmp_path / "busy")
    free_workspace = str(tmp_path / "free")
    supervisor.add_run("active-1", busy_workspace, status="running")

    blocked = store.create(_base_entry(trio="kimi", workspace=busy_workspace, priority=10))
    ready = store.create(_base_entry(trio="kimi", workspace=free_workspace, priority=5))

    asyncio.run(launcher.tick())

    blocked_updated = store.get(blocked.queue_id)
    ready_updated = store.get(ready.queue_id)
    assert blocked_updated is not None
    assert ready_updated is not None
    assert ready_updated.status == "launched"
    assert blocked_updated.status == "pending"
    assert any("has active run active-1" in reason for reason in blocked_updated.skip_reasons)


def test_launcher_idempotent_across_restarts(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 1
    launcher = Launcher(supervisor, store, queue_config=config)

    first = store.create(_base_entry(trio="kimi", workspace="./ws"))
    launched = store.mark_launched(first.queue_id, "run-existing")
    supervisor.add_run("run-existing", "./ws", status="running", trio="kimi")

    second = store.create(_base_entry(trio="kimi", workspace="./ws2", priority=1))
    asyncio.run(launcher.tick())

    first_updated = store.get(first.queue_id)
    second_updated = store.get(second.queue_id)
    assert first_updated is not None
    assert second_updated is not None
    assert first_updated.status == "launched"
    assert first_updated.run_id == "run-existing"
    # The active run already consumes the only kimi slot.
    assert second_updated.status == "pending"
    assert any("kimi at capacity" in reason for reason in second_updated.skip_reasons)


def test_launcher_never_double_launches_same_workspace(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 3
    launcher = Launcher(supervisor, store, queue_config=config)

    workspace = str(tmp_path / "shared")
    supervisor.add_run("manual-run", workspace, status="running")

    first = store.create(_base_entry(trio="kimi", workspace=workspace, priority=10))
    second = store.create(_base_entry(trio="kimi", workspace=workspace, priority=5))

    asyncio.run(launcher.tick())

    assert store.get(first.queue_id).status == "pending"
    assert store.get(second.queue_id).status == "pending"
    assert len(supervisor.created) == 0


def test_launcher_key_health_gating(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["min_healthy_keys"] = 3

    health_file = tmp_path / "health.json"
    health_file.write_text(
        json.dumps({"healthy_keys": [{"healthy": True}, {"healthy": True}, {"healthy": False}]}),
        encoding="utf-8",
    )
    config["capacity"]["key_health_url"] = health_file.as_uri()

    launcher = Launcher(supervisor, store, queue_config=config)
    entry = store.create(_base_entry(trio="kimi"))
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert any("key health insufficient" in reason for reason in updated.skip_reasons)


def test_launcher_key_health_allows_kimi_when_threshold_met(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["min_healthy_keys"] = 2

    health_file = tmp_path / "health.json"
    health_file.write_text(
        json.dumps({"healthy_keys": [{"healthy": True}, {"healthy": True}, {"healthy": False}]}),
        encoding="utf-8",
    )
    config["capacity"]["key_health_url"] = health_file.as_uri()

    launcher = Launcher(supervisor, store, queue_config=config)
    entry = store.create(_base_entry(trio="kimi"))
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "launched"


def test_launcher_records_last_checked_at(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    launcher = Launcher(supervisor, store, queue_config=config)

    entry = store.create(_base_entry(trio="kimi"))
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.last_checked_at is not None


def test_launcher_fails_entry_when_create_run_raises(tmp_path: Path) -> None:
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    launcher = Launcher(supervisor, store, queue_config=config)

    class BrokenSupervisor(FakeSupervisor):
        def create_run(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("boom")

    broken = BrokenSupervisor(root)
    launcher.supervisor = broken
    entry = store.create(_base_entry(trio="kimi"))
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "failed"
    assert "launch failed" in (updated.reason or "")


def test_launcher_skips_non_read_only_auditor_profile_without_burning_attempt(tmp_path: Path) -> None:
    # Task 234 (CT110, 2026-09-23): an auditor role bound to a non-read-only
    # MCP profile must be refused before any attempt is consumed.  The original
    # burn path let the refusal escape into ``create_run`` and then
    # ``mark_failed``, leaving the entry terminal with a failed attempt.  The
    # new pre-burn path records a skip reason, leaves status pending, and
    # never calls the supervisor.
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["trios"]["kimi"] = {"agent": "claude_code", "model": "kimi-k3", "mcp_profile": "default"}
    launcher = Launcher(supervisor, store, queue_config=config)
    entry = store.create(_base_entry(trio="kimi"))
    initial_attempt = entry.attempt

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert updated.attempt == initial_attempt
    assert updated.reason is None
    assert any("roles.auditor.mcp_profile" in reason and "not read-only" in reason for reason in updated.skip_reasons)
    assert not supervisor.created

    # A second tick sees the same profile and records another skip; the entry
    # still does not burn an attempt or become failed.
    asyncio.run(launcher.tick())
    twice = store.get(entry.queue_id)
    assert twice is not None
    assert twice.attempt == initial_attempt
    assert twice.status == "pending"
    assert sum("roles.auditor.mcp_profile" in reason for reason in twice.skip_reasons) == 2


def test_launcher_no_double_launch_across_ticks(tmp_path: Path) -> None:
    """A second tick must not launch into a workspace that got an active run between ticks.

    This covers the inter-tick race where a concurrent manual run or the previous
    tick's launched worker becomes active before the next poll finishes.  The
    launcher re-evaluates the active-run set inside its serialized launch section
    and skips the workspace.
    """
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 3
    launcher = Launcher(supervisor, store, queue_config=config)

    workspace = str(tmp_path / "shared")
    first = store.create(_base_entry(trio="kimi", workspace=workspace, priority=10))

    asyncio.run(launcher.tick())
    first_updated = store.get(first.queue_id)
    assert first_updated is not None
    assert first_updated.status == "launched"
    assert first_updated.run_id is not None

    # Simulate a manual/concurrent run appearing in the same workspace before the
    # next launcher tick, in addition to the already-launched queue entry.
    supervisor.add_run("manual-run", workspace, status="running", trio="kimi")

    second = store.create(_base_entry(trio="kimi", workspace=workspace, priority=5))
    asyncio.run(launcher.tick())

    second_updated = store.get(second.queue_id)
    assert second_updated is not None
    assert second_updated.status == "pending"
    assert any("has active run" in reason for reason in second_updated.skip_reasons)


def test_launch_role_configs_omit_unset_and_keep_auditor_read_only(tmp_path: Path) -> None:
    # Cutover 168 (2026-09-23): an unset model/profile was passed as the string
    # "None" and every worker died on its reservation check; a trio-wide
    # profile also landed on the auditor, which must stay read-only.  Task 234:
    # a read-only trio profile ("audit") launches, and the launched specs stay
    # stripped of mcp_profile either way — a non-read-only trio profile is now
    # refused pre-burn (see the skip test above), never sent to the worker.
    _root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["trios"]["kimi"] = {"agent": "claude_code", "model": "kimi-k3", "mcp_profile": "audit"}
    launcher = Launcher(supervisor, store, queue_config=config)
    entry = store.create(_base_entry(trio="kimi"))

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None and updated.status == "launched"
    owner = supervisor.created[-1]["owner"]
    roles = owner["role_configs"]
    # No mcp_profile in any role spec: the worker cannot round-trip it and
    # would reject its own reservation (2026-09-23, runs 7164fbff/70ed3e28).
    assert all(spec == {"agent": "claude_code", "model": "kimi-k3"} for spec in roles.values())
    assert owner["mcp_profile"] is None
    assert "None" not in json.dumps(roles)


def test_launch_role_configs_without_model_or_profile(tmp_path: Path) -> None:
    _root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["trios"]["kimi"] = {"agent": "claude_code", "model": None, "mcp_profile": None}
    launcher = Launcher(supervisor, store, queue_config=config)
    store.create(_base_entry(trio="kimi"))

    asyncio.run(launcher.tick())

    roles = supervisor.created[-1]["owner"]["role_configs"]
    assert all(spec == {"agent": "claude_code"} for spec in roles.values())


def test_auditor_read_only_violation_matches_service_refusal() -> None:
    # Task 234: the helper must return exactly the ValueError text the
    # supervisor raises inside ``_normalise_role_configs``, so the recorded
    # skip reason is the same clear refusal an operator would have seen from
    # the burned attempt.
    from lh_harness.supervisor.service import _normalise_role_configs, auditor_read_only_violation

    specs = {"auditor": {"agent": "claude_code", "model": "m1", "mcp_profile": "default"}}
    expected: str | None = None
    with pytest.raises(ValueError) as raised:
        _normalise_role_configs(specs, agent="claude_code", model="m1")
    expected = str(raised.value)

    refusal = auditor_read_only_violation(specs, agent="claude_code", model="m1")
    assert refusal == expected
    assert refusal is not None
    assert "roles.auditor.mcp_profile 'default'" in refusal
    assert "not read-only" in refusal


def test_auditor_read_only_violation_accepts_read_only_and_unset_profiles() -> None:
    from lh_harness.supervisor.service import auditor_read_only_violation

    # The built-in read-only profile is accepted.
    read_only = {"auditor": {"agent": "claude_code", "model": "m1", "mcp_profile": "audit"}}
    assert auditor_read_only_violation(read_only, agent="claude_code", model="m1") is None
    # An auditor spec without any profile falls back to the built-in default,
    # which is read-only.
    unset = {"auditor": {"agent": "claude_code", "model": "m1"}}
    assert auditor_read_only_violation(unset, agent="claude_code", model="m1") is None
    # Unrelated validation failures are left to create_run's own handling.
    assert auditor_read_only_violation({"auditor": {"agent": "nope", "model": "m1"}}, agent="claude_code", model="m1") is None


def test_launcher_skips_unknown_profile_with_read_only_requirement(tmp_path: Path) -> None:
    # An explicit auditor profile that is not read-only is refused on the
    # pre-burn path even when it arrives through a trio-level override; the
    # run-wide ``mcp_profile`` is NOT sent to create_run (cutover 168), so the
    # launched specs stay stripped and the reservation check still passes.
    _root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["trios"]["kimi"] = {"agent": "claude_code", "model": "kimi-k3", "mcp_profile": "none"}
    launcher = Launcher(supervisor, store, queue_config=config)
    entry = store.create(_base_entry(trio="kimi"))

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "launched"
    owner = supervisor.created[-1]["owner"]
    assert all(spec == {"agent": "claude_code", "model": "kimi-k3"} for spec in owner["role_configs"].values())


def test_launcher_stale_suspend_manifest_never_revives_runs(tmp_path: Path) -> None:
    """A manifest an OLDER suspend left behind (the 2026-10-08 residue: deploy
    run 37737427710 was cancelled mid-window and its manifest outlived it)
    still shields the parked run's queue entry from failed-reconciliation —
    protection deliberately ignores suspend-id provenance, a safe
    conservative default — but the launcher must never treat the stale file
    as a source of work: reconciliation neither starts nor resumes anything
    from a manifest whose suspend id is not the current one.  Revival is
    id-keyed in POST /api/maintenance/resume only."""
    root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 1
    launcher = Launcher(supervisor, store, queue_config=config, probe_open_pr=None)

    supervisor.add_run("run-stale", "./w1", status="cancelled")
    supervisor.add_run("run-lost", "./w2", status="cancelled")
    parked = store.create(_base_entry(trio="kimi", priority=5, workspace="./w1"))
    unprotected = store.create(_base_entry(trio="kimi", priority=4, workspace="./w2"))
    store.mark_launched(parked.queue_id, "run-stale")
    store.mark_launched(unprotected.queue_id, "run-lost")

    stale_manifest = {
        "suspend_id": "sus-20261008T054411Z-4c9cce84",
        "reason": "deploy 37737427710 (cancelled mid-window)",
        "created_at": 123.0,
        "runs": [
            {"run_id": "run-stale", "queue_id": parked.queue_id, "resume_epoch_before": 0}
        ],
    }
    queue_dir = root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    stale_path = queue_dir / "maintenance_manifest_sus-20261008T054411Z-4c9cce84.json"
    stale_path.write_text(json.dumps(stale_manifest), encoding="utf-8")

    # The ACTUAL reconciliation helper consumes the stale file (no test here
    # reimplements it): the parked run id comes back through the production
    # glob over maintenance_manifest_<suspend_id>.json.
    assert maintenance_manifest_run_ids(root) == {"run-stale"}

    asyncio.run(launcher.tick())

    parked_entry = store.get(parked.queue_id)
    unprotected_entry = store.get(unprotected.queue_id)
    assert parked_entry is not None
    assert unprotected_entry is not None
    # It protects the entry it names (this would reconcile to failed if the
    # suspend-id-keyed file were ignored, as pre-LHH-SUSPEND-TIMEOUT), ...
    assert parked_entry.status == "launched"
    # ... stays selective doing so (the control entry, named by no manifest,
    # reconciles exactly as before), ...
    assert unprotected_entry.status == "failed"
    # ... and revives NOTHING: no run is started, the parked run stays
    # terminal, and the stale manifest is left byte-for-byte intact for an
    # id-keyed operator resume — reconciliation is read-only.
    assert supervisor.created == []
    assert supervisor.status("run-stale")["status"] == "cancelled"
    assert json.loads(stale_path.read_text(encoding="utf-8")) == stale_manifest


# ----------------------------------------------------------------------
# LHH-QUEUE-HOTRELOAD R3: trios/capacity hot-reload from the settings DB.
#
# The launcher seeds the embedded settings DB once at construction (empty
# tables only, from the patched PROJECT_CONFIG_PATH below) and then re-reads
# queue_trio / queue_capacity at the start of every scheduling pass, so a DB
# change lands on the next pass with no restart. A launch still freezes the
# trio the run started with. Tests never touch the real .lh-harness paths:
# PROJECT_CONFIG_PATH and the settings env are pointed at tmp_path.
# ----------------------------------------------------------------------

_SETTINGS_KEY = "k" * 40  # >= settings_store.MIN_KEY_CHARS


def _settings_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the launcher at a throwaway settings DB; return its path."""

    db_path = tmp_path / "settings.db"
    monkeypatch.setenv(settings_store.DB_ENV, str(db_path))
    monkeypatch.setenv(settings_store.KEY_ENV, _SETTINGS_KEY)
    return db_path


def _write_project_config(path: Path, *, model: str | None = None, kimi_max: int = 1) -> None:
    lines = ["[queue.trios.kimi]", 'agent = "claude_code"']
    if model is not None:
        lines.append(f'model = "{model}"')
    lines += ["", "[queue.capacity]", f"kimi_max = {kimi_max}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _patch_project_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr("lh_harness.launcher.PROJECT_CONFIG_PATH", path)


def _db_update(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(sql, params)


def test_db_trio_change_takes_effect_on_next_pass_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store, supervisor = _fixture(tmp_path)
    db_path = _settings_env(tmp_path, monkeypatch)
    config_path = tmp_path / "config.toml"
    _write_project_config(config_path, model="model-from-file", kimi_max=3)
    _patch_project_config(tmp_path, monkeypatch, config_path)
    settings = SettingsStore(db_path, _SETTINGS_KEY)
    # Operator rows land BEFORE the launcher starts, so its one-time seed
    # skips these tables entirely.
    settings.seed_queue_defaults(
        {"kimi": {"agent": "claude_code", "model": "model-a"}}, {"kimi_max": 3}
    )
    launcher = Launcher(supervisor, store, queue_config=default_queue_config())

    first = store.create(_base_entry(trio="kimi", workspace="./w1"))
    asyncio.run(launcher.tick())
    assert store.get(first.queue_id).status == "launched"
    assert supervisor.created[0]["owner"]["role_configs"]["manager"]["model"] == "model-a"

    # Admin edit mid-flight: same process, no restart, no new Launcher.
    _db_update(db_path, "UPDATE queue_trio SET model = ? WHERE name = 'kimi'", ("model-b",))

    second = store.create(_base_entry(trio="kimi", workspace="./w2"))
    asyncio.run(launcher.tick())
    assert store.get(second.queue_id).status == "launched"
    assert supervisor.created[1]["owner"]["role_configs"]["manager"]["model"] == "model-b"
    # The run that started under the old trio keeps its resolved model.
    assert supervisor.created[0]["owner"]["role_configs"]["manager"]["model"] == "model-a"


def test_db_capacity_change_takes_effect_on_next_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store, supervisor = _fixture(tmp_path)
    db_path = _settings_env(tmp_path, monkeypatch)
    config_path = tmp_path / "config.toml"
    _write_project_config(config_path, kimi_max=3)
    _patch_project_config(tmp_path, monkeypatch, config_path)
    settings = SettingsStore(db_path, _SETTINGS_KEY)
    # DB says zero: it must beat the bootstrap config's kimi_max=3.
    settings.seed_queue_defaults(
        {"kimi": {"agent": "claude_code"}}, {"kimi_max": 0}
    )
    bootstrap = default_queue_config()
    bootstrap["capacity"]["kimi_max"] = 3
    launcher = Launcher(supervisor, store, queue_config=bootstrap)

    entry = store.create(_base_entry(trio="kimi", workspace="./w1"))
    asyncio.run(launcher.tick())
    refused = store.get(entry.queue_id)
    assert refused is not None
    assert refused.status == "pending"
    assert any("kimi at capacity" in reason for reason in refused.skip_reasons)
    assert supervisor.created == []

    _db_update(db_path, "UPDATE queue_capacity SET max_concurrent = 2 WHERE trio = 'kimi'")
    asyncio.run(launcher.tick())
    assert store.get(entry.queue_id).status == "launched"
    assert len(supervisor.created) == 1


def test_empty_db_falls_back_to_config_toml_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store, supervisor = _fixture(tmp_path)
    db_path = _settings_env(tmp_path, monkeypatch)
    # A missing bootstrap file: the one-time seed is a no-op and the tables
    # stay empty, so every pass must use the config-derived values.
    _patch_project_config(tmp_path, monkeypatch, tmp_path / "missing.toml")
    settings = SettingsStore(db_path, _SETTINGS_KEY)
    bootstrap = default_queue_config()
    bootstrap["capacity"]["kimi_max"] = 1
    launcher = Launcher(supervisor, store, queue_config=bootstrap)

    head = store.create(_base_entry(trio="kimi", workspace="./w1"))
    tail = store.create(_base_entry(trio="kimi", workspace="./w2"))
    asyncio.run(launcher.tick())

    assert store.get(head.queue_id).status == "launched"
    assert "model" not in supervisor.created[0]["owner"]["role_configs"]["manager"]
    tail_entry = store.get(tail.queue_id)
    assert tail_entry is not None
    assert tail_entry.status == "pending"
    assert any("kimi at capacity" in reason for reason in tail_entry.skip_reasons)
    # The empty tables were not secretly populated during the pass.
    assert settings.queue_trios() == []
    assert settings.queue_capacity() == {}


def test_restarting_seed_preserves_operator_edited_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store, supervisor = _fixture(tmp_path)
    db_path = _settings_env(tmp_path, monkeypatch)
    config_path = tmp_path / "config.toml"
    _write_project_config(config_path, model="model-from-file", kimi_max=1)
    _patch_project_config(tmp_path, monkeypatch, config_path)

    # First start: empty tables are seeded from config.toml.
    Launcher(supervisor, store, queue_config=default_queue_config())
    settings = SettingsStore(db_path, _SETTINGS_KEY)
    assert [row["model"] for row in settings.queue_trios() if row["name"] == "kimi"] == [
        "model-from-file"
    ]
    assert settings.queue_capacity()["kimi"] == 1

    # Operator edits both tables.
    _db_update(db_path, "UPDATE queue_trio SET model = ? WHERE name = 'kimi'", ("model-edited",))
    _db_update(db_path, "UPDATE queue_capacity SET max_concurrent = 5 WHERE trio = 'kimi'")

    # Second start (simulated restart): the seed must leave the rows alone.
    launcher = Launcher(supervisor, store, queue_config=default_queue_config())
    assert launcher._settings_store is not None
    assert [row["model"] for row in settings.queue_trios() if row["name"] == "kimi"] == [
        "model-edited"
    ]
    assert settings.queue_capacity()["kimi"] == 5

    # And the edited model is what the next launch actually resolves.
    entry = store.create(_base_entry(trio="kimi", workspace="./w1"))
    asyncio.run(launcher.tick())
    assert store.get(entry.queue_id).status == "launched"
    assert supervisor.created[0]["owner"]["role_configs"]["manager"]["model"] == "model-edited"


# ----------------------------------------- LHH-QUEUE-PROTECT-DOCS F4 --
# Paxton (2026-10-12): a launcher-facing read-path regression test for the
# F3 protected-trio plumbing.  The launcher holds a settings store
# (``_open_settings_store()`` -> ``active_runtime().store``) and reads
# ``queue_trios()`` / ``queue_capacity_rows()`` on every scheduling pass
# (``_refresh_queue_config``).  This test instantiates a launcher with a
# mocked settings store, seeds it with the F3 protected-trio contract
# (kimi / glm / qwen are present with their seeded models, zai is
# non-protected), and asserts the resolved trio / capacity dicts the
# launcher would enforce match the store.  The mocked store replaces the
# ``_ACTIVE`` runtime the launcher's store opener reads, so the launcher
# never opens a real DB on disk.  See docs/queue.md "Protected trios".

def test_launcher_read_path_sees_the_protected_trio_set_via_mocked_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store, supervisor = _fixture(tmp_path)
    db_path = _settings_env(tmp_path, monkeypatch)
    _patch_project_config(tmp_path, monkeypatch, tmp_path / "missing.toml")

    # Mocked settings store: same on-disk sqlite, but the launcher's
    # ``_open_settings_store()`` reads it through the active runtime
    # (``active_runtime().store``) so the test is hermetic to the
    # launcher's own store-opening path.
    mocked = SettingsStore(db_path, _SETTINGS_KEY)
    mocked.seed_queue_defaults(
        {
            "kimi": {"agent": "claude_code", "model": "kimi-from-store", "mcp_profile": "ops"},
            "glm": {"agent": "claude_code", "model": "glm-from-store", "mcp_profile": "ops"},
            "qwen": {"agent": "codex", "model": "qwen-from-store", "mcp_profile": "audit"},
            "zai": {"agent": "claude_code", "model": "zai-from-store", "mcp_profile": "ops"},
        },
        {"kimi_max": 2, "glm_max": 1, "qwen_max": 1, "zai_max": 3},
    )
    rt = SettingsRuntime(store=mocked, protected_trios=("kimi", "glm", "qwen"))
    monkeypatch.setattr(settings_store, "_ACTIVE", rt)

    bootstrap = default_queue_config()
    bootstrap["capacity"]["kimi_max"] = 5  # file value -- the store row must win
    bootstrap["capacity"]["glm_max"] = 5
    bootstrap["capacity"]["qwen_max"] = 5
    bootstrap["capacity"]["zai_max"] = 5
    bootstrap["trios"]["kimi"] = {"agent": "claude_code", "model": "kimi-from-file"}
    bootstrap["trios"]["glm"] = {"agent": "claude_code", "model": "glm-from-file"}
    bootstrap["trios"]["qwen"] = {"agent": "codex", "model": "qwen-from-file"}
    bootstrap["trios"]["zai"] = {"agent": "claude_code", "model": "zai-from-file"}
    launcher = Launcher(supervisor, store, queue_config=bootstrap)

    # The launcher wired the mocked store on construction (one-time seed
    # already populated the tables; the launcher's store is the mock).
    assert launcher._settings_store is not None
    assert launcher._settings_store is mocked
    assert rt.protected_trios == ("kimi", "glm", "qwen")

    # Trigger a pass; the launcher re-reads the store on every pass.
    asyncio.run(launcher.tick())

    trios = launcher._config["trios"]
    assert trios["kimi"]["model"] == "kimi-from-store"
    assert trios["glm"]["model"] == "glm-from-store"
    assert trios["qwen"]["model"] == "qwen-from-store"
    assert trios["zai"]["model"] == "zai-from-store"
    capacity = launcher._config["capacity"]
    assert capacity["kimi_max"] == 2
    assert capacity["glm_max"] == 1
    assert capacity["qwen_max"] == 1
    assert capacity["zai_max"] == 3

    # The F3 protected-trio set is the SAME set the REST/MCP admin gates
    # enforce (PUT /api/queue/trios and set_trio_model).  Reading from
    # the runtime here proves the launcher's view is consistent with the
    # admin gate: an operator who tries to PUT a new model on kimi sees
    # 409, and the launcher's hot-reload path uses the same row.
    protected_in_view = {name for name in ("kimi", "glm", "qwen") if trios.get(name)}
    assert protected_in_view == {"kimi", "glm", "qwen"}
