"""LHH-QUEUE-HOTRELOAD R8: launch-time consumption of per-task overrides + run lock.

The launcher resolves the queued entry's per-task fields exactly once -- at
launch -- and hands them to ``create_run``:

* the effective MODEL is the entry's ``model`` override (validated against
  the live list at enqueue/edit) when the entry carries one, else the trio
  default from THIS pass's hot-reloaded trio set;
* a dict-form ``roles`` pin set (stored unresolved by R6/R7 so trio/default
  edits keep flowing until launch) is applied on top of the legacy trio
  specs and resolved through ``create_run``'s own ``_normalise_role_configs``
  call, so inheritance lands this pass's agent/model in every unpinned
  field;
* the supervisor's reservation (``control/owner.json``) then persists the
  resolved model and the fully resolved role configs verbatim, and nothing
  rewrites that record afterwards: a later trio default change, later
  hot-reload passes, or later entry edits (refused with 409 after launch by
  R7) must never mutate an already-launched run's recorded model/roles.

The recording supervisor below mirrors the production reservation shape (raw
launch model + resolved role configs), so every assertion names what
actually lands on the run record at launch.  All model-list validation goes
through the same fake catalog as the R6 suite -- never the network.  Tests
never touch real ``.lh-harness`` paths: the settings env and
PROJECT_CONFIG_PATH are pointed at tmp_path exactly like the R3 tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from lh_harness import queue as queue_module
from lh_harness.launcher import Launcher
from lh_harness.queue import QueueStore, default_queue_config
from lh_harness.settings_store import SettingsRuntime, SettingsStore
from lh_harness.supervisor.service import _normalise_role_configs

from .test_launcher import (
    FakeSupervisor,
    _base_entry,
    _db_update,
    _patch_project_config,
    _settings_env,
    _write_project_config,
)

_FAKE_CATALOG = {
    "agents": [],
    "models": {
        "claude_code": [
            {"id": "opus", "label": "opus · default", "availability": "detected"},
            {"id": "sonnet", "label": "sonnet", "availability": "detected"},
        ],
        "codex": [{"id": "codex-mini", "label": "codex-mini", "availability": "detected"}],
    },
    "model_discovery": {},
}


def _fake_model_catalog(*, force: bool = False, **_kwargs: Any) -> dict[str, Any]:
    return json.loads(json.dumps(_FAKE_CATALOG))


class _RecordingSupervisor(FakeSupervisor):
    """Fake supervisor whose owner record mirrors the production reservation.

    The real supervisor's ``create_run`` resolves the role configs it
    receives through ``_normalise_role_configs`` (the exact call the
    create-run API applies for POST /api/runs and fleet runs) and writes the
    RESOLVED specs, together with the raw launch model, into the reservation
    (``control/owner.json``) verbatim -- then nothing rewrites that record.
    That record is the R8 launch lock's carrier, so this fake stores the
    same resolved shape (via the production normalizer, not a
    reimplementation) and keeps the raw launch model untouched.
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


def _setup_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    trio_model: str | None = None,
    kimi_max: int = 2,
) -> tuple[QueueStore, _RecordingSupervisor, Path, SettingsRuntime, Launcher]:
    """Launcher + file store + throwaway settings DB seeded from a tmp config.

    The DB trio row for kimi starts with ``trio_model`` and ``kimi_max``;
    later admin edits simulate hot DB changes via ``_db_update`` between
    ticks of the SAME launcher (no restart), exactly like the R3 tests.
    """

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _RecordingSupervisor(root)
    db_path = _settings_env(tmp_path, monkeypatch)
    config_path = tmp_path / "config.toml"
    _write_project_config(config_path, model=trio_model, kimi_max=kimi_max)
    _patch_project_config(tmp_path, monkeypatch, config_path)
    runtime = SettingsRuntime(store=SettingsStore(db_path, "k" * 40), error=None)
    launcher = Launcher(supervisor, store, queue_config=default_queue_config())
    return store, supervisor, db_path, runtime, launcher


def _launch(launcher: Launcher, store: QueueStore, entry: Any) -> tuple[Any, dict[str, Any]]:
    """Tick once, assert the entry launched, and return (entry, owner record)."""

    asyncio.run(launcher.tick())
    updated = store.get(entry.queue_id)
    assert updated is not None, entry.queue_id
    assert updated.status == "launched", (updated.status, updated.skip_reasons, updated.reason)
    assert updated.run_id
    return updated, _supervisor_owner(launcher, updated.run_id)


