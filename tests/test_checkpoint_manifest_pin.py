"""W-3 F12 pin conformance tests for the checkpoint/delta manifest library.

Task easybutt0n-171 leaf B (pack task T4.1), contract step 3: one test per
pinned rule, row for row with ``docs/CONFORMANCE.md`` (31 rules).  Every test
encodes its rule against the *pinned* manifest shape -- the envelope of pin
section 3.1 and the body field table of pin section 3.2 -- and is executed
against the library's public API (``validate_chain`` plus the
``lh_harness.checkpoint`` primitives) *before* any library change, so the red
baseline recorded in the PR is the gap.

Authoritative input (read-only): cogniziocompany/claude-code-teams, branch
cognizio-overseer, ``docs/v15/t0.3/w3-schema-version-compatibility-pin.md``
-- 3.1 envelope (L181-187), 3.2 body field table (L199-210), 4.2 terminal
content digest (L229-241), 4.3 predecessor digest links (L245-256), section 7
explicit removals (L293-304).  Acceptance semantics A1/A2:
``agents/orchestrator/plans/native-171-admission-contract-20261114.md``
L20-33.  Acceptance is by pointer to those sections; this file does not
restate them as its own rules.

Acceptance case ids of record (pin section 11.2) used here, and the rows they
name.  Only these ids appear in test names; rows with no applicable id carry
a plain descriptive name:

| Id        | Rows (CONFORMANCE.md order)                                  |
|-----------|--------------------------------------------------------------|
| NEG-39    | 3.1 policy_revision; 3.2 #4 cursor epoch                     |
| EDGE-20   | 3.1 schema_version; 3.2 #2 revision mirror; sec 7 validation |
| EDGE-17   | 3.1 base_digest (single-manifest scope)                      |
| POS-05    | 3.1 terminal_content_digest; 4.2 rules 1 and 2               |
| EDGE-16   | 3.1 predecessor_digest; 4.3 rule 1                           |
| EDGE-18   | 3.1 created_at; 3.1 producer (field set, not content volume) |
| EDGE-35   | 3.2 #3 config (immutable active-attempt config)              |
| NEG-25    | 4.3 rule 3 (broken link)                                     |
| POS-16    | 4.3 rules 2 and 4 (hash links; restore-side validation)      |
| NEG-38    | 4.2 rule 3 (digest mismatch refusal)                         |
| NEG-26    | 3.2 8b closed set; section 7 rows 1-7 (removed elements)     |
| (plain)   | 3.2 #1 task, #5 question, #6 action, #7 child, 8a coverage   |

Ids of record with no row in the five mapped spans: POS-28 and EDGE-19
(retention, section 9), NEG-24 (missing-base, section 4.1) and NEG-37
(truncated-content block, section 8).  Those spans are outside this leaf's
rule mapping, so no test here carries them; none was invented for them.

Design contracts this file fixes for the library (step 5 of the leaf
implements them; they are the step-3 target, written against the pin):

* A pinned manifest document carries exactly the pinned field set: envelope
  ``policy_revision``, ``schema_version``, ``base_digest``,
  ``terminal_content_digest``, ``predecessor_digest`` (null at the chain
  head), ``created_at``, ``producer``; body ``task``, ``revision``,
  ``config``, ``cursor``, ``question``, ``action``, ``child``.
* ``terminal_content_digest`` is ``"sha256:" + hexdigest`` over
  ``canonical_bytes`` (canonical-json/v1, the library's pinned serializer)
  of the full content minus the ``terminal_content_digest`` field itself.
  The hash function is the pin's implementation choice (4.2); these tests
  fix it to the library's existing sha256 + canonical-json/v1.
* The trusted side is the handoff document (A1: authoritative values live
  there, never in the manifest): ``accepted_schema_versions``,
  ``config_in_force``, ``epoch_in_force`` ride alongside the v2 handoff
  keys and are read by the pinned validation path.
* Refusal messages name their rule: the field or element name (for example
  "policy_revision", "predecessor_digest"); a removed element is reported
  as a removed element with its category (narrative, secret, latency,
  threshold, host, predecessor content, unlisted field); the trusted
  terminal mismatch keeps the existing "terminal content" wording.
* Scan order: the section 7 removed-element scan runs before the per-field
  shape checks, and within it name-pattern categories (secret, latency,
  threshold, host/session, predecessor content) win over the narrative
  fall-through (an unlisted key whose value is a free-form string), which
  wins over the generic unlisted/extension report.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from lh_harness.checkpoint import (
    ChainSummary,
    CheckpointManifest,
    IncompleteContext,
    TrustedHandoff,
    canonical_bytes,
    validate_chain,
    validate_checkpoint_manifest,
)

# --------------------------------------------------------------------------
# Pinned constants and trusted facts (the handoff side, never the manifest's)
# --------------------------------------------------------------------------

POLICY_REVISION = "pol-7"
TASK_ID = "T4.1"
PINNED_SCHEMA_VERSION = "w3-f12-manifest/v1"
UNACCEPTED_SCHEMA_VERSION = "w3-f12-manifest/v9"
EPOCH_IN_FORCE = 3
CONFIG_IN_FORCE = {"retention": "until-acceptance", "channel": "stable"}
TRUSTED_BASE_DIGEST = "sha256:" + hashlib.sha256(b"leaf-b-trusted-base").hexdigest()

ENVELOPE_FIELDS = (
    "policy_revision",
    "schema_version",
    "base_digest",
    "terminal_content_digest",
    "predecessor_digest",
    "created_at",
    "producer",
)
BODY_FIELDS = (
    "task",
    "revision",
    "config",
    "cursor",
    "question",
    "action",
    "child",
)


def _pinned_digest(content: dict) -> str:
    """The pinned terminal content digest: full content, minus itself."""
    unsigned = {
        key: value
        for key, value in content.items()
        if key != "terminal_content_digest"
    }
    return "sha256:" + hashlib.sha256(canonical_bytes(unsigned)).hexdigest()


def _pinned_content(**overrides) -> dict:
    """A pinned manifest's content, before its terminal_content_digest."""
    content = {
        "policy_revision": POLICY_REVISION,
        "schema_version": PINNED_SCHEMA_VERSION,
        "base_digest": TRUSTED_BASE_DIGEST,
        "predecessor_digest": None,
        "created_at": "2026-11-15T00:00:00Z",
        "producer": "lh-harness/checkpoint",
        "task": {
            "task_id": TASK_ID,
            "requirement_ids": ["F12"],
            "attempt_id": "att-1",
        },
        "revision": {
            "policy_revision": POLICY_REVISION,
            "schema_version": PINNED_SCHEMA_VERSION,
            "source_revision": "35ea475f",
        },
        "config": dict(CONFIG_IN_FORCE),
        "cursor": {"scope": "leaf-b", "position": "round-4", "epoch": EPOCH_IN_FORCE},
        "question": None,
        "action": {
            "action_id": "act-1",
            "kind": "checkpoint",
            "arguments_digest": "sha256:" + "1" * 64,
            "dispatch_state": "completed",
            "receipt_ref": "rcpt-1",
        },
        "child": None,
    }
    content.update(overrides)
    return content


def _head(**overrides) -> dict:
    """A complete, digest-correct pinned chain head (a manifest document)."""
    content = _pinned_content(**overrides)
    content["terminal_content_digest"] = _pinned_digest(content)
    return content


