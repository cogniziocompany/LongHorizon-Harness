from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import DashboardState
from lh_harness.webapi.server import create_app


def _fixture(tmp_path: Path, *, rounds: int = 16, events_per_round: int = 10) -> tuple[Path, DashboardState]:
    root = tmp_path / "runs"
    run = root / "run-1"
    role_dir = run / "logs" / "role_management"
    role_dir.mkdir(parents=True)
    (run / "logs" / "report.json").write_text(
        json.dumps({"task": "speed test", "status": "running"}),
        encoding="utf-8",
    )
    events: list[dict[str, object]] = [{"ts": 1, "event": "role_harness_start", "run_id": "run-1"}]
    for round_index in range(1, rounds + 1):
        round_dir = role_dir / "rounds" / f"round_{round_index:03d}"
        round_dir.mkdir(parents=True)
        (round_dir / "manager_plan.txt").write_text(
            f"next_step: round {round_index}\nline\n" * 50,
            encoding="utf-8",
        )
        (round_dir / "executor_output.txt").write_text(
            f"output for round {round_index}\nlog\n" * 100,
            encoding="utf-8",
        )
        for seq in range(events_per_round):
            events.append(
                {
                    "ts": 100 * round_index + seq,
                    "event": "manager_round_start",
                    "round_index": round_index,
                }
            )
    (role_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    state = DashboardState(run / "logs", runs_root=root, control_enabled=False)
    return root, state


def test_snapshot_summary_omits_heavy_fields(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path, rounds=4, events_per_round=5)
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    full = client.get("/api/runs/run-1/snapshot").json()
    summary = client.get("/api/runs/run-1/snapshot?fields=summary").json()

    assert "rounds" in full
    assert "events" in full
    assert "legacy" in full
    assert "rounds" not in summary
    assert "events" not in summary
    assert "legacy" not in summary
    assert summary["schema_version"] == full["schema_version"]
    assert summary["run"] == full["run"]
    assert summary["diagnostics"]["event_count"] == full["diagnostics"]["event_count"]


def test_snapshot_summary_returns_304_with_matching_etag(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path, rounds=4)
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    first = client.get("/api/runs/run-1/snapshot?fields=summary")
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"')

    second = client.get(
        "/api/runs/run-1/snapshot?fields=summary",
        headers={"If-None-Match": etag},
    )
    assert second.status_code == 304
    assert second.headers["etag"] == etag
    assert second.headers["cache-control"] == "private, max-age=2"


def test_snapshot_etag_changes_when_events_change(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path, rounds=4)
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    first = client.get("/api/runs/run-1/snapshot?fields=summary")
    etag1 = first.headers["etag"]

    events_path = root / "run-1" / "logs" / "role_management" / "events.jsonl"
    events_path.open("a", encoding="utf-8").write(
        json.dumps({"ts": 999, "event": "manager_round_done", "round_index": 4}) + "\n"
    )

    second = client.get("/api/runs/run-1/snapshot?fields=summary")
    etag2 = second.headers["etag"]
    assert etag2 != etag1


def test_snapshot_gzips_full_payload(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path, rounds=16, events_per_round=20)
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    response = client.get(
        "/api/runs/run-1/snapshot",
        headers={"Accept-Encoding": "gzip"},
    )
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    assert response.headers.get("vary") == "Accept-Encoding"
    # The test client decompresses automatically; assert it parsed as JSON.
    assert response.json()["schema_version"] == 1
    # Ensure the response was actually compressed by checking the raw size hint.
    assert int(response.headers["content-length"]) < len(json.dumps(response.json()).encode("utf-8"))


def test_snapshot_cached_summary_is_fast(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path, rounds=16, events_per_round=20)
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    # Warm the cache.
    client.get("/api/runs/run-1/snapshot?fields=summary")
    times: list[float] = []
    for _ in range(10):
        start = time.perf_counter()
        response = client.get("/api/runs/run-1/snapshot?fields=summary")
        times.append((time.perf_counter() - start) * 1000)
        assert response.status_code == 200

    assert max(times) < 300, f"cached summary path too slow: {max(times):.1f} ms"


def test_full_snapshot_backward_compatible_shape(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path, rounds=4)
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    response = client.get("/api/runs/run-1/snapshot")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["run"]["id"] == "run-1"
    assert "rounds" in body
    assert "events" in body
    assert "controls" in body
    assert "approvals" in body
    assert "diagnostics" in body


def test_instructions_invalidates_snapshot_cache(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path, rounds=1)
    state.control_enabled = True
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    client.post("/api/runs/run-1/instructions", json={"instructions": "go"})
    queued = client.get("/api/runs/run-1/snapshot").json()
    assert queued["operator_messages"][0]["status"] == "queued"

    state.drain_injections()
    applied = client.get("/api/runs/run-1/snapshot").json()
    assert applied["operator_messages"][0]["status"] == "applied"


def test_resolve_approval_invalidates_snapshot_cache(tmp_path: Path) -> None:
    root, state = _fixture(tmp_path, rounds=1)
    state.control_enabled = True
    client = TestClient(create_app(state=state, runs_root=root, run_id="run-1"))

    approval = state.create_approval(title="Confirm", message="Proceed?")
    client.post(
        f"/api/runs/run-1/approvals/{approval.approval_id}/resolve",
        json={"action": "continue"},
    )

    snapshot = client.get("/api/runs/run-1/snapshot").json()
    resolved = next(item for item in snapshot["approvals"] if item["approval_id"] == approval.approval_id)
    assert resolved["status"] == "resolved"
    assert resolved["action"] == "continue"
