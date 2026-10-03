"""The optional ``orfree`` trio: every role on an OpenRouter free model.

It exists only when a node's config defines ``[queue.trios.orfree]``; it is
never filled in by default, so nothing launches on the free tier by accident.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lh_harness.config import ProjectConfigError, load_run_defaults
from lh_harness.launcher import Launcher
from lh_harness.queue import _normalize_request, default_queue_config, queue_config_from_config

from .test_launcher import _base_entry, _fixture

ORFREE_MODEL = "qwen3.8-27b:openrouter-free"
ORFREE_TRIO = {"agent": "claude_code", "model": ORFREE_MODEL, "mcp_profile": "ops"}
# Launcher tests leave the trio profile unset: a non-read-only trio profile is
# refused for the auditor unless [run.roles.auditor] binds a read-only one
# (docs/queue.md), which is the service's concern, not this trio's.
ORFREE_LAUNCH_TRIO = {"agent": "claude_code", "model": ORFREE_MODEL, "mcp_profile": None}


def _write_config(tmp_path: Path, *lines: str) -> Path:
    config = tmp_path / "config.toml"
    config.write_text("\n".join(lines), encoding="utf-8")
    return config


def test_orfree_is_not_a_default_trio_but_has_a_default_cap() -> None:
    config = default_queue_config()
    assert "orfree" not in config["trios"]
    assert config["capacity"]["orfree_max"] == 2


def test_enqueue_accepts_orfree() -> None:
    params = _normalize_request(
        {"name": "n", "task": "t", "workspace": "w", "trio": "orfree", "requested_by": "ci"}
    )
    assert params["trio"] == "orfree"


def test_queue_config_carries_orfree_trio_and_cap() -> None:
    config = queue_config_from_config(
        {"queue": {"trios": {"orfree": dict(ORFREE_TRIO)}, "capacity": {"orfree_max": 1}}}
    )
    assert config["trios"]["orfree"]["agent"] == "claude_code"
    assert config["trios"]["orfree"]["model"] == ORFREE_MODEL
    assert config["capacity"]["orfree_max"] == 1
    # The required trios keep their fallback specs.
    assert {"kimi", "qwen"} <= set(config["trios"])


def test_project_config_accepts_orfree_trio(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        "[queue.trios.orfree]",
        'agent = "claude_code"',
        f'model = "{ORFREE_MODEL}"',
        'mcp_profile = "ops"',
        "[queue.capacity]",
        "orfree_max = 2",
    )
    queue = load_run_defaults(config)["queue"]
    assert queue["trios"]["orfree"] == ORFREE_TRIO
    assert queue["capacity"]["orfree_max"] == 2
    assert set(queue["trios"]) == {"kimi", "qwen", "orfree"}


def test_project_config_without_orfree_does_not_invent_it(tmp_path: Path) -> None:
    config = _write_config(tmp_path, "[queue.capacity]", "kimi_max = 1")
    queue = load_run_defaults(config)["queue"]
    assert set(queue["trios"]) == {"kimi", "qwen"}
    assert queue["capacity"]["orfree_max"] == 2


def test_project_config_still_rejects_unknown_trio(tmp_path: Path) -> None:
    config = _write_config(tmp_path, "[queue.trios.gpt]", 'agent = "codex"')
    with pytest.raises(ProjectConfigError, match="unknown queue trio"):
        load_run_defaults(config)


def test_launcher_runs_all_three_roles_on_the_orfree_model(tmp_path: Path) -> None:
    _root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["trios"]["orfree"] = dict(ORFREE_LAUNCH_TRIO)
    launcher = Launcher(supervisor, store, queue_config=config)

    entry = store.create(_base_entry(trio="orfree"))
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "launched"
    assert updated.run_id is not None
    roles = supervisor.owner(updated.run_id)["role_configs"]
    assert set(roles) == {"manager", "executor", "auditor"}
    for spec in roles.values():
        assert spec == {"agent": "claude_code", "model": ORFREE_MODEL}


def test_launcher_caps_orfree_at_orfree_max(tmp_path: Path) -> None:
    _root, store, supervisor = _fixture(tmp_path)
    config = default_queue_config()
    config["trios"]["orfree"] = dict(ORFREE_LAUNCH_TRIO)
    config["capacity"]["poll_seconds"] = 1
    launcher = Launcher(supervisor, store, queue_config=config)

    entries = [
        store.create(_base_entry(trio="orfree", priority=10 - i, workspace=f"./w{i}"))
        for i in range(3)
    ]
    for _ in range(4):
        asyncio.run(launcher.tick())
        for run_id in list(supervisor._statuses):
            supervisor._statuses[run_id]["status"] = "running"

    statuses = [store.get(e.queue_id).status for e in entries]
    assert statuses == ["launched", "launched", "pending"]
    third = store.get(entries[2].queue_id)
    assert any("orfree at capacity" in reason for reason in third.skip_reasons)


def test_orfree_entry_stays_pending_when_the_node_does_not_define_the_trio(tmp_path: Path) -> None:
    _root, store, supervisor = _fixture(tmp_path)
    launcher = Launcher(supervisor, store, queue_config=default_queue_config())

    entry = store.create(_base_entry(trio="orfree"))
    asyncio.run(launcher.tick())

    updated = store.get(entry.queue_id)
    assert updated is not None
    assert updated.status == "pending"
    assert supervisor.created == []
    assert any("orfree at capacity" in reason for reason in updated.skip_reasons)