def _delta(predecessor_digest, n, **overrides) -> dict:
    """A complete, digest-correct pinned delta following ``predecessor_digest``."""
    # An explicit override (a null predecessor_digest on a delta) must win,
    # so the default is injected only when the caller did not supply one.
    overrides.setdefault("predecessor_digest", predecessor_digest)
    content = _pinned_content(
        action={
            "action_id": f"act-{n + 1}",
            "kind": "checkpoint",
            "arguments_digest": "sha256:" + f"{n + 1}" * 64,
            "dispatch_state": "completed",
            "receipt_ref": f"rcpt-{n + 1}",
        },
        cursor={
            "scope": "leaf-b",
            "position": f"round-{4 + n}",
            "epoch": EPOCH_IN_FORCE,
        },
        **overrides,
    )
    content["terminal_content_digest"] = _pinned_digest(content)
    return content


def _handoff(terminal_digest, *, config_in_force=None) -> TrustedHandoff:
    """The trusted handoff for the pinned plane.

    The pinned trusted facts (accepted schema versions, the config in force,
    the epoch in force) ride alongside the v2 handoff keys: they are the A1
    authority for those values, and they never come from the manifest.
    """
    document = {
        "schema": "trusted-handoff/v2",
        "scope": {
            "tenant_id": "t-1",
            "task_id": TASK_ID,
            "policy_revision": POLICY_REVISION,
        },
        "required_state_schemas": {},
        "expected_base": {
            "checkpoint_id": "cp-base",
            "sequence": 0,
            "digest": TRUSTED_BASE_DIGEST,
        },
        "expected_terminal": {"digest": terminal_digest},
        "accepted_schema_versions": [PINNED_SCHEMA_VERSION],
        "config_in_force": dict(
            config_in_force if config_in_force is not None else CONFIG_IN_FORCE
        ),
        "epoch_in_force": EPOCH_IN_FORCE,
    }
    return TrustedHandoff.from_document(document)


def _head_handoff(head: dict) -> TrustedHandoff:
    """A handoff whose trusted terminal is this single head manifest."""
    return _handoff(head["terminal_content_digest"])


# --------------------------------------------------------------------------
# Re-slice B: RULE_STATUS -- the state of every pin test on the adapter
#
# Each adapted test still builds its document from the pinned fixture and
# still applies the same mutation it always did; the mutated document is
# mapped through ``_adapt_pinned_to_library`` and validated exactly as the
# positive control validates (``validate_checkpoint_manifest(adapted,
# _adapter_handoff(adapted))``; ``validate_chain(adapted, [], ...)`` where
# the pin asserted on the returned summary).  So a red adapted test means
# the library itself does not enforce the pinned rule, never the shape.
#
# * ``ENFORCED`` -- the adapted test passes.
# * ``GAP: ...`` -- the field crosses the boundary (it rides in ``state``
#   or the cursor slot) but manifest.py has no rule for it, so the adapted
#   test still fails; the test carries ``xfail(strict=True)`` with the same
#   reason and the reason names the manifest.py lines the rule would live
#   at.
# * ``ADAPTER-LIMIT: ...`` -- a leg cannot be expressed through the fixed
#   boundary at all (the adapter lifts or recomputes the pinned field, or
#   the pin's refusal wording belongs to a plane the boundary cannot
#   reach).  The test stays red with no xfail.
#
# Rows land batch by batch, in file order.
# --------------------------------------------------------------------------

