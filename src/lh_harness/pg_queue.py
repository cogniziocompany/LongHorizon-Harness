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
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    # Import cycle: lh_harness.queue selects PgQueueStore lazily (see
    # _select_queue_store), so this module must not import from .queue at
    # module level. Runtime uses below import the shared names lazily instead.
    from .queue import QueueEntry

# Column names mirror QueueEntry.to_dict() field-for-field; see docs/queue.md.
_COLUMN_QUEUE_ID = "queue_id"
_COLUMN_NAME = "name"
_COLUMN_TASK = "task"
_COLUMN_WORKSPACE = "workspace"
_COLUMN_MAX_ROUNDS = "max_rounds"
_COLUMN_TRIO = "trio"
_COLUMN_PRIORITY = "priority"
_COLUMN_REQUESTED_BY = "requested_by"
_COLUMN_BRANCH = "branch"
_COLUMN_CONTINUE_BRANCH = "continue_branch"
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
# Retry lineage and provider-quota backoff (migration 005): persisted like the
# file store persists them, so a requeued entry keeps its attempt count (the
# max_retries cap) and its not_before window across a re-read.
_COLUMN_RETRY_OF = "retry_of"
_COLUMN_ATTEMPT = "attempt"
_COLUMN_FAILURE_CAUSE = "failure_cause"
_COLUMN_NOT_BEFORE = "not_before"
_COLUMN_WAIT_REASON = "wait_reason"
# Per-task wall-clock limit (task fc-H2, migration 006).
_COLUMN_TIME_LIMIT_MINUTES = "time_limit_minutes"
# ``requester`` (task 300) still has no column; see ``create``.
_QUEUE_COLUMNS = (
    _COLUMN_QUEUE_ID,
    _COLUMN_NAME,
    _COLUMN_TASK,
    _COLUMN_WORKSPACE,
    _COLUMN_MAX_ROUNDS,
    _COLUMN_TRIO,
    _COLUMN_PRIORITY,
    _COLUMN_REQUESTED_BY,
    _COLUMN_BRANCH,
    _COLUMN_CONTINUE_BRANCH,
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
    _COLUMN_RETRY_OF,
    _COLUMN_ATTEMPT,
    _COLUMN_FAILURE_CAUSE,
    _COLUMN_NOT_BEFORE,
    _COLUMN_WAIT_REASON,
    _COLUMN_TIME_LIMIT_MINUTES,
)

# Same bound the file store applies in ``QueueStore.requeue``.
_MAX_WAIT_REASON_CHARS = 64


def _entry_values(entry: QueueEntry) -> list[Any]:
    """One row's values in ``_QUEUE_COLUMNS`` order (the single source for
    INSERT and UPDATE, so a new column cannot be written by one and not the
    other)."""
    not_before = entry.not_before
    if isinstance(not_before, bool) or not isinstance(not_before, (int, float)):
        not_before = None
    wait_reason = entry.wait_reason
    if not_before is None or not wait_reason:
        wait_reason = None
    else:
        wait_reason = str(wait_reason)[:_MAX_WAIT_REASON_CHARS]
    attempt = entry.attempt
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        attempt = 1
    time_limit = getattr(entry, "time_limit_minutes", None)
    if isinstance(time_limit, bool) or not isinstance(time_limit, int) or time_limit < 1:
        time_limit = None
    return [
        entry.queue_id,
        entry.name,
        entry.task,
        entry.workspace,
        entry.max_rounds,
        entry.trio,
        entry.priority,
        entry.requested_by,
        entry.branch,
        entry.continue_branch,
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
        entry.retry_of,
        attempt,
        entry.failure_cause,
        float(not_before) if not_before is not None else None,
        wait_reason,
        time_limit,
    ]


