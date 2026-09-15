"""Postgres-backed store for the fleet task queue.

``PgQueueStore`` mirrors the file-backed ``QueueStore`` surface one-to-one so
the two stores are interchangeable behind ``QueueStore``.  It is selected by
``queue_backend="postgres"`` plus a ``database_url`` in the project config; the
file store remains the default and stays hermetic -- Postgres is never a hard
dependency of the test suite.

The driver is imported lazily so this module (and therefore ``QueueStore``)
imports cleanly on hosts without ``psycopg``.  A missing driver is handled
exactly like a missing connection: every method raises ``OperationalError``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .queue import QueueEntry, _NON_TERMINAL_STATUS, _VALID_STATUS, _normalize_request

# Column names mirror QueueEntry.to_dict() field-for-field; see docs/queue.md.
_COLUMN_QUEUE_ID = "queue_id"
_COLUMN_NAME = "name"
_COLUMN_TASK = "task"
_COLUMN_WORKSPACE = "workspace"
_COLUMN_MAX_ROUNDS = "max_rounds"
_COLUMN_TRIO = "trio"
_COLUMN_PRIORITY = "priority"
_COLUMN_REQUESTED_BY = "requested_by"
_COLUMN_BASE_CHECK = "base_check"
_COLUMN_STATUS = "status"
_COLUMN_RUN_ID = "run_id"
_COLUMN_REASON = "reason"
_COLUMN_SKIP_REASONS = "skip_reasons"
_COLUMN_CREATED_AT = "created_at"
_COLUMN_UPDATED_AT = "updated_at"
_COLUMN_LAUNCHED_AT = "launched_at"
_COLUMN_LAST_CHECKED_AT = "last_checked_at"
_COLUMN_DEDUP_KEY = "dedup_key"
_QUEUE_COLUMNS = (
    _COLUMN_QUEUE_ID,
    _COLUMN_NAME,
    _COLUMN_TASK,
    _COLUMN_WORKSPACE,
    _COLUMN_MAX_ROUNDS,
    _COLUMN_TRIO,
    _COLUMN_PRIORITY,
    _COLUMN_REQUESTED_BY,
    _COLUMN_BASE_CHECK,
    _COLUMN_STATUS,
    _COLUMN_RUN_ID,
    _COLUMN_REASON,
    _COLUMN_SKIP_REASONS,
    _COLUMN_CREATED_AT,
    _COLUMN_UPDATED_AT,
    _COLUMN_LAUNCHED_AT,
    _COLUMN_LAST_CHECKED_AT,
    _COLUMN_DEDUP_KEY,
)


class OperationalError(RuntimeError):
    """Raised when the Postgres backend is unavailable or the query fails."""


def _pg_connect(database_url: str) -> Any:
    try:
        # Imported lazily so hosts without psycopg still import this module.
        from psycopg import connect
    except ImportError as exc:  # pragma: no cover - driver absent
        raise OperationalError(
            "psycopg is not installed; install it to use the Postgres queue store"
        ) from exc
    url = _resolve_connection_url(database_url)
    try:
        return connect(url)
    except Exception as exc:  # pragma: no cover - host/credential dependent
        raise OperationalError(f"cannot connect to Postgres: {exc}") from exc


def _resolve_connection_url(database_url: str) -> str:
    """Build the connection URL from the config URL and the env-supplied password.

    The URL (host, schema, database) comes from ``database_url`` in config; the
    password comes only from the ``LH_HARNESS_DB_PASSWORD`` environment variable --
    never baked into config, a migration, a fixture, a doc, or a commit.  If the URL
    already carries a password it wins, so this only supplies the credential when
    the URL is missing one.
    """
    password = os.environ.get("LH_HARNESS_DB_PASSWORD") or None
    if not password:
        return database_url
    if "password=" in database_url:
        return database_url
    parsed = urlsplit(database_url)
    if not parsed.username or not parsed.password:
        return f"{database_url}?password={password}"
    # URL already has a user/password; do not clobber it with the env password.
    return database_url


class PgQueueStore:
    """Postgres-backed store for queue entries.

    The public surface matches :class:`QueueStore` exactly:
    ``create`` / ``list`` / ``get`` / ``update`` / ``delete`` /
    ``set_priority`` / ``mark_launched`` / ``mark_done`` / ``mark_failed`` /
    ``record_skip`` / ``counts``.  ``_normalize_request`` and ``QueueEntry``
    validation live in ``queue.py`` so the two stores reject the same bad input.
    """

    def __init__(self, database_url: str) -> None:
        if not isinstance(database_url, str) or not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url
        self._conn = _pg_connect(database_url)
        self._migrate()

    def _txn(self) -> Any:
        return self._conn

    def _migrate(self) -> None:
        """Apply the queue migrations idempotently on first connect.

        Migrations live next to this module under ``..migrations/`` and are
        ordered by filename.  Each file is a schema statement batch that is a
        no-op if the objects already exist.  Runs inside a transaction so a
        partial file never leaves the schema half-applied.
        """
        try:
            with self._txn() as txn:
                for sql in _read_migrations():
                    txn.execute(sql)
                txn.commit()
        except Exception as exc:
            raise OperationalError(f"migration failed: {exc}") from exc


def _read_migrations() -> list[str]:
    """Return the ordered list of migration SQL strings for the queue schema.

    Migrations live under the repository ``migrations/`` directory (``harness``
    schema), not next to this module, so the search is anchored to the repo root
    and falls back to the in-package location for a fresh checkout.  Ordering is
    by filename so ``001_harness_queue`` runs before ``002_harness_queue_events``.
    """
    import importlib.util
    import os

    spec = importlib.util.find_spec("lh_harness.pg_queue")
    if spec is None or spec.origin is None:
        raise OperationalError("could not locate lh_harness.pg_queue for migrations")
    module_dir = os.path.dirname(spec.origin)
    repo_root = os.path.dirname(os.path.dirname(module_dir))
    candidates = (
        os.path.join(repo_root, "migrations"),
        os.path.join(module_dir, "..", "migrations"),
    )
    migrations_dir = next((dir for dir in candidates if os.path.isdir(dir)), None)
    if migrations_dir is None:
        raise OperationalError(
            f"migrations directory not found under the repo root or next to lh_harness.pg_queue"
        )
    entries = sorted(
        name for name in os.listdir(migrations_dir) if name.endswith(".sql")
    )
    sqls: list[str] = []
    for name in entries:
        path = os.path.join(migrations_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                sqls.append(fh.read())
        except OSError as exc:
            raise OperationalError(f"could not read migration {name}: {exc}") from exc
    return sqls

    def _row_to_entry(self, row: tuple[Any, ...]) -> QueueEntry | None:
        """Build a QueueEntry from a fetched row, rejecting anything malformed."""
        values = [self._cast(row[index], column) for index, column in enumerate(_QUEUE_COLUMNS)]
        try:
            entry = QueueEntry.from_dict(dict(zip(_QUEUE_COLUMNS, values)))
        except (TypeError, ValueError):
            return None
        if entry.status not in _VALID_STATUS:
            return None
        return entry

    # -- create ---------------------------------------------------------------

    def create(self, body: dict[str, Any]) -> QueueEntry:
        """Create a queue entry, de-duplicating by ``dedup_key`` when supplied.

        Idempotent-enqueue semantics mirror the file store exactly.
        """
        params = _normalize_request(body)
        dedup_key = params.get("dedup_key")
        now = time.time()
        queue_id = f"q-{uuid.uuid4().hex[:16]}"
        values: list[Any] = [
            queue_id,
            params["name"],
            params["task"],
            params["workspace"],
            params["max_rounds"],
            params["trio"],
            params["priority"],
            params["requested_by"],
            params["base_check"],
            "pending",
            None,
            None,
            json.dumps([]),
            now,
            now,
            None,
            None,
            dedup_key,
        ]
        if dedup_key:
            existing = self._find_non_terminal_by_dedup(dedup_key)
            if existing is not None:
                return existing
        try:
            with self._txn() as txn:
                txn.execute(
                    "INSERT INTO harness.queue (" + ", ".join(_QUEUE_COLUMNS) + ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    values,
                )
                txn.commit()
        except Exception as exc:
            raise OperationalError(f"could not create queue entry: {exc}") from exc
        return QueueEntry.from_dict(
            {
                "queue_id": queue_id,
                "created_at": now,
                "updated_at": now,
                "status": "pending",
                **params,
                "skip_reasons": [],
            }
        )

    def _find_non_terminal_by_dedup(self, dedup_key: str) -> QueueEntry | None:
        """Return the non-terminal entry currently holding ``dedup_key``, if any.

        A non-terminal entry is one whose dedup key stays "in use" -- pending,
        launched, or blocked (see ``_NON_TERMINAL_STATUS`` in queue.py).
        """
        try:
            with self._txn() as txn:
                txn.execute(
                    "SELECT queue_id, status FROM harness.queue "
                    "WHERE dedup_key = %s AND status IN (%s, %s, %s)",
                    (dedup_key, "pending", "launched", "blocked"),
                )
                rows = txn.fetchall()
        except Exception as exc:
            raise OperationalError(f"could not look up dedup entry: {exc}") from exc
        for row in rows:
            if row[1] in _NON_TERMINAL_STATUS:
                return QueueEntry.from_dict(
                    {
                        "queue_id": row[0],
                        "status": row[1],
                        "dedup_key": dedup_key,
                        "skip_reasons": [],
                    }
                )
        return None

    # -- list / get -----------------------------------------------------------

    def list(self) -> list[QueueEntry]:
        entries: list[QueueEntry] = []
        try:
            with self._txn() as txn:
                txn.execute(f"SELECT {_comma_select(_QUEUE_COLUMNS)} FROM harness.queue")
                rows = txn.fetchall()
        except Exception as exc:
            raise OperationalError(f"could not list queue entries: {exc}") from exc
        for row in rows:
            entry = self._row_to_entry(row)
            if entry is not None:
                entries.append(entry)
        entries.sort(key=lambda item: (-item.priority, item.created_at))
        return entries

    def get(self, queue_id: str) -> QueueEntry | None:
        try:
            with self._txn() as txn:
                txn.execute(
                    f"SELECT {_comma_select(_QUEUE_COLUMNS)} FROM harness.queue WHERE queue_id = %s",
                    (queue_id,),
                )
                row = txn.fetchone()
        except Exception as exc:
            raise OperationalError(f"could not read queue entry: {exc}") from exc
        if row is None:
            return None
        return self._row_to_entry(row)

    @staticmethod
    def _cast(value: Any, column: str) -> Any:
        """Coerce a Postgres row value to the type QueueEntry expects.

        ``None`` (SQL NULL) round-trips to the dataclass default: ``None`` for
        the optional string/list columns, and ``0.0`` for the timestamp columns
        so arithmetic never sees a NULL.
        """
        if column == "skip_reasons":
            if value is None:
                return []
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return []
            return parsed if isinstance(parsed, list) else []
        if column in {"max_rounds", "priority", "created_at", "updated_at", "launched_at", "last_checked_at"}:
            return value if value is not None else 0.0
        return value

    def _write(self, entry: QueueEntry) -> None:
        values: list[Any] = [
            entry.queue_id,
            entry.name,
            entry.task,
            entry.workspace,
            entry.max_rounds,
            entry.trio,
            entry.priority,
            entry.requested_by,
            entry.base_check,
            entry.status,
            entry.run_id,
            entry.reason,
            json.dumps(entry.skip_reasons),
            entry.created_at,
            entry.updated_at,
            entry.launched_at,
            entry.last_checked_at,
            entry.dedup_key,
        ]
        try:
            with self._txn() as txn:
                txn.execute(
                    f"UPDATE harness.queue SET " + ", ".join(f"{column} = %s" for column in _QUEUE_COLUMNS)
                    + " WHERE queue_id = %s",
                    values + [entry.queue_id],
                )
                txn.commit()
        except Exception as exc:
            raise OperationalError(f"could not update queue entry: {exc}") from exc

    def update(self, entry: QueueEntry) -> QueueEntry:
        entry.updated_at = time.time()
        self._write(entry)
        return entry

    def delete(self, queue_id: str) -> QueueEntry | None:
        entry = self.get(queue_id)
        if entry is None:
            return None
        try:
            with self._txn() as txn:
                txn.execute("DELETE FROM harness.queue WHERE queue_id = %s", (queue_id,))
                txn.commit()
        except Exception as exc:
            raise OperationalError(f"could not delete queue entry: {exc}") from exc
        return entry

    def set_priority(self, queue_id: str, priority: int) -> QueueEntry | None:
        entry = self.get(queue_id)
        if entry is None:
            return None
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError("priority must be an integer")
        entry.priority = priority
        return self.update(entry)

    def mark_launched(self, queue_id: str, run_id: str) -> QueueEntry | None:
        entry = self.get(queue_id)
        if entry is None:
            return None
        if entry.status != "pending":
            raise ValueError("entry is not pending")
        entry.status = "launched"
        entry.run_id = run_id
        entry.launched_at = time.time()
        return self.update(entry)

    def mark_done(self, queue_id: str, *, reason: str | None = None) -> QueueEntry | None:
        entry = self.get(queue_id)
        if entry is None:
            return None
        entry.status = "done"
        if reason is not None:
            entry.reason = reason[:_MAX_QUEUE_REASON_CHARS]
        return self.update(entry)

    def mark_failed(self, queue_id: str, reason: str) -> QueueEntry | None:
        entry = self.get(queue_id)
        if entry is None:
            return None
        entry.status = "failed"
        entry.reason = reason[:_MAX_QUEUE_REASON_CHARS]
        return self.update(entry)

    def record_skip(self, queue_id: str, reason: str) -> QueueEntry | None:
        entry = self.get(queue_id)
        if entry is None:
            return None
        if entry.status != "pending":
            return None
        entry.skip_reasons.append(str(reason)[:_MAX_QUEUE_REASON_CHARS])
        return self.update(entry)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {status: 0 for status in _VALID_STATUS}
        for entry in self.list():
            counts[entry.status] = counts.get(entry.status, 0) + 1
        return counts


def _comma_select(columns: tuple[str, ...]) -> str:
    return ", ".join(columns)
