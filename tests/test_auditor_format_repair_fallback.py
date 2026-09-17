from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lh_harness.environment.local import LocalEnvironment
from lh_harness.manager import (
    _auditor_report_with_format_repair,
    _format_repair_budget,
)
from lh_harness.types import EpisodeBudget, EpisodeResult, HarnessConfig


class FakeTimeoutRepairAgent:
    """An AgentAdapter that always returns a timeout."""

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    async def run_episode(
        self,
        prompt: str,
        env,
        budget: EpisodeBudget,
        live_trajectory_path: str | None = None,
    ) -> EpisodeResult:
        self._calls.append(
            {"prompt": prompt, "budget": budget, "live_trajectory_path": live_trajectory_path}
        )
        return EpisodeResult(
            status="timeout",
            error="budget exhausted",
            duration_ms=120_187,
        )


class FakeErrorRepairAgent:
    """An AgentAdapter that always returns an error."""

    async def run_episode(
        self,
        prompt: str,
        env,
        budget: EpisodeBudget,
        live_trajectory_path: str | None = None,
    ) -> EpisodeResult:
        return EpisodeResult(
            status="error",
            error="backend unavailable",
            duration_ms=500,
        )


def _auditor_result(status: str, *, raw_report: str = "", duration_ms: int = 1000) -> EpisodeResult:
    """Build an EpisodeResult that surfaces *raw_report* as visible auditor text."""
    # The primary result's visible output is read from metadata keys, then actions_log.
    # Putting it in assistant_visible_output avoids format-detection surprises.
    return EpisodeResult(
        status=status,  # type: ignore[arg-type]
        metadata={"assistant_visible_output": raw_report},
        duration_ms=duration_ms,
    )


@pytest.mark.asyncio
async def test_repair_timeout_preserves_successful_auditor_report(tmp_path: Path) -> None:
    """
    When the auditor itself succeeded but the format-repair pass times out,
    the auditor's raw text must survive with an explicit 'unformatted' marker
    instead of being replaced by the blocked/suspect runtime-failure stub.
    """
    raw_text = (
        "Audit facts: the task requested a new file and the executor produced it.\n"
        "Next step: mark complete."
    )
    primary = _auditor_result("done", raw_report=raw_text, duration_ms=855_922)

    round_dir = tmp_path / "round_001"
    round_dir.mkdir(parents=True)
    events_path = tmp_path / "events.jsonl"
    harness_dir = tmp_path / "harness"
    config = HarnessConfig(harness_dir=str(harness_dir), prompt_language="en")
    env = LocalEnvironment(tmp_dir=str(tmp_path))

    calls: list[dict[str, Any]] = []
    agent = FakeTimeoutRepairAgent(calls)

    report, status = await _auditor_report_with_format_repair(
        env=env,
        config=config,
        round_dir=round_dir,
        events_path=events_path,
        format_repair_agent=agent,
        auditor_budget=EpisodeBudget(max_duration_seconds=300),
        primary_result=primary,
        round_index=1,
        episode_root=tmp_path,
    )

    # The returned report contains the raw auditor text, not the suspect stub.
    assert raw_text in report, f"Expected raw text in report; got:\n{report}"
    assert "[Unformatted auditor report" in report
    assert "Status: blocked" not in report
    assert "Integrity: suspect" not in report
    assert "Auditor runtime failed" not in report

    # The repair pass really ran, under the 120 s clamp.
    assert len(calls) == 1
    assert calls[0]["budget"].max_duration_seconds == 120

    # The recorded status notes the repair attempt and its failure.
    assert status["format_repair_attempted"] is True
    assert status["format_repair_accepted"] is False
    assert status["format_repair_status"]["status"] == "timeout"

    # Event log records the timeout with accepted=False.
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    done_event = next(e for e in events if e["event"] == "auditor_format_repair_done")
    assert done_event["accepted"] is False
    assert done_event["episode_status"]["status"] == "timeout"

    # Repair budget clamp remains exactly 120 s.
    repair_budget = _format_repair_budget(EpisodeBudget(max_duration_seconds=300))
    assert repair_budget.max_duration_seconds == 120


@pytest.mark.asyncio
async def test_repair_error_does_not_fallback_to_raw_report(tmp_path: Path) -> None:
    """
    When the repair fails with a non-timeout error, the existing blocked/suspect
    stub must still be emitted so we do not silently hide a backend failure.
    """
    raw_text = "Some raw auditor text."
    primary = _auditor_result("done", raw_report=raw_text)

    round_dir = tmp_path / "round_001"
    round_dir.mkdir(parents=True)
    events_path = tmp_path / "events.jsonl"
    harness_dir = tmp_path / "harness"
    config = HarnessConfig(harness_dir=str(harness_dir), prompt_language="en")
    env = LocalEnvironment(tmp_dir=str(tmp_path))

    report, status = await _auditor_report_with_format_repair(
        env=env,
        config=config,
        round_dir=round_dir,
        events_path=events_path,
        format_repair_agent=FakeErrorRepairAgent(),
        auditor_budget=EpisodeBudget(max_duration_seconds=300),
        primary_result=primary,
        round_index=1,
        episode_root=tmp_path,
    )

    assert status["format_repair_attempted"] is True
    assert status["format_repair_accepted"] is False
    # Because the repair errored (not timed out), the suspect stub should still appear.
    assert "Status: blocked" in report
    assert "Integrity: suspect" in report
    assert "[Unformatted auditor report" not in report
    assert raw_text not in report
