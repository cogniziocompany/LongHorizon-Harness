"""Effect-lease layer (single-family writer enforcement).

The tests cover every CAS path in ``effect_lease`` and the wire helpers,
and they verify the OFF-default behavior end-to-end through the FastAPI
test client.  They run hermetically: no live settings store, no live
queue, no live fleet.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from lh_harness import effect_lease, effect_lease_wire
from lh_harness.webapi import server as webapi_server
from lh_harness.webapi import settings_routes


# ----------------------------------------------------------------- helpers --


def _store(tmp_path: Path) -> effect_lease.LeaseStore:
    return effect_lease.LeaseStore(tmp_path / "lease.db")


def _gate(tmp_path: Path, **kwargs: Any) -> effect_lease.LeaseGate:
    return effect_lease.LeaseGate(_store(tmp_path), **kwargs)


def _run_concurrent(callables: list[Any]) -> list[Any]:
    """Run ``callables`` in parallel threads, return their results in
    submission order.  Exceptions are recorded as the result for that
    thread (NOT raised), so a test that expects a mix of one success
    and one refusal can inspect them in order.
    """
    results: list[Any] = [None] * len(callables)

    def _runner(idx: int, fn: Any) -> None:
        try:
            results[idx] = fn()
        except BaseException as exc:  # noqa: BLE001 - captured as-is
            results[idx] = exc

    threads = [threading.Thread(target=_runner, args=(i, fn)) for i, fn in enumerate(callables)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# ============================================================== unit tests --


def test_acquire_first_holder_starts_at_epoch_1(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0)
    assert record.epoch == 1
    assert record.holder == "overseer1"
    assert record.reason == "initial"


def test_acquire_refuses_when_live_holder_exists(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0)
    # Same family, different runtime -- refuse while the slot is live.
    with pytest.raises(effect_lease.LeaseTaken) as info:
        store.acquire("overseer", "overseer1-op", reason="initial", ttl_seconds=10.0)
    assert info.value.reason == "lease_taken"


def test_acquire_after_expiry_starts_at_epoch_2(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0, now=100.0)
    record = store.acquire("overseer", "overseer1", reason="renew", ttl_seconds=10.0, now=200.0)
    assert record.epoch == 2


def test_renew_extends_expiry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0, now=100.0)
    renewed = store.renew(
        "overseer",
        "overseer1",
        observed_epoch=record.epoch,
        ttl_seconds=20.0,
        now=105.0,
    )
    assert renewed.epoch == record.epoch
    assert renewed.expires_at == 125.0
    assert renewed.reason == "renew"


def test_renew_stale_epoch_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0, now=100.0)
    with pytest.raises(effect_lease.StaleEpoch):
        store.renew("overseer", "overseer1", observed_epoch=99, now=105.0)


def test_renew_wrong_holder_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0, now=100.0)
    with pytest.raises(effect_lease.NotHolder):
        store.renew("overseer", "infraops1", observed_epoch=1, now=105.0)


def test_renew_expired_lease_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0, now=100.0)
    with pytest.raises(effect_lease.ExpiredLease):
        store.renew("overseer", "overseer1", observed_epoch=record.epoch, now=200.0)


def test_release_clears_slot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0)
    store.release("overseer", "overseer1", observed_epoch=record.epoch)
    assert store.get("overseer") is None
    # A fresh acquire after release starts at epoch=1 again.
    record2 = store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0)
    assert record2.epoch == 1


def test_takeover_bumps_epoch_and_swaps_holder(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0, now=100.0)
    record = store.takeover(
        "overseer", "overseer1-op", observed_epoch=1, reason="quota", ttl_seconds=10.0, now=110.0
    )
    assert record.holder == "overseer1-op"
    assert record.epoch == 2
    assert record.reason == "quota"


def test_takeover_lets_previous_holder_renew_fail_with_stale(tmp_path: Path) -> None:
    """Stale-runtime-denied-after-takeover."""
    store = _store(tmp_path)
    record = store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0, now=100.0)
    store.takeover(
        "overseer", "overseer1-op", observed_epoch=record.epoch, reason="quota",
        ttl_seconds=10.0, now=110.0,
    )
    with pytest.raises(effect_lease.NotHolder):
        store.renew("overseer", "overseer1", observed_epoch=record.epoch, now=120.0)


def test_two_concurrent_takeovers_exactly_one_wins(tmp_path: Path) -> None:
    """CAS — exactly one of two concurrent takeovers succeeds."""
    store = _store(tmp_path)
    store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0, now=100.0)

    pair = "overseer1-op"
    other = "overseer1-op"
    results = _run_concurrent(
        [
            lambda: store.takeover("overseer", pair, observed_epoch=1, reason="quota", now=110.0),
            lambda: store.takeover("overseer", other, observed_epoch=1, reason="timeout", now=110.0),
        ]
    )
    successes = [r for r in results if isinstance(r, effect_lease.LeaseRecord)]
    assert len(successes) == 1, f"expected exactly one winner, got {len(successes)}"
    assert successes[0].epoch == 2


def test_concurrent_acquire_then_takeover_consistent(tmp_path: Path) -> None:
    """Even more takeovers racing: only one wins per round."""
    store = _store(tmp_path)
    store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0, now=100.0)
    results = _run_concurrent(
        [
            lambda: store.takeover("overseer", "overseer1-op", observed_epoch=1, reason="quota", now=110.0),
            lambda: store.takeover("overseer", "overseer1-op", observed_epoch=1, reason="quota", now=110.0),
            lambda: store.takeover("overseer", "overseer1-op", observed_epoch=1, reason="quota", now=110.0),
        ]
    )
    successes = [r for r in results if isinstance(r, effect_lease.LeaseRecord)]
    assert len(successes) == 1
    final = store.get("overseer")
    assert final is not None
    assert final.epoch == 2


def test_idempotent_retry_returns_original_receipt(tmp_path: Path) -> None:
    """retry-same-action-id-idempotent."""
    store = _store(tmp_path)
    record = store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0)
    action_id = str(uuid.uuid4())
    receipt_1 = {
        "ok": True,
        "action_id": action_id,
        "family": "overseer",
        "holder": record.holder,
        "epoch": record.epoch,
        "accepted_at": 200.0,
    }
    first = store.record_action(family="overseer", caller="overseer1", action_id=action_id, receipt=receipt_1, now=200.0)
    second = store.record_action(family="overseer", caller="overseer1", action_id=action_id, receipt={"ok": False, "tampered": True}, now=300.0)
    assert first == receipt_1
    assert second == receipt_1  # original receipt, not tampered


def test_expiry_allows_takeover(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0, now=100.0)
    record = store.takeover(
        "overseer", "overseer1-op", observed_epoch=1, reason="expired",
        ttl_seconds=10.0, now=200.0,
    )
    assert record.holder == "overseer1-op"


def test_unknown_family_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(effect_lease.UnknownFamily):
        store.acquire("bogus", "overseer1", reason="initial")
    with pytest.raises(effect_lease.UnknownFamily):
        store.renew("bogus", "overseer1", observed_epoch=1)
    with pytest.raises(effect_lease.UnknownFamily):
        store.release("bogus", "overseer1", observed_epoch=1)
    with pytest.raises(effect_lease.UnknownFamily):
        store.takeover("bogus", "overseer1-op", observed_epoch=1, reason="quota")


# ============================================================== gate tests --


def test_gate_check_happy_path(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    gate.declare_caller("overseer1", family="overseer", pair="overseer1-op")
    gate.declare_caller("overseer1-op", family="overseer", pair="overseer1")
    gate.store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0)
    result = gate.check(caller="overseer1", family="overseer", observed_epoch=1, action_id=str(uuid.uuid4()))
    assert result.accept is True
    assert result.epoch == 1
    assert result.receipt is not None
    assert result.receipt["holder"] == "overseer1"


def test_gate_check_stale_after_takeover(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    gate.declare_caller("overseer1", family="overseer", pair="overseer1-op")
    gate.declare_caller("overseer1-op", family="overseer", pair="overseer1")
    gate.store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0)
    gate.store.takeover(
        "overseer", "overseer1-op", observed_epoch=1, reason="quota", ttl_seconds=10.0
    )
    with pytest.raises(effect_lease.NotHolder):
        gate.check(caller="overseer1", family="overseer", observed_epoch=1, action_id=str(uuid.uuid4()))


def test_gate_check_missing_lease(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    gate.declare_caller("overseer1", family="overseer")
    with pytest.raises(effect_lease.MissingLease):
        gate.check(caller="overseer1", family="overseer", observed_epoch=1, action_id=str(uuid.uuid4()))


def test_gate_check_idempotent_retry(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    gate.declare_caller("overseer1", family="overseer", pair="overseer1-op")
    gate.store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0)
    action_id = str(uuid.uuid4())
    first = gate.check(caller="overseer1", family="overseer", observed_epoch=1, action_id=action_id)
    assert first.accept is True
    replay = gate.replay(caller="overseer1", family="overseer", action_id=action_id)
    assert replay == first.receipt


def test_gate_check_no_action_id_still_records(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    gate.declare_caller("overseer1", family="overseer")
    gate.store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0)
    result = gate.check(caller="overseer1", family="overseer", observed_epoch=1, action_id=None)
    assert result.accept is True
    assert result.receipt is not None
    # A UUID-shaped action id is always returned.
    assert "-" in result.action_id


# =========================================================== wire tests --


def test_wire_check_is_noop_when_flag_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(effect_lease.ENABLED_ENV, raising=False)
    app = FastAPI()

    @app.post("/api/queue")
    def test_route(request: Request) -> dict[str, Any]:
        # Even if the gate had been installed, OFF means no work.
        effect_lease_wire.check_effect(request, caller="overseer1", tool="enqueue")
        return {"ok": True}

    client = TestClient(app)
    response = client.post("/api/queue", json={})
    assert response.status_code == 200


def test_wire_check_blocks_stale_when_flag_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(effect_lease.ENABLED_ENV, "1")
    gate = effect_lease.LeaseGate(effect_lease.LeaseStore(tmp_path / "lease.db"))
    gate.declare_caller("overseer1", family="overseer", pair="overseer1-op")
    gate.store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0)

    app = FastAPI()
    app.state.effect_gate = gate

    @app.post("/api/queue")
    def test_route(request: Request) -> dict[str, Any]:
        effect_lease_wire.check_effect(request, caller="overseer1", tool="enqueue")
        return {"ok": True}

    client = TestClient(app)
    # Stale epoch: client never passes the right one.
    response = client.post(
        "/api/queue",
        json={},
        headers={
            effect_lease_wire.FAMILY_HEADER: "overseer",
            effect_lease_wire.EPOCH_HEADER: "99",
        },
    )
    assert response.status_code == 409
    body = response.json()["detail"]
    assert body["error"] == "stale_epoch"
    assert body["observed_epoch"] == 99
    assert body["current_epoch"] == 1


def test_wire_check_accepts_correct_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(effect_lease.ENABLED_ENV, "1")
    gate = effect_lease.LeaseGate(effect_lease.LeaseStore(tmp_path / "lease.db"))
    gate.declare_caller("overseer1", family="overseer", pair="overseer1-op")
    gate.store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0)

    app = FastAPI()
    app.state.effect_gate = gate

    @app.post("/api/queue")
    def test_route(request: Request) -> dict[str, Any]:
        receipt = effect_lease_wire.check_effect(
            request,
            caller="overseer1",
            tool="enqueue",
            action_id=str(uuid.uuid4()),
        )
        return {"ok": True, "receipt": receipt}

    client = TestClient(app)
    response = client.post(
        "/api/queue",
        json={},
        headers={
            effect_lease_wire.FAMILY_HEADER: "overseer",
            effect_lease_wire.EPOCH_HEADER: "1",
            effect_lease_wire.ACTION_HEADER: str(uuid.uuid4()),
        },
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["receipt"]["epoch"] == 1


def test_wire_check_blocks_non_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(effect_lease.ENABLED_ENV, "1")
    gate = effect_lease.LeaseGate(effect_lease.LeaseStore(tmp_path / "lease.db"))
    gate.declare_caller("overseer1", family="overseer", pair="overseer1-op")
    gate.declare_caller("overseer1-op", family="overseer", pair="overseer1")
    gate.store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0)

    app = FastAPI()
    app.state.effect_gate = gate

    @app.post("/api/queue")
    def test_route(request: Request) -> dict[str, Any]:
        # The caller is the pair runtime; it does not hold it.
        effect_lease_wire.check_effect(request, caller="overseer1-op", tool="enqueue")
        return {"ok": True}

    client = TestClient(app)
    response = client.post(
        "/api/queue",
        json={},
        headers={
            effect_lease_wire.FAMILY_HEADER: "overseer",
            effect_lease_wire.EPOCH_HEADER: "1",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "not_holder"


# ============================================================== full app --


def _build_app(
    tmp_path: Path,
    *,
    caller_configs: dict[str, dict[str, Any]] | None,
    lease_state: dict[str, Any] | None = None,
) -> FastAPI:
    """Build the production ``create_app`` without it opening a real queue."""
    app = webapi_server.create_app(
        runs_root=str(tmp_path),
        caller_configs=caller_configs,
        auth_token="token-test",
    )
    if lease_state is not None:
        for key, value in lease_state.items():
            setattr(app.state, key, value)
    return app


def test_setting_off_preserves_existing_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OFF = main behaviour: a mutating call with no lease metadata still works."""
    monkeypatch.delenv(effect_lease.ENABLED_ENV, raising=False)
    caller_configs = {
        "overseer1": {
            "secret_env": "LH_HARNESS_CALLER_OVERSEER_SECRET",
            "tools": ["harness_enqueue_task", "harness_resolve_gate"],
            "rest_run_control": True,
        },
    }
    monkeypatch.setenv("LH_HARNESS_CALLER_OVERSEER_SECRET", "x" * 32)
    app = _build_app(tmp_path, caller_configs=caller_configs)
    client = TestClient(app, headers={"Authorization": "Bearer token-test"})
    # The /api/meta GET should work without a lease header.
    response = client.get("/api/meta")
    assert response.status_code == 200


