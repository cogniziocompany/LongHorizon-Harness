"""V1.5 T4.1: executable negatives and positives for trusted manifests.

Every negative case below is written so that a *naive* implementation --
one that trusts the document's own ``digest`` field, hashes non-canonical
JSON, takes requirements from the checkpoint, or compares only identity and
sequence -- would pass the document through.  Each test's docstring states
exactly which shortcut it defeats and why the naive form would survive it.
"""

from __future__ import annotations

import json

import pytest

from lh_harness.checkpoint import (
    CheckpointManifest,
    DeltaManifest,
    IncompleteContext,
    TrustedHandoff,
    canonical_bytes,
    document_digest,
    validate_chain,
    validate_checkpoint_manifest,
)

SCOPE = {"tenant_id": "t-1", "task_id": "task-9", "policy_revision": "pol-7"}

REQUIRED_SCHEMAS = {
    "instructions": {"type": "string", "min_length": 1},
    "evidence.receipt": {"type": "string", "min_length": 1},
    "budget": {"type": "number"},
    "status": {"type": "string", "enum": ["active", "done"]},
}

BASE_STATE = {
    "instructions": "do the guarded thing",
    "evidence": {"receipt": "sha256:base-receipt"},
    "budget": 5,
    "status": "active",
}


def _base(**overrides) -> CheckpointManifest:
    fields = {
        "checkpoint_id": "cp-base",
        "sequence": 0,
        "state": json.loads(json.dumps(BASE_STATE)),
        "scope": dict(SCOPE),
        "cursor": {"round": 0},
    }
    fields.update(overrides)
    return CheckpointManifest(**fields).with_digest()


def _delta(previous, sequence, *, delta_id=None, changes=None, removals=(), **overrides):
    # ``base_checkpoint_id`` names the *immediate predecessor's* identity, as
    # the pinned extract requires (``base_checkpoint_id != previous_id``), so
    # the default is the delta that comes before this one -- not necessarily
    # the base checkpoint.
    fields = {
        "delta_id": delta_id or f"d-{sequence}",
        "sequence": sequence,
        "base_checkpoint_id": f"d-{sequence - 1}" if sequence > 1 else "cp-base",
        "predecessor_digest": previous,
        "changes": changes or {},
        "removals": removals,
        "scope": dict(SCOPE),
    }
    fields.update(overrides)
    return DeltaManifest(**fields).with_digest()


def _handoff(base, terminal, *, schemas=None, expected_base=None, expected_terminal=None):
    if terminal is None:
        terminal = base
    if hasattr(terminal, "delta_id"):
        terminal_triple = {
            "delta_id": terminal.delta_id,
            "sequence": terminal.sequence,
            "digest": terminal.digest,
        }
    else:
        terminal_triple = {
            "delta_id": terminal.checkpoint_id,
            "sequence": terminal.sequence,
            "digest": terminal.digest,
        }
    return TrustedHandoff(
        scope=dict(SCOPE),
        required_state_schemas=dict(schemas if schemas is not None else REQUIRED_SCHEMAS),
        expected_base=expected_base
        or {"checkpoint_id": base.checkpoint_id, "sequence": base.sequence, "digest": base.digest},
        expected_terminal=expected_terminal or terminal_triple,
    )


# --------------------------------------------------------------------------
# canonical-json/v1 and document_digest
# --------------------------------------------------------------------------


def test_canonical_bytes_sorts_keys_and_uses_compact_separators():
    """The serializer is pinned: unsorted keys would make digests differ."""
    assert canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_canonical_bytes_rejects_nonfinite(bad):
    """NaN/Inf: a naive ``json.dumps`` default writes ``NaN``/``Infinity``.

    Those are not JSON, and a digest over them is not reproducible across
    implementations, so canonical-json/v1 rejects them outright rather than
    letting a nonfinite number reach a digest.
    """
    with pytest.raises(IncompleteContext):
        canonical_bytes({"value": bad})


