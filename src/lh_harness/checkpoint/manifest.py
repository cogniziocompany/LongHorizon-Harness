"""``trusted-handoff/v2`` checkpoint and delta manifests -- validation only.

V1.5 T4.1 (F12).  This module defines the *typed manifests* and the
validation library for the base-plus-delta chain; it deliberately stops
short of restoration (T4.3) and acknowledgement (T4.4).

The trust split is the whole point, and it mirrors the pinned spec:

* The **trusted handoff** is an authenticated, pinned object supplied by the
  harness/evidence owner.  It carries the schema id (``trusted-handoff/v2``),
  the scope (tenant/task/policy revision), the mandatory value schemas, and
  the *trusted* base and terminal identities, sequences and content digests.
* The **checkpoint and its deltas** are untrusted documents.  They carry
  their own ``digest`` field, but that field is never the thing that
  establishes trust: every digest here is recomputed from content under
  canonical-json/v1 and compared against either the trusted handoff or the
  previous link in the chain.

``document_digest`` excludes a document's own ``digest`` key (a digest can
never cover itself) and emits ``"sha256:" + hexdigest``.  ``canonical_bytes``
is the single serializer every implementation must share: sorted keys,
compact separators, and ``allow_nan=False`` so NaN and infinities are a
hard ``ValueError`` rather than a non-reproducible digest.

Validation shape
----------------
:func:`validate_chain` checks the baseline against the trusted base, then
validates *every* delta in sequence order -- identity, contiguity,
predecessor digest, self-digest and scope -- and finally compares the
recomputed terminal (id, sequence, digest) triple against the trusted
handoff.  The mandatory state schemas are applied to the baseline and again
after *every* delta, never sourced from the checkpoint itself.

The module is pure: no I/O, no network, no clock, no global state.  It takes
plain mappings and returns plain values so it can be unit-tested as the
spec's reference behaviour.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

#: The pinned, versioned trusted-handoff schema this module validates against.
SCHEMA_ID = "trusted-handoff/v2"

#: The pinned serializer version.  Every digest in the chain uses exactly this.
CANONICAL_JSON_VERSION = "canonical-json/v1"


class IncompleteContext(ValueError):
    """A checkpoint/delta chain cannot be trusted, so it is visibly incomplete.

    This is the single failure type for the manifest plane.  Callers that
    consume a validated chain (T4.3) must treat it as "no verified result is
    available" rather than falling back to partial or fabricated context.
    """


_MISSING = object()


# --------------------------------------------------------------------------
# canonical-json/v1
# --------------------------------------------------------------------------


def canonical_bytes(value: Any) -> bytes:
    """Serialize ``value`` under canonical-json/v1.

    Sorted keys, compact separators, and ``allow_nan=False``: the last one is
    load-bearing.  Default ``json`` settings happily emit ``NaN``/``Infinity``
    (which are not JSON), and two implementations that disagree on their
    spelling would produce digests that cannot be compared.  Non-finite
    numbers are therefore rejected here, before they can reach a digest.
    """
    try:
        text = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise IncompleteContext("value is not canonical-json/v1") from exc
    return text.encode("utf-8")


def document_digest(document: Mapping[str, Any]) -> str:
    """``sha256:`` digest over ``document`` with its own ``digest`` key removed.

    A document cannot sign itself: the ``digest`` field is excluded from the
    bytes that are hashed, so the digest covers content only.  Anything that
    fails canonical-json/v1 (a non-serializable value, NaN, infinity) raises
    :class:`IncompleteContext` instead of producing a digest.
    """
    unsigned = {key: value for key, value in document.items() if key != "digest"}
    payload = canonical_bytes(unsigned)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------
# Manifest types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckpointManifest:
    """The base of a chain: a compact typed snapshot reference.

    The manifest carries identity, scope, a cursor and the state itself as
    plain values.  It is a value object: :meth:`as_document` renders it to
    the mapping that is canonicalized and digested, and :meth:`with_digest`
    returns a copy carrying a computed ``digest``.  Nothing here decides what
    is *required* -- that is the trusted handoff's job.
    """

    checkpoint_id: str
    sequence: int
    state: Mapping[str, Any]
    scope: Mapping[str, Any]
    cursor: Any = None
    digest: str | None = None

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "checkpoint_id": self.checkpoint_id,
            "sequence": self.sequence,
            "state": dict(self.state),
            "cursor": self.cursor,
        }
        document.update(self.scope)
        if self.digest is not None:
            document["digest"] = self.digest
        return document

    def computed_digest(self) -> str:
        """Content digest, recomputed from the unsigned document."""
        return document_digest(self.as_document())

    def with_digest(self) -> "CheckpointManifest":
        return CheckpointManifest(
            checkpoint_id=self.checkpoint_id,
            sequence=self.sequence,
            state=self.state,
            scope=self.scope,
            cursor=self.cursor,
            digest=self.computed_digest(),
        )


@dataclass(frozen=True)
class DeltaManifest:
    """One immutable delta: changes and explicit removals over a predecessor.

    ``predecessor_digest`` is the *content* digest of the document this delta
    was built on -- the base checkpoint for the first delta, and the previous
    delta's content digest afterwards.  ``base_checkpoint_id`` names the base
    the chain descends from, so a delta cannot be spliced onto another base.

    Omitted fields mean unchanged; a removal is only a removal when it is
    listed in ``removals`` (explicit-removal contract from the spec).
    """

    delta_id: str
    sequence: int
    base_checkpoint_id: str
    predecessor_digest: str
    changes: Mapping[str, Any] = None  # type: ignore[assignment]
    removals: Sequence[str] = ()
    scope: Mapping[str, Any] = None  # type: ignore[assignment]
    digest: str | None = None

    def __post_init__(self) -> None:
        if self.changes is None:
            object.__setattr__(self, "changes", {})
        if self.scope is None:
            object.__setattr__(self, "scope", {})

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "delta_id": self.delta_id,
            "sequence": self.sequence,
            "base_checkpoint_id": self.base_checkpoint_id,
            "predecessor_digest": self.predecessor_digest,
            "changes": dict(self.changes),
            "removals": list(self.removals),
        }
        document.update(self.scope)
        if self.digest is not None:
            document["digest"] = self.digest
        return document

    def computed_digest(self) -> str:
        return document_digest(self.as_document())

    def with_digest(self) -> "DeltaManifest":
        return DeltaManifest(
            delta_id=self.delta_id,
            sequence=self.sequence,
            base_checkpoint_id=self.base_checkpoint_id,
            predecessor_digest=self.predecessor_digest,
            changes=self.changes,
            removals=self.removals,
            scope=self.scope,
            digest=self.computed_digest(),
        )


# --------------------------------------------------------------------------
# Trusted handoff
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustedHandoff:
    """The authenticated, pinned handoff the checkpoint cannot influence.

    ``required_state_schemas`` maps a dotted state path to a schema such as
    ``{"type": "string", "min_length": 1}`` or
    ``{"type": "string", "enum": ["a", "b"]}``.  ``expected_base`` and
    ``expected_terminal`` are ``{id, sequence, digest}`` triples -- the
    terminal key is normalized to ``delta_id`` by the spec's wording, but
    this module accepts either spelling.
    """

    scope: Mapping[str, Any]
    required_state_schemas: Mapping[str, Mapping[str, Any]]
    expected_base: Mapping[str, Any]
    expected_terminal: Mapping[str, Any]
    schema: str = SCHEMA_ID

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "TrustedHandoff":
        """Build a handoff from its raw document, rejecting the wrong schema id."""
        if document.get("schema") != SCHEMA_ID:
            raise IncompleteContext("unsupported trusted handoff schema")
        try:
            return cls(
                schema=document["schema"],
                scope=document["scope"],
                required_state_schemas=document["required_state_schemas"],
                expected_base=document["expected_base"],
                expected_terminal=document["expected_terminal"],
            )
        except KeyError as exc:
            raise IncompleteContext(f"trusted handoff is missing {exc.args[0]}") from exc


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _require_schema(handoff: TrustedHandoff) -> None:
    """The schema id is pinned by the handoff, never by the checkpoint."""
    if handoff.schema != SCHEMA_ID:
        raise IncompleteContext("unsupported trusted handoff schema")


def _require_scope_binding(
    document: Mapping[str, Any], scope: Mapping[str, Any]
) -> None:
    """Tenant/task/policy binding: every scoped key must match exactly."""
    for key in ("tenant_id", "task_id", "policy_revision"):
        if key not in scope:
            raise IncompleteContext(f"trusted handoff scope is missing {key}")
        if document.get(key) != scope[key]:
            raise IncompleteContext(f"document scope mismatch: {key}")


def _parts(path: str) -> list[str]:
    parts = path.split(".")
    if not all(parts):
        raise IncompleteContext("invalid state path")
    return parts


def _get_path(state: Any, path: str) -> Any:
    current = state
    for part in _parts(path):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _validate_value(value: Any, schema: Mapping[str, Any], path: str) -> None:
    kind = schema.get("type")
    valid = {
        "object": lambda: isinstance(value, Mapping),
        "array": lambda: isinstance(value, (list, tuple)),
        "string": lambda: isinstance(value, str),
        "number": lambda: isinstance(value, (int, float))
        and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
    }.get(kind, lambda: False)()
    if not valid:
        raise IncompleteContext(f"mandatory state has wrong type or is null: {path}")
    if "min_length" in schema and len(value) < schema["min_length"]:
        raise IncompleteContext(f"mandatory state is too short: {path}")
    if "enum" in schema and value not in schema["enum"]:
        raise IncompleteContext(f"mandatory state is outside its allowed values: {path}")


def require_mandatory(
    state: Any, schemas: Mapping[str, Mapping[str, Any]]
) -> None:
    """Check every mandatory path is present, non-null and typed.

    ``null`` is not presence: a schema of ``{"type": "string"}`` rejects
    ``None`` because ``None`` fails the type test.  The requirement list comes
    from the trusted handoff, so a checkpoint cannot shrink its own
    obligations by emptying or rewriting the list it carries.
    """
    for path, schema in schemas.items():
        value = _get_path(state, path)
        if value is _MISSING:
            raise IncompleteContext(f"mandatory state missing: {path}")
        _validate_value(value, schema, path)


def _mandatory_view(
    state: Mapping[str, Any], schemas: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Project only the mandatory paths out of the baseline state.

    This is a validation projection, not a restored context: it keeps one
    entry per trusted required path so later deltas can be checked for
    removing or re-typing it.  Nothing else from the checkpoint is materialized.
    """
    view: dict[str, Any] = {}
    for path in schemas:
        value = _get_path(state, path)
        if value is _MISSING:
            raise IncompleteContext(f"mandatory state missing: {path}")
        view[path] = value
    return view


