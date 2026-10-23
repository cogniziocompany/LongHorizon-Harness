"""Postgres queue backend: per-task model/roles override persistence (R9).

LHH-QUEUE-HOTRELOAD R6 put the validated per-task ``model`` and the dict-form
``roles`` pin set on ``QueueEntry`` and the file ``QueueStore`` persists them
fully; R8/R7 consume them (pending edits while queued, launch-time resolution
and lock).  ``PgQueueStore`` had no columns and dropped both fields on every
INSERT/UPDATE and re-read, so a postgres-backed queue silently lost them --
this module proves the R9 closure: the two columns, written and read back
exactly like the file store persists its JSON, through the store's own create
/ get / list / update surface, with the file store's enqueue preflight (live
model-list validation + all-or-nothing settings audit) running identically.

Two layers:
- hermetic (always run): the column set, the in-code migration tail, and the
  exact values the store writes and reads, through a recording fake
  connection (same pattern as test_pg_queue_retry_columns.py);
- live (only with ``LH_HARNESS_DB_URL`` naming a SCRATCH database, skipped
  otherwise): the same round trip against a real server, proving the real
  schema carries the columns after ``PgQueueStore``'s connect-time migration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from lh_harness import pg_queue
from lh_harness.queue import QueueEntry
from lh_harness.settings_store import SettingsRuntime, SettingsStore

OVERRIDE_COLUMNS = ("model", "roles")

_FAKE_CATALOG = {
    "agents": [],
    "models": {
        "claude_code": [
            {"id": "opus", "label": "opus · default", "availability": "detected"},
            {"id": "sonnet", "label": "sonnet", "availability": "detected"},
        ],
        "codex": [{"id": "codex-mini", "label": "codex-mini", "availability": "detected"}],
    },
    "model_discovery": {},
}

_PINS = {"executor": {"model": "sonnet", "reasoning_effort": "high"}}

_BASE_BODY: dict[str, Any] = {
    "name": "n",
    "task": "t",
    "workspace": "w",
    "requested_by": "api-test",
}


def _fake_model_catalog(*, force: bool = False, **_kwargs: Any) -> dict[str, Any]:
    return json.loads(json.dumps(_FAKE_CATALOG))


def _broken_fetch(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("provider unreachable")


class _FetchSpy:
    """A model_fetch seam that records how often the live list was consulted."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return _fake_model_catalog()


def _body(**overrides: Any) -> dict[str, Any]:
    return {**_BASE_BODY, **overrides}


# --- hermetic: column set, values, and the in-code migration tail -----------


def test_column_list_covers_both_overrides_append_only() -> None:
    columns = pg_queue._QUEUE_COLUMNS
    # Append-only after the migration-006 column, so the 001..006 row shape
    # (and every existing assertion zipping columns to values) is untouched.
    # The gtm_manager column (migration 007, GTM-MANAGER-G1-a) is appended
    # after the two override columns, so it sits at the end of the list.
    assert columns[-4] == "time_limit_minutes"
    assert columns[-3:-1] == OVERRIDE_COLUMNS
    assert columns[-1] == "gtm_manager"
    assert _OVERRIDE_COLUMNS_SQL_MARKERS()


def _OVERRIDE_COLUMNS_SQL_MARKERS() -> bool:
    sql = pg_queue._MIGRATION_TASK_OVERRIDES_SQL
    return (
        "ALTER TABLE harness.queue" in sql
        and "ADD COLUMN IF NOT EXISTS model VARCHAR(256)" in sql
        and "ADD COLUMN IF NOT EXISTS roles JSONB" in sql
        # Marker comment so the applied statement is greppable in any
        # statement log, exactly like the migrations/*.sql header comments.
        and "LHH-QUEUE-HOTRELOAD R9" in sql
    )


def test_entry_values_write_model_and_roles_like_the_file_store() -> None:
    columns = pg_queue._QUEUE_COLUMNS
    entry = QueueEntry(
        queue_id="q-1", name="n", task="t", workspace="w", max_rounds=3, trio="kimi",
        priority=1, requested_by="ci", model="sonnet", roles=dict(_PINS),
    )
    values = dict(zip(columns, pg_queue._entry_values(entry)))
    assert len(pg_queue._entry_values(entry)) == len(columns)
    assert values["model"] == "sonnet"
    assert json.loads(values["roles"]) == _PINS  # JSONB text, like skip_reasons


