"""easybutt0n-238: per-role pins are guarded, and a missing worker refuses.

This module covers two independently testable behaviours:

(A) A REGRESSION GUARD around the per-role pins that ALREADY WORKED, plus
    the role-key de-duplication refactor.  A task enqueued through the queue
    with a per-role ``roles`` dict (each role pinned to its own agent AND
    model) must reach the launched run with those pins intact -- the SAME
    values a direct ``POST /api/runs`` create honours.  The pin set wins
    over the trio default for EVERY role, not only the executor, and no role
    name or model id is special-cased: the test pins three different models
    and asserts equality role by role.

    These tests are a GUARD, not a fix for a broken queue path.  The
    per-role pin behaviour was already correct before this work: the
    function that merges the caller's pins onto the trio specs
    (``launcher._launch_role_configs``) is byte-identical between the base
    commit and this branch, and the three role-pin tests below pass
    UNMODIFIED on the base commit (only this module's newer-API import
    removed).  What this change adds on the role-handling side is the
    role-key de-duplication refactor: the launcher no longer spells the
    ``("manager", "executor", "auditor")`` tuple out in three places, it
    reads ``supervisor.service._ROLE_KEYS`` through
    ``launcher._launch_role_keys``, so "which roles exist" is one fact.

    The easybutt0n-238 ROLE LOSS is NOT in the harness: the role loss was
    in the gateway `enqueue_task` tool (hydra `orchestrator/src/mcp.js`),
    which did not forward a `roles` field; that is fixed separately in
    hydra PR #104 (merged 2026-11-14 08:37Z). These tests pin the
    harness-side behaviour so the forwarded roles are honoured.

(B) A launch naming an agent with no worker installed on this node used to
    spawn the worker anyway; the worker could not spawn the agent and died
    with a bare ``worker exited with status 2`` that named nothing.  The
    launch is now refused BEFORE any process is spawned, with the named
    reason ``launch_refused: agent <name> has no worker on this node`` --
    present in the run record AND in the queue entry.

    The refusal is detected INSIDE ``supervisor.create_run``, so a run
    record and a run directory already exist when it is decided, and the
    refusal FAILS that run record.  Nothing is launched (no worker process,
    no agent CLI) and no capacity slot is reserved.

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
# (A) REGRESSION GUARD: the caller's per-role pins reach the launched run
#
# These behaviours already worked before this branch (the pin-merge
# function is byte-identical to the base commit and these tests pass
# unmodified on it).  They are guarded here so they keep working, and so
# that the gateway's ``enqueue_task`` tool -- the actual source of the
# reported role loss -- can forward ``roles`` and have the harness honour
# them.
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

    The mixed case: the pinned role must win while its UNPINNED siblings
    still inherit the trio's own values.  (This is the shape the reported
    defect took in reverse -- the gateway dropped the ``roles`` field
    entirely, so every role fell back to the trio default.)
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


def test_real_agent_binary_resolution_against_a_synthetic_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REAL ``resolve_agent_binary`` answers the question on a real PATH.

    Every other test in this module stubs the registry's PATH lookup, so
    nothing in the suite exercises the actual resolution against an actual
    ``PATH``.  This test does, and it is the one that closes that gap: it
    opts out of the suite-wide autouse stub (``tests/conftest.py`` stubs
    ``resolve_agent_binary`` to "every agent has a worker" for every test in
    the repository), restores the REAL function, and points ``PATH`` at a
    synthetic directory that either contains an executable stub named like
    the agent's CLI or does not.

    With the stub present the agent HAS a worker; with it absent the agent
    has none, and the launch is refused with the named reason.

    The stub is a real executable file, not a mock: the resolution is
    ``shutil.which`` over ``PATH``, so only a real file on ``PATH`` can
    satisfy it.
    """

    from lh_harness.supervisor.service import RunSupervisor
    from lh_harness.utils.agent_cli import resolve_agent_binary as real_resolve

    # Opt out of the suite-wide autouse stub for THIS test only: put the
    # real resolution back on the registry.  (The autouse fixture patched
    # the same attribute with the same function-scoped monkeypatch, so this
    # setattr is undone with the test like any other.)
    monkeypatch.setattr(agent_registry, "resolve_agent_binary", real_resolve)

    binary = agent_registry.agent_spec("claude_code").binary
    assert binary == "claude"

    # The synthetic PATH: a temp dir that holds ONLY what this test puts
    # in it, so the host's own installation of the CLI cannot answer.
    synthetic_path = tmp_path / "synthetic-bin"
    synthetic_path.mkdir()
    monkeypatch.setenv("PATH", str(synthetic_path))

    # --- the agent is NOT installed on this node ------------------------
    assert agent_registry.agent_has_worker("claude_code") is False

    root = tmp_path / "case"
    root.mkdir()
    store, _sup, runtime, _launcher_obj = _launcher(root, monkeypatch)
    runs_root = root / "runs"
    supervisor = RunSupervisor(runs_root=runs_root, workspace_root=root)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 3
    launcher = Launcher(supervisor, store, queue_config=config)

    entry = store.create(
        {**_base_entry(trio="kimi", workspace="./w-missing"), "roles": _PINNED},
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )
    asyncio.run(launcher.tick())
    refused = store.get(entry.queue_id)
    assert refused is not None
    # The launch is refused with the NAMED reason.
    assert refused.status == "failed"
    assert refused.reason == (
        "launch_refused: agent claude_code has no worker on this node"
    )
    assert refused.run_id is None

    # --- the agent IS installed on this node ----------------------------
    stub = synthetic_path / binary
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)

    assert agent_registry.agent_has_worker("claude_code") is True

    present_root = tmp_path / "case-present"
    present_root.mkdir()
    present_store, present_sup, present_runtime, _present_launcher_obj = _launcher(
        present_root, monkeypatch
    )
    present_runs_root = present_root / "runs"
    present_supervisor = RunSupervisor(
        runs_root=present_runs_root, workspace_root=present_root
    )
    present_launcher = Launcher(
        present_supervisor, present_store, queue_config=config
    )
    present_entry = present_store.create(
        {**_base_entry(trio="kimi", workspace="./w-present"), "roles": _PINNED},
        model_fetch=_fake_model_catalog,
        settings_runtime=present_runtime,
    )
    asyncio.run(present_launcher.tick())
    launched = present_store.get(present_entry.queue_id)
    assert launched is not None
    # The same code path now launches: the agent has a worker.
    assert launched.status == "launched", (launched.status, launched.skip_reasons)


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
    # No worker for this agent on this node.  The override is scoped to this
    # block: ``monkeypatch.context()`` patches through a SEPARATE undo
    # registry, so leaving the block restores the value the attribute had on
    # entry (the suite-wide autouse stub) and leaves every other patch --
    # including the ones the autouse fixture made -- in place.
    with pytest.MonkeyPatch.context() as scoped:
        scoped.setattr(
            "lh_harness.agent_registry.resolve_agent_binary", lambda binary: None
        )
        created = supervisor.create_run(
            task="do something",
            agent="codex",
            role_configs={"manager": {"agent": "codex", "model": "m"}},
            workspace=str(tmp_path),
        )

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


