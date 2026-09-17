"""Stalled-episode detection: a hung round fails FAST with a runtime signal.

The 09-16 fleet correction measured run 20260916T065253Z_7784478f: six of nine
executor rounds burned the full 3600 s wall and returned ``duration_ms`` ≈
3600xxx with an EMPTY ``runtime_signals`` array, interleaved with 76 s / 1299 s
/ 1913 s completions on the SAME workspace and role.  That bimodal split is a
hang signature, not a slowness signature, and it proves a discriminator is
available in the data.

THE SIGNAL, stated plainly: OUTPUT PROGRESS.  A working agent CLI emits
stream-json records continuously; a hung one stops emitting entirely.  The
watchdog therefore kills a child after a configurable silent window
(``EpisodeBudget.stall_seconds``, or a conservative quarter of the episode
budget floored at 120 s and capped at 900 s -- 900 s against the 3600 s wall)
when neither stdout nor stderr produces a single byte.  The failed episode
carries a ``NO_OUTPUT_STALL`` runtime signal, the opposite of the empty
runtime_signals / ~3600xxx ms shape the hangs used to leave behind.

These tests are hermetic: real local subprocesses, tmp paths, no network, no
Postgres.
"""

from __future__ import annotations

import asyncio

import pytest

from lh_harness.adapters.cli_agent import (
    DEFAULT_STALL_SECONDS_CAP,
    CommandAgentAdapter,
    NO_OUTPUT_STALL_SIGNAL,
    _stall_window,
)
from lh_harness.environment.local import LocalEnvironment
from lh_harness.provider_errors import classify_agent_runtime_failure
from lh_harness.runtime_signals import hard_signal_labels
from lh_harness.types import EpisodeBudget


def test_hung_episode_fails_fast_with_runtime_signal() -> None:
    """A silent child is killed at the stall window, not at the 3600 s wall."""
    adapter = CommandAgentAdapter(command_template="sleep 300", workspace_path=".")
    budget = EpisodeBudget(max_duration_seconds=3600, stall_seconds=1)
    result = asyncio.run(adapter.run_episode("prompt", LocalEnvironment(), budget))

    # Fails FAST: the wall would be 3600000-ish ms; the watchdog must end the
    # episode at the silent window instead.
    assert result.duration_ms < 60_000
    assert result.duration_ms < 3_000_000
    assert result.status == "error"
    assert result.error is not None and "stall" in result.error.lower()
    assert result.metadata["termination_reason"] == "stall"
    # The runtime signal is NON-EMPTY, with the NO_OUTPUT_STALL label that
    # downstream classification keys on.
    signals = result.metadata["runtime_signals"]
    assert signals, "a stalled episode must carry a runtime signal"
    assert any(
        item.get("signal") == NO_OUTPUT_STALL_SIGNAL for item in signals if isinstance(item, dict)
    )
    assert hard_signal_labels(signals) == [NO_OUTPUT_STALL_SIGNAL]


def test_stalled_episode_classifies_as_provider_stall() -> None:
    """provider_stall is the abort reason the queue-level retry keys on."""
    adapter = CommandAgentAdapter(command_template="sleep 300", workspace_path=".")
    budget = EpisodeBudget(max_duration_seconds=3600, stall_seconds=1)
    result = asyncio.run(adapter.run_episode("prompt", LocalEnvironment(), budget))
    failure = classify_agent_runtime_failure(result)
    assert failure is not None
    assert failure.kind == "stall"
    assert failure.abort_reason == "provider_stall"


def test_progressing_episode_is_not_stalled() -> None:
    """A slowly-working child (output keeps flowing) is left alone.

    This is the other half of the discriminator: output progress feeds the
    watchdog, so wall-clock slowness alone must not kill an episode.
    """
    adapter = CommandAgentAdapter(
        command_template="for i in 1 2 3; do echo tick$i; sleep 0.3; done",
        workspace_path=".",
    )
    budget = EpisodeBudget(max_duration_seconds=30, stall_seconds=1)
    result = asyncio.run(adapter.run_episode("prompt", LocalEnvironment(), budget))
    assert result.status == "done"
    assert result.metadata["termination_reason"] != "stall"
    assert result.metadata["runtime_signals"] == []


def test_stall_window_derivation() -> None:
    """Quarter of the budget, floored at 120 s, capped at 900 s; 0 disables."""
    assert _stall_window(EpisodeBudget(max_duration_seconds=3600)) == DEFAULT_STALL_SECONDS_CAP
    assert _stall_window(EpisodeBudget(max_duration_seconds=1800)) == 450.0
    assert _stall_window(EpisodeBudget(max_duration_seconds=300)) == 120.0
    assert _stall_window(EpisodeBudget(max_duration_seconds=3600, stall_seconds=0)) is None
    assert _stall_window(EpisodeBudget(max_duration_seconds=3600, stall_seconds=45)) == 45.0
    with pytest.raises(ValueError, match="stall_seconds must be non-negative"):
        EpisodeBudget(max_duration_seconds=3600, stall_seconds=-1)


def test_watchdog_disabled_by_default_keeps_legacy_semantics() -> None:
    """exec() without a stall window must behave exactly as before."""
    env = LocalEnvironment()
    result = asyncio.run(env.exec("echo legacy-ok", timeout=10))
    assert result.termination_reason is None
    assert result.exit_code == 0
    assert "legacy-ok" in result.stdout


if __name__ == "__main__":
    pytest.main([__file__, "-v"])