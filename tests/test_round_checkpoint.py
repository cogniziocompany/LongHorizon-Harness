"""Durable per-round checkpoints: write, read and fail-closed restore state.

The production writer is ``manager._record_round`` -> ``_round_checkpoint`` and
the restore primitive is ``lh_harness.round_checkpoint``; these tests cover the
second half of that pair (recovery-slice-c §1, §8 item 1).  They never touch a
supervisor, a worker command or a launcher: the checkpoint is read straight off
the role directory the manager owns.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from lh_harness.manager import _round_checkpoint, _round_checkpoint_path
from lh_harness.round_checkpoint import (
    MANAGED_ROUND_KIND,
    ROUND_CHECKPOINT_SCHEMA_VERSION,
    build_round_checkpoint,
    checkpoint_fingerprint,
    read_last_completed_round,
    round_checkpoint_path,
    write_round_checkpoint,
)
from lh_harness.types import MAX_ROUNDS, ManagedRound


def _record(index: int, **overrides: Any) -> ManagedRound:
    values: dict[str, Any] = {
        "round_index": index,
        "next_step": "cli",
        "plan_text": f"plan {index}",
        "executor_output": f"output {index}",
        "auditor_report": f"report {index}",
        "harness_feedback": "",
        "task_state": f"state {index}",
        "task_contract": f"contract {index}",
        "related_report_refs": [f"report-{index}.md"],
        "manager_status": {"round": index},
        "executor_status": {"ok": True},
        "auditor_status": {},
    }
    values.update(overrides)
    return ManagedRound(**values)


def _role(tmp_path: Path) -> Path:
    role = tmp_path / "role_orchestration"
    role.mkdir(parents=True)
    return role


def _write(role: Path, record: ManagedRound) -> Path:
    return write_round_checkpoint(role, record)


def _read_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# --- write/read round trip --------------------------------------------------


def test_write_then_read_round_trips_every_field(tmp_path: Path) -> None:
    role = _role(tmp_path)
    for index in (1, 2):
        _write(role, _record(index))
    record = _record(3)

    written = _write(role, record)
    restored = read_last_completed_round(role)

    assert written == role / "rounds" / "round_003" / "checkpoint.json"
    assert restored.status == "ok"
    assert restored.resumable is True
    assert restored.round == record
    assert restored.path == written
    assert restored.resume_from_round == 3
    assert restored.next_round == 4
    assert restored.completed_rounds == (1, 2, 3)


def test_write_uses_the_same_layout_and_payload_as_the_manager(tmp_path: Path) -> None:
    role = _role(tmp_path)
    record = _record(2)

    written = write_round_checkpoint(role, record)
    payload = _read_payload(written)

    # One writer definition: the manager's helper must produce the same file
    # name, the same labels and the same fingerprint as this module.
    assert written == _round_checkpoint_path(role, 2)
    assert round_checkpoint_path(role, 2) == written
    assert payload["schema_version"] == ROUND_CHECKPOINT_SCHEMA_VERSION
    assert payload["checkpoint_kind"] == MANAGED_ROUND_KIND
    assert payload["fingerprint"] == checkpoint_fingerprint(payload)
    manager_payload = _round_checkpoint(record)
    assert manager_payload["fingerprint"] == payload["fingerprint"]
    assert {
        key: value for key, value in manager_payload.items() if key != "recorded_at"
    } == {key: value for key, value in payload.items() if key != "recorded_at"}


def test_checkpoint_payload_is_exactly_the_round_record_plus_envelope(tmp_path: Path) -> None:
    role = _role(tmp_path)

    payload = _read_payload(_write(role, _record(1)))

    envelope = {"schema_version", "checkpoint_kind", "recorded_at", "fingerprint"}
    assert set(payload) - envelope == set(asdict(_record(1)))
    assert isinstance(payload["recorded_at"], float)


def test_fingerprint_ignores_the_writer_produced_fields() -> None:
    record = _record(1)
    first = build_round_checkpoint(record, recorded_at=1000.0)
    second = build_round_checkpoint(record, recorded_at=2000.0)

    assert first["recorded_at"] != second["recorded_at"]
    assert first["fingerprint"] == second["fingerprint"]


# --- resume at the last completed round -------------------------------------


def test_resume_point_is_the_last_completed_round(tmp_path: Path) -> None:
    role = _role(tmp_path)
    for index in (1, 2, 3):
        _write(role, _record(index))

    restored = read_last_completed_round(role)

    assert restored.status == "ok"
    assert restored.completed_rounds == (1, 2, 3)
    assert restored.resume_from_round == 3
    # The successor replays round 1 never and round 4 next.
    assert restored.next_round == 4
    assert restored.round is not None and restored.round.task_state == "state 3"


def test_resume_uses_the_highest_round_not_the_newest_file(tmp_path: Path) -> None:
    role = _role(tmp_path)
    # A late gate decision re-records an earlier round; the highest index is
    # still the resume point even if it was written first.
    for index in (3, 1, 2):
        _write(role, _record(index, plan_text=f"plan {index}"))
    _write(role, _record(2, plan_text="revised plan"))

    restored = read_last_completed_round(role)

    assert restored.status == "ok"
    assert restored.resume_from_round == 3
    assert restored.round is not None and restored.round.round_index == 3


def test_round_directories_without_a_checkpoint_are_ignored(tmp_path: Path) -> None:
    role = _role(tmp_path)
    _write(role, _record(1))
    # Round 2 started but never finished: its directory exists, the record does
    # not.  The resume point stays at the last *completed* round.
    (role / "rounds" / "round_002").mkdir()

    restored = read_last_completed_round(role)

    assert restored.status == "ok"
    assert restored.completed_rounds == (1,)
    assert restored.next_round == 2


def test_no_rounds_directory_is_missing_not_corrupt(tmp_path: Path) -> None:
    role = _role(tmp_path)

    restored = read_last_completed_round(role)

    assert restored.status == "missing"
    assert restored.resumable is False
    assert restored.round is None
    assert restored.next_round == 1
    assert restored.completed_rounds == ()


def test_rounds_directory_without_any_checkpoint_is_missing(tmp_path: Path) -> None:
    role = _role(tmp_path)
    (role / "rounds" / "round_001").mkdir(parents=True)

    restored = read_last_completed_round(role)

    assert restored.status == "missing"
    assert restored.next_round == 1


# --- corrupt checkpoints ----------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"[]", b'"a string"', b'{"round_index": 1', b"\xff\xfe"],
    ids=["empty", "garbage", "list", "string", "truncated", "not-utf8"],
)
def test_unreadable_checkpoint_is_corrupt(tmp_path: Path, raw: bytes) -> None:
    role = _role(tmp_path)
    path = round_checkpoint_path(role, 1)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)

    restored = read_last_completed_round(role)

    assert restored.status == "corrupt"
    assert restored.resumable is False
    assert restored.reason
    assert restored.next_round == 1


def test_tampered_round_state_is_corrupt(tmp_path: Path) -> None:
    role = _role(tmp_path)
    path = _write(role, _record(1))
    payload = _read_payload(path)
    payload["task_state"] = "state rewritten behind the harness's back"
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = read_last_completed_round(role)

    assert restored.status == "corrupt"
    assert "fingerprint mismatch" in restored.reason


def test_checkpoint_without_a_fingerprint_is_corrupt(tmp_path: Path) -> None:
    role = _role(tmp_path)
    payload = build_round_checkpoint(_record(1))
    payload.pop("fingerprint")
    path = round_checkpoint_path(role, 1)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = read_last_completed_round(role)

    assert restored.status == "corrupt"
    assert "no fingerprint" in restored.reason


def test_unusable_round_record_is_corrupt(tmp_path: Path) -> None:
    role = _role(tmp_path)
    payload = build_round_checkpoint(_record(1))
    payload["round_index"] = MAX_ROUNDS + 1
    # Re-fingerprint so the record itself, not the checksum, is what fails.
    payload["fingerprint"] = checkpoint_fingerprint(payload)
    path = round_checkpoint_path(role, 1)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = read_last_completed_round(role)

    assert restored.status == "corrupt"
    assert "unusable" in restored.reason


def test_hole_in_the_completed_prefix_is_corrupt(tmp_path: Path) -> None:
    role = _role(tmp_path)
    _write(role, _record(1))
    _write(role, _record(3))

    restored = read_last_completed_round(role)

    assert restored.status == "corrupt"
    assert "round_002 is missing" in restored.reason
    assert restored.completed_rounds == (1, 3)
    assert restored.next_round == 1


def test_a_corrupt_tail_does_not_resume_from_an_earlier_round(tmp_path: Path) -> None:
    role = _role(tmp_path)
    _write(role, _record(1))
    path = _write(role, _record(2))
    path.write_text("{", encoding="utf-8")

    restored = read_last_completed_round(role)

    assert restored.status == "corrupt"
    assert restored.round is None
    assert restored.completed_rounds == (1,)


# --- foreign checkpoints ----------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("checkpoint_kind", "some_other_product_state"),
        ("schema_version", 999),
    ],
)
def test_foreign_kind_or_schema_is_refused(tmp_path: Path, key: str, value: Any) -> None:
    role = _role(tmp_path)
    payload = build_round_checkpoint(_record(1))
    payload[key] = value
    payload["fingerprint"] = checkpoint_fingerprint(payload)
    path = round_checkpoint_path(role, 1)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = read_last_completed_round(role)

    assert restored.status == "foreign"
    assert restored.resumable is False
    assert restored.next_round == 1


def test_checkpoint_in_the_wrong_round_slot_is_foreign(tmp_path: Path) -> None:
    role = _role(tmp_path)
    payload = build_round_checkpoint(_record(5))
    path = round_checkpoint_path(role, 2)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = read_last_completed_round(role)

    assert restored.status == "foreign"
    assert "round_002" in restored.reason


def test_a_valid_checkpoint_next_to_foreign_state_is_never_resumed(tmp_path: Path) -> None:
    role = _role(tmp_path)
    _write(role, _record(1))
    payload = build_round_checkpoint(_record(2))
    payload["checkpoint_kind"] = "managed_round_v2"
    path = round_checkpoint_path(role, 2)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = read_last_completed_round(role)

    assert restored.status == "foreign"
    assert restored.round is None


def test_unrelated_files_in_the_rounds_tree_are_ignored(tmp_path: Path) -> None:
    role = _role(tmp_path)
    _write(role, _record(1))
    rounds = role / "rounds"
    (rounds / "round_notes").mkdir()
    (rounds / "round_notes" / "checkpoint.json").write_text("{}", encoding="utf-8")
    (rounds / "round_002.txt").write_text("not a round", encoding="utf-8")
    (rounds / "round_0").mkdir()

    restored = read_last_completed_round(role)

    assert restored.status == "ok"
    assert restored.completed_rounds == (1,)


# --- agreement with the existing fail-closed validator ----------------------


def test_the_supervisor_validator_accepts_what_this_writer_produces(tmp_path: Path) -> None:
    """The two halves of the restore path must not drift apart.

    ``RunSupervisor._validate_latest_round_checkpoint`` is what a continuation
    runs before it trusts the round state; the reader here decides *which*
    round is the resume point.  Both recompute the same fingerprint, so a file
    written by either half must satisfy the other.
    """

    from lh_harness.supervisor.service import RunSupervisor

    runs_root = tmp_path / "runs"
    run_id = "20990101T000000Z_checkpoint"
    run_dir = runs_root / run_id
    role = run_dir / "lh_harness" / "role_orchestration"
    role.mkdir(parents=True)
    for index in (1, 2):
        _write(role, _record(index))

    supervisor = RunSupervisor(runs_root, workspace_root=tmp_path)
    # Must not raise: the file this module writes is the file the continuation
    # path validates.
    supervisor._validate_latest_round_checkpoint(run_id, run_dir)

    restored = read_last_completed_round(role)
    assert restored.status == "ok"
    assert restored.next_round == 3

    # A tampered checkpoint must be refused by both halves, not just this one.
    path = round_checkpoint_path(role, 2)
    payload = _read_payload(path)
    payload["task_state"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        supervisor._validate_latest_round_checkpoint(run_id, run_dir)
    assert read_last_completed_round(role).status == "corrupt"


# --- the production writer path ---------------------------------------------


def test_the_manager_record_round_path_writes_a_readable_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the real ``manager._record_round``, not just this module's writer.

    ``_record_round`` is the only place a finished round is persisted; the
    restore reader must accept exactly what that path produces.  The remote and
    fleet side effects are stubbed because this checkout has no task VM and no
    fleet endpoint.
    """

    from lh_harness import manager

    async def fake_remote(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(manager, "_write_remote_round_text", fake_remote)

    role = _role(tmp_path)
    config = manager.HarnessConfig(
        log_dir=str(tmp_path),
        runs_root=str(tmp_path / "runs"),
        harness_dir=str(tmp_path / "remote"),
    )
    for index in (1, 2):
        asyncio.run(
            manager._record_round(
                env=object(),
                config=config,
                role_dir=role,
                events_path=role / "events.jsonl",
                record=_record(index),
            )
        )

    restored = read_last_completed_round(role)

    assert restored.status == "ok"
    assert restored.completed_rounds == (1, 2)
    assert restored.next_round == 3
    assert restored.round is not None and restored.round.task_state == "state 2"
    # The ledger and the checkpoint must agree on the same round.
    ledger = [
        json.loads(line)
        for line in (role / "rounds.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [entry["round_index"] for entry in ledger] == [1, 2]
    assert restored.round.plan_text == ledger[-1]["plan_text"]
