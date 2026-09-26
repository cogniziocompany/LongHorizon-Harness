"""Capture entry point: reward/alpha/backfill assembly, redaction before
write, dedupe across captures, no-signal cancellation, tolerant ledger reads,
and the feature gate."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from lh_harness.experience.capture import (
    ENV_FLAG,
    experience_enabled,
    persist_run_experience,
    run_dir_for_log_dir,
)
from lh_harness.config import ProjectConfigError, load_run_defaults
from lh_harness.experience.store import EXPERIENCE_FILENAME
from lh_harness.types import HarnessConfig, ManagedRound

_CLEAN_AUDIT = (
    "Status: complete\n"
    "Integrity: clean\n"
    "Contract audit: aligned\n"
    "Blocking constraints: none\n"
)


def _round(index: int, **overrides) -> ManagedRound:
    record = ManagedRound(
        round_index=index,
        next_step="cli",
        plan_text=f"plan {index}",
        executor_output=f"output {index}",
        auditor_report=_CLEAN_AUDIT,
        harness_feedback="",
        task_state=f"state {index}",
        task_contract=f"contract {index}",
    )
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


def _run_dir(tmp_path: Path, *, owner: dict | None = None) -> tuple[Path, Path, Path]:
    run_dir = tmp_path / "run-abc123"
    role_dir = run_dir / "lh_harness" / "role_orchestration"
    role_dir.mkdir(parents=True)
    control = run_dir / "control"
    control.mkdir()
    (control / "owner.json").write_text(
        json.dumps(
            owner
            if owner is not None
            else {
                "run_id": "run-abc123",
                "workspace": "/home/harness/work/LongHorizon-Harness",
                "role_configs": {
                    "manager": {"agent": "claude_code", "model": "model-m"},
                    "executor": {"agent": "codex", "model": "model-e"},
                    "auditor": {"agent": "claude_code", "model": "model-a"},
                },
            }
        ),
        encoding="utf-8",
    )
    return run_dir, run_dir / "lh_harness", role_dir


def _complete_report(rounds_run: int = 3) -> dict:
    return {
        "status": "complete",
        "task": "do the refactor",
        "completion_satisfied": True,
        "rounds_run": rounds_run,
        "max_rounds": 4,
        "abort_reason": "",
        "rounds": [],
    }


def _read_records(role_dir: Path) -> list[dict]:
    path = role_dir / EXPERIENCE_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- feature gate --------------------------------------------------------


def test_gate_is_off_by_default() -> None:
    assert experience_enabled(HarnessConfig(), env={}) is False
    assert experience_enabled(None, env={}) is False


def test_gate_reads_config_flag_and_env_override() -> None:
    config = HarnessConfig(experience=True)
    assert experience_enabled(config, env={}) is True
    assert experience_enabled(config, env={ENV_FLAG: "0"}) is False
    config = HarnessConfig()
    assert experience_enabled(config, env={ENV_FLAG: "1"}) is True
    assert experience_enabled(config, env={ENV_FLAG: "true"}) is True
    # An empty env var is unset; a non-truthy value disables explicitly.
    assert experience_enabled(HarnessConfig(experience=True), env={ENV_FLAG: ""}) is True
    assert experience_enabled(HarnessConfig(experience=True), env={ENV_FLAG: "nope"}) is False


def test_run_config_parses_experience_boolean(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[run]\nexperience = true\n", encoding="utf-8")
    assert load_run_defaults(config_path)["experience"] is True
    config_path.write_text("[run]\nexperience = false\n", encoding="utf-8")
    assert load_run_defaults(config_path)["experience"] is False
    config_path.write_text("[run]\n", encoding="utf-8")
    assert "experience" not in load_run_defaults(config_path)
    config_path.write_text('[run]\nexperience = "yes"\n', encoding="utf-8")
    with pytest.raises(ProjectConfigError):
        load_run_defaults(config_path)


def test_run_dir_for_log_dir_walks_standard_layout(tmp_path: Path) -> None:
    for name in ("lh_harness", "cua_harness", "logs"):
        run_dir = tmp_path / "run-x"
        assert run_dir_for_log_dir(run_dir / name) == run_dir
    standalone = tmp_path / "custom-log-dir"
    assert run_dir_for_log_dir(standalone) == standalone


# --- capture -------------------------------------------------------------


def test_capture_writes_one_record_per_round(tmp_path: Path) -> None:
    run_dir, log_dir, role_dir = _run_dir(tmp_path)
    rounds = [_round(1), _round(2), _round(3)]
    config = HarnessConfig(log_dir=str(log_dir))

    result = persist_run_experience(rounds, _complete_report(), config, run_dir, occurred_at="2026-09-08T00:00:00+00:00")

    assert result.run_id == "run-abc123"
    assert result.rounds_captured == 3
    assert result.written == 3
    records = _read_records(role_dir)
    assert [item["round_index"] for item in records] == [1, 2, 3]
    assert all(item["run_id"] == "run-abc123" for item in records)
    assert all("content_hash" in item for item in records)
    # Tags from the owner: repo + model trio; the branch is absent here (no
    # branch in task text, no run-dir copy), never invented.
    assert all(item.get("repo") == "LongHorizon-Harness" for item in records)
    assert all("branch" not in item for item in records)
    assert records[0]["roles"]["manager"] == {"role": "manager", "agent": "claude_code", "model": "model-m"}
    # Device dimension: no remote execution was recorded, so the fields are
    # absent rather than empty strings or invented ids.
    for item in records:
        assert "device_id" not in item
        assert "terminal_id" not in item
        assert "hydra_node" not in item


def test_capture_values_are_reward_consistent(tmp_path: Path) -> None:
    run_dir, log_dir, role_dir = _run_dir(tmp_path)
    rounds = [_round(1), _round(2)]
    config = HarnessConfig(log_dir=str(log_dir))

    result = persist_run_experience(rounds, _complete_report(rounds_run=2), config, run_dir)

    records = _read_records(role_dir)
    terminal = records[-1]
    # Clean verdicts: complete status with no budget waste ⇒ R = 0.6 + 0.3*0.5.
    expected_R = 0.45 * 1.0 + 0.30 * 0.5
    assert terminal["value"] == pytest.approx(expected_R, abs=1e-6)
    assert terminal["reward_terms"] == {"goal": 1.0, "process": 0.5, "satisfaction": 0.0}
    assert terminal["alpha"] == pytest.approx(0.7)
    # Eq. 2 with alpha 0.7 and gamma 0.9 for the non-terminal round.
    first = records[0]
    assert first["value"] == pytest.approx(0.7 * expected_R + 0.3 * 0.9 * expected_R, abs=1e-6)


def test_double_capture_yields_one_record_set(tmp_path: Path) -> None:
    run_dir, log_dir, role_dir = _run_dir(tmp_path)
    rounds = [_round(1), _round(2)]
    config = HarnessConfig(log_dir=str(log_dir))
    report = _complete_report(rounds_run=2)

    persist_run_experience(rounds, report, config, run_dir)
    second = persist_run_experience(rounds, report, config, run_dir)
    assert second.written == 0
    assert second.skipped_duplicates == 2
    assert len(_read_records(role_dir)) == 2


def test_secret_in_round_output_never_reaches_the_ledger(tmp_path: Path) -> None:
    run_dir, log_dir, role_dir = _run_dir(tmp_path)
    secret = "sk-ant-" + "a1" * 24
    rounds = [_round(1), _round(2, executor_output=f"applied fix with {secret}")]
    config = HarnessConfig(log_dir=str(log_dir))

    persist_run_experience(rounds, _complete_report(rounds_run=2), config, run_dir)

    text = (role_dir / EXPERIENCE_FILENAME).read_text(encoding="utf-8")
    assert secret not in text
    assert "***REDACTED***" in text


def test_no_signal_web_cancel_records_unvalued_traces(tmp_path: Path) -> None:
    run_dir, log_dir, role_dir = _run_dir(tmp_path)
    control = run_dir / "control"
    (control / "commands.jsonl").write_text(
        json.dumps({"kind": "stop", "created_by": "web", "payload": {}}) + "\n",
        encoding="utf-8",
    )
    report = {
        "status": "cancelled",
        "task": "watch the fleet",
        "completion_satisfied": False,
        "rounds_run": 1,
        "max_rounds": 4,
        "abort_reason": "user_cancelled",
        "rounds": [],
    }
    config = HarnessConfig(log_dir=str(log_dir))

    result = persist_run_experience([_round(1)], report, config, run_dir)

    assert result.terminal_reward is None
    assert result.reward_reason == "no_signal:user_cancelled_web"
    records = _read_records(role_dir)
    assert len(records) == 1
    record = records[0]
    assert "value" not in record
    assert "reward_terms" not in record
    assert record["reward_reason"] == "no_signal:user_cancelled_web"
    # Alpha is still recorded: it describes the reflection, not the reward.
    assert record["alpha"] == pytest.approx(0.7)


def test_non_web_cancel_is_valued(tmp_path: Path) -> None:
    run_dir, log_dir, role_dir = _run_dir(tmp_path)
    report = {
        "status": "cancelled",
        "task": "watch the fleet",
        "completion_satisfied": False,
        "rounds_run": 1,
        "max_rounds": 4,
        "abort_reason": "user_cancelled",
        "rounds": [],
    }
    config = HarnessConfig(log_dir=str(log_dir))

    result = persist_run_experience([_round(1)], report, config, run_dir)

    assert result.terminal_reward is not None
    assert _read_records(role_dir)[0]["value"] is not None


def test_malformed_rounds_jsonl_tail_does_not_raise(tmp_path: Path) -> None:
    run_dir, log_dir, role_dir = _run_dir(tmp_path)
    ledger = role_dir / "rounds.jsonl"
    ledger.write_text(
        json.dumps(asdict(_round(1)), ensure_ascii=False)
        + "\n"
        + '{"round_index": 2, "broken'
        + "\n"
        + json.dumps(asdict(_round(3)), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    config = HarnessConfig(log_dir=str(log_dir))

    result = persist_run_experience(None, _complete_report(rounds_run=3), config, run_dir)

    assert result.rounds_captured == 2
    assert [item["round_index"] for item in _read_records(role_dir)] == [1, 3]


def test_missing_owner_and_ledgers_still_capture(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-solo"
    role_dir = run_dir / "lh_harness" / "role_orchestration"
    role_dir.mkdir(parents=True)
    config = HarnessConfig(log_dir=str(run_dir / "lh_harness"))

    result = persist_run_experience([_round(1)], _complete_report(rounds_run=1), config, run_dir)

    # The run id falls back to the run dir name; absent identity data stays absent.
    assert result.run_id == "run-solo"
    records = _read_records(role_dir)
    assert len(records) == 1
    assert "roles" not in records[0] or not records[0]["roles"]


def test_traces_read_tools_from_round_trajectories(tmp_path: Path) -> None:
    run_dir, log_dir, role_dir = _run_dir(tmp_path)
    round_dir = role_dir / "rounds" / "round_002"
    round_dir.mkdir(parents=True)
    (round_dir / "executor_trajectory.jsonl").write_text(
        json.dumps({"kind": "tool_use", "name": "Bash"})
        + "\n"
        + json.dumps({"kind": "tool_use", "name": "Read"})
        + "\n",
        encoding="utf-8",
    )
    config = HarnessConfig(log_dir=str(log_dir))

    persist_run_experience([_round(1), _round(2)], _complete_report(rounds_run=2), config, run_dir)

    records = _read_records(role_dir)
    assert records[0].get("tool_names") in (None, [])
    assert records[1]["tool_names"] == ["Bash", "Read"]


def test_error_signature_prefers_blockers_then_provider_abort(tmp_path: Path) -> None:
    run_dir, log_dir, role_dir = _run_dir(tmp_path)
    blocked_audit = (
        "Status: blocked\n"
        "Integrity: clean\n"
        "Contract audit: unknown\n"
        "Blocking constraints:\n"
        "- provider refused every key\n"
    )
    rounds = [_round(1), _round(2, auditor_report=blocked_audit)]
    report = {
        "status": "failed",
        "task": "do it",
        "completion_satisfied": False,
        "rounds_run": 2,
        "max_rounds": 4,
        "abort_reason": "provider_auth",
        "rounds": [],
    }
    config = HarnessConfig(log_dir=str(log_dir))

    persist_run_experience(rounds, report, config, run_dir)

    records = _read_records(role_dir)
    # Mid-run round: no blockers of its own, and the run-level abort signs the
    # terminal round, not every round.
    assert "error_signature" not in records[0]
    assert records[1]["error_signature"] == "provider refused every key"
