"""PostgreSQL-backed queue store for LongHorizon-Harness.

This store implements the same interface as QueueStore but uses a PostgreSQL
database for persistence. It is selected by setting `queue_backend = "postgres"`
and providing a `database_url` in the config.toml file.

The actual database URL is constructed by combining the `database_url` from
config (which should not include the password) with the password from the
environment variable `LH_HARNESS_DB_PASSWORD`.

Example config.toml:
    [queue.backend]
    queue_backend = "postgres"
    database_url = "postgresql://harness_user@localhost:5432/lh_harness"

Then set the environment variable:
    export LH_HARNESS_DB_PASSWORD="the_password"

The store uses the following tables in the `harness` schema:
    harness.queue
    harness.queue_events

However, note that this store only interacts with the `harness.queue` table
to implement the QueueStore interface. The `harness.queue_events` table is
intended for event logging and is not used by this store.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .queue import QueueEntry, _VALID_STATUS, _NON_TERMINAL_STATUS, _now, _validate_task, _validate_name, _validate_workspace, _validate_trio, _validate_max_rounds, _validate_priority, _validate_requested_by, _validate_base_check, _validate_dedup_key, _normalize_request
from .types import DEFAULT_MAX_ROUNDS, MAX_ROUNDS

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.extensions
    _HAS_POSTGRES = True
except ImportError:  # pragma: no cover
    _HAS_POSTGRES = False

# SQL queries for the queue table
# Note: We are using the `harness` schema.
_CREATE_QUEUE_TABLE_IF_NOT_EXISTS = """
    CREATE TABLE IF NOT EXISTS harness.queue (
        queue_id VARCHAR(128) PRIMARY KEY,
        name VARCHAR(256) NOT NULL,
        task TEXT NOT NULL,
        workspace VARCHAR(4096) NOT NULL,
        max_rounds INTEGER NOT NULL,
        trio VARCHAR(10) NOT NULL,
        priority INTEGER NOT NULL,
        requested_by VARCHAR(256) NOT NULL,
        base_check VARCHAR(4000) DEFAULT '',
        status VARCHAR(20) NOT NULL,
        run_id VARCHAR(128),
        reason VARCHAR(4000),
        skip_reasons TEXT[] DEFAULT '{}',
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        launched_at DOUBLE PRECISION,
        last_checked_at DOUBLE PRECISION,
        dedup_key VARCHAR(256),
        CONSTRAINT valid_status CHECK (status IN ('pending', 'launched', 'done', 'failed', 'blocked'))
    );
"""

# We assume the table already exists via migrations, so we don't create it here.
# But we leave the SQL in case we want to run it manually for testing.

_INSERT_QUEUE_ENTRY = """
    INSERT INTO harness.queue (
        queue_id, name, task, workspace, max_rounds, trio, priority,
        requested_by, base_check, status, run_id, reason, skip_reasons,
        created_at, updated_at, launched_at, last_checked_at, dedup_key
    ) VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
    )
"""

_SELECT_QUEUE_ENTRY_BY_ID = """
    SELECT queue_id, name, task, workspace, max_rounds, trio, priority,
           requested_by, base_check, status, run_id, reason, skip_reasons,
           created_at, updated_at, launched_at, last_checked_at, dedup_key
    FROM harness.queue
    WHERE queue_id = %s
"""

_SELECT_QUEUE_ENTRY_BY_DEDUP_KEY = """
    SELECT queue_id, name, task, workspace, max_rounds, trio, priority,
           requested_by, base_check, status, run_id, reason, skip_reasons,
           created_at, updated_at, launched_at, last_checked_at, dedup_key
    FROM harness.queue
    WHERE dedup_key = %s AND status IN ('pending', 'launched')
    ORDER BY created_at ASC
    LIMIT 1
"""

_SELECT_ALL_QUEUE_ENTRIES = """
    SELECT queue_id, name, task, workspace, max_rounds, trio, priority,
           requested_by, base_check, status, run_id, reason, skip_reasons,
           created_at, updated_at, launched_at, last_checked_at, dedup_key
    FROM harness.queue
    ORDER BY priority DESC, created_at ASC
"""

_UPDATE_QUEUE_ENTRY = """
    UPDATE harness.queue SET
        name = %s,
        task = %s,
        workspace = %s,
        max_rounds = %s,
        trio = %s,
        priority = %s,
        requested_by = %s,
        base_check = %s,
        status = %s,
        run_id = %s,
        reason = %s,
        skip_reasons = %s,
        updated_at = %s,
        launched_at = %s,
        last_checked_at = %s,
        dedup_key = %s
    WHERE queue_id = %s
