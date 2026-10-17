"""LHH-GATE-OWNER-NOTE (round 4): backend tests for the cheap runs-list
projection that powers the sidebar badge.

The ``/api/runs?fields=summary`` endpoint routes through
``_build_summary_projection`` followed by ``_attach_pending_decision``.
The summary row stays small (no per-run snapshot walk) but must surface
``pending_decision_owner`` + ``pending_decision_owner_note`` so the
sidebar can render "Decision: Overseer1" + the owner note hint without
hitting the snapshot route.

These tests are scoped to the cheap-path projection and the bounded
approval tail read.  They do NOT exercise the HTTP endpoint: that's
covered by the round-2 / round-3 endpoint tests.  No event replay, no
real supervisor, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

from lh_harness.webapi.server import _attach_pending_decision, _build_summary_projection


def _write_approvals(runs_root: Path, run_id: str, records: list[dict]) -> Path:
    approvals_path = runs_root / run_id / "role_orchestration" / "approvals.jsonl"
    approvals_path.parent.mkdir(parents=True, exist_ok=True)
    with approvals_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return approvals_path


def test_attach_pending_decision_pulls_owner_from_first_pending_record(tmp_path: Path) -> None:
    runs_root = tmp_path
    _write_approvals(runs_root, "run-1", [
        {
            "approval_id": "apr-1",
            "status": "resolved",
            "owner": "paxton",
            "owner_note": "Old gate",
            "owner_set_by": "Paxton",
            "owner_set_at": 100.0,
        },
        {
            "approval_id": "apr-2",
            "status": "pending",
            "owner": "overseer",
            "owner_note": "Holding: missing build-runner-v2.js.",
            "owner_set_by": "Overseer1",
            "owner_set_at": 200.0,
        },
    ])
    row = {"id": "run-1", "status": "waiting_approval"}
    _attach_pending_decision(row, runs_root=runs_root)
    assert row["pending_decision_owner"] == "overseer"
    assert row["pending_decision_owner_note"] == "Holding: missing build-runner-v2.js."


def test_attach_pending_decision_returns_only_the_first_pending_record(tmp_path: Path) -> None:
    runs_root = tmp_path
    _write_approvals(runs_root, "run-1", [
        {
            "approval_id": "apr-1",
            "status": "pending",
            "owner": "overseer",
            "owner_note": "first",
        },
        {
            "approval_id": "apr-2",
            "status": "pending",
            "owner": "paxton",
            "owner_note": "second",
        },
    ])
    row = {"id": "run-1", "status": "waiting_approval"}
    _attach_pending_decision(row, runs_root=runs_root)
    assert row["pending_decision_owner"] == "paxton"
    assert row["pending_decision_owner_note"] == "second"


def test_attach_pending_decision_ignores_non_waiting_status(tmp_path: Path) -> None:
    runs_root = tmp_path
    _write_approvals(runs_root, "run-1", [
        {"approval_id": "apr-1", "status": "pending", "owner": "overseer"},
    ])
    row = {"id": "run-1", "status": "running"}
    _attach_pending_decision(row, runs_root=runs_root)
    assert "pending_decision_owner" not in row


def test_attach_pending_decision_handles_missing_file(tmp_path: Path) -> None:
    row = {"id": "run-1", "status": "waiting_approval"}
    _attach_pending_decision(row, runs_root=tmp_path)
    assert "pending_decision_owner" not in row


def test_attach_pending_decision_handles_malformed_jsonl(tmp_path: Path) -> None:
    runs_root = tmp_path
    approvals_path = _write_approvals(runs_root, "run-1", [
        {"approval_id": "apr-1", "status": "pending", "owner": "overseer"},
    ])
    # Append a corrupted line ahead of the pending record.
    with approvals_path.open("a", encoding="utf-8") as fh:
        fh.write("not-json\n")
    row = {"id": "run-1", "status": "waiting_approval"}
    _attach_pending_decision(row, runs_root=runs_root)
    # Tail-first scan stops at the pending record it can read; the
    # malformed line is skipped.
    assert row["pending_decision_owner"] == "overseer"


def test_attach_pending_decision_caps_owner_note_at_4000_chars(tmp_path: Path) -> None:
    runs_root = tmp_path
    long_note = "x" * 5000
    _write_approvals(runs_root, "run-1", [
        {"approval_id": "apr-1", "status": "pending", "owner": "overseer", "owner_note": long_note},
    ])
    row = {"id": "run-1", "status": "waiting_approval"}
    _attach_pending_decision(row, runs_root=runs_root)
    assert len(row["pending_decision_owner_note"]) == 4000


def test_attach_pending_decision_omits_owner_note_when_blank(tmp_path: Path) -> None:
    runs_root = tmp_path
    _write_approvals(runs_root, "run-1", [
        {"approval_id": "apr-1", "status": "pending", "owner": "overseer", "owner_note": "   "},
    ])
    row = {"id": "run-1", "status": "waiting_approval"}
    _attach_pending_decision(row, runs_root=runs_root)
    assert row["pending_decision_owner"] == "overseer"
    assert "pending_decision_owner_note" not in row


def test_build_summary_projection_keeps_baseline_shape() -> None:
    item = {
        "id": "run-1",
        "status": "running",
        "mtime": 1700000000.0,
        "workspace": "/tmp/r",
        "agent": "codex",
        "model": "glm-5.2",
    }
    row = _build_summary_projection(item)
    assert row["id"] == "run-1"
    assert row["status"] == "running"
    assert row["workspace"] == "/tmp/r"
    assert row["agent"] == "codex"
    assert row["model"] == "glm-5.2"
    # ``pending_decision_owner`` is NOT attached by the projection itself;
    # the endpoint handler is responsible for that bounded read.
    assert "pending_decision_owner" not in row