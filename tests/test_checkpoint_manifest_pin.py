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
    IncompleteContext,
    TrustedHandoff,
    canonical_bytes,
    validate_chain,
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
# 3.1 The manifest envelope (pin L181-187) -- 7 rules
# --------------------------------------------------------------------------


def test_neg39_envelope_policy_revision_mandatory_and_stale_revision_refused():
    """3.1 L181: policy_revision is required; a revision not in force is stale.

    The revision in force comes from the trusted handoff, never from the
    manifest (A1): a manifest cannot declare itself current.
    """
    head = _head()
    handoff = _head_handoff(head)

    missing = _pinned_content()
    del missing["policy_revision"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="policy_revision"):
        validate_chain(missing, [], handoff=handoff)

    stale = _head(
        policy_revision="pol-8",
        revision={
            "policy_revision": "pol-8",
            "schema_version": PINNED_SCHEMA_VERSION,
            "source_revision": "35ea475f",
        },
    )
    with pytest.raises(IncompleteContext, match="policy_revision"):
        validate_chain(stale, [], handoff=handoff)


def test_edge20_envelope_schema_version_mandatory_and_version_compatibility_enforced():
    """3.1 L182 with section 2: schema_version is required and must be accepted.

    A manifest is judged against the version it declares, and only versions
    the trusted side accepts can validate at all.
    """
    head = _head()
    handoff = _head_handoff(head)

    missing = _pinned_content()
    del missing["schema_version"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="schema_version"):
        validate_chain(missing, [], handoff=handoff)

    unaccepted = _head(schema_version=UNACCEPTED_SCHEMA_VERSION)
    with pytest.raises(IncompleteContext, match="schema_version"):
        validate_chain(unaccepted, [], handoff=handoff)


def test_edge17_envelope_base_digest_mandatory_and_binds_the_trusted_base():
    """3.1 L183: base_digest is required and names the trusted base content.

    Single-manifest scope (pin 11.2 EDGE-17): with no deltas, the trusted
    base digest and the head's terminal content digest are the same trust
    anchor, and the head must validate against it.
    """
    head = _head()
    assert head["base_digest"] == TRUSTED_BASE_DIGEST
    summary = validate_chain(head, [], handoff=_head_handoff(head))
    assert isinstance(summary, ChainSummary)
    assert summary.terminal_digest == head["terminal_content_digest"]

    wrong_base = _head(base_digest="sha256:" + "c" * 64)
    with pytest.raises(IncompleteContext, match="base_digest"):
        validate_chain(wrong_base, [], handoff=_head_handoff(wrong_base))

    missing = _pinned_content()
    del missing["base_digest"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="base_digest"):
        validate_chain(missing, [], handoff=_handoff(missing["terminal_content_digest"]))


def test_pos05_envelope_terminal_content_digest_mandatory():
    """3.1 L184: every manifest carries terminal_content_digest, verified.

    A missing or wrongly valued terminal_content_digest is refused; the
    value is recomputed by the library, never taken from the document.
    """
    head = _head()
    handoff = _head_handoff(head)

    missing = _pinned_content()
    missing["terminal_content_digest"] = _pinned_digest(missing)
    del missing["terminal_content_digest"]
    with pytest.raises(IncompleteContext, match="terminal_content_digest"):
        validate_chain(missing, [], handoff=handoff)

    wrong = dict(head)
    wrong["terminal_content_digest"] = "sha256:" + "d" * 64
    with pytest.raises(IncompleteContext, match="terminal_content_digest"):
        validate_chain(wrong, [], handoff=handoff)


def test_edge16_envelope_predecessor_digest_null_only_for_the_chain_head():
    """3.1 L185: predecessor_digest is required on every manifest.

    Null is the chain head's mark: a head carrying a non-null predecessor
    digest, or a delta carrying null, is refused.
    """
    head = _head()
    handoff = _head_handoff(head)

    missing = _pinned_content()
    del missing["predecessor_digest"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="predecessor_digest"):
        validate_chain(missing, [], handoff=handoff)

    head_with_link = _head(predecessor_digest=head["terminal_content_digest"])
    with pytest.raises(IncompleteContext, match="predecessor_digest"):
        validate_chain(head_with_link, [], handoff=_head_handoff(head_with_link))

    d1 = _delta(head["terminal_content_digest"], 1, predecessor_digest=None)
    with pytest.raises(IncompleteContext, match="predecessor_digest"):
        validate_chain(
            head,
            [d1],
            handoff=_handoff(d1["terminal_content_digest"]),
        )


