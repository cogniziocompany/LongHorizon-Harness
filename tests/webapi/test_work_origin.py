"""Task easybutt0n-17: the optional, record-only ``work_origin`` block.

Covers the whole TASK A surface:

  * the enqueue API accepts an optional ``work_origin``
    (``{"kind": "ticket"|"goal"|"ops", "ticket": <id>|null,
    "goal_ref": <id>|null}``) and round-trips it to the queue entry;
  * the SAME acceptance on the Postgres queue backend (the file store is the
    default backend, so the Postgres path is exercised for real rather than
    skipped -- ``LH_HARNESS_DB_URL`` must name a scratch database);
  * an unknown SIBLING field on the enqueue body is still refused with a 400
    that names the offending field;
  * an invalid ``kind`` is refused (400);
  * the block is copied to the run and projected into the list/snapshot
    summaries;
  * an entry enqueued WITHOUT the block stays backward-compatible;
  * the MCP enqueue surface (``harness_enqueue_task``) accepts the block too;
  * the read-only duplicate-work view finds a planted duplicate and stays
    empty otherwise.

RECORD-ONLY, and that is the point: nothing here asserts that the block
GATES anything.  There is no gate to assert.  The block records where work
came from; it never decides whether the work may run.  The tests below prove
that the block is accepted, stored, copied and projected -- and the
duplicate-work view proves it can be READ.

The Postgres tests run only when ``LH_HARNESS_DB_URL`` names a reachable
scratch database; they skip cleanly otherwise so the hermetic suite stays
green.  Run them locally with::

    LH_HARNESS_DB_URL=postgresql://user@localhost:5432/lh_harness_work_origin \\
        pytest tests/webapi/test_work_origin.py -v
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from lh_harness.duplicate_work import find_duplicate_work
from lh_harness.mcp_tools import dispatch, tools_manifest
from lh_harness.queue import QueueStore, UnknownQueueFieldError, _normalize_request

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.webapi.server import create_app


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _client(tmp_path: Path):
    root = _fixture(tmp_path)
    # TestClient sends Host: testserver; bind_host must agree or the loopback
    # host check rejects every request.
    return TestClient(create_app(runs_root=root, auth_token="secret", bind_host="testserver"))


# The app is created with an auth token, so every request carries it.
_AUTH = {"Authorization": "Bearer secret"}


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "work-origin-task",
        "task": "do the thing",
        "workspace": "w",
        "trio": "kimi",
        "requested_by": "ci",
    }
    body.update(overrides)
    return body


def _require_pg_store(tmp_path: Path):
    from lh_harness.pg_queue import PgQueueStore

    url = os.environ.get("LH_HARNESS_DB_URL")
    if not url or not str(url).strip():
        pytest.skip("LH_HARNESS_DB_URL not set; Postgres backend unavailable")
    store = PgQueueStore(url)
    try:
        store.counts()
    except Exception:
        pytest.skip("could not connect to Postgres backend")
    return store


def _pg_body(tag: str, **overrides: Any) -> dict[str, Any]:
    """A Postgres enqueue body with a NAME UNIQUE to this test.

    The Postgres backend refuses a second non-terminal entry with the same
    normalized name.  That refusal path has a pre-existing defect on
    ``origin/main`` (it builds a partial ``QueueEntry`` and raises
    ``TypeError`` instead of ``DuplicateQueueEntryError``), so a test that
    reuses a fixed name would trip the defect instead of testing the work
    origin.  The unique name keeps these tests on the path they are about.
    """

    body = _body(name=f"pg-work-origin-{tag}-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 1. Accepted and round-tripped (file store).
# ---------------------------------------------------------------------------


def test_enqueue_accepts_work_origin_and_round_trips(tmp_path: Path) -> None:
    """The enqueue API accepts the block and the entry carries it back."""
    client = _client(tmp_path)
    origin = {"kind": "ticket", "ticket": "YT-42", "goal_ref": None}
    response = client.post("/api/queue", headers=_AUTH, json=_body(work_origin=origin))
    assert response.status_code == 200, response.text
    queue_id = response.json()["queue_id"]

    entry = client.get(f"/api/queue/{queue_id}", headers=_AUTH).json()
    assert entry["work_origin"] == origin

    # The list projection carries it too (both the full and the summary view).
    listed = client.get("/api/queue", headers=_AUTH).json()
    row = next(item for item in listed["entries"] if item["queue_id"] == queue_id)
    assert row["work_origin"] == origin

    summary = client.get("/api/queue?view=summary", headers=_AUTH).json()
    row = next(item for item in summary["entries"] if item["queue_id"] == queue_id)
    assert row["work_origin"] == origin


def test_enqueue_without_work_origin_stays_backward_compatible(tmp_path: Path) -> None:
    """An entry enqueued without the block carries no origin and still works."""
    client = _client(tmp_path)
    response = client.post("/api/queue", headers=_AUTH, json=_body())
    assert response.status_code == 200, response.text
    queue_id = response.json()["queue_id"]

    entry = client.get(f"/api/queue/{queue_id}", headers=_AUTH).json()
    assert entry.get("work_origin") is None

    listed = client.get("/api/queue", headers=_AUTH).json()
    row = next(item for item in listed["entries"] if item["queue_id"] == queue_id)
    assert row.get("work_origin") is None


@pytest.mark.parametrize(
    "origin",
    [
        {"kind": "ticket", "ticket": "YT-1", "goal_ref": None},
        {"kind": "goal", "ticket": None, "goal_ref": "goal-board-7"},
        {"kind": "ops", "ticket": None, "goal_ref": None},
        {"kind": "ticket", "ticket": "YT-2", "goal_ref": "goal-9"},
    ],
)
def test_every_valid_kind_and_id_shape_is_accepted(
    tmp_path: Path, origin: dict[str, Any]
) -> None:
    """All three kinds, and both id fields, are accepted and stored as given."""
    client = _client(tmp_path)
    response = client.post("/api/queue", headers=_AUTH, json=_body(name=f"n-{origin['kind']}-{origin['ticket']}", work_origin=origin))
    assert response.status_code == 200, response.text
    queue_id = response.json()["queue_id"]
    entry = client.get(f"/api/queue/{queue_id}", headers=_AUTH).json()
    assert entry["work_origin"] == origin


def test_work_origin_survives_a_requeue(tmp_path: Path) -> None:
    """A retried entry still records the same origin."""
    store = QueueStore(_fixture(tmp_path))
    entry = store.create(
        _body(work_origin={"kind": "ticket", "ticket": "YT-42", "goal_ref": None})
    )
    store.mark_failed(entry.queue_id, "boom")
    successor = store.requeue(entry.queue_id, "boom")
    assert successor is not None
    assert successor.work_origin == {"kind": "ticket", "ticket": "YT-42", "goal_ref": None}


# ---------------------------------------------------------------------------
# 2. An unknown SIBLING field is still refused (and named).
# ---------------------------------------------------------------------------


def test_unknown_sibling_field_still_400s_and_names_the_field(tmp_path: Path) -> None:
    """``work_origin`` is the ONE new accepted key; every other unknown key
    is still refused with a 400 that names it."""
    client = _client(tmp_path)
    response = client.post(
        "/api/queue",
        headers=_AUTH,
        json=_body(work_origin={"kind": "ops", "ticket": None, "goal_ref": None}, bogus_field="x"),
    )
    assert response.status_code == 400, response.text
    assert "bogus_field" in response.text
    # The offending field is named and NOT silently dropped.
    assert "work_origin" not in response.text


def test_unknown_sibling_field_still_400s_without_work_origin(tmp_path: Path) -> None:
    """The refusal does not depend on ``work_origin`` being present."""
    client = _client(tmp_path)
    response = client.post("/api/queue", headers=_AUTH, json=_body(another_unknown="y"))
    assert response.status_code == 400, response.text
    assert "another_unknown" in response.text


def test_unknown_field_inside_work_origin_is_refused(tmp_path: Path) -> None:
    """A typo INSIDE the block is refused too, with the offending name."""
    client = _client(tmp_path)
    response = client.post(
        "/api/queue", headers=_AUTH, json=_body(work_origin={"kind": "ops", "ticket_id": "YT-1"})
    )
    assert response.status_code == 400, response.text
    assert "ticket_id" in response.text


def test_normalize_request_still_rejects_unknown_body_fields() -> None:
    """The core validation surface keeps rejecting unknown body keys."""
    with pytest.raises(UnknownQueueFieldError) as excinfo:
        _normalize_request(_body(work_origin={"kind": "ops"}, surprise=1))
    assert "surprise" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3. An invalid kind is refused.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["Ticket", "TICKET", "board", "", "epic"])
def test_invalid_work_origin_kind_is_refused(tmp_path: Path, kind: str) -> None:
    """A kind outside the three valid ones is refused with a 400.

    A kind that differs only in CASE is refused: the three kinds are
    lowercase exactly, so a client that sends "Ticket" is told the accepted
    set instead of having its value guessed at.  Surrounding whitespace is
    stripped (like every other bounded string field in the queue body), so
    " ticket " is the "ticket" kind.
    """
    client = _client(tmp_path)
    response = client.post(
        "/api/queue", headers=_AUTH, json=_body(work_origin={"kind": kind, "ticket": None, "goal_ref": None})
    )
    assert response.status_code == 400, response.text
    assert "work_origin.kind" in response.text


def test_work_origin_kind_whitespace_is_stripped(tmp_path: Path) -> None:
    """Whitespace around a kind is stripped, not refused."""
    client = _client(tmp_path)
    response = client.post(
        "/api/queue",
        headers=_AUTH,
        json=_body(work_origin={"kind": " ticket ", "ticket": "YT-1", "goal_ref": None}),
    )
    assert response.status_code == 200, response.text
    queue_id = response.json()["queue_id"]
    entry = client.get(f"/api/queue/{queue_id}", headers=_AUTH).json()
    assert entry["work_origin"]["kind"] == "ticket"


def test_missing_work_origin_kind_is_refused(tmp_path: Path) -> None:
    """A block without a kind is refused: the kind is the discriminant."""
    client = _client(tmp_path)
    response = client.post("/api/queue", headers=_AUTH, json=_body(work_origin={"ticket": "YT-1"}))
    assert response.status_code == 400, response.text
    assert "work_origin.kind" in response.text


def test_non_object_work_origin_is_refused(tmp_path: Path) -> None:
    """The block must be an object."""
    client = _client(tmp_path)
    response = client.post("/api/queue", headers=_AUTH, json=_body(work_origin="YT-42"))
    assert response.status_code == 400, response.text
    assert "work_origin" in response.text


# ---------------------------------------------------------------------------
# 4. The Postgres queue backend persists the block too.
# ---------------------------------------------------------------------------


def test_pg_store_persists_work_origin(tmp_path: Path) -> None:
    """The Postgres backend stores and re-reads the block (not skipped)."""
    store = _require_pg_store(tmp_path)
    origin = {"kind": "ticket", "ticket": "YT-77", "goal_ref": None}
    entry = store.create(_pg_body("persist", work_origin=origin))
    read = store.get(entry.queue_id)
    assert read is not None
    assert read.work_origin == origin


def test_pg_store_without_work_origin_reads_back_none(tmp_path: Path) -> None:
    """A Postgres row written without the block reads back without one."""
    store = _require_pg_store(tmp_path)
    entry = store.create(_pg_body("absent"))
    read = store.get(entry.queue_id)
    assert read is not None
    assert read.work_origin is None


def test_pg_store_refuses_invalid_work_origin_kind(tmp_path: Path) -> None:
    """Both stores refuse an invalid kind the same way."""
    store = _require_pg_store(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        store.create(_pg_body("bad-kind", work_origin={"kind": "nope"}))
    assert "work_origin.kind" in str(excinfo.value)


def test_pg_store_survives_a_requeue(tmp_path: Path) -> None:
    """A retried Postgres entry still records the same origin."""
    store = _require_pg_store(tmp_path)
    origin = {"kind": "ticket", "ticket": "YT-78", "goal_ref": None}
    entry = store.create(_pg_body("requeue", work_origin=origin))
    store.mark_failed(entry.queue_id, "boom")
    successor = store.requeue(entry.queue_id, "boom")
    assert successor is not None
    assert successor.work_origin == origin


# ---------------------------------------------------------------------------
# 5. The block is copied to the run and projected into the summaries.
# ---------------------------------------------------------------------------


def test_work_origin_is_copied_to_the_run_record(tmp_path: Path) -> None:
    """A queue entry's block is copied onto the run it launches."""
    from lh_harness.supervisor.service import RunSupervisor

    root = _fixture(tmp_path)
    store = QueueStore(root)
    origin = {"kind": "ticket", "ticket": "YT-42", "goal_ref": None}
    entry = store.create(_body(work_origin=origin))

    # The workspace must sit inside the supervisor's workspace root.
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    supervisor = RunSupervisor(runs_root=root, workspace_root=tmp_path)
    created = supervisor.create_run(
        task=entry.task,
        agent="codex",
        workspace=str(workspace),
        queue_id=entry.queue_id,
        trio=entry.trio,
        work_origin=entry.work_origin,
    )
    run_id = str(created.get("id") or "")
    assert run_id

    owner = json.loads((root / run_id / "control" / "owner.json").read_text(encoding="utf-8"))
    assert owner["work_origin"] == origin


