# CONFORMANCE: checkpoint/delta manifest library vs the W-3 F12 schema pin

Task: easybutt0n-171 leaf B (pack task T4.1). This file is contract step 2 of
the leaf: a rule-by-rule mapping of the pinned W-3 F12 manifest schema onto
the existing manifest library. It records, for every pinned rule, where the
library implements it (file:line) and a verdict: conforms / differs /
missing. It is documentation only -- it changes no code and claims nothing
about runtime behaviour. The verdicts below describe the library as it
exists at this commit and are the gap list that the pin tests (step 3) and
the library change (step 5) of this same leaf are closed against.

## Provenance

- Pin (read-only authoritative input): repository
  cogniziocompany/claude-code-teams, branch cognizio-overseer, file
  docs/v15/t0.3/w3-schema-version-compatibility-pin.md, commit
  f345141aad6d0a1bcbe68e44b98549493b782487 (fetched fresh this round).
  The branch moved cd34cda93..f345141aa since the round_001 audit;
  `git diff cd34cda93..origin/cognizio-overseer` on the pin file and on the
  admission contract is empty, so the audited line spans are unchanged.
- Acceptance semantics A1/A2: same repository and branch,
  agents/orchestrator/plans/native-171-admission-contract-20261114.md
  lines 20-33.
- Mapped code: src/lh_harness/checkpoint/manifest.py and
  src/lh_harness/checkpoint/__init__.py on branch
  easybutt0n-171/leaf-b-manifest-pin-conformance (base origin/main
  35ea475f). This commit does not touch either file, so every line number
  below resolves identically on origin/main.
- Context for the verdict density: the library's own source of record
  (src/lh_harness/checkpoint/__init__.py:19-25) is the cognizioware
  design/factory-specs/v1.5 extract at f4d8b2a9, written before the W-3 pin
  was merged. Rows marked differs/missing are expected; none is skipped.

Reading the tables: "pin LNNN" means
docs/v15/t0.3/w3-schema-version-compatibility-pin.md line NNN. Verdict
conforms = the pinned rule is enforced at the cited location. differs =
some code enforces a related but not pinned-conformant rule; the note says
what. missing = no enforcing code exists. Every differs/missing row carries
a note.

Row totals: 31 rules mapped -- 7 envelope (3.1), 9 body (3.2 table rows 1-7
plus the two rules of the closing closed-set paragraph), 3 digest rules
(4.2), 4 predecessor rules (4.3), 8 removal rules (section 7 table rows 1-7
plus the removal-is-validation rule). Summary: 8 conforms, 7 differs,
16 missing.

## 3.1 The manifest envelope (pin lines 181-187) -- 7 rules