def test_the_retention_sweep_reclaims_a_refused_run_directory(
    tmp_path: Path,
) -> None:
    """The refused run directory is reclaimed by the retention sweep.

    The agent refusal is detected inside ``create_run``, so the run record
    and the run directory exist and are then FAILED.  The run directory is
    left behind on purpose (the refusal must be visible in the run record),
    so the operator-facing question is whether that directory is reclaimed.

    It is: the refusal's terminal status is ``failed``, and the retention
    sweep's ``LIVE_STATES`` -- the only states it refuses to prune -- does
    NOT include ``failed``.  A refused run directory older than the
    configured ``run_max_age_days`` is therefore pruned like any other
    terminal run.

    This test PROVES the existing behaviour; it does not change it.  It
    refuses a real launch, then runs the real sweep over the real run
    directory.
    """

    import os
    import time

    from lh_harness.disk_retention import RetainMode, prune_runs
    from lh_harness.disk_retention.config import DEFAULT_RUN_MAX_AGE_DAYS
    from lh_harness.disk_retention.runs import LIVE_STATES
    from lh_harness.supervisor.service import RunSupervisor

    runs_root = tmp_path / "runs"
    runs_root.mkdir(parents=True)
    supervisor = RunSupervisor(runs_root=runs_root, workspace_root=tmp_path)

    # No worker for this agent on this node -> the refusal path.  The
    # override is scoped to this block, so the suite-wide autouse stub is
    # back in place the moment the block ends.
    with pytest.MonkeyPatch.context() as scoped:
        scoped.setattr(
            "lh_harness.agent_registry.resolve_agent_binary", lambda binary: None
        )
        created = supervisor.create_run(
            task="do something",
            agent="codex",
            role_configs={"manager": {"agent": "codex", "model": "m"}},
            workspace=str(tmp_path),
        )

    run_id = str(created["id"])
    run_dir = runs_root / run_id
    # The refusal left a run directory behind, and the run is TERMINAL.
    assert created["status"] == "failed"
    assert run_dir.is_dir()
    # The sweep's live states never include a terminal state, so the refused
    # run is never protected as "live".
    assert "failed" not in LIVE_STATES

    # A fresh refused run is inside the age gate: it is kept, not pruned.
    fresh = {r.run_id: r for r in prune_runs(runs_root, mode=RetainMode.live())}
    assert fresh[run_id].state == "skipped_recent"
    assert run_dir.is_dir()

    # Older than the threshold, the refused run directory is reclaimed.
    aged = time.time() - (DEFAULT_RUN_MAX_AGE_DAYS + 1) * 86400
    os.utime(run_dir, (aged, aged))
    pruned = {r.run_id: r for r in prune_runs(runs_root, mode=RetainMode.live())}
    assert pruned[run_id].state == "pruned"
    assert not run_dir.exists()