class _Txn:
    """One unit of work on the store's connection, shaped like the calls the
    store makes (``execute`` / ``fetchone`` / ``fetchall`` / ``commit``).

    psycopg 3 closes a connection at the end of ``with connection:`` and a
    connection has no ``fetchall``, so the store's ``with self._txn() as txn``
    blocks could never run against a real server (every PG test skipped at
    ``counts()``).  This wrapper runs the block on a cursor, commits on a clean
    exit (which also ends a read's implicit transaction, so the connection is
    never left idle in transaction) and rolls back on an exception.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._cur: Any = None

    def __enter__(self) -> "_Txn":
        self._cur = self._conn.cursor()
        return self

    def execute(self, sql: str, params: Any = None) -> "_Txn":
        self._cur.execute(sql, params)
        return self

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cur.fetchall()

    def commit(self) -> None:
        self._conn.commit()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            try:
                self._cur.close()
            except Exception:
                pass
        return False


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

    def _txn(self) -> _Txn:
        return _Txn(self._conn)

    def _migrate(self) -> None:
        """Apply the queue migrations idempotently on first connect.

        Migrations live under the repository ``migrations/`` directory and are
        ordered by filename.  Each file is a schema statement batch that is a
        no-op if the objects already exist.  Runs inside a transaction so a
        partial file never leaves the schema half-applied.
        """
        try:
            with self._txn() as txn:
                for sql in self._read_migrations():
                    txn.execute(sql)
                txn.commit()
        except Exception as exc:
            raise OperationalError(f"migration failed: {exc}") from exc

    @staticmethod
    def _read_migrations() -> list[str]:
        """Return the ordered list of migration SQL strings for the queue schema.

        Migrations live under the repository ``migrations/`` directory (``harness``
        schema), not next to this module, so the search is anchored to the repo root
        and falls back to the in-package location for a fresh checkout.  Ordering is
        by filename so ``001_harness_queue`` runs before ``002_harness_queue_events``.
        """
        import importlib.util

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
                "migrations directory not found under the repo root or next to lh_harness.pg_queue"
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
        from .queue import QueueEntry, _VALID_STATUS  # lazy: see TYPE_CHECKING note

        values = [self._cast(row[index], column) for index, column in enumerate(_QUEUE_COLUMNS)]
        try:
            entry = QueueEntry.from_dict(dict(zip(_QUEUE_COLUMNS, values)))
        except (TypeError, ValueError):
            return None
        if entry.status not in _VALID_STATUS:
            return None
        return entry

    # -- create ---------------------------------------------------------------

    def create(
        self,
        body: dict[str, Any],
        *,
        observed_addr: str | None = None,
        verified_caller: str | None = None,
    ) -> QueueEntry:
        """Create a queue entry, de-duplicating by ``dedup_key`` when supplied.

        Idempotent-enqueue semantics mirror the file store exactly.

        Task 300: the requester block is validated (strict/legacy) and stamped
        exactly like the file store, and returned on the entry.  It is NOT yet
        persisted: ``harness.queue`` has no requester column, so a re-read loads
        ``requester=None``.  Adding the column is a schema migration tracked as
        follow-up work; the file store (CT110's backend) persists it fully.
        """
        from .queue import _normalize_request, _stamp_requester  # lazy: see TYPE_CHECKING note

        params = _normalize_request(body)
        if params.get("requester") is not None:
            params["requester"] = _stamp_requester(
                params["requester"], observed_addr=observed_addr, verified_caller=verified_caller
            )
        dedup_key = params.get("dedup_key")
        now = time.time()
        queue_id = f"q-{uuid.uuid4().hex[:16]}"
        from .queue import QueueEntry  # lazy: see TYPE_CHECKING note

        entry = QueueEntry.from_dict(
            {
                "queue_id": queue_id,
                "created_at": now,
                "updated_at": now,
                "status": "pending",
                **params,
                "skip_reasons": [],
            }
        )
        values = _entry_values(entry)
        if dedup_key:
            existing = self._find_non_terminal_by_dedup(dedup_key)
            if existing is not None:
                return existing
        try:
            with self._txn() as txn:
                txn.execute(
                    "INSERT INTO harness.queue (" + ", ".join(_QUEUE_COLUMNS) + ") VALUES (" + ", ".join(["%s"] * len(_QUEUE_COLUMNS)) + ")",
                    values,
                )
                txn.commit()
        except Exception as exc:
            raise OperationalError(f"could not create queue entry: {exc}") from exc
        return entry

    def _find_non_terminal_by_dedup(self, dedup_key: str) -> QueueEntry | None:
        """Return the non-terminal entry currently holding ``dedup_key``, if any.

        A non-terminal entry is one whose dedup key stays "in use" -- pending,
        launched, or blocked (see ``_NON_TERMINAL_STATUS`` in queue.py).  The
        whole row is read, so the caller gets the same complete entry the file
        store returns (a partial ``QueueEntry`` cannot even be constructed).
        """
        from .queue import _NON_TERMINAL_STATUS  # lazy: see TYPE_CHECKING note

        try:
            with self._txn() as txn:
                txn.execute(
                    f"SELECT {_comma_select(_QUEUE_COLUMNS)} FROM harness.queue "
                    "WHERE dedup_key = %s AND status IN (%s, %s, %s) "
                    "ORDER BY created_at",
                    (dedup_key, "pending", "launched", "blocked"),
                )
                rows = txn.fetchall()
        except Exception as exc:
            raise OperationalError(f"could not look up dedup entry: {exc}") from exc
        for row in rows:
            entry = self._row_to_entry(row)
            if entry is not None and entry.status in _NON_TERMINAL_STATUS:
                return entry
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
            if isinstance(value, list):  # psycopg decodes JSONB itself
                return value
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return []
            return parsed if isinstance(parsed, list) else []
        if column in {"max_rounds", "priority", "created_at", "updated_at", "launched_at", "last_checked_at"}:
            return value if value is not None else 0.0
        if column == "attempt":
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else 1
        return value

    def _write(self, entry: QueueEntry) -> None:
        values = _entry_values(entry)
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
        from .queue import _MAX_QUEUE_REASON_CHARS  # lazy: see TYPE_CHECKING note

        entry = self.get(queue_id)
        if entry is None:
            return None
        entry.status = "done"
        if reason is not None:
            entry.reason = reason[:_MAX_QUEUE_REASON_CHARS]
        return self.update(entry)

    def mark_failed(self, queue_id: str, reason: str) -> QueueEntry | None:
        from .queue import _MAX_QUEUE_REASON_CHARS  # lazy: see TYPE_CHECKING note

        entry = self.get(queue_id)
        if entry is None:
            return None
        entry.status = "failed"
        entry.reason = reason[:_MAX_QUEUE_REASON_CHARS]
        return self.update(entry)

    def record_skip(self, queue_id: str, reason: str) -> QueueEntry | None:
        from .queue import _MAX_QUEUE_REASON_CHARS  # lazy: see TYPE_CHECKING note

        entry = self.get(queue_id)
        if entry is None:
            return None
        if entry.status != "pending":
            return None
        entry.skip_reasons.append(str(reason)[:_MAX_QUEUE_REASON_CHARS])
        return self.update(entry)

    def record_block(self, queue_id: str) -> QueueEntry | None:
        """Transition an entry from pending to blocked.

        Args:
            queue_id: The ID of the entry to block

        Returns:
            The updated QueueEntry, or None if the entry not found
            or if the transition is invalid
        """
        entry = self.get(queue_id)
        if entry is None:
            return None
        from .queue import _valid_transition  # lazy: see TYPE_CHECKING note

        if not _valid_transition(entry.status, "blocked"):
            return None
        entry.status = "blocked"
        return self.update(entry)

    def record_unblock(self, queue_id: str) -> QueueEntry | None:
        """Transition an entry from blocked to pending.

        Args:
            queue_id: The ID of the entry to unblock

        Returns:
            The updated QueueEntry, or None if the entry not found
            or if the transition is invalid
        """
        entry = self.get(queue_id)
        if entry is None:
            return None
        from .queue import _valid_transition  # lazy: see TYPE_CHECKING note

        if not _valid_transition(entry.status, "pending"):
            return None
        entry.status = "pending"
        return self.update(entry)

    def requeue(
        self,
        queue_id: str,
        cause: str,
        *,
        not_before: float | None = None,
        wait_reason: str | None = None,
    ) -> QueueEntry | None:
        """Create a successor pending entry for a failed entry.

        Args:
            queue_id: The ID of the failed entry to retry
            cause: The failure cause that triggered the retry
            not_before: Optional epoch seconds before which the launcher must
                not launch the successor (provider-quota backoff); persisted
                in the ``not_before`` column (migration 005)
            wait_reason: Optional label for the wait (``wait_reason`` column,
                at most 64 characters, kept only together with ``not_before``)

        Returns:
            The new successor QueueEntry, or None if the original entry not found

        Raises:
            ValueError: If the original entry is not failed, or if attempt would exceed max_retries
        """
        entry = self.get(queue_id)
        if entry is None:
            return None

        # Load config to get max_retries. `queue_config_from_config` (and
        # `_flatten_queue_table` in config.py) both normalize [queue.capacity]
        # into a dict that always carries `max_retries`, so the default only
        # applies when the store was built with no config at all.
        max_retries = 2  # default
        # Note: PgQueueStore doesn't have direct access to project config like QueueStore does
        # For now, we'll use the default max_retries=2, matching the file store's default
        # In a full implementation, this would need to be passed in or retrieved from somewhere

        # An entry's own attempt counts toward the cap: original=attempt 1,
        # first retry=attempt 2, ... so the highest allowed attempt is
        # max_retries + 1.  Refuse when the successor would exceed that cap.
        # Checked before the status guard so a retry loop that keeps asking
        # after exhaustion learns it hit the cap, not just that the successor
        # is (still) pending.
        if entry.attempt > max_retries:
            raise ValueError(f"exceeded max_retries ({max_retries})")

        if entry.status != "failed":
            raise ValueError("can only requeue failed entries")

        # Create successor entry
        from .queue import QueueEntry  # lazy: see TYPE_CHECKING note

        successor = QueueEntry(
            queue_id=f"q-{uuid.uuid4().hex[:16]}",
            name=entry.name,
            task=entry.task,
            workspace=entry.workspace,
            max_rounds=entry.max_rounds,
            trio=entry.trio,
            priority=entry.priority,
            requested_by=entry.requested_by,
            # Like the file store (task 233): a retried continuation task
            # stays a continuation task.
            branch=entry.branch,
            continue_branch=entry.continue_branch,
            base_check=entry.base_check,
            status="pending",
            retry_of=entry.queue_id,
            attempt=entry.attempt + 1,
            failure_cause=cause,
            created_at=time.time(),
            updated_at=time.time(),
            dedup_key=None,  # retries must not collide with original dedup_key
            time_limit_minutes=entry.time_limit_minutes,
            not_before=float(not_before) if not_before is not None else None,
            wait_reason=(
                str(wait_reason)[:_MAX_WAIT_REASON_CHARS]
                if (not_before is not None and wait_reason)
                else None
            ),
        )

        # Insert the successor entry and an audit event
        try:
            with self._txn() as txn:
                values = _entry_values(successor)
                txn.execute(
                    "INSERT INTO harness.queue (" + ", ".join(_QUEUE_COLUMNS) + ") VALUES (" + ", ".join(["%s"] * len(_QUEUE_COLUMNS)) + ")",
                    values,
                )
                # Insert queue_events row for the enqueue event
                try:
                    host = socket.gethostname()
                except Exception:
                    host = ''
                # Prepare payload: successor entry data without queue_id and created_at (to avoid duplication with columns)
                succ_data = successor.to_dict()
                succ_data.pop('queue_id', None)
                succ_data.pop('created_at', None)
                payload_json = json.dumps(succ_data)
                txn.execute(
                    "INSERT INTO harness.queue_events (host, queue_id, ts, event, actor, rationale, payload) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (host, successor.queue_id, successor.created_at, "enqueue", "", cause, payload_json),
                )
                txn.commit()
        except Exception as exc:
            raise OperationalError(f"could not create queue entry: {exc}") from exc

        return successor

    def counts(self) -> dict[str, int]:
        from .queue import _VALID_STATUS  # lazy: see TYPE_CHECKING note

        counts: dict[str, int] = {status: 0 for status in _VALID_STATUS}
        for entry in self.list():
            counts[entry.status] = counts.get(entry.status, 0) + 1
        return counts


def _comma_select(columns: tuple[str, ...]) -> str:
    return ", ".join(columns)
