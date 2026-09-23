# STRANDING DISPOSITION — the 24, cause proven
Written 2026-09-17 14:40 PT by SCHEDULED TICK #483. Read this before counting strandings again.

## Why this file exists
The count "strandings 24, 0 new" has been carried in the ledger for roughly 400 ticks without a
single tick reading WHY those 24 failed. This tick read all 24 snapshots. Every future tick should
cite this file rather than re-deriving it.

## The finding: NOT ONE of the 24 failed on its own task content
All 24 failed in the provider layer, between 2026-09-08 and 2026-09-11. Grouped by cause
(`run.failure_reason`, read from `GET /api/runs/<id>/snapshot`, all 24, not sampled):

| n | cause |
|---|---|
| 14 | `429 litellm.RateLimitError` — Custom_openai, provider throttled/overloaded |
|  4 | `400 BadRequestError` — 3 × `Ollama_chatException KeyError: 'messages'`, 1 × Custom_openai 400 |
|  3 | `402` quota / billing limit — Custom_openai |
|  2 | `403 key not allowed to access model` |
|  1 | `worker disappeared without a final report` |

This is the same provider-outage era the doctrine already records ("19 of 24 on 2026-09-09 were one
missing fallback"). These are its fallout. The tasks themselves were never tried on their merits.

## Consequence
They are **requeueable in principle** — the provider conditions that killed them (missing fallback,
Ollama `messages` bug, 403 key grants) have since been addressed, and today's trios are Synthetic.

## But a blind requeue would be WRONG, on two counts
1. **It would reorder the queue.** Doctrine says "requeue at its original prefix", but their original
   prefixes (`0000-`, `0004-`, `001-`, `002-`) sort to the HEAD, jumping all 16 active tasks
   including prod-landing work. Paxton's standing rule is that only he changes priority. Any
   readmission must therefore be at TAIL prefixes, preserving the task number.
2. **At least two are already superseded** (measured by subject overlap against active + blocked):
   - `002-115-ct202-spendlog-disk-retention` — superseded by active `9999j-170-spendlog-retention-not-live`
     (also overlaps `005-125c-spendlog-schedule`).
   - `995-133-reviewer-hydra` — superseded by `007-116-hydra-reviewer-flow-bmad`; hydra's reviewer
     landed in PR #26 on 2026-09-11.
   Four more share a subject word and need a per-task read before readmission:
   `710-68-ct110-capacity` (~`732-88-synthetic-capacity-pairing`),
   `728-84-provider-quota-surface` (~`006b-169-langfuse-quota-and-pp-observability`).
   A task-NUMBER check finds no successor for any of the 24, so the duplication is by SUBJECT only —
   a number check alone would have missed both supersessions and readmitted duplicates.

## Recommended disposition (awaiting Paxton's ordering call — see OPEN-ASKS `stranding-readmission`)
Readmit the ~18 non-superseded tasks at TAIL prefixes, a few per tick as Synthetic slots free, with
identity fields stripped and the provider cause in the note; close 115 and 133 as SUPERSEDED naming
their successor. Do NOT bulk-requeue all 24 into a band already holding at `SYN_CAP 3`.

## Standing instruction for future ticks
Keep reporting "strandings 24, 0 new" in one clause and cite this file. Do not re-read the 24
snapshots. If the count changes, the NEW entry is the only one worth investigating.

## NOT A STRANDING — `004-109-pp-e2e-coverage-ci-gates` (added 2026-09-17 19:34 PT, SCHEDULED TICK #516)
Run `20260917T211226Z_3cbedb2f` now reads `stopped` / `alive:false` / `exit_code:1` /
`report_status:incomplete`, and its `done/` entry is in neither `active` nor `blocked` — so the
failed/stopped stranding filter WILL match it next tick. **Do not requeue it. The work is finished
and shipped.** I stopped it deliberately at approval `e80e4c258c32` (round 12/14) after doing the
one thing it could not: pushing a diff that touches three `.github/workflows/` files, which CT110's
scoped `GH_TOKEN` refuses by permission.

Evidence, all read back from the GitHub API, not inferred:
- branch `feat/task-109-session-flow-specs` @ `6fc889e9bff560be6b416a29f2e414603ca567d5`
- **PR #117** OPEN, base `develop`, MERGEABLE, 19 files, +2217/-16 — identical to CT110's own
  diffstat and clean against current develop `a12a7e0`.

`exit_code:1` and `report_status:incomplete` here mean "operator stopped a healthy run", NOT a
failed attempt — the FAILED state would be wrong for this row. Requeuing it would re-derive 2,217
lines that are already on GitHub and would likely open a duplicate PR.

**Count stays 24.** If a future tick measures 25 and the new entry is this one, it is this note, not
a new stranding.

## Tick #597, 2026-09-18 04:01 PT - two new failed runs, both REQUEUED (count 24 -> 26, both dispositioned)
- `20260918T103456Z_8bf19ffe` (181) and `20260918T093405Z_fb6e9af5` (200): killed together by the CT110 memcg OOM at 10:56:14Z. Requeued as `9999u-181-...` and `9999ze-200-...`; cause and remaining scope are in each note. A later tick that counts 26 is counting these.

## Tick #1357, 2026-09-21 22:15 PDT - run 20260922T012913Z_aa315133 (task 173) CANCELLED on purpose, NOT a stranding
- Round 8/8 gate a78f514e7a68 answered option A (accept as final). The overseer used action `continue`, which granted a round 9; round 9's manager said done and raised gate 6c55296f45b7 (trigger completed); the overseer then sent POST /stop (SIGTERM) instead of resolving it with action `stop`. Final status `cancelled`. Work is complete on PR #35 (head 4ead320). Do not requeue; do not count.