RULE_STATUS = {
    # Batch 1: the 3.1 envelope rules (7).
    "test_neg39_envelope_policy_revision_mandatory_and_stale_revision_refused": (
        "ADAPTER-LIMIT: the missing-policy_revision leg cannot cross the "
        "boundary (the adapter lifts policy_revision into a scope key, "
        "KeyError before validation); the stale leg is enforced by the "
        "native scope binding (manifest.py:271-272)"
    ),
    "test_edge20_envelope_schema_version_mandatory_and_version_compatibility_enforced": (
        "GAP: no rule requires state.schema_version or gates its version; "
        "would live at require_mandatory (manifest.py:481-484)"
    ),
    "test_edge17_envelope_base_digest_mandatory_and_binds_the_trusted_base": (
        "GAP: no rule reads state.base_digest (the native binding at "
        "manifest.py:478-479 compares the recomputed document digest); a "
        "state-field rule would sit at manifest.py:481-484"
    ),
    "test_pos05_envelope_terminal_content_digest_mandatory": (
        "ADAPTER-LIMIT: the boundary recomputes the digest and never "
        "carries the pin digest value, so a missing or wrong "
        "terminal_content_digest cannot reach validation (native "
        "self-digest rule manifest.py:475-477)"
    ),
    "test_edge16_envelope_predecessor_digest_null_only_for_the_chain_head": (
        "GAP: no rule reads state.predecessor_digest; the native link rule "
        "(manifest.py:518-519) is unreachable because every pinned "
        "manifest maps to a base checkpoint; a state rule would sit at "
        "manifest.py:481-484"
    ),
    # Batch 2: the remaining 3.1 envelope rules (2) and 3.2 rules 1-3.
    "test_edge18_envelope_created_at_mandatory": (
        "GAP: no rule requires state.created_at -- null is not presence; "
        "would live at require_mandatory (manifest.py:481-484)"
    ),
    "test_edge18_envelope_producer_mandatory": (
        "GAP: no rule requires state.producer -- null is not presence; "
        "would live at require_mandatory (manifest.py:481-484)"
    ),
    "test_body_task_object_mandatory_with_stable_ids": (
        "ADAPTER-LIMIT: the deleted-task leg cannot cross the boundary "
        "(the adapter lifts task_id out of it, KeyError); the partial-task "
        "and foreign-task_id legs ride inside state.task with no rule "
        "reading them (a state rule would sit at manifest.py:481-484); "
        "native scope binding manifest.py:268-272 reads only the lifted key"
    ),
    "test_edge20_body_revision_mirrors_the_envelope_exactly": (
        "GAP: no rule reads state.revision or compares its mirror fields "
        "to the envelope; would live at require_mandatory + a mirror check "
        "(manifest.py:481-484)"
    ),
    "test_edge35_body_config_mandatory_named_and_matching_config_in_force": (
        "GAP: no rule requires state.config or checks it against the "
        "trusted config in force; would live at require_mandatory + a "
        "trusted-value check (manifest.py:481-484)"
    ),
    # Batch 3: the 3.2 rules 4-7 and 8a.
    "test_neg39_body_cursor_requires_epoch_and_stale_epoch_is_refused": (
        "GAP: the cursor crosses in the library's dedicated slot "
        "(manifest.py:121) but no rule checks its shape or epoch; would "
        "live beside require_mandatory (manifest.py:481-484)"
    ),
    "test_body_question_conditional_and_well_formed": (
        "GAP: no rule reads state.question -- the malformed legs ride as "
        "state content and the well-formed legs validate; would live at "
        "require_mandatory + a shape check (manifest.py:481-484)"
    ),
    "test_body_action_mandatory_with_stable_action_id": (
        "GAP: no rule requires state.action or its pinned subfields; would "
        "live at require_mandatory (manifest.py:481-484)"
    ),
    "test_body_child_conditional_and_well_formed": (
        "GAP: no rule reads state.child -- the malformed leg rides as "
        "state content and the well-formed legs validate; would live at "
        "require_mandatory + a shape check (manifest.py:481-484)"
    ),
    "test_body_field_set_is_fully_digest_covered": (
        "ENFORCED: every body field rides inside state (or the cursor "
        "slot) of the digested document, so each mutation changes the "
        "recomputed digest and the trusted-base comparison refuses it "
        "(manifest.py:475-479)"
    ),
    # Batch 4: the 3.2 closed-set rule and the 4.2 digest rules.
    "test_neg26_body_field_set_is_closed_unlisted_field_refused": (
        "GAP: the closed-set scan is not native -- an unlisted field "
        "crosses inside state with no rule refusing it; would live at a "
        "state-field allowlist beside require_mandatory "
        "(manifest.py:481-484)"
    ),
    "test_pos05_digest_covers_full_content_except_the_digest_field_itself": (
        "ADAPTER-LIMIT: the honest leg is enforced by the native chain "
        "validation (manifest.py:475-479), but the garbage-digest leg, "
        "while refused in effect (the garbage value rides inside state "
        "and changes the recomputed digest), is refused by the "
        "trusted-base comparison (manifest.py:478-479) with no rule "
        "naming terminal_content_digest, and the missing leg is dropped "
        "by the boundary before validation (native self-digest rule "
        "manifest.py:475-477)"
    ),
    "test_pos05_digest_deterministic_under_reordering_and_reformatting": (
        "ENFORCED: both pin renderings adapt to the same library document "
        "and validate against the same recomputed digest; the boundary's "
        "canonical-json/v1 serializer (manifest.py:70-98) is what makes "
        "re-ordering and re-formatting digest-neutral"
    ),
    "test_neg38_any_body_field_change_changes_the_digest_and_only_content_changes_do": (
        "ENFORCED: the changed receipt_ref rides inside state, the "
        "recomputed digest differs and the trusted-base comparison "
        "refuses it; the re-rendered pin document adapts to the same "
        "bytes and validates (manifest.py:475-479)"
    ),
    # Batch 5: the 4.3 predecessor-link rules.
    "test_edge16_predecessor_digest_carried_by_every_manifest_and_null_at_head": (
        "GAP: no rule reads state.predecessor_digest -- the head/delta "
        "distinction is erased by the boundary (every pinned manifest "
        "maps to a base checkpoint), and the malformed legs ride as state "
        "content; a state rule would sit at manifest.py:481-484, the "
        "native link rule is manifest.py:518-519"
    ),
    "test_pos16_chain_is_hash_linked_by_predecessor_terminal_digests": (
        "ENFORCED: the pin-side linking assertions hold on the fixture "
        "and the adapted terminal manifest validates against its own "
        "recomputed digest and trusted base (manifest.py:475-479); the "
        "native hash-link rule (manifest.py:516-519) applies to library "
        "deltas, which this boundary never produces"
    ),
    "test_neg25_broken_predecessor_link_is_refused": (
        "GAP: a broken predecessor link rides inside state with no rule "
        "refusing it; the native link rule (manifest.py:518-519) is "
        "unreachable because the boundary maps every pinned manifest to a "
        "base checkpoint; a state rule would sit at manifest.py:481-484"
    ),
    "test_pos16_restore_side_validation_covers_content_predecessor_and_terminal": (
        "ADAPTER-LIMIT: the content-tamper leg is refused in effect (the "
        "mutated state changes the recomputed digest, manifest.py:475-479) "
        "but by the trusted-base comparison with no predecessor/terminal "
        "wording; the stored-digest-tamper leg is dropped by the boundary "
        "(the digest is recomputed); the native terminal rule is "
        "manifest.py:632-633"
    ),
    # Batch 6: the section 7 removed-element rules (8).
    "test_neg26_removed_narrative_text_is_refused": (
        "GAP: no rule scans state values for narrative text; would live at "
        "a removed-element scan beside require_mandatory "
        "(manifest.py:481-484)"
    ),
    "test_neg26_removed_secret_material_is_refused": (
        "GAP: no rule scans state values for secret material; would live "
        "at a removed-element scan beside require_mandatory "
        "(manifest.py:481-484)"
    ),
    "test_neg26_removed_latency_or_timing_values_are_refused": (
        "GAP: no rule scans state values for latency or timing values; "
        "would live at a removed-element scan beside require_mandatory "
        "(manifest.py:481-484)"
    ),
    "test_neg26_removed_threshold_or_slo_values_are_refused": (
        "GAP: no rule scans state values for threshold or SLO values; "
        "would live at a removed-element scan beside require_mandatory "
        "(manifest.py:481-484)"
    ),
    "test_neg26_removed_host_or_session_identifiers_are_refused": (
        "GAP: no rule scans state keys/values for host or session "
        "identifiers; would live at a removed-element scan beside "
        "require_mandatory (manifest.py:481-484)"
    ),
    "test_neg26_removed_embedded_predecessor_content_is_refused": (
        "GAP: no rule refuses an embedded predecessor document inside "
        "state; would live at a removed-element scan beside "
        "require_mandatory (manifest.py:481-484)"
    ),
    "test_neg26_removed_unlisted_or_extension_field_is_refused": (
        "GAP: the closed-set scan is not native -- an unlisted or "
        "extension field crosses inside state and validates, and the "
        "future-schema leg has no version gate; would live at a "
        "state-field allowlist beside require_mandatory "
        "(manifest.py:481-484)"
    ),
    "test_edge20_removal_is_validation_not_deletion": (
        "GAP: no removed-element rule fires, so the offending document is "
        "not refused and the non-mutation assertion never runs; the "
        "validation-not-deletion rule would live beside "
        "require_mandatory (manifest.py:481-484)"
    ),
}


# --------------------------------------------------------------------------
# 3.1 The manifest envelope (pin L181-187) -- 7 rules
# --------------------------------------------------------------------------


def test_neg39_envelope_policy_revision_mandatory_and_stale_revision_refused():
    """3.1 L181: policy_revision is required; a revision not in force is stale.

    The revision in force comes from the trusted handoff, never from the
    manifest (A1): a manifest cannot declare itself current.  Adapted: the
    revision is lifted into a scope key, so the stale leg must be refused
    by the native scope binding and the missing leg cannot cross the
    boundary at all (ADAPTER-LIMIT; see RULE_STATUS).
    """
    head = _head()
    handoff = _adapter_handoff(_adapt_pinned_to_library(head))

    missing = _pinned_content()
    del missing["policy_revision"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="policy_revision"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(missing), handoff)

    stale = _head(
        policy_revision="pol-8",
        revision={
            "policy_revision": "pol-8",
            "schema_version": PINNED_SCHEMA_VERSION,
            "source_revision": "35ea475f",
        },
    )
    with pytest.raises(IncompleteContext, match="policy_revision"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(stale), handoff)


def test_edge20_envelope_schema_version_mandatory_and_version_compatibility_enforced():
    """3.1 L182 with section 2: schema_version is required and must be accepted.

    A manifest is judged against the version it declares, and only versions
    the trusted side accepts can validate at all.  Adapted: schema_version
    rides inside ``state``, where the library has no rule for it (GAP; see
    RULE_STATUS).
    """
    head = _head()
    handoff = _adapter_handoff(_adapt_pinned_to_library(head))

    missing = _pinned_content()
    del missing["schema_version"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="schema_version"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(missing), handoff)

    unaccepted = _head(schema_version=UNACCEPTED_SCHEMA_VERSION)
    with pytest.raises(IncompleteContext, match="schema_version"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(unaccepted), handoff)


def test_edge17_envelope_base_digest_mandatory_and_binds_the_trusted_base():
    """3.1 L183: base_digest is required and names the trusted base content.

    Single-manifest scope (pin 11.2 EDGE-17): with no deltas, the trusted
    base digest and the head's terminal content digest are the same trust
    anchor, and the head must validate against it.  Adapted: the binding
    the library performs is the recomputed document digest
    (manifest.py:478-479); the carried state field has no rule (GAP; see
    RULE_STATUS).
    """
    head = _head()
    assert head["base_digest"] == TRUSTED_BASE_DIGEST
    adapted_head = _adapt_pinned_to_library(head)
    handoff = _adapter_handoff(adapted_head)
    summary = validate_chain(adapted_head, [], handoff=handoff)
    assert isinstance(summary, ChainSummary)
    assert summary.terminal_digest == adapted_head["digest"]

    wrong_base = _head(base_digest="sha256:" + "c" * 64)
    with pytest.raises(IncompleteContext, match="base_digest"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(wrong_base), handoff)

    missing = _pinned_content()
    del missing["base_digest"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="base_digest"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(missing), handoff)