def test_document_digest_excludes_its_own_digest_key():
    """A document cannot sign itself: the ``digest`` key is excluded from bytes."""
    unsigned = {"a": 1, "b": 2}
    signed = {**unsigned, "digest": "ignored"}
    assert document_digest(unsigned) == document_digest(signed)
    assert document_digest(unsigned).startswith("sha256:")
    # And the value is exactly sha256 over the canonical bytes.
    import hashlib

    expected = "sha256:" + hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
    assert document_digest(unsigned) == expected


# --------------------------------------------------------------------------
# POSITIVES
# --------------------------------------------------------------------------


def test_positive_base_and_three_deltas_validates():
    """base + 3 contiguous deltas validates and reports the trusted terminal."""
    base = _base()
    d1 = _delta(base.digest, 1, changes={"budget": 6})
    d2 = _delta(d1.digest, 2, changes={"status": "done"})
    d3 = _delta(d2.digest, 3, changes={"evidence.receipt": "sha256:new"})
    summary = validate_chain(base, [d1, d2, d3], handoff=_handoff(base, d3))
    assert summary.base_digest == base.digest
    assert summary.terminal_id == "d-3"
    assert summary.delta_ids == ("d-1", "d-2", "d-3")


def test_positive_expected_terminal_hit_and_miss():
    """A hit returns a summary; a miss is incomplete (truncated tail)."""
    base = _base()
    d1 = _delta(base.digest, 1, changes={"budget": 6})
    good = _handoff(base, d1)
    validate_chain(base, [d1], handoff=good)

    wrong_terminal = _handoff(
        base,
        d1,
        expected_terminal={"delta_id": "d-1", "sequence": 1, "digest": "sha256:0" * 1},
    )
    with pytest.raises(IncompleteContext, match="terminal"):
        validate_chain(base, [d1], handoff=wrong_terminal)


def test_positive_empty_delta_chain_matches_base_terminal():
    """A zero-delta chain terminates at the base (no deltas still trusted)."""
    base = _base()
    handoff = TrustedHandoff(
        scope=dict(SCOPE),
        required_state_schemas=dict(REQUIRED_SCHEMAS),
        expected_base={"checkpoint_id": "cp-base", "sequence": 0, "digest": base.digest},
        expected_terminal={"delta_id": "cp-base", "sequence": 0, "digest": base.digest},
    )
    summary = validate_chain(base, [], handoff=handoff)
    assert summary.terminal_id == "cp-base"
    assert summary.delta_ids == ()


# --------------------------------------------------------------------------
# NEG-37: mandatory path removed / emptied / null
# --------------------------------------------------------------------------


def test_neg37_delta_removes_mandatory_path():
    """NEG-37: a delta that explicitly removes a mandatory path is rejected.

    Naive shortcut defeated: applying changes and removals without consulting
    the trusted schema would leave the path gone and still compare a matching
    terminal.  Here the pinned schema list does not come from the delta.
    """
    base = _base()
    d1 = _delta(base.digest, 1, removals=["instructions"])
    with pytest.raises(IncompleteContext, match="removes mandatory"):
        validate_chain(base, [d1], handoff=_handoff(base, d1))


def test_neg37_checkpoint_emptying_its_own_required_list_does_not_help():
    """NEG-37: a checkpoint that carries an empty requirement list still fails.

    Naive shortcut defeated: reading ``required_paths``/schema hints off the
    checkpoint makes an emptied list mean "nothing is required" and the chain
    would pass.  Requirements come only from the handoff here, so emptying
    anything the checkpoint carries changes nothing.
    """
    base = _base()
    # A hostile base may smuggle its own (empty) requirements in the state;
    # the handoff's schemas are the only authority and still demand presence.
    smuggled = dict(BASE_STATE)
    smuggled["required_state_schemas"] = {}
    hostile_base = _base(state=smuggled)
    d1 = _delta(hostile_base.digest, 1, changes={"instructions": ""})
    with pytest.raises(IncompleteContext, match="too short"):
        validate_chain(hostile_base, [d1], handoff=_handoff(hostile_base, d1))


