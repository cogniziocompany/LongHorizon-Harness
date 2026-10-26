"""Tests for the tev1 lane optimizer (TEV1-LANE-OPTIMIZER-a, Paxton 2026-10-26).

All tests are hermetic: the tev1 HTTP call is a stub (or a raising stub for
the fallback case), the runs root is a ``tmp_path`` file queue, and the
settings store is the same metadata/capacity proxy pattern the trio-router
tests use.  No test ever reaches the model lane, the real settings DB, or a
real workspace — the optimizer is strictly suggest-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lh_harness import lane_optimizer
from lh_harness.queue import QueueEntry


@pytest.fixture(autouse=True)
def _clean_router_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic: a leaked TRIO_ROUTER_* env must not enable the real model call."""

    for name in (
        "TRIO_ROUTER_ENABLED",
        "TRIO_ROUTER_URL",
        "TRIO_ROUTER_MODEL",
        "TRIO_ROUTER_MIN_CONFIDENCE",
        "TRIO_ROUTER_SMALL_FLOOR",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------- fixtures --
def _queue_entry(
    queue_id: str,
    *,
    trio: str,
    status: str = "pending",
    workspace: str = "/tmp/ws-a",
    priority: int = 1,
    skip_reasons: list[str] | None = None,
) -> dict[str, Any]:
    entry = QueueEntry(
        queue_id=queue_id,
        name=f"n-{queue_id}",
        task="t",
        workspace=workspace,
        max_rounds=3,
        trio=trio,
        priority=priority,
        requested_by="ci",
        status=status,
        run_id=f"r-{queue_id}" if status == "launched" else None,
    )
    entry.skip_reasons = list(skip_reasons or [])
    return entry.to_dict()


def _runs_root(tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    for entry in entries:
        (queue_dir / f"{entry['queue_id']}.json").write_text(
            json.dumps(entry, ensure_ascii=False), encoding="utf-8"
        )
    return tmp_path


class _StoreProxy:
    """Minimal SettingsStore stand-in: queue_trios/queue_capacity_rows + metadata()."""

    def __init__(
        self,
        trios: list[dict[str, Any]],
        caps: dict[str, dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._trios = trios
        self._caps = caps
        self._metadata = metadata or {}

    def queue_trios(self) -> list[dict[str, Any]]:
        return list(self._trios)

    def queue_capacity_rows(self) -> dict[str, dict[str, Any]]:
        return {name: dict(row) for name, row in self._caps.items()}

    def metadata(self, name: str) -> dict[str, Any]:
        if name in self._metadata:
            return {"name": name, "secret": False, "set": True, "value": self._metadata[name]}
        raise KeyError(name)


def _store(
    *,
    trios: list[str] | None = None,
    caps: dict[str, int] | None = None,
    metadata: dict[str, Any] | None = None,
) -> _StoreProxy:
    names = trios if trios is not None else ["glm", "zai"]
    return _StoreProxy(
        [{"name": name, "agent": "claude_code", "model": "m", "mcp_profile": None, "enabled": True}
         for name in names],
        {name: {"max": cap, "max_peak": None} for name, cap in (caps or {}).items()},
        metadata,
    )


def _usage(lane: str, used_pct: float, *, plan: str = "pro") -> dict[str, Any]:
    return {
        "trio": lane,
        "provider": lane,
        "plan": plan,
        "window": "weekly",
        "used_pct": used_pct,
        "reset_at": "2026-10-30T00:00:00Z",
        "observed_at": "2026-10-26T07:00:00Z",
        "observed_by": "fleet-meter",
    }


def _snapshot(
    tmp_path: Path,
    entries: list[dict[str, Any]],
    store: Any,
    usage_rows: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    return lane_optimizer.build_snapshot(_runs_root(tmp_path, entries), store, usage_rows, now=1761997500.0)


def _suggest(snapshot: dict[str, Any], http_call: Any = None) -> dict[str, Any]:
    return lane_optimizer.suggest(
        snapshot,
        store=object(),  # a store with no metadata(): the router reads env only -> disabled
        http_call=http_call,
        now=1761997500.0,
    )


# ------------------------------------------------------------------- cases --
def test_full_lane_plus_idle_paid_lane_suggests_move(tmp_path: Path) -> None:
    """glm 5/5 with pending + zai cap 1 idle at 8% used -> move onto zai."""

    entries = [
        *[_queue_entry(f"run-{i}", trio="glm", status="launched", workspace=f"/tmp/ws-run{i}")
          for i in range(5)],
        _queue_entry("q-blocked", trio="glm", skip_reasons=["glm at capacity"]),
    ]
    store = _store(caps={"glm": 5, "zai": 1})
    snapshot = _snapshot(tmp_path, entries, store, [_usage("glm", 60.0), _usage("zai", 8.0)])

    assert snapshot["trios"]["glm"]["active"] == 5
    assert snapshot["trios"]["glm"]["pending"] == 1
    assert snapshot["trios"]["zai"]["active"] == 0

    plan = _suggest(snapshot)
    moves = [move for move in plan["moves"] if move["queue_id"] == "q-blocked"]
    assert moves, f"expected a move for the blocked entry, got {plan}"
    move = moves[0]
    assert move["to_trio"] == "zai"
    assert move["to_workspace"] == "/tmp/ws-a"
    assert "idle paid lane" in move["reason"]
    assert plan["source"] == "rules"


def test_dirty_workspace_suggests_clone_never_discard(tmp_path: Path) -> None:
    """A pending entry blocked by a dirty tree gets add_clone; never stash/reset/discard."""

    dirty_reason = (
        "workspace /tmp/ws-dirty occupied: dirty tree (git status --porcelain non-empty)"
    )
    entries = [
        _queue_entry("run-live", trio="glm", status="launched", workspace="/tmp/ws-run0"),
        _queue_entry("q-ws", trio="glm", workspace="/tmp/ws-dirty", skip_reasons=[dirty_reason]),
    ]
    store = _store(caps={"glm": 1, "zai": 1})
    snapshot = _snapshot(tmp_path, entries, store, [_usage("glm", 60.0), _usage("zai", 12.0)])

    assert snapshot["workspaces"]["/tmp/ws-dirty"]["state"] == "dirty"

    plan = _suggest(snapshot)
    actions = plan["workspace_actions"]
    targets = [action for action in actions if action["workspace"] == "/tmp/ws-dirty"]
    assert targets, f"expected a workspace action for the dirty tree, got {plan}"
    assert all(action["action"] == "add_clone" for action in actions)

    text = json.dumps(plan, ensure_ascii=False)
    # The launcher reason string says "dirty tree" — allowed; the FORBIDDEN
    # verbs (stash / reset / discard / clean) may only appear as negations in
    # our own safety text, never as the suggested action.
    assert '"action": "stash"' not in text
    assert '"action": "reset"' not in text
    assert '"action": "discard"' not in text


def test_hot_provider_above_85_percent_is_never_a_move_target(tmp_path: Path) -> None:
    """synthetic has room but sits at 92% used -> no move onto it; alert instead."""

    entries = [
        _queue_entry("run-0", trio="glm", status="launched"),
        _queue_entry("q-blocked", trio="glm", skip_reasons=["glm at capacity"]),
    ]
    store = _store(trios=["glm", "synthetic"], caps={"glm": 1, "synthetic": 3})
    snapshot = _snapshot(
        tmp_path,
        entries,
        store,
        [_usage("glm", 55.0), _usage("synthetic", 92.0, plan="foundry")],
    )
    plan = _suggest(snapshot)

    assert all(move["to_trio"] != "synthetic" for move in plan["moves"])
    assert any("92.0%" in alert or "92%" in alert for alert in plan["alerts"])
    assert all(change["trio"] != "synthetic" for change in plan["cap_changes"])


def test_tev1_down_falls_back_to_rules(tmp_path: Path) -> None:
    """Router enabled but the HTTP call raises -> source 'rules', alert names the fallback."""

    entries = [
        _queue_entry("run-0", trio="glm", status="launched"),
        _queue_entry("q-blocked", trio="glm", skip_reasons=["glm at capacity"]),
    ]
    store = _store(caps={"glm": 1, "zai": 1}, metadata={"TRIO_ROUTER_ENABLED": "true"})
    snapshot = _snapshot(tmp_path, entries, store, [_usage("glm", 60.0), _usage("zai", 7.0)])

    def _down(url: str, model: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        raise ConnectionError("model lane down")

    plan = lane_optimizer.suggest(snapshot, store=store, http_call=_down, now=1761997500.0)
    assert plan["source"] == "rules"
    assert any("fallback" in alert for alert in plan["alerts"])
    # The rules still produce the move the model would have confirmed.
    assert [move for move in plan["moves"] if move["to_trio"] == "zai"]


def test_output_never_contains_env_or_key_values(tmp_path: Path, monkeypatch: Any) -> None:
    """A secret-shaped env value woven into skip text is redacted from serialized output."""

    sentinel = "ze5ba7cf-fake-sentinel-token-9d2e"
    monkeypatch.setenv("LH_HARNESS_TEST_SENTINEL_TOKEN", sentinel)

    entries = [
        _queue_entry("run-0", trio="glm", status="launched"),
        _queue_entry(
            "q-leak",
            trio="glm",
            skip_reasons=[f"glm at capacity; cached header {sentinel} seen in provider reply"],
        ),
    ]
    store = _store(caps={"glm": 1, "zai": 1})
    snapshot = _snapshot(tmp_path, entries, store, [_usage("glm", 60.0), _usage("zai", 3.0)])
    plan = _suggest(snapshot)

    blob = json.dumps({"snapshot": snapshot, "suggestion": plan}, ensure_ascii=False)
    assert sentinel not in blob
    assert "[redacted]" in blob


# ---------------------------------------------------------- snapshot shape --
def test_build_snapshot_reports_caps_pending_and_usage(tmp_path: Path) -> None:
    """The snapshot carries the per-trio cap/active/pending rows and the meter rows."""

    entries = [
        _queue_entry("run-0", trio="glm", status="launched"),
        _queue_entry("q-a", trio="glm", priority=5, skip_reasons=["glm at capacity"]),
        _queue_entry("q-b", trio="zai", workspace="/tmp/ws-free"),
    ]
    store = _store(caps={"glm": 2, "zai": 0})
    snapshot = _snapshot(
        tmp_path, entries, store, [_usage("glm", 61.5), _usage("zai", 8.0)]
    )

    assert snapshot["trios"]["glm"]["cap"] == 2
    assert snapshot["trios"]["glm"]["active"] == 1
    assert snapshot["trios"]["glm"]["pending"] == 1
    assert snapshot["trios"]["zai"]["cap"] == 0
    assert snapshot["usage_available"] is True
    assert snapshot["usage"] and snapshot["usage"][0]["used_pct"] == 61.5
    pending = {item["queue_id"]: item for item in snapshot["pending"]}
    assert pending["q-a"]["last_skip_reason"] == "glm at capacity"
    assert snapshot["workspaces"]["/tmp/ws-free"]["state"] == "free"

    # An idle paid lane pinned at cap 0 while backlog stalls gets the raise.
    plan = _suggest(snapshot)
    assert [
        change
        for change in plan["cap_changes"]
        if change["trio"] == "zai" and change["from"] == 0 and change["to"] == 1
    ]


def test_no_usage_rows_means_honest_degradation(tmp_path: Path) -> None:
    """Without meter rows nothing is a move target and the output says why."""

    entries = [
        _queue_entry("run-0", trio="glm", status="launched"),
        _queue_entry("q-blocked", trio="glm", skip_reasons=["glm at capacity"]),
    ]
    store = _store(caps={"glm": 1, "zai": 1})
    snapshot = _snapshot(tmp_path, entries, store, None)
    plan = _suggest(snapshot)

    assert snapshot["usage_available"] is False
    assert plan["usage_available"] is False
    assert plan["moves"] == []
    assert any("usage" in alert for alert in plan["alerts"])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