def test_pos05_envelope_terminal_content_digest_mandatory():
    """3.1 L184: every manifest carries terminal_content_digest, verified.

    A missing or wrongly valued terminal_content_digest is refused; the
    value is recomputed by the library, never taken from the document.
    Adapted: the boundary recomputes the digest under the library's name
    and never carries the pin's value, so neither defect can reach
    validation (ADAPTER-LIMIT; see RULE_STATUS).
    """
    head = _head()
    handoff = _adapter_handoff(_adapt_pinned_to_library(head))

    missing = _pinned_content()
    missing["terminal_content_digest"] = _pinned_digest(missing)
    del missing["terminal_content_digest"]
    with pytest.raises(IncompleteContext, match="terminal_content_digest"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(missing), handoff)

    wrong = dict(head)
    wrong["terminal_content_digest"] = "sha256:" + "d" * 64
    with pytest.raises(IncompleteContext, match="terminal_content_digest"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(wrong), handoff)


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no rule reads state.predecessor_digest; the native link rule "
    "(manifest.py:518-519) is unreachable, a state rule would sit at "
    "manifest.py:481-484",
)
def test_edge16_envelope_predecessor_digest_null_only_for_the_chain_head():
    """3.1 L185: predecessor_digest is required on every manifest.

    Null is the chain head's mark: a head carrying a non-null predecessor
    digest, or a delta carrying null, is refused.  Adapted: every pinned
    manifest maps to a base checkpoint, so the field rides in ``state``
    with no rule reading it (GAP; see RULE_STATUS).
    """
    head = _head()
    handoff = _adapter_handoff(_adapt_pinned_to_library(head))

    missing = _pinned_content()
    del missing["predecessor_digest"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="predecessor_digest"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(missing), handoff)

    head_with_link = _head(predecessor_digest=head["terminal_content_digest"])
    with pytest.raises(IncompleteContext, match="predecessor_digest"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(head_with_link), handoff)

    d1 = _delta(head["terminal_content_digest"], 1, predecessor_digest=None)
    with pytest.raises(IncompleteContext, match="predecessor_digest"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(d1), handoff)


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no rule requires state.created_at (would live at "
    "require_mandatory, manifest.py:481-484)",
)
def test_edge18_envelope_created_at_mandatory():
    """3.1 L186: created_at is required; null is not presence.

    Completeness requires the field set, not a content volume (pin 11.2
    EDGE-18): a manifest with substantial content but no production time is
    still incomplete.  Adapted: created_at rides inside ``state``, where
    the library has no presence rule (GAP; see RULE_STATUS).
    """
    head = _head()
    handoff = _adapter_handoff(_adapt_pinned_to_library(head))

    missing = _pinned_content()
    del missing["created_at"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="created_at"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(missing), handoff)

    nulled = _head(created_at=None)
    with pytest.raises(IncompleteContext, match="created_at"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(nulled), handoff)


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no rule requires state.producer (would live at "
    "require_mandatory, manifest.py:481-484)",
)
def test_edge18_envelope_producer_mandatory():
    """3.1 L187: producer is required; null is not presence.

    Adapted: producer rides inside ``state``, where the library has no
    presence rule (GAP; see RULE_STATUS).
    """
    head = _head()
    handoff = _adapter_handoff(_adapt_pinned_to_library(head))

    missing = _pinned_content()
    del missing["producer"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="producer"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(missing), handoff)

    nulled = _head(producer=None)
    with pytest.raises(IncompleteContext, match="producer"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(nulled), handoff)


def test_body_task_object_mandatory_with_stable_ids():
    """3.2 #1 L199: task is required, with task_id, requirement_ids, attempt_id.

    task_id binds to the trusted scope (A1: the binding comes from the
    handoff, not from the manifest's own claim).  Adapted: task_id is
    lifted to a scope key; the deleted-task leg cannot cross the boundary
    and the partial/foreign shapes ride inside state.task with no rule
    (ADAPTER-LIMIT; see RULE_STATUS).
    """
    head = _head()
    handoff = _adapter_handoff(_adapt_pinned_to_library(head))

    missing = _pinned_content()
    del missing["task"]
    with pytest.raises(IncompleteContext, match="task"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(missing), handoff)

    partial = _head(task={"task_id": TASK_ID, "attempt_id": "att-1"})
    with pytest.raises(IncompleteContext, match="task"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(partial), handoff)

    foreign = _head(
        task={
            "task_id": "T9.9",
            "requirement_ids": ["F12"],
            "attempt_id": "att-1",
        }
    )
    with pytest.raises(IncompleteContext, match="task"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(foreign), handoff)


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no rule reads state.revision or checks its mirror fields "
    "(would live at require_mandatory + mirror check, manifest.py:481-484)",
)
def test_edge20_body_revision_mirrors_the_envelope_exactly():
    """3.2 #2 L200: revision is required and mirrors policy_revision and
    schema_version from the envelope, plus source_revision.

    A mirror that disagrees with the envelope makes the manifest invalid.
    Adapted: revision rides inside ``state`` with no rule reading it (GAP;
    see RULE_STATUS).
    """
    head = _head()
    handoff = _adapter_handoff(_adapt_pinned_to_library(head))

    missing = _pinned_content()
    del missing["revision"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="revision"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(missing), handoff)

    disagreeing_policy = _head(
        revision={
            "policy_revision": "pol-8",
            "schema_version": PINNED_SCHEMA_VERSION,
            "source_revision": "35ea475f",
        }
    )
    with pytest.raises(IncompleteContext, match="revision"):
        validate_checkpoint_manifest(
            _adapt_pinned_to_library(disagreeing_policy), handoff
        )

    disagreeing_version = _head(
        revision={
            "policy_revision": POLICY_REVISION,
            "schema_version": UNACCEPTED_SCHEMA_VERSION,
            "source_revision": "35ea475f",
        }
    )
    with pytest.raises(IncompleteContext, match="revision"):
        validate_checkpoint_manifest(
            _adapt_pinned_to_library(disagreeing_version), handoff
        )

    no_source = _head(
        revision={
            "policy_revision": POLICY_REVISION,
            "schema_version": PINNED_SCHEMA_VERSION,
        }
    )
    with pytest.raises(IncompleteContext, match="revision"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(no_source), handoff)


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no rule requires state.config or checks it against the "
    "trusted config in force (would live at require_mandatory + trusted-value "
    "check, manifest.py:481-484)",
)
def test_edge35_body_config_mandatory_named_and_matching_config_in_force():
    """3.2 #3 L201 with section 5: config is required as a named value map.

    The active attempt's config is immutable underneath a written manifest
    (pin 11.2 EDGE-35): a manifest whose config keys or values differ from
    the config in force -- which lives in the trusted handoff, never in the
    manifest (A1) -- is refused.  Adapted: config rides inside ``state``
    with no rule reading it, and the adapter handoff carries no config in
    force to check against (GAP; see RULE_STATUS).
    """
    head = _head()
    handoff = _adapter_handoff(_adapt_pinned_to_library(head))

    missing = _pinned_content()
    del missing["config"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="config"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(missing), handoff)

    extra_key = _head(config={"retention": "until-acceptance", "channel": "stable",
                              "retries": 3})
    with pytest.raises(IncompleteContext, match="config"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(extra_key), handoff)

    changed_value = _head(config={"retention": "forever", "channel": "stable"})
    with pytest.raises(IncompleteContext, match="config"):
        validate_checkpoint_manifest(
            _adapt_pinned_to_library(changed_value), handoff
        )