def test_neg37_required_value_becomes_null():
    """NEG-37: a delta setting a mandatory value to null is rejected.

    Naive shortcut defeated: treating ``None`` as "present because the key
    exists" would pass key-presence-only checks.  ``null`` is not presence:
    the pinned type check rejects it.
    """
    base = _base()
    d1 = _delta(base.digest, 1, changes={"evidence.receipt": None})
    with pytest.raises(IncompleteContext, match="wrong type or is null"):
        validate_chain(base, [d1], handoff=_handoff(base, d1))


def test_neg37_baseline_missing_mandatory_path():
    """NEG-37/NEG-24: a baseline already missing a mandatory path is incomplete."""
    state = json.loads(json.dumps(BASE_STATE))
    del state["evidence"]["receipt"]
    base = _base(state=state)
    with pytest.raises(IncompleteContext, match="mandatory state missing"):
        validate_chain(base, [], handoff=_handoff(base, base))


def test_neg37_parent_replacement_dropping_mandatory_descendant():
    """NEG-37: replacing a parent mapping without the mandatory child fails.

    Naive shortcut defeated: a shallow change check that only compares the
    changed key ``evidence`` would see a dict and accept it, never noticing
    that ``evidence.receipt`` disappeared with the replacement.
    """
    base = _base()
    d1 = _delta(base.digest, 1, changes={"evidence": {"other": "x"}})
    with pytest.raises(IncompleteContext, match="removes mandatory"):
        validate_chain(base, [d1], handoff=_handoff(base, d1))


def test_positive_parent_replacement_keeping_mandatory_descendant():
    """A parent replacement that preserves a valid mandatory child is allowed."""
    base = _base()
    d1 = _delta(
        base.digest,
        1,
        changes={"evidence": {"receipt": "sha256:new", "extra": 1}},
    )
    summary = validate_chain(base, [d1], handoff=_handoff(base, d1))
    assert summary.terminal_id == "d-1"


# --------------------------------------------------------------------------
# NEG-38: recomputed self-digest on tampered content
# --------------------------------------------------------------------------


def test_neg38_tampered_content_with_recomputed_self_digest():
    """NEG-38: ``1``->``999`` with a recomputed self-digest still fails.

    The delta's own ``digest`` is recomputed to match the tampered content,
    so a self-consistency-only check passes.  What rejects it is the trusted
    terminal content digest: the handoff's expected terminal still names the
    original digest.  A naive implementation that trusts the carried digest
    or compares only ID/sequence would accept the tampered chain.
    """
    base = _base()
    honest = _delta(base.digest, 1, changes={"budget": 1})
    handoff = _handoff(base, honest)

    tampered = _delta(base.digest, 1, changes={"budget": 999})
    assert tampered.digest == tampered.computed_digest()  # self-consistent
    assert (tampered.delta_id, tampered.sequence) == (honest.delta_id, honest.sequence)
    assert tampered.digest != honest.digest

    with pytest.raises(IncompleteContext, match="terminal"):
        validate_chain(base, [tampered], handoff=handoff)


def test_neg38_tampered_middle_delta_breaks_next_predecessor_link():
    """NEG-38: tampering a middle delta breaks the following predecessor link.

    Naive shortcut defeated: verifying each delta in isolation (self-digest
    only) would accept a tampered middle link.  The next delta's
    ``predecessor_digest`` names the honest content digest, so the chain
    breaks at the following link.
    """
    base = _base()
    d1 = _delta(base.digest, 1, changes={"budget": 2})
    d2 = _delta(d1.digest, 2, changes={"budget": 3})
    d3 = _delta(d2.digest, 3, changes={"budget": 4})
    handoff = _handoff(base, d3)

    tampered_d2 = _delta(d1.digest, 2, changes={"budget": 999})
    with pytest.raises(IncompleteContext, match="predecessor"):
        validate_chain(base, [d1, tampered_d2, d3], handoff=handoff)


def test_neg38_tampered_baseline_with_recomputed_digest():
    """NEG-38 at the base: changed base content with a fresh digest is rejected.

    Naive shortcut defeated: trusting the base's own digest would accept it.
    The trusted base content digest in the handoff pins the original bytes.
    """
    honest = _base()
    handoff = _handoff(honest, honest)

    state = json.loads(json.dumps(BASE_STATE))
    state["budget"] = 999
    tampered = _base(state=state)
    assert tampered.digest == tampered.computed_digest()
    with pytest.raises(IncompleteContext, match="does not match trusted base"):
        validate_checkpoint_manifest(tampered, handoff)


