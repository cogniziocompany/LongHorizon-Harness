"""Migration-order tests for the Postgres queue schema.

Applies ``migrations/001_harness_queue.sql`` then
``migrations/002_harness_queue_events.sql`` to an *empty* scratch database and
asserts the final shape, then re-applies both files and asserts the second run
is a clean no-op.  This is the fresh-DB regression test for task 218: 001 used
to fail on an empty schema with ``ERROR: default for column "status" cannot be
cast automatically to type queue_status`` because it converted the ``status``
column after creating it with a ``VARCHAR`` default.

Every test runs only when a scratch Postgres is reachable via
``LH_HARNESS_DB_URL``; without it the whole module is skipped so the no-DB
hermetic suite stays green.  To run the check locally::

    LH_HARNESS_DB_URL=postgresql://user@localhost:5432/lh_harness_migrate_test \\
        pytest tests/webapi/test_pg_migrations.py -v

The URL may point at any empty database the role owns; the tests never write
to the production ``lh_harness`` database.  Credentials come from the
environment names only (``LH_HARNESS_DB_PASSWORD`` is picked up by the driver
at connect time) -- never from a file, fixture, or commit.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("psycopg")

REPO_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

# Applied in filename order, exactly as PgQueueStore._migrate does.
MIGRATION_FILES = (
    "001_harness_queue.sql",
    "002_harness_queue_events.sql",
    "003_harness_queue_continuation.sql",
)


def _migration_sql() -> list[str]:
    """Read the ordered migration files straight from the repository."""
    return [(REPO_MIGRATIONS / name).read_text() for name in MIGRATION_FILES]


def _scratch_db_name() -> str:
    """Return the scratch database name from the configured URL, if any."""
    value = os.environ.get("LH_HARNESS_DB_URL")
    if value is None or not value.strip():
        return ""
    from urllib.parse import urlsplit

    return urlsplit(value.strip()).path.lstrip("/")


def _apply_migrations(conn) -> None:
    """Apply all migrations inside one transaction, mirroring _migrate()."""
    with conn.transaction():
        for sql in _migration_sql():
            conn.execute(sql)


@pytest.fixture(scope="module")
def scratch_conn():
    """Yield a connection to an empty scratch database, reset around the run.

    Skips the module when ``LH_HARNESS_DB_URL`` is unset (no Postgres
    configured) so the hermetic suite stays green without a backend.
    """
    url = os.environ.get("LH_HARNESS_DB_URL")
    if not url or not url.strip():
        pytest.skip("LH_HARNESS_DB_URL not set; Postgres backend unavailable")
    url = url.strip()

    psycopg = pytest.importorskip("psycopg")

    db = urlsplit_path_db(url)
    admin_url = _admin_url(url)
    try:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            _drop_scratch_objects(admin, db)
    except Exception as exc:  # pragma: no cover - environment-specific
        pytest.skip(f"could not reach Postgres at LH_HARNESS_DB_URL: {exc}")

    with psycopg.connect(url, autocommit=True) as conn:
        # The database must be empty of our objects: the tests assert the
        # shape a fresh apply produces, and a polluted scratch db would
        # silently weaken the fresh-DB guarantee.
        _assert_scratch_empty(conn)
        yield conn
        _drop_scratch_objects(conn, db)


def urlsplit_path_db(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path.lstrip("/")


def _admin_url(url: str) -> str:
    """Same server as the scratch URL, but pointed at the default database."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    return urlunsplit(parts._replace(path="/postgres"))


def _drop_scratch_objects(conn, db: str) -> None:
    """Drop the queue schema objects so the scratch db is empty again.

    Runs only against the scratch database named by ``LH_HARNESS_DB_URL`` --
    never against ``lh_harness`` or any other database.
    """
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_namespace WHERE nspname='harness'")
    if cur.fetchone() is None:
        return
    conn.execute("DROP SCHEMA harness CASCADE")