def _path_relation(left: str, right: str) -> str:
    """``left`` vs ``right``: ``equal``, ``parent``, ``child`` or ``unrelated``."""
    if left == right:
        return "equal"
    if right.startswith(left + "."):
        return "parent"
    if left.startswith(right + "."):
        return "child"
    return "unrelated"


def _apply_delta_to_mandatory(
    view: dict[str, Any],
    document: Mapping[str, Any],
    schemas: Mapping[str, Mapping[str, Any]],
) -> None:
    """Apply one delta's changes/removals to the mandatory projection.

    Only mandatory paths matter here, and removal of one -- directly or by
    replacing an ancestor with a scalar -- is rejected.  A changed value is
    re-validated against the pinned schema, so ``null``, a wrong type or a
    value outside its enum fails immediately after the delta rather than only
    at the end of a restore (NEG-37, NEG-39).
    """
    changes = document.get("changes") or {}
    removals = document.get("removals") or ()

    for path in removals:
        for mandatory in list(view):
            relation = _path_relation(str(path), mandatory)
            if relation in ("equal", "parent"):
                raise IncompleteContext(
                    f"delta removes mandatory state: {mandatory}"
                )

    for raw_path, value in changes.items():
        path = str(raw_path)
        for mandatory in list(view):
            relation = _path_relation(path, mandatory)
            if relation == "equal":
                _validate_value(value, schemas[mandatory], mandatory)
                view[mandatory] = value
            elif relation == "parent":
                # Replacing an ancestor is only allowed when the result still
                # carries the mandatory descendant with a valid value.
                descended = _get_path(value, mandatory[len(path) + 1:])
                if descended is _MISSING:
                    raise IncompleteContext(
                        f"delta removes mandatory state: {mandatory}"
                    )
                _validate_value(descended, schemas[mandatory], mandatory)
                view[mandatory] = descended