def test_entry_values_normalise_override_shapes() -> None:
    def _values(entry_kwargs: dict[str, Any]) -> dict[str, Any]:
        entry = QueueEntry(
            queue_id="q-1", name="n", task="t", workspace="w", max_rounds=3, trio="kimi",
            priority=1, requested_by="ci", **entry_kwargs,
        )
        return dict(zip(pg_queue._QUEUE_COLUMNS, pg_queue._entry_values(entry)))

    plain = _values({})
    assert plain["model"] is None and plain["roles"] is None
    # A non-string model a hand-built entry could carry never reaches SQL.
    assert _values({"model": 5})["model"] is None
    assert _values({"model": "   "})["model"] is None
    assert _values({"model": " sonnet "})["model"] == "sonnet"
    # A non-dict roles value is stored NULL (the entry carries no pins);
    # an empty dict keeps the file store's on-disk shape verbatim.
    assert _values({"roles": ["kimi"]})["roles"] is None
    assert _values({"roles": {}})["roles"] == "{}"


def test_cast_reads_back_model_and_roles() -> None:
    cast = pg_queue.PgQueueStore._cast
    # psycopg decodes JSONB itself; a raw string is decoded explicitly.
    assert cast(dict(_PINS), "roles") == _PINS
    assert cast(json.dumps(_PINS), "roles") == _PINS
    assert cast("{}", "roles") == {}
    # Anything that is not a pin-set dict is "no pins" (None), like NULL.
    assert cast(None, "roles") is None
    assert cast("[1]", "roles") is None
    assert cast(7.0, "roles") is None
    assert cast(["kimi"], "roles") is None
    # Persistence stores model as-validated and never re-validates on read:
    # the VARCHAR value passes through untouched, NULL reads back None.
    assert cast("gpt-unknown-tomorrow", "model") == "gpt-unknown-tomorrow"
    assert cast(None, "model") is None


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


def _settings(tmp_path: Path) -> tuple[SettingsStore, SettingsRuntime]:
    store = SettingsStore(tmp_path / "settings.db", "k" * 40)
    return store, SettingsRuntime(store=store, error=None)


def _insert_count(conn: _FakeConn) -> int:
    return sum(
        1
        for sql, params in conn.statements
        if params is not None and " ".join(sql.split()).startswith("INSERT INTO harness.queue (")
    )


def test_migrate_applies_the_file_migrations_and_the_override_tail(
    fake_store,
) -> None:
    _, conn = fake_store
    applied = [sql for sql, params in conn.statements if params is None]
    # The migration files (header comment carries the filename) AND the
    # in-code override tail, in one committed unit -- a connect can never
    # leave a schema that takes the old columns but not the new ones.
    assert any("006_harness_queue_time_limit.sql" in sql for sql in applied)
    assert sum("LHH-QUEUE-HOTRELOAD R9" in sql for sql in applied) == 1
    assert applied.index(_match(applied, "006_harness_queue_time_limit.sql")) < applied.index(
        _match(applied, "LHH-QUEUE-HOTRELOAD R9")
    )
    assert conn.commits >= 1 and conn.rollbacks == 0


def _match(applied: list[str], needle: str) -> str:
    return next(sql for sql in applied if needle in sql)


def test_enqueue_with_overrides_persists_across_store_instances(
    fake_store, tmp_path: Path
) -> None:
    store, conn = fake_store
    settings_store, runtime = _settings(tmp_path)
    entry = store.create(
        _body(trio="glm", model="sonnet", roles=dict(_PINS)),
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )
    assert entry.model == "sonnet" and entry.roles == _PINS
    # The raw row carries both columns (the SQL itself wrote them).
    row = dict(zip(pg_queue._QUEUE_COLUMNS, conn.rows[entry.queue_id]))
    assert row["model"] == "sonnet"
    assert json.loads(row["roles"]) == _PINS
    # A re-read through the same store sees them...
    read = store.get(entry.queue_id)
    assert read is not None and read.model == "sonnet" and read.roles == _PINS
    # ...and so does a fresh store instance over the same backing rows:
    # the overrides survive persistence, like the file store's JSON files.
    with patch("lh_harness.pg_queue._pg_connect", return_value=conn):
        reopened = pg_queue.PgQueueStore("postgresql://fake/scratch")
    reread = reopened.get(entry.queue_id)
    assert reread is not None and reread.model == "sonnet" and reread.roles == _PINS
    assert any(item.queue_id == entry.queue_id and item.model == "sonnet" for item in reopened.list())
    # One audit row, the exact R4/R5/R6 shape the file store writes.
    rows = settings_store.audit(10)
    assert len(rows) == 1
    record = rows[0]
    assert record["action"] == "enqueue"
    assert record["name"] == f"queue_entry:{entry.queue_id}"
    assert record["actor"] == "api-test"
    assert record["detail"]["before"] is None
    assert record["detail"]["after"] == {"model": "sonnet", "roles": _PINS}


