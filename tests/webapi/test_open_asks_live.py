"""Task A3a: open asks derived live from harness state (gates, blocked queue entries)."""

from __future__ import annotations

from pathlib import Path

from lh_harness.overseer_state import list_open_asks, live_open_ask_rows

_COLUMNS = {"id", "ask", "kind", "evidence", "recommended", "default_if_silent", "state"}

_TABLE = (
    "# OPEN ASKS\n"
    "Updated 2026-10-01.\n"
    "\n"
    "| id | ask | kind | evidence | recommended | default if silent | state |\n"
    "|---|---|---|---|---|---|---|\n"
    "| 1-file-open | Decide X | DECISION | evidence | yes | nothing | open |\n"
    "| 2-file-closed | Old ask | CREDENTIAL | evidence | n/a | nothing | **CLOSED 2026-09-21** |\n"
)

_GATED = [
    {"run_id": "20261001T181200Z_5eb7003b", "approval_id": "4578da6dba6d", "title": "Push the branch?"},
    {"run_id": "", "approval_id": "skipped-no-run"},
]
_BLOCKED = [{"queue_id": "q-1", "name": "demo-task", "reason": "waiting on Paxton"}]


def _archive(tmp_path: Path, table: str | None = _TABLE) -> Path:
    root = tmp_path / "archive"
    for name in ("tasks", "queue", "docs"):
        (root / name).mkdir(parents=True)
    if table is not None:
        (root / "queue" / "OPEN-ASKS.md").write_text(table, encoding="utf-8")
    return root


def test_live_rows_have_the_seven_columns() -> None:
    rows = live_open_ask_rows(_GATED, _BLOCKED)
    assert [r["id"] for r in rows] == ["gate-20261001T181200Z_5eb7003b-4578da6dba6d", "blocked-q-1"]
    assert all(set(r) == _COLUMNS for r in rows)
    assert rows[0]["kind"] == "GATE"
    assert rows[0]["evidence"] == "/runs/20261001T181200Z_5eb7003b"
    assert "Push the branch?" in rows[0]["ask"]
    assert rows[1]["kind"] == "BLOCKED"
    assert rows[1]["evidence"] == "waiting on Paxton"
    assert {r["state"] for r in rows} == {"open"}


def test_live_rows_bound_long_text() -> None:
    rows = live_open_ask_rows([{"run_id": "r", "approval_id": "a", "message": "x" * 5000}], [])
    assert len(rows[0]["ask"]) < 260


def test_live_rows_come_first_then_open_file_rows(tmp_path: Path) -> None:
    live = live_open_ask_rows(_GATED, _BLOCKED)
    result = list_open_asks({}, overseer_root=_archive(tmp_path), live_rows=live)
    assert result["ok"] is True
    assert [r["id"] for r in result["rows"]] == [
        "gate-20261001T181200Z_5eb7003b-4578da6dba6d",
        "blocked-q-1",
        "1-file-open",
    ]
    assert result["live_rows"] == 2
    assert result["open_rows"] == 3
    assert result["total_rows"] == 4


def test_missing_file_is_not_an_error_with_live_rows(tmp_path: Path) -> None:
    live = live_open_ask_rows(_GATED, [])
    result = list_open_asks({}, overseer_root=_archive(tmp_path, table=None), live_rows=live)
    assert result["ok"] is True
    assert [r["id"] for r in result["rows"]] == ["gate-20261001T181200Z_5eb7003b-4578da6dba6d"]
    assert "OPEN-ASKS.md" in result["file_note"]


def test_missing_archive_is_not_an_error_with_live_rows(tmp_path: Path) -> None:
    result = list_open_asks({}, overseer_root=str(tmp_path / "nope"), live_rows=[])
    assert result["ok"] is True
    assert result["rows"] == []
    assert result["file_note"]


def test_without_live_rows_behaviour_is_unchanged(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    result = list_open_asks({}, overseer_root=root)
    assert [r["id"] for r in result["rows"]] == ["1-file-open"]
    assert "live_rows" not in result
    (root / "queue" / "OPEN-ASKS.md").unlink()
    missing = list_open_asks({}, overseer_root=root)
    assert missing["ok"] is False
    assert missing["code"] == 404


def test_bad_limit_is_still_400_with_live_rows(tmp_path: Path) -> None:
    result = list_open_asks({"limit": 0}, overseer_root=_archive(tmp_path), live_rows=[])
    assert result["ok"] is False
    assert result["code"] == 400


def test_limit_applies_to_the_merged_list(tmp_path: Path) -> None:
    live = live_open_ask_rows(_GATED, _BLOCKED)
    result = list_open_asks({"limit": 1}, overseer_root=_archive(tmp_path), live_rows=live)
    assert [r["id"] for r in result["rows"]] == ["gate-20261001T181200Z_5eb7003b-4578da6dba6d"]