def test_edge18_envelope_created_at_mandatory():
    """3.1 L186: created_at is required; null is not presence.

    Completeness requires the field set, not a content volume (pin 11.2
    EDGE-18): a manifest with substantial content but no production time is
    still incomplete.
    """
    head = _head()
    handoff = _head_handoff(head)

    missing = _pinned_content()
    del missing["created_at"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="created_at"):
        validate_chain(missing, [], handoff=handoff)

    nulled = _head(created_at=None)
    with pytest.raises(IncompleteContext, match="created_at"):
        validate_chain(nulled, [], handoff=_head_handoff(nulled))


def test_edge18_envelope_producer_mandatory():
    """3.1 L187: producer is required; null is not presence."""
    head = _head()
    handoff = _head_handoff(head)

    missing = _pinned_content()
    del missing["producer"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="producer"):
        validate_chain(missing, [], handoff=handoff)

    nulled = _head(producer=None)
    with pytest.raises(IncompleteContext, match="producer"):
        validate_chain(nulled, [], handoff=_head_handoff(nulled))


# --------------------------------------------------------------------------
# 3.2 The mandatory value schema, manifest body (pin L199-210) -- 9 rules
# --------------------------------------------------------------------------


def test_body_task_object_mandatory_with_stable_ids():
    """3.2 #1 L199: task is required, with task_id, requirement_ids, attempt_id.

    task_id binds to the trusted scope (A1: the binding comes from the
    handoff, not from the manifest's own claim).
    """
    head = _head()
    handoff = _head_handoff(head)

    missing = _pinned_content()
    del missing["task"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="task"):
        validate_chain(missing, [], handoff=handoff)

    partial = _head(task={"task_id": TASK_ID, "attempt_id": "att-1"})
    with pytest.raises(IncompleteContext, match="task"):
        validate_chain(partial, [], handoff=_head_handoff(partial))

    foreign = _head(
        task={
            "task_id": "T9.9",
            "requirement_ids": ["F12"],
            "attempt_id": "att-1",
        }
    )
    with pytest.raises(IncompleteContext, match="task"):
        validate_chain(foreign, [], handoff=_head_handoff(foreign))


def test_edge20_body_revision_mirrors_the_envelope_exactly():
    """3.2 #2 L200: revision is required and mirrors policy_revision and
    schema_version from the envelope, plus source_revision.

    A mirror that disagrees with the envelope makes the manifest invalid.
    """
    head = _head()
    handoff = _head_handoff(head)

    missing = _pinned_content()
    del missing["revision"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="revision"):
        validate_chain(missing, [], handoff=handoff)

    disagreeing_policy = _head(
        revision={
            "policy_revision": "pol-8",
            "schema_version": PINNED_SCHEMA_VERSION,
            "source_revision": "35ea475f",
        }
    )
    with pytest.raises(IncompleteContext, match="revision"):
        validate_chain(disagreeing_policy, [], handoff=_head_handoff(disagreeing_policy))

    disagreeing_version = _head(
        revision={
            "policy_revision": POLICY_REVISION,
            "schema_version": UNACCEPTED_SCHEMA_VERSION,
            "source_revision": "35ea475f",
        }
    )
    with pytest.raises(IncompleteContext, match="revision"):
        validate_chain(
            disagreeing_version, [], handoff=_head_handoff(disagreeing_version)
        )

    no_source = _head(
        revision={
            "policy_revision": POLICY_REVISION,
            "schema_version": PINNED_SCHEMA_VERSION,
        }
    )
    with pytest.raises(IncompleteContext, match="revision"):
        validate_chain(no_source, [], handoff=_head_handoff(no_source))