def test_mcp_create_kwargs_accepted_and_actor_stamped(
    fake_store, tmp_path: Path
) -> None:
    """The MCP dispatcher calls create with verified_caller + both seams;
    the store must take that exact call (it previously had no such kwargs)."""
    store, conn = fake_store
    settings_store, runtime = _settings(tmp_path)
    entry = store.create(
        _body(trio="glm", model="sonnet"),
        observed_addr="10.0.0.1",
        verified_caller="caller-sso",
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )
    assert store.get(entry.queue_id).model == "sonnet"
    rows = settings_store.audit(10)
    assert len(rows) == 1 and rows[0]["actor"] == "caller-sso"


def test_pending_edit_through_the_store_persists(fake_store, tmp_path: Path) -> None:
    """The R7 route's store mechanics: read -> validate -> mutate -> update.

    The edited values must survive the UPDATE exactly like the file store's
    rewritten JSON: this is the pending-edit -> launch-resolution hop.
    """
    store, conn = fake_store
    settings_store, runtime = _settings(tmp_path)
    entry = store.create(
        _body(trio="glm", model="sonnet", roles=dict(_PINS)),
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )
    read = store.get(entry.queue_id)
    read.model = "codex-mini"  # the R7 route re-validates against the live list first
    read.roles = {"manager": {"model": "opus"}}
    store.update(read)
    row = dict(zip(pg_queue._QUEUE_COLUMNS, conn.rows[entry.queue_id]))
    assert row["model"] == "codex-mini"
    assert json.loads(row["roles"]) == {"manager": {"model": "opus"}}
    again = store.get(entry.queue_id)
    assert again.model == "codex-mini" and again.roles == {"manager": {"model": "opus"}}


def test_legacy_entries_and_old_rows_read_back_unchanged(fake_store, tmp_path: Path) -> None:
    """Backward compatibility, no migration that breaks existing rows:
    override-free entries, the legacy string roles alias, and rows written
    before the columns existed (NULL) all behave exactly as before."""
    store, conn = fake_store
    spy = _FetchSpy()
    # Plain legacy body, string form of ``roles`` -- the unchanged trio alias.
    entry = store.create(_body(roles="kimi"), model_fetch=spy)
    assert entry.trio == "kimi"
    assert entry.model is None and entry.roles is None
    # No overrides: the preflight never consulted the live list at all.
    assert spy.calls == 0
    row = dict(zip(pg_queue._QUEUE_COLUMNS, conn.rows[entry.queue_id]))
    assert row["trio"] == "kimi" and row["model"] is None and row["roles"] is None
    read = store.get(entry.queue_id)
    assert read.trio == "kimi" and read.model is None and read.roles is None
    # No settings store was required for an override-free enqueue.
    # Old rows: a pre-R9 row has NULL in both columns; force it, then read.
    forced = list(conn.rows[entry.queue_id])
    for column in OVERRIDE_COLUMNS:
        forced[pg_queue._QUEUE_COLUMNS.index(column)] = None
    conn.rows[entry.queue_id] = tuple(forced)
    legacy = store.get(entry.queue_id)
    assert legacy.trio == "kimi" and legacy.model is None and legacy.roles is None
    assert legacy.status == "pending" and legacy.requested_by == "api-test"