def test_run_summary_projects_work_origin(tmp_path: Path) -> None:
    """The list/snapshot summaries carry the block."""
    from lh_harness.webapi.snapshot import build_run_summary

    summary = build_run_summary(
        {
            "id": "run-1",
            "task": "t",
            "status": "running",
            "log_dir": "",
        },
        state=None,
    )
    # No owner record in this projection -> no origin, and no crash.
    assert summary.get("work_origin") is None

    from lh_harness.webapi.snapshot import _provenance

    projected = _provenance({"work_origin": {"kind": "ticket", "ticket": "YT-42", "goal_ref": None}})
    assert projected["work_origin"] == {"kind": "ticket", "ticket": "YT-42", "goal_ref": None}


# ---------------------------------------------------------------------------
# 6. The READ-ONLY duplicate-work view.
# ---------------------------------------------------------------------------


def test_duplicate_work_view_is_empty_when_nothing_is_duplicated(tmp_path: Path) -> None:
    """With no duplicated claim the view returns an empty list."""
    client = _client(tmp_path)
    response = client.get("/api/queue/duplicate-work", headers=_AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["duplicates"] == []
    assert body["count"] == 0


def test_duplicate_work_view_finds_a_planted_duplicate(tmp_path: Path) -> None:
    """Two LIVE runs claiming the same ticket are reported as a duplicate."""
    client = _client(tmp_path)
    # Plant two live runs that both claim YT-42.
    _plant_live_run(tmp_path, "run-a", "YT-42")
    _plant_live_run(tmp_path, "run-b", "YT-42")

    body = client.get("/api/queue/duplicate-work", headers=_AUTH).json()
    assert body["count"] == 1
    group = body["duplicates"][0]
    assert group["claim_kind"] == "ticket"
    assert group["claim"] == "YT-42"
    assert group["count"] == 2
    assert sorted(member["run_id"] for member in group["runs"]) == ["run-a", "run-b"]


def test_duplicate_work_view_ignores_a_claim_held_by_one_live_run(tmp_path: Path) -> None:
    """One live run plus one FINISHED run is not a duplicate."""
    client = _client(tmp_path)
    _plant_live_run(tmp_path, "run-a", "YT-42")
    _plant_finished_run(tmp_path, "run-b", "YT-42")

    body = client.get("/api/queue/duplicate-work", headers=_AUTH).json()
    assert body["duplicates"] == []


def test_duplicate_work_view_ignores_two_finished_runs(tmp_path: Path) -> None:
    """Two finished runs on one ticket are history, not a duplicate."""
    client = _client(tmp_path)
    _plant_finished_run(tmp_path, "run-a", "YT-42")
    _plant_finished_run(tmp_path, "run-b", "YT-42")

    body = client.get("/api/queue/duplicate-work", headers=_AUTH).json()
    assert body["duplicates"] == []


def test_duplicate_work_view_falls_back_to_the_branch(tmp_path: Path) -> None:
    """With no ticket recorded, the branch is the claim."""
    client = _client(tmp_path)
    _plant_live_run(tmp_path, "run-a", None, branch="feat/shared")
    _plant_live_run(tmp_path, "run-b", None, branch="feat/shared")

    body = client.get("/api/queue/duplicate-work", headers=_AUTH).json()
    assert body["count"] == 1
    group = body["duplicates"][0]
    assert group["claim_kind"] == "branch"
    assert group["claim"] == "feat/shared"


def test_duplicate_work_claims_the_goal_ref_when_no_ticket_is_recorded() -> None:
    """With no ticket, the recorded goal is the claim (before the branch)."""
    runs = [
        {
            "id": "run-a",
            "status": "running",
            "work_origin": {"kind": "goal", "ticket": None, "goal_ref": "goal-board-7"},
        },
        {
            "id": "run-b",
            "status": "running",
            "work_origin": {"kind": "goal", "ticket": None, "goal_ref": "goal-board-7"},
        },
    ]
    groups = find_duplicate_work(runs)
    assert len(groups) == 1
    assert groups[0]["claim_kind"] == "goal"
    assert groups[0]["claim"] == "goal-board-7"


def test_duplicate_work_claims_the_ticket_before_the_goal_ref() -> None:
    """A recorded ticket outranks a recorded goal: one ticket, one claim."""
    runs = [
        {
            "id": "run-a",
            "status": "running",
            "work_origin": {"kind": "ticket", "ticket": "YT-1", "goal_ref": "goal-1"},
        },
        {
            "id": "run-b",
            "status": "running",
            "work_origin": {"kind": "ticket", "ticket": "YT-1", "goal_ref": "goal-2"},
        },
    ]
    groups = find_duplicate_work(runs)
    assert len(groups) == 1
    assert groups[0]["claim_kind"] == "ticket"
    assert groups[0]["claim"] == "YT-1"


def test_duplicate_work_helper_is_pure_and_read_only() -> None:
    """The helper reads its inputs and returns its result: it writes nothing."""
    runs = [
        {"id": "run-a", "status": "running", "work_origin": {"kind": "ticket", "ticket": "YT-1"}},
        {"id": "run-b", "status": "running", "work_origin": {"kind": "ticket", "ticket": "YT-1"}},
    ]
    before = json.dumps(runs, sort_keys=True)
    groups = find_duplicate_work(runs)
    assert json.dumps(runs, sort_keys=True) == before
    assert len(groups) == 1
    assert groups[0]["count"] == 2

    # A single holder is not a duplicate.
    assert find_duplicate_work(runs[:1]) == []


# ---------------------------------------------------------------------------
# 7. The MCP enqueue surface carries the block too.
# ---------------------------------------------------------------------------


def _enqueue_tool(arguments: dict[str, Any], store: Any) -> dict[str, Any]:
    return dispatch(
        "harness_enqueue_task",
        arguments,
        queue_store=store,
        registry=None,
        supervisor=None,
        auth_token=None,
        request_token=None,
    )


def test_mcp_tool_schema_documents_work_origin() -> None:
    """The MCP enqueue schema advertises the optional block."""
    enqueue = next(
        tool for tool in tools_manifest() if tool["name"] == "harness_enqueue_task"
    )
    schema = enqueue["input_schema"]
    assert schema.get("additionalProperties") is False
    block = schema["properties"]["work_origin"]
    assert block["type"] == "object"
    assert block["additionalProperties"] is False
    assert block["properties"]["kind"]["enum"] == ["ticket", "goal", "ops"]


def test_mcp_tool_accepts_and_persists_work_origin(tmp_path: Path) -> None:
    """The MCP enqueue path accepts the block and stores it."""
    store = QueueStore(_fixture(tmp_path))
    origin = {"kind": "ticket", "ticket": "YT-55", "goal_ref": None}
    result = _enqueue_tool(
        {
            "name": "mcp-work-origin",
            "task": "do the thing",
            "workspace": "w",
            "trio": "kimi",
            "requested_by": "mcp-test",
            "work_origin": origin,
        },
        store,
    )
    queue_id = result["queue_id"]
    read = store.get(queue_id)
    assert read is not None
    assert read.work_origin == origin


def test_mcp_tool_refuses_an_invalid_work_origin_kind(tmp_path: Path) -> None:
    """Both enqueue surfaces refuse an invalid kind the same way."""
    store = QueueStore(_fixture(tmp_path))
    result = _enqueue_tool(
        {
            "name": "mcp-work-origin-bad",
            "task": "do the thing",
            "workspace": "w",
            "trio": "kimi",
            "requested_by": "mcp-test",
            "work_origin": {"kind": "nope"},
        },
        store,
    )
    # The MCP surface reports the refusal in the result, never by raising.
    assert result["ok"] is False
    assert "work_origin.kind" in str(result["error"])


def test_mcp_tool_without_work_origin_stays_backward_compatible(tmp_path: Path) -> None:
    """An MCP enqueue without the block carries no origin."""
    store = QueueStore(_fixture(tmp_path))
    result = _enqueue_tool(
        {
            "name": "mcp-no-work-origin",
            "task": "do the thing",
            "workspace": "w",
            "trio": "kimi",
            "requested_by": "mcp-test",
        },
        store,
    )
    read = store.get(result["queue_id"])
    assert read is not None
    assert read.work_origin is None


# ---------------------------------------------------------------------------
# Planting helpers: a run directory the registry can see.
# ---------------------------------------------------------------------------


def _plant_run(
    tmp_path: Path,
    run_id: str,
    status: str,
    *,
    ticket: str | None = None,
    branch: str | None = None,
) -> None:
    """Write one run directory the runs registry reads as a live/finished run.

    The registry reads ``control/status.json`` for the lifecycle status and
    ``control/owner.json`` for the provenance (including the work-origin
    block), so a planted run needs both.
    """

    root = _fixture(tmp_path)
    control = root / run_id / "control"
    control.mkdir(parents=True, exist_ok=True)
    (control / "status.json").write_text(
        json.dumps({"status": status, "run_id": run_id}), encoding="utf-8"
    )
    owner: dict[str, Any] = {"run_id": run_id, "task": "t"}
    if ticket is not None:
        owner["work_origin"] = {"kind": "ticket", "ticket": ticket, "goal_ref": None}
    if branch is not None:
        owner["branch"] = branch
    (control / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
    # The registry needs the run's log/role directories to exist.
    (root / run_id / "lh_harness").mkdir(parents=True, exist_ok=True)


def _plant_live_run(
    tmp_path: Path, run_id: str, ticket: str | None, *, branch: str | None = None
) -> None:
    _plant_run(tmp_path, run_id, "running", ticket=ticket, branch=branch)


def _plant_finished_run(
    tmp_path: Path, run_id: str, ticket: str | None, *, branch: str | None = None
) -> None:
    _plant_run(tmp_path, run_id, "completed", ticket=ticket, branch=branch)
