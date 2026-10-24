"""Node-level active-run ceiling (``[queue] node_max_runs``).

The launcher has always enforced the PER-TRIO caps in ``[queue.capacity]``.
Those caps bound one lane each, so their SUM can exceed the maximum this node
advertises to the fleet registry as ``maxConcurrentRuns`` — on 2026-10-22 ct110
carried 11 active runs against an advertised maximum of 5 (per-trio caps at the
time: zai 4, mm31f 5, qwen 2, glm 1, zaiq 1).  These tests pin the node-wide
refusal: the pass must stop launching at ``node_max_runs`` active runs, leave
the refused entry pending with a node-capacity reason, and keep the per-trio
refusal working when the node still has room (the tighter of the two wins).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from lh_harness.config import DEFAULT_NODE_MAX_RUNS
from lh_harness.launcher import Launcher
from lh_harness.queue import default_queue_config

from .test_launcher import FakeSupervisor, _base_entry, _fixture


def _config(node_max_runs: int | None, **capacity: int) -> dict:
    config = default_queue_config()
    if node_max_runs is None:
        config.pop("node_max_runs", None)
    else:
        config["node_max_runs"] = node_max_runs
    config["capacity"].update(capacity)
    return config


def test_launcher_refuses_past_node_maximum_and_entry_stays_pending(tmp_path: Path) -> None:
    """Node at its advertised maximum: no launch, entry pending, node reason.

    The per-trio caps are deliberately LARGER than the node maximum (zai_max 5
    against node_max_runs 2) — exactly the ct110 shape — so the per-trio gate
    would happily launch. Only the node ceiling can refuse here.
    """

    root, store, supervisor = _fixture(tmp_path)
    config = _config(node_max_runs=2, kimi_max=5, qwen_max=5)
    launcher = Launcher(supervisor, store, queue_config=config)
    assert launcher._node_max_runs() == 2

    # Two runs already active on this node, in workspaces the new entry does
    # not touch (so only the node ceiling can refuse it).
    supervisor.add_run("active-1", "./busy-1", status="running", trio="kimi")
    supervisor.add_run("active-2", "./busy-2", status="running", trio="qwen")

    entry = store.create(_base_entry(trio="kimi", workspace="./free"))
    asyncio.run(launcher.tick())

    refused = store.get(entry.queue_id)
    assert refused is not None
    assert refused.status == "pending"
    assert any(
        "node at capacity: 2 active runs, node max 2" in reason
        for reason in refused.skip_reasons
    ), refused.skip_reasons
    # The trio itself had room: this is the node check, not the per-trio one.
    assert not any("kimi at capacity" in reason for reason in refused.skip_reasons)
    assert supervisor.created == []


def test_node_refusal_counts_launches_made_earlier_in_the_same_pass(tmp_path: Path) -> None:
    """A launch this pass raises the node count for the rest of that pass.

    Starting from one active run with ``node_max_runs = 2``: the first pending
    entry launches (node now 2 of 2) and the second must be refused by the node
    ceiling even though its own trio still has capacity left.
    """

    root, store, supervisor = _fixture(tmp_path)
    config = _config(node_max_runs=2, kimi_max=5, qwen_max=5)
    launcher = Launcher(supervisor, store, queue_config=config)

    supervisor.add_run("active-1", "./busy", status="running", trio="qwen")

    head = store.create(_base_entry(trio="kimi", priority=10, workspace="./w1"))
    tail = store.create(_base_entry(trio="kimi", priority=1, workspace="./w2"))
    asyncio.run(launcher.tick())

    assert store.get(head.queue_id).status == "launched"
    assert len(supervisor.created) == 1
    tail_entry = store.get(tail.queue_id)
    assert tail_entry is not None
    assert tail_entry.status == "pending"
    assert any(
        "node at capacity: 2 active runs, node max 2" in reason
        for reason in tail_entry.skip_reasons
    ), tail_entry.skip_reasons


def test_per_trio_refusal_still_refuses_when_node_has_room(tmp_path: Path) -> None:
    """The tighter of the two wins: a full trio refuses under node headroom."""

    root, store, supervisor = _fixture(tmp_path)
    # Node ceiling well clear of the one active run; the trio cap is zero.
    config = _config(node_max_runs=5, kimi_max=0, qwen_max=5)
    launcher = Launcher(supervisor, store, queue_config=config)

    supervisor.add_run("active-1", "./busy", status="running", trio="qwen")

    entry = store.create(_base_entry(trio="kimi", workspace="./free"))
    asyncio.run(launcher.tick())

    refused = store.get(entry.queue_id)
    assert refused is not None
    assert refused.status == "pending"
    assert any("kimi at capacity" in reason for reason in refused.skip_reasons)
    assert not any("node at capacity" in reason for reason in refused.skip_reasons)
    assert supervisor.created == []


def test_node_max_comes_from_one_config_value_and_defaults_when_unset(tmp_path: Path) -> None:
    """One configuration value decides the node maximum; unset -> default."""

    root, store, supervisor = _fixture(tmp_path)

    unset = Launcher(supervisor, store, queue_config=_config(node_max_runs=None))
    assert unset._node_max_runs() == DEFAULT_NODE_MAX_RUNS == 5

    configured = Launcher(
        supervisor, store, queue_config=_config(node_max_runs=7, kimi_max=5)
    )
    assert configured._node_max_runs() == 7
    # The key is the node ceiling only: it never lands in the per-trio table.
    assert "node_max_runs" not in configured._config["capacity"]
    assert configured._config["capacity"]["kimi_max"] == 5


def test_node_max_runs_default_is_fixed(tmp_path: Path) -> None:
    """A default run config carries the documented default and no other value.

    Guards the "numbers are Paxton's call" boundary: this PR must not move any
    existing per-trio cap while adding the node key.
    """

    config = default_queue_config()
    assert config["node_max_runs"] == 5
    assert config["capacity"]["kimi_max"] == 3
    assert config["capacity"]["qwen_max"] == 1
    assert config["capacity"]["glm_max"] == 1
    assert config["capacity"]["zai_max"] == 5
    assert config["capacity"]["zaiq_max"] == 1
    assert config["capacity"]["minimax_max"] == 2