| Pin rule (ptr) | Where (file:line) | Verdict | Note |
|---|---|---|---|
| `policy_revision`: string, required; join key to section 5 config and section 2 schema version; a manifest not matching the revision in force is stale and must not be consumed (L181) | manifest.py:264-272 (`_require_scope_binding`: a document's `policy_revision` must equal the trusted handoff's), applied at manifest.py:467 and manifest.py:512 | differs | Enforced as a handoff-pinned scope key compared for exact equality, not as a typed envelope field on the manifest; no join to any config surface or schema-version rule is performed. |
| `schema_version`: string, required; version of the manifest's own field set, governed by section 2 (L182) | manifest.py:47 (`SCHEMA_ID`), manifest.py:258-261 (`_require_schema`) | missing | The only schema id in the library is the trusted handoff's (`trusted-handoff/v2`); no manifest carries a `schema_version` for its own field set and nothing checks one. |
| `base_digest`: string, required; the trusted base content digest (L183) | manifest.py:232 (`TrustedHandoff.expected_base`), manifest.py:469-479 (base identity, sequence and content vs the trusted base), manifest.py:170 and 518-519 (each delta continues from the previous link) | differs | The trusted-base relationship is enforced, but as equality against a trusted `{checkpoint_id, sequence, digest}` triple on the handoff plus a per-delta `predecessor_digest`; no manifest envelope field named `base_digest` exists. |
| `terminal_content_digest`: string, required; digest of the manifest's own content (L184) | manifest.py:88-98 (`document_digest`), manifest.py:136-138 and 196-197 (`computed_digest`), manifest.py:475-477 and 521-523 (recomputed and compared, never trusted as carried) | differs | The self-excluding content digest exists, but the field is named `digest` and only the `digest` key is excluded; the pinned name and its meaning (covers the pinned 3.1 + 3.2 field set) are absent because most of those fields are themselves missing below. |
| `predecessor_digest`: string or null, required; digest of the immediately preceding manifest, null for the chain head (L185) | manifest.py:170 (`DeltaManifest.predecessor_digest`), manifest.py:518-519 (must equal the predecessor's recomputed content digest) | differs | Enforced for deltas as a non-null string; the chain head is a `CheckpointManifest` (manifest.py:117-122) that has no `predecessor_digest` field at all, so "string or null, required on every manifest" does not hold. |
| `created_at`: timestamp (UTC), required; when the manifest was produced; not a trust anchor (L186) | none | missing | No manifest field and no validation records or checks production time. |
| `producer`: string, required; logical producing component in stable component/task id form (L187) | none | missing | No manifest field and no validation names the producing component. |

## 3.2 The mandatory value schema, manifest body (pin lines 199-210) -- 9 rules

| Pin rule (ptr) | Where (file:line) | Verdict | Note |
|---|---|---|---|
| 1. `task`: object, required; `task_id`, `requirement_ids`, `attempt_id`; stable ids (L199) | manifest.py:268-272 (`task_id` is one of the three scope keys every document must carry and must equal the trusted scope) | missing | Only `task_id` exists, as a scope-binding key; there is no `task` object, no `requirement_ids`, no `attempt_id`. |
| 2. `revision`: object, required; `policy_revision` and `schema_version` mirrored from the envelope, plus `source_revision`; mirrored fields disagreeing with the envelope make the manifest invalid (L200) | none | missing | No `revision` object and no envelope-mirror agreement check anywhere in the library. |
| 3. `config`: object, required; immutable, named, digest-covered value map; key set fixed by the W-2 store inventory, shape pinned here (L201) | none | missing | No `config` field; nothing binds, checks or digests a named configuration map. |
| 4. `cursor`: object, required; `scope`, `position`, `epoch`; a cursor with no epoch or a superseded epoch is stale and must be rejected (L202) | manifest.py:121 (`CheckpointManifest.cursor`), manifest.py:129 (serialized into the digested document) | differs | A `cursor` exists and is digest-covered, but it is typed `Any`: no required `scope`/`position`/`epoch` subfields and no staleness or epoch-supersession rule. |
| 5. `question`: object or null, conditional; present exactly when the work item has an open question; `question_id`, `asker`, `answerer`, `request_scope`, `state`, `answer` (L203) | none | missing | No `question` field and no conditional-presence predicate. |
| 6. `action`: object, required; `action_id`, `kind`, `arguments_digest`, `dispatch_state`, `receipt_ref`; stable action ids (L204) | none | missing | No `action` field of any kind. |
| 7. `child`: object or null, conditional; present exactly when a child is spawned; `child_id`, `child_kind`, `parent_manifest_digest`, `child_state`, `child_receipt_ref` (L205) | none | missing | No `child` field; the `parent_manifest_digest` link does not exist. |
| 8a. closed set, coverage: every body field is inside the digest-covered content; no body field may be excluded from the content digest (L207-208) | manifest.py:88-98 (`document_digest` hashes every key except the digest key itself), manifest.py:124-134 and 182-194 (every typed field plus any scope keys are serialized into the digested document) | conforms | -- |
| 8b. closed set, closure: the manifest field set is a closed set; a manifest may not carry a field outside it; adding a field requires a new `schema_version` (L208-210) | none | missing | Unknown fields are accepted and digested, not rejected -- the scope merge at manifest.py:131 and manifest.py:191 injects arbitrary keys into the digested document, and raw mappings are accepted at manifest.py:462-466 and manifest.py:511; no closed-set or `schema_version` gate exists. |

## 4.2 Terminal content digest (pin lines 229-241) -- 3 rules

| Pin rule (ptr) | Where (file:line) | Verdict | Note |
|---|---|---|---|
| Every manifest carries a digest over its full content -- the 3.2 body field set and the 3.1 envelope -- except the digest field itself, which cannot contain its own digest (L229-232) | manifest.py:88-98 (`document_digest`: the document minus its own `digest` key), computed at manifest.py:136-138 and 196-197, enforced at manifest.py:475-477 and 521-523 | conforms | The digest field is named `digest` (see the 3.1 `terminal_content_digest` row); the exclusion-by-self rule itself is exactly this rule. |
| The digest is computed over a canonical serialization; deterministic -- same content, same digest on every producer; a re-ordered or re-formatted but equal body must not change the digest; the hash function is the implementation's choice (L233-237) | manifest.py:70-85 (`canonical_bytes`: sorted keys, compact separators, `allow_nan=False`), manifest.py:50 (`CANONICAL_JSON_VERSION` pins the serializer version) | conforms | -- |
| Any change to a body field changes the digest; any change that does not change a body field must not change the digest (L238-241) | manifest.py:96-98 (the digest covers the full unsigned document, so any content change changes it), refusals at manifest.py:475-477 and 521-523 | conforms | -- |

## 4.3 Predecessor digest links (pin lines 245-256) -- 4 rules

| Pin rule (ptr) | Where (file:line) | Verdict | Note |
|---|---|---|---|
| Every manifest carries `predecessor_digest`: the terminal content digest of the manifest it follows, or null for the chain head (L245-246) | manifest.py:170 and 518-519 (deltas); chain head is `CheckpointManifest` at manifest.py:117-122 | differs | The rule holds for deltas; the head is modelled as a different manifest type with the field absent rather than an envelope field that is null. |
| The chain is a hash-linked sequence: each manifest's `predecessor_digest` names the previous manifest's terminal content digest, and each manifest's `base_digest` names the trusted base it continues from (L247-249) | manifest.py:514-519 (contiguity, predecessor identity, predecessor digest), manifest.py:579-581 and 597-603 (each link validated against the previous link's recomputed digest), manifest.py:469-479 (the base continues from the trusted base) | conforms | Under the library's names: `predecessor_digest`, and the trusted base carried as `TrustedHandoff.expected_base` (manifest.py:232) rather than an envelope `base_digest`. |
| A predecessor link resolving to a manifest whose stored terminal content digest does not equal the linking value is a broken link and makes the whole chain segment from that point untrusted (L250-253) | manifest.py:518-519 (the link value must equal the predecessor's recomputed content digest, not a stored claim), manifest.py:521-523 (a stored digest disagreeing with recomputed content is refused) | conforms | -- |
| Restore must perform content/predecessor/terminal digest validation (T4.3 wording carried into the pin, L254-256) | manifest.py:521-523 (content/self digest), manifest.py:518-519 (predecessor), manifest.py:632-633 (terminal triple vs the trusted receipt) | conforms | The validation half only: restoration is T4.3 and is deliberately absent from this library (see `__init__.py:12-17`); no restore exists or is claimed here. |

## 7. Explicit removals (pin lines 286-304) -- 8 rules

"A manifest may not carry a field that is not in the pinned field set"
(pin L286). Each row below is a removed element a manifest must not
contain, plus the closing rule that removal is a validation, not a
deletion.

| Pin rule (ptr) | Where (file:line) | Verdict | Note |
|---|---|---|---|
| Free-form narrative / summary text removed (L293) | none | missing | Nothing rejects non-pinned narrative content; this is the closed-set gap of rule 3.2 8b. |
| Secret material, credential values or private key material removed (L294) | none | missing | No rule rejects secret-looking keys or values anywhere in validation. |
| Latency, timing or performance values removed (L295) | none | missing | No rejection rule. |
| Threshold values, targets or SLOs removed (L296) | none | missing | No rejection rule. |
| Host identifiers, node names or session identifiers removed (L297) | none | missing | Nothing forbids environment-identifying keys; the scope binding (manifest.py:268) requires only `tenant_id`/`task_id`/`policy_revision` and the scope merge (manifest.py:131, manifest.py:191) admits anything else. |
| The predecessor's content itself removed; linking is by digest only (L298) | manifest.py:167-174 (`DeltaManifest` carries the predecessor digest only, never the predecessor body) | differs | The typed delta cannot express it, but raw-mapping validation (manifest.py:511) does not reject a document that embeds predecessor content; only the missing closed-set rule would. |
| An unlisted or extension field removed; any extension requires a new `schema_version` (L299) | none | missing | Extra keys are accepted and digested (manifest.py:96-98, manifest.py:131, manifest.py:191); there is no `schema_version` gate. |
| Removal is a validation, not a deletion: a manifest carrying a removed element is invalid and must be refused, never edited to strip the element, because editing would change its digest and break the link that referenced it (L301-304) | manifest.py:53-59 (`IncompleteContext`: a chain that cannot be trusted is visibly incomplete, never partially repaired); every check in the module raises instead of mutating, and the module is pure (manifest.py:34-36) | conforms | Refuse-not-repair is the module's single failure mode; it becomes operative for the rows above only once their detection rules exist. |

## Summary

- 31 pinned rules mapped, none skipped: 8 conforms, 7 differs, 16 missing.
- The library's digest spine (canonical serialization, self-excluding
  content digest, hash-linked predecessor chain, trusted base and terminal
  triples, refuse-not-repair) already conforms to pin sections 4.2 and 4.3
  in mechanism.
