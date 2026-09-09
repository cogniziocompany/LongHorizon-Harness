"""Hermetic tests for the orchestrator liveness signal (secondary commit A.2).

The fleet reporter registers a ``_heartbeat`` callback in ``webapi/server.py``
whose fourth return value the fleet window consumes as ``queueLen``.  These
tests prove the heartbeat reports the *real* queue depth read from the durable
``QueueStore`` instead of the former hardcoded ``0`` placeholder, so a fleet
surface can actually tell whether the orchestrator's backlog is draining.

No live gateway or harness API calls are made.  The reporter is pointed at a
discard loopback URL and, where a post is exercised, ``_post`` is intercepted,
so nothing leaves the process.  The reporter singleton and the fleet env vars
are reset around every test.  The worker thread's periodic invocation of the
callback is already covered by ``tests/fleet/test_reporter.py``; here we drive
the registered callback directly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from lh_harness.fleet.reporter import get_reporter  # noqa: E402
from lh_harness.webapi.server import (  # noqa: E402
    _maybe_start_fleet_reporter,
    create_app,
)

_FLEET_ENV = (
    "LH_HARNESS_FLEET_URL",
    "LH_HARNESS_FLEET_NODE",
    "LH_HARNESS_FLEET_KEY",
    "LH_HARNESS_FLEET_LABELS",
)


def _getenv() -> dict[str, str | None]:
    return {name: os.environ.get(name) for name in _FLEET_ENV}


def _setenv(
    url: str | None,
    node: str | None,
    key: str | None,
    labels: str | None,
) -> None:
    values = {
        "LH_HARNESS_FLEET_URL": url,
        "LH_HARNESS_FLEET_NODE": node,
        "LH_HARNESS_FLEET_KEY": key,
        "LH_HARNESS_FLEET_LABELS": labels,
    }
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(autouse=True)
def _isolate_reporter() -> None:
    """Reset the reporter singleton and the fleet env around every test."""
    original = _getenv()
    get_reporter(reset=True)
    yield
    for name, value in original.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    get_reporter(reset=True)


def _runs_root(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    return root


def _payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "liveness",
        "task": "do the thing",
        "workspace": "./workspace",
        "trio": "kimi",
        "priority": 0,
        "requested_by": "orchestrator",
    }
    body.update(overrides)
    return body


def _enable_reporter() -> None:
    """Configure fleet env and (re)create an enabled reporter singleton.

    A discard loopback URL is enough to enable the reporter.  No request ever
    leaves the process: the 30 s heartbeat interval never elapses inside a test,
    and ``_post`` is intercepted wherever a post is actually exercised.
    """
    _setenv("http://127.0.0.1:9", "liveness-node", "liveness-key", None)
    get_reporter(reset=True)


def _registered_heartbeat():
    """The heartbeat closure ``create_app`` registered on the singleton."""
    reporter = get_reporter()
    assert reporter is not None and reporter.enabled, "fleet reporter not enabled"
    assert reporter._heartbeat_callback is not None, "no heartbeat callback registered"
    return reporter._heartbeat_callback


def test_heartbeat_reports_real_queue_depth(tmp_path: Path) -> None:
    """The heartbeat's queue_len tracks live queue state, not a constant.

    On the former hardcoded implementation the callback always returned 0, so
    the ``== 2`` assertion after two enqueues would fail -- this test therefore
    guards the regression directly.
    """
    _enable_reporter()
    app = create_app(runs_root=_runs_root(tmp_path))
    store = app.state.queue_store
    heartbeat = _registered_heartbeat()

    _, _, _, queue_len = heartbeat()
    assert queue_len == 0  # empty queue

    first = store.create(_payload())
    _, _, _, queue_len = heartbeat()
    assert queue_len == 1  # one pending entry

    second = store.create(_payload())
    _, _, _, queue_len = heartbeat()
    assert queue_len == 2  # two non-terminal entries

    # Launching one moves it pending -> launched; it is still non-terminal, so
    # the backlog the orchestrator owns is unchanged.
    store.mark_launched(second.queue_id, "run-1")
    _, _, _, queue_len = heartbeat()
    assert queue_len == 2

    # Reaching a terminal state removes the entry from the live backlog.
    store.mark_done(second.queue_id)
    _, _, _, queue_len = heartbeat()
    assert queue_len == 1

    store.mark_failed(first.queue_id, "boom")
    _, _, _, queue_len = heartbeat()
    assert queue_len == 0


def test_heartbeat_queue_len_matches_counts(tmp_path: Path) -> None:
    """queue_len equals the non-terminal count derived from counts()."""
    _enable_reporter()
    app = create_app(runs_root=_runs_root(tmp_path))
    store = app.state.queue_store
    heartbeat = _registered_heartbeat()

    for _ in range(3):
        store.create(_payload())

    counts = store.counts()
    expected = counts["pending"] + counts["launched"]
    _, _, _, queue_len = heartbeat()
    assert queue_len == expected == 3


def test_heartbeat_queue_len_reaches_fleet_payload(tmp_path: Path) -> None:
    """The real queue_len is what the heartbeat posts to the fleet surface.

    Drives the registered callback through the real ``queue_heartbeat`` ->
    ``_post`` path (with ``_post`` intercepted) so the value the fleet window
    would receive is verified, not just the in-process return value.
    """
    _enable_reporter()
    app = create_app(runs_root=_runs_root(tmp_path))
    store = app.state.queue_store
    for _ in range(3):
        store.create(_payload())

    reporter = get_reporter()
    posted: list[tuple[str, dict[str, Any]]] = []
    reporter._post = (  # type: ignore[method-assign]
        lambda endpoint, payload, gzip_body=True: posted.append((endpoint, payload))
    )

    runs, active, cap, queue_len = _registered_heartbeat()()
    reporter.queue_heartbeat(runs, active, cap, queue_len)

    assert posted, "a heartbeat should have been posted"
    endpoint, body = posted[-1]
    assert endpoint == "/harness/heartbeat"
    assert body["queueLen"] == 3  # the real depth, not the former hardcoded 0


def test_heartbeat_ignores_queue_when_store_absent(tmp_path: Path) -> None:
    """A node with no queue_store (runs_root unset) reports queue_len 0.

    Guards the None branch: even with a pending entry on the app's own store,
    a heartbeat registered without a queue_store must stay 0 rather than read a
    queue it does not own (preserves the prior no-queue behaviour).
    """
    _enable_reporter()
    app = create_app(runs_root=_runs_root(tmp_path))
    store = app.state.queue_store
    store.create(_payload())
    assert store.counts()["pending"] == 1

    # Re-register the heartbeat with no queue_store, as a node not running a
    # queue would.  create_app already wired the reporter; this replaces the
    # callback with the None-store variant on the same singleton.
    _maybe_start_fleet_reporter(app.state.registry, None, queue_store=None)
    reporter = get_reporter()
    assert reporter._heartbeat_callback is not None
    _, _, _, queue_len = reporter._heartbeat_callback()
    assert queue_len == 0