def _expected_triple(handoff_value: Mapping[str, Any]) -> dict[str, Any]:
    terminal_id = handoff_value.get("delta_id", handoff_value.get("id"))
    try:
        return {
            "delta_id": terminal_id,
            "sequence": handoff_value["sequence"],
            "digest": handoff_value["digest"],
        }
    except KeyError as exc:
        raise IncompleteContext(
            f"trusted handoff triple is missing {exc.args[0]}"
        ) from exc


def validate_checkpoint_manifest(
    checkpoint: CheckpointManifest | Mapping[str, Any],
    handoff: TrustedHandoff,
) -> str:
    """Validate the baseline against the trusted base; return its content digest.

    Checks, in order: the pinned schema id, the scope binding, the base
    identity and sequence, the self-digest (recomputed -- the carried
    ``digest`` is only compared, never trusted), the base content digest
    against the trusted handoff, and finally the mandatory state schemas.
    """
    _require_schema(handoff)
    document = (
        checkpoint.as_document()
        if isinstance(checkpoint, CheckpointManifest)
        else dict(checkpoint)
    )
    _require_scope_binding(document, handoff.scope)

    expected_base = handoff.expected_base
    if document.get("checkpoint_id") != expected_base.get("checkpoint_id"):
        raise IncompleteContext("checkpoint identity does not match trusted base")
    if document.get("sequence", 0) != expected_base.get("sequence"):
        raise IncompleteContext("checkpoint sequence does not match trusted base")

    computed = document_digest(document)
    if document.get("digest") != computed:
        raise IncompleteContext("checkpoint self-digest mismatch")
    if computed != expected_base.get("digest"):
        raise IncompleteContext("checkpoint content does not match trusted base")

    state = document.get("state")
    if not isinstance(state, Mapping):
        raise IncompleteContext("checkpoint state is missing or not an object")
    require_mandatory(state, handoff.required_state_schemas)
    return computed