- The gaps concentrate in the pinned field set: the envelope fields
  `schema_version`, `created_at`, `producer`; the body objects `task`
  (beyond a scope-bound `task_id`), `revision`, `config`, `question`,
  `action`, `child`; the typed shape and epoch rule of `cursor`; and the
  closed-set / removed-element rejection rules of 3.2 8b and section 7.
- These verdicts are the input to step 3 (pin tests, red before the library
  changes) and step 5 (change manifest.py only as far as needed to go
  green) of this leaf. Nothing in this file changes code.

## Shape contract: pin versus library (PROPOSAL, not decided)

Re-slice A (2026-11-16) of ticket easybutt0n-171 records how the pinned
content built by `_pinned_content` (tests/test_checkpoint_manifest_pin.py)
differs from the document shape the manifest library validates. Each row
below is one difference the test-local adapter `_adapt_pinned_to_library`
(tests/test_checkpoint_manifest_pin.py) bridges today. This is a proposal,
not a decision: nothing here is implemented as a library change, agreed,
or scheduled -- re-slice B (reworking the 31 pin tests onto the adapter)
and any manifest.py change remain undecided and out of scope.

Two rows name absences on the pin side, not library defects: the pin has
no tenant field and no checkpoint identity of its own. The library renders
a document's scope keys at the top level because `as_document` merges the
scope mapping into the rendered document (manifest.py:131).