# --------------------------------------------------------------------------
# 3.2 The mandatory value schema, manifest body (pin L199-210) -- 9 rules
# --------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no rule checks the cursor slot's shape or epoch (would "
    "live beside require_mandatory, manifest.py:481-484)",
)
def test_neg39_body_cursor_requires_epoch_and_stale_epoch_is_refused():
    """3.2 #4 L202: cursor is required with scope, position and epoch.

    A cursor with no epoch, or with a superseded epoch, is stale and must be
    rejected; the epoch in force comes from the trusted handoff (A1).
    Adapted: the cursor crosses in the library's dedicated slot, where no
    rule reads it (GAP; see RULE_STATUS).
    """
    head = _head()
    handoff = _adapter_handoff(_adapt_pinned_to_library(head))

    missing = _pinned_content()
    del missing["cursor"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="cursor"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(missing), handoff)

    no_epoch = _head(cursor={"scope": "leaf-b", "position": "round-4"})
    with pytest.raises(IncompleteContext, match="cursor"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(no_epoch), handoff)

    stale_epoch = _head(
        cursor={"scope": "leaf-b", "position": "round-4", "epoch": EPOCH_IN_FORCE - 1}
    )
    with pytest.raises(IncompleteContext, match="stale"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(stale_epoch), handoff)


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no rule reads state.question (would live at "
    "require_mandatory + shape check, manifest.py:481-484)",
)
def test_body_question_conditional_and_well_formed():
    """3.2 #5 L203: question is null or an object with the pinned fields.

    Present exactly when a question awaits an answer; malformed question
    objects (missing question_id, or a state outside the pinned set) are
    refused.  ``None`` is valid when there is no open question.  Adapted:
    question rides inside ``state`` with no rule reading it (GAP; see
    RULE_STATUS).
    """
    head = _head()
    handoff = _adapter_handoff(_adapt_pinned_to_library(head))

    partial = _head(
        question={
            "asker": "executor",
            "answerer": "manager",
            "request_scope": "leaf-b",
            "state": "open",
            "answer": None,
        }
    )
    with pytest.raises(IncompleteContext, match="question"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(partial), handoff)

    bad_state = _head(
        question={
            "question_id": "q-1",
            "asker": "executor",
            "answerer": "manager",
            "request_scope": "leaf-b",
            "state": "limbo",
            "answer": None,
        }
    )
    with pytest.raises(IncompleteContext, match="question"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(bad_state), handoff)

    answered = _head(
        question={
            "question_id": "q-1",
            "asker": "executor",
            "answerer": "manager",
            "request_scope": "leaf-b",
            "state": "answered",
            "answer": "proceed",
        }
    )
    summary = validate_chain(
        _adapt_pinned_to_library(answered), [], handoff=handoff
    )
    assert summary.terminal_digest == _adapt_pinned_to_library(answered)["digest"]


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no rule requires state.action or its pinned subfields "
    "(would live at require_mandatory, manifest.py:481-484)",
)
def test_body_action_mandatory_with_stable_action_id():
    """3.2 #6 L204: action is required with action_id, kind,
    arguments_digest, dispatch_state and receipt_ref.

    Adapted: action rides inside ``state`` with no rule reading it (GAP;
    see RULE_STATUS).
    """
    head = _head()
    handoff = _adapter_handoff(_adapt_pinned_to_library(head))

    missing = _pinned_content()
    del missing["action"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="action"):
        adapted_missing = _adapt_pinned_to_library(missing)
        validate_checkpoint_manifest(
            adapted_missing, _adapter_handoff(adapted_missing)
        )

    partial = _head(
        action={
            "action_id": "act-1",
            "kind": "checkpoint",
            "dispatch_state": "completed",
            "receipt_ref": "rcpt-1",
        }
    )
    with pytest.raises(IncompleteContext, match="action"):
        adapted_partial = _adapt_pinned_to_library(partial)
        validate_checkpoint_manifest(
            adapted_partial, _adapter_handoff(adapted_partial)
        )


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no rule reads state.child (would live at require_mandatory "
    "+ shape check, manifest.py:481-484)",
)
def test_body_child_conditional_and_well_formed():
    """3.2 #7 L205: child is null or an object with the pinned fields.

    ``None`` is valid when no child was spawned; a malformed child object
    is refused.  Adapted: child rides inside ``state`` with no rule reading
    it (GAP; see RULE_STATUS).
    """
    head = _head()
    handoff = _adapter_handoff(_adapt_pinned_to_library(head))

    partial = _head(
        child={
            "child_kind": "subtask",
            "parent_manifest_digest": "sha256:" + "2" * 64,
            "child_state": "running",
            "child_receipt_ref": "rcpt-c-1",
        }
    )
    with pytest.raises(IncompleteContext, match="child"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(partial), handoff)

    spawned = _head(
        child={
            "child_id": "c-1",
            "child_kind": "subtask",
            "parent_manifest_digest": "sha256:" + "2" * 64,
            "child_state": "running",
            "child_receipt_ref": "rcpt-c-1",
        }
    )
    summary = validate_chain(
        _adapt_pinned_to_library(spawned), [], handoff=handoff
    )
    assert summary.terminal_digest == _adapt_pinned_to_library(spawned)["digest"]


def test_body_field_set_is_fully_digest_covered():
    """3.2 8a L207-208: no body field may be excluded from the content digest.

    For each body field, a mutation that keeps the manifest otherwise valid
    (with the config-in-force adjusted for the config case, since a valid
    config is pinned to the values in force) must change the digest the
    library recomputes, proven by the trusted terminal mismatch refusal.
    Adapted: every body field rides inside the adapted document's digested
    content (state or the cursor slot), so each mutation must change the
    recomputed digest and the trusted-base comparison must refuse it.
    """
    head = _head()
    adapted_head = _adapt_pinned_to_library(head)
    handoff = _adapter_handoff(adapted_head)

    mutations = [
        (
            "task",
            {"task_id": TASK_ID, "requirement_ids": ["F12"], "attempt_id": "att-2"},
            None,
        ),
        (
            "revision",
            {
                "policy_revision": POLICY_REVISION,
                "schema_version": PINNED_SCHEMA_VERSION,
                "source_revision": "35ea475f0",
            },
            None,
        ),
        (
            "config",
            {"retention": "until-acceptance", "channel": "nightly"},
            {"retention": "until-acceptance", "channel": "nightly"},
        ),
        (
            "cursor",
            {"scope": "leaf-b", "position": "round-5", "epoch": EPOCH_IN_FORCE},
            None,
        ),
        (
            "question",
            {
                "question_id": "q-1",
                "asker": "executor",
                "answerer": "manager",
                "request_scope": "leaf-b",
                "state": "open",
                "answer": None,
            },
            None,
        ),
        (
            "action",
            {
                "action_id": "act-1",
                "kind": "checkpoint",
                "arguments_digest": "sha256:" + "1" * 64,
                "dispatch_state": "dispatched",
                "receipt_ref": "rcpt-1",
            },
            None,
        ),
        (
            "child",
            {
                "child_id": "c-1",
                "child_kind": "subtask",
                "parent_manifest_digest": "sha256:" + "2" * 64,
                "child_state": "running",
                "child_receipt_ref": "rcpt-c-1",
            },
            None,
        ),
    ]
    assert [name for name, _, _ in mutations] == list(BODY_FIELDS)

    for field, new_value, in_force in mutations:
        mutated = _head(**{field: new_value})
        assert mutated["terminal_content_digest"] != head["terminal_content_digest"], (
            f"the test helper itself must change the digest for {field}"
        )
        adapted_mutated = _adapt_pinned_to_library(mutated)
        assert adapted_mutated["digest"] != adapted_head["digest"], (
            f"the adapter itself must change the digest for {field}"
        )
        # The handoff still trusts the original adapted head's digest: if the
        # library excluded this body field from the content digest, the
        # mutated manifest would recompute the same digest and validate.
        # The config leg keeps the pin's adjusted-config-in-force meaning by
        # trusting the *original* head's digest with the mutated config
        # (a config the library would have to reject as not in force).
        mutated_handoff = (
            _adapter_handoff(adapted_head) if in_force is not None else handoff
        )
        with pytest.raises(IncompleteContext, match="checkpoint content"):
            validate_checkpoint_manifest(adapted_mutated, mutated_handoff)