def test_edge35_body_config_mandatory_named_and_matching_config_in_force():
    """3.2 #3 L201 with section 5: config is required as a named value map.

    The active attempt's config is immutable underneath a written manifest
    (pin 11.2 EDGE-35): a manifest whose config keys or values differ from
    the config in force -- which lives in the trusted handoff, never in the
    manifest (A1) -- is refused.
    """
    head = _head()
    handoff = _head_handoff(head)

    missing = _pinned_content()
    del missing["config"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="config"):
        validate_chain(missing, [], handoff=handoff)

    extra_key = _head(config={"retention": "until-acceptance", "channel": "stable",
                              "retries": 3})
    with pytest.raises(IncompleteContext, match="config"):
        validate_chain(extra_key, [], handoff=_head_handoff(extra_key))

    changed_value = _head(config={"retention": "forever", "channel": "stable"})
    with pytest.raises(IncompleteContext, match="config"):
        validate_chain(changed_value, [], handoff=_head_handoff(changed_value))


def test_neg39_body_cursor_requires_epoch_and_stale_epoch_is_refused():
    """3.2 #4 L202: cursor is required with scope, position and epoch.

    A cursor with no epoch, or with a superseded epoch, is stale and must be
    rejected; the epoch in force comes from the trusted handoff (A1).
    """
    head = _head()
    handoff = _head_handoff(head)

    missing = _pinned_content()
    del missing["cursor"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="cursor"):
        validate_chain(missing, [], handoff=handoff)

    no_epoch = _head(cursor={"scope": "leaf-b", "position": "round-4"})
    with pytest.raises(IncompleteContext, match="cursor"):
        validate_chain(no_epoch, [], handoff=_head_handoff(no_epoch))

    stale_epoch = _head(
        cursor={"scope": "leaf-b", "position": "round-4", "epoch": EPOCH_IN_FORCE - 1}
    )
    with pytest.raises(IncompleteContext, match="stale"):
        validate_chain(stale_epoch, [], handoff=_head_handoff(stale_epoch))


def test_body_question_conditional_and_well_formed():
    """3.2 #5 L203: question is null or an object with the pinned fields.

    Present exactly when a question awaits an answer; malformed question
    objects (missing question_id, or a state outside the pinned set) are
    refused.  ``None`` is valid when there is no open question.
    """
    head = _head()
    handoff = _head_handoff(head)

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
        validate_chain(partial, [], handoff=_head_handoff(partial))

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
        validate_chain(bad_state, [], handoff=_head_handoff(bad_state))

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
    summary = validate_chain(answered, [], handoff=_head_handoff(answered))
    assert summary.terminal_digest == answered["terminal_content_digest"]


def test_body_action_mandatory_with_stable_action_id():
    """3.2 #6 L204: action is required with action_id, kind,
    arguments_digest, dispatch_state and receipt_ref."""
    head = _head()
    handoff = _head_handoff(head)

    missing = _pinned_content()
    del missing["action"]
    missing["terminal_content_digest"] = _pinned_digest(missing)
    with pytest.raises(IncompleteContext, match="action"):
        validate_chain(missing, [], handoff=handoff)

    partial = _head(
        action={
            "action_id": "act-1",
            "kind": "checkpoint",
            "dispatch_state": "completed",
            "receipt_ref": "rcpt-1",
        }
    )
    with pytest.raises(IncompleteContext, match="action"):
        validate_chain(partial, [], handoff=_head_handoff(partial))


def test_body_child_conditional_and_well_formed():
    """3.2 #7 L205: child is null or an object with the pinned fields.

    ``None`` is valid when no child was spawned; a malformed child object
    is refused.
    """
    head = _head()
    handoff = _head_handoff(head)

    partial = _head(
        child={
            "child_kind": "subtask",
            "parent_manifest_digest": "sha256:" + "2" * 64,
            "child_state": "running",
            "child_receipt_ref": "rcpt-c-1",
        }
    )
    with pytest.raises(IncompleteContext, match="child"):
        validate_chain(partial, [], handoff=_head_handoff(partial))

    spawned = _head(
        child={
            "child_id": "c-1",
            "child_kind": "subtask",
            "parent_manifest_digest": "sha256:" + "2" * 64,
            "child_state": "running",
            "child_receipt_ref": "rcpt-c-1",
        }
    )
    summary = validate_chain(spawned, [], handoff=_head_handoff(spawned))
    assert summary.terminal_digest == spawned["terminal_content_digest"]