"""

_DELETE_QUEUE_ENTRY_BY_ID = """
    DELETE FROM harness.queue WHERE queue_id = %s
"""

_COUNT_QUEUE_ENTRIES_BY_STATUS = """
    SELECT status, COUNT(*) FROM harness.queue GROUP BY status
"""


def _make_database_url(config_database_url: str) -> str:
    """Construct the actual database URL by injecting the password from the environment.

    Args:
        config_database_url: The URL from config.toml, expected to be of the form
            "postgresql://user@host:port/database" (without password).

    Returns:
        A database URL with the password injected from the LH_HARNESS_DB_PASSWORD
        environment variable.

    Raises:
        ValueError: If the LH_HARNESS_DB_PASSWORD environment variable is not set.
    """
    password = os.environ.get("LH_HARNESS_DB_PASSWORD")
    if not password:
        raise ValueError("LH_HARNESS_DB_PASSWORD environment variable must be set for Postgres queue backend")

    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(config_database_url)
    if not parsed.username:
        raise ValueError("Database URL must include a username")

    # Replace the password part
    netloc = f"{parsed.username}:{password}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"

    # Reconstruct the URL
    return urlunparse((
        parsed.scheme,
        netloc,
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment
    ))


class PgQueueStore:
    """PostgreSQL-backed store for queue entries.

    This class mimics the interface of QueueStore but uses a PostgreSQL
    database for persistence.
    """

    def __init__(self, runs_root: str | Path, database_url: str) -> None:
        if not _HAS_POSTGRES:
            raise ImportError(
                "psycopg2 is not installed. Install it to use the Postgres queue backend."
            )

        self.runs_root = Path(runs_root).expanduser().resolve()
        # Construct the actual database URL with password from environment
        self.database_url = _make_database_url(database_url)

    def _get_connection(self):
        """Get a new database connection.

        Returns:
            A psycopg2 connection object.

        Note: The caller is responsible for closing the connection.
        """
        return psycopg2.connect(self.database_url)

    def _row_to_entry(self, row: tuple) -> QueueEntry:
        """Convert a database row to a QueueEntry instance.

        Args:
            row: A tuple matching the columns selected by _SELECT_QUEUE_ENTRY_BY_ID.

        Returns:
            A QueueEntry instance.
        """
        (
            queue_id, name, task, workspace, max_rounds, trio, priority,
            requested_by, base_check, status, run_id, reason, skip_reasons,
            created_at, updated_at, launched_at, last_checked_at, dedup_key
        ) = row

        # Convert skip_reasons from a list (as returned by psycopg2) to a Python list
        if skip_reasons is None:
            skip_reasons_list = []
        else:
            skip_reasons_list = list(skip_reasons)

        return QueueEntry(
            queue_id=queue_id,
            name=name,
            task=task,
            workspace=workspace,
            max_rounds=max_rounds,
            trio=trio,
            priority=priority,
            requested_by=requested_by,
            base_check=base_check or "",
            status=status,
            run_id=run_id,
            reason=reason,
            skip_reasons=skip_reasons_list,
            created_at=created_at,
            updated_at=updated_at,
            launched_at=launched_at,
            last_checked_at=last_checked_at,
            dedup_key=dedup_key
        )

    def create(self, body: dict[str, Any]) -> QueueEntry:
        """Create a queue entry, de-duplicating by dedup_key when supplied.

        This method mirrors the semantics of QueueStore.create.
        """
        params = _normalize_request(body)
        dedup_key = params.get("dedup_key")
        if dedup_key:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(_SELECT_QUEUE_ENTRY_BY_DEDUP_KEY, (dedup_key,))
                    row = cur.fetchone()
                    if row is not None:
                        return self._row_to_entry(row)

        queue_id = f"q-{uuid.uuid4().hex[:16]}"
        now = _now()
        entry = QueueEntry(queue_id=queue_id, created_at=now, updated_at=now, **params)

        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _INSERT_QUEUE_ENTRY,
                    (
                        entry.queue_id, entry.name, entry.task, entry.workspace,
                        entry.max_rounds, entry.trio, entry.priority,
                        entry.requested_by, entry.base_check, entry.status,
                        entry.run_id, entry.reason, entry.skip_reasons,
                        entry.created_at, entry.updated_at, entry.launched_at,
                        entry.last_checked_at, entry.dedup_key
                    )
                )
            conn.commit()

        return entry

    def _find_non_terminal_by_dedup(self, dedup_key: str) -> Optional[QueueEntry]:
        """Return the non-terminal entry currently holding dedup_key, if any."""
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_SELECT_QUEUE_ENTRY_BY_DEDUP_KEY, (dedup_key,))
                row = cur.fetchone()
                if row is None:
                    return None
                return self._row_to_entry(row)

    def list(self) -> list[QueueEntry]:
        """List all queue entries, sorted by priority (desc) then created_at (asc)."""
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_SELECT_ALL_QUEUE_ENTRIES)
                rows = cur.fetchall()
                return [self._row_to_entry(row) for row in rows]

    def get(self, queue_id: str) -> Optional[QueueEntry]:
        """Get a queue entry by its ID."""
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_SELECT_QUEUE_ENTRY_BY_ID, (queue_id,))
                row = cur.fetchone()
                if row is None:
                    return None
                return self._row_to_entry(row)

    def update(self, entry: QueueEntry) -> QueueEntry:
        """Update a queue entry in the database.

        This method updates the entry's fields and sets updated_at to the current time.
        """
        entry.updated_at = _now()
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _UPDATE_QUEUE_ENTRY,
                    (
                        entry.name, entry.task, entry.workspace, entry.max_rounds,
                        entry.trio, entry.priority, entry.requested_by,
                        entry.base_check, entry.status, entry.run_id,
                        entry.reason, entry.skip_reasons,
                        entry.updated_at, entry.launched_at,
                        entry.last_checked_at, entry.dedup_key,
                        entry.queue_id
                    )
                )
            conn.commit()
        return entry

    def delete(self, queue_id: str) -> Optional[QueueEntry]:
        """Delete a queue entry by its ID.

        Returns the deleted entry if it existed, otherwise None.
        """
        entry = self.get(queue_id)
        if entry is None:
            return None

        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_DELETE_QUEUE_ENTRY_BY_ID, (queue_id,))
            conn.commit()
        return entry

    def set_priority(self, queue_id: str, priority: int) -> Optional[QueueEntry]:
        """Set the priority of a queue entry.

        Returns the updated entry if it existed, otherwise None.
        """
        entry = self.get(queue_id)
        if entry is None:
            return None

        if not isinstance(priority, int):
            raise ValueError("priority must be an integer")

        entry.priority = priority
        return self.update(entry)

    def mark_launched(self, queue_id: str, run_id: str) -> Optional[QueueEntry]:
        """Mark a queue entry as launched.

        Returns the updated entry if it existed and was pending, otherwise None.
        """
        entry = self.get(queue_id)
        if entry is None:
            return None
        if entry.status != "pending":
            raise ValueError("entry is not pending")

        entry.status = "launched"
        entry.run_id = run_id
        entry.launched_at = _now()
        return self.update(entry)

    def mark_done(self, queue_id: str, *, reason: str | None = None) -> Optional[QueueEntry]:
        """Mark a queue entry as done.

        Returns the updated entry if it existed, otherwise None.
        """
        entry = self.get(queue_id)
        if entry is None:
            return None
        entry.status = "done"
        if reason is not None:
            entry.reason = reason[:_MAX_QUEUE_REASON_CHARS]
        return self.update(entry)

    def mark_failed(self, queue_id: str, reason: str) -> Optional[QueueEntry]:
        """Mark a queue entry as failed.

        Returns the updated entry if it existed, otherwise None.
        """
        entry = self.get(queue_id)
        if entry is None:
            return None
        entry.status = "failed"
        entry.reason = reason[:_MAX_QUEUE_REASON_CHARS]
        return self.update(entry)

    def record_skip(self, queue_id: str, reason: str) -> Optional[QueueEntry]:
        """Record a skip reason for a queue entry.

        Returns the updated entry if it existed and was pending, otherwise None.
        """
        entry = self.get(queue_id)
        if entry is None:
            return None
        if entry.status != "pending":
            return None
        entry.skip_reasons.append(str(reason)[:_MAX_QUEUE_REASON_CHARS])
        return self.update(entry)

    def unblock(self, queue_id: str) -> Optional[QueueEntry]:
        """Move a blocked entry back to pending for retry.

        Returns the updated entry if it existed and was blocked, otherwise None.
        """
        entry = self.get(queue_id)
        if entry is None:
            return None
        if entry.status != "blocked":
            raise ValueError("entry is not blocked")
        entry.status = "pending"
        return self.update(entry)

    def counts(self) -> dict[str, int]:
        """Return a dictionary of status counts.

        The keys are the statuses from _VALID_STATUS and the values are the counts.
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_COUNT_QUEUE_ENTRIES_BY_STATUS)
                rows = cur.fetchall()
                counts = {status: 0 for status in _VALID_STATUS}
                for status, count in rows:
                    counts[status] = count
                return counts