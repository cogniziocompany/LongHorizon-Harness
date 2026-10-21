"""Single-family writer lease enforced at the harness effect boundary.

The three paired-runtime families (overseer / infraops / devspecops) share
one lease per family.  Within a family exactly one runtime may cause
mutating effects through the harness at a time.  A quota-exhausted or
stalled holder hands the role to its pair by ``takeover``.  See
``docs/effect-lease.md`` for the model.

This module is the storage + CAS surface, not the wire-level gate.  The
gate is wired in ``webapi/server.py`` for REST and ``mcp_tools.dispatch``
for MCP.  The lease layer is OFF by default; the
``LH_HARNESS_EFFECT_LEASE_ENABLED`` setting must be true for any
mutation to require a lease.

Lease state lives in a small SQLite file the harness creates next to the
settings DB (``<settings.db>.lease.db``).  Every CAS is one
``UPDATE ... WHERE epoch = :observed`` so the race semantics are exactly
the SQL guarantees.

The module is deliberately hermetic: it knows nothing about FastAPI, MCP
JSON-RPC, or HTTP.  Tests inject a path; the wire layer injects a
``LeaseGate`` per app.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Mapping

logger = logging.getLogger(__name__)

# The three family names Paxton defined.  Anything else is rejected.
FAMILIES: tuple[str, ...] = ("overseer", "infraops", "devspecops")
_FAMILY_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

# Default TTL for acquire / renew / takeover.  The pair launches after 2*TTL
# without a renew.
DEFAULT_TTL_SECONDS = 60.0
# Action-id format: a v4 UUID (hex + dashes).  Tight enough to keep client
# bugs out, loose enough to accept any RFC-4122 v4 producer.
_ACTION_ID_RE = re.compile(r"^[0-9a-fA-F-]{32,64}$")
_REASON_MAX = 128

LEASE_DB_SUFFIX = ".lease.db"

ENABLED_ENV = "LH_HARNESS_EFFECT_LEASE_ENABLED"


# ---------------------------------------------------------------- exceptions --


class LeaseError(Exception):
    """Base for the lease layer.  ``code`` is the HTTP-ish status the wire
    layer translates into a response.  ``reason`` is the public tag
    (``stale_epoch``, ``not_holder``, ``unknown_family``, etc.)."""

    code: int = 409

    def __init__(self, reason: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.detail = detail


class UnknownFamily(LeaseError):
    code = 422

    def __init__(self, family: str) -> None:
        super().__init__(
            "unknown_family",
            f"family {family!r} is not one of {sorted(FAMILIES)}",
            family=family,
        )


class MalformedActionId(LeaseError):
    code = 422

    def __init__(self, action_id: Any) -> None:
        super().__init__(
            "malformed_action_id",
            f"action_id must match {_ACTION_ID_RE.pattern}",
            action_id=str(action_id)[:64],
        )


class NotHolder(LeaseError):
    code = 409

    def __init__(self, family: str, expected_holder: str, observed_holder: str) -> None:
        super().__init__(
            "not_holder",
            f"caller {expected_holder!r} is not the holder of family {family!r} "
            f"(observed holder: {observed_holder!r})",
            family=family,
            expected_holder=expected_holder,
            observed_holder=observed_holder,
        )


class StaleEpoch(LeaseError):
    code = 409

    def __init__(self, family: str, observed_epoch: int, current_epoch: int) -> None:
        super().__init__(
            "stale_epoch",
            f"caller presented epoch {observed_epoch} for family {family!r} "
            f"but the current epoch is {current_epoch}",
            family=family,
            observed_epoch=observed_epoch,
            current_epoch=current_epoch,
        )


class ExpiredLease(LeaseError):
    code = 409

    def __init__(self, family: str, observed_epoch: int, expires_at: float) -> None:
        super().__init__(
            "expired_lease",
            f"lease for family {family!r} expired",
            family=family,
            observed_epoch=observed_epoch,
            expires_at=expires_at,
        )


class MissingLease(LeaseError):
    code = 409

    def __init__(self, family: str) -> None:
        super().__init__(
            "missing_lease",
            f"no lease is held for family {family!r}; the holder must acquire first",
            family=family,
        )


class LeaseTaken(LeaseError):
    """An acquire found the slot held by someone who is not the caller."""

    code = 409

    def __init__(self, family: str, observed_holder: str, observed_epoch: int) -> None:
        super().__init__(
            "lease_taken",
            f"family {family!r} is held by {observed_holder!r} (epoch {observed_epoch})",
            family=family,
            observed_holder=observed_holder,
            observed_epoch=observed_epoch,
        )


class PairOnly(LeaseError):
    """A takeover from outside the holder/pair relationship."""

    code = 409

    def __init__(self, family: str, caller: str, holder: str, reason: str) -> None:
        super().__init__(
            "pair_only",
            f"only the pair of {holder!r} may take over family {family!r} for {reason!r}",
            family=family,
            caller=caller,
            holder=holder,
            reason=reason,
        )


class EvidenceRequired(LeaseError):
    code = 409

    def __init__(self, family: str, reason: str) -> None:
        super().__init__(
            "evidence_required",
            f"takeover of family {family!r} for reason {reason!r} requires evidence",
            family=family,
            reason=reason,
        )


# ----------------------------------------------------------------- the record --


@dataclass(frozen=True)
class LeaseRecord:
    family: str
    holder: str
    epoch: int
    acquired_at: float
    expires_at: float
    reason: str

    def public_dict(self) -> dict[str, object]:
        return {
            "family": self.family,
            "holder": self.holder,
            "epoch": self.epoch,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
            "reason": self.reason,
        }


# ---------------------------------------------------------------- the store --


class LeaseStore:
    """SQLite-backed lease storage.  One row per family.

    The CAS contract on every mutator: ``UPDATE family SET ... WHERE
    family = :family AND epoch = :observed``.  Concurrent CAS attempts
    race on the same row; SQLite serializes writers, the second one
    sees ``rowcount == 0`` and fails.  There is no application-level
    lock.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        parent = os.path.dirname(self.path) or "."
        os.makedirs(parent, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(parent, 0o700)
        new = not os.path.exists(self.path)
        if new:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS lease (
                    family TEXT PRIMARY KEY,
                    holder TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    acquired_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lease_action (
                    family TEXT NOT NULL,
                    caller TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    accepted_at REAL NOT NULL,
                    receipt_json TEXT NOT NULL,
                    PRIMARY KEY (family, caller, action_id)
                    );
                CREATE INDEX IF NOT EXISTS idx_lease_action_accepted
                    ON lease_action (accepted_at);
                """
            )

    # ------------------------------------------------------ read helpers --
    def get(self, family: str, *, now: float | None = None) -> LeaseRecord | None:
        if family not in FAMILIES:
            raise UnknownFamily(family)
        cur = time.time() if now is None else now
        with self._conn() as conn:
            row = conn.execute(
                "SELECT family, holder, epoch, acquired_at, expires_at, reason "
                "FROM lease WHERE family = ?",
                (family,),
            ).fetchone()
        if row is None:
            return None
        return LeaseRecord(
            family=row["family"],
            holder=row["holder"],
            epoch=row["epoch"],
            acquired_at=row["acquired_at"],
            expires_at=row["expires_at"],
            reason=row["reason"],
        )

    # ------------------------------------------------------ write helpers --
    def acquire(
        self,
        family: str,
        holder: str,
        *,
        reason: str,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        now: float | None = None,
    ) -> LeaseRecord:
        if family not in FAMILIES:
            raise UnknownFamily(family)
        _validate_holder(holder)
        _validate_reason(reason)
        cur = time.time() if now is None else now
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT family, holder, epoch, expires_at FROM lease WHERE family = ?",
                    (family,),
                ).fetchone()
                if row is None:
                    new_epoch = 1
                    conn.execute(
                        "INSERT INTO lease(family, holder, epoch, acquired_at, expires_at, reason) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (family, holder, new_epoch, cur, cur + ttl_seconds, reason),
                    )
                    return LeaseRecord(
                        family=family,
                        holder=holder,
                        epoch=new_epoch,
                        acquired_at=cur,
                        expires_at=cur + ttl_seconds,
                        reason=reason,
                    )
                if row["expires_at"] > cur:
                    raise LeaseTaken(
                        family=family,
                        observed_holder=row["holder"],
                        observed_epoch=row["epoch"],
                    )
                # Expired slot: bump the epoch and take it.
                new_epoch = row["epoch"] + 1
                conn.execute(
                    "UPDATE lease SET holder = ?, epoch = ?, acquired_at = ?, "
                    "expires_at = ?, reason = ? "
                    "WHERE family = ? AND epoch = ?",
                    (holder, new_epoch, cur, cur + ttl_seconds, reason, family, row["epoch"]),
                )
            finally:
                conn.execute("COMMIT")
        return LeaseRecord(
            family=family,
            holder=holder,
            epoch=new_epoch,
            acquired_at=cur,
            expires_at=cur + ttl_seconds,
            reason=reason,
        )

    def renew(
        self,
        family: str,
        holder: str,
        *,
        observed_epoch: int,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        now: float | None = None,
    ) -> LeaseRecord:
        if family not in FAMILIES:
            raise UnknownFamily(family)
        _validate_holder(holder)
        _validate_epoch(observed_epoch)
        cur = time.time() if now is None else now
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT family, holder, epoch, expires_at FROM lease WHERE family = ?",
                    (family,),
                ).fetchone()
                if row is None:
                    raise MissingLease(family)
                if row["holder"] != holder:
                    raise NotHolder(
                        family=family,
                        expected_holder=holder,
                        observed_holder=row["holder"],
                    )
                if row["epoch"] != observed_epoch:
                    raise StaleEpoch(
                        family=family,
                        observed_epoch=observed_epoch,
                        current_epoch=row["epoch"],
                    )
                if row["expires_at"] <= cur:
                    raise ExpiredLease(
                        family=family,
                        observed_epoch=observed_epoch,
                        expires_at=row["expires_at"],
                    )
                new_expires = cur + ttl_seconds
                conn.execute(
                    "UPDATE lease SET acquired_at = ?, expires_at = ?, reason = 'renew' "
                    "WHERE family = ? AND epoch = ?",
                    (cur, new_expires, family, observed_epoch),
                )
            finally:
                conn.execute("COMMIT")
        return LeaseRecord(
            family=family,
            holder=holder,
            epoch=observed_epoch,
            acquired_at=cur,
            expires_at=new_expires,
            reason="renew",
        )

    def release(
        self,
        family: str,
        holder: str,
        *,
        observed_epoch: int,
        now: float | None = None,
    ) -> None:
        if family not in FAMILIES:
            raise UnknownFamily(family)
        _validate_holder(holder)
        _validate_epoch(observed_epoch)
        cur = time.time() if now is None else now
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT family, holder, epoch, expires_at FROM lease WHERE family = ?",
                    (family,),
                ).fetchone()
                if row is None:
                    raise MissingLease(family)
                if row["holder"] != holder:
                    raise NotHolder(
                        family=family,
                        expected_holder=holder,
                        observed_holder=row["holder"],
                    )
                if row["epoch"] != observed_epoch:
                    raise StaleEpoch(
                        family=family,
                        observed_epoch=observed_epoch,
                        current_epoch=row["epoch"],
                    )
                conn.execute("DELETE FROM lease WHERE family = ? AND epoch = ?", (family, observed_epoch))
            finally:
                conn.execute("COMMIT")

    def takeover(
        self,
        family: str,
        new_holder: str,
        *,
        observed_epoch: int,
        reason: str,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        now: float | None = None,
    ) -> LeaseRecord:
        """Take the lease for the pair runtime.

        ``observed_epoch`` MUST equal the holder's current epoch at the
        moment the takeover started.  Concurrent takeover attempts race
        on the same row and the second one fails with ``StaleEpoch`` --
        exactly one wins.

        The pair is determined by ``[callers].<name>.pair``; the gate
        calls this only after that policy is checked.  ``reason`` MUST
        be one of ``expired``, ``quota``, ``timeout``, ``operator``.
        Evidence is supplied as the boolean ``quota_evidence`` /
        ``timeout_evidence`` kwargs and the gate decides whether the
        threshold is met.
        """
        if family not in FAMILIES:
            raise UnknownFamily(family)
        _validate_holder(new_holder)
        _validate_reason(reason)
        _validate_epoch(observed_epoch)
        cur = time.time() if now is None else now
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT family, holder, epoch, expires_at FROM lease WHERE family = ?",
                    (family,),
                ).fetchone()
                if row is None:
                    raise MissingLease(family)
                if row["epoch"] != observed_epoch:
                    raise StaleEpoch(
                        family=family,
                        observed_epoch=observed_epoch,
                        current_epoch=row["epoch"],
                    )
                new_epoch = observed_epoch + 1
                cur_row = conn.execute(
                    "UPDATE lease SET holder = ?, epoch = ?, acquired_at = ?, "
                    "expires_at = ?, reason = ? "
                    "WHERE family = ? AND epoch = ?",
                    (
                        new_holder,
                        new_epoch,
                        cur,
                        cur + ttl_seconds,
                        reason,
                        family,
                        observed_epoch,
                    ),
                )
                if cur_row.rowcount != 1:
                    raise StaleEpoch(
                        family=family,
                        observed_epoch=observed_epoch,
                        current_epoch=row["epoch"],
                    )
            finally:
                conn.execute("COMMIT")
        return LeaseRecord(
            family=family,
            holder=new_holder,
            epoch=new_epoch,
            acquired_at=cur,
            expires_at=cur + ttl_seconds,
            reason=reason,
        )

    # ----------------------------------------------------- idempotency --
    def record_action(
        self,
        *,
        family: str,
        caller: str,
        action_id: str,
        receipt: dict[str, object],
        now: float | None = None,
    ) -> dict[str, object]:
        """Record one accepted action; return the original receipt if
        ``(family, caller, action_id)`` was previously recorded.
        """
        if family not in FAMILIES:
            raise UnknownFamily(family)
        if not _ACTION_ID_RE.match(action_id or ""):
            raise MalformedActionId(action_id)
        cur = time.time() if now is None else now
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT receipt_json FROM lease_action "
                "WHERE family = ? AND caller = ? AND action_id = ?",
                (family, caller, action_id),
            ).fetchone()
            if existing is not None:
                try:
                    return json.loads(existing["receipt_json"])
                except (TypeError, ValueError):
                    return receipt
            conn.execute(
                "INSERT INTO lease_action(family, caller, action_id, accepted_at, receipt_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (family, caller, action_id, cur, json.dumps(receipt, sort_keys=True)),
            )
        return receipt

    def look_up_action(
        self,
        *,
        family: str,
        caller: str,
        action_id: str,
    ) -> dict[str, object] | None:
        """Return the previously recorded receipt, or None."""
        if family not in FAMILIES:
            raise UnknownFamily(family)
        if not _ACTION_ID_RE.match(action_id or ""):
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT receipt_json FROM lease_action "
                "WHERE family = ? AND caller = ? AND action_id = ?",
                (family, caller, action_id),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["receipt_json"])
        except (TypeError, ValueError):
            return None


# ----------------------------------------------------------------- the gate --


@dataclass(frozen=True)
class CheckResult:
    """Result of a gate check on a mutating call."""

    accept: bool
    family: str
    holder: str
    epoch: int
    action_id: str
    receipt: dict[str, object] | None = None
    reason: str | None = None
    http_status: int = 200
    deny_reason: str | None = None


class LeaseGate:
    """The wire-level gate.  Holds a ``LeaseStore`` and the runtime-id
    ↔ family ↔ pair mapping.  Stateless apart from the store."""

    def __init__(
        self,
        store: LeaseStore,
        *,
        family_map: Mapping[str, str] | None = None,
        pair_map: Mapping[str, str] | None = None,
    ) -> None:
        self.store = store
        self._family_map: dict[str, str] = dict(family_map or {})
        self._pair_map: dict[str, str] = dict(pair_map or {})
        self._lock = threading.Lock()

    # ------------------------------------------------------------- mapping --
    def declare_caller(
        self,
        caller: str,
        *,
        family: str,
        pair: str | None = None,
    ) -> None:
        """Bind a runtime id to its family (and optionally its pair)."""
        if family not in FAMILIES:
            raise UnknownFamily(family)
        if not _CALLER_RE.match(caller):
            raise ValueError(f"caller {caller!r} must match {_CALLER_RE.pattern}")
        with self._lock:
            self._family_map[caller] = family
            if pair is not None:
                if not _CALLER_RE.match(pair):
                    raise ValueError(f"pair {pair!r} must match {_CALLER_RE.pattern}")
                self._pair_map[caller] = pair

    def family_for(self, caller: str) -> str | None:
        return self._family_map.get(caller)

    def pair_for(self, caller: str) -> str | None:
        return self._pair_map.get(caller)

    # ----------------------------------------------------------- check --
    def check(
        self,
        *,
        caller: str,
        family: str,
        observed_epoch: int | None,
        action_id: str | None,
        now: float | None = None,
    ) -> CheckResult:
        """Check whether ``caller`` may cause an effect in ``family``.

        ``observed_epoch`` and ``action_id`` may be None (legacy callers
        that have not yet been upgraded).  When they are None and the
        caller's family does not match ``family``, the call is refused
        with ``stale_epoch`` (``epoch=0``).  When ``action_id`` is
        missing we mint a fresh UUID v4 so the call is still recorded.
        """
        cur = time.time() if now is None else now
        if family not in FAMILIES:
            raise UnknownFamily(family)
        if observed_epoch is not None and not _validate_epoch_value(observed_epoch):
            raise StaleEpoch(
                family=family,
                observed_epoch=observed_epoch,
                current_epoch=0,
            )
        record = self.store.get(family)
        if record is None:
            raise MissingLease(family)
        if record.holder != caller:
            raise NotHolder(
                family=family,
                expected_holder=caller,
                observed_holder=record.holder,
            )
        if observed_epoch is None or observed_epoch != record.epoch:
            raise StaleEpoch(
                family=family,
                observed_epoch=observed_epoch or 0,
                current_epoch=record.epoch,
            )
        if record.expires_at <= cur:
            raise ExpiredLease(
                family=family,
                observed_epoch=record.epoch,
                expires_at=record.expires_at,
            )
        receipt_id = action_id or str(uuid.uuid4())
        if action_id and not _ACTION_ID_RE.match(action_id):
            raise MalformedActionId(action_id)
        receipt = {
            "ok": True,
            "action_id": receipt_id,
            "family": family,
            "holder": record.holder,
            "epoch": record.epoch,
            "accepted_at": cur,
        }
        receipt = self.store.record_action(
            family=family,
            caller=caller,
            action_id=receipt_id,
            receipt=receipt,
            now=cur,
        )
        return CheckResult(
            accept=True,
            family=family,
            holder=record.holder,
            epoch=record.epoch,
            action_id=receipt_id,
            receipt=receipt,
        )

    def replay(
        self,
        *,
        caller: str,
        family: str,
        action_id: str,
    ) -> dict[str, object] | None:
        """Return the original receipt for a known action id, or None."""
        return self.store.look_up_action(family=family, caller=caller, action_id=action_id)


# --------------------------------------------------------------------- env --


def is_enabled() -> bool:
    """The single switch consulted by every gate path."""
    return os.environ.get(ENABLED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


# ------------------------------------------------------------------ helpers --


_CALLER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def _validate_holder(holder: str) -> None:
    if not isinstance(holder, str) or not _CALLER_RE.fullmatch(holder):
        raise ValueError(f"holder {holder!r} must match {_CALLER_RE.pattern}")


def _validate_epoch(epoch: int) -> None:
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
        raise ValueError(f"epoch {epoch!r} must be a positive integer")


def _validate_epoch_value(epoch: Any) -> bool:
    return isinstance(epoch, int) and not isinstance(epoch, bool) and epoch >= 1


def _validate_reason(reason: str) -> None:
    if not isinstance(reason, str) or not reason or len(reason) > _REASON_MAX or "\n" in reason:
        raise ValueError(f"reason must be 1..{_REASON_MAX} characters on one line")


__all__ = [
    "FAMILIES",
    "DEFAULT_TTL_SECONDS",
    "LEASE_DB_SUFFIX",
    "ENABLED_ENV",
    "LeaseError",
    "UnknownFamily",
    "MalformedActionId",
    "NotHolder",
    "StaleEpoch",
    "ExpiredLease",
    "MissingLease",
    "LeaseTaken",
    "PairOnly",
    "EvidenceRequired",
    "LeaseRecord",
    "LeaseStore",
    "LeaseGate",
    "CheckResult",
    "is_enabled",
]