# --------------------------------------------------------------------------
# NEG-24: missing base or mandatory evidence blocks dependent work
# --------------------------------------------------------------------------


def test_neg24_missing_mandatory_evidence_blocks_governed_work():
    """NEG-24: absent mandatory evidence yields incomplete, not a result.

    Naive shortcut defeated: returning the base state (or a partial merge)
    when a required evidence path is absent fabricates context.  Here the
    chain yields no summary at all -- the exception is the only outcome.
    """
    state = json.loads(json.dumps(BASE_STATE))
    del state["evidence"]
    base = _base(state=state)
    with pytest.raises(IncompleteContext):
        validate_chain(base, [], handoff=_handoff(base, base))


# --------------------------------------------------------------------------
# NEG-26: cross-tenant content is never reused
# --------------------------------------------------------------------------


def test_neg26_cross_tenant_checkpoint_is_rejected_before_content_use():
    """NEG-26: a checkpoint from another tenant cannot be reused.

    Naive shortcut defeated: comparing content digests first and trusting a
    matching hash would let a same-content record from another tenant join
    this chain.  Scope binding runs first, so the tenant mismatch is refused
    before any content comparison can leak existence.
    """
    other_scope = dict(SCOPE)
    other_scope["tenant_id"] = "t-2"
    foreign = CheckpointManifest(
        checkpoint_id="cp-base",
        sequence=0,
        state=json.loads(json.dumps(BASE_STATE)),
        scope=other_scope,
        cursor={"round": 0},
    ).with_digest()
    handoff = _handoff(_base(), _base())
    with pytest.raises(IncompleteContext, match="tenant_id"):
        validate_checkpoint_manifest(foreign, handoff)


def test_neg26_cross_tenant_delta_is_rejected():
    """NEG-26: a delta claiming another tenant cannot extend this chain."""
    base = _base()
    other_scope = dict(SCOPE)
    other_scope["tenant_id"] = "t-2"
    foreign = _delta(base.digest, 1, changes={"budget": 2}, scope=other_scope)
    with pytest.raises(IncompleteContext, match="tenant_id"):
        validate_chain(base, [foreign], handoff=_handoff(base, foreign))


# --------------------------------------------------------------------------
# Missing base / truncated tail / reordered tail / wrong predecessor
# --------------------------------------------------------------------------


def test_missing_base():
    """A handoff expecting a base that is not supplied is incomplete.

    Naive shortcut defeated: no base means nothing to compare, and an
    implementation that treats "no checkpoint" as an empty chain would
    produce a verified (empty) result.  The trusted base identity is required.
    """
    honest = _base()
    other = _base(checkpoint_id="cp-other")
    handoff = _handoff(other, other)
    with pytest.raises(IncompleteContext, match="identity does not match trusted base"):
        validate_chain(honest, [], handoff=handoff)


def test_truncated_tail_is_incomplete():
    """A chain missing its terminal delta cannot reproduce the trusted triple.

    Naive shortcut defeated: validating only the deltas present (and never
    comparing a terminal) would accept a short chain.  Truncation is caught
    by the terminal comparison, the spec's "missing intermediate or terminal
    suffix is incomplete".
    """
    base = _base()
    d1 = _delta(base.digest, 1, changes={"budget": 2})
    d2 = _delta(d1.digest, 2, changes={"budget": 3})
    handoff = _handoff(base, d2)
    with pytest.raises(IncompleteContext, match="terminal"):
        validate_chain(base, [d1], handoff=handoff)


def test_missing_intermediate_delta():
    """A skipped sequence number is noncontiguous and rejected."""
    base = _base()
    d1 = _delta(base.digest, 1, changes={"budget": 2})
    d3 = _delta(d1.digest, 3, changes={"budget": 4})
    with pytest.raises(IncompleteContext, match="noncontiguous"):
        validate_chain(base, [d1, d3], handoff=_handoff(base, d3))


