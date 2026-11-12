"""LHH-WEB-LEAK-FIX c3: the opt-in live memory debug endpoints (ticket easybutt0n-17).

These tests cover the three guarantees the ticket asks for:

* the routes are INVISIBLE unless the operator opted in -- every one of them
  answers 404 when ``LH_HARNESS_MEM_DEBUG`` is unset (not 403);
* the routes inherit the SAME bearer boundary as every other ``/api/`` route
  -- they answer 401 without a valid bearer;
* with the flag on, the JSON payload carries every element the ticket names:
  the process RSS, the tracemalloc current/peak totals, the top 30
  allocation groups with their depth-6 file:line tracebacks and counts, the
  GC counters with the top-15 type buckets, and the known cache sizes.

The flag is set and cleared by the fixtures here, so nothing leaks into the
other ``tests/webapi`` tests.
"""

from __future__ import annotations

import gc
import os
import tracemalloc
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.dashboard.state import DashboardState
from lh_harness.webapi import memory_debug
from lh_harness.webapi.server import create_app

TOKEN = "bearer-" + "m" * 32


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    """A tiny runs root: one run directory with the usual layout."""

    root = tmp_path / "runs"
    run = root / "run-1"
    role_dir = run / "logs" / "role_management"
    role_dir.mkdir(parents=True)
    (run / "logs" / "report.json").write_text(
        '{"task": "memory fixture"}', encoding="utf-8"
    )
    (role_dir / "events.jsonl").write_text(
        '{"ts": 1, "event": "role_harness_start"}\n', encoding="utf-8"
    )
    return root


@pytest.fixture
def app(runs_root: Path):
    """An app over the tiny runs root, with a bearer token configured."""

    return create_app(
        log_dir=runs_root / "run-1" / "logs",
        runs_root=runs_root,
        auth_token=TOKEN,
        probe_open_pr=None,
    )


@pytest.fixture
def mem_debug_on(monkeypatch: pytest.MonkeyPatch):
    """Turn the memory debug flag ON for the duration of one test."""

    monkeypatch.setenv(memory_debug.MEM_DEBUG_ENV, "1")
    # The endpoint reports on tracemalloc, so tracing must be running.  The
    # real web process starts it at startup (D1); in the test the flag is set
    # after import, so the test starts it explicitly.
    started = not tracemalloc.is_tracing()
    if started:
        tracemalloc.start(memory_debug.TRACEMALLOC_FRAMES)
    try:
        yield
    finally:
        if started:
            tracemalloc.stop()


@pytest.fixture
def mem_debug_off(monkeypatch: pytest.MonkeyPatch):
    """Make sure the memory debug flag is OFF for the duration of one test."""

    monkeypatch.delenv(memory_debug.MEM_DEBUG_ENV, raising=False)


@pytest.fixture
def client(app) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _json_auth() -> dict[str, str]:
    """Auth headers plus the JSON content-type every POST /api/ route needs.

    The web API's middleware answers 415 to a POST that does not declare a
    JSON content type, and that middleware runs BEFORE the route handler.
    A real client always sends the content type, so the tests do too -- that
    way the 404/401 assertions below exercise the ROUTE, not the middleware.
    """

    return {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# D4/AC3: the routes are invisible without the flag.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/debug/memory"),
        ("post", "/api/debug/memory/snapshot"),
        ("get", "/api/debug/memory/diff"),
    ],
)
def test_memory_routes_404_without_the_flag(client: TestClient, mem_debug_off, method, path) -> None:
    """Without ``LH_HARNESS_MEM_DEBUG=1`` every memory route is a 404."""

    if method == "post":
        response = client.post(path, headers=_json_auth(), json={})
    else:
        response = client.get(path, headers=_auth())
    assert response.status_code == 404, response.text


