"""Task A3b: mcp_tools.dispatch feeds live open asks (gates, blocked entries) into list_open_asks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from lh_harness.mcp_tools import dispatch


class _FakeState:
    def __init__(self, approvals: list[dict[str, Any]]) -> None:
        self._approvals = approvals

    def list_approvals(self) -> list[dict[str, Any]]:
        return list(self._approvals)


class _FakeRegistry:
    def __init__(self, states: dict[str, _FakeState]) -> None:
        self._states = states

    def state_for(self, run_id: str) -> _FakeState | None:
        return self._states.get(run_id)


class _FakeSupervisor:
    def __init__(self, summaries: list[dict[str, Any]]) -> None:
        self._summaries = summaries
        self.asked: list[Any] = []

    def list_run_summaries(self, statuses: set[str] | None = None) -> list[dict[str, Any]]:
        self.asked.append(statuses)
        return [s for s in self._summaries if statuses is None or s["status"] in statuses]


class _FakeQueue:
    def __init__(self, entries: list[Any]) -> None:
        self._entries = entries

    def list(self) -> list[Any]:
        return list(self._entries)


def _call(tmp_path: Path, *, registry: Any = None, supervisor: Any = None, queue_store: Any = None) -> dict[str, Any]:
    return dispatch(
        "list_open_asks",
        {},
        queue_store=queue_store,
        registry=registry,
        supervisor=supervisor,
        auth_token=None,
        request_token=None,
        overseer_root=str(tmp_path / "no-archive"),
    )


def _live_world() -> tuple[_FakeRegistry, _FakeSupervisor, _FakeQueue]:
    registry = _FakeRegistry(
        {
            "run-gated": _FakeState(
                [
                    {"approval_id": "old", "status": "resolved", "title": "already answered"},
                    {"approval_id": "ap1", "status": "pending", "title": "Push the branch?"},
                ]
            )
        }
    )
    supervisor = _FakeSupervisor(
        [
            {"id": "run-gated", "status": "waiting_approval"},
            {"id": "run-busy", "status": "running"},
        ]
    )
    queue_store = _FakeQueue(
        [
            SimpleNamespace(queue_id="q-blocked", name="demo-task", reason="waiting on Paxton", status="blocked"),
            SimpleNamespace(queue_id="q-pending", name="other", reason=None, status="pending"),
        ]
    )
    return registry, supervisor, queue_store


def test_dispatch_returns_live_gate_and_blocked_rows(tmp_path: Path) -> None:
    registry, supervisor, queue_store = _live_world()
    result = _call(tmp_path, registry=registry, supervisor=supervisor, queue_store=queue_store)
    assert result["ok"] is True
    assert [r["id"] for r in result["rows"]] == ["gate-run-gated-ap1", "blocked-q-blocked"]
    assert result["live_rows"] == 2
    assert result["file_note"]
    assert supervisor.asked == [{"waiting_approval"}]


def test_dispatch_without_live_state_keeps_file_only_behaviour(tmp_path: Path) -> None:
    result = _call(tmp_path)
    assert result["ok"] is False


def test_one_broken_run_does_not_break_open_asks(tmp_path: Path) -> None:
    class _Boom(_FakeRegistry):
        def state_for(self, run_id: str) -> _FakeState | None:
            raise RuntimeError("corrupt run dir")

    _, supervisor, queue_store = _live_world()
    result = _call(tmp_path, registry=_Boom({}), supervisor=supervisor, queue_store=queue_store)
    assert result["ok"] is True
    assert [r["id"] for r in result["rows"]] == ["blocked-q-blocked"]


def test_supervisor_without_summaries_yields_queue_rows_only(tmp_path: Path) -> None:
    registry, _, queue_store = _live_world()
    result = _call(tmp_path, registry=registry, supervisor=object(), queue_store=queue_store)
    assert result["ok"] is True
    assert [r["id"] for r in result["rows"]] == ["blocked-q-blocked"]
