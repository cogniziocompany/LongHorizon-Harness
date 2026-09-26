"""Run-dir experience store: append-only JSONL, dedupe, caps, no-follow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lh_harness.experience.store import (
    EXPERIENCE_FILENAME,
    append_trace_records,
    content_hash,
    dedupe_key,
    experience_ledger_path,
)


def _record(run_id: str = "run-1", round_index: int = 1, **extra):
    payload = {"run_id": run_id, "round_index": round_index, "value": 0.5}
    payload.update(extra)
    return payload


def _read_records(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def test_ledger_path_sits_beside_rounds_jsonl(tmp_path: Path) -> None:
    role_dir = tmp_path / "run-9" / "lh_harness" / "role_orchestration"
    path = experience_ledger_path(role_dir)
    assert path == role_dir / EXPERIENCE_FILENAME
    # The ledger shares the directory with rounds.jsonl and report.json so the
    # ingester finds it where it already walks; it is not in any workspace.
    assert path.parent.name == "role_orchestration"


def test_content_hash_is_stable_and_excludes_itself() -> None:
    record = _record(alpha=0.7)
    digest = content_hash(record)
    assert digest == content_hash(dict(record))
    # A record that already carries a hash hashes like the same record without it.
    assert content_hash(_record(alpha=0.7, content_hash=digest)) == digest


def test_dedupe_key_requires_run_id_and_int_round_index() -> None:
    assert dedupe_key(_record()) == ("run-1", 1)
    assert dedupe_key({"round_index": 1}) is None
    assert dedupe_key({"run_id": "r", "round_index": "n/a"}) is None


def test_append_creates_parents_and_writes_one_line_per_record(tmp_path: Path) -> None:
    role_dir = tmp_path / "run-1" / "lh_harness" / "role_orchestration"
    path = experience_ledger_path(role_dir)
    stats = append_trace_records(path, [_record(round_index=1), _record(round_index=2)])
    assert stats.written == 2
    records = _read_records(path)
    assert [item["round_index"] for item in records] == [1, 2]
    assert all("content_hash" in item for item in records)


def test_double_append_yields_one_record_set(tmp_path: Path) -> None:
    path = experience_ledger_path(tmp_path / "role_orchestration")
    records = [_record(round_index=1), _record(round_index=2)]
    append_trace_records(path, records)
    stats = append_trace_records(path, records)
    assert stats.written == 0
    assert stats.skipped_duplicates == 2
    assert len(_read_records(path)) == 2


def test_existing_malformed_tail_is_tolerated(tmp_path: Path) -> None:
    path = experience_ledger_path(tmp_path / "role_orchestration")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(_record(round_index=1)) + "\n('{not-json'\n" + json.dumps(_record(round_index=2))[:40],
        encoding="utf-8",
    )
    stats = append_trace_records(path, [_record(round_index=1), _record(round_index=2), _record(round_index=3)])
    # Round 1 survives via its key; the truncated round-2 line does not
    # register a key, so a fresh round 2 is appended.
    assert stats.skipped_duplicates == 1
    stored = _read_records(path)
    assert [item["round_index"] for item in stored][-2:] == [2, 3]


def test_invalid_records_are_dropped_not_written(tmp_path: Path) -> None:
    path = experience_ledger_path(tmp_path / "role_orchestration")
    stats = append_trace_records(path, [_record(), {"round_index": 2}, "not a mapping", None])
    assert stats.written == 1
    assert stats.dropped == 3


def test_oversized_record_is_dropped(tmp_path: Path, monkeypatch) -> None:
    path = experience_ledger_path(tmp_path / "role_orchestration")
    monkeypatch.setattr("lh_harness.experience.store.MAX_RECORD_BYTES", 256)
    huge = _record(state_summary="x" * 1024)
    stats = append_trace_records(path, [huge, _record()])
    assert stats.written == 1
    assert stats.dropped == 1


def test_file_size_cap_stops_growth(tmp_path: Path, monkeypatch) -> None:
    path = experience_ledger_path(tmp_path / "role_orchestration")
    monkeypatch.setattr("lh_harness.experience.store.MAX_FILE_BYTES", 512)
    big_records = [
        _record(round_index=index, filler="x" * 256)
        for index in range(1, 6)
    ]
    stats = append_trace_records(path, big_records)
    assert stats.file_capped
    assert stats.dropped
    assert stats.written >= 1
    stats2 = append_trace_records(path, big_records)
    assert stats2.written == 0


def test_symlinked_ledger_fails_closed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "experience.jsonl"
    sentinel.write_text("private\n", encoding="utf-8")
    role_dir = tmp_path / "run" / "lh_harness" / "role_orchestration"
    role_dir.mkdir(parents=True)
    link = role_dir / EXPERIENCE_FILENAME
    link.symlink_to(sentinel)

    with pytest.raises(OSError):
        append_trace_records(link, [_record()])
    # The pointed-at file is untouched.
    assert sentinel.read_text(encoding="utf-8") == "private\n"
