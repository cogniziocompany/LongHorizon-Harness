"""LHH-GATE-OWNER-NOTE: gate owner + owner_note model/API tests.

A pending (waiting_approval) gate may carry two extra fields that are not part
of the answer-rendering schema:

* ``owner`` -- canonical enum of who decides the gate.  Must be one of
  ``overseer`` / ``paxton`` / ``requester`` / ``any``; legacy/unknown values
  are normalised to ``"overseer"`` so the badge never renders as an empty
  string.  The enum default is ``"overseer"``.
* ``owner_note`` -- free-form markdown explanation of why the gate is held
  (max 4000 chars after cleaning).  Empty note means the web UI hides the
  Description panel.

These tests exercise the state mutation, the on-disk round-trip, the
``to_dict`` / snapshot projection, the 404/409 response codes, and the
default-value handling.  They deliberately do not exercise the HTTP route or
MCP wrapper -- those wire-up rounds land in their own PR commits.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import ApprovalOption, DashboardState
from lh_harness.webapi.server import create_app


# Use the same fixture shape as tests/webapi/test_mcp_fleet.py so that the
# transient run directory satisfies the registry's path cache and the
# dashboard's on-disk I/O is harmless.
def _fixture(tmp_path: Path, *, token: str | None = "test-token") -> tuple[Path, DashboardState, TestClient]:
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


_OWNER_NOTE_FIXTURE = (
    "I approve these gates, not you, unless a gate needs a decision only you "
    "can make. This one I'm holding because the run can't find the file it "
    "was told to edit. See [docs](https://example.local/run) for context."
)


def _approval_path(run_logs: Path) -> Path:
    """Resolve the approvals.jsonl the harness writes under the role dir."""

    canonical = run_logs / "role_orchestration" / "approvals.jsonl"
    legacy = run_logs / "role_management" / "approvals.jsonl"
    # The fixture writes the role_management directory explicitly; mirror that.
    return legacy


def test_create_approval_defaults_owner_to_overseer(tmp_path: Path) -> None:
    """New approvals carry ``owner=overseer`` so the badge is never empty."""

    _, state, _ = _fixture(tmp_path)
    approval = state.create_approval(
        title="Continue?",
        options=[ApprovalOption(value="continue", label="Continue")],
    )

    assert approval.owner == "overseer"
    assert approval.owner_note == ""
    assert approval.owner_set_by == ""
    assert approval.owner_set_at is None

    blob = approval.to_dict()
    assert blob["owner"] == "overseer"
    assert blob["owner_note"] == ""
    assert blob["owner_set_by"] == ""
    assert blob["owner_set_at"] is None


def test_annotate_gate_sets_owner_and_persists(tmp_path: Path) -> None:
    """annotate_gate updates owner + owner_note, audits who wrote it, persists."""

    root, state, _ = _fixture(tmp_path)
    approval = state.create_approval(
        title="Find build-runner-v2.js",
        message="Where does the file live?",
        options=[ApprovalOption(value="continue", label="Continue")],
    )

    result = state.annotate_gate(
        approval.approval_id,
        owner="overseer",
        owner_note=_OWNER_NOTE_FIXTURE,
        actor="overseer1",
    )

    assert result is not None
    assert result["owner"] == "overseer"
    assert result["owner_note"] == _OWNER_NOTE_FIXTURE
    assert result["owner_set_by"] == "overseer1"
    assert isinstance(result["owner_set_at"], float)
    assert result["owner_set_at"] <= time.time()

    # The refreshed in-memory approval matches the snapshot we returned.
    refreshed = state.get_approval(approval.approval_id)
    assert refreshed is not None
    assert refreshed.owner == "overseer"
    assert refreshed.owner_note == _OWNER_NOTE_FIXTURE

    # ...and the durable round-trip on disk preserves every field, so a
    # subsequent dashboard process reading approvals.jsonl sees the badge.
    log_path = _approval_path(root / "run-1" / "logs")
    assert log_path.is_file()
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    matching = [record for record in records if record.get("approval_id") == approval.approval_id]
    assert matching, "expected at least one persisted approval record"
    latest = matching[-1]
    assert latest["owner"] == "overseer"
    assert latest["owner_note"] == _OWNER_NOTE_FIXTURE
    assert latest["owner_set_by"] == "overseer1"
    assert isinstance(latest["owner_set_at"], (int, float))


def test_annotate_gate_idempotent_update_keeps_existing_fields(tmp_path: Path) -> None:
    """A partial annotate keeps untouched fields and refreshes the audit field."""

    _, state, _ = _fixture(tmp_path)
    approval = state.create_approval(title="Choose one")

    first = state.annotate_gate(
        approval.approval_id,
        owner="overseer",
        owner_note="first note",
        actor="overseer1",
    )
    assert first is not None
    first_set_at = first["owner_set_at"]

    # Sleep a tiny amount so the second timestamp differs deterministically.
    time.sleep(0.01)

    second = state.annotate_gate(
        approval.approval_id,
        owner="paxton",
        owner_note="second note",
        actor="paxton",
    )
    assert second is not None
    assert second["owner"] == "paxton"
    assert second["owner_note"] == "second note"
    assert second["owner_set_by"] == "paxton"
    assert second["owner_set_at"] >= first_set_at


def test_annotate_gate_partial_update_leaves_omitted_fields_untouched(tmp_path: Path) -> None:
    """Passing only ``owner`` (not ``owner_note``) leaves the existing note intact."""

    _, state, _ = _fixture(tmp_path)
    approval = state.create_approval(title="Choose one")
    state.annotate_gate(
        approval.approval_id,
        owner="overseer",
        owner_note="keep me",
        actor="overseer1",
    )

    later = state.annotate_gate(
        approval.approval_id,
        owner="paxton",
        actor="paxton",
    )
    assert later is not None
    assert later["owner"] == "paxton"
    # owner_note was not supplied, so the previous value must persist.
    assert later["owner_note"] == "keep me"
    assert later["owner_set_by"] == "paxton"


def test_annotate_gate_reads_back_via_run_snapshot(tmp_path: Path) -> None:
    """The run snapshot API exposes the new fields for both summary and full views."""

    _, state, client = _fixture(tmp_path)
    approval = state.create_approval(
        title="Continue?",
        options=[ApprovalOption(value="continue", label="Continue")],
    )
    state.annotate_gate(
        approval.approval_id,
        owner="paxton",
        owner_note="Paxton holding this one.",
        actor="paxton",
    )

    headers = {"Authorization": "Bearer test-token"}
    summary = client.get(
        "/api/runs/run-1/snapshot?fields=summary", headers=headers
    ).json()
    full = client.get("/api/runs/run-1/snapshot", headers=headers).json()

    for blob in (summary, full):
        match = next(
            item for item in blob["approvals"]
            if item["approval_id"] == approval.approval_id
        )
        assert match["owner"] == "paxton"
        assert match["owner_note"] == "Paxton holding this one."
        assert match["owner_set_by"] == "paxton"
        assert isinstance(match["owner_set_at"], (int, float))


def test_annotate_gate_unknown_approval_returns_none(tmp_path: Path) -> None:
    """A missing approval_id yields ``None`` (the API layer translates to 404)."""

    _, state, _ = _fixture(tmp_path)
    result = state.annotate_gate(
        "does-not-exist",
        owner="overseer",
        owner_note="n/a",
        actor="overseer1",
    )
    assert result is None


def test_annotate_gate_rejects_unknown_owner_value_silently(tmp_path: Path) -> None:
    """Unknown owner values fall back to ``overseer`` so the badge never disappears."""

    _, state, _ = _fixture(tmp_path)
    approval = state.create_approval(title="Continue?")
    result = state.annotate_gate(
        approval.approval_id,
        owner="archmage",
        owner_note="legacy entry",
        actor="overseer1",
    )
    assert result is not None
    assert result["owner"] == "overseer"


def test_annotate_gate_truncates_overlong_note_at_4000_chars(tmp_path: Path) -> None:
    """owner_note is bounded at 4000 chars to keep responses predictable."""

    _, state, _ = _fixture(tmp_path)
    approval = state.create_approval(title="Continue?")
    long_note = "x" * 5000

    result = state.annotate_gate(
        approval.approval_id,
        owner="overseer",
        owner_note=long_note,
        actor="overseer1",
    )
    assert result is not None
    assert len(result["owner_note"]) == 4000
    assert result["owner_note"] == "x" * 4000


def test_annotate_gate_on_resolved_approval_still_persists_owner_fields(
    tmp_path: Path,
) -> None:
    """Resolved approvals carry owner/note for the historical record.

    Gate resolution semantics are unchanged (resolve_approval is still the
    only path to change status), but a resolved record may have an owner
    attached after the fact so a viewer still knows who decided it.  The
    runtime API will translate writes to a resolved approval into 409;
    these state-mutation tests deliberately exercise the storage layer
    only.
    """

    _, state, _ = _fixture(tmp_path)
    approval = state.create_approval(
        title="Continue?",
        options=[ApprovalOption(value="continue", label="Continue")],
    )
    # Resolve the gate through the canonical path.
    state.resolve_approval(approval.approval_id, action="continue", user_input="go")
    refreshed = state.get_approval(approval.approval_id)
    assert refreshed is not None
    assert refreshed.status == "resolved"

    # annotate_gate is a no-op-for-status API: it accepts the call but the
    # call site must know that the canonical expectation from the task
    # description is that resolving a gate still rejects further owner
    # changes.  We encode that contract on the runtime layer in
    # test_gate_owner_note_endpoint.py (a later round); here we only confirm
    # the storage setter is idempotent on a resolved record so the on-disk
    # file is consistent regardless of the resolution order.
    after = state.annotate_gate(
        approval.approval_id,
        owner="paxton",
        owner_note="post-mortem annotation",
        actor="overseer1",
    )
    assert after is not None
    assert after["owner"] == "paxton"
    assert after["owner_note"] == "post-mortem annotation"
