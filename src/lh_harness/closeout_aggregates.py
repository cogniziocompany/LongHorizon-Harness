"""Read-only aggregates over the close-out outcome vocabulary.

Why this exists
---------------
An RSI (recursive self-improvement) loop that tunes on "how many runs
delivered?" needs counts it can trust, split by outcome, by node and by
day, and split again by whether the value was OBSERVED or DERIVED.  Without
that, a loop re-derives the semantics itself and two loops disagree.

This module provides those counts as a PURE FUNCTION over close-out
records read from the run layer.  It is read-only: it opens no socket,
starts no service, and writes nothing.  It is not a new service -- it is a
projection over the same durable ``closeout.json`` records the run layer
already writes next to ``report.json``.

The vocabulary comes from :mod:`lh_harness.closeout`, which is the SINGLE
source of truth: ``CLOSE_OUT_OUTCOMES`` defines the six values and
``DELIVERED_OUTCOMES`` defines which of them count as a delivery.  Nothing
here re-derives either; an outcome that is not in the canonical enum is
never counted, so a second vocabulary cannot appear in an aggregate.

Observed vs derived
-------------------
A close-out is OBSERVED when a human or the agent submitted the structured
form.  It is DERIVED when the value was reconstructed from a legacy
free-text ``done:`` rationale (``derived=true`` /
``derived_from="rationale_parse"``).  A measurement layer must be able to
tell the two apart, because a derived value is a migration hint about the
past, not an observation of it.
"""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .closeout import (
    CLOSE_OUT_OUTCOMES,
    DELIVERED_OUTCOMES,
    CloseOutRecord,
    read_close_out,
)

#: The canonical outcome order used by every aggregate, so two consumers
#: reading the same aggregate see the same key order.
OUTCOME_ORDER: tuple[str, ...] = CLOSE_OUT_OUTCOMES

#: The environment variable naming this node in the fleet.
NODE_ENV_VAR = "LH_HARNESS_FLEET_NODE"

#: The key under which the observed/derived split is reported.
DERIVED_KEY = "derived"


def current_node() -> str:
    """The node this run layer belongs to.

    The fleet's node name comes from ``LH_HARNESS_FLEET_NODE``; without it
    the harness falls back to the local hostname, exactly like the fleet
    reporter does.  A run's node is therefore a property of the NODE's run
    layer, not of the run record -- the run record does not carry it.
    """

    return os.environ.get(NODE_ENV_VAR) or socket.gethostname()


def _day_key(timestamp: float) -> str:
    """The UTC day (``YYYY-MM-DD``) a close-out timestamp falls in."""

    return time.strftime("%Y-%m-%d", time.gmtime(float(timestamp)))


def empty_counts() -> dict[str, int]:
    """A zeroed count row: every canonical outcome, plus ``total``."""

    counts: dict[str, int] = {"total": 0}
    for outcome in OUTCOME_ORDER:
        counts[outcome] = 0
    return counts


@dataclass(frozen=True)
class CloseOutAggregate:
    """Counts of close-outs, split by outcome and by observed/derived.

    ``counts`` holds the totals (``total`` plus one key per canonical
    outcome).  ``observed`` and ``derived`` hold the same shape, so a
    consumer can always check ``observed[outcome] + derived[outcome] ==
    counts[outcome]``.
    """

    counts: dict[str, int] = field(default_factory=empty_counts)
    observed: dict[str, int] = field(default_factory=empty_counts)
    derived: dict[str, int] = field(default_factory=empty_counts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.counts.get("total", 0),
            "counts": dict(self.counts),
            "observed": dict(self.observed),
            "derived": dict(self.derived),
        }


class OutcomeCounter:
    """Accumulate close-out records into an :class:`CloseOutAggregate`.

    The counter is deliberately dumb: it counts what it is given.  The
    vocabulary comes from the caller, which reads it from the durable
    records -- so a record carrying an outcome outside the canonical enum
    is refused rather than silently counted as a synonym.
    """

    def __init__(self) -> None:
        self._counts = empty_counts()
        self._observed = empty_counts()
        self._derived = empty_counts()

    def add(self, record: CloseOutRecord) -> bool:
        """Count one record.  Returns False when its outcome is unknown.

        An unknown outcome means the record came from a future vocabulary
        (or a hand-edited file).  It is NOT counted, so the aggregate can
        never grow a seventh vocabulary.
        """

        outcome = record.outcome
        if outcome not in self._counts:
            return False
        self._counts["total"] += 1
        self._counts[outcome] += 1
        bucket = self._derived if record.derived else self._observed
        bucket["total"] += 1
        bucket[outcome] += 1
        return True

    def aggregate(self) -> CloseOutAggregate:
        return CloseOutAggregate(
            counts=dict(self._counts),
            observed=dict(self._observed),
            derived=dict(self._derived),
        )


def _bucket(
    buckets: dict[str, dict[str, dict[str, int]]],
    key: str,
    record: CloseOutRecord,
) -> None:
    """Count one record into one bucket row, keeping the observed/derived split.

    Every bucket row has the SAME shape as the ``overall`` aggregate: a
    ``counts`` row, an ``observed`` row and a ``derived`` row.  A consumer
    therefore never has to ask whether a given split carries the
    observed/derived distinction -- it is always there, and
    ``observed[outcome] + derived[outcome] == counts[outcome]`` holds for
    every bucket, not just for the flat matrix.
    """

    row = buckets.get(key)
    if row is None:
        row = {
            "counts": empty_counts(),
            "observed": empty_counts(),
            "derived": empty_counts(),
        }
        buckets[key] = row
    outcome = record.outcome
    if outcome not in row["counts"]:
        return
    row["counts"]["total"] += 1
    row["counts"][outcome] += 1
    bucket = row["derived"] if record.derived else row["observed"]
    bucket["total"] += 1
    bucket[outcome] += 1


