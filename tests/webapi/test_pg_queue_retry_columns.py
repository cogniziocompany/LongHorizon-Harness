"""Postgres queue backend: retry lineage and provider-quota backoff columns.

Migration 005 adds ``retry_of`` / ``attempt`` / ``failure_cause`` /
``not_before`` / ``wait_reason`` to ``harness.queue`` so ``PgQueueStore``
round-trips them like the file ``QueueStore`` does.

Two layers:
- hermetic (always run): the migration text, the column list, and the exact
  values the store writes and reads, through a recording fake connection;
- live (only with ``LH_HARNESS_DB_URL`` naming a SCRATCH database, skipped
  otherwise): the same behaviour against a real server, including parity with
  the file store and the launcher's "waiting:" skip on a re-read entry.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from lh_harness import pg_queue
from lh_harness.queue import QueueEntry, QueueStore

REPO_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
NEW_COLUMNS = ("retry_of", "attempt", "failure_cause", "not_before", "wait_reason")

# --- hermetic: migration and column set -------------------------------------


def test_migration_005_adds_the_five_columns_idempotently() -> None:
    path = REPO_MIGRATIONS / "005_harness_queue_retry_backoff.sql"
    sql = path.read_text(encoding="utf-8")
    for column, sql_type in (
        ("retry_of", "VARCHAR(128)"),
        ("attempt", "INTEGER NOT NULL DEFAULT 1"),
        ("failure_cause", "TEXT"),
        ("not_before", "DOUBLE PRECISION"),
        ("wait_reason", "VARCHAR(64)"),
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column} {sql_type}" in sql
    assert "CREATE INDEX IF NOT EXISTS harness_queue_retry_of_idx" in sql
    # Runs after every earlier migration (PgQueueStore applies them by filename).
    names = sorted(p.name for p in REPO_MIGRATIONS.glob("*.sql"))
    assert names[-1] == path.name


def test_store_column_list_and_row_values_cover_the_new_fields() -> None:
    columns = pg_queue._QUEUE_COLUMNS
    for column in NEW_COLUMNS:
        assert column in columns
    entry = QueueEntry(
        queue_id="q-1", name="n", task="t", workspace="w", max_rounds=3, trio="kimi",
        priority=1, requested_by="ci", retry_of="q-0", attempt=2, failure_cause="provider_quota",
        not_before=1234.5, wait_reason="provider_quota",
    )
    values = dict(zip(columns, pg_queue._entry_values(entry)))
    assert len(pg_queue._entry_values(entry)) == len(columns)
    assert values["retry_of"] == "q-0"
    assert values["attempt"] == 2
    assert values["failure_cause"] == "provider_quota"
    assert values["not_before"] == 1234.5
    assert values["wait_reason"] == "provider_quota"
    # Every other column carries the entry's own value.
    expected = entry.to_dict()
    for column in columns:
        if column == "skip_reasons":
            continue
        assert values[column] == expected[column], column


def test_row_values_normalise_like_the_file_store() -> None:
    entry = QueueEntry(
        queue_id="q-1", name="n", task="t", workspace="w", max_rounds=3, trio="kimi",
        priority=1, requested_by="ci", attempt=0, wait_reason="orphan label",
    )
    values = dict(zip(pg_queue._QUEUE_COLUMNS, pg_queue._entry_values(entry)))
    assert values["attempt"] == 1  # never below 1
    assert values["not_before"] is None
    assert values["wait_reason"] is None  # a label only travels with a not_before
    entry.not_before = 99.0
    entry.wait_reason = "x" * 200
    values = dict(zip(pg_queue._QUEUE_COLUMNS, pg_queue._entry_values(entry)))
    assert values["wait_reason"] == "x" * 64


# --- hermetic: a recording fake connection ----------------------------------


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self.conn = conn
        self._result: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.statements.append((sql, params))
        text = " ".join(sql.split())
        cols = pg_queue._QUEUE_COLUMNS
        if text.startswith("INSERT INTO harness.queue ("):
            row = tuple(params)
            self.conn.rows[row[0]] = row
            self._result = []
        elif text.startswith("UPDATE harness.queue SET"):
            row = tuple(params[: len(cols)])
            self.conn.rows[params[-1]] = row
            self._result = []
        elif text.startswith("SELECT") and "FROM harness.queue WHERE queue_id" in text:
            row = self.conn.rows.get(params[0])
            self._result = [row] if row else []
        elif text.startswith("SELECT") and "FROM harness.queue" in text and "WHERE" not in text:
            self._result = list(self.conn.rows.values())
        else:
            self._result = []

    def fetchone(self) -> Any:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[Any]:
        return list(self._result)

    def close(self) -> None:
        self.conn.closed_cursors += 1


class _FakeConn:
    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []
        self.rows: dict[str, tuple[Any, ...]] = {}
        self.commits = 0
        self.rollbacks = 0
        self.closed_cursors = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture()
def fake_store() -> tuple[pg_queue.PgQueueStore, _FakeConn]:
    conn = _FakeConn()
    with patch("lh_harness.pg_queue._pg_connect", return_value=conn):
        store = pg_queue.PgQueueStore("postgresql://fake/scratch")
    return store, conn


def _comparable(entry: QueueEntry) -> dict[str, Any]:
    """to_dict() minus launched_at/last_checked_at, which the PG store reads
    back as 0.0 for NULL (pre-existing ``_cast`` behaviour, out of scope here)."""
    data = entry.to_dict()
    data.pop("launched_at")
    data.pop("last_checked_at")
    return data


def _failed_original(store: pg_queue.PgQueueStore) -> QueueEntry:
    entry = store.create(
        {"name": "quota task", "task": "t", "workspace": "/w", "trio": "kimi",
         "requested_by": "ci", "branch": "feat/x"}
    )
    store.mark_failed(entry.queue_id, "provider_quota | run failed")
    return entry


def test_migrations_include_005_and_run_in_one_committed_unit(fake_store) -> None:
    store, conn = fake_store
    applied = [sql for sql, params in conn.statements if params is None]
    assert any("005_harness_queue_retry_backoff.sql" in sql for sql in applied)
    assert conn.commits >= 1 and conn.rollbacks == 0


def test_requeue_writes_and_reads_back_lineage_and_backoff(fake_store) -> None:
    store, conn = fake_store
    original = _failed_original(store)
    successor = store.requeue(
        original.queue_id, "provider_quota", not_before=2_000_000_000.0, wait_reason="provider_quota"
    )
    assert successor is not None
    row = dict(zip(pg_queue._QUEUE_COLUMNS, conn.rows[successor.queue_id]))
    assert row["retry_of"] == original.queue_id
    assert row["attempt"] == 2
    assert row["failure_cause"] == "provider_quota"
    assert row["not_before"] == 2_000_000_000.0
    assert row["wait_reason"] == "provider_quota"
    assert row["branch"] == "feat/x"  # a retried continuation stays one (file-store parity)

    read = store.get(successor.queue_id)
    assert read is not None
    assert (read.retry_of, read.attempt, read.failure_cause, read.not_before, read.wait_reason) == (
        original.queue_id, 2, "provider_quota", 2_000_000_000.0, "provider_quota",
    )
    assert _comparable(read) == _comparable(successor)


def test_update_keeps_the_backoff_columns(fake_store) -> None:
    store, conn = fake_store
    original = _failed_original(store)
    successor = store.requeue(original.queue_id, "provider_quota", not_before=5.0e9, wait_reason="provider_quota")
    store.record_skip(successor.queue_id, "waiting: provider_quota until ...")
    read = store.get(successor.queue_id)
    assert read.not_before == 5.0e9 and read.wait_reason == "provider_quota" and read.attempt == 2
    assert read.skip_reasons == ["waiting: provider_quota until ..."]


def test_attempt_chain_reaches_the_max_retries_cap_through_reads(fake_store) -> None:
    """Before 005 the attempt was not stored, so every re-read was attempt 1 and
    the cap (attempt > 2) could never trip."""
    store, _ = fake_store
    current = _failed_original(store)
    for expected_attempt in (2, 3):
        current = store.requeue(current.queue_id, "provider_rate_limit")
        assert store.get(current.queue_id).attempt == expected_attempt
        store.mark_failed(current.queue_id, "provider_rate_limit")
    with pytest.raises(ValueError, match="exceeded max_retries"):
        store.requeue(current.queue_id, "provider_rate_limit")


def test_null_columns_from_old_rows_read_as_file_store_defaults(fake_store) -> None:
    store, conn = fake_store
    entry = store.create({"name": "n", "task": "t", "workspace": "/w", "trio": "kimi", "requested_by": "ci"})
    row = list(conn.rows[entry.queue_id])
    for column in NEW_COLUMNS:
        row[pg_queue._QUEUE_COLUMNS.index(column)] = None
    conn.rows[entry.queue_id] = tuple(row)
    read = store.get(entry.queue_id)
    assert (read.retry_of, read.attempt, read.failure_cause, read.not_before, read.wait_reason) == (
        None, 1, None, None, None,
    )


def test_jsonb_skip_reasons_decoded_by_the_driver_are_kept() -> None:
    assert pg_queue.PgQueueStore._cast(["a", "b"], "skip_reasons") == ["a", "b"]
    assert pg_queue.PgQueueStore._cast('["a"]', "skip_reasons") == ["a"]
    assert pg_queue.PgQueueStore._cast(None, "skip_reasons") == []


def test_txn_commits_on_success_and_rolls_back_on_error() -> None:
    conn = _FakeConn()
    with pg_queue._Txn(conn) as txn:
        txn.execute("SELECT 1")
    assert (conn.commits, conn.rollbacks, conn.closed_cursors) == (1, 0, 1)
    with pytest.raises(RuntimeError):
        with pg_queue._Txn(conn) as txn:
            raise RuntimeError("boom")
    assert (conn.commits, conn.rollbacks, conn.closed_cursors) == (1, 1, 2)


# --- live: a real (scratch) Postgres ----------------------------------------


def _live_store() -> pg_queue.PgQueueStore:
    url = (os.environ.get("LH_HARNESS_DB_URL") or "").strip()
    if not url:
        pytest.skip("LH_HARNESS_DB_URL not set; Postgres backend unavailable")
    from urllib.parse import urlsplit

    if urlsplit(url).path.lstrip("/") == "lh_harness":
        pytest.skip("LH_HARNESS_DB_URL names the production database; use a scratch database")
    pytest.importorskip("psycopg")
    try:
        store = pg_queue.PgQueueStore(url)
        store.counts()
    except Exception as exc:
        pytest.skip(f"could not connect to Postgres backend: {exc}")
    return store


def test_live_requeue_backoff_survives_a_fresh_connection() -> None:
    store = _live_store()
    original = _failed_original(store)
    until = time.time() + 1800
    successor = store.requeue(original.queue_id, "provider_quota", not_before=until, wait_reason="provider_quota")
    reopened = pg_queue.PgQueueStore(store.database_url)
    read = reopened.get(successor.queue_id)
    assert read is not None
    assert read.retry_of == original.queue_id
    assert read.attempt == 2
    assert read.failure_cause == "provider_quota"
    assert read.not_before == pytest.approx(until)
    assert read.wait_reason == "provider_quota"
    assert _comparable(read) == _comparable(successor)
    # The launcher skips it until the window ends, exactly as for the file store.
    from lh_harness.launcher import Launcher

    reason = Launcher._waiting_skip_reason(None, read, now=until - 60)  # type: ignore[arg-type]
    assert reason is not None and reason.startswith("waiting: provider_quota until ")
    assert Launcher._waiting_skip_reason(None, read, now=until + 1) is None  # type: ignore[arg-type]
    # A pending successor is findable by retry_of from list().
    assert [e.queue_id for e in reopened.list() if e.retry_of == original.queue_id] == [successor.queue_id]


def test_live_matches_the_file_store_field_for_field(tmp_path: Path) -> None:
    pg = _live_store()
    files = QueueStore(tmp_path / "runs")
    body = {"name": "parity", "task": "t", "workspace": "/w", "trio": "kimi", "requested_by": "ci",
            "priority": 4, "max_rounds": 6, "branch": "feat/p"}

    def run(store: Any) -> list[dict[str, Any]]:
        first = store.create(dict(body))
        store.mark_failed(first.queue_id, "provider_rate_limit")
        second = store.requeue(first.queue_id, "provider_rate_limit", not_before=3.0e9, wait_reason="provider_rate_limit")
        store.mark_failed(second.queue_id, "provider_rate_limit")
        third = store.requeue(second.queue_id, "provider_rate_limit")
        out = []
        for entry in (store.get(first.queue_id), store.get(second.queue_id), store.get(third.queue_id)):
            data = entry.to_dict()
            for volatile in ("queue_id", "retry_of", "created_at", "updated_at", "launched_at",
                             "last_checked_at", "requester"):
                data.pop(volatile)
            data["has_retry_of"] = entry.retry_of is not None
            out.append(data)
        return out

    assert run(pg) == run(files)


def test_live_old_rows_get_attempt_1_after_the_migration() -> None:
    store = _live_store()
    entry = store.create({"name": "old", "task": "t", "workspace": "/w", "trio": "kimi", "requested_by": "ci"})
    with store._txn() as txn:  # noqa: SLF001 - simulate a row written before 005
        txn.execute(
            "UPDATE harness.queue SET retry_of = NULL, failure_cause = NULL, not_before = NULL, "
            "wait_reason = NULL, attempt = DEFAULT WHERE queue_id = %s",
            (entry.queue_id,),
        )
    read = store.get(entry.queue_id)
    assert (read.retry_of, read.attempt, read.failure_cause, read.not_before, read.wait_reason) == (
        None, 1, None, None, None,
    )
