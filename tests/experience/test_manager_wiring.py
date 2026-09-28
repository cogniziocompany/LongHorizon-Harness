"""Finalization wiring: the manager captures experience traces only when the
gate is on, never writes the workspace, never fails a run, and leaves the
final report untouched either way."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lh_harness.adapters.claude_permissions import (
    snapshot_workspace,
    workspace_snapshot_diff,
)
from lh_harness.environment.local import LocalEnvironment
from lh_harness.experience.capture import ENV_FLAG
from lh_harness.experience.store import EXPERIENCE_FILENAME
from lh_harness.manager import run
from lh_harness.types import EpisodeResult, HarnessConfig

_PLAN = "Next: cli\n\nCurrent Task State:\nstill working\n\nPlan:\nkeep going"
_DONE = "Next: done\n\nCurrent Task State:\nall finished"
_EXECUTED = "executor made the change"
_CLEAN_AUDIT = (
    "Status: complete\n"
    "Integrity: clean\n"
    "Contract audit: aligned\n"
    "Summary:\nthe change is verified"
)


class SequencedAgent:
    """Replays a fixed reply sequence (same pattern as the resume-loop tests)."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    async def run_episode(self, prompt, _env, _budget, live_trajectory_path=None):
        self.prompts.append(str(prompt))
        reply = self._replies.pop(0) if self._replies else _DONE
        return EpisodeResult(status="done", actions_log=reply)


def _ledger_dir(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "role_orchestration"


async def _two_round_run(
    tmp_path: Path,
    *,
    workspace: Path,
    replies: list[str] | None = None,
) -> dict:
    return await run(
        task="finish the refactor",
        env=LocalEnvironment(str(tmp_path / "tmp")),
        config=HarnessConfig(
            max_total_episodes=2,
            workspace_path=str(workspace),
            harness_dir=str(tmp_path / "harness"),
            log_dir=str(tmp_path / "logs"),
        ),
        agent=SequencedAgent(replies or [_PLAN, _EXECUTED, _CLEAN_AUDIT, _DONE]),
    )


def _events(tmp_path: Path) -> list[dict]:
    events_path = _ledger_dir(tmp_path) / "events.jsonl"
    return [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _report_json(tmp_path: Path) -> dict:
    return json.loads((_ledger_dir(tmp_path) / "report.json").read_text(encoding="utf-8"))


def _without_timing(report: dict) -> dict:
    return {key: value for key, value in report.items() if key != "elapsed_seconds"}


def _read_experience(tmp_path: Path) -> list[dict]:
    path = _ledger_dir(tmp_path) / EXPERIENCE_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_flag_off_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    report = await _two_round_run(tmp_path, workspace=workspace)

    assert report["status"] == "complete"
    assert not (_ledger_dir(tmp_path) / EXPERIENCE_FILENAME).exists()
    assert not list(tmp_path.rglob(EXPERIENCE_FILENAME))
    assert not any("experience" in event["event"] for event in _events(tmp_path))


@pytest.mark.asyncio
async def test_report_content_is_identical_with_the_flag_on_or_off(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    off_dir = tmp_path / "off"
    on_dir = tmp_path / "on"
    (off_dir / "workspace").mkdir(parents=True)
    (on_dir / "workspace").mkdir(parents=True)

    await _two_round_run(off_dir, workspace=off_dir / "workspace")
    monkeypatch.setenv(ENV_FLAG, "1")
    await _two_round_run(on_dir, workspace=on_dir / "workspace")

    # report.json is finalized before the capture hook and must carry the
    # same content modulo wall-clock timing, whichever way the gate resolved.
    assert _without_timing(_report_json(off_dir)) == _without_timing(_report_json(on_dir))


@pytest.mark.asyncio
async def test_flag_on_captures_one_redacted_record_per_round(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = snapshot_workspace(str(workspace))

    report = await _two_round_run(tmp_path, workspace=workspace)

    after = snapshot_workspace(str(workspace))
    diff = workspace_snapshot_diff(before, after)
    assert diff["verifier_workspace_mutation_detected"] is False, (
        f"capture wrote into the workspace: {diff['verifier_workspace_mutations']}"
    )

    records = _read_experience(tmp_path)
    assert [item["round_index"] for item in records] == [1, 2]
    terminal = records[-1]
    # 2 of 2 rounds used ⇒ process 0; clean verdict ⇒ goal 1; no operator
    # input ⇒ satisfaction 0. R = 0.45 * 1 + 0.30 * 0 + 0.25 * 0.
    assert terminal["value"] == pytest.approx(0.45)
    assert terminal["reward_terms"] == {"goal": 1.0, "process": 0.0, "satisfaction": 0.0}
    # The terminal "done" round carries no independent auditor reflection, so
    # its weight is low; the executed round's clean audit weights high.
    assert terminal["alpha"] == pytest.approx(0.3)
    assert records[0]["alpha"] == pytest.approx(0.7)
    assert records[0]["value"] == pytest.approx(0.7 * 0.45 + 0.3 * 0.9 * 0.45)
    assert all("content_hash" in item for item in records)
    captured = next(event for event in _events(tmp_path) if event["event"] == "experience_captured")
    assert captured["records_written"] == 2
    assert captured["terminal_reward"] == pytest.approx(0.45)


@pytest.mark.asyncio
async def test_capture_failure_is_a_non_fatal_event(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        "lh_harness.experience.capture.persist_run_experience", _boom
    )

    report = await _two_round_run(tmp_path, workspace=workspace)

    assert report["status"] == "complete"
    events = _events(tmp_path)
    failure = next(event for event in events if event.get("event") == "experience_capture_failed")
    assert failure["error"] == "OSError"
    assert not (_ledger_dir(tmp_path) / EXPERIENCE_FILENAME).exists()


@pytest.mark.asyncio
async def test_env_var_off_overrides_config_on(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(ENV_FLAG, "0")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    report = await run(
        task="finish the refactor",
        env=LocalEnvironment(str(tmp_path / "tmp")),
        config=HarnessConfig(
            max_total_episodes=2,
            workspace_path=str(workspace),
            harness_dir=str(tmp_path / "harness"),
            log_dir=str(tmp_path / "logs"),
            experience=True,
        ),
        agent=SequencedAgent([_PLAN, _EXECUTED, _CLEAN_AUDIT, _DONE]),
    )

    assert report["status"] == "complete"
    assert not (_ledger_dir(tmp_path) / EXPERIENCE_FILENAME).exists()


@pytest.mark.asyncio
async def test_config_flag_enables_without_the_env_var(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    await run(
        task="finish the refactor",
        env=LocalEnvironment(str(tmp_path / "tmp")),
        config=HarnessConfig(
            max_total_episodes=2,
            workspace_path=str(workspace),
            harness_dir=str(tmp_path / "harness"),
            log_dir=str(tmp_path / "logs"),
            experience=True,
        ),
        agent=SequencedAgent([_PLAN, _EXECUTED, _CLEAN_AUDIT, _DONE]),
    )

    assert len(_read_experience(tmp_path)) == 2
