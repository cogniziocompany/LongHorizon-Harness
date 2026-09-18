"""API tests for kind="review" run creation and the review verdict endpoint.

The review path is a different run shape from task runs: one reviewer seat,
one round, no caller-controlled knobs, and a dedicated verdict endpoint. A
review POST must not need (and must reject) task-run fields.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.review import (
    REVIEWER_TIMEOUT_SECONDS,
    REVIEW_MAX_ROUNDS,
    review_report,
)
from lh_harness.supervisor.service import RunSupervisor
from lh_harness.webapi.server import create_app


def _fixture(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    supervisor = RunSupervisor(str(root), workspace_root=str(tmp_path / "workspace"))
    app = create_app(runs_root=root, supervisor=supervisor)
    return root, supervisor, TestClient(app)


def _review_body(**overrides: dict) -> dict:
    body = {
        "kind": "review",
        "repo": "cogniziocompany/LongHorizon-Harness",
        "pr_number": 186,
        "head_sha": "0123456789abcdef",
        "base_ref": "main",
        "gate_results_url": "https://gate.example.com/pr/186.json",
    }
    body.update(overrides)
    return body


def test_post_runs_kind_review_creates_a_review_run(tmp_path: Path) -> None:
    root, _supervisor, client = _fixture(tmp_path)
    response = client.post("/api/runs", json=_review_body())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    run = payload["run"]
    assert run["run_kind"] == "review"
    assert run["owner"]["max_rounds"] == REVIEW_MAX_ROUNDS == 1
    assert run["owner"]["reviewer_timeout_s"] == REVIEWER_TIMEOUT_SECONDS == 900
    assert run["owner"]["workspace"].endswith("/review/cogniziocompany/LongHorizon-Harness/186")


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_rounds": 3},
        {"roles": {"reviewer": {"model": "x"}}},
        {"task": "review pr 186"},
        {"agent": "codex"},
        {"timeout": 60},
    ],
)
def test_post_runs_kind_review_rejects_caller_knobs(
    tmp_path: Path, overrides: dict
) -> None:
    _root, _supervisor, client = _fixture(tmp_path)
    response = client.post("/api/runs", json=_review_body(**overrides))
    assert response.status_code == 400, response.text


def test_post_runs_kind_review_requires_the_full_body(tmp_path: Path) -> None:
    _root, _supervisor, client = _fixture(tmp_path)
    response = client.post("/api/runs", json={"kind": "review", "repo": "a/b"})
    assert response.status_code == 400, response.text
    assert "pr_number" in response.json()["detail"]


def test_get_run_review_returns_the_verdict(tmp_path: Path) -> None:
    root, _supervisor, client = _fixture(tmp_path)
    created = client.post("/api/runs", json=_review_body(pr_number=7)).json()["run"]
    run_id = created["id"]
    # A review run in flight has not produced a verdict yet.
    missing = client.get(f"/api/runs/{run_id}/review")
    assert missing.status_code == 404
    # Once the worker persisted review.json the endpoint returns it verbatim.
    report = {
        "verdict": "fail",
        "blocking": [{"file": "src/a.py", "line": 3, "why": "test_x fails"}],
        "findings": [],
        "evidence": ["gate report names test_x"],
        "model": "kimi-k3:synthetic-anthropic",
        "duration_s": 12.5,
    }
    report_dir = root / run_id / "lh_harness"
    report_dir.mkdir(parents=True)
    (report_dir / "review.json").write_text(json.dumps(report), encoding="utf-8")
    response = client.get(f"/api/runs/{run_id}/review")
    assert response.status_code == 200
    assert response.json()["review"] == report


def test_get_review_unknown_run_is_404(tmp_path: Path) -> None:
    _root, _supervisor, client = _fixture(tmp_path)
    response = client.get("/api/runs/missing-run/review")
    assert response.status_code == 404


def test_task_run_body_without_kind_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The kind="review" branch must not swallow ordinary task runs."""
    _root, supervisor, client = _fixture(tmp_path)
    monkeypatch.setattr(
        RunSupervisor,
        "_launch_worker",
        lambda self, **kwargs: {"run_id": kwargs["run_id"], "owner": kwargs["reservation"]},
    )
    response = client.post(
        "/api/runs",
        json={"task": "normal task run", "agent": "codex", "workspace": "."},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload.get("run", payload).get("run_kind") != "review"