| Field | Pin form | Library form | file:line in manifest.py |
|---|---|---|---|
| `tenant_id` | absent: the pin has no tenant field; the pinned trusted handoff carries `tenant_id` as a scope fact | top-level scope key; the document's value must equal the trusted handoff scope's value | manifest.py:268-272 |
| `task_id` | nested: `task.task_id` inside the pinned `task` body object | top-level scope key; must equal the trusted handoff scope's value | manifest.py:268-272 |
| `policy_revision` | envelope field on the manifest (pin 3.1) | top-level scope key read by the scope binding, not as an envelope field; must equal the trusted handoff scope's value | manifest.py:264-272 |
| `checkpoint_id`, `sequence` | absent: the pin carries no checkpoint identity or sequence; the pinned handoff's `expected_base` names them | required document fields, compared for identity and sequence against the trusted base triple | manifest.py:117-118, manifest.py:469-473 |
| remaining pinned fields (`schema_version`, `base_digest`, `predecessor_digest`, `created_at`, `producer`, `task`, `revision`, `config`, `question`, `action`, `child`) | top-level fields on the pinned manifest | carried inside `state`, which the validator requires to be present and an object | manifest.py:119, manifest.py:481-484 |
| `cursor` | top-level pinned body field (pin 3.2 rule 4) | dedicated `cursor` slot on the rendered document, digest-covered like the rest | manifest.py:121, manifest.py:129 |
| digest field | `terminal_content_digest`: sha256 over the full pinned content minus that key | `digest`: sha256 over the document minus its own `digest` key, recomputed by the validator and only compared as carried | manifest.py:88-98, manifest.py:475-477 |