def aggregate_records(
    records: Iterable[CloseOutRecord],
    *,
    node: str | None = None,
) -> dict[str, Any]:
    """Aggregate close-out records into the measurement matrix.

    Returns a dictionary with:

    ``overall``       the aggregate over every record
    ``by_outcome``    one aggregate per canonical outcome (each row counts
                      the records carrying that outcome; ``total`` is the
                      number of records with that outcome)
    ``by_node``       one aggregate per node
    ``by_day``        one aggregate per UTC day (``YYYY-MM-DD``)

    EVERY row -- ``overall`` and each per-outcome / per-node / per-day row
    -- carries the SAME shape: a ``counts`` row, an ``observed`` row and a
    ``derived`` row.  The observed/derived split is therefore never missing
    from a per-node or per-day view, which is exactly where a measurement
    layer reads a fleet's history.

    The per-node split uses the node the run layer belongs to.  The
    per-day split uses the close-out's ``closed_at`` timestamp, in UTC.

    An empty input yields a well-formed, all-zero matrix rather than an
    error: a measurement layer that asks an empty fleet for counts must get
    zeros, not a crash.
    """

    effective_node = node if node is not None else current_node()
    overall = OutcomeCounter()
    by_outcome: dict[str, dict[str, dict[str, int]]] = {}
    by_node: dict[str, dict[str, dict[str, int]]] = {}
    by_day: dict[str, dict[str, dict[str, int]]] = {}

    for record in records:
        if not overall.add(record):
            continue
        outcome = record.outcome
        outcome_row = by_outcome.get(outcome)
        if outcome_row is None:
            outcome_row = {
                "counts": empty_counts(),
                "observed": empty_counts(),
                "derived": empty_counts(),
            }
            by_outcome[outcome] = outcome_row
        outcome_row["counts"]["total"] += 1
        outcome_row["counts"][outcome] += 1
        outcome_bucket = (
            outcome_row["derived"] if record.derived else outcome_row["observed"]
        )
        outcome_bucket["total"] += 1
        outcome_bucket[outcome] += 1

        _bucket(by_node, effective_node, record)
        _bucket(by_day, _day_key(record.closed_at), record)

    return {
        "node": effective_node,
        "overall": overall.aggregate().to_dict(),
        "by_outcome": {key: _bucket_row(value) for key, value in sorted(by_outcome.items())},
        "by_node": {key: _bucket_row(value) for key, value in sorted(by_node.items())},
        "by_day": {key: _bucket_row(value) for key, value in sorted(by_day.items())},
    }


def _bucket_row(row: dict[str, dict[str, int]]) -> dict[str, Any]:
    """Render one bucket row in the ``overall`` aggregate's shape."""

    counts = dict(row["counts"])
    return {
        "total": counts.get("total", 0),
        "counts": counts,
        "observed": dict(row["observed"]),
        "derived": dict(row["derived"]),
    }


def _close_out_dirs(runs_root: Path) -> list[Path]:
    """The log directories that may hold a ``closeout.json``."""

    dirs: list[Path] = []
    try:
        entries = sorted(runs_root.iterdir())
    except OSError:
        return dirs
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        for name in ("lh_harness", "logs", "role_orchestration"):
            candidate = entry / name
            try:
                if candidate.is_dir():
                    dirs.append(candidate)
                    break
            except OSError:
                continue
    return dirs


def aggregate_runs_root(
    runs_root: str | Path,
    *,
    node: str | None = None,
) -> dict[str, Any]:
    """Aggregate the close-outs stored under a runs root.

    Reads the durable ``closeout.json`` next to each run's ``report.json``
    and aggregates them.  Read-only: nothing is written and no live queue
    file is touched.
    """

    root = Path(runs_root)
    records: list[CloseOutRecord] = []
    for directory in _close_out_dirs(root):
        record = read_close_out(directory)
        if record is not None:
            records.append(record)
    return aggregate_records(records, node=node)


def outcome_count_matrix(
    records: Iterable[CloseOutRecord],
) -> dict[str, Any]:
    """The flat count matrix the measurement layer reads.

    A single row per canonical outcome, with the observed/derived split
    beside it.  The keys are exactly the canonical vocabulary, so a
    consumer never has to know which outcomes exist -- it reads them from
    here.
    """

    observed: dict[str, int] = {outcome: 0 for outcome in OUTCOME_ORDER}
    derived: dict[str, int] = {outcome: 0 for outcome in OUTCOME_ORDER}
    total: dict[str, int] = {outcome: 0 for outcome in OUTCOME_ORDER}
    grand_total = 0
    for record in records:
        outcome = record.outcome
        if outcome not in total:
            continue
        total[outcome] += 1
        grand_total += 1
        if record.derived:
            derived[outcome] += 1
        else:
            observed[outcome] += 1
    return {
        "total": grand_total,
        "outcomes": dict(total),
        "observed": dict(observed),
        "derived": dict(derived),
        "vocabulary": list(OUTCOME_ORDER),
        "delivered_outcomes": sorted(DELIVERED_OUTCOMES),
    }


__all__ = [
    "DERIVED_KEY",
    "NODE_ENV_VAR",
    "OUTCOME_ORDER",
    "CloseOutAggregate",
    "OutcomeCounter",
    "aggregate_records",
    "aggregate_runs_root",
    "current_node",
    "empty_counts",
    "outcome_count_matrix",
]