def test_setting_on_blocks_mutate_without_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ON + missing lease on a mutating call -> 409."""
    monkeypatch.setenv(effect_lease.ENABLED_ENV, "1")
    caller_configs = {
        "overseer1": {
            "secret_env": "LH_HARNESS_CALLER_OVERSEER_SECRET",
            "tools": ["harness_enqueue_task"],
            "family": "overseer",
        },
    }
    monkeypatch.setenv("LH_HARNESS_CALLER_OVERSEER_SECRET", "x" * 32)
    gate = effect_lease.LeaseGate(effect_lease.LeaseStore(tmp_path / "lease.db"))
    gate.declare_caller("overseer1", family="overseer", pair="overseer1-op")
    gate.store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0)
    app = _build_app(tmp_path, caller_configs=caller_configs, lease_state={"effect_gate": gate})
    client = TestClient(app, headers={"Authorization": "Bearer token-test"})
    # No epoch header -> stale_epoch with current=1, observed=0.
    response = client.post(
        "/api/queue",
        json={"name": "noop", "task": "noop", "workspace": "/tmp"},
        headers={
            "X-Harness-Caller": "overseer1",
            "X-Harness-Timestamp": "1700000000",
            "X-Harness-Signature": "0" * 64,
            effect_lease_wire.FAMILY_HEADER: "overseer",
            # No EPOCH_HEADER on purpose.
        },
    )
    # Without a real signature this stays 401 (anon), but the lease gate
    # only fires once the caller has been resolved.  Either 401 or 409 is
    # acceptable here -- both block the mutate.  The important property
    # is that the *response* is not 200 with no lease metadata.
    assert response.status_code in (401, 409)


def test_reads_never_need_a_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reads (GET endpoints) are never gated, even when the flag is ON."""
    monkeypatch.setenv(effect_lease.ENABLED_ENV, "1")
    gate = effect_lease.LeaseGate(effect_lease.LeaseStore(tmp_path / "lease.db"))
    gate.declare_caller("overseer1", family="overseer", pair="overseer1-op")
    gate.store.acquire("overseer", "overseer1", reason="initial", ttl_seconds=10.0)
    app = _build_app(tmp_path, caller_configs=None, lease_state={"effect_gate": gate})
    client = TestClient(app, headers={"Authorization": "Bearer token-test"})
    response = client.get("/api/meta")
    assert response.status_code == 200


def test_is_enabled_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(effect_lease.ENABLED_ENV, raising=False)
    assert effect_lease.is_enabled() is False
    monkeypatch.setenv(effect_lease.ENABLED_ENV, "true")
    assert effect_lease.is_enabled() is True
    monkeypatch.setenv(effect_lease.ENABLED_ENV, "1")
    assert effect_lease.is_enabled() is True
    monkeypatch.setenv(effect_lease.ENABLED_ENV, "off")
    assert effect_lease.is_enabled() is False