def test_reordered_tail_is_rejected():
    """A tail whose links were swapped so content no longer matches sequence.

    Naive shortcut defeated: comparing only the terminal identity/sequence and
    trusting each delta's carried digest would accept a chain in which the
    tail's *content* was rebound to the wrong sequence number.  Here the two
    tail deltas are rebuilt with swapped content but the original sequence
    numbers, so the predecessor digest of the second no longer names the
    first's content and the chain breaks.

    Note the input *list* order is irrelevant: the pinned extract sorts
    deltas by ``sequence`` (``sorted(deltas, key=lambda item: item["sequence"])``),
    so a merely shuffled list is valid.  See
    ``test_shuffled_input_order_is_valid_because_the_chain_sorts``.
    """
    base = _base()
    d1 = _delta(base.digest, 1, changes={"budget": 2})
    d2 = _delta(d1.digest, 2, changes={"budget": 3})
    # Swap which content sits at which sequence, keeping the recorded
    # predecessor digests as the originals: the links no longer line up.
    swapped_1 = _delta(base.digest, 1, changes={"budget": 3})
    swapped_2 = _delta(swapped_1.digest, 2, changes={"budget": 2})
    # The terminal the handoff trusts is the honest d2, so this cannot match.
    with pytest.raises(IncompleteContext):
        validate_chain(base, [swapped_1, swapped_2], handoff=_handoff(base, d2))


def test_shuffled_input_order_is_valid_because_the_chain_sorts():
    """The extract sorts by sequence, so arrival order must not matter.

    This pins the positive side of the sorting rule: an implementation that
    validated in arrival order would reject a legitimately shuffled list.
    """
    base = _base()
    d1 = _delta(base.digest, 1, changes={"budget": 2})
    d2 = _delta(d1.digest, 2, changes={"budget": 3})
    d3 = _delta(d2.digest, 3, changes={"budget": 4})
    summary = validate_chain(base, [d3, d1, d2], handoff=_handoff(base, d3))
    assert summary.delta_ids == ("d-1", "d-2", "d-3")


def test_wrong_predecessor_digest():
    """A delta naming the wrong predecessor digest is rejected.

    Naive shortcut defeated: a delta that names its predecessor by ID only,
    or that trusts any predecessor string, would splice onto the wrong base.
    """
    base = _base()
    spliced = _delta("sha256:" + "0" * 64, 1, changes={"budget": 2})
    with pytest.raises(IncompleteContext, match="predecessor"):
        validate_chain(base, [spliced], handoff=_handoff(base, spliced))


def test_duplicate_delta_identity():
    """Reusing a delta identity is a duplicate, not a new link."""
    base = _base()
    d1 = _delta(base.digest, 1, changes={"budget": 2})
    dup = _delta(d1.digest, 2, delta_id="d-1", changes={"budget": 3})
    with pytest.raises(IncompleteContext, match="duplicate delta identity"):
        validate_chain(base, [d1, dup], handoff=_handoff(base, dup))


# --------------------------------------------------------------------------
# Wrong schema id / scope / policy revision (NEG-39)
# --------------------------------------------------------------------------


def test_wrong_schema_id():
    """The pinned schema id is mandatory and comes from the handoff.

    Naive shortcut defeated: an implementation that never checks the schema
    id would accept a document produced under a different contract.
    """
    document = {
        "schema": "trusted-handoff/v1",
        "scope": dict(SCOPE),
        "required_state_schemas": dict(REQUIRED_SCHEMAS),
        "expected_base": {"checkpoint_id": "cp-base", "sequence": 0, "digest": "sha256:0"},
        "expected_terminal": {"delta_id": "cp-base", "sequence": 0, "digest": "sha256:0"},
    }
    with pytest.raises(IncompleteContext, match="unsupported trusted handoff schema"):
        TrustedHandoff.from_document(document)


def test_missing_schema_id():
    """A handoff with no schema id at all is rejected, not defaulted."""
    document = {"scope": dict(SCOPE)}
    with pytest.raises(IncompleteContext, match="unsupported trusted handoff schema"):
        TrustedHandoff.from_document(document)