def validate_delta_manifest(
    delta: DeltaManifest | Mapping[str, Any],
    *,
    handoff: TrustedHandoff,
    previous_id: str,
    previous_sequence: int,
    previous_digest: str,
    base_checkpoint_id: str,
) -> str:
    """Validate one delta link; return its recomputed content digest.

    ``previous_*`` are the *validated* predecessor facts (the base for the
    first delta, the previous delta after that).  A delta whose predecessor
    digest does not equal the predecessor's recomputed content digest is
    rejected even if its own self-digest was recomputed to match its tampered
    content.
    """
    _require_schema(handoff)
    document = delta.as_document() if isinstance(delta, DeltaManifest) else dict(delta)
    _require_scope_binding(document, handoff.scope)

    if document.get("sequence") != previous_sequence + 1:
        raise IncompleteContext("noncontiguous delta sequence")
    if document.get("base_checkpoint_id") != base_checkpoint_id:
        raise IncompleteContext("delta base identity mismatch")
    if document.get("delta_id") == previous_id:
        raise IncompleteContext("duplicate delta identity")
    if document.get("predecessor_digest") != previous_digest:
        raise IncompleteContext("delta predecessor content digest mismatch")

    computed = document_digest(document)
    if document.get("digest") != computed:
        raise IncompleteContext("delta self-digest mismatch")
    return computed


