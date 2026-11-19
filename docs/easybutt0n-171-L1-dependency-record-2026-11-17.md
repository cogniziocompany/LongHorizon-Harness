# easybutt0n-171 L1 pre-edit dependency record — 2026-11-17

Ticket `easybutt0n-171`, implementation leaf L1 ("envelope mandatory fields").
This is the pre-decided substitute for the unavailable graphify pre-edit query:
a dated grep/read dependency record, committed BEFORE any edit to
`src/lh_harness/checkpoint/manifest.py`,
`tests/test_checkpoint_manifest_pin.py`, or `docs/CONFORMANCE.md`.

- Date: 2026-11-17
- Base branch: `easybutt0n-171/leaf-b-manifest-pin-conformance` (PR #206, unmerged)
- Base head (freshly fetched `origin/...`, this run): `224a5696cb94e728a7f7d46cc0f973f87a2bcbb3`
  subject: `easybutt0n-171: CONFORMANCE shape contract PROPOSAL -> DECIDED with the decider's receipt (guard d)`
- Working branch: `easybutt0n-171/impl-L1-envelope-mandatory`, created from that exact SHA.
- Prior run `c7fd2c32` state reused: NONE. Everything re-derived from scratch.

## (i) `src/lh_harness/checkpoint/manifest.py` — validation region (read 2026-11-17)

File length: 641 lines. The gap this leaf closes sits in the native
validation region; anchors `manifest.py:478-479` and `manifest.py:481-484`.
Verbatim excerpt (`grep -n '' src/lh_harness/checkpoint/manifest.py | sed -n '465,505p'`):

```
465:        else dict(checkpoint)
466:    )
467:    _require_scope_binding(document, handoff.scope)
468:
469:    expected_base = handoff.expected_base
470:    if document.get("checkpoint_id") != expected_base.get("checkpoint_id"):
471:        raise IncompleteContext("checkpoint identity does not match trusted base")
472:    if document.get("sequence", 0) != expected_base.get("sequence"):
473:        raise IncompleteContext("checkpoint sequence does not match trusted base")
474:
475:    computed = document_digest(document)
476:    if document.get("digest") != computed:
477:        raise IncompleteContext("checkpoint self-digest mismatch")
478:    if computed != expected_base.get("digest"):
479:        raise IncompleteContext("checkpoint content does not match trusted base")
480:
481:    state = document.get("state")
482:    if not isinstance(state, Mapping):
483:        raise IncompleteContext("checkpoint state is missing or not an object")
484:    require_mandatory(state, handoff.required_state_schemas)
485:    return computed
```

Dependency reading of the anchors:

- `manifest.py:478-479` — the recomputed content digest is compared against the
  trusted base digest. The pin's `base_digest` envelope field has no library
  rule that reads it (EDGE-17 anchor).
- `manifest.py:481-484` — `state` must be a present object, then
  `require_mandatory(state, handoff.required_state_schemas)` is the ONLY
  presence machinery. No rule requires `state.schema_version`, gates its
  version compatibility (EDGE-20), requires `state.created_at` (EDGE-18), or
  requires `state.producer` (EDGE-18). These four absences are the L1 gap.
- `validate_delta_manifest` begins at `manifest.py:488`; L1 edits must stay
  within the native validation region and not weaken lines 469-485.

## (ii) `tests/test_checkpoint_manifest_pin.py` — the four L1 xfail rows (read 2026-11-17)

The file carries exactly 22 `pytest.mark.xfail` marks (grep count: 22), 67 `GAP`
mentions, 11 `ADAPTER-LIMIT` mentions. The four rows this leaf closes, with
mark-decorator and `def` line numbers:

1. `test_edge20_envelope_schema_version_mandatory_and_version_compatibility_enforced`
   - mark line 479, def line 484
   - reason: `GAP: no rule requires state.schema_version or gates its version (would live at require_mandatory, manifest.py:481-484)`
2. `test_edge17_envelope_base_digest_mandatory_and_binds_the_trusted_base`
   - mark line 506, def line 511
   - reason: `GAP: no rule reads state.base_digest (would sit at require_mandatory, manifest.py:481-484)` (also anchors 478-479)
3. `test_edge18_envelope_created_at_mandatory`
   - mark line 596, def line 601
   - reason: `GAP: no rule requires state.created_at (would live at require_mandatory, manifest.py:481-484)`
4. `test_edge18_envelope_producer_mandatory`
   - mark line 623, def line 628
   - reason: `GAP: no rule requires state.producer (would live at require_mandatory, manifest.py:481-484)`

All four are `strict=True`. The remaining 18 xfail marks (lines 564, 680,
736, 778, 811, 869, 908, 1048, 1169, 1225, 1319, 1337, 1364, 1388, 1412,
1431, 1453, 1480 in the 2026-11-17 numbering) are NOT in scope: the other
18 GAP rows and the 5 ADAPTER-LIMIT rows exist in this file untouched and
must remain untouched, marks intact, no assertion weakened.

## (iii) `docs/CONFORMANCE.md` — decided contract as of base head (read 2026-11-17)

Verbatim excerpt (`docs/CONFORMANCE.md:132-143`):

```
Status: DECIDED -- decided continuity1-op 2026-11-16, one round, evidence PR #206 body table at cc34ae96.
The LIBRARY is the decided native shape; the PIN stays the pinned exchange
contract; the ADAPTER is a permanent named boundary, not scaffolding.
Four standing guards: (a) digests are recomputed at the boundary on the
native form, never copied across; (b) scope keys are equality-checked
against the trusted handoff, never trusted from the document alone; (c) the
adapter fails on unnamed fields in both directions (closed set, no silent
drops); (d) this section carries the decision receipt above.
Re-slice B (2026-11-16) moved the 31 pin tests onto the adapter. As a REPORT,
not a pass: 4 ENFORCED, 22 GAP (strict xfail naming the rule), 5
ADAPTER-LIMIT (left red with the reason). No manifest.py change is part of
this decision; library changes for the GAP rows are separate work.
```

L1 is that separate work for four of the GAP rows. Guard (a) constrains the
EDGE-17 rule: any base-digest binding must recompute, never copy.
`docs/CONFORMANCE.md:156` maps the pinned envelope fields (`schema_version`,
`base_digest`, `created_at`, `producer`, ...) onto the library `state` object
at `manifest.py:119, manifest.py:481-484`.

## (iv) Pin-file baseline, re-derived from scratch (2026-11-17)

Command (repo-standard invocation per `Makefile:8-9`):

```
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m pytest \
    -p no:cacheprovider tests/test_checkpoint_manifest_pin.py -q
```

Final summary line, verbatim:

```
5 failed, 5 passed, 22 xfailed, 1 warning in 0.43s
```

Matches the request-stated baseline `5 failed, 5 passed, 22 xfailed`.
Failed rows at baseline (all outside L1 scope):
`test_neg39_envelope_policy_revision_mandatory_and_stale_revision_refused`,
`test_pos05_envelope_terminal_content_digest_mandatory`,
`test_body_task_object_mandatory_with_stable_ids`,
`test_pos05_digest_covers_full_content_except_the_digest_field_itself`,
`test_pos16_restore_side_validation_covers_content_predecessor_and_terminal`.

## (v) Provenance

- Fetched `origin` on 2026-11-17; base head quoted above; base branch not
  merged into `origin/main` (checked: `git branch -r --merged origin/main` → 0).
- Record committed as the FIRST commit on
  `easybutt0n-171/impl-L1-envelope-mandatory`, before any manifest.py edit.