# ----------------------------------------------------------------------
# operator addition 1: a refused launch consumes no capacity and leaves
# no orphan launched entry
# ----------------------------------------------------------------------


def test_refused_launch_consumes_no_capacity_slot_and_leaves_no_orphan_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused launch must not burn a capacity slot.

    The launcher's per-trio capacity is a batch budget: a pass that launches
    one entry shrinks the remaining slots for the others.  A refusal is NOT
    a launch, so it must leave the remaining capacity exactly as it was --
    otherwise one entry whose agent is missing on this node silently starves
    every other entry in the same trio.

    The assertion is on the launcher's OWN remaining-capacity computation
    (``_remaining_capacity`` -- the same call the pass makes), read before
    and after the refused launch, plus the queue's own view: the entry is
    not ``launched`` and carries no run id, so no orphan launched entry is
    left behind.

    The REAL supervisor is used (not the recording stand-in): the refusal
    lives in the supervisor's pre-spawn check, and a stand-in that always
    creates a run would never exercise it.
    """

    from lh_harness.supervisor.service import RunSupervisor

    store, _sup, runtime, _launcher_obj = _launcher(tmp_path, monkeypatch)
    runs_root = tmp_path / "runs"
    supervisor = RunSupervisor(runs_root=runs_root, workspace_root=tmp_path)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 3
    launcher = Launcher(supervisor, store, queue_config=config)

    # Nothing is installed on this node, so every role's agent is missing.
    monkeypatch.setattr(
        "lh_harness.agent_registry.resolve_agent_binary", lambda binary: None
    )

    refused_entry = store.create(
        {**_base_entry(trio="kimi", workspace="./w-refused"), "roles": _PINNED},
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )
    before = launcher._remaining_capacity(launcher._active_runs())

    asyncio.run(launcher.tick())

    after = launcher._remaining_capacity(launcher._active_runs())
    # No slot was consumed by the refusal.
    assert after == before

    # And no orphan launched entry: the refused entry is failed, has no run
    # id, and no queue entry anywhere claims to be launched.
    refused = store.get(refused_entry.queue_id)
    assert refused is not None
    assert refused.status == "failed"
    assert refused.run_id is None
    assert [entry for entry in store.list() if entry.status == "launched"] == []


def test_a_refused_entry_does_not_starve_the_next_eligible_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal falls through to the next entry instead of blocking it.

    The refusal is the FIRST entry in the pass; the second entry names an
    agent that IS installed on this node, so it must still launch.  This is
    the observable consequence of "a refusal consumes no capacity": the pass
    does not stop at the refused head entry.

    The REAL supervisor is used (the refusal lives in its pre-spawn check).
    """

    from lh_harness.supervisor.service import RunSupervisor

    store, _sup, runtime, _launcher_obj = _launcher(tmp_path, monkeypatch)
    runs_root = tmp_path / "runs"
    supervisor = RunSupervisor(runs_root=runs_root, workspace_root=tmp_path)
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 3
    launcher = Launcher(supervisor, store, queue_config=config)

    real_resolve = agent_registry.resolve_agent_binary

    def _resolve(binary: str) -> str | None:
        # The pinned agent (``claude_code``) resolves to the ``claude`` CLI;
        # that CLI is missing on this node, while every other agent's is not.
        if binary == agent_registry.agent_spec("claude_code").binary:
            return None
        return real_resolve(binary)

    monkeypatch.setattr("lh_harness.agent_registry.resolve_agent_binary", _resolve)

    refused_entry = store.create(
        {**_base_entry(trio="kimi", workspace="./w-refused"), "roles": _PINNED},
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )
    # The second entry names an agent that IS installed on this node, so it
    # must still launch: the refusal must not block the rest of the pass.
    ok_entry = store.create(
        {
            **_base_entry(trio="kimi", workspace="./w-ok"),
            "roles": {
                role: {"agent": "codex", "model": spec["model"]}
                for role, spec in _PINNED.items()
            },
        },
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )

    asyncio.run(launcher.tick())

    refused = store.get(refused_entry.queue_id)
    ok = store.get(ok_entry.queue_id)
    assert refused is not None and refused.status == "failed"
    assert refused.reason == "launch_refused: agent claude_code has no worker on this node"
    assert ok is not None and ok.status == "launched", (ok.status, ok.skip_reasons)