def _supervisor_owner(launcher: Launcher, run_id: str) -> dict[str, Any]:
    owner = launcher.supervisor._owners[run_id]  # the reservation-shaped record
    return json.loads(json.dumps(owner))  # snapshot: later passes must not rewrite it


def _edit_pending_model(store: QueueStore, queue_id: str, model: str | None) -> None:
    """The R7 edit endpoint's store-level write path, network-free.

    ``PUT /api/queue/{queue_id}/model`` (settings_routes.py) validates with
    the R6 helpers, mutates the still-pending entry, and ``update``s the
    store -- with an HTTP 409 before any of this once the entry has left
    pending (the endpoint's 409 is covered by the R7 suite itself).  This
    helper runs the same validators against the same fake catalog the R6/R7
    suites use, so the launcher consumes exactly what the endpoint would
    have persisted.
    """

    entry = store.get(queue_id)
    assert entry is not None and entry.status == "pending"
    cleaned = queue_module._validate_model_override(model)
    if cleaned is not None:
        queue_module._validate_model_live(cleaned, _fake_model_catalog)
    entry.model = cleaned
    store.update(entry)


def test_edited_pending_entry_launches_with_edited_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, supervisor, _db_path, runtime, launcher = _setup_launcher(
        tmp_path, monkeypatch, trio_model="model-from-trio"
    )
    # R6 mechanism: enqueued WITH an override, validated at enqueue.  The
    # launcher launches at most one entry per pass (the batch bookkeeping in
    # _run_pass), so the two entries take one tick each in priority order.
    enqueued = store.create(
        {**_base_entry(trio="kimi", priority=10, workspace="./w1"), "model": "opus"},
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )
    # R7 mechanism: enqueued plain, then edited while pending.
    edited = store.create(_base_entry(trio="kimi", priority=5, workspace="./w2"))
    _edit_pending_model(store, edited.queue_id, "sonnet")

    asyncio.run(launcher.tick())
    enqueued_entry, edited_entry = store.get(enqueued.queue_id), store.get(edited.queue_id)
    assert enqueued_entry is not None and edited_entry is not None
    assert enqueued_entry.status == "launched" and edited_entry.status == "pending"
    asyncio.run(launcher.tick())
    edited_entry = store.get(edited.queue_id)
    assert edited_entry is not None and edited_entry.status == "launched"

    assert len(supervisor.created) == 2
    for entry, expected in ((enqueued_entry, "opus"), (edited_entry, "sonnet")):
        owner = _supervisor_owner(launcher, entry.run_id)
        # The override won over the trio default on the run record ...
        assert owner["model"] == expected
        # ... in every role (a bare model override, no roles dict, is the
        # legacy spec shape carrying THIS launch's resolved model).
        for role in ("manager", "executor", "auditor"):
            assert owner["role_configs"][role] == {"agent": "claude_code", "model": expected}


def test_no_override_launches_with_trio_default_current_at_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _supervisor, db_path, _runtime, launcher = _setup_launcher(
        tmp_path, monkeypatch, trio_model="model-at-enqueue"
    )
    entry = store.create(_base_entry(trio="kimi", workspace="./w1"))
    assert entry.model is None and entry.roles is None

    # The admin changes the trio default while the entry waits; the launch
    # must take the value CURRENT at launch, not the value at enqueue.
    _db_update(db_path, "UPDATE queue_trio SET model = ? WHERE name = 'kimi'", ("model-at-launch",))

    launched, owner = _launch(launcher, store, entry)
    assert owner["model"] == "model-at-launch"
    for role in ("manager", "executor", "auditor"):
        assert owner["role_configs"][role] == {"agent": "claude_code", "model": "model-at-launch"}
    assert launched.status == "launched"


def test_trio_change_after_launch_never_alters_running_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _supervisor, db_path, _runtime, launcher = _setup_launcher(
        tmp_path, monkeypatch, trio_model="model-a"
    )
    first_entry = store.create(_base_entry(trio="kimi", workspace="./w1"))
    first, first_owner = _launch(launcher, store, first_entry)
    assert first_owner["model"] == "model-a"

    # Administrator changes the trio default AFTER the launch; two further
    # passes run -- one that launches a second entry (which must take the
    # new default) and one with nothing to launch at all.
    _db_update(db_path, "UPDATE queue_trio SET model = ? WHERE name = 'kimi'", ("model-b",))
    second = store.create(_base_entry(trio="kimi", workspace="./w2"))
    _, second_owner = _launch(launcher, store, second)
    assert second_owner["model"] == "model-b"
    asyncio.run(launcher.tick())  # another full pass: no eligible entries

    # The running run's recorded model/roles are the ones it started with:
    # byte-identical to the snapshot taken at launch, across later passes
    # and a later trio default change.
    assert _supervisor_owner(launcher, first.run_id) == first_owner
    assert _supervisor_owner(launcher, first.run_id)["model"] == "model-a"
    assert store.get(first.queue_id).status == "launched"


