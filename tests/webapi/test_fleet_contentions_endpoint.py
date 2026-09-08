from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import DashboardState
from lh_harness.webapi.server import create_app


def _fixture(tmp_path: Path, *, token: str | None = "test-token", runs_root: Path | None = None) -> tuple[Path, DashboardState, TestClient]:
    root = runs_root if runs_root is not None else tmp_path / "runs"
    root.mkdir(parents=True, exist_ok=True)
    run = root / "run-1"
    role_dir = run / "logs" / "role_management"
    role_dir.mkdir(parents=True)
    (run / "control").mkdir(parents=True)
    (role_dir / "rounds").mkdir(parents=True)
    (run / "logs" / "report.json").write_text(
        json.dumps({"task": "fixture", "status": "running"}), encoding="utf-8"
    )
    (role_dir / "events.jsonl").write_text(
        json.dumps({"ts": 1, "event": "role_harness_start"}) + "\n",
        encoding="utf-8",
    )
    state = DashboardState(
        run / "logs",
        runs_root=root,
        control_enabled=True,
    )
    app = create_app(
        state=state,
        runs_root=root,
        control_enabled=True,
        auth_token=token,
        bind_host="testserver",
    )
    return root, state, TestClient(app)


def test_contentions_endpoint_missing_file_returns_available_false(tmp_path: Path) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        response = client.get("/api/fleet/contentions", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200
        data = response.json()
        assert data == {"ok": True, "available": False, "contentions": []}


def test_contentions_endpoint_reads_persisted_file(tmp_path: Path) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        contention_path = root / "queue" / "contention.json"
        contention_path.parent.mkdir(parents=True, exist_ok=True)
        contention_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "available": True,
                    "contentions": [
                        {
                            "contention_id": "abc123",
                            "severity": "same_repo",
                            "group_key": "repo:remote:host/org/repo",
                            "members": [
                                {"run_id": "run-a", "workspace": "/a", "branch": "main"},
                                {"run_id": "run-b", "workspace": "/b", "branch": "main"},
                            ],
                            "truncated": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        response = client.get("/api/fleet/contentions", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200
        data = response.json()
        assert data["available"] is True
        assert len(data["contentions"]) == 1


def test_contentions_endpoint_501_without_runs_root(tmp_path: Path) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "runs"
        root.mkdir(parents=True)
        run = root / "run-1"
        role_dir = run / "logs" / "role_management"
        role_dir.mkdir(parents=True)
        (run / "control").mkdir(parents=True)
        (role_dir / "rounds").mkdir(parents=True)
        (run / "logs" / "report.json").write_text(
            json.dumps({"task": "fixture", "status": "running"}), encoding="utf-8"
        )
        (role_dir / "events.jsonl").write_text(
            json.dumps({"ts": 1, "event": "role_harness_start"}) + "\n",
            encoding="utf-8",
        )
        state = DashboardState(
            run / "logs",
            runs_root=root,
            control_enabled=True,
        )
        app = create_app(
            state=state,
            runs_root=None,
            control_enabled=True,
            auth_token="test-token",
            bind_host="testserver",
        )
        client = TestClient(app)
        response = client.get("/api/fleet/contentions", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 501


def test_runs_listing_annotates_contention_and_omits_when_clean(tmp_path: Path) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root, state, client = _fixture(Path(tmp))
        contention_path = root / "queue" / "contention.json"
        contention_path.parent.mkdir(parents=True, exist_ok=True)
        contention_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "available": True,
                    "contentions": [
                        {
                            "contention_id": "abc123",
                            "severity": "same_repo",
                            "group_key": "repo:remote:host/org/repo",
                            "members": [
                                {"run_id": "run-1", "workspace": "/a", "branch": "main"},
                                {"run_id": "run-b", "workspace": "/b", "branch": "main"},
                            ],
                            "truncated": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        response = client.get("/api/runs", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200
        data = response.json()
        runs = data["runs"]
        run1 = next(run for run in runs if run["id"] == "run-1")
        assert "contention" in run1
        assert run1["contention"]["severity"] == "same_repo"
        assert any(peer["run_id"] == "run-b" for peer in run1["contention"]["peers"])

        # Empty the contention file to exercise the clean path.
        contention_path.write_text(
            json.dumps({"ok": True, "available": True, "contentions": []}),
            encoding="utf-8",
        )
        response = client.get("/api/runs", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200
        data = response.json()
        run1 = next(run for run in data["runs"] if run["id"] == "run-1")
        assert "contention" not in run1