# ----------------------------------------------------------------------
# operator addition 2: the workspace preflight
# ----------------------------------------------------------------------


def test_launch_is_refused_when_the_workspace_is_not_a_git_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace that exists but is not a checkout refuses the launch.

    The preflight names the refusal ``workspace_not_a_repo`` so an operator
    learns WHICH preflight refused the launch instead of finding a run that
    died somewhere else.  The entry stays PENDING (the workspace is a
    configuration the operator can fix, so it must be re-attempted), the
    reason lands on the entry, and nothing is spawned.
    """

    plain = tmp_path / "plain-workspace"
    plain.mkdir()

    store, supervisor, runtime, launcher = _launcher(tmp_path, monkeypatch)
    entry = store.create(
        {**_base_entry(trio="kimi", workspace=str(plain)), "roles": _PINNED},
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending", updated.status
    assert updated.run_id is None
    # The named reason is on the entry, verbatim.
    assert any(
        "workspace_not_a_repo" in reason for reason in updated.skip_reasons
    ), updated.skip_reasons
    # Nothing was launched.
    assert supervisor.created == []


def test_both_refusals_record_the_named_reason_in_the_same_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two refusals agree on WHERE the named reason lands.

    An operator reading the queue entry's ``reason`` field must see BOTH
    refusals.  The agent refusal puts the text in ``entry.reason`` AND in
    ``entry.skip_reasons``; the workspace refusal used to put it only in
    ``entry.skip_reasons``, so it was invisible to anyone who reads just
    ``reason``.  This pins that both land in ``entry.reason`` with the SAME
    text as their ``skip_reasons`` entry.

    The STATUSES are deliberately different and are pinned here too: the
    agent refusal is terminal (the same node would refuse identically, so
    the entry is not requeued) while the workspace refusal is re-attemptable
    (a plain directory is a configuration the operator can fix).
    """

    from lh_harness.supervisor.service import RunSupervisor

    # --- the AGENT refusal ---------------------------------------------
    agent_root = tmp_path / "agent-case"
    agent_root.mkdir()
    agent_store, _agent_sup, agent_runtime, _agent_launcher = _launcher(
        agent_root, monkeypatch
    )
    agent_runs_root = agent_root / "runs"
    agent_supervisor = RunSupervisor(
        runs_root=agent_runs_root, workspace_root=agent_root
    )
    agent_config = default_queue_config()
    agent_config["capacity"]["kimi_max"] = 3
    agent_launcher = Launcher(
        agent_supervisor, agent_store, queue_config=agent_config
    )
    # No agent CLI is installed on this node.  The override is scoped with
    # ``monkeypatch.context()``: that context manager patches through a
    # SEPARATE undo registry, so leaving it restores the value the attribute
    # had on entry -- here, the suite-wide autouse stub from
    # ``tests/conftest.py`` -- without consuming the SHARED fixture's undo
    # stack.  A blanket ``monkeypatch.undo()`` would do the opposite: it
    # unwinds every patch the autouse fixture made (the stub disappears for
    # the rest of the test) and empties the shared registry, so the fixture
    # can no longer restore anything at teardown.
    with monkeypatch.context() as scoped:
        scoped.setattr(
            "lh_harness.agent_registry.resolve_agent_binary", lambda binary: None
        )
        agent_entry = agent_store.create(
            {**_base_entry(trio="kimi", workspace="./w-agent"), "roles": _PINNED},
            model_fetch=_fake_model_catalog,
            settings_runtime=agent_runtime,
        )
        asyncio.run(agent_launcher.tick())
        agent_refused = agent_store.get(agent_entry.queue_id)
    assert agent_refused is not None
    # The named reason is in ``reason``...
    assert agent_refused.reason == (
        "launch_refused: agent claude_code has no worker on this node"
    )
    # ... and in ``skip_reasons``, with the SAME text.
    assert agent_refused.reason in agent_refused.skip_reasons
    # The agent refusal is terminal.
    assert agent_refused.status == "failed"

    # The scoped context restored the suite-wide stub exactly: the registry
    # answer is the autouse stub's again, NOT the real PATH lookup and NOT
    # this test's "no worker" override.
    assert agent_registry.resolve_agent_binary("codex") == "/usr/bin/codex"

    # --- the WORKSPACE refusal ----------------------------------------
    ws_root = tmp_path / "workspace-case"
    ws_root.mkdir()
    plain = ws_root / "plain-workspace"
    plain.mkdir()
    ws_store, ws_sup, ws_runtime, ws_launcher = _launcher(ws_root, monkeypatch)
    ws_entry = ws_store.create(
        {**_base_entry(trio="kimi", workspace=str(plain)), "roles": _PINNED},
        model_fetch=_fake_model_catalog,
        settings_runtime=ws_runtime,
    )
    asyncio.run(ws_launcher.tick())
    ws_refused = ws_store.get(ws_entry.queue_id)
    assert ws_refused is not None
    # The named reason is in ``reason`` too...
    assert ws_refused.reason is not None
    assert ws_refused.reason.startswith("workspace_not_a_repo: ")
    # ... and in ``skip_reasons``, with the SAME text.
    assert ws_refused.reason in ws_refused.skip_reasons
    # The workspace refusal stays re-attemptable: the status is unchanged.
    assert ws_refused.status == "pending"
    assert ws_refused.run_id is None
    # Nothing was launched for it either.
    assert ws_sup.created == []