def test_dict_roles_resolve_at_launch_and_follow_hot_reload_until_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _supervisor, db_path, runtime, launcher = _setup_launcher(
        tmp_path, monkeypatch, trio_model="trio-model-1"
    )
    pins = {"executor": {"model": "codex-mini"}, "auditor": {"mcp_profile": "audit"}}
    body = {**_base_entry(trio="kimi", workspace="./w1"), "roles": pins}
    first = store.create(body, model_fetch=_fake_model_catalog, settings_runtime=runtime)
    # Pins persist UNRESOLVED (R6 semantics): the trio's agent/default model
    # are not baked into the entry.
    assert first.roles == pins and first.model is None

    first_launched, first_owner = _launch(launcher, store, first)
    first_roles = first_owner["role_configs"]
    # The pin won for executor; every unpinned field inherited THIS pass's
    # trio values through create_run's own normalizer.
    assert first_roles["executor"] == {"agent": "claude_code", "model": "codex-mini"}
    assert first_roles["manager"] == {"agent": "claude_code", "model": "trio-model-1"}
    assert first_roles["auditor"] == {"agent": "claude_code", "model": "trio-model-1"}
    # Profile pins never reach the run specs (the worker round-trips
    # agent/model/reasoning_effort only; see launcher._launch_role_configs).
    assert "mcp_profile" not in first_roles["auditor"]

    # Trio default changes after the launch: the SAME pins enqueued again
    # still resolve at launch, so unpinned roles follow the hot-reload...
    _db_update(db_path, "UPDATE queue_trio SET model = ? WHERE name = 'kimi'", ("trio-model-2",))
    second = store.create(
        {**_base_entry(trio="kimi", workspace="./w2"), "roles": pins},
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )
    second_launched, second_owner = _launch(launcher, store, second)
    assert second_owner["role_configs"]["executor"] == {"agent": "claude_code", "model": "codex-mini"}
    assert second_owner["role_configs"]["manager"] == {"agent": "claude_code", "model": "trio-model-2"}
    assert second_launched.run_id
    # ...while the first run keeps exactly what it started with.
    assert _supervisor_owner(launcher, first_launched.run_id) == first_owner


def test_legacy_string_roles_launch_specs_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Empty settings tables + a missing bootstrap config: the launch uses the
    # bootstrap config's pure defaults, as before (R3's fallback pattern).
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    store = QueueStore(root)
    supervisor = _RecordingSupervisor(root)
    _settings_env(tmp_path, monkeypatch)
    _patch_project_config(tmp_path, monkeypatch, tmp_path / "missing.toml")
    config = default_queue_config()
    config["capacity"]["kimi_max"] = 2
    launcher = Launcher(supervisor, store, queue_config=config)

    plain = store.create(_base_entry(trio="kimi", priority=10, workspace="./w1"))
    aliased = store.create(
        {**_base_entry(trio="kimi", priority=5, workspace="./w2"), "roles": "kimi"}
    )
    assert aliased.trio == "kimi" and aliased.model is None and aliased.roles is None

    # One launch per pass: two ticks, one entry each, in priority order.
    asyncio.run(launcher.tick())
    asyncio.run(launcher.tick())

    plain_entry, aliased_entry = store.get(plain.queue_id), store.get(aliased.queue_id)
    assert plain_entry is not None and aliased_entry is not None
    assert plain_entry.status == aliased_entry.status == "launched"
    plain_owner = _supervisor_owner(launcher, plain_entry.run_id)
    aliased_owner = _supervisor_owner(launcher, aliased_entry.run_id)
    # Legacy behaviour byte-identical for the launch parameters: the string
    # roles alias produced the same run-record model and the same resolved
    # role specs as the plain trio entry, with no per-task fields involved.
    assert aliased_owner["model"] == plain_owner["model"]
    assert aliased_owner["role_configs"] == plain_owner["role_configs"]
