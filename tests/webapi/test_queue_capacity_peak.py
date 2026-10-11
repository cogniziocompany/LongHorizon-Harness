"""Schedule-aware lane capacity (``*_max_peak``) — 2026-10-11 work order item 5.

Peak = Mon-Fri 06:00-10:00 UTC (z.ai peak billing hours; dynamic concurrency
is tightest then). Each ``<trio>_max`` may carry a LOWER ``max_peak``
companion; the launcher resolves which cap applies on every scheduling pass.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from lh_harness.launcher import Launcher
from lh_harness.settings_store import SettingsStore


def _utc(year: int, month: int, day: int, hour: int) -> float:
    return dt.datetime(year, month, day, hour, tzinfo=dt.timezone.utc).timestamp()


# ---------------------------------------------------------------- window edges --

@pytest.mark.parametrize(
    "moment,expected",
    [
        (_utc(2026, 10, 12, 5), False),   # Monday 05:00 — before
        (_utc(2026, 10, 12, 6), True),    # Monday 06:00 — first peak minute
        (_utc(2026, 10, 12, 9), True),    # Monday 09:59 hour
        (_utc(2026, 10, 12, 10), False),  # Monday 10:00 — off again
        (_utc(2026, 10, 10, 8), False),   # Saturday — fully off-peak
        (_utc(2026, 10, 11, 8), False),   # Sunday — fully off-peak
        (_utc(2026, 10, 9, 8), False),    # Friday 08:00 UTC? -> Friday IS a weekday
    ],
)
def test_peak_window_edges(moment: float, expected: bool) -> None:
    # Friday is a weekday: fix the expectation explicitly.
    is_friday = dt.datetime.fromtimestamp(moment, dt.timezone.utc).weekday() == 4
    assert Launcher._in_capacity_peak_window(moment) == (True if is_friday else expected)


def test_effective_cap_resolution() -> None:
    peak_time = _utc(2026, 10, 12, 7)   # Monday, inside the window
    off_time = _utc(2026, 10, 12, 15)   # Monday afternoon, outside
    assert Launcher._effective_cap(20, 5, peak_time) == 5
    assert Launcher._effective_cap(20, 5, off_time) == 20
    assert Launcher._effective_cap(20, None, peak_time) == 20
    # A peak HIGHER than base is clamped to base: the companion can only lower.
    assert Launcher._effective_cap(3, 99, peak_time) == 3


# ------------------------------------------------------------------ store layer --

def _store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "settings.db", "x" * 40)


def test_store_adds_max_peak_column_and_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.seed_queue_defaults(
        trios={"kimi": {"agent": "codex"}},
        capacity={"kimi_max": 2},
    )
    with store._db() as db:
        cols = {row[1] for row in db.execute("PRAGMA table_info(queue_capacity)").fetchall()}
    assert "max_peak" in cols
    rows = store.queue_capacity_rows()
    assert rows["kimi"] == {"max": 2, "max_peak": None}
    with store._db() as db:
        db.execute("UPDATE queue_capacity SET max_peak = 1 WHERE trio = 'kimi'")
    assert store.queue_capacity_rows()["kimi"] == {"max": 2, "max_peak": 1}
    # Back-compat: the plain accessor keeps its int shape.
    assert store.queue_capacity()["kimi"] == 2


# ------------------------------------------------------------- admin API layer --

def _admin_client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from lh_harness.settings_store import SettingsRuntime
    from lh_harness.webapi.server import create_app

    store = SettingsStore(tmp_path / "settings.db", "k" * 40)
    store.seed_queue_defaults(
        trios={"zai": {"agent": "claude"}, "kimi": {"agent": "codex"}},
        capacity={"zai_max": 5, "kimi_max": 2},
    )
    rt = SettingsRuntime(store=store, admins=("admin@example.com",), proxy_auth="p" * 32, error=None)
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    app = create_app(runs_root=runs, auth_token="t" * 20, bind_host="testserver",
                     probe_open_pr=None, settings_runtime=rt)
    client = TestClient(app)

    def h(write: bool = False) -> dict[str, str]:
        out = {
            "Authorization": "Bearer " + "t" * 20,
            "X-LH-Proxy-Auth": "p" * 32,
            "X-Auth-Request-Email": "admin@example.com",
        }
        if write:
            out["X-Requested-With"] = "lh-harness"
        return out

    return client, store, h


def test_capacity_put_rich_form_sets_peak_and_legacy_preserves(tmp_path: Path) -> None:
    client, store, h = _admin_client(tmp_path)

    r = client.put("/api/queue/capacity", json={"capacity": {"zai": {"max": 20, "max_peak": 4}}}, headers=h(write=True))
    assert r.status_code == 200, r.text
    assert r.json()["capacity"]["zai"] == 20
    assert r.json()["max_peak"]["zai"] == 4

    # Legacy bare-int form: max changes, the peak override SURVIVES.
    r2 = client.put("/api/queue/capacity", json={"capacity": {"zai": 18}}, headers=h(write=True))
    assert r2.status_code == 200, r2.text
    assert r2.json()["capacity"]["zai"] == 18
    assert r2.json()["max_peak"]["zai"] == 4

    # Explicit null clears the peak override.
    r3 = client.put("/api/queue/capacity", json={"capacity": {"zai": {"max": 18, "max_peak": None}}}, headers=h(write=True))
    assert r3.status_code == 200, r3.text
    assert r3.json()["max_peak"]["zai"] is None

    got = client.get("/api/queue/capacity", headers=h()).json()
    assert got["capacity"]["zai"] == 18
    assert got["max_peak"]["zai"] is None
    assert got["peak_window"] == "Mon-Fri 06:00-10:00 UTC"


def test_capacity_put_validates_peak(tmp_path: Path) -> None:
    client, store, h = _admin_client(tmp_path)
    bad = client.put("/api/queue/capacity", json={"capacity": {"zai": {"max": 5, "max_peak": -1}}}, headers=h(write=True))
    assert bad.status_code == 422
    bad2 = client.put("/api/queue/capacity", json={"capacity": {"zai": {"max": 5, "max_peak": True}}}, headers=h(write=True))
    assert bad2.status_code == 422
    # Nothing was written by the failed batch.
    assert client.get("/api/queue/capacity", headers=h()).json()["capacity"]["zai"] == 5


def test_capacity_write_requires_admin(tmp_path: Path) -> None:
    client, store, h = _admin_client(tmp_path)
    no_auth = client.put("/api/queue/capacity", json={"capacity": {"zai": 9}})
    assert no_auth.status_code in (401, 403)


# ------------------------------------------------------------ launcher overlay --

def test_refresh_applies_peak_inside_window_and_base_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from lh_harness.launcher import Launcher
    from lh_harness.queue import QueueStore, default_queue_config

    root = tmp_path / "runs"
    root.mkdir(parents=True)
    queue_store = QueueStore(root)

    class Supervisor:
        def __init__(self) -> None:
            self.created = []

        def can_control(self, run_id: str) -> bool:
            return True

        def create_run(self, **kwargs):
            self.created.append(kwargs)
            return {"run_id": f"run-{len(self.created)}"}

        def status(self, run_id: str):
            return {"managed": True, "alive": True, "status": "running"}

    db_path = tmp_path / "settings.db"
    monkeypatch.setenv("LH_HARNESS_SETTINGS_DB", str(db_path))
    monkeypatch.setenv("LH_HARNESS_SETTINGS_KEY", "k" * 40)

    from lh_harness import settings_store as ss

    settings = SettingsStore(db_path, "k" * 40)
    settings.seed_queue_defaults(
        trios={"kimi": {"agent": "claude_code"}},
        capacity={"kimi_max": 2},
    )
    with settings._db() as db:
        db.execute("UPDATE queue_capacity SET max_peak = 1 WHERE trio = 'kimi'")

    launcher = Launcher(Supervisor(), queue_store, queue_config=default_queue_config())
    assert launcher._settings_store is not None

    inside = dt.datetime(2026, 10, 12, 7, tzinfo=dt.timezone.utc).timestamp()
    outside = dt.datetime(2026, 10, 12, 15, tzinfo=dt.timezone.utc).timestamp()

    monkeypatch.setattr(Launcher, "_in_capacity_peak_window", staticmethod(lambda now=None: True))
    launcher._refresh_queue_config()
    assert launcher._config["capacity"]["kimi_max"] == 1

    monkeypatch.setattr(Launcher, "_in_capacity_peak_window", staticmethod(lambda now=None: False))
    launcher._refresh_queue_config()
    assert launcher._config["capacity"]["kimi_max"] == 2
    _ = inside, outside, ss