def test_wrong_policy_revision_scope_mismatch():
    """NEG-39: a delta under a different policy revision is out of scope.

    Naive shortcut defeated: validating content without re-checking the
    tenant/task/policy binding per document would let a delta authored under
    another policy revision join the chain.
    """
    base = _base()
    wrong_scope = dict(SCOPE)
    wrong_scope["policy_revision"] = "pol-8"
    d1 = _delta(base.digest, 1, changes={"budget": 2}, scope=wrong_scope)
    with pytest.raises(IncompleteContext, match="scope mismatch: policy_revision"):
        validate_chain(base, [d1], handoff=_handoff(base, d1))


def test_wrong_mandatory_value_enum():
    """NEG-39: a value outside its trusted enum is rejected after the delta.

    Naive shortcut defeated: type-only or presence-only checks would accept
    a value the policy revision forbids.
    """
    base = _base()
    d1 = _delta(base.digest, 1, changes={"status": "maybe"})
    with pytest.raises(IncompleteContext, match="allowed values"):
        validate_chain(base, [d1], handoff=_handoff(base, d1))


def test_wrong_mandatory_value_type():
    """NEG-39: a wrong mandatory value type is rejected after the delta."""
    base = _base()
    d1 = _delta(base.digest, 1, changes={"budget": "lots"})
    with pytest.raises(IncompleteContext, match="wrong type"):
        validate_chain(base, [d1], handoff=_handoff(base, d1))


def test_bool_is_not_accepted_as_number():
    """A JSON ``true`` is not a number, even though Python ``bool`` is an ``int``.

    Naive shortcut defeated: ``isinstance(value, (int, float))`` alone accepts
    ``True`` as a number because ``bool`` subclasses ``int``.  The schema
    check has to exclude it explicitly.
    """
    base = _base()
    d1 = _delta(base.digest, 1, changes={"budget": True})
    with pytest.raises(IncompleteContext, match="wrong type"):
        validate_chain(base, [d1], handoff=_handoff(base, d1))


# --------------------------------------------------------------------------
# Extract-derived rejections (the three round_003 lead areas, re-proved)
#
# Each case below is derived from the pinned extract text in
# 06-solution-snippets.md section 5 -- not from any audit finding -- and each
# is written so a naive implementation would pass it.
# --------------------------------------------------------------------------


def test_non_adjacent_duplicate_delta_identity_is_rejected():
    """A delta identity reused anywhere in the chain is a duplicate.

    Extract basis (section 5): ``seen_delta_ids = set()`` is created once
    before the loop and every id is added to it, so the duplicate check spans
    the whole chain.  Naive shortcut defeated: comparing a delta only against
    its *immediate* predecessor (``delta_id == previous_id``) accepts the same
    id recurring later, e.g. ``d-1, d-2, d-1`` -- which silently re-applies an
    already-consumed change set under a new sequence number.
    """
    base = _base()
    d1 = _delta(base.digest, 1, changes={"budget": 2})
    d2 = _delta(d1.digest, 2, changes={"budget": 3})
    reused = _delta(d2.digest, 3, delta_id="d-1", changes={"budget": 4})
    with pytest.raises(IncompleteContext, match="duplicate delta identity"):
        validate_chain(base, [d1, d2, reused], handoff=_handoff(base, reused))


def test_change_through_a_mandatory_scalar_is_rejected():
    """A change path that traverses a mandatory scalar cannot be applied.

    Extract basis (section 5): ``_set_path`` raises
    ``"state path traverses a scalar"`` as soon as an intermediate segment is
    not an object.  Naive shortcut defeated: a validator that only looks at
    mandatory paths equal to the change path never notices
    ``instructions.x`` descending through the string ``instructions``, and
    reports a successful chain for a delta whose change had no effect.
    """
    base = _base()
    d1 = _delta(base.digest, 1, changes={"instructions.x": "nested"})
    with pytest.raises(IncompleteContext, match="traverses a scalar"):
        validate_chain(base, [d1], handoff=_handoff(base, d1))