def test_body_field_set_is_fully_digest_covered():
    """3.2 8a L207-208: no body field may be excluded from the content digest.

    For each body field, a mutation that keeps the manifest otherwise valid
    (with the config-in-force adjusted for the config case, since a valid
    config is pinned to the values in force) must change the digest the
    library recomputes, proven by the trusted terminal mismatch refusal.
    """
    head = _head()
    handoff = _head_handoff(head)

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
        # The handoff still trusts the original head's digest: if the library
        # excluded this body field from the content digest, the mutated
        # manifest would recompute the same digest and validate.
        mutated_handoff = (
            _handoff(
                head["terminal_content_digest"], config_in_force=in_force
            )
            if in_force is not None
            else handoff
        )
        with pytest.raises(IncompleteContext, match="terminal content"):
            validate_chain(mutated, [], handoff=mutated_handoff)


def test_neg26_body_field_set_is_closed_unlisted_field_refused():
    """3.2 8b L208-210: the manifest field set is a closed set.

    A field outside the pinned set is refused, whatever its shape; adding a
    field requires a new schema_version, not an extension of this one.
    """
    head = _head()
    handoff = _head_handoff(head)

    extra = _head(evidence_bundle={"note": "not a pinned field"})
    with pytest.raises(IncompleteContext, match="unlisted"):
        validate_chain(extra, [], handoff=_head_handoff(extra))


# --------------------------------------------------------------------------
# 4.2 Terminal content digest (pin L229-241) -- 3 rules
# --------------------------------------------------------------------------


def test_pos05_digest_covers_full_content_except_the_digest_field_itself():
    """4.2 L229-232: the digest covers the full content, except itself.

    The head validates with a correctly computed terminal_content_digest
    (impossible if the field hashed itself), and the same content with a
    garbage value is refused, so the field is verified but excluded.
    """
    head = _head()
    summary = validate_chain(head, [], handoff=_head_handoff(head))
    assert summary.terminal_digest == head["terminal_content_digest"]

    garbage = dict(head)
    garbage["terminal_content_digest"] = "sha256:" + "e" * 64
    with pytest.raises(IncompleteContext, match="terminal_content_digest"):
        validate_chain(garbage, [], handoff=_head_handoff(head))


def test_pos05_digest_deterministic_under_reordering_and_reformatting():
    """4.2 L233-237: same content, same digest; re-ordering or re-formatting
    an equal body must not change the digest.

    Two renderings of the same manifest -- keys built in a different order,
    and one round-tripped through pretty-printed JSON -- must both validate
    against the same trusted digest.
    """
    ordered = _head()

    reversed_content = {key: ordered[key] for key in reversed(list(ordered))}
    assert list(reversed_content) != list(ordered)

    reformatted = json.loads(json.dumps(ordered, indent=4, sort_keys=False))

    for equivalent in (reversed_content, reformatted):
        assert equivalent["terminal_content_digest"] == ordered["terminal_content_digest"]
        summary = validate_chain(
            equivalent, [], handoff=_handoff(ordered["terminal_content_digest"])
        )
        assert summary.terminal_digest == ordered["terminal_content_digest"]


def test_neg38_any_body_field_change_changes_the_digest_and_only_content_changes_do():
    """4.2 L238-241: any body field change changes the digest; a change that
    does not change body content must not.

    A body mutation whose terminal_content_digest is honestly recomputed is
    still refused against the trusted original digest (the change shows up
    in the digest); a pure re-rendering of the same content still validates
    (nothing shows up).
    """
    head = _head()
    handoff = _head_handoff(head)

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
    with pytest.raises(IncompleteContext, match="terminal content"):
        validate_chain(changed, [], handoff=handoff)

    rerendered = json.loads(json.dumps(head, indent=2))
    summary = validate_chain(rerendered, [], handoff=handoff)
    assert summary.terminal_digest == head["terminal_content_digest"]