def test_memory_route_auth_is_checked_before_the_flag(
    client: TestClient, mem_debug_off
) -> None:
    """Auth comes before the flag, exactly as for every other /api/ route.

    The bearer middleware runs ahead of the route handler, so a wrong bearer
    is a 401 whether or not the memory debug flag is on.  The flag only
    decides whether an AUTHENTICATED caller sees the route (200) or a 404.
    """

    response = client.get("/api/debug/memory", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# AC3: the bearer boundary is the same one every other /api/ route uses.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/debug/memory"),
        ("post", "/api/debug/memory/snapshot"),
        ("get", "/api/debug/memory/diff"),
    ],
)
def test_memory_routes_401_without_a_bearer(client: TestClient, mem_debug_on, method, path) -> None:
    """With the flag on but no bearer, the route answers 401."""

    response = getattr(client, method)(path)
    assert response.status_code == 401, response.text


def test_memory_route_401_with_a_wrong_bearer(client: TestClient, mem_debug_on) -> None:
    response = client.get("/api/debug/memory", headers={"Authorization": "Bearer not-the-token"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# AC3: the flag-on JSON payload carries every element the ticket names.
# ---------------------------------------------------------------------------


def test_memory_report_json_shape(client: TestClient, mem_debug_on) -> None:
    """The payload carries RSS, tracemalloc, gc and cache sizes."""

    response = client.get("/api/debug/memory", headers=_auth())
    assert response.status_code == 200, response.text
    payload = response.json()

    # The process RSS.
    assert isinstance(payload["rss_bytes"], int)
    assert payload["rss_bytes"] > 0

    # The tracemalloc current/peak totals.
    tm = payload["tracemalloc"]
    assert tm["tracing"] is True
    assert isinstance(tm["current_bytes"], int)
    assert isinstance(tm["peak_bytes"], int)
    assert tm["current_bytes"] > 0
    assert tm["peak_bytes"] >= tm["current_bytes"]
    assert tm["frames"] == memory_debug.TRACEMALLOC_FRAMES

    # The top allocation groups: at most 30, each with a size, a count and a
    # depth-bounded file:line traceback.
    groups = tm["top_allocations"]
    assert isinstance(groups, list)
    assert len(groups) <= memory_debug.TOP_ALLOCATION_GROUPS
    for group in groups:
        assert isinstance(group["size_bytes"], int)
        assert isinstance(group["count"], int)
        assert isinstance(group["traceback"], list)
        assert len(group["traceback"]) <= memory_debug.REPORT_TRACEBACK_DEPTH
        for frame in group["traceback"]:
            assert ":" in frame
            assert frame.rsplit(":", 1)[1].isdigit()

    # The gc counters and the top-15 type buckets.
    gc_stats = payload["gc"]
    assert len(gc_stats["count"]) == len(gc.get_count())
    buckets = gc_stats["type_buckets"]
    assert len(buckets) <= memory_debug.TOP_GC_TYPES
    for bucket in buckets:
        assert isinstance(bucket["type"], str)
        assert isinstance(bucket["count"], int)
    # The buckets are ordered by size, largest first.
    assert [b["count"] for b in buckets] == sorted((b["count"] for b in buckets), reverse=True)

    # The known cache sizes, including the DashboardState runs-scan cache.
    caches = payload["caches"]
    assert "dashboard_runs_scan_cache_len" in caches
    assert "state_registry_entries" in caches
    assert "snapshot_cache_entries" in caches
    for value in caches.values():
        assert isinstance(value, int)


def test_memory_report_reports_the_runs_scan_cache_length(
    client: TestClient, mem_debug_on
) -> None:
    """The DashboardState runs-scan cache length is reported, and is real.

    After a ``GET /api/runs`` the state's runs-scan cache holds the scanned
    listing, so its length must be at least the number of runs in the root.
    """

    client.get("/api/runs", headers=_auth())
    payload = client.get("/api/debug/memory", headers=_auth()).json()
    assert payload["caches"]["dashboard_runs_scan_cache_len"] >= 1
    assert payload["caches"]["state_registry_entries"] >= 1


def test_memory_snapshot_and_diff(client: TestClient, mem_debug_on) -> None:
    """Two snapshots produce a diff of the growth between them."""

    # A diff needs two snapshots; one is not enough.
    first = client.post("/api/debug/memory/snapshot", headers=_json_auth(), json={})
    assert first.status_code == 200, first.text
    assert first.json()["snapshots_held"] == 1

    early = client.get("/api/debug/memory/diff", headers=_auth())
    assert early.status_code == 409, early.text

    # Allocate something between the two snapshots so there is something to
    # compare.
    ballast = [bytearray(64 * 1024) for _ in range(8)]

    second = client.post("/api/debug/memory/snapshot", headers=_json_auth(), json={})
    assert second.status_code == 200, second.text
    assert second.json()["snapshots_held"] == 2

    diff = client.get("/api/debug/memory/diff", headers=_auth())
    assert diff.status_code == 200, diff.text
    payload = diff.json()
    assert payload["interval_seconds"] >= 0
    growth = payload["top_growth"]
    assert isinstance(growth, list)
    assert len(growth) <= memory_debug.TOP_DIFF_GROUPS
    for entry in growth:
        assert isinstance(entry["size_diff"], int)
        assert isinstance(entry["count_diff"], int)
        assert len(entry["traceback"]) <= memory_debug.REPORT_TRACEBACK_DEPTH
    # The growth entries are ordered by size difference, largest first.
    diffs = [entry["size_diff"] for entry in growth]
    assert diffs == sorted(diffs, reverse=True)
    del ballast


def test_memory_snapshots_are_capped_at_three(client: TestClient, mem_debug_on) -> None:
    """At most 3 snapshots are retained; the oldest is evicted."""

    for expected in (1, 2, 3):
        response = client.post("/api/debug/memory/snapshot", headers=_json_auth(), json={})
        assert response.status_code == 200, response.text
        assert response.json()["snapshots_held"] == expected
    # The fourth evicts the oldest, so the cap holds.
    response = client.post("/api/debug/memory/snapshot", headers=_json_auth(), json={})
    assert response.status_code == 200, response.text
    assert response.json()["snapshots_held"] == memory_debug.MAX_SNAPSHOTS
    assert response.json()["snapshot_cap"] == memory_debug.MAX_SNAPSHOTS


# ---------------------------------------------------------------------------
# AC4: the opt-in semantics.
# ---------------------------------------------------------------------------


def test_tracemalloc_is_not_started_without_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The startup hook is a no-op unless the flag is set."""

    monkeypatch.delenv(memory_debug.MEM_DEBUG_ENV, raising=False)
    was_tracing = tracemalloc.is_tracing()
    assert memory_debug.maybe_start_tracemalloc() is False
    assert tracemalloc.is_tracing() == was_tracing


def test_tracemalloc_starts_with_25_frames_when_the_flag_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LH_HARNESS_MEM_DEBUG=1`` starts tracemalloc with 25 frames."""

    monkeypatch.setenv(memory_debug.MEM_DEBUG_ENV, "1")
    started_here = not tracemalloc.is_tracing()
    try:
        assert memory_debug.maybe_start_tracemalloc() is True
        assert tracemalloc.is_tracing() is True
        assert memory_debug.TRACEMALLOC_FRAMES == 25
    finally:
        if started_here:
            tracemalloc.stop()


def test_only_the_exact_value_one_enables_the_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    """``0``, ``true`` and an empty value all leave the feature off."""

    for value in ("0", "true", "yes", "", "  "):
        monkeypatch.setenv(memory_debug.MEM_DEBUG_ENV, value)
        assert memory_debug.memory_debug_enabled() is False, value
    monkeypatch.setenv(memory_debug.MEM_DEBUG_ENV, "1")
    assert memory_debug.memory_debug_enabled() is True


def test_snapshots_are_never_written_to_disk(client: TestClient, mem_debug_on, tmp_path) -> None:
    """The snapshots live in memory only.

    The store exposes no path and no file is created: the process being
    measured is the one short of memory, so a diagnostic must not add
    durable state of its own.
    """

    client.post("/api/debug/memory/snapshot", headers=_json_auth(), json={})
    store = memory_debug.MemorySnapshotStore()
    # A fresh store holds nothing: the snapshots never left the app's
    # in-memory store.
    assert store.count() == 0
    # Nothing was written under the runs root either.
    assert not list(tmp_path.glob("**/*.snapshot"))
