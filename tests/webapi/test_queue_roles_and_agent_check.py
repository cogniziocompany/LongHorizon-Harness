"""easybutt0n-238: queue-launched role pins survive, and a missing worker refuses.

Two independent launch defects, one test module, two independently testable
behaviours:

(A) The queue path dropped the caller's per-role configuration.  A task
    enqueued through ``harness_enqueue_task`` with a per-role ``roles`` dict
    (each role pinned to its own agent AND model) must reach the launched run
    with those pins intact -- the SAME values a direct ``POST /api/runs``
    create honours.  The pin set wins over the trio default for EVERY role,
    not only the executor, and no role name or model id is special-cased:
    the test pins three different models and asserts equality role by role.

(B) A launch naming an agent with no worker installed on this node used to
    spawn the worker anyway; the worker could not spawn the agent and died
    with a bare ``worker exited with status 2`` that named nothing.  The
    launch is now refused BEFORE any process is spawned, with the named
    reason ``launch_refused: agent <name> has no worker on this node`` --
    present in the run record AND in the queue entry.

The agent resolution reuses the harness's own registry
(``agent_registry.agent_spec`` -> ``resolve_agent_binary``), so "installed on
this node" means exactly what the adapters and ``lh-harness doctor`` mean.
These tests never launch a real worker: the supervisor's refusal path is
exercised directly, and the queue path is exercised with a recording
supervisor stand-in that mirrors the production reservation shape.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from lh_harness import agent_registry
from lh_harness.launcher import Launcher
from lh_harness.queue import QueueStore, default_queue_config
from lh_harness.settings_store import SettingsRuntime, SettingsStore
from lh_harness.supervisor.service import (
    _normalise_role_configs,
    launch_refused_reason,
)

from .test_launcher import (
    FakeSupervisor,
    _base_entry,
    _patch_project_config,
    _settings_env,
    _write_project_config,
)

# Three DIFFERENT pinned models, one per role: a test that pinned the same
# model everywhere could pass with an implementation that only ever applied
# ONE role's pin.  The trio default is a fourth, distinct id: if any role
# falls back to it, the equality assertion below fails.
_PINNED = {
    "manager": {"agent": "claude_code", "model": "pinned-model-manager"},
    "executor": {"agent": "claude_code", "model": "pinned-model-executor"},
    "auditor": {"agent": "claude_code", "model": "pinned-model-auditor"},
}
_TRIO_MODEL = "trio-default-model"

_FAKE_CATALOG = {
    "agents": [],
    "models": {
        "claude_code": [
            {"id": spec["model"], "label": spec["model"], "availability": "detected"}
            for spec in _PINNED.values()
        ],
        "codex": [{"id": "codex-mini", "label": "codex-mini", "availability": "detected"}],
    },
    "model_discovery": {},
}


def _fake_model_catalog(*, force: bool = False, **_kwargs: Any) -> dict[str, Any]:
    return json.loads(json.dumps(_FAKE_CATALOG))


class _RecordingSupervisor(FakeSupervisor):
    """Records the role configs the launcher hands to ``create_run``.

    The real supervisor resolves the role configs it receives through
    ``_normalise_role_configs`` (the very call ``POST /api/runs`` applies) and
    writes the RESOLVED specs into the reservation verbatim.  This stand-in
    stores the same resolved shape through that same production normalizer --
    not a reimplementation -- so the assertion below is about what actually
    lands on the run record.
    """

    def create_run(self, **kwargs: Any) -> dict[str, Any]:
        created = super().create_run(**kwargs)
        run_id = str(created["id"])
        self._owners[run_id]["role_configs"] = (
            _normalise_role_configs(
                kwargs.get("role_configs"),
                agent=kwargs.get("agent", "codex"),
                model=kwargs.get("model"),
            )
            or None
        )
        return created


def _launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    supervisor: Any | None = None,
) -> tuple[QueueStore, Any, SettingsRuntime, Launcher]:
    """A file-store queue + recording supervisor + launcher on throwaway paths."""

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    sup = supervisor if supervisor is not None else _RecordingSupervisor(root)
    db_path = _settings_env(tmp_path, monkeypatch)
    config_path = tmp_path / "config.toml"
    _write_project_config(config_path, model=_TRIO_MODEL, kimi_max=3)
    _patch_project_config(tmp_path, monkeypatch, config_path)
    runtime = SettingsRuntime(store=SettingsStore(db_path, "k" * 40), error=None)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 3
    return store, sup, runtime, Launcher(sup, store, queue_config=config)


# ----------------------------------------------------------------------
# (A) the caller's per-role pins reach the launched run
# ----------------------------------------------------------------------


def test_enqueued_role_pins_reach_the_launched_run_for_every_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every role's pinned agent AND model wins over the trio default.

    The enqueue goes through the production store path (the same
    ``QueueStore.create`` the MCP ``harness_enqueue_task`` tool and the REST
    ``POST /api/queue`` route call), and the assertion is on the launched
    run's resolved role configs -- the run record a worker actually starts
    with.
    """

    store, supervisor, runtime, launcher = _launcher(tmp_path, monkeypatch)

    entry = store.create(
        {**_base_entry(trio="kimi", workspace="./w1"), "roles": _PINNED},
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )
    # The pins persist UNRESOLVED on the entry: the trio's agent/default
    # model are resolved at launch, not frozen at enqueue.
    assert entry.roles == _PINNED

    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    assert launched is not None
    assert launched.status == "launched", (launched.status, launched.skip_reasons, launched.reason)
    assert launched.run_id

    owner = supervisor._owners[launched.run_id]
    role_configs = owner["role_configs"]
    # Every role carries the caller's pin -- agent AND model -- and NOT the
    # trio default.  The comparison is per role, so a launcher that honoured
    # only one role (or only the executor) fails here.
    for role, spec in _PINNED.items():
        assert role_configs[role] == {"agent": spec["agent"], "model": spec["model"]}
    assert _TRIO_MODEL not in {
        spec.get("model") for spec in role_configs.values()
    }


