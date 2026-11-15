"""Duplicate-work detection over the record-only ``work_origin`` block.

Task easybutt0n-17.  The goal board's "easy to detect duplicate work" need
turned into a READ-ONLY view: which queue entries / runs claim the SAME
YouTrack ticket (or, when no ticket is recorded, the same branch), while
more than one LIVE run holds that claim.

This module is deliberately PURE: it performs no I/O, mutates nothing, and
writes nothing.  It consumes already-resolved inputs (queue entries and run
rows) and returns the duplicate groups.  That is the whole point of the
``work_origin`` block: it RECORDS where work came from; it never decides
anything.  Nothing here gates a launch, refuses a run, or writes a store --
the Harness queue stays the only execution store and YouTrack the only
ticket store.

THE CLAIM KEY.  A claim is identified by the recorded ticket id when there
is one; otherwise, when the entry recorded no ticket, by the branch it
continues.  So:

  * two live runs both claiming ticket ``YT-42``  -> a duplicate, and
  * two live runs both continuing branch ``feat/x`` with no recorded
    ticket -> a duplicate too.

An entry that records NEITHER a ticket NOR a branch claims nothing and is
never reported: it names no shared piece of work.

WHAT COUNTS AS LIVE.  A run is live while its lifecycle status is one of the
active statuses (``creating``/``starting``/``running``/``waiting_approval``/
``stopping``).  A finished or failed run holds no claim, so two runs that
worked the same ticket at different times are NOT a duplicate -- that is the
normal case of a retry after a crash, and the duplicate view must stay quiet
about it.

An entry that has not launched yet (no run id) is not a live run either: it
is queued work, not work in flight.  The view reports RUNS, so a queued
duplicate is invisible until one of the copies actually starts.
"""

from __future__ import annotations

from typing import Any

from .supervisor.lifecycle import ACTIVE_STATUSES, canonical_lifecycle_status


# The two keys a claim can be identified by, in precedence order: a recorded
# ticket wins; the branch is the fallback for work that recorded no ticket.
_CLAIM_TICKET = "ticket"
_CLAIM_BRANCH = "branch"

# The bounded id characters a claim key may carry.  A claim key ends up in a
# URL and in a JSON body, so it is bounded and stripped of NUL bytes.
_MAX_CLAIM_CHARS = 256


def _claim_key(work_origin: Any, branch: Any) -> tuple[str, str] | None:
    """The (kind, value) pair this work claims, or None when it claims none.

    ``kind`` is ``"ticket"`` when a YouTrack id is recorded, ``"branch"``
    when the work instead continues a branch, and the pair is None when the
    work names neither.
    """

    ticket = None
    goal_ref = None
    if isinstance(work_origin, dict):
        raw_ticket = work_origin.get("ticket")
        if isinstance(raw_ticket, str) and raw_ticket.strip():
            ticket = raw_ticket.strip()
        raw_goal = work_origin.get("goal_ref")
        if isinstance(raw_goal, str) and raw_goal.strip():
            goal_ref = raw_goal.strip()
    # A recorded ticket is the strongest claim.  A recorded GOAL reference is
    # the next: two runs working the same goal-board item are doing the same
    # work even when they name different tickets.  The branch is the last
    # fallback, for work that recorded neither.
    if ticket:
        return (_CLAIM_TICKET, ticket)
    if goal_ref:
        return ("goal", goal_ref)
    if isinstance(branch, str) and branch.strip():
        return (_CLAIM_BRANCH, branch.strip())
    return None


def is_live_status(status: Any) -> bool:
    """True when a run status names a run that still holds its claim."""

    return canonical_lifecycle_status(status, default="") in ACTIVE_STATUSES


def find_duplicate_work(
    runs: list[dict[str, Any]],
    *,
    entries: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Group the LIVE runs that claim the same piece of work.

    ``runs`` is a list of run rows; each row must carry a ``run_id`` (or
    ``id``), a ``status``, and either a ``work_origin`` block or a ``branch``.
    The run's ``status`` is its LIFECYCLE status, and only a live run holds a
    claim.

    ``entries`` is the optional queue side.  A queue entry that already
    launched carries the run id its run has, so the caller can pair the
    entry's OWN ``work_origin``/``branch`` with that run's live status.  The
    entry's status is a QUEUE status (pending/launched/done/...), never a run
    lifecycle status, so it cannot decide liveness on its own -- the entry
    only supplies the CLAIM, and the run row supplies the LIVENESS.

    Returns one group per claim held by MORE THAN ONE live run, each with
    the claim kind, the claim value, and the runs holding it.  A claim held
    by exactly one live run is not a duplicate and is not returned.
    """

    # The claim each run holds: the run's own block/branch, falling back to
    # the queue entry it came from (a run launched before the block existed
    # carries no block, but its entry does).
    entry_claims: dict[str, tuple[Any, Any]] = {}
    for entry in entries or []:
        if entry is None:
            continue
        if isinstance(entry, dict):
            run_id = str(entry.get("run_id") or "")
            work_origin = entry.get("work_origin")
            branch = entry.get("branch")
        else:
            run_id = str(getattr(entry, "run_id", "") or "")
            work_origin = getattr(entry, "work_origin", None)
            branch = getattr(entry, "branch", None)
        # A queue entry that has not launched is queued work, not a live run;
        # it holds no claim yet.
        if not run_id:
            continue
        entry_claims[run_id] = (work_origin, branch)

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in runs:
        if not isinstance(row, dict):
            continue
        run_id = str(row.get("run_id") or row.get("id") or "")
        if not run_id or not is_live_status(row.get("status")):
            continue
        work_origin = row.get("work_origin")
        branch = row.get("branch")
        if work_origin is None and branch is None:
            entry_claim = entry_claims.get(run_id)
            if entry_claim is not None:
                work_origin, branch = entry_claim
        claim = _claim_key(work_origin, branch)
        if claim is None:
            continue
        groups.setdefault(claim, []).append(
            {
                "run_id": run_id,
                "status": str(row.get("status") or ""),
            }
        )

    duplicates: list[dict[str, Any]] = []
    for (kind, value), members in groups.items():
        if len(members) < 2:
            continue
        duplicates.append(
            {
                "claim_kind": kind,
                "claim": value[:_MAX_CLAIM_CHARS],
                "runs": members,
                "count": len(members),
            }
        )
    duplicates.sort(key=lambda group: (group["claim_kind"], group["claim"]))
    return duplicates