def _assert_scratch_empty(conn) -> None:
    """Fail loudly if the configured scratch database is not empty."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS harness")
    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FROM pg_class WHERE relnamespace='harness'::regnamespace"
    )
    count = cur.fetchone()[0]
    assert count == 0, (
        f"LH_HARNESS_DB_URL points at a non-empty database "
        f"(harness schema holds {count} objects); refusing to run the "
        f"fresh-DB migration test against it. Point it at an empty scratch "
        f"database instead."
    )


def _object_definitions(conn) -> dict:
    """Snapshot every harness-schema object definition the migrations create."""
    cur = conn.cursor()
    parts = {}
    cur.execute(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname='harness' ORDER BY indexname"
    )
    parts["indexes"] = cur.fetchall()
    cur.execute(
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE connamespace='harness'::regnamespace ORDER BY conname"
    )
    parts["constraints"] = cur.fetchall()
    cur.execute(
        "SELECT table_name, column_name, data_type, column_default, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema='harness' ORDER BY table_name, ordinal_position"
    )
    parts["columns"] = cur.fetchall()
    cur.execute(
        "SELECT e.enumlabel FROM pg_enum e "
        "JOIN pg_type t ON t.oid = e.enumtypid "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE n.nspname='harness' ORDER BY e.enumsortorder"
    )
    parts["enum_labels"] = cur.fetchall()
    cur.execute(
        "SELECT relname FROM pg_class "
        "WHERE relnamespace='harness'::regnamespace ORDER BY relname"
    )
    parts["relations"] = cur.fetchall()
    return parts


def test_fresh_apply_then_reapply_is_noop(scratch_conn) -> None:
    """001 then 002 apply cleanly to an empty DB; re-running changes nothing."""
    conn = scratch_conn
    _assert_scratch_empty(conn)

    _apply_migrations(conn)
    shape_after_first = _object_definitions(conn)

    # Deliverable 2's final shape: enum queue_status, status default
    # 'pending', harness_queue_dedup_key_idx present.
    cur = conn.cursor()
    cur.execute(
        "SELECT t.typtype FROM pg_type t "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE t.typname='queue_status' AND n.nspname='harness'"
    )
    row = cur.fetchone()
    assert row is not None, "type harness.queue_status was not created"
    assert row[0] == "e", f"harness.queue_status is not an enum: {row[0]}"

    cur.execute(
        "SELECT format_type(a.atttypid, a.atttypmod), "
        "pg_get_expr(ad.adbin, ad.adrelid) "
        "FROM pg_attribute a "
        "JOIN pg_class rel ON rel.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = rel.relnamespace "
        "JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum "
        "WHERE n.nspname='harness' AND rel.relname='queue' AND a.attname='status'"
    )
    row = cur.fetchone()
    assert row is not None, "harness.queue.status has no default"
    assert row[0] == "harness.queue_status", f"status not enum-typed: {row[0]}"
    assert row[1] == "'pending'::harness.queue_status", f"bad default: {row[1]}"

    cur.execute("SELECT to_regclass('harness.harness_queue_dedup_key_idx')::text")
    assert cur.fetchone()[0] == "harness.harness_queue_dedup_key_idx", (
        "index harness_queue_dedup_key_idx is missing"
    )

    # 002's objects, so the whole order is proven end to end.
    cur.execute("SELECT to_regclass('harness.harness_queue_events_order_idx')::text")
    assert cur.fetchone()[0] == "harness.harness_queue_events_order_idx"
    cur.execute(
        "SELECT 1 FROM pg_constraint con "
        "JOIN pg_class rel ON rel.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = rel.relnamespace "
        "WHERE con.conname='harness_queue_events_queue_id_fkey' "
        "AND n.nspname='harness'"
    )
    assert cur.fetchone() is not None, "queue_events foreign key is missing"

    # 003's columns (task 233): the continuation fields exist with the right
    # types and non-NULL defaults.
    cur.execute(
        "SELECT data_type, column_default, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema='harness' AND table_name='queue' "
        "AND column_name='branch'"
    )
    row = cur.fetchone()
    assert row is not None, "harness.queue.branch column is missing"
    assert row[0] == "character varying", f"branch wrong type: {row[0]}"
    assert row[1] == "''::character varying", f"branch wrong default: {row[1]}"
    assert row[2] == "NO", "branch must be NOT NULL"

    cur.execute(
        "SELECT data_type, column_default, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema='harness' AND table_name='queue' "
        "AND column_name='continue_branch'"
    )
    row = cur.fetchone()
    assert row is not None, "harness.queue.continue_branch column is missing"
    assert row[0] == "boolean", f"continue_branch wrong type: {row[0]}"
    assert row[1] == "false", f"continue_branch wrong default: {row[1]}"
    assert row[2] == "NO", "continue_branch must be NOT NULL"

    # Re-apply: must succeed and must not change any object definition.
    _apply_migrations(conn)
    shape_after_second = _object_definitions(conn)
    assert shape_after_second == shape_after_first, (
        "re-running the migrations changed object definitions; the files are "
        "not idempotent"
    )


def test_002_reapplies_cleanly_alone(scratch_conn) -> None:
    """002 alone is also idempotent (its FK is the non-guarded statement)."""
    conn = scratch_conn
    before = _object_definitions(conn)
    _apply_migrations(conn)
    assert _object_definitions(conn) == before