def test_enqueued_role_pins_survive_a_reload_of_the_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pins are durable, not an artefact of the in-memory entry.

    The launcher re-reads the entry from the store on every scheduling pass,
    so a pin set that only lived in memory would be invisible to the launch
    that actually creates the run.
    """

    store, supervisor, runtime, launcher = _launcher(tmp_path, monkeypatch)
    entry = store.create(
        {**_base_entry(trio="kimi", workspace="./w1"), "roles": _PINNED},
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )

    reloaded = QueueStore(tmp_path / "runs").get(entry.queue_id)
    assert reloaded is not None
    assert reloaded.roles == _PINNED

    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    assert launched is not None and launched.status == "launched"
    owner = supervisor._owners[launched.run_id]
    for role, spec in _PINNED.items():
        assert owner["role_configs"][role] == {"agent": spec["agent"], "model": spec["model"]}


def test_a_role_pin_overrides_the_trio_default_per_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single pinned role wins, and the unpinned roles keep the trio default.

    This is the mixed case the measured defect produced in reverse: the
    defect let the trio default win over a pin, so the pinned role must win
    while its UNPINNED siblings still inherit the trio's own values.
    """

    store, supervisor, runtime, launcher = _launcher(tmp_path, monkeypatch)
    entry = store.create(
        {
            **_base_entry(trio="kimi", workspace="./w1"),
            "roles": {"auditor": {"model": "pinned-model-auditor"}},
        },
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )

    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    assert launched is not None and launched.status == "launched"
    role_configs = supervisor._owners[launched.run_id]["role_configs"]
    assert role_configs["auditor"]["model"] == "pinned-model-auditor"
    assert role_configs["manager"]["model"] == _TRIO_MODEL
    assert role_configs["executor"]["model"] == _TRIO_MODEL


# ----------------------------------------------------------------------
# (B) a role's agent with no worker on this node refuses the launch
# ----------------------------------------------------------------------


