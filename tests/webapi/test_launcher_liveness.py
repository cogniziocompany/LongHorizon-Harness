"""Launcher-liveness heartbeat fields — task 173, scope 6 / migration doc §4.3.

The fleet heartbeat payload gains ``launcher_tick_at`` (the launcher lease's
last refresh time — the launcher's last pass) and ``lease_holder`` (the
pid/host currently holding the lease), read from ``runs_root/queue/.lease``.
An absent lease reports ``None`` for both, the fleet window's "no launcher"
signal.  Same hermetic style as the queue-depth tests above: no request ever
leaves the process.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from lh_harness.fleet.reporter import get_reporter  # noqa: E402
from lh_harness.webapi.server import _maybe_start_fleet_reporter, create_app  # noqa: E402

from .test_fleet_liveness import (  # noqa: E402
    _enable_reporter,
    _isolate_reporter,  # noqa: F401  (fixture, applied per test)
    _payload,
    _registered_heartbeat,
    _runs_root,
)


def _write_lease(root: Path, *, ts: float | None = None, pid: int = 7, host: str = "ct110") -> None:
    path = root / "queue" / ".lease"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": pid, "host": host, "ts": time.time() if ts is None else ts}),
        encoding="utf-8",
    )


def test_heartbeat_reports_lease_tick_and_holder(tmp_path: Path) -> None:
    _enable_reporter()
    root = _runs_root(tmp_path)
    app = create_app(runs_root=root)
    heartbeat = _registered_heartbeat()

    _write_lease(root, ts=1700000000.0, pid=1234, host="ct110")
    result = heartbeat()

    assert len(result) == 7
    runs, active, cap, queue_len, _review_verdicts, launcher_tick_at, lease_holder = result
    assert launcher_tick_at == 1700000000.0
    assert lease_holder == {"pid": 1234, "host": "ct110"}


def test_heartbeat_without_lease_reports_none(tmp_path: Path) -> None:
    _enable_reporter()
    app = create_app(runs_root=_runs_root(tmp_path))
    heartbeat = _registered_heartbeat()

    result = heartbeat()
    assert len(result) == 7
    _, _, _, queue_len, _review_verdicts, launcher_tick_at, lease_holder = result
    assert launcher_tick_at is None
    assert lease_holder is None


def test_heartbeat_liveness_fields_reach_fleet_payload(tmp_path: Path) -> None:
    """The values the fleet window receives are the lease's, end to end."""

    _enable_reporter()
    root = _runs_root(tmp_path)
    app = create_app(runs_root=root)
    _write_lease(root, ts=1700000000.0, pid=99, host="ct110")

    reporter = get_reporter()
    posted: list[tuple[str, dict[str, Any]]] = []
    reporter._post = (  # type: ignore[method-assign]
        lambda endpoint, payload, gzip_body=True: posted.append((endpoint, payload))
    )

    runs, active, cap, queue_len, _review_verdicts, launcher_tick_at, lease_holder = (
        _registered_heartbeat()()
    )
    reporter.queue_heartbeat(
        runs, active, cap, queue_len, _review_verdicts, launcher_tick_at, lease_holder
    )

    endpoint, body = posted[-1]
    assert endpoint == "/harness/heartbeat"
    assert body["liveness"]["launcher_tick_at"] == 1700000000.0
    assert body["liveness"]["lease_holder"] == {"pid": 99, "host": "ct110"}


def test_heartbeat_liveness_fields_absent_when_no_lease(tmp_path: Path) -> None:
    _enable_reporter()
    root = _runs_root(tmp_path)
    app = create_app(runs_root=root)

    reporter = get_reporter()
    posted: list[tuple[str, dict[str, Any]]] = []
    reporter._post = (  # type: ignore[method-assign]
        lambda endpoint, payload, gzip_body=True: posted.append((endpoint, payload))
    )

    runs, active, cap, queue_len, _review_verdicts, launcher_tick_at, lease_holder = (
        _registered_heartbeat()()
    )
    reporter.queue_heartbeat(
        runs, active, cap, queue_len, _review_verdicts, launcher_tick_at, lease_holder
    )

    _, body = posted[-1]
    assert body["liveness"] == {"launcher_tick_at": None, "lease_holder": None}


def test_heartbeat_with_legacy_callback_still_posts(tmp_path: Path) -> None:
    """A 4-tuple callback (the old contract) still reaches the fleet surface."""

    _enable_reporter()
    app = create_app(runs_root=_runs_root(tmp_path))
    store = app.state.queue_store
    store.create(_payload())

    reporter = get_reporter()
    posted: list[tuple[str, dict[str, Any]]] = []
    reporter._post = (  # type: ignore[method-assign]
        lambda endpoint, payload, gzip_body=True: posted.append((endpoint, payload))
    )

    reporter.queue_heartbeat([], 0, 0, 1)
    endpoint, body = posted[-1]
    assert endpoint == "/harness/heartbeat"
    assert body["queueLen"] == 1
    assert body["liveness"] == {"launcher_tick_at": None, "lease_holder": None}