@pytest.mark.xfail(
    strict=True,
    reason="GAP: the closed-set scan is not native -- an unlisted field "
    "crosses inside state and validates (would live at a state-field "
    "allowlist beside require_mandatory, manifest.py:481-484)",
)
def test_neg26_body_field_set_is_closed_unlisted_field_refused():
    """3.2 8b L208-210: the manifest field set is a closed set.

    A field outside the pinned set is refused, whatever its shape; adding a
    field requires a new schema_version, not an extension of this one.
    Adapted: the unlisted field crosses inside ``state`` and the adapted
    document validates against its own derived handoff -- the closed-set
    scan is not native (GAP; see RULE_STATUS).
    """
    head = _head()

    extra = _head(evidence_bundle={"note": "not a pinned field"})
    adapted_extra = _adapt_pinned_to_library(extra)
    with pytest.raises(IncompleteContext, match="unlisted"):
        validate_checkpoint_manifest(
            adapted_extra, _adapter_handoff(adapted_extra)
        )


# --------------------------------------------------------------------------
# 4.2 Terminal content digest (pin L229-241) -- 3 rules
# --------------------------------------------------------------------------


def test_pos05_digest_covers_full_content_except_the_digest_field_itself():
    """4.2 L229-232: the digest covers the full content, except itself.

    The head validates with a correctly computed terminal_content_digest
    (impossible if the field hashed itself), and the same content with a
    garbage value is refused, so the field is verified but excluded.
    Adapted: the honest head validates (the native chain rule enforces
    it); the garbage-digest leg is refused in effect -- the garbage value
    rides inside state and changes the recomputed digest -- but by the
    trusted-base comparison, with no rule naming terminal_content_digest,
    and the missing leg is dropped by the boundary before validation
    (ADAPTER-LIMIT; see RULE_STATUS).
    """
    head = _head()
    adapted = _adapt_pinned_to_library(head)
    handoff = _adapter_handoff(adapted)
    summary = validate_chain(adapted, [], handoff=handoff)
    assert summary.terminal_digest == adapted["digest"]

    garbage = dict(head)
    garbage["terminal_content_digest"] = "sha256:" + "e" * 64
    with pytest.raises(IncompleteContext, match="terminal_content_digest"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(garbage), handoff)


def test_pos05_digest_deterministic_under_reordering_and_reformatting():
    """4.2 L233-237: same content, same digest; re-ordering or re-formatting
    an equal body must not change the digest.

    Two renderings of the same manifest -- keys built in a different order,
    and one round-tripped through pretty-printed JSON -- must both validate
    against the same trusted digest.  Adapted: both renderings adapt to the
    same library document and validate against the same recomputed digest;
    the boundary's canonical-json/v1 serializer is what makes re-ordering
    and re-formatting digest-neutral (ENFORCED; see RULE_STATUS).
    """
    ordered = _head()

    reversed_content = {key: ordered[key] for key in reversed(list(ordered))}
    assert list(reversed_content) != list(ordered)

    reformatted = json.loads(json.dumps(ordered, indent=4, sort_keys=False))

    for equivalent in (reversed_content, reformatted):
        assert equivalent["terminal_content_digest"] == ordered["terminal_content_digest"]
        adapted = _adapt_pinned_to_library(equivalent)
        summary = validate_chain(adapted, [], handoff=_adapter_handoff(adapted))
        assert summary.terminal_digest == _adapt_pinned_to_library(ordered)["digest"]


def test_neg38_any_body_field_change_changes_the_digest_and_only_content_changes_do():
    """4.2 L238-241: any body field change changes the digest; a change that
    does not change body content must not.

    A body mutation whose terminal_content_digest is honestly recomputed is
    still refused against the trusted original digest (the change shows up
    in the digest); a pure re-rendering of the same content still validates
    (nothing shows up).  Adapted: the changed receipt_ref rides inside
    ``state``, so the recomputed digest differs and the trusted-base
    comparison refuses it; the re-rendered pin document adapts to the same
    bytes and validates (ENFORCED; see RULE_STATUS).
    """
    head = _head()
    adapted_head = _adapt_pinned_to_library(head)
    handoff = _adapter_handoff(adapted_head)

    changed = _head(
        action={
            "action_id": "act-1",
            "kind": "checkpoint",
            "arguments_digest": "sha256:" + "1" * 64,
            "dispatch_state": "completed",
            "receipt_ref": "rcpt-2",
        }
    )
    assert changed["terminal_content_digest"] != head["terminal_content_digest"]
    with pytest.raises(IncompleteContext, match="checkpoint content"):
        validate_checkpoint_manifest(_adapt_pinned_to_library(changed), handoff)

    rerendered = json.loads(json.dumps(head, indent=2))
    summary = validate_chain(
        _adapt_pinned_to_library(rerendered), [], handoff=handoff
    )
    assert summary.terminal_digest == adapted_head["digest"]


# --------------------------------------------------------------------------
# 4.3 Predecessor digest links (pin L245-256) -- 4 rules
# --------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no rule reads state.predecessor_digest; the head/delta "
    "distinction is erased by the boundary (would sit at manifest.py:481-484)",
)
def test_edge16_predecessor_digest_carried_by_every_manifest_and_null_at_head():
    """4.3 L245-246: every manifest carries predecessor_digest; null marks
    the chain head, and a delta must carry its predecessor's digest.

    A valid head (null) followed by valid deltas (non-null, each naming the
    previous terminal content digest) validates.  Adapted: every pinned
    manifest maps to a base checkpoint, so the field rides in ``state``
    with no rule reading it (GAP; see RULE_STATUS).
    """
    head = _head()
    d1 = _delta(head["terminal_content_digest"], 1)
    d2 = _delta(d1["terminal_content_digest"], 2)
    assert head["predecessor_digest"] is None
    assert d1["predecessor_digest"] == head["terminal_content_digest"]
    adapted_d2 = _adapt_pinned_to_library(d2)
    summary = validate_chain(
        adapted_d2, [], handoff=_adapter_handoff(adapted_d2)
    )
    assert summary.terminal_digest == adapted_d2["digest"]

    null_delta = _delta(head["terminal_content_digest"], 1, predecessor_digest=None)
    adapted_null = _adapt_pinned_to_library(null_delta)
    with pytest.raises(IncompleteContext, match="predecessor_digest"):
        validate_checkpoint_manifest(
            adapted_null, _adapter_handoff(adapted_null)
        )


def test_pos16_chain_is_hash_linked_by_predecessor_terminal_digests():
    """4.3 L247-249: each manifest's predecessor_digest names the previous
    manifest's terminal content digest, and base_digest names the trusted
    base it continues from.

    Adapted: the pin-side linking assertions hold on the fixture, and the
    adapted terminal manifest validates against its own recomputed digest
    and trusted base (ENFORCED; see RULE_STATUS).
    """
    head = _head()
    d1 = _delta(head["terminal_content_digest"], 1)
    d2 = _delta(d1["terminal_content_digest"], 2)
    assert d1["base_digest"] == TRUSTED_BASE_DIGEST
    assert d2["base_digest"] == TRUSTED_BASE_DIGEST
    assert d2["predecessor_digest"] == d1["terminal_content_digest"]
    adapted_d2 = _adapt_pinned_to_library(d2)
    summary = validate_chain(
        adapted_d2, [], handoff=_adapter_handoff(adapted_d2)
    )
    assert isinstance(summary, ChainSummary)
    assert summary.terminal_digest == adapted_d2["digest"]