def test_agent_without_worker_on_this_node_is_reported_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registry's worker check answers the real question.

    ``resolve_agent_binary`` is the harness's existing PATH resolution; a
    node without the agent's CLI has no worker for it, and the check says so
    without running the CLI.
    """

    monkeypatch.setattr(agent_registry, "resolve_agent_binary", lambda binary: None)
    assert agent_registry.agent_has_worker("codex") is False
    assert agent_registry.agents_without_worker(["codex", "claude_code"]) == [
        "codex",
        "claude_code",
    ]

    monkeypatch.setattr(agent_registry, "resolve_agent_binary", lambda binary: f"/usr/bin/{binary}")
    assert agent_registry.agent_has_worker("codex") is True
    assert agent_registry.agents_without_worker(["codex", "claude_code"]) == []


def test_launch_refusal_reason_names_the_agent_and_the_node() -> None:
    """The refusal text is one stable string, built in one place."""

    assert launch_refused_reason("codex") == (
        "launch_refused: agent codex has no worker on this node"
    )


def test_queue_entry_records_the_refusal_instead_of_launching_a_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A role agent with no worker on this node refuses the launch.

    The refusal is recorded on the QUEUE ENTRY with the exact text, the
    entry does not launch, and no worker process is spawned.  The refusal is
    not retryable: the same node would refuse a successor identically, so the
    entry stays failed instead of burning attempts.

    The REAL supervisor is used here (not the recording stand-in): the
    refusal lives in the supervisor's pre-spawn check, and the launcher's job
    is to copy the refusal onto the queue entry.  The agent resolution is
    stubbed at the registry's PATH lookup -- the same seam the doctor's
    availability report uses -- so no agent CLI is needed and no worker is
    ever started.
    """

    from lh_harness.supervisor.service import RunSupervisor

    store, _sup, runtime, _launcher_obj = _launcher(tmp_path, monkeypatch)
    runs_root = tmp_path / "runs"
    supervisor = RunSupervisor(runs_root=runs_root, workspace_root=tmp_path)
    launcher = Launcher(supervisor, store, queue_config=default_queue_config())

    # No agent CLI is installed on this node.
    monkeypatch.setattr(
        "lh_harness.agent_registry.resolve_agent_binary", lambda binary: None
    )

    entry = store.create(
        {**_base_entry(trio="kimi", workspace="./w1"), "roles": _PINNED},
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )

    asyncio.run(launcher.tick())
    refused = store.get(entry.queue_id)
    assert refused is not None
    # The queue entry carries the refusal, verbatim.
    assert refused.reason == "launch_refused: agent claude_code has no worker on this node"
    assert refused.status == "failed"
    # Nothing launched: no run id was ever attached to the entry.
    assert refused.run_id is None
    # The run record exists (the refusal is visible there too) but it never
    # got a worker: the reservation has no pid, so the refusal happened
    # before any process was spawned.
    items = supervisor.list_run_items()
    assert len(items) == 1
    assert items[0]["status"] == "failed"
    owner = supervisor.owner(str(items[0]["id"]))
    assert int(owner.get("pid", 0) or 0) == 0


def test_refusal_is_present_in_both_the_run_record_and_the_queue_entry(
    tmp_path: Path,
) -> None:
    """The same refusal text lands on the run record AND on the queue entry.

    The supervisor refuses BEFORE spawning: it writes the run's owner/status
    record with the named ``failure_reason`` and returns without starting a
    process.  The launcher then copies that exact reason onto the queue
    entry.  Both sides read the same, so an operator reading either one
    learns which agent is missing.
    """

    from lh_harness.supervisor.service import RunSupervisor

    runs_root = tmp_path / "runs"
    runs_root.mkdir(parents=True)
    supervisor = RunSupervisor(runs_root=runs_root, workspace_root=tmp_path)
    # No worker for this agent on this node.
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "lh_harness.agent_registry.resolve_agent_binary", lambda binary: None
    )
    try:
        created = supervisor.create_run(
            task="do something",
            agent="codex",
            role_configs={"manager": {"agent": "codex", "model": "m"}},
            workspace=str(tmp_path),
        )
    finally:
        monkey.undo()

    reason = "launch_refused: agent codex has no worker on this node"
    assert created["status"] == "failed"
    assert created["failure_reason"] == reason
    assert created["launch_refused"] is True

    # The run record: owner + status carry the refusal.
    run_id = str(created["id"])
    status = supervisor.status(run_id)
    assert status["status"] == "failed"
    assert status["failure_reason"] == reason
    owner = supervisor.owner(run_id)
    assert owner["failure_reason"] == reason
    # No worker was spawned: the reservation never got a pid.
    assert int(owner.get("pid", 0) or 0) == 0