@dataclass(frozen=True)
class ChainSummary:
    """What a successfully validated chain proves.

    This is the T4.1 output a T4.3 restorer would consume: the trusted base,
    the validated terminal triple, and the ordered delta identities.  It is
    evidence of a complete, trusted chain -- not a restored context.
    """

    base_digest: str
    terminal_id: str
    terminal_sequence: int
    terminal_digest: str
    delta_ids: tuple[str, ...]


def validate_chain(
    checkpoint: CheckpointManifest | Mapping[str, Any],
    deltas: Iterable[DeltaManifest | Mapping[str, Any]],
    *,
    handoff: TrustedHandoff,
) -> ChainSummary:
    """Validate a trusted baseline followed by an ordered chain of deltas.

    The baseline is validated first.  Deltas must then arrive in contiguous
    sequence order starting at base sequence + 1; each link is validated
    against the previous link's *recomputed* digest.  Truncation falls out of
    the terminal comparison: a chain missing its tail cannot reproduce the
    trusted terminal triple, so it is visibly incomplete rather than
    silently short.  Reordering breaks contiguity or the predecessor digest,
    and a spliced delta breaks the base identity.

    The trusted mandatory schemas are applied to the baseline and then kept
    in a mandatory-only projection that each delta's changes/removals are
    checked against, so a delta that removes a required path or sets it to
    ``null`` is rejected at that link.  What is deliberately absent is
    restoration (T4.3): no full state is materialized or returned, only the
    proof that the chain is complete and trusted.
    """
    base_digest = validate_checkpoint_manifest(checkpoint, handoff)

    base_document = (
        checkpoint.as_document()
        if isinstance(checkpoint, CheckpointManifest)
        else dict(checkpoint)
    )
    base_id = base_document["checkpoint_id"]
    base_sequence = base_document.get("sequence", 0)

    previous_id = base_id
    previous_sequence = base_sequence
    previous_digest = base_digest
    delta_ids: list[str] = []

    mandatory_view = _mandatory_view(
        base_document["state"], handoff.required_state_schemas
    )

    for delta in deltas:
        document = delta.as_document() if isinstance(delta, DeltaManifest) else dict(delta)
        previous_digest = validate_delta_manifest(
            document,
            handoff=handoff,
            previous_id=previous_id,
            previous_sequence=previous_sequence,
            previous_digest=previous_digest,
            base_checkpoint_id=base_id,
        )
        # The trusted schemas are applied again after every delta: a delta
        # that removes or mistypes a mandatory path is rejected at that link,
        # not discovered later.
        _apply_delta_to_mandatory(
            mandatory_view, document, handoff.required_state_schemas
        )
        previous_id = document["delta_id"]
        previous_sequence = document["sequence"]
        delta_ids.append(previous_id)

    for path, schema in handoff.required_state_schemas.items():
        if path not in mandatory_view:
            raise IncompleteContext(f"mandatory state missing: {path}")
        _validate_value(mandatory_view[path], schema, path)

    actual_terminal = {
        "delta_id": previous_id,
        "sequence": previous_sequence,
        "digest": previous_digest,
    }
    if actual_terminal != _expected_triple(handoff.expected_terminal):
        raise IncompleteContext("terminal content does not match trusted receipt")

    return ChainSummary(
        base_digest=base_digest,
        terminal_id=previous_id,
        terminal_sequence=previous_sequence,
        terminal_digest=previous_digest,
        delta_ids=tuple(delta_ids),
    )