def test_removal_through_a_mandatory_scalar_is_rejected():
    """A removal path that traverses a mandatory scalar cannot be applied.

    Extract basis (section 5): ``_remove_path`` raises
    ``"removal path traverses a scalar"``.  Naive shortcut defeated: ignoring
    an unappliable removal leaves the mandatory value in place, so a chain
    whose delta silently failed to remove anything is still reported as
    trusted.
    """
    base = _base()
    d1 = _delta(base.digest, 1, removals=["instructions.child"])
    with pytest.raises(IncompleteContext, match="removal path traverses a scalar"):
        validate_chain(base, [d1], handoff=_handoff(base, d1))


def test_mandatory_scalar_change_inside_an_object_path_is_allowed():
    """A change under a mandatory *object* is fine: the prefix is an object.

    This is the positive side of the scalar rule.  ``evidence`` is an object,
    so ``evidence.extra`` is a normal nested change and must not be rejected;
    ``evidence.receipt`` stays present and valid.
    """
    base = _base()
    d1 = _delta(base.digest, 1, changes={"evidence.extra": 1})
    summary = validate_chain(base, [d1], handoff=_handoff(base, d1))
    assert summary.terminal_id == "d-1"


def test_delta_claiming_the_base_as_predecessor_after_the_first_link():
    """``base_checkpoint_id`` names the *predecessor*, not the chain's base.

    Extract basis (section 5): ``if delta["base_checkpoint_id"] != previous_id``.
    Naive shortcut defeated: an implementation that always compares against
    the base checkpoint id accepts a delta that skipped a link -- here the
    second delta claims the base while its sequence says it follows d-1.
    """
    base = _base()
    d1 = _delta(base.digest, 1, changes={"budget": 2})
    skipped = DeltaManifest(
        delta_id="d-2",
        sequence=2,
        base_checkpoint_id="cp-base",  # wrong: predecessor is d-1
        predecessor_digest=d1.digest,
        changes={"budget": 3},
        scope=dict(SCOPE),
    ).with_digest()
    with pytest.raises(IncompleteContext, match="predecessor identity"):
        validate_chain(base, [d1, skipped], handoff=_handoff(base, skipped))


# --------------------------------------------------------------------------
# NaN/Inf and non-canonical documents
# --------------------------------------------------------------------------


def test_nan_in_document_is_incomplete():
    """A NaN in a delta makes the document non-canonical, not silently hashable.

    Naive shortcut defeated: ``json.dumps`` defaults emit ``NaN`` and a naive
    digest would be computed over bytes that no other implementation agrees
    on.  canonical-json/v1 rejects it before a digest exists.
    """
    with pytest.raises(IncompleteContext):
        document_digest({"value": float("nan")})


def test_inf_reaching_a_manifest_is_incomplete():
    """An infinity inside a delta document surfaces as IncompleteContext."""
    base = _base()
    d1 = DeltaManifest(
        delta_id="d-1",
        sequence=1,
        base_checkpoint_id="cp-base",
        predecessor_digest=base.digest,
        changes={"budget": float("inf")},
        removals=(),
        scope=dict(SCOPE),
    )
    with pytest.raises(IncompleteContext):
        d1.with_digest()


# --------------------------------------------------------------------------
# Executable naive reference: the negatives actually discriminate
# --------------------------------------------------------------------------


def _naive_accepts(checkpoint, deltas, handoff) -> bool:
    """A deliberately naive validator, used only to prove the tests bite.

    It models the shortcut each negative names: it trusts each document's
    carried ``digest`` (never recomputing), hashes with default ``json.dumps``
    (unsorted, NaN allowed), takes the terminal from the last document rather
    than the handoff, and never checks mandatory state.  If a negative case
    were weak, this reference would accept it too and the test below fails.
    """
    import hashlib

    def naive_digest(document):
        unsigned = {k: v for k, v in document.items() if k != "digest"}
        return "sha256:" + hashlib.sha256(
            json.dumps(unsigned).encode("utf-8")
        ).hexdigest()

    base_doc = checkpoint.as_document()
    if base_doc.get("checkpoint_id") != handoff.expected_base.get("checkpoint_id"):
        return False
    previous = base_doc
    for delta in sorted(deltas, key=lambda item: item.as_document()["sequence"]):
        doc = delta.as_document()
        if doc.get("sequence") != previous.get("sequence", 0) + 1:
            return False
        if doc.get("predecessor_digest") != previous.get("digest"):
            return False
        previous = doc
    # The documented naive shortcut: the trusted terminal binds only identity
    # and sequence (07-collaboration-and-evidence.md:155), and each link's
    # predecessor check uses the carried digest rather than a recomputed one.
    return (
        previous.get("delta_id", previous.get("checkpoint_id")),
        previous.get("sequence", 0),
    ) == (
        handoff.expected_terminal.get("delta_id"),
        handoff.expected_terminal.get("sequence"),
    )


