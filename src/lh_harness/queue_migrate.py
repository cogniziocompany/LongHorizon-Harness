"""Moving a PENDING queue entry to another node, durably.

LHH-QUEUE-MIGRATION-TOMBSTONE, slices B and C.

Why this module exists
----------------------
Paxton's hard rule (2026-10-26) is to split the workload across nodes, and
the Codex review of the host-recovery work (gw-v1-host-recovery-owner-20261022,
02:08Z / 03:12Z) spells out why a naive "enqueue on B, cancel on A" is not
enough:

* **Enqueue-readback then cancel-readback leaves a duplicate-writer window.**
  Between the destination's enqueue and the source's cancel, BOTH nodes hold a
  launchable copy of the same work.  Either node may launch it, and the two
  launches would write to the same workspace.  The window has to be closed by
  the SOURCE side going durable-FIRST, not by the two sides agreeing later.
* **The destination claim must be gated on a confirmed source cancel or a
  lease transfer.**  The source has to be provably unable to launch before the
  destination may launch.
* **A partial pair needs recovery.**  If the process dies between the source
  tombstone and the destination create, the work exists on neither side until
  something repairs it.

The shape this module implements:

1. ONE source-side transaction marks the source entry ``migrated`` -- a
   durable TOMBSTONE (slice A).  ``migrated`` is terminal, so the source can
   never launch that entry again.  This closes the duplicate-writer window:
   from the tombstone onwards there is exactly ONE node that may hold a
   launchable copy, and it is the destination.
2. THEN the destination entry is created through the destination's own
   ``POST /api/queue`` with the same idempotency key and a ``dedup_key`` of
   ``<source queue_id>:<dest node>``.
3. If the destination create fails, the source STAYS tombstoned with
   ``migrated_to.pending = true``.  The work is not resurrected on the source
   (that would reopen the duplicate-writer window); it is completed later by a
   retry of the same call, or by the recovery sweep (slice C).

Never two launchable copies at any instant: the source's tombstone is written
before the destination's entry exists, and the destination's entry is not
launchable until it exists.

The module is deliberately transport-agnostic.  The HTTP calls to the
destination go through two small injectable seams (``dest_get_entry`` and
``dest_create_entry``); the default implementations speak the destination's
own REST API over ``urllib.request``.  Tests substitute loopback stubs, so no
test ever contacts a non-local host.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from .queue import QueueEntry, QueueStore

logger = logging.getLogger(__name__)

# The destination node's own API root, e.g. "http://ct110:8080".  Only the
# scheme and the netloc are used to build the API paths; a path component on
# the supplied base URL is ignored (the harness API lives at the root).
_DEST_TIMEOUT_SECONDS = 15.0


class MigrationError(RuntimeError):
    """A migration could not be completed.

    ``status`` is the HTTP-shaped status the REST layer should answer with
    (409 for a refused source entry, 502 for an unreachable destination), and
    ``detail`` is the human-readable reason.
    """

    def __init__(self, detail: str, *, status: int = 502) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


def destination_dedup_key(source_queue_id: str, dest_node: str) -> str:
    """The destination entry's ``dedup_key``: ``<source queue_id>:<dest node>``.

    LHH-QUEUE-MIGRATION-TOMBSTONE (B).  The key is what makes the destination
    create idempotent: the destination's own ``dedup_key`` semantics return
    the existing entry instead of minting a second one, so a retry of the same
    migration -- or the recovery sweep re-creating the entry -- converges on
    ONE destination entry.
    """

    return f"{source_queue_id}:{dest_node}"


def workspace_basename(workspace: str) -> str:
    """The destination workspace: the SOURCE workspace's basename.

    LHH-QUEUE-MIGRATION-TOMBSTONE (B): the work keeps its workspace identity
    across the move, but only the basename travels -- the destination node has
    its own root, so an absolute source path would point at a directory that
    does not exist there.  The basename is the name the work is known by.
    """

    text = str(workspace or "").strip()
    if not text:
        return ""
    return os.path.basename(text.rstrip("/")) or text


def destination_create_body(
    entry: QueueEntry,
    *,
    dest_node: str,
    source_queue_id: str | None = None,
    idempotency_key: str | None = None,
    source_node: str = "",
) -> dict[str, Any]:
    """The body sent to the destination's ``POST /api/queue``.

    LHH-QUEUE-MIGRATION-TOMBSTONE (B).  The mapping is exact and fixed:

    * ``task``          -- the source task text, unchanged.
    * ``name``          -- the source name, unchanged.
    * ``trio``          -- the source trio, unchanged.
    * ``priority``      -- the source priority, unchanged.
    * ``max_rounds``    -- the source max_rounds, unchanged.
    * ``workspace``     -- the source workspace's BASENAME (see
      ``workspace_basename``).
    * ``dedup_key``     -- ``<source queue_id>:<dest node>`` (see
      ``destination_dedup_key``).
    * ``requested_by``  -- the source queue id, so the destination entry names
      the source it came from.  The destination's own requester block is NOT
      forwarded: the destination stamps its own, and the source id travels in
      ``migrated_from`` too.
    * ``migrated_from`` -- the destination-side link
      ``{node: <source node>, queue_id: <source queue id>, at: <epoch>}``.
      The destination validates it against the ``dedup_key`` (the link and
      the key must name the same source), so the two cannot disagree.

    ``idempotency_key`` rides along as the HTTP ``Idempotency-Key`` header
    (the wire-level idempotency), not as a body field: the body allowlist
    rejects unknown keys, and the body's own ``dedup_key`` is the
    store-level idempotency.

    ``branch``/``continue_branch``/``base_check`` are deliberately NOT carried:
    they describe a source-side git state that does not exist on the
    destination.  The destination entry starts from a clean workspace, and the
    launcher applies the destination node's own defaults.
    """

    source_id = source_queue_id or entry.queue_id
    body: dict[str, Any] = {
        "name": entry.name,
        "task": entry.task,
        "trio": entry.trio,
        "priority": entry.priority,
        "max_rounds": entry.max_rounds,
        "workspace": workspace_basename(entry.workspace),
        "dedup_key": destination_dedup_key(source_id, dest_node),
        "requested_by": source_id,
    }
    if source_node:
        body["migrated_from"] = {
            "node": source_node,
            "queue_id": source_id,
            "at": float(entry.created_at or 0.0),
        }
    if entry.time_limit_minutes is not None:
        body["time_limit_minutes"] = entry.time_limit_minutes
    if entry.model is not None:
        body["model"] = entry.model
    if entry.roles is not None:
        body["roles"] = entry.roles
    if entry.gtm_manager:
        body["gtm_manager"] = entry.gtm_manager
    return body


def _destination_root(dest_base_url: str) -> str:
    """The destination API root: ``scheme://netloc`` of the supplied base URL."""

    parts = urlsplit(str(dest_base_url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise MigrationError(
            f"dest_base_url must be an http(s) URL, got {dest_base_url!r}",
            status=422,
        )
    return f"{parts.scheme}://{parts.netloc}"


def _http_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = _DEST_TIMEOUT_SECONDS,
) -> tuple[int, Any]:
    """One JSON HTTP call against the destination. Returns ``(status, payload)``.

    A transport failure (the destination is down) raises ``MigrationError``;
    an HTTP error status is returned, not raised, so the caller can tell
    "the destination answered 404" (a real answer) from "the destination did
    not answer" (no answer at all).
    """

    data = None
    request_headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-supplied node URL
            raw = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        status = int(exc.code)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise MigrationError(f"destination unreachable: {exc}") from exc
    payload: Any = None
    if raw:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
    return status, payload


def default_dest_create_entry(
    dest_base_url: str,
    body: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Create the destination entry through its own ``POST /api/queue``.

    Returns ``(queue_id, response_payload)``.  The destination's
    ``dedup_key`` semantics make the call idempotent: a retry returns the
    existing entry rather than minting a second one.
    """

    root = _destination_root(dest_base_url)
    headers = {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    status, payload = _http_json(
        f"{root}/api/queue",
        method="POST",
        body=body,
        headers=headers,
    )
    if status >= 400:
        raise MigrationError(
            f"destination refused the enqueue (HTTP {status})",
            status=502,
        )
    if not isinstance(payload, dict) or not payload.get("queue_id"):
        raise MigrationError("destination enqueue returned no queue_id")
    return str(payload["queue_id"]), payload


def default_dest_get_entry(
    dest_base_url: str,
    *,
    dedup_key: str,
    timeout: float = _DEST_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Find the destination entry with this ``dedup_key``, or ``None``.

    LHH-QUEUE-MIGRATION-TOMBSTONE (C): the recovery sweep asks the
    destination "do you already hold this work?".  A destination that answers
    "no" (or cannot be reached) is handled by the caller: a reachable
    destination that does not hold it means the create never landed and the
    sweep re-creates it; an unreachable destination is left pending for the
    next attempt.
    """

    root = _destination_root(dest_base_url)
    url = f"{root}/api/queue?status=pending,launched,done,failed,blocked,migrated&limit=500"
    try:
        status, payload = _http_json(url, method="GET", timeout=timeout)
    except MigrationError:
        raise
    if status >= 400:
        raise MigrationError(f"destination query failed (HTTP {status})", status=502)
    return _find_by_dedup(payload, dedup_key)


def _find_by_dedup(payload: Any, dedup_key: str) -> dict[str, Any] | None:
    """Find the entry carrying ``dedup_key`` in a ``GET /api/queue`` payload.

    The endpoint returns ``{"groups": {"<status>": [...]}}`` (the full view)
    or a legacy flat shape; both are searched, so the lookup does not depend
    on which view the destination happens to serve.
    """

    if not isinstance(payload, dict):
        return None
    groups = payload.get("groups")
    candidates: list[Any] = []
    if isinstance(groups, dict):
        for bucket in groups.values():
            if isinstance(bucket, list):
                candidates.extend(bucket)
    for key in ("entries", "items"):
        bucket = payload.get(key)
        if isinstance(bucket, list):
            candidates.extend(bucket)
    for item in candidates:
        if isinstance(item, dict) and item.get("dedup_key") == dedup_key:
            return item
    return None


@dataclass
class MigrationResult:
    """The outcome of one migration call."""

    queue_id: str
    status: str
    migrated: bool
    dest_node: str = ""
    dest_queue_id: str | None = None
    dedup_key: str = ""
    idempotency_key: str = ""
    epoch: int | None = None
    detail: str = ""
    already_migrated: bool = False
    repairs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "queue_id": self.queue_id,
            "status": self.status,
            "migrated": self.migrated,
            "dest_node": self.dest_node,
            "dest_queue_id": self.dest_queue_id,
            "dedup_key": self.dedup_key,
            "idempotency_key": self.idempotency_key,
            "migration_epoch": self.epoch,
            "detail": self.detail,
            "already_migrated": self.already_migrated,
            "repairs": list(self.repairs),
        }


def migrate_pending_entry(
    store: QueueStore,
    queue_id: str,
    *,
    dest_node: str,
    dest_base_url: str,
    idempotency_key: str,
    source_node: str = "",
    dest_create: Callable[[str, dict[str, Any], str | None], tuple[str, dict[str, Any]]] | None = None,
    dest_lookup: Callable[[str, str], dict[str, Any] | None] | None = None,
    on_repair: Callable[[str], None] | None = None,
) -> MigrationResult:
    """Migrate one PENDING entry to another node. Idempotent.

    LHH-QUEUE-MIGRATION-TOMBSTONE (B).  The order is the whole point:

    1. ONE source-side transaction tombstones the source entry
       (``status = "migrated"``, ``migrated_to = {node, queue_id, at}``,
       ``migrated_to.pending = true`` while the destination create is still
       outstanding).  After this write the source can NEVER launch the entry
       again -- ``migrated`` is terminal -- so the duplicate-writer window is
       closed on the source side before the destination exists.
    2. THEN the destination entry is created, with ``dedup_key =
       <source queue_id>:<dest node>`` and the same idempotency key.

    If step 2 fails, the source stays tombstoned with ``migrated_to.pending
    = true`` and the failure is reported; the entry is NOT resurrected on the
    source (that would reopen the duplicate-writer window).  Re-running the
    same call completes the migration: the tombstone is found, the pending
    flag says "the destination may not exist yet", and the destination create
    is repeated.  The destination's ``dedup_key`` semantics make the repeat
    idempotent -- it returns the existing entry instead of a second one.

    A source that is not ``pending`` is REFUSED: a launched entry has a live
    run behind it (running runs are untouched), and a terminal entry is
    already finished or already a tombstone.
    """

    dest_node = str(dest_node or "").strip()
    if not dest_node:
        raise MigrationError("dest_node is required", status=422)
    idempotency_key = str(idempotency_key or "").strip()
    if not idempotency_key:
        raise MigrationError("idempotency_key is required", status=422)

    entry = store.get(queue_id)
    if entry is None:
        raise MigrationError("queue entry not found", status=404)

    already = entry.status == "migrated"
    if not already and entry.status != "pending":
        # The hard rule: a RUNNING run is never migrated.  A launched entry
        # owns a live run on this node; moving it would mean two writers.
        raise MigrationError(
            f"cannot migrate entry with status {entry.status}",
            status=409,
        )

    dedup_key = destination_dedup_key(queue_id, dest_node)

    if already:
        # The source is already a tombstone.  Resume the outstanding
        # migration instead of starting a second one: the link's node and
        # dedup key must agree with this call, otherwise the two migrations
        # disagree about the destination and neither can be repaired.
        link = entry.migrated_to or {}
        link_node = str(link.get("node") or "").strip()
        if link_node and link_node != dest_node:
            raise MigrationError(
                f"entry is already migrated to {link_node}",
                status=409,
            )
        epoch = entry.migration_epoch
    else:
        # ONE source-side transaction: the tombstone and its link are written
        # in the same store write (QueueStore.mark_migrated mutates the entry
        # and calls update, which writes the whole record atomically).
        tombstoned = store.mark_migrated(
            queue_id,
            node=dest_node,
            dest_queue_id=None,
            epoch=(entry.migration_epoch or 0) + 1,
            pending=True,
            dest_base_url=dest_base_url,
        )
        if tombstoned is None:
            raise MigrationError("queue entry not found", status=404)
        entry = tombstoned
        epoch = tombstoned.migration_epoch

    # The destination create.  A failure leaves the tombstone pending; the
    # source is never resurrected.
    create = dest_create or default_dest_create_entry
    body = destination_create_body(
        entry,
        dest_node=dest_node,
        source_queue_id=queue_id,
        idempotency_key=idempotency_key,
        source_node=source_node,
    )
    try:
        dest_queue_id, payload = create(dest_base_url, body, idempotency_key)
    except MigrationError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise MigrationError(f"destination create failed: {exc}") from exc

    # Finalize the link: the destination entry exists, so the tombstone is no
    # longer pending.  This is a LINK update, not a status transition -- the
    # entry stays ``migrated``.
    finalized = store.finalize_migration(
        queue_id,
        node=dest_node,
        dest_queue_id=dest_queue_id,
        epoch=epoch,
        dest_base_url=dest_base_url,
    )
    if finalized is None:
        raise MigrationError("queue entry not found", status=404)

    logger.info(
        "queue migrate: %s -> node=%s queue_id=%s dedup_key=%s epoch=%s",
        queue_id,
        dest_node,
        dest_queue_id,
        dedup_key,
        epoch,
    )
    return MigrationResult(
        queue_id=queue_id,
        status="migrated",
        migrated=True,
        dest_node=dest_node,
        dest_queue_id=dest_queue_id,
        dedup_key=dedup_key,
        idempotency_key=idempotency_key,
        epoch=epoch,
        detail="migrated",
        already_migrated=already,
        repairs=list(payload.get("repairs") or []) if isinstance(payload, dict) else [],
    )


def _pending_tombstones(store: QueueStore) -> list[QueueEntry]:
    """The source tombstones whose destination create never landed.

    LHH-QUEUE-MIGRATION-TOMBSTONE (C).  A tombstone with
    ``migrated_to.pending = true`` is a PARTIAL PAIR: the source is
    durably tombstoned but the destination entry may not exist.  Only these
    are candidates for repair.
    """

    out: list[QueueEntry] = []
    for entry in store.list():
        if entry.status != "migrated":
            continue
        link = entry.migrated_to
        if not isinstance(link, dict):
            continue
        if link.get("pending"):
            out.append(entry)
    return out


def repair_pending_migrations(
    store: QueueStore,
    *,
    dest_base_url_of: Callable[[QueueEntry], str | None] | None = None,
    dest_lookup: Callable[[str, str], dict[str, Any] | None] | None = None,
    dest_create: Callable[[str, dict[str, Any], str | None], tuple[str, dict[str, Any]]] | None = None,
    source_node: str = "",
) -> list[str]:
    """Repair every partial pair. Returns one log line per repair.

    LHH-QUEUE-MIGRATION-TOMBSTONE (C).  For each tombstone whose
    ``migrated_to.pending`` is true, the destination is asked whether it
    already holds the work, identified by the ``dedup_key``
    (``<source queue_id>:<dest node>``):

    * **present** -> the destination create DID land; the link is finalized
      (the tombstone stops being pending).
    * **absent**  -> the create never landed; the entry is re-created from the
      SOURCE's stored task text -- the same body the original migration would
      have sent, so the destination ends up with the same work under the same
      dedup key.

    An unreachable destination is left pending: the next sweep (or the next
    retry of the migration call) tries again.  Leaving it pending is the safe
    outcome -- the source stays tombstoned, so there is still exactly one
    possible writer.

    The lookup and the create go through the same injectable seams as the
    migration itself, so the repair path is exercised by the same tests (and
    the same loopback stubs) as the happy path.
    """

    lookup = dest_lookup or default_dest_get_entry
    create = dest_create or default_dest_create_entry
    repairs: list[str] = []

    for entry in _pending_tombstones(store):
        link = entry.migrated_to or {}
        dest_node = str(link.get("node") or "").strip()
        if not dest_node:
            continue
        base_url = dest_base_url_of(entry) if dest_base_url_of is not None else entry.migrated_dest_base_url
        if not base_url:
            # No destination URL is known for this tombstone; it cannot be
            # repaired from here.  It stays pending, and the retry of the
            # original migration call still completes it.
            continue
        dedup_key = destination_dedup_key(entry.queue_id, dest_node)
        try:
            found = lookup(base_url, dedup_key)
        except MigrationError as exc:
            logger.warning(
                "queue migrate repair skipped: %s node=%s (%s)",
                entry.queue_id,
                dest_node,
                exc.detail,
            )
            continue
        if found is not None:
            dest_queue_id = str(found.get("queue_id") or "")
            store.finalize_migration(
                entry.queue_id,
                node=dest_node,
                dest_queue_id=dest_queue_id or None,
                epoch=entry.migration_epoch,
                dest_base_url=base_url,
            )
            line = (
                f"queue migrate repair: {entry.queue_id} -> node={dest_node} "
                f"queue_id={dest_queue_id or '?'} finalized (destination already held the entry)"
            )
            logger.info(line)
            repairs.append(line)
            continue
        # The destination does not hold it: re-create from the stored task
        # text.  The body is built from the SOURCE entry, which still holds
        # every field the destination needs.
        body = destination_create_body(
            entry,
            dest_node=dest_node,
            source_queue_id=entry.queue_id,
            source_node=source_node,
        )
        try:
            dest_queue_id, _payload = create(base_url, body, None)
        except MigrationError as exc:
            logger.warning(
                "queue migrate repair failed: %s node=%s (%s)",
                entry.queue_id,
                dest_node,
                exc.detail,
            )
            continue
        store.finalize_migration(
            entry.queue_id,
            node=dest_node,
            dest_queue_id=dest_queue_id,
            epoch=entry.migration_epoch,
            dest_base_url=base_url,
        )
        line = (
            f"queue migrate repair: {entry.queue_id} -> node={dest_node} "
            f"queue_id={dest_queue_id} re-created from the stored task text"
        )
        logger.info(line)
        repairs.append(line)
    return repairs
