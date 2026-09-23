"""TASK 222: fixture tests for scripts/compare_shadow.py.

Reproduces the three real day-7 shadow-window cases from cutover task 168:

  - 9999zv-221-queue-database-url-dropped: shadow_launch at 2026-09-23 03:57:16Z,
    PC run 20260923T035722Z_d1d74675 launched at 03:57:22Z, later shadow
    self-occupancy skips  -> verdict AGREE (the post-launch self-occupied skips
    are never scored as missed).

  - 9999zv-218-queue-validator: shadow_launch at 2026-09-23 07:19:53Z,
    PC run 24b4a1ad launched at 07:19:54Z, later self-occupancy skips
    -> verdict AGREE.

  - 217-clawdtalk: PC launched before the CT110 launcher's first pass; the
    shadow only ever recorded the PC run's own occupancy -> verdict
    unobserved-race, evidence item 3.

Plus one genuine disagreement (phantom) and one tick probe.  The PC log's own
``shadow-filed <name>.json <queue_id>`` lines provide the queue-name map by
default; ``--queue-names`` is an override only.

All data in this fixture is synthetic except the three real timestamps/run_ids
and the real log-line grammar.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "compare_shadow.py"

# Load the script as a module (it lives in scripts/, not in a package).
spec = importlib.util.spec_from_file_location("compare_shadow", SCRIPT)
assert spec is not None and spec.loader is not None
cs = importlib.util.module_from_spec(spec)
sys.modules["compare_shadow"] = cs
spec.loader.exec_module(cs)

# ---------------------------------------------------------------------------
# Timestamps: PC log is in America/Los_Angeles (UTC-7).  Shadow records are UTC.
# ---------------------------------------------------------------------------


def _utc(iso: str) -> float:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()


CASE_221_NAME = "9999zv-221-queue-database-url-dropped"
CASE_221_QID = "q-221feed1234abcd"
CASE_221_RUN = "20260923T035722Z_d1d74675"
CASE_221_WORKSPACE = "/work/cognizioware-mcp-tools"
CASE_221_SHADOW_LAUNCH = _utc("2026-09-23T03:57:16")
CASE_221_PC_EXACT = _utc("2026-09-23T03:57:22")
CASE_221_SKIP_1 = _utc("2026-09-23T04:12:10")
CASE_221_SKIP_2 = _utc("2026-09-23T04:27:05")

CASE_218_NAME = "9999zv-218-queue-validator"
CASE_218_QID = "q-218feed1234abcd"
CASE_218_RUN = "24b4a1ad"
CASE_218_WORKSPACE = "/work/LongHorizon-Harness"
CASE_218_SHADOW_LAUNCH = _utc("2026-09-23T07:19:53")
CASE_218_SKIP = _utc("2026-09-23T07:34:20")

CASE_217_NAME = "217-clawdtalk"
CASE_217_QID = "q-217feed1234abcd"
CASE_217_RUN = "9f2e4c6a1b3d"
CASE_217_WORKSPACE = "/work/cognizioware-nebo"
CASE_217_PC_EXACT = _utc("2026-09-23T07:31:00")  # 00:31 PT = 07:31Z
CASE_217_SHADOW_SKIP = _utc("2026-09-23T07:46:00")  # first CT110 pass after PC launch

CASE_230_NAME = "9999zv-230-genuine-disagreement"
CASE_230_QID = "q-230feed1234abcd"
CASE_230_WORKSPACE = "/work/some-service"
CASE_230_SHADOW_LAUNCH = _utc("2026-09-23T08:04:00")  # 01:04 PT = 08:04Z
CASE_230_PC_HOLD = _utc("2026-09-23T08:05:00")  # 01:05 PT = 08:05Z

# Task-204 (overseer FIX 5, exact values): the shadow's only pre-launch decision
# was a dirty-tree safety skip at the same second the PC launched.
CASE_204_NAME = "9999zv-204-memory-death-reason"
CASE_204_QID = "q-5573d2ee7cb64715"
CASE_204_RUN = "20260923T005453Z_c1724ef3"
CASE_204_WORKSPACE = "/work/LongHorizon-Harness"
CASE_204_REASON = "workspace LongHorizon-Harness occupied: dirty tree"
CASE_204_PC_EXACT = _utc("2026-09-23T00:54:53")  # 17:54 PT on 09-22 = 00:54:53Z on 09-23
CASE_204_SHADOW_SKIP = _utc("2026-09-23T00:54:53")

PROBE_NAME = "__tick1558-workspace-does-not-exist__"
PROBE_QID = "q-eee555feed1234abc"
PROBE_RUN = "tickprobe99"
PROBE_WORKSPACE = "/work/__tick1558-workspace-does-not-exist__"
PROBE_SHADOW_LAUNCH = _utc("2026-09-23T07:40:30")
PROBE_PC_EXACT = _utc("2026-09-23T07:41:00")  # 00:41 PT = 07:41Z

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _shadow_launch(queue_id: str, name: str, workspace: str, ts: float) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "type": "queue.shadow_launch",
        "ts": ts,
        "payload": {
            "queue_id": queue_id,
            "trio": "kimi",
            "workspace": workspace,
            "would_run_at": ts,
            "roles": {
                "manager": {"agent": "codex", "model": "syn:large"},
                "executor": {"agent": "codex", "model": "syn:large"},
                "auditor": {"agent": "codex", "model": "syn:large"},
            },
        },
    }


def _shadow_skip(queue_id: str, name: str, workspace: str, ts: float, *, run_id: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "type": "queue.shadow_skip",
        "ts": ts,
        "payload": {
            "queue_id": queue_id,
            "trio": "kimi",
            "workspace": workspace,
            "reason": f"workspace {workspace} has active run {run_id}",
        },
    }


def _shadow_skip_reason(
    queue_id: str, name: str, workspace: str, ts: float, *, reason: str
) -> dict[str, Any]:
    """A shadow skip carrying its own reason (safety-check shape, FIX 5)."""

    return {
        "schema_version": 2,
        "type": "queue.shadow_skip",
        "ts": ts,
        "payload": {
            "queue_id": queue_id,
            "trio": "kimi",
            "workspace": workspace,
            "reason": reason,
        },
    }


def _write_shadow_log(path: Path) -> None:
    records = [
        _shadow_launch(CASE_221_QID, CASE_221_NAME, CASE_221_WORKSPACE, CASE_221_SHADOW_LAUNCH),
        _shadow_skip(CASE_221_QID, CASE_221_NAME, CASE_221_WORKSPACE, CASE_221_SKIP_1, run_id=CASE_221_RUN),
        _shadow_skip(CASE_221_QID, CASE_221_NAME, CASE_221_WORKSPACE, CASE_221_SKIP_2, run_id=CASE_221_RUN),
        _shadow_launch(CASE_218_QID, CASE_218_NAME, CASE_218_WORKSPACE, CASE_218_SHADOW_LAUNCH),
        _shadow_skip(CASE_218_QID, CASE_218_NAME, CASE_218_WORKSPACE, CASE_218_SKIP, run_id=CASE_218_RUN),
        _shadow_skip(CASE_217_QID, CASE_217_NAME, CASE_217_WORKSPACE, CASE_217_SHADOW_SKIP, run_id=CASE_217_RUN),
        _shadow_launch(CASE_230_QID, CASE_230_NAME, CASE_230_WORKSPACE, CASE_230_SHADOW_LAUNCH),
        # Task-204: the shadow's ONLY pre-launch decision is the dirty-tree
        # safety skip at 00:54:53Z — the same second the PC launched.  This is
        # not run-occupancy (no run_id in the reason): FIX 5's
        # shadow-more-conservative bucket.
        _shadow_skip_reason(
            CASE_204_QID,
            CASE_204_NAME,
            CASE_204_WORKSPACE,
            CASE_204_SHADOW_SKIP,
            reason=CASE_204_REASON,
        ),
        _shadow_launch(PROBE_QID, PROBE_NAME, PROBE_WORKSPACE, PROBE_SHADOW_LAUNCH),
    ]
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8")


def _pc_log_real_slice() -> str:
    """PC log shaped like the real day-7 window slice, starting at 20:56 PT on
    2026-09-22 and crossing midnight into 09-23."""
    return (
        "20:56 active=3 kimi=1 qwen=1 keys_ok=True queued=6 synthetic=ok doctor_quota=ok\n"
        f"20:56 shadow-filed {CASE_221_NAME}.json {CASE_221_QID}\n"
        f'20:57   emit-probe {CASE_221_NAME} [trio 2] {{"kimi": {{"agent": "codex", "model": "syn:large"}}}}\n'
        f"20:57 LAUNCHED {CASE_221_NAME} {CASE_221_RUN}\n"
        f"00:18 shadow-filed {CASE_218_NAME}.json {CASE_218_QID}\n"
        f"00:19 LAUNCHED {CASE_218_NAME} {CASE_218_RUN}\n"
        f"00:30 shadow-filed {CASE_217_NAME}.json {CASE_217_QID}\n"
        f"00:31 LAUNCHED {CASE_217_NAME} {CASE_217_RUN}\n"
        f"00:40 shadow-filed {PROBE_NAME}.json {PROBE_QID}\n"
        f"00:41 LAUNCHED {PROBE_NAME} {PROBE_RUN}\n"
        f"01:04 shadow-filed {CASE_230_NAME}.json {CASE_230_QID}\n"
        f"01:05   HOLD {CASE_230_NAME} - kimi at capacity\n"
        f"17:54 shadow-filed {CASE_204_NAME}.json {CASE_204_QID}\n"
        f"17:54 LAUNCHED {CASE_204_NAME} {CASE_204_RUN}\n"
    )


def _pc_log_with_stale_head() -> str:
    """Same log with a stale 09-21 head before the real anchor line."""
    head = (
        "19:41 LAUNCHED 9999zv-215-earlier-entry 20260922T024100Z_deadbeefcafe\n"
        "20:10   HOLD 9999zv-216-stale-entry - kimi at capacity\n"
    )
    return head + _pc_log_real_slice()


def _run_main(
    tmp_path: Path, pc_log: str, *, extra_args: list[str] | None = None, expect_out: bool = True
) -> tuple[int, str, dict[str, Any]]:
    pc_path = tmp_path / "pc.log"
    pc_path.write_text(pc_log, encoding="utf-8")
    shadow_dir = tmp_path / "runs" / "queue"
    shadow_dir.mkdir(parents=True)
    shadow_path = shadow_dir / "shadow.jsonl"
    _write_shadow_log(shadow_path)
    out_path = tmp_path / "out.json"

    argv = [
        "--pc-log",
        str(pc_path),
        "--shadow",
        str(tmp_path / "runs"),
    ]
    if expect_out:
        argv.extend(["--out", str(out_path)])
    if extra_args:
        argv.extend(extra_args)

    stdout = io.StringIO()
    stderr = io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout, stderr
    try:
        code = cs.main(argv)
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr

    output = stdout.getvalue() + stderr.getvalue()
    data: dict[str, Any] = {}
    if expect_out:
        data = json.loads(out_path.read_text(encoding="utf-8"))
    return code, output, data


# ---------------------------------------------------------------------------
# Verdict tests
# ---------------------------------------------------------------------------


def test_exact_real_cases_verdicts(tmp_path: Path):
    code, output, data = _run_main(tmp_path, _pc_log_real_slice(), extra_args=["--pc-date", "2026-09-22"])
    assert code == 0, output
    by_name = {e["name"]: e for e in data["entries"]}

    assert by_name[CASE_221_NAME]["verdict"] == "agree", by_name[CASE_221_NAME]
    assert by_name[CASE_221_NAME]["self_occupied_after"] == 2
    # pc_ts is the log's minute stamp (03:57 PT -> 03:57Z, seconds 0); the
    # run_id's exact 03:57:22Z stamp is the compare instant.
    assert by_name[CASE_221_NAME]["pc_ts"] == _utc("2026-09-23T03:57:00")

    assert by_name[CASE_218_NAME]["verdict"] == "agree", by_name[CASE_218_NAME]

    assert by_name[CASE_217_NAME]["verdict"] == "unobserved-race", by_name[CASE_217_NAME]
    assert "UNOBSERVED" in by_name[CASE_217_NAME]["detail"]

    assert by_name[CASE_230_NAME]["verdict"] == "phantom", by_name[CASE_230_NAME]

    assert by_name[PROBE_NAME]["verdict"] == "probe", by_name[PROBE_NAME]

    # FIX 5 / task-204: dirty-tree safety skip at-or-before the PC launch —
    # agreement-in-intent, never scored as missed.
    entry_204 = by_name[CASE_204_NAME]
    assert entry_204["verdict"] == "shadow-more-conservative", entry_204
    assert entry_204["pc_run_id"] == CASE_204_RUN
    # pc_ts is the log's minute stamp (17:54 PT 09-22 = 00:54Z 09-23, seconds 0);
    # the run_id's exact 00:54:53Z stamp is the compare instant.
    assert entry_204["pc_ts"] == _utc("2026-09-23T00:54:00")
    assert entry_204["shadow_ts"] == CASE_204_SHADOW_SKIP  # the same second as the launch
    assert entry_204["shadow_reason"] == CASE_204_REASON
    assert entry_204["shadow_queue_id"] == CASE_204_QID
    assert "safety check" in entry_204["detail"]
    assert "agreement-in-intent" in entry_204["detail"]
    # It must never be folded into missed/shadow-only/agreement tallies:
    assert data["summary"] == (
        "entries=6  agree=2  phantom=1  shadow-more-conservative=1  "
        "unobserved-race=1  probe=1  observable-agreement=2/3"
    ), data["summary"]

    # Every instance listed individually by the FIX 5 instance printer:
    assert "shadow-more-conservative instances" in output
    assert f"entry={CASE_204_NAME}" in output
    assert f"queue_id={CASE_204_QID}" in output
    assert f"pc_run_id={CASE_204_RUN}" in output
    assert CASE_204_REASON in output


def test_queue_name_map_defaults_to_pc_log_shadow_filed_lines(tmp_path: Path):
    code, output, data = _run_main(tmp_path, _pc_log_real_slice(), extra_args=["--pc-date", "2026-09-22"])
    assert code == 0, output
    assert data["queue_names_source"].startswith("pc-log shadow-filed lines")
    assert data["queue_names"] == {
        CASE_221_QID: CASE_221_NAME,
        CASE_218_QID: CASE_218_NAME,
        CASE_217_QID: CASE_217_NAME,
        PROBE_QID: PROBE_NAME,
        CASE_230_QID: CASE_230_NAME,
        CASE_204_QID: CASE_204_NAME,
    }
    by_name = {e["name"]: e for e in data["entries"]}
    # Joining by PC-log-derived names means the real cases connect:
    assert by_name[CASE_221_NAME]["shadow_decision"] == "launch"
    assert by_name[CASE_218_NAME]["shadow_decision"] == "launch"


def test_queue_names_override_replaces_log_map(tmp_path: Path):
    override = {CASE_221_QID: "renamed-entry"}
    override_path = tmp_path / "names.json"
    override_path.write_text(json.dumps(override), encoding="utf-8")
    code, output, data = _run_main(
        tmp_path,
        _pc_log_real_slice(),
        extra_args=["--pc-date", "2026-09-22", "--queue-names", str(override_path)],
    )
    assert code == 0, output
    assert data["queue_names_source"] == "--queue-names override file"
    assert data["queue_names"] == {CASE_221_QID: "renamed-entry"}
    names = {e["name"] for e in data["entries"]}
    assert "renamed-entry" in names
    assert CASE_221_NAME in names


def test_since_slices_and_anchors(tmp_path: Path):
    code, output, data = _run_main(
        tmp_path,
        _pc_log_with_stale_head(),
        extra_args=[
            "--since",
            f"shadow-filed {CASE_221_NAME}",
            "--since-date",
            "2026-09-22",
        ],
    )
    assert code == 0, output
    by_name = {e["name"]: e for e in data["entries"]}
    # Stale 09-21 head must be dropped, not mis-dated into the window.
    assert "9999zv-215-earlier-entry" not in by_name
    assert "9999zv-216-stale-entry" not in by_name
    assert data["pc_parse_counts"]["before_since"] == 3
    # Real anchor line's date is honored (minute stamp; run_id carries 03:57:22Z).
    assert by_name[CASE_221_NAME]["pc_ts"] == _utc("2026-09-23T03:57:00")
    assert by_name[CASE_221_NAME]["verdict"] == "agree"
    assert by_name[CASE_221_NAME]["verdict"] == "agree"
    # 218 PC minute stamp must roll into 09-23, not shift to 09-30.
    assert by_name[CASE_218_NAME]["pc_ts"] == _utc("2026-09-23T07:19:00")


def test_since_anchor_not_found_is_usage_error(tmp_path: Path):
    code, output, _ = _run_main(
        tmp_path,
        _pc_log_real_slice(),
        extra_args=["--since", "this-string-is-not-in-the-log", "--since-date", "2026-09-22"],
        expect_out=False,
    )
    assert code == 2
    assert "matched no line" in output


def test_since_date_required_with_since(tmp_path: Path):
    code, output, _ = _run_main(
        tmp_path,
        _pc_log_real_slice(),
        extra_args=["--since", f"shadow-filed {CASE_221_NAME}"],
        expect_out=False,
    )
    assert code == 2
    assert "requires --since-date" in output


def test_probe_never_scores_phantom_or_missed(tmp_path: Path):
    code, output, data = _run_main(tmp_path, _pc_log_real_slice(), extra_args=["--pc-date", "2026-09-22"])
    assert code == 0, output
    by_name = {e["name"]: e for e in data["entries"]}
    probe = by_name[PROBE_NAME]
    assert probe["verdict"] == "probe"
    assert probe["pc_decision"] == "launch"
    # A probe entry is reported as its own bucket regardless of what either
    # side decided; it must never surface as phantom or missed.
    assert probe["verdict"] not in ("phantom", "missed", "agree")
    # Observable denominator does not include probe:
    assert "observable-agreement=2/3" in data["summary"]


def test_task_204_safety_skip_is_shadow_more_conservative(tmp_path: Path):
    """Task-204 (overseer FIX 5, exact values): PC run 20260923T005453Z_c1724ef3
    launched 00:54:53Z; the shadow's only pre-launch decision is
    'workspace LongHorizon-Harness occupied: dirty tree' at 00:54:53Z.

    The reason contains the word ``occupied`` but names NO run — it is a
    dirty-tree safety check, not run-occupancy — so a naive run-id match would
    mis-bucket it.  It must classify as ``shadow-more-conservative``:
    agreement-in-intent, never missed, never shadow-only.
    """
    pc_log = (
        "17:54 shadow-filed 9999zv-204-memory-death-reason.json q-5573d2ee7cb64715\n"
        "17:54 LAUNCHED 9999zv-204-memory-death-reason 20260923T005453Z_c1724ef3\n"
    )
    shadow_records = [
        _shadow_skip_reason(
            "q-5573d2ee7cb64715",
            "9999zv-204-memory-death-reason",
            "/work/LongHorizon-Harness",
            _utc("2026-09-23T00:54:53"),
            reason="workspace LongHorizon-Harness occupied: dirty tree",
        ),
    ]
    pc_path = tmp_path / "pc.log"
    pc_path.write_text(pc_log, encoding="utf-8")
    shadow_path = tmp_path / "shadow.jsonl"
    shadow_path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in shadow_records), encoding="utf-8"
    )

    pc_decisions, _, _ = cs.parse_pc_log(pc_path, log_date=datetime(2026, 9, 22, tzinfo=cs.PC_TZ))
    shadow_decisions, _ = cs.parse_shadow_log(
        shadow_path, queue_names={"q-5573d2ee7cb64715": "9999zv-204-memory-death-reason"}
    )
    reports = cs.compare_streams(pc_decisions, shadow_decisions)
    assert len(reports) == 1
    r = reports[0]
    assert r.verdict == "shadow-more-conservative", r
    assert r.pc_run_id == "20260923T005453Z_c1724ef3"
    assert r.pc_ts == _utc("2026-09-23T00:54:00")
    assert r.shadow_ts == _utc("2026-09-23T00:54:53")
    assert r.shadow_reason == "workspace LongHorizon-Harness occupied: dirty tree"
    assert r.shadow_queue_id == "q-5573d2ee7cb64715"
    # Not folded into the other buckets' semantics:
    assert r.verdict not in ("missed", "self-occupied", "agree", "shadow-only")


def test_legend_states_shadow_more_conservative_meaning(tmp_path: Path):
    code, output, data = _run_main(tmp_path, _pc_log_real_slice(), extra_args=["--pc-date", "2026-09-22"])
    assert code == 0, output
    assert "shadow-more-conservative (1):" in output
    assert "AGREEMENT-IN-INTENT" in output
    assert "safety check" in output
    assert "NEVER" in output


def test_self_occupied_skip_is_never_missed(tmp_path: Path):
    """A shadow skip naming the PC's own run for the same entry is
    self-occupied, even when it lands within the PC-minute tolerance."""
    run_id = "shortrun123"
    pc_log = (
        "20:00 shadow-filed self-occupied-test.json q-abc123def4567890\n"
        f"20:01 LAUNCHED self-occupied-test {run_id}\n"
    )
    shadow_records = [
        _shadow_skip("q-abc123def4567890", "self-occupied-test", "/work/self-occupied-test", _utc("2026-09-23T03:01:30"), run_id=run_id),
    ]
    pc_path = tmp_path / "pc.log"
    pc_path.write_text(pc_log, encoding="utf-8")
    shadow_path = tmp_path / "shadow.jsonl"
    shadow_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in shadow_records), encoding="utf-8")

    pc_decisions, _, _ = cs.parse_pc_log(pc_path, log_date=datetime(2026, 9, 22, tzinfo=cs.PC_TZ))
    shadow_decisions, _ = cs.parse_shadow_log(shadow_path, queue_names={"q-abc123def4567890": "self-occupied-test"})
    reports = cs.compare_streams(pc_decisions, shadow_decisions)
    assert len(reports) == 1
    assert reports[0].verdict == "self-occupied"
    assert "same run" in reports[0].detail


def test_foreign_occupied_skip_is_shadow_more_conservative(tmp_path: Path):
    """FIX 5: a shadow skip naming a DIFFERENT run is occupancy by a run other
    than this entry's own PC run — a workspace safety check — so it is
    shadow-more-conservative (agreement-in-intent), no longer missed (the FIX 5
    spec supersedes the pre-FIX-5 classification)."""
    pc_run_id = "pcrun123"
    foreign_run_id = "foreignrun999"
    pc_log = (
        "20:00 shadow-filed missed-test.json q-abc123def4567890\n"
        f"20:01 LAUNCHED missed-test {pc_run_id}\n"
    )
    shadow_records = [
        _shadow_skip("q-abc123def4567890", "missed-test", "/work/missed-test", _utc("2026-09-23T03:01:30"), run_id=foreign_run_id),
    ]
    pc_path = tmp_path / "pc.log"
    pc_path.write_text(pc_log, encoding="utf-8")
    shadow_path = tmp_path / "shadow.jsonl"
    shadow_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in shadow_records), encoding="utf-8")

    pc_decisions, _, _ = cs.parse_pc_log(pc_path, log_date=datetime(2026, 9, 22, tzinfo=cs.PC_TZ))
    shadow_decisions, _ = cs.parse_shadow_log(shadow_path, queue_names={"q-abc123def4567890": "missed-test"})
    reports = cs.compare_streams(pc_decisions, shadow_decisions)
    assert reports[0].verdict == "shadow-more-conservative"
    assert "safety check" in reports[0].detail


def test_genuine_non_safety_skip_is_missed(tmp_path: Path):
    """A shadow skip whose reason is NOT a safety check (no dirty tree, no
    unpushed branch, no run occupancy at all) remains a genuine missed."""
    pc_run_id = "pcrun123"
    pc_log = (
        "20:00 shadow-filed missed-test.json q-abc123def4567890\n"
        f"20:01 LAUNCHED missed-test {pc_run_id}\n"
    )
    shadow_records = [
        {
            "schema_version": 2,
            "type": "queue.shadow_skip",
            "ts": _utc("2026-09-23T03:01:30"),
            "payload": {
                "queue_id": "q-abc123def4567890",
                "trio": "kimi",
                "workspace": "/work/missed-test",
                "reason": "kimi at capacity",
            },
        },
    ]
    pc_path = tmp_path / "pc.log"
    pc_path.write_text(pc_log, encoding="utf-8")
    shadow_path = tmp_path / "shadow.jsonl"
    shadow_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in shadow_records), encoding="utf-8")

    pc_decisions, _, _ = cs.parse_pc_log(pc_path, log_date=datetime(2026, 9, 22, tzinfo=cs.PC_TZ))
    shadow_decisions, _ = cs.parse_shadow_log(shadow_path, queue_names={"q-abc123def4567890": "missed-test"})
    reports = cs.compare_streams(pc_decisions, shadow_decisions)
    assert reports[0].verdict == "missed"
    assert "shadow skipped" in reports[0].detail


def test_legend_prints_evidence_meaning(tmp_path: Path):
    code, output, data = _run_main(tmp_path, _pc_log_real_slice(), extra_args=["--pc-date", "2026-09-22"])
    assert code == 0, output
    assert "verdict buckets -> cutover task 168 evidence items" in output
    assert "unobserved-race (1):" in output
    assert "evidence item 3" in output
    assert "UNOBSERVED" in output
    assert "NEVER counted against evidence item 2" in output
    assert "self-occupied" in output


def test_since_anchor_on_launched_line_keeps_earlier_shadow_filed_name(tmp_path: Path):
    # Real window: the entry is shadow-filed a few lines BEFORE its LAUNCHED
    # line, so anchoring --since on the LAUNCHED line must not drop its name.
    code, output, data = _run_main(
        tmp_path,
        _pc_log_with_stale_head(),
        extra_args=["--since", f"LAUNCHED {CASE_221_NAME}", "--since-date", "2026-09-22"],
    )
    assert code == 0, output
    by_name = {e["name"]: e for e in data["entries"]}
    assert by_name[CASE_221_NAME]["verdict"] == "agree"


def test_no_upstream_unpushed_skip_is_a_safety_check():
    # Real CT110 skip reason seen for 222 on 2026-09-23 08:51Z.
    reason = (
        "workspace /home/harness/work/LongHorizon-Harness occupied: checked-out "
        "branch has no upstream and carries commits not on origin/main"
    )
    assert cs._SHADOW_SKIP_SAFETY.search(reason)