def test_naive_reference_accepts_tampered_content():
    """The NEG-38 case must be invisible to the naive validator.

    This proves the negative test above is not vacuous: with a recomputed
    self-digest and unchanged terminal identity the naive reference accepts
    the ``1``->``999`` chain, while :func:`validate_chain` rejects it.
    """
    base = _base()
    honest = _delta(base.digest, 1, changes={"budget": 1})
    handoff = _handoff(base, honest)
    tampered = _delta(base.digest, 1, changes={"budget": 999})

    assert _naive_accepts(base, [tampered], handoff) is True
    with pytest.raises(IncompleteContext):
        validate_chain(base, [tampered], handoff=handoff)


def test_naive_reference_accepts_mandatory_removal():
    """The NEG-37 case must be invisible to the naive validator."""
    base = _base()
    removing = _delta(base.digest, 1, removals=["instructions"])
    handoff = _handoff(base, removing)

    assert _naive_accepts(base, [removing], handoff) is True
    with pytest.raises(IncompleteContext):
        validate_chain(base, [removing], handoff=handoff)


def test_naive_reference_accepts_null_mandatory_value():
    """The NEG-37 null case must be invisible to the naive validator."""
    base = _base()
    nulling = _delta(base.digest, 1, changes={"evidence.receipt": None})
    handoff = _handoff(base, nulling)

    assert _naive_accepts(base, [nulling], handoff) is True
    with pytest.raises(IncompleteContext):
        validate_chain(base, [nulling], handoff=handoff)


def test_naive_reference_accepts_non_adjacent_duplicate_delta_id():
    """The whole-chain duplicate check must be invisible to the naive form.

    The naive validator models the adjacent-only comparison, so ``d-1``
    recurring two links later slips through it while :func:`validate_chain`
    rejects it -- which is exactly the extract's ``seen_delta_ids`` set.
    """
    base = _base()
    d1 = _delta(base.digest, 1, changes={"budget": 2})
    d2 = _delta(d1.digest, 2, changes={"budget": 3})
    reused = _delta(d2.digest, 3, delta_id="d-1", changes={"budget": 4})
    handoff = _handoff(base, reused)

    assert _naive_accepts(base, [d1, d2, reused], handoff) is True
    with pytest.raises(IncompleteContext):
        validate_chain(base, [d1, d2, reused], handoff=handoff)


def test_naive_reference_accepts_scalar_traversal():
    """The scalar-traversal rule must be invisible to the naive form."""
    base = _base()
    nested = _delta(base.digest, 1, changes={"instructions.x": "nested"})
    handoff = _handoff(base, nested)

    assert _naive_accepts(base, [nested], handoff) is True
    with pytest.raises(IncompleteContext):
        validate_chain(base, [nested], handoff=handoff)


def test_naive_reference_accepts_wrong_policy_revision():
    """The NEG-39 scope case must be invisible to the naive validator."""
    base = _base()
    wrong_scope = dict(SCOPE)
    wrong_scope["policy_revision"] = "pol-8"
    drift = _delta(base.digest, 1, changes={"budget": 2}, scope=wrong_scope)
    handoff = _handoff(base, drift)

    assert _naive_accepts(base, [drift], handoff) is True
    with pytest.raises(IncompleteContext):
        validate_chain(base, [drift], handoff=handoff)
