"""LHH-GATE-OWNER-NOTE: HTTP endpoint tests for ``POST /api/runs/.../annotate``.

The endpoint pairs with the round-1 ``annotate_gate`` model API.  It is the
write surface for the decision-maker badge (``owner``) and the free-form
description panel (``owner_note``); gate resolution still flows through
``/api/runs/{run_id}/approvals/{approval_id}/resolve``.  These tests
exercise the runtime contract the task spec calls out:

* 200 on a pending gate with a valid body, including the audit metadata
  fields (``owner_set_by`` / ``owner_set_at``) so the UI can render
  "Decision: Paxton  -- set by overseer1 at <ts>".
* 404 when the approval id is unknown.
* 409 when the approval already resolved.
* The round-1 invariants (storage set / update / read-back, default
  ``owner``) are covered by ``tests/webapi/test_gate_owner_note.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import ApprovalOption, DashboardState
from lh_harness.webapi.server import create_app


def _fixture(
    tmp_path: Path, *, token: str | None = "test-token"
) -> tuple[Path, DashboardState, TestClient]:
    root = tmp_path / "runs"
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


def _post(
    client: TestClient,
    path: str,
    payload: dict[str, Any],
    token: str | None = "test-token",
) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(path, json=payload, headers=headers)


_OWNER_NOTE_FIXTURE = (
    "I approve these gates, not you, unless a gate needs a decision only you "
    "can make. This one I'm holding because the run can't find the file it "
    "was told to edit. See [docs](https://example.local/run) for context."
)


def test_api_annotate_gate_sets_owner_and_note_on_pending_approval(tmp_path: Path) -> None:
    """Happy path: 200 with audit metadata; the snapshot picks it up."""

    root, state, client = _fixture(tmp_path)
    approval = state.create_approval(
        title="Continue?",
        options=[ApprovalOption(value="continue", label="Continue")],
    )

    response = _post(
        client,
        f"/api/runs/run-1/approvals/{approval.approval_id}/annotate",
        {"owner": "paxton", "owner_note": _OWNER_NOTE_FIXTURE},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["approval_id"] == approval.approval_id
    assert body["status"] == "accepted"
    assert body["owner"] == "paxton"
    assert body["owner_note"] == _OWNER_NOTE_FIXTURE
    assert body["owner_set_by"] == "overseer1"
    assert isinstance(body["owner_set_at"], (int, float))

    # The snapshot must reflect what was written so any viewer opening
    # the run sees the badge.
    headers = {"Authorization": "Bearer test-token"}
    snap = client.get("/api/runs/run-1/snapshot", headers=headers).json()
    match = next(
        item for item in snap["approvals"]
        if item["approval_id"] == approval.approval_id
    )
    assert match["owner"] == "paxton"
    assert match["owner_note"] == _OWNER_NOTE_FIXTURE
    assert match["owner_set_by"] == "overseer1"


def test_api_annotate_gate_accepts_owner_only_update(tmp_path: Path) -> None:
    """Either field alone is enough; the endpoint does not force both."""

    root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="Continue?")

    response = _post(
        client,
        f"/api/runs/run-1/approvals/{approval.approval_id}/annotate",
        {"owner": "overseer"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["owner"] == "overseer"


def test_api_annotate_gate_accepts_owner_note_only_update(tmp_path: Path) -> None:
    root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="Continue?")

    response = _post(
        client,
        f"/api/runs/run-1/approvals/{approval.approval_id}/annotate",
        {"owner_note": "blank badge for now"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["owner"] == "overseer"  # defaults unchanged
    assert body["owner_note"] == "blank badge for now"


def test_api_annotate_gate_rejects_empty_body(tmp_path: Path) -> None:
    """422 when the body supplies neither field; the endpoint must require one."""

    root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="Continue?")

    response = _post(
        client,
        f"/api/runs/run-1/approvals/{approval.approval_id}/annotate",
        {},
    )
    assert response.status_code == 422
    assert "owner" in response.json()["detail"]


def test_api_annotate_gate_unknown_approval_returns_404(tmp_path: Path) -> None:
    """Contract: unknown approval id surfaces as 404, not 400."""

    root, state, client = _fixture(tmp_path)

    response = _post(
        client,
        "/api/runs/run-1/approvals/does-not-exist/annotate",
        {"owner": "overseer", "owner_note": "ignored"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "approval not found"


def test_api_annotate_gate_unknown_run_returns_404(tmp_path: Path) -> None:
    """A bad run id also produces 404, matching the resolve route's shape."""

    root, state, client = _fixture(tmp_path)

    response = _post(
        client,
        "/api/runs/run-404/approvals/ap-1/annotate",
        {"owner": "overseer"},
    )
    assert response.status_code == 404
    assert "run not found" in response.json()["detail"]


def test_api_annotate_gate_resolved_approval_returns_409(tmp_path: Path) -> None:
    """Once the gate is decided, the badge is frozen: 409 even with a valid body."""

    root, state, client = _fixture(tmp_path)
    approval = state.create_approval(
        title="Continue?",
        options=[ApprovalOption(value="continue", label="Continue")],
    )
    assert state.resolve_approval(approval.approval_id, action="continue", user_input="go")

    response = _post(
        client,
        f"/api/runs/run-1/approvals/{approval.approval_id}/annotate",
        {"owner": "paxton", "owner_note": "post-mortem"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "approval is already resolved"


def test_api_annotate_gate_idempotent_repeat_returns_200(tmp_path: Path) -> None:
    """Re-annotating the same gate is idempotent and rewrites the audit row."""

    root, state, client = _fixture(tmp_path)
    approval = state.create_approval(title="Continue?")

    first = _post(
        client,
        f"/api/runs/run-1/approvals/{approval.approval_id}/annotate",
        {"owner": "overseer", "owner_note": "first"},
    )
    assert first.status_code == 200
    first_ts = first.json()["owner_set_at"]

    second = _post(
        client,
        f"/api/runs/run-1/approvals/{approval.approval_id}/annotate",
        {"owner_note": "second"},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["owner_note"] == "second"
    assert body["owner"] == "overseer"  # owner not touched on this call
    assert isinstance(body["owner_set_at"], (int, float))
    assert body["owner_set_at"] >= first_ts
    assert body["owner_set_by"] == "overseer1"