def test_create_preflight_matches_the_file_store(fake_store, tmp_path: Path) -> None:
    """Unknown model / provider down / settings store down all refuse BEFORE
    the dedup lookup and the INSERT, and nothing is persisted or audited --
    the exact all-or-nothing semantics of the file store's create."""
    from fastapi import HTTPException

    store, conn = fake_store
    settings_store, runtime = _settings(tmp_path)
    with pytest.raises(ValueError, match="unknown model 'zzz' in the live model list"):
        store.create(_body(model="zzz"), model_fetch=_fake_model_catalog, settings_runtime=runtime)
    with pytest.raises(HTTPException) as provider:
        store.create(_body(model="sonnet"), model_fetch=_broken_fetch, settings_runtime=runtime)
    assert provider.value.status_code == 503
    assert "live model list is unavailable" in provider.value.detail
    broken = SettingsRuntime(store=None, error="no settings db")
    with pytest.raises(HTTPException) as unavailable:
        store.create(_body(model="sonnet"), model_fetch=_fake_model_catalog, settings_runtime=broken)
    assert unavailable.value.status_code == 503
    # The runtime's own error text wins, exactly like the file store's create
    # (``_service_unavailable(store_error or _AUDIT_STORE_UNAVAILABLE)``).
    assert unavailable.value.detail == "no settings db"
    with pytest.raises(HTTPException) as canonical:
        store.create(
            _body(model="sonnet"),
            model_fetch=_fake_model_catalog,
            settings_runtime=SettingsRuntime(store=None, error=None),
        )
    assert canonical.value.status_code == 503
    assert "settings store is not configured" in canonical.value.detail
    # Nothing was inserted, no dedup SELECT resolved anything, no audit row.
    assert _insert_count(conn) == 0
    assert conn.rows == {}
    assert settings_store.audit(10) == []


def test_override_free_enqueue_neither_fetches_nor_audits(
    fake_store, tmp_path: Path
) -> None:
    store, _ = fake_store
    settings_store, _unused = _settings(tmp_path)
    spy = _FetchSpy()
    entry = store.create(
        _body(trio="glm"),
        model_fetch=spy,
        settings_runtime=SettingsRuntime(store=None, error="unconfigured"),
    )
    assert entry.model is None and entry.roles is None
    assert spy.calls == 0
    assert settings_store.audit(10) == []


def test_roles_pin_set_round_trips_byte_like_for_like(
    fake_store, tmp_path: Path
) -> None:
    """The pin set (not resolved output) is what persists: the launcher's R8
    resolution must see exactly the caller's pins, with hot-reload still
    flowing into unpinned fields until launch."""
    store, conn = fake_store
    _, runtime = _settings(tmp_path)
    pins = {
        "executor": {"model": "sonnet", "reasoning_effort": "high"},
        "auditor": {"model": "opus"},
    }
    entry = store.create(
        _body(trio="glm", roles=pins),
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )
    read = store.get(entry.queue_id)
    assert read.roles == pins
    assert read.model is None  # roles pins never imply a model override
    assert read.trio == "glm"  # dict-form roles does not alias the trio


def test_model_only_override_round_trips(fake_store, tmp_path: Path) -> None:
    store, _ = fake_store
    _, runtime = _settings(tmp_path)
    entry = store.create(
        _body(trio="kimi", model="codex-mini"),
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )
    read = store.get(entry.queue_id)
    assert read.model == "codex-mini" and read.roles is None and read.trio == "kimi"


# --- live: a real (scratch) Postgres ----------------------------------------


def _live_store() -> pg_queue.PgQueueStore:
    url = (os.environ.get("LH_HARNESS_DB_URL") or "").strip()
    if not url:
        pytest.skip("LH_HARNESS_DB_URL not set; Postgres backend unavailable")
    pytest.importorskip("psycopg")
    from urllib.parse import urlsplit

    if urlsplit(url).path.lstrip("/") == "lh_harness":
        pytest.skip("LH_HARNESS_DB_URL names the production database lh_harness; use a scratch database")
    store = pg_queue.PgQueueStore(url)
    try:
        with store._txn() as txn:  # noqa: SLF001 - test-only reset
            txn.execute("TRUNCATE harness.queue_events, harness.queue")
    except Exception:
        pytest.skip("could not reach/reset the scratch Postgres backend")
    return store


def test_live_overrides_round_trip(tmp_path: Path) -> None:
    store = _live_store()
    settings_store, runtime = _settings(tmp_path)
    entry = store.create(
        _body(trio="glm", model="sonnet", roles=dict(_PINS)),
        model_fetch=_fake_model_catalog,
        settings_runtime=runtime,
    )
    read = store.get(entry.queue_id)
    assert read is not None
    assert read.model == "sonnet" and read.roles == _PINS
    read.model = "codex-mini"
    store.update(read)
    assert store.get(entry.queue_id).model == "codex-mini"
    rows = settings_store.audit(10)
    assert len(rows) == 1 and rows[0]["detail"]["after"] == {"model": "sonnet", "roles": _PINS}