@pytest.mark.xfail(
    strict=True,
    reason="GAP: a broken predecessor link rides inside state with no rule "
    "refusing it; the native link rule (manifest.py:518-519) is unreachable "
    "through the boundary",
)
def test_neg25_broken_predecessor_link_is_refused():
    """4.3 L250-253: a predecessor link that does not equal the predecessor's
    terminal content digest is a broken link and is refused, even when the
    delta's own digest is honestly recomputed over its tampered content.

    Adapted: the boundary maps every pinned manifest to a base checkpoint,
    so the broken link rides inside ``state`` and validates (GAP; see
    RULE_STATUS).
    """
    head = _head()
    d1 = _delta("sha256:" + "0" * 64, 1)
    assert d1["predecessor_digest"] == "sha256:" + "0" * 64
    adapted_d1 = _adapt_pinned_to_library(d1)
    with pytest.raises(IncompleteContext, match="predecessor"):
        validate_checkpoint_manifest(
            adapted_d1, _adapter_handoff(adapted_d1)
        )

    honest = _delta(head["terminal_content_digest"], 1)
    tampered = _delta(
        head["terminal_content_digest"],
        1,
        action={
            "action_id": "act-2",
            "kind": "checkpoint",
            "arguments_digest": "sha256:" + "1" * 64,
            "dispatch_state": "completed",
            "receipt_ref": "rcpt-9",
        },
    )
    assert tampered["predecessor_digest"] == honest["predecessor_digest"]
    assert tampered["terminal_content_digest"] != honest["terminal_content_digest"]
    d2 = _delta(honest["terminal_content_digest"], 2)
    adapted_chain = [_adapt_pinned_to_library(tampered), _adapt_pinned_to_library(d2)]
    with pytest.raises(IncompleteContext, match="predecessor"):
        validate_chain(
            adapted_chain[0], adapted_chain[1:], handoff=_adapter_handoff(adapted_chain[0])
        )


def test_pos16_restore_side_validation_covers_content_predecessor_and_terminal():
    """4.3 L254-256: the validation the restore path relies on -- content,
    predecessor and terminal digest validation -- is present.

    Tampering terminal content (with the self digest recomputed) or the
    stored terminal digest is refused; the honest chain validates.
    Adapted: the content-tamper leg is refused in effect (the mutated
    state changes the recomputed digest) but by the trusted-base
    comparison with no predecessor/terminal wording, and the
    stored-digest-tamper leg is dropped by the boundary (ADAPTER-LIMIT;
    see RULE_STATUS).
    """
    head = _head()
    d1 = _delta(head["terminal_content_digest"], 1)
    adapted_d1 = _adapt_pinned_to_library(d1)
    handoff = _adapter_handoff(adapted_d1)
    summary = validate_chain(adapted_d1, [], handoff=handoff)
    assert summary.terminal_digest == adapted_d1["digest"]

    tampered_content = _delta(
        head["terminal_content_digest"],
        1,
    )
    tampered_content["cursor"] = {
        "scope": "leaf-b",
        "position": "round-99",
        "epoch": EPOCH_IN_FORCE,
    }
    tampered_content["terminal_content_digest"] = _pinned_digest(tampered_content)
    assert tampered_content["terminal_content_digest"] != d1["terminal_content_digest"]
    with pytest.raises(IncompleteContext, match="terminal"):
        validate_checkpoint_manifest(
            _adapt_pinned_to_library(tampered_content), handoff
        )

    tampered_digest = dict(d1)
    tampered_digest["terminal_content_digest"] = "sha256:" + "f" * 64
    with pytest.raises(IncompleteContext, match="terminal_content_digest"):
        validate_checkpoint_manifest(
            _adapt_pinned_to_library(tampered_digest), handoff
        )


# --------------------------------------------------------------------------
# Section 7 explicit removals (pin L293-304) -- 8 rules
# --------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no rule scans state values for narrative text (would "
    "live at a removed-element scan beside require_mandatory, "
    "manifest.py:481-484)",
)
def test_neg26_removed_narrative_text_is_refused():
    """Section 7 L293: free-form narrative or summary text is removed.

    Adapted: the narrative field crosses inside ``state`` with no rule
    scanning for it (GAP; see RULE_STATUS).
    """
    narrative = _head(summary="the run went well and everyone was happy")
    adapted = _adapt_pinned_to_library(narrative)
    with pytest.raises(IncompleteContext, match="narrative"):
        validate_checkpoint_manifest(adapted, _adapter_handoff(adapted))


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no rule scans state values for secret material (would "
    "live at a removed-element scan beside require_mandatory, "
    "manifest.py:481-484)",
)
def test_neg26_removed_secret_material_is_refused():
    """Section 7 L294: secret, credential or private key material is removed.

    The secret sits inside a *pinned* free-string field, so only a value
    scan for secret material can catch it.  Adapted: the secret rides
    inside ``state`` with no rule scanning for it (GAP; see RULE_STATUS).
    """
    secret = _head(
        action={
            "action_id": "act-1",
            "kind": "checkpoint",
            "arguments_digest": "sha256:" + "1" * 64,
            "dispatch_state": "completed",
            "receipt_ref": "-----BEGIN PRIVATE KEY-----",
        }
    )
    adapted = _adapt_pinned_to_library(secret)
    with pytest.raises(IncompleteContext, match="secret"):
        validate_checkpoint_manifest(adapted, _adapter_handoff(adapted))


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no rule scans state values for latency or timing values "
    "(would live at a removed-element scan beside require_mandatory, "
    "manifest.py:481-484)",
)
def test_neg26_removed_latency_or_timing_values_are_refused():
    """Section 7 L295: latency, timing or performance values are removed.

    Adapted: the latency value rides inside ``state`` with no rule
    scanning for it (GAP; see RULE_STATUS).
    """
    latency = _head(
        config={
            "retention": "until-acceptance",
            "channel": "stable",
            "dispatch_latency_ms": 42,
        }
    )
    adapted = _adapt_pinned_to_library(latency)
    with pytest.raises(IncompleteContext, match="latency"):
        validate_checkpoint_manifest(adapted, _adapter_handoff(adapted))


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no rule scans state values for threshold or SLO values "
    "(would live at a removed-element scan beside require_mandatory, "
    "manifest.py:481-484)",
)
def test_neg26_removed_threshold_or_slo_values_are_refused():
    """Section 7 L296: threshold values, targets or SLOs are removed.

    Adapted: the threshold value rides inside ``state`` with no rule
    scanning for it (GAP; see RULE_STATUS).
    """
    threshold = _head(
        config={
            "retention": "until-acceptance",
            "channel": "stable",
            "error_threshold": 3,
        }
    )
    adapted = _adapt_pinned_to_library(threshold)
    with pytest.raises(IncompleteContext, match="threshold"):
        validate_checkpoint_manifest(adapted, _adapter_handoff(adapted))


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no rule scans state keys/values for host or session "
    "identifiers (would live at a removed-element scan beside "
    "require_mandatory, manifest.py:481-484)",
)
def test_neg26_removed_host_or_session_identifiers_are_refused():
    """Section 7 L297: host identifiers, node names or session identifiers
    are removed; the manifest is environment-independent.

    Adapted: the identifiers ride inside ``state`` with no rule scanning
    for them (GAP; see RULE_STATUS).
    """
    identified = _head(host="worker-7", session_id="s-9")
    adapted = _adapt_pinned_to_library(identified)
    with pytest.raises(IncompleteContext, match="host"):
        validate_checkpoint_manifest(adapted, _adapter_handoff(adapted))


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no rule refuses an embedded predecessor document inside "
    "state (would live at a removed-element scan beside "
    "require_mandatory, manifest.py:481-484)",
)
def test_neg26_removed_embedded_predecessor_content_is_refused():
    """Section 7 L298: linking is by digest only; the predecessor's content
    itself is removed.

    Adapted: the embedded predecessor rides inside ``state`` with no rule
    refusing it (GAP; see RULE_STATUS).
    """
    head = _head()
    embedding = _delta(head["terminal_content_digest"], 1)
    embedding["predecessor"] = dict(head)
    embedding["terminal_content_digest"] = _pinned_digest(embedding)
    adapted = _adapt_pinned_to_library(embedding)
    with pytest.raises(IncompleteContext, match="predecessor content"):
        validate_checkpoint_manifest(adapted, _adapter_handoff(adapted))