def test_workspace_preflight_names_the_reason_in_one_place() -> None:
    """One builder, one wording -- the same pattern as the agent refusal."""

    from lh_harness.workspace_guard import (
        WORKSPACE_NOT_A_REPO,
        workspace_not_a_repo_reason,
    )

    assert WORKSPACE_NOT_A_REPO == "workspace_not_a_repo"
    reason = workspace_not_a_repo_reason("/tmp/plain")
    assert reason.startswith("workspace_not_a_repo: ")
    assert "/tmp/plain" in reason


def test_workspace_preflight_lets_a_real_checkout_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preflight does not refuse a workspace that IS a git checkout.

    A guard that refused everything would be useless; this pins that the
    preflight is narrow: a real checkout launches exactly as before.

    The fixture is a real repository with a real ``origin`` (the branch
    guard needs one to resolve the default branch), so the launch reaches
    ``create_run`` for real.
    """

    import subprocess

    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
    }
    origin = tmp_path / "checkout.origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True, env=env,
    )
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, env=env,
    )
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=repo, check=True, env=env)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=repo, check=True, env=env)

    store, supervisor, runtime, launcher = _launcher(tmp_path, monkeypatch)
    entry = store.create(
        {**_base_entry(trio="kimi", workspace=str(repo)), "roles": _PINNED},
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )

    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "launched", (updated.status, updated.skip_reasons)


# ----------------------------------------------------------------------
# operator addition 3: role propagation does not move identity
# ----------------------------------------------------------------------


def test_role_propagation_leaves_the_requester_block_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enqueuing pinned roles must not change the requester/tenant scoping.

    The role pins are a per-role configuration; the requester block is the
    identity proof of WHO asked.  Carrying the pins through the queue must
    leave the identity exactly as the caller supplied it -- same kind, name,
    host, agent and session id -- and must not invent or drop one.
    """

    requester = {
        "kind": "ai",
        "name": "continuity1-op",
        "host": "ci",
        "cwd": "/tmp",
        "agent": "claude",
        "session_id": "b5038555-9e10-4bb3-a81e-aa03a602d445",
    }

    store, supervisor, runtime, launcher = _launcher(tmp_path, monkeypatch)
    entry = store.create(
        {
            **_base_entry(trio="kimi", workspace="./w-identity"),
            "roles": _PINNED,
            "requester": dict(requester),
        },
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )

    # The block survives the enqueue: every field the caller supplied is
    # still there, unchanged.  (The store's own normaliser may ADD fields it
    # defaults -- address/notify/session_ref -- but it must not change or
    # drop one the caller supplied.)
    assert entry.requester is not None
    for key, value in requester.items():
        assert entry.requester[key] == value, key

    asyncio.run(launcher.tick())

    launched = store.get(entry.queue_id)
    assert launched is not None and launched.status == "launched"
    # ... and after the launch that carried the role pins.
    assert launched.requester is not None
    for key, value in requester.items():
        assert launched.requester[key] == value, key
    # The caller's own stamp is untouched too.
    assert launched.requested_by == "ci"
    # The roles are the caller's pins -- the propagation did not rewrite them.
    assert launched.roles == _PINNED
    # The run record carries the resolved role configs, not the identity.
    owner = supervisor._owners[launched.run_id]
    for role, spec in _PINNED.items():
        assert owner["role_configs"][role] == {"agent": spec["agent"], "model": spec["model"]}


def test_role_propagation_does_not_leak_the_requester_into_the_role_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The role specs carry agent and model -- nothing from the identity.

    The identity block and the role pins are independent: a role spec must
    never pick up a requester field, and the requester must never pick up a
    role pin.
    """

    requester = {
        "kind": "ai",
        "name": "continuity1-op",
        "host": "ci",
        "cwd": "/tmp",
        "agent": "claude",
        "session_id": "b5038555-9e10-4bb3-a81e-aa03a602d445",
    }

    store, supervisor, runtime, launcher = _launcher(tmp_path, monkeypatch)
    entry = store.create(
        {
            **_base_entry(trio="kimi", workspace="./w-identity-2"),
            "roles": _PINNED,
            "requester": dict(requester),
        },
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )

    asyncio.run(launcher.tick())
    launched = store.get(entry.queue_id)
    assert launched is not None and launched.status == "launched"

    owner = supervisor._owners[launched.run_id]
    for role, spec in _PINNED.items():
        resolved = owner["role_configs"][role]
        assert set(resolved) <= {"agent", "model"}
        assert resolved["model"] == spec["model"]
        assert resolved["model"] != requester["agent"]
        assert resolved["model"] != requester["session_id"]