# --------------------------------------------------------------------------
# 4.3 Predecessor digest links (pin L245-256) -- 4 rules
# --------------------------------------------------------------------------


def test_edge16_predecessor_digest_carried_by_every_manifest_and_null_at_head():
    """4.3 L245-246: every manifest carries predecessor_digest; null marks
    the chain head, and a delta must carry its predecessor's digest.

    A valid head (null) followed by valid deltas (non-null, each naming the
    previous terminal content digest) validates.
    """
    head = _head()
    d1 = _delta(head["terminal_content_digest"], 1)
    d2 = _delta(d1["terminal_content_digest"], 2)
    assert head["predecessor_digest"] is None
    assert d1["predecessor_digest"] == head["terminal_content_digest"]
    summary = validate_chain(
        head, [d1, d2], handoff=_handoff(d2["terminal_content_digest"])
    )
    assert summary.terminal_digest == d2["terminal_content_digest"]

    null_delta = _delta(head["terminal_content_digest"], 1, predecessor_digest=None)
    with pytest.raises(IncompleteContext, match="predecessor_digest"):
        validate_chain(
            head, [null_delta], handoff=_handoff(null_delta["terminal_content_digest"])
        )


def test_pos16_chain_is_hash_linked_by_predecessor_terminal_digests():
    """4.3 L247-249: each manifest's predecessor_digest names the previous
    manifest's terminal content digest, and base_digest names the trusted
    base it continues from."""
    head = _head()
    d1 = _delta(head["terminal_content_digest"], 1)
    d2 = _delta(d1["terminal_content_digest"], 2)
    assert d1["base_digest"] == TRUSTED_BASE_DIGEST
    assert d2["base_digest"] == TRUSTED_BASE_DIGEST
    assert d2["predecessor_digest"] == d1["terminal_content_digest"]
    summary = validate_chain(
        head, [d1, d2], handoff=_handoff(d2["terminal_content_digest"])
    )
    assert isinstance(summary, ChainSummary)
    assert summary.terminal_digest == d2["terminal_content_digest"]


