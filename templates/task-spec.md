---
title: ""
task: "<queue name>"
workspace: ""
status: draft            # draft | ready-for-dev  (launch gate)
context: []              # files the role must read itself; never inline them
source_task_file: ""
created: ""
---

<!-- OVERSEER: HOW TO FILL THIS SPEC (BMAD-derived; sections are the contract the roles read)
     - Intent: one paragraph of Problem, one of Approach, 3-6 lines total. Any note-style
       rationale from the queue entry ("Paxton asked for X because Y, measured Z") goes HERE as
       ONE paragraph - not scattered into other sections, not reproduced verbatim.
     - Boundaries: Always = invariants the roles must hold. Never = out of scope + forbidden
       approaches. Terse bullets; a boundary the roles cannot check is not a boundary.
     - I/O & Edge-Case Matrix: one row per scenario the result must handle. Delete the section if
       no meaningful I/O exists; never write "N/A".
     - Code Map: paths only, each with a role-or-relevance phrase. No retelling of how you found
       them. `context:` in the frontmatter lists files the role must READ ITSELF; never inline
       file contents into this spec.
     - Tasks & Acceptance: checklist of `path -- action -- rationale`, then Given/When/Then
       acceptance criteria for behaviours the matrix does not already cover.
     - Verification: exact commands with the expected result. Manual checks only when no CLI
       check applies.
     - Open Questions: one entry per intent gap the human would notice. The spec cannot leave
       draft while any entry remains.
     - SIZE: there is no size rule here. The app measures the spec (spec_chars / spec_tokens_est)
       and flags it against the fleet's established range; write what the task needs.
     - GATE: flipping `status: draft` -> `status: ready-for-dev` is what makes the launcher take
       this task. Until then it prints SPEC_PENDING and skips. Remove this comment when done.
     - If a "## Source task (verbatim, delete once distilled)" section was appended below,
       distill it into the sections above and delete it. -->

## Intent

**Problem:** ONE_TO_TWO_SENTENCES

**Approach:** ONE_TO_TWO_SENTENCES

## Boundaries

**Always:** INVARIANT_RULES

**Never:** NON_GOALS_AND_FORBIDDEN_APPROACHES

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| HAPPY_PATH | INPUT | OUTCOME | N/A |
| ERROR_CASE | INPUT | OUTCOME | ERROR_HANDLING |

## Code Map

- `FILE` -- ROLE_OR_RELEVANCE

## Tasks & Acceptance

**Execution:**
- [ ] `FILE` -- ACTION -- RATIONALE

**Acceptance Criteria:**
- Given PRECONDITION, when ACTION, then EXPECTED_RESULT

## Verification

- `COMMAND` -- expected: SUCCESS_CRITERIA

## Open Questions

- CHOICE -- options: OPTION_A (CONSEQUENCE_A) / OPTION_B (CONSEQUENCE_B)