@pytest.mark.xfail(
    strict=True,
    reason="GAP: the closed-set scan is not native and there is no version "
    "gate (would live at a state-field allowlist beside require_mandatory, "
    "manifest.py:481-484)",
)
def test_neg26_removed_unlisted_or_extension_field_is_refused():
    """Section 7 L299: an unlisted or extension field is removed; any
    extension requires a new schema_version.

    Adapted: the extension field crosses inside ``state`` and validates,
    and the future-schema leg has no version gate to refuse it (GAP; see
    RULE_STATUS).
    """
    extension = _head(x_extension=1)
    adapted = _adapt_pinned_to_library(extension)
    with pytest.raises(IncompleteContext, match="unlisted"):
        validate_checkpoint_manifest(adapted, _adapter_handoff(adapted))

    future = _head(schema_version="w3-f12-manifest/v2", x_extension=1)
    adapted_future = _adapt_pinned_to_library(future)
    with pytest.raises(IncompleteContext, match="schema_version"):
        validate_checkpoint_manifest(
            adapted_future, _adapter_handoff(adapted_future)
        )


@pytest.mark.xfail(
    strict=True,
    reason="GAP: no removed-element rule fires, so the offending document "
    "is not refused and the non-mutation check never runs (would live "
    "beside require_mandatory, manifest.py:481-484)",
)
def test_edge20_removal_is_validation_not_deletion():
    """Section 7 L301-304: a manifest carrying a removed element is refused,
    never edited to strip the element (editing would change its digest and
    break the link that referenced it).

    Adapted: the offending host field rides inside ``state`` with no rule
    refusing it, so the refusal leg never fires (GAP; see RULE_STATUS).
    """
    head = _head()
    offending = _delta(head["terminal_content_digest"], 1)
    offending["host"] = "worker-7"
    offending["terminal_content_digest"] = _pinned_digest(offending)
    pristine = json.loads(json.dumps(offending))

    adapted = _adapt_pinned_to_library(offending)
    with pytest.raises(IncompleteContext, match="removed element"):
        validate_checkpoint_manifest(adapted, _adapter_handoff(adapted))
    assert offending == pristine, "refusal must not strip or mutate the document"


# --------------------------------------------------------------------------
# Re-slice A: the adapter (skeleton) and its green positive control
#
# The 31 tests above encode the *pinned* manifest shape and are red against
# the current library by design (they are the step-3 target, reworked onto
# this adapter in re-slice B).  This section adds only the adapter skeleton
# plus one positive control proving the adapter's output is a document the
# current library accepts.  Nothing above is modified.
# --------------------------------------------------------------------------


# The pin has no tenant field at all; the pinned plane's trusted handoff
# (``_handoff`` above) carries ``tenant_id: "t-1"`` as its scope fact, so the
# trusted side is the adapter's only possible source for it.
ADAPTER_TENANT_ID = "t-1"

# The pin also has no checkpoint identity or sequence of its own; the pinned
# handoff's ``expected_base`` names them, and the adapted document takes its
# identity from there (single-manifest scope: it *is* the base).
ADAPTER_BASE_CHECKPOINT_ID = "cp-base"
ADAPTER_BASE_SEQUENCE = 0


def _adapt_pinned_to_library(pinned: dict) -> dict:
    """Map the pinned manifest content onto the library's document shape.

    Re-slice A step 2: the input is the pinned document produced by
    :func:`_pinned_content`, mapped field by field into the shape
    ``validate_checkpoint_manifest`` accepts (``manifest.py``): top-level
    scope keys, checkpoint identity + sequence + state against a trusted
    base, and a self-digest under the library's field name.

    * ``policy_revision`` (pin envelope) is promoted to a top-level scope
      key, where ``_require_scope_binding`` reads it (manifest.py L268-272).
    * ``task_id`` is lifted out of the pinned ``task`` body object into a
      top-level scope key (same reader).
    * ``tenant_id`` has no pinned form; it comes from the trusted side
      (``ADAPTER_TENANT_ID``), matching the pinned handoff's scope.
    * ``checkpoint_id``/``sequence`` have no pinned form either; they come
      from the trusted base the pinned handoff declares.
    * ``cursor`` (pin body #4) rides in the library document's dedicated
      ``cursor`` slot (``CheckpointManifest.cursor``, manifest.py L121).
    * every remaining pinned field -- envelope ``schema_version``,
      ``base_digest``, ``predecessor_digest``, ``created_at``, ``producer``
      and body ``task``, ``revision``, ``config``, ``question``, ``action``,
      ``child`` -- is carried over verbatim inside ``state``.
    * the digest is recomputed the library's way: field name ``digest``
      (the pin calls it ``terminal_content_digest``) and value from
      ``with_digest()`` -- sha256 over canonical-json/v1 of the document
      with its own ``digest`` key excluded (manifest.py L88-98).
    """
    scope = {
        "tenant_id": ADAPTER_TENANT_ID,
        "task_id": pinned["task"]["task_id"],
        "policy_revision": pinned["policy_revision"],
    }
    state = {
        key: value
        for key, value in pinned.items()
        if key not in ("policy_revision", "cursor")
    }
    return (
        CheckpointManifest(
            checkpoint_id=ADAPTER_BASE_CHECKPOINT_ID,
            sequence=ADAPTER_BASE_SEQUENCE,
            state=json.loads(json.dumps(state)),
            scope=scope,
            cursor=json.loads(json.dumps(pinned["cursor"])),
        )
        .with_digest()
        .as_document()
    )


def _adapter_handoff(document: dict) -> TrustedHandoff:
    """The trusted handoff the adapted document must validate against.

    The scope binding names exactly the adapted document's own top-level
    scope keys, and the trusted base triple names its own identity, sequence
    and computed digest, so a positive validation is the library accepting
    the adapter's shape, not a coincidence of matching digests.
    """
    return TrustedHandoff(
        scope={
            "tenant_id": document["tenant_id"],
            "task_id": document["task_id"],
            "policy_revision": document["policy_revision"],
        },
        required_state_schemas={},
        expected_base={
            "checkpoint_id": document["checkpoint_id"],
            "sequence": document["sequence"],
            "digest": document["digest"],
        },
        expected_terminal={
            "delta_id": document["checkpoint_id"],
            "sequence": document["sequence"],
            "digest": document["digest"],
        },
    )


def test_positive_control_adapted_document_validates():
    """Positive control: the adapter's document passes the library validation.

    This is re-slice A's green anchor: it proves that the pinned content
    produced by :func:`_pinned_content`, mapped field by field through
    :func:`_adapt_pinned_to_library`, yields a document
    ``validate_checkpoint_manifest`` accepts (scope binding, base identity,
    self-digest), independent of the 31 red pin tests above.
    """
    document = _adapt_pinned_to_library(_pinned_content())
    computed = validate_checkpoint_manifest(document, _adapter_handoff(document))
    assert computed == document["digest"]
    assert computed.startswith("sha256:")