def test_neg25_broken_predecessor_link_is_refused():
    """4.3 L250-253: a predecessor link that does not equal the predecessor's
    terminal content digest is a broken link and is refused, even when the
    delta's own digest is honestly recomputed over its tampered content."""
    head = _head()
    d1 = _delta("sha256:" + "0" * 64, 1)
    assert d1["predecessor_digest"] == "sha256:" + "0" * 64
    with pytest.raises(IncompleteContext, match="predecessor"):
        validate_chain(
            head, [d1], handoff=_handoff(d1["terminal_content_digest"])
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
    with pytest.raises(IncompleteContext, match="predecessor"):
        validate_chain(
            head, [tampered, d2], handoff=_handoff(d2["terminal_content_digest"])
        )


def test_pos16_restore_side_validation_covers_content_predecessor_and_terminal():
    """4.3 L254-256: the validation the restore path relies on -- content,
    predecessor and terminal digest validation -- is present.

    Tampering terminal content (with the self digest recomputed) or the
    stored terminal digest is refused; the honest chain validates.
    """
    head = _head()
    d1 = _delta(head["terminal_content_digest"], 1)
    handoff = _handoff(d1["terminal_content_digest"])
    summary = validate_chain(head, [d1], handoff=handoff)
    assert summary.terminal_digest == d1["terminal_content_digest"]

    tampered_content = _delta(
        head["terminal_content_digest"],
        1,
        cursor={"scope": "leaf-b", "position": "round-99", "epoch": EPOCH_IN_FORCE},
    )
    assert tampered_content["terminal_content_digest"] != d1["terminal_content_digest"]
    with pytest.raises(IncompleteContext, match="terminal"):
        validate_chain(head, [tampered_content], handoff=handoff)

    tampered_digest = dict(d1)
    tampered_digest["terminal_content_digest"] = "sha256:" + "f" * 64
    with pytest.raises(IncompleteContext, match="terminal_content_digest"):
        validate_chain(head, [tampered_digest], handoff=handoff)


# --------------------------------------------------------------------------
# Section 7 explicit removals (pin L293-304) -- 8 rules
# --------------------------------------------------------------------------


def test_neg26_removed_narrative_text_is_refused():
    """Section 7 L293: free-form narrative or summary text is removed."""
    narrative = _head(summary="the run went well and everyone was happy")
    with pytest.raises(IncompleteContext, match="narrative"):
        validate_chain(narrative, [], handoff=_head_handoff(narrative))


def test_neg26_removed_secret_material_is_refused():
    """Section 7 L294: secret, credential or private key material is removed.

    The secret sits inside a *pinned* free-string field, so only a value
    scan for secret material can catch it.
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
    with pytest.raises(IncompleteContext, match="secret"):
        validate_chain(secret, [], handoff=_head_handoff(secret))


def test_neg26_removed_latency_or_timing_values_are_refused():
    """Section 7 L295: latency, timing or performance values are removed."""
    latency = _head(
        config={
            "retention": "until-acceptance",
            "channel": "stable",
            "dispatch_latency_ms": 42,
        }
    )
    with pytest.raises(IncompleteContext, match="latency"):
        validate_chain(latency, [], handoff=_head_handoff(latency))


def test_neg26_removed_threshold_or_slo_values_are_refused():
    """Section 7 L296: threshold values, targets or SLOs are removed."""
    threshold = _head(
        config={
            "retention": "until-acceptance",
            "channel": "stable",
            "error_threshold": 3,
        }
    )
    with pytest.raises(IncompleteContext, match="threshold"):
        validate_chain(threshold, [], handoff=_head_handoff(threshold))


def test_neg26_removed_host_or_session_identifiers_are_refused():
    """Section 7 L297: host identifiers, node names or session identifiers
    are removed; the manifest is environment-independent."""
    identified = _head(host="worker-7", session_id="s-9")
    with pytest.raises(IncompleteContext, match="host"):
        validate_chain(identified, [], handoff=_head_handoff(identified))


def test_neg26_removed_embedded_predecessor_content_is_refused():
    """Section 7 L298: linking is by digest only; the predecessor's content
    itself is removed."""
    head = _head()
    embedding = _delta(head["terminal_content_digest"], 1)
    embedding["predecessor"] = dict(head)
    embedding["terminal_content_digest"] = _pinned_digest(embedding)
    with pytest.raises(IncompleteContext, match="predecessor content"):
        validate_chain(
            head, [embedding], handoff=_handoff(embedding["terminal_content_digest"])
        )


def test_neg26_removed_unlisted_or_extension_field_is_refused():
    """Section 7 L299: an unlisted or extension field is removed; any
    extension requires a new schema_version."""
    extension = _head(x_extension=1)
    with pytest.raises(IncompleteContext, match="unlisted"):
        validate_chain(extension, [], handoff=_head_handoff(extension))

    future = _head(schema_version="w3-f12-manifest/v2", x_extension=1)
    with pytest.raises(IncompleteContext, match="schema_version"):
        validate_chain(future, [], handoff=_head_handoff(future))


def test_edge20_removal_is_validation_not_deletion():
    """Section 7 L301-304: a manifest carrying a removed element is refused,
    never edited to strip the element (editing would change its digest and
    break the link that referenced it)."""
    head = _head()
    offending = _delta(head["terminal_content_digest"], 1)
    offending["host"] = "worker-7"
    offending["terminal_content_digest"] = _pinned_digest(offending)
    pristine = json.loads(json.dumps(offending))

    with pytest.raises(IncompleteContext, match="removed element"):
        validate_chain(
            head, [offending], handoff=_handoff(offending["terminal_content_digest"])
        )
    assert offending == pristine, "refusal must not strip or mutate the document"
