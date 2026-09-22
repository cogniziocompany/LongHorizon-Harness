"""Postgres-safety of ``GET /api/queue/shadow`` — task 220.

``PgQueueStore`` has no ``read_shadow_records`` method (the shadow log is a
file-store concept).  Before task 220 the endpoint called it unconditionally,
so under ``[queue] backend = "postgres"`` the route raised AttributeError and
surfaced as HTTP 500.  Now the endpoint returns an explicit, documented 501
naming the store that cannot serve shadow records, and the file-store path
(200 + real records) is unchanged.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lh_harness.pg_queue import PgQueueStore
from lh_harness.queue import QueueStore
from lh_harness.webapi.server import create_app

# A syntactically valid DSN; the connection itself is mocked so nothing ever
# leaves the host (same pattern as test_queue_store_selection.py).
FAKE_DSN = "postgresql://queue_user@127.0.0.1:5432/harness"


def _pg_conn_patches():
    """Patch the Postgres connection + migration so PgQueueStore constructs
    without touching any database."""
    return (
        patch("lh_harness.pg_queue._pg_connect", return_value=object()),
        patch.object(PgQueueStore, "_migrate", lambda self: None),
    )


def test_shadow_endpoint_501_on_store_without_read_shadow_records(tmp_path: Any) -> None:
    """A store lacking read_shadow_records (the PgQueueStore shape) gets an
    explicit 501 whose detail names the store class -- never AttributeError/500.
    """
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    conn_patch, migrate_patch = _pg_conn_patches()
    with patch(
        "lh_harness.config.load_run_defaults",
        return_value={"queue": {"backend": "postgres", "database_url": FAKE_DSN}},
    ):
        with conn_patch, migrate_patch:
            app = create_app(runs_root=root)
    # The active store really is the Postgres one and really lacks the method.
    store = app.state.queue_store
    assert isinstance(store, PgQueueStore)
    assert not hasattr(store, "read_shadow_records")

    client = TestClient(app)
    response = client.get("/api/queue/shadow")
    assert response.status_code == 501
    detail = response.json()["detail"]
    assert "PgQueueStore" in detail
    assert "shadow records are not available" in detail


def test_shadow_endpoint_serves_records_with_file_store(tmp_path: Any) -> None:
    """The default file-store path is unchanged: 200 + the real shadow record."""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    app = create_app(runs_root=root)
    assert isinstance(app.state.queue_store, QueueStore)
    store = app.state.queue_store
    store.append_shadow_record(
        {
            "schema_version": 2,
            "type": "queue.shadow_launch",
            "ts": 500.0,
            "payload": {"queue_id": "q-1"},
        }
    )

    client = TestClient(app)
    response = client.get("/api/queue/shadow")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["count"] == 1
    assert data["events"][0]["payload"]["queue_id"] == "q-1"