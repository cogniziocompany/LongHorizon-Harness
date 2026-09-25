# Audit of the overseer (`longhorizon-harness-99`) artifacts, queue, and Langfuse usage — findings and fix list

## Session & agent identity — for a reviewing agent

**This session**
| field | value |
|---|---|
| Agent name (address it with this) | Not assigned — no fleet/harness agent name was issued to this session |
| Short ref | `[fe2679]` |
| Session UUID | `fe2679d4-b905-5a8a-ae87-f3ce1d1143cb` (newest transcript in the project dir; inferred, not given directly) |
| Repo / cwd | `C:\Users\PaxtonTait\source` |
| Role | Interactive CLI session, auditing on behalf of the user. Not the overseer. The overseer is `longhorizon-harness-99` `[2df7a7]`, session `5321285c-35e2-459a-9dae-92ea8811669f`, `/loop` cron `f91de072` |

## Context

Paxton asked for an audit of the overseer's two dashboards (Ship Plane, Shipped to Production — saved as HTML in `~/Downloads`), its task queue at `C:\tmp\queue`, and the lh-harness orchestrator contract, plus a high-level read on why the Langfuse free tier (50,152 / 50,000 events, resets 2026-09-30) is exhausted and whether powerplatform is instrumented. Everything below was read from disk / gh / the running launcher process on 2026-09-11; nothing was changed.

**Live state at the overseer's latest tick (~10:45 PT 2026-09-11), cross-checked on disk:** five runs active (153 r3, 143 r2 auditor, 69b r2, 134 r2, 159 r1), zero gates, both GPUs 94/94, litellm 1.30 GiB of 6 GiB, 0 restarts, oom_kill 138. Synthetic recovery ramp widened 1→3→5. The overseer also found a 14th stranding: task 135 (`756-135-seq-powerplatform-prod-logs`) failed at 02:25 on a Synthetic 429 while filed in `done/` at launch, invisible for eight hours; it requeued 135 by hand at its `756-` prefix. Disk now: active 24, blocked 15, done 148.

Two consequences for this audit: (1) all five running tasks' JSONs sit in `done/` right now (`000-153`, `1001-143`, `711-69b`, `992-134`, `9999-159`), so the file-on-launch defect in A/B/D7 is the root cause of the stranding class the overseer keeps hitting, now 14 times; (2) the queue is under heavier load than when the artifacts were published, so nothing that pauses the launcher (zero-pad rename, launcher patch/restart) should run until ACTIVE drops, and the overseer's counted-drain rule applies to this plan too.


## Findings — ranked

### A. Orchestration queue: the standing requirement is to move it off this PC onto CT110, and the cutover step is unowned

**The requirement, as already submitted (not new):**
- Ship Plane l.15: "Today that is a JSON entry in the queue folder on this PC; after the CT110 release it is the harness queue (POST /api/queue, the chat's lhharness tool, or the Hydra panel), and the queue chooses a model per role from what is actually available and records why (task 49)."
- `LongHorizon-Harness/tasks/dynamic-model-routing-2026-09-08.md:29,35`: the queue is the decision point for every entry point (chat.easybutt0n.ai via the `lhharness` gateway alias, Hydra); "release CT110 at idle, **retire `C:/tmp/launch_queue.py`**, rotate the CT110 bearer it embeds".
- `tasks/fleet-entry-point-and-harness-ux-2026-09-07.md`: deploy order harness release → CT110 idle window → gateway alias/key for `lhharness`.
- Task 134 (running now, `4edf6c98`): PgQueueStore behind the existing `QueueStore`, `blocked` added to `_VALID_STATUS`. Its own text lists **"Step 0, cutting the queue over off C:/tmp — overseer/ops work, not run work"** as out of the run's scope.
- Task 145 L70: "`GET /api/queue` and friends ARE already deployed on CT110 (22 endpoints); nothing writes to it."
- Task 150 L60: "THREE TASKS NOW EDIT C:/tmp/launch_queue.py. THIS MUST BE SEQUENCED." Task 155 (blocked) edits the launcher; task 104 (blocked, task_file missing) migrates `C:/tmp` into the repo.

**What CT110 already has** (`src/lh_harness/queue.py`, `webapi/server.py`): integer `priority` (`:73`, `POST /api/queue/{id}/priority` at `server.py:1175`), `trio` validated against `{kimi,qwen}` (`:184-191`), a `Launcher` class wired to the store (`server.py:887`), `POST/GET/DELETE /api/queue`, spec-staging routes, and pending/launched/done/failed statuses that distinguish launch from completion.

**So every defect found in `C:\tmp\launch_queue.py` is already solved by the deployed CT110 queue, and none of them should be fixed twice:**
| PC launcher defect (evidence) | CT110 equivalent | disposition |
|---|---|---|
| lexicographic sort inverts the 1002-band vs 740/990 (`:579`) | integer `priority` + priority endpoint | do not zero-pad rename; carry priority as an int at cutover |
| launcher dies on one malformed JSON (`:610-611`, no try/except) | API validates on `POST /api/queue` and returns 400 | no PC patch beyond a one-line guard if the launcher must live > 1 week |
| `trio: "default"` on 740-97 silently bypasses the kimi safety block (`:662-702`) | `_validate_trio` rejects it | fix the one file to `kimi` now (it is a live hazard); nothing else |
| `blocked_until` is prose, nothing reads it (721-77b) | `blocked` status + transition table from 134 | encode as a status at cutover |
| default `max_rounds=8` vs packaged 25 (`types.py:42`); nine blocked rows at `1` | one default in the API config | decide the default in 134's config, not the PC script |
| **files to `done/` on launch (`:763`)** — 14 strandings incl. 135 today; five running tasks sit in `done/` | `launched` vs `done` statuses already distinct | this is the strongest reason to cut over; no PC rework |
| KEYS SHORT / SPAN FULL choice overwritten by the rotation loop (`:744-746`, section G) | "the queue chooses a model per role and records why" (task 49) | fix belongs in the CT110 `Launcher` role selection; note it in 146 as the interim PC patch only if cutover slips |

**The gap this audit adds:** nobody owns **Step 0, the cutover**. It is out of 134's run scope by design, not in 150/155/104, and the overseer's LOOP-PROMPT still reads `C:\tmp\queue`. Queue one overseer-executed task, `harness-queue-cutover` (blocked until 134 lands and at a counted zero-active window, like 144/147/150), scope:
1. Import the 24 active + 15 blocked entries into `POST /api/queue` with int priorities in the current file order, `blocked` status preserved, notes carried as the entry rationale; `done/` is NOT imported (it is an upper bound, section D7) — reconcile each `done/` row against `/api/runs` first.
2. Set `DRAIN`, confirm ACTIVE=0, stop the PC launcher, rotate the CT110 bearer it embeds (task 49 doc), archive `C:/tmp/launch_queue.py` into the repo per task 104.
3. Point the overseer's LOOP-PROMPT at `GET /api/queue`; the Ship Plane's "live counts: GET /api/queue" box becomes true.
4. Expose the queue through the public entry points already specified: `lhharness` gateway alias on the LiteLLM gateway and the chat.easybutt0n.ai tool, with the queue API token on the alias, not on this PC.
Prereq on Paxton: the `lh_harness` database/role on CT103 (134's stated prerequisite).

**Still true regardless of cutover:**
- Task 154's "manager=1200" fix targets the wrong role: failures were executor timeouts at 3600 s; `config.py:27` has no `executor` key, only `gui_executor`/`cli_executor`; 3600 appears nowhere in the repo. Fix in the CT110 workspace `.lh-harness/config.toml` and correct the note.
- "Three strikes" in code (`dashboard/rules.py:25-60`) only requests review at 3 trailing failures; the same-role discriminator is prose. Stops are manual today; once the CT110 `Launcher` owns launches it can also own the stop.

Checked out: PC launcher PID 102836 started after the script's mtime, so its 00:30 patches are live; log fresh; `.bak` files invisible to the glob; DRAIN not set.

### B. Queue files (`C:\tmp\queue`)
1. **`blocked/745-104-tmp-to-repo-migration.json` cites a task_file that does not exist** (`C:/tmp/tmp-to-repo-migration-task.txt`). Only missing task_file of 38. Fix: recover from a `.bak`/transcript or rewrite the brief before any unblock.
2. **`blocked/1013-157-litellm-container-memcap.json` is finished but still parked** — PR #143 merged, `mem_limit` applied live (OPEN-ASKS L60-71), yet the note still asks Paxton to post `@claude`. Fix: move to `done/` with evidence in-row (same class as 151/82).
3. **Task 154 requeued 3× in ~5 h**, nine failed episodes, note is 4 KB of `|| ***` strata, and nine uncommitted files on the CT110 workspace. Fix: fix the executor timeout (A4) before relaunch, and commit or stash the WIP.
4. **Stale `run_id`/`launched_at`** on 4 requeued rows (125a, 97, 77b, 88); 125a's row names a run its note never mentions. Fix: strip identity fields on requeue (launcher overwrites them anyway).
5. **`blocked_until` on 721-77b is prose**, nothing reads it. Either drop the key or make it an ISO date the overseer checks.
6. **Prefix collision `1002-`** (144 blocked, 145 active). Fix with the zero-pad rename in A1.
7. **name ≠ filename on 4 rows**, worst `990-87-…` whose `name` is `731-87-…`. Fix: rename `name` to match.
8. **`workspace` is a sentence** on `blocked/1011-155…`. Fix: point at LongHorizon-Harness workspace or a dedicated `ptait09-tools` repo once 155 moves `launch_queue.py` into git (which is that task's point).
9. **Orphan `.bak` files in `blocked/`** for 82 and 152. Fix: move to `done/` or delete.

### C. Ledger / OPEN-ASKS hygiene
1. Band counts drifted: ledger last says `active 23, blocked 16, done 140`; disk says `23 / 15 / 149`. No band line in the final ~3,190 ledger lines. Fix: emit a measured band line on every move.
2. `OPEN-ASKS.md` header says "Updated 2026-09-10 23:00 PT" but has entries to 09:37 PT 09-11; the "SYNTHETIC CREDIT EXHAUSTED — blocking the queue" heading (L123) is stale (synthetic=ok since 10:23). Three de-facto open questions live in prose, not the table (terranas01 host key L169, canonical spendlog path L211, synthetic credit L123). Fix: one row per open thing, date the header.
3. The table's only row, `144-window`, is APPROVED — by its own Rule 2 it is not an ask. Fix: move to Answered; the drain-window execution is task 147's job.
4. The four "still needs you" asks from yesterday are all already Answered in the file (push-handoffs, 135-ssh key). Only the Seq ingest key and the mcp-cogn-gh capability decision are still genuinely on Paxton.

Checked out: no illegal row states (COMMITTED/DONE) in the ledger; no duplicate names; JSON all valid.

### D. Ship Plane / Shipped to Production (saved HTML in `~/Downloads`, text at `C:\tmp\artifacts_text.txt`)
Both pages fail their own stated purpose. Ranked:
1. **The Ship Plane has no task state board.** Header promises "every harness task with its PR / lane / environment"; none of the 38 active+blocked task ids appear. The eleven-state table exists only in LEDGER.md. Fix: publish the ledger's `| task | state | evidence |` table on the page, generated from the queue dirs + ledger, not hand-typed.
2. **"Shipped to Production" admits ~10 entries that are not shipped** (lines 302-336: "held", "queued", "open for review", "I have not started", and line 307 "recorded as done and it is still there"). Fix: move them back to the Ship Plane; enforce the page's own rule (verified read-back only).
3. **Retracted 625.9M-token figure still stands on the Shipped page** (l.305) though the Ship Plane retracts it twice. Fix: propagate the correction.
4. **"Needs Paxton" PR figures are stale by 2 days**: mcp-tools 12 open/4 mergeable vs real 16/6 (#111,#113 merged 09-09; #112 conflicting); mcp-cognizioware 7/3 vs 5/1 (#91,#92 merged); LH omits #10. **#116/#117 "kept off CT202 tonight"** were merged 09-09. Fix: regenerate from `gh pr list` at publish time.
5. **Prod LiteLLM version stated three ways** (v1.100.0 l.215/254 vs v1.100.1 l.78); truth v1.100.1 via #142.
6. **Executor context window stated three ways** (128K l.89 marked LIVE, 64k l.260, 256K l.200); truth 262144 via #136 (VERIFIED). Line 89 attaches "verified on the router" to a wrong number.
7. **`done/` holds running and unfixed tasks** because the launcher files on launch (`launch_queue.py:763`): all five live runs (153, 143, 69b, 134, 159) plus 110 ("still unfixed"), 114 (REQUEUED; page l.48 calls it a live defect), 20c (empty note, PR #120 open/conflicting). This is the mechanism behind 14 strandings including task 135 today. **Highest-value fix in this audit:** launcher moves the entry to a `running/` dir on launch and to `done/` only when `/api/runs/<id>` reports a terminal success; a failed/stopped run goes back to active with the failure written into the note. Until then treat `done/` as an upper bound.
8. **Task 157/#143**: merged per gh and OPEN-ASKS, "PR IS OPEN" in blocked/, unmentioned on the page (see B2).
9. **Stale "Next" actions**: #85 "rebased, mergeable" merged 09-08 (l.265); "#83 deploying" merged 09-07 while l.256 says audioqa stopped; "deploying #115" merged 09-08 (l.67).
10. **Counts disagree on the same page**: 19 queued (l.249), 20 (l.246), 23 (l.117); no blocked/done/verified-N-of-M anywhere.
11. **Layered staleness under one 10:02 stamp**: Shipped page 18:20 09-10; feature walk-through 00:55 09-08; system map 15:15 09-08. Misses #145, Synthetic recovery, 151/82 close, task 160. Fix: stamp each section.
12. Lesser: 104's "plan kept intact" is false (task_file missing, B1); 154's three strikes unnamed; #134 superseded draft still counted open; #122 duplicate of shipped #128 still open; billing hostname contradiction l.210 vs l.262 (needs DNS probe).
Matched: #142, #143, #74, LH #3, hydra #26, pp #88, #127/#128, all cited shas exist, 50-entry count, dev-only scoping at l.180 is exemplary.

### E. Ledger claims vs GitHub (44 claims checked; 36 match, 4 unverifiable without ssh/token, 4 mismatch)
Matched: PR #140/#142/#143 (mcp-tools), hydra #26, powerplatform #106/#107, `8210a96`→`ff690e1` merge, #144/#145/#36/#35 mergeability, lane run 34566965170 success, HYDRA_REPO_READ_TOKEN present, DRAIN switch built, 151/82/160/153 file locations, task-160 premises, PR #105 UNSTABLE, eval-gate run 34603164085 failed.
1. **"8 of 13 open mcp-tools PRs conflict" is wrong both ways.** Real: 10 of 15 (PRs #2 and #11 omitted, both CONFLICTING). Fix: ledger correction row; make the census `gh pr list --state open` not a hand list.
2. **"`47b00cb` is already on develop" is true of content, false of the commit** — #107 was squash-merged (`c485cc9b`, one parent). Any rebase/cherry-pick of the 154 workspace will replay 440 lines. Fix: the 154 carry-over must be per-path, never `rebase onto develop`; say so in the task note.
3. **Task 154's note says "pushed to fix/setup-wizard-prod"; that branch is deleted** — sha survives only in `refs/pull/107/head`. The next run will look for a branch that isn't there. Fix: amend the note.
4. **`C:/tmp/window-actions.sh` still prints the retracted "#36 is 326 behind main" rationale** (wrong base; real divergence ahead 2 / behind 0). Decision to hold #36 is still right. Fix: correct the comment/echo before the window runs.
Also: the `c8756e7b9a61` router pin is gone from the repo (now `d85c0b36e2fd` in the same four files) — consistent with #142; the running CT202 image was not read.

### G. Trio performance metrics + the "all-Synthetic leaked a local executor" question

**The leak is real, and it is not `_trio_syn()`.** `launch_queue.py:687` (KEYS SHORT) and `:701` (SPAN FULL) set `roles=_trio_syn(...)`, but the rotation loop at `:744-746` then does `roles=_trio(_SEQ[0]+attempt)` for the `kimi` trio unconditionally, overwriting the all-Synthetic choice with the plain table row. `[trio N]` in the log is the plain `_TRIOS` index, so you read it right. Log 10:23: `KEYS SHORT (0 ok, local 2/2) -> all-Synthetic trio 711-69b` followed by `emit-probe [trio 1] ... ornith-1.5:pool=11.7s` and `LAUNCHED` — trio 1 has the ornith executor. Same bug class as the 05:37 qwen note at `:731` ("the rotation loop overwrote roles anyway"). Consequence: with local 2/2 the launcher put a third executor on the single-slot local lane, which is the stampede the fallback exists to prevent; **Confirmed live right now:** `done/711-69b…json` `roles_bound.executor = ornith-1.5:pool` (run `3c241a1c`, launched 17:23:38Z). With 153 and 143 already all-ornith, the local lane holds three executors against `LOCAL_CAP=2`; the launcher's own 10:38 lines read `local 3/2`. The 10:38 launches (134, 159) got all-Synthetic trios only because `_SEQ` happened to land on table rows 2 and 3, which are all-Synthetic — luck, not the guard. The overseer's "all five healthy" tick masks that 69b's executor is queued behind the two ornith runs; its 3600 s executor budget is the likely next casualty. Fix: when the KEYS SHORT/SPAN FULL branch fired, rotate over `SYN_ONLY` indices (`_trio_syn(_SEQ[0]+attempt)`) instead of `EXEC_POOL`, and log `[syn N]`. Files into **task 146** (`synthetic-quota-gate-task.txt`, active, unlaunched, already cites this exact line) — not a new task.

**Metric coverage (no duplicates):**
| metric | owner today | gap |
|---|---|---|
| per-role episode seconds by backend (Synthetic executor 421 s vs ornith 1,648 s / 3,600 s) | none. 145 only says "distinguish queued from computing or say so" (L83-85) | add to **148** trios table: a measured column per trio row — executor median/max episode seconds and n, from `/api/runs` episode timings joined on `roles_bound` |
| emit-probe latency per seat at launch (1.1 s vs 11.7 s) | launcher log only (`emit-probe` lines) | add to **148**: last-10 probe latencies per backend, pushed in the `lh-trios/v1` payload alongside `_TRIOS`/`DEGRADED_TRIOS`; source is the same `push_trios()` patch 148 already delivers |
| planned trio vs bound trio drift (the leak above) | none; `roles_bound` is written (`:761`) but never compared | add to **148**: a `drift` flag per launch = chosen table row ≠ `roles_bound`; 146 fixes the cause, 148 makes recurrence visible |
| local-slot queueing (11.7 s probe, 72 s timeout when slot busy) | 145 L83 (display), LOCAL_CAP guard | covered once the 146 fix lands; no new task |
| per-role token cost | **88** Part 3 (blocked, per-role virtual keys) | leave in 88; 148 L14 already says cost columns are out of v1 |
Rule: extend 146 and 148 task texts (with `.bak`), do not create a new task; 145 unchanged.

### F. Langfuse — why 50,152 events, and what powerplatform is missing (high level)
**The burner is the LongHorizon-Harness, not powerplatform.** The `lh-harness` LiteLLM virtual key has per-key Langfuse logging (set 2026-08-29, `infrastructure/scripts/set-perkey-langfuse.sh` in mcp-tools) to its own project, and `adapters/claude_code.py:214-223` tags every Claude Code turn. This box alone: 27 September runs, 380 rounds, 528 episodes, ~22 turns each ≈ 11.6k calls × 2 events ≈ 23k events; the Hydra fleet runners (ptait09, desk03, htpc01) run the same harness. Quota is org-wide, so the harness's separate project gives powerplatform no headroom. Global LiteLLM callbacks are `opentelemetry` only (Langfuse deliberately removed 2026-06-28 after a tenant leak) — but CT202 is `store_model_in_db: true`, so confirm via `/config/list` that the DB isn't shadowing it. Nothing in the queue tracks Langfuse quota; this is new work, no duplicate.

**Powerplatform today:** SDK is wired (`packages/backend/src/config/tracing.ts`, `agents/base-agent.ts`) with one generation per agent call and sessionId grouping, but **prod.env has no Langfuse keys, so production emits nothing**; dev/uat use `pk-lf-ced0…`. Gaps: no `userId`, no tags (env/tenant), **zero scores** despite `evals/acceptance.ts` already computing `combinedBasisPoints`/`isVerified100`, no tool-call spans (`handleToolCall` writes Postgres only), env var is `LANGFUSE_BASE_URL` while everything else uses `LANGFUSE_HOST` and the default is the EU host (wrong region). Double-count risk: `librechat` and `n8n-dev-pipeline-orchestrator` keys also push into the same project via per-key logging. Third dormant consumer: `cognizioware-agent-harness` (`pk-lf-b9f6…`). Plaintext `sk-lf-` keys sit in `dev.env`/`uat.env`/agent-harness `.env` — confirm gitignored (ties to the committed-secrets rotation memory).

**Plan (one new queue task, `pp-langfuse-observability`, sized small; harness sampling as a note on task 88 since it owns per-role attribution):**
1. Stop the bleed before 09-30 reset: sample harness tracing (executor role off, manager/auditor on, or 1-in-N episodes) via the per-key script; or point `lh-harness` at self-hosted Langfuse.
2. Powerplatform: one trace per user turn with 6 child generations; add `userId` from the MSAL principal and `env`/pipeline tags; post acceptance eval results as numeric + categorical scores on the session; add tool-call spans; add keys to prod.env through the lane's secret path; rename to `LANGFUSE_HOST` and default to the US host.
3. Pick one mechanism per call path (per-key logging or app SDK), not both.

**Sampling size (Paxton: "create a sampling size to meet our requirements"). Budget: 50,000 events per calendar month, org-wide, resets 09-30.**

| consumer | allotment | basis |
|---|---|---|
| powerplatform prod (target state: 1 trace + 6 generations + ~3 scores + ~4 tool spans ≈ 14 events per user turn) | 15,000 | ≈ 1,070 user turns/month; measure real turns/day in week 1 and re-cut |
| librechat + n8n-dev-pipeline-orchestrator (per-key logging into the same project) | 5,000 | human chat + pipeline runs; re-measure from Langfuse usage by key |
| **lh-harness** | **25,000** | the burner today; below |
| headroom for the agent-harness evaluator and surprises | 5,000 | do not spend |

**Harness arithmetic (measured 09-01→09-11 on this box):** 528 episodes / 11 days ≈ 48 episodes/day; ~22 assistant turns per episode; 2 events per turn (trace + generation) ⇒ **≈ 44 events/episode ≈ 2,100/day ≈ 63,000/month from one box**, before the Hydra fleet runners. Budget 25,000/month ⇒ **≤ 570 episodes/month ≈ 19 episodes/day fleet-wide** at full tracing.

**Mechanism that actually exists:** LiteLLM per-key logging is on/off per virtual key; there is no per-request sampling through the Claude Code adapter (it can set headers, not body metadata). So sampling = **which role keys log**, which is exactly task 88's per-role virtual keys (`lh-harness-manager`, `lh-harness-executor`, `lh-harness-auditor`).
- **Executor OFF.** Executor episodes carry ~60% of turns (build rounds, tool-heavy). Removing them: 48 × 44 × 0.40 ≈ **845 events/day ≈ 25,000/month on one box** — exactly the allotment, with zero fleet headroom.
- **Manager ON, auditor ON**, because the plan and the verdict are what you want to inspect and score; executor output is on disk in the run JSONL anyway.
- **Fleet runners (ptait09, desk03, htpc01): executor OFF and auditor OFF, manager ON** (≈ 15% of turns ⇒ ~300/day per box). Three boxes ≈ 900/day ≈ 27,000/month — over budget if all three run daily. Hence the tripwire below.
- **Tripwire:** a daily read of Langfuse usage by key (the litellm-admin skill can list spend per key; Langfuse's `/api/public/metrics` gives event counts). If harness > 900 events/day for 3 days, the next step is **not** thinner sampling — it is self-hosted Langfuse for the harness project (own Postgres on CT103, free tier gone), which removes the harness from the org quota entirely. Queue that as 169b only if the tripwire fires.
- **Until 88's role keys exist:** the single `lh-harness` key must have per-key logging **removed** now (`set-perkey-langfuse.sh` remove path, read the key metadata back) — that is the "stop the bleed" step; all-or-nothing is the only choice on one key, and nothing is the right one until 09-30.
- Powerplatform must land its **one-trace-per-turn** shape before prod keys go in; the current six-root-traces-per-turn shape is ~6× the events for the same information.

### H. The hard requirement that got lost: the overseer stops running the queue from `C:\tmp` and orchestrates through the CT110 API only, after reading the orchestrator skill in LiteLLM

**Where it stands:** the skill does not exist yet. Checked 2026-09-11: `GET /claude-code/marketplace.json` on the gateway returns `"plugins":[]`; `cogniziocompany/cognizioware-skills` is README-only; `gateway-skills-mcp/skills/` has four skills (audio-qa, gateway-skill-authoring, litellm-gateway-quickstart, remote-pc) and the only "orchestrator" text is the Hydra paragraph in remote-pc. Nothing in the queue or OPEN-ASKS carries this requirement; LOOP-PROMPT.md still hard-codes `C:/tmp/queue`. Two artifacts (Ship Plane l.15, the task-49 doc) state it as the target state but no task owns it.

**Sequence (each step gated on read-back of the previous):**
1. **Author `lh-orchestrator` skill** in `cognizioware-mcp-tools/infrastructure/docker/gateway-skills-mcp/skills/lh-orchestrator/SKILL.md` using the existing `gateway-skill-authoring` skill's frontmatter contract. Content: the eleven-state machine and RULE 1 read-back rules from LOOP-PROMPT (moved out of the PC file), the CT110 queue API contract (`POST/GET /api/queue`, priority endpoint, `blocked` status, `GET /api/runs` filtering by id prefix), model-per-role selection recorded on the entry (task 49), drain-window rule (`running + waiting_approval == 0`), the `lhharness` gateway alias for chat.easybutt0n.ai, and the ledger row format. Publish through the two channels the fleet-skill handoff already defines (MCP prompt `skills-skill-lh-orchestrator` is source of truth; Skill Hub via `publish-skill-hub.mjs` with the private repo + per-device deploy token, per Paxton's 19:50 PT decision). Verify: `prompts/get skills-skill-lh-orchestrator` returns the body and `marketplace.json` lists it.
2. **Cutover task** (section A, `harness-queue-cutover`): only after 134 lands and the CT103 database/role exists.
3. **Overseer switch**: LOOP-PROMPT step 0 becomes "read `skills-skill-lh-orchestrator` from the gateway; if it is unreadable, do not tick"; every queue read/write goes through the API; `C:\tmp\queue` becomes read-only archive. The overseer's evidence stays in LEDGER.md until the ledger itself moves (task 119's scope).
4. **Retire** the PC launcher and rotate the CT110 bearer it embeds.

**How to get the overseer to do this (your question on guiding it vs. a second local agent):** guide `longhorizon-harness-99` directly. It is listed and addressable (`longhorizon-harness-99 [2df7a7]`), it owns LOOP-PROMPT and the ledger, and it already runs `/claude-bridge` on every tick, which is precisely the mechanism for absorbing a plan from another session into a queue task. A second Claude agent on this machine writing to `C:\tmp\queue` would be a second writer to a file store with no locking, which is the dual-state problem in section A made worse; and until cutover the file queue is the only thing the PC launcher reads, so two writers would race the launcher too. So: this plan file is the handoff artifact, one `SendMessage` to `longhorizon-harness-99` pointing at it plus the four ledger corrections (E1-E4), and let claude-bridge turn sections A/G/H into tasks. If the overseer does not pick it up within two ticks, the fallback is not another agent but Paxton pasting the plan path into that session.

**The false-alarm → real-stranding episode (task 135, and it is end-to-end):** the overseer's account is accurate, and it is the third stale-glob false alarm in one day plus the 14th done-at-launch stranding. Both have one cause: state is inferred from files the overseer or the launcher wrote, not from the system of record. Recommendations, in order of leverage:
1. **Gate detection reads `GET /api/runs`, never scratch files.** The per-tick directory helps but still reads its own output; a gate exists only if the run's status on CT110 says so. Add to LOOP-PROMPT RULE 1 as a named trap.
2. **Launch is not done.** Until cutover, the PC launcher must move an entry to `running/` at launch and to `done/` or back to active with the failure in the note only when `/api/runs/<id>` reports terminal status; after cutover the API's `launched`→`done|failed` transition does this natively. This is the single change that ends the stranding class.
3. **A stranding sweep every tick:** for each `done/` entry with a `run_id`, read the run's status; any `failed`/`stopped` run whose entry is not in active or blocked is a stranding, counted and requeued automatically with the cause in the note. Today this was found by accident.
4. **E2E test in LongHorizon-Harness** (`e2e/`): enqueue → launch → force a 429 on round 1 via the synthetic-provider fake (task 47's provider) → assert the entry is `failed` with the cause recorded and is re-eligible after the ramp, and that no entry is ever `done` while its run is non-terminal. Add a second case: the stale-glob shape, i.e. the dashboard's gate list must equal the API's, never a directory listing.
5. **Ledger row for 135** should record the 8-hour invisibility window as the cost, so the cutover task's priority is justified by a measured number.

### I. TOP PRIORITY (Paxton, 11:35 PT): powerplatform production deployment, lh-harness building and testing it
Full chronological plan at `C:\tmp\PLAN-powerplatform-prod-chronology-2026-09-11.md` (T0–T4). Critical path in one line: **Ruflo up → eval-gate green → PR #105 (159) and 154 onto develop → dev tested by 153 → one manual `promote.yml` dev→uat→prod → then 156 automates it and 109/132 gate it.** The pipeline blocker today is not code: `eval-gate.yml` fails at "Ruflo live verification" on main and the last promote died at the dev eval suite; nothing has been promoted since 2026-09-10 11:46Z and prod still serves PR #102.

### J. Handoff: ruflo patch overlay (new task 165, mcp-tools workspace, appended `9999e-`)
**Problem:** the overseer fixed the Ruflo eval gate (11/11 pass, CI run 34644524906) with three edits inside ruflo's installed package trees on CT101: Change A typed four `value` properties in `/opt/ruflo/v3/@claude-flow/cli/dist/src/mcp-tools/{config,hive-mind,memory}-tools.js`; Change C added a type-array branch to `validateType` in `/usr/lib/node_modules/ruflo/node_modules/@claude-flow/mcp/dist/schema-validator.js`; Change D symlinked `sql.js` into `/opt/ruflo/node_modules/`. Backups sit beside each file (`*.bak-untyped-20260911-202147`, `*.bak-typearray-20260911-202636`). Any `npm install -g` of ruflo silently reverts all three and the gate goes red with no code change. (Change B and the 13:18 agentdb symlink are inert copies the server never loads; leave them.)

**Task text `C:/tmp/ruflo-patch-overlay-task.txt` will say:**
1. Create `infrastructure/ruflo/patches/` in cognizioware-mcp-tools: one unified diff per change, each header pinned to the exact installed version (`@claude-flow/cli`, `@claude-flow/mcp`, agentdb `2.0.0-alpha.3.7`, sql.js `1.14.0`; read them from CT101 `package.json`s, do not guess).
2. `infrastructure/ruflo/apply-ruflo-patches.sh`, idempotent: for each patch, assert the installed version matches (exit non-zero with a named message if not), grep for the marker line (`type: ['string', 'object']`, `Array.isArray(expectedType)`), apply only when absent, back up first. Replace Change D's symlink with `NODE_PATH=/usr/lib/node_modules/ruflo/node_modules` in the service units once verified equivalent; keep the symlink until then.
3. Wire-in: a step after `npm install -g` in `infrastructure/ruflo-uat/deploy-ruflo-uat.ps1` (existing deploy path), and `ExecStartPre=` in the `ruflo-mcp` / `ruflo-daemon` unit files, which move into the repo beside `ruflo-uat.service`.
4. Drift check: extend `ruflo-live-verify.mjs` (the eval gate's Ruflo step) to assert the markers exist, so a reinstall fails as "ruflo patches missing", not as a mystery red gate.
5. Pin the ruflo install version in the manifest; upgrades become deliberate and re-run the check.
6. Upstream issues/PRs on ruflo and claude-flow for the three defects; the apply script no-ops as each lands.
Hard rules: no host mutation by the run — it delivers files + tests + a PR and STOPS; the overseer applies on CT101 in a quiet window with the existing backups as rollback.

**CORRECTED 2026-09-14 after the overseer's measurement (it replaced task 165's text; my draft was wrong on three premises):** there is exactly ONE ruflo container, CT101, which is production; there is no `/opt/ruflo-uat`; the global npm prefix is `/usr`, so `deploy-ruflo-uat.ps1`'s `npm install -g` targets container 101 — my "verify with a fresh `npm install -g`" step would have reverted Change C on production and reddened the blocking gate. Also: Change A lives in `/opt/ruflo/v3` (not node_modules) and cannot be version-pinned by package; Change D (the sql.js symlink) survives a reinstall; **only Change C is reinstall-fragile**. The overseer's replacement pins by file hash, forbids any install inside CT101, and gates the PR on a host-free fixture test. Section J's steps 1-6 stand as intent; step 2's version-pinning becomes hash-pinning and verification is fixture-only. The overseer owns 165 now; do not re-queue.

### K. Retire the spendlog export-to-terranas01 (task 125 family) — Paxton 2026-09-11: deprecated, no migration
**Why:** the app-side retention requirement (task 115, done: 7-day window with `store_prompts_in_spend_logs:false`) clears spend logs past size, so exporting and moving rows off CT202 to the NAS is no longer needed. **Verify first** that the retention is live on the router (DB-shadowed setting: read `/config/list` or `LiteLLM_Config`, not the yaml) before closing anything; if it is not live, that is the one thing to fix, and 125 still does not come back.

**What changes (overseer-executed; queue + one running run):**
1. `done/003-125a-spendlog-export-core.json` — run `5bda1709` is RUNNING now (launched 13:10 PT, all-local trio). Stop it at its next gate via the API; mark the entry SUPERSEDED with reason "Paxton 2026-09-11: retention covers it; no migration", evidence in-row.
2. `005-125c-spendlog-schedule.json` (active, unlaunched) — move to `done/` as SUPERSEDED, same reason. Nothing to wrap a scheduler around.
3. `done/004-125b-spendlog-audit-entry.json` + **PR #144** (`POST /api/ingest/spendlog-prune` on ops-control-center, +262/-0, clean) — the audit write path is still useful for the retention prune itself; keep the PR, retitle its purpose in the merge window as "audit entry for the retention prune", and drop it from the 144/147 window only if Paxton says so. Default: merge as planned.
4. `done/003-125-spendlog-2h-export-terranas.json` — already SUPERSEDED; note that the successor is retention, not slices.
5. OPEN-ASKS: close **"WHICH terranas01 spendlogs path is canonical / 5.5 GB"** as moot — nothing moves; leave the existing 5.5 GB where it is as a historical archive (only copy of already-deleted rows), do not create the usbdrive directory. Close the **terranas01 sshd** question's blocking role (it only blocked 125a); the "did a NAS update rotate sshd" confirmation stays as a one-line security note, not an ask.
6. Task text files stay on disk (renamed `.SUPERSEDED-<date>`), the house convention for surviving cancelled briefs.
7. Ledger row with all of the above; remove the two items from the overseer's "still on your list".
**Not touched:** task 141 (disk remediation) — its Track C "CT202 SpendLogs reclaim" is now satisfied by retention; amend its note rather than the task.

### L. OPEN-ASKS.md audit (file as of 2026-09-14 16:43 PT, attached by Paxton)
**Bottom line: nothing is genuinely on Paxton right now** (L483, and the 16:17 token close). But the file contradicts itself in ways that will mislead the next reader or a run, and one of its premises has flipped under the queue.

1. **The GitHub-capability premise flipped and the queue was not re-evaluated.** L30 (09-10): "CT110 has no `gh` and no GitHub token, so PR-centric work is structurally impossible for a run" → several entries were parked as OVERSEER-EXECUTED on that basis (144, 147, 149, 150, 102, and my 162/163/132/164 notes cite it). L486-510 (09-14): scoped token installed, push + PR-create proven from CT110, lh-harness restarted 16:21 with `GH_TOKEN`, LOOP-PROMPT STEP 1 now says runs push and open PRs. **Action:** amend the L30 row ("superseded 09-14: runs push/PR; merge stays overseer's"), then sweep every blocked/ note whose only blocker was "no gh on CT110" and either release it to a run or restate the real blocker (service restart, prod window, credential). Task 102's note carries the old decision and must change. My tasks 162 (Windows service), 163 (queue tree), 168 (cutover) stay overseer-executed for other reasons; 132 stays parked on Paxton's go; 164's Phase 1 could now be a run.
2. **The overseer's "three runs stalled on git ops the sandbox cannot perform… deserves its own task" (its status text) is already moot** after 16:21; if a task was queued for it, rescope to "merge only" or drop it. Check the queue for it.
3. **Open table vs prose disagree.** L18 still lists `144-window` as the sole open row while L448 says it was moved to Answered. Remove the row. The table then has zero rows, which is correct and should be stated.
4. **Stale "STILL OPEN" heading for the Seq ingest key** (L392) contradicted by L417 (Paxton: dev, no auth; 201 proven). Strike the heading.
5. **Retention is NOT live** (L455: "task 170 owns that") — this changes my section K: 115 did not land the setting; 170 must verify by reading `/config/list` or the DB before the 125 family is called superseded-with-cover. Nothing else in K changes (Paxton's instruction stands: no migration).
6. **Ruflo section (L257-284)** still says the gateway registration points at dead .218 and waits for a yaml edit; the ledger (L20583) says the fix landed via `mcp-tools.env` L49 = .155 and survived the stack recreate. Mark resolved.
7. **Synthetic CAP = 3 accepted (L480)** — a launcher constant plus restart. Verify it is applied: `grep -n "^CAP" C:/tmp/launch_queue.py` and process start time > file mtime. If not, it is an overseer action, not an ask.
8. **Typo that will misdirect a run:** L382 "cogniziocompany/ptait06-easybutt0n-ai" should be ptait09.
9. **Structure:** 511 lines, ~40 of them live. Split into OPEN-ASKS.md (rules + the table + "on Paxton now") and ASKS-HISTORY.md (everything struck/answered), each answered item linked by id. The overseer's own Rule 1 ("an ask already answered on disk is not an ask") is easier to obey when the answered list is not interleaved with prose retractions.
10. **Security note to keep visible, not as an ask:** terranas01 sshd rotated in place on a 5-day uptime (L198-207) — one line in the history file, and a memory entry so it is not re-derived.

**Handoff:** these ten items go to the overseer via `C:/tmp/HANDOFF-open-asks-audit-2026-09-14.md` (SendMessage does not reach it; files do). Items 1, 5, 7 change queue behaviour; the rest are hygiene.

### M. Hydra fleet plane handoff (overseer, 2026-09-14) — audit, then the whole plan by repo

Sections A–K are the 09-11 audit; L and M are 09-14. Everything in M was read from the repos, queue, and ledger today (local clones; no fetch — where a clone may be stale it is marked).

#### M1. Audit of the handoff's "measured" table — six rows need correction before anyone acts on it

| handoff claim | what the repos say | consequence |
|---|---|---|
| Step A: "confirm which of task 55's three secondary commits landed" | **All three are on origin/main**: `0de7b2d` dedup_key on enqueue (`queue.py:79,350-364`), `3efb79b` real `queue_len` (`server.py:674-679`), `ca4670c` `docs/queue.md` (12,980 b) | Step A is done. Drop it from the critical path. Note: idempotency landed as a body field `dedup_key`, not an `Idempotency-Key` header; Hydra/chat callers must send it |
| "Hydra deploy path does not exist; only claude-code.yml and hydra-ci.yml" | `hydra-ci.yml` **has a deploy job** (`:110+`): self-hosted `hydra-host` runner on the target, rsync to `/opt/cognizioware-hydra`, compose build + up, health wait, smoke, then dispatches `hydra-deployed` to cognizioware-qa. `claude-code.yml` is absent in the local clone (ledger says #26 merged it 09-11 — clone likely stale; verify with `gh api`) | Task 166's "no deploy workflow" premise is wrong; 166 becomes "make the existing deploy job the only path + runbook", not "create one" |
| "feat/harness-fleet @ 38e4184 stranded, ahead 3 / behind 47, no PR" | Merge-base with main is the branch's own commit `fbd150d`, i.e. **the control-plane work is already in main**. Remaining delta = 2 commits (device-token provisioning) that duplicate **open PR #5, CONFLICTING** | Do not "carry or retire" 38e4184; the branch is spent. Decide PR #5 (rebase or close). Branch new work from `origin/main` |
| "Hydra fleet MCP: none enqueues, launches, gates or reads harness runs; the orchestration half does not exist" | `hydrafleet` (28 tools) **already has** `list_fleet_runs`, `create_fleet_run`, `get_run_snapshot`, `send_run_instructions`, `resolve_run_gate`, `stop_run`, `resume_run`, `get_fleet_capacity`, `get_run_activity`, `list_harness_nodes`… (`orchestrator/src/mcp.js:122+`); `fleet-scheduler.js` consumes the LHH queue contract (`listQueue`, `listAllQueue`); `fleet-routes.js:277` resolves gates. **What is missing is exactly one thing: an `enqueue` tool** (create_fleet_run creates a run directly, bypassing the queue — the §3.2 "never call" path) | The Hydra side is 90% built. The task is "add `enqueue_task` → `POST /api/queue` with `dedup_key`, and stop the chat agent from reaching `create_fleet_run`/`resolve_run_gate`" — not "build the orchestration half" |
| "Shared-infrastructure interlock: proposed home Hydra" | No interlock exists; the nearest is the per-node guard `fleet-routes.js:94-95` (409 on non-terminal runs, **bypassable with `force:true`**) and `restart_harness_node`'s refusal | Interlock = extend that guard to pending gates (`fleet.harness_gates` status) and make `force` impossible for router/DB restarts. Small, and it has a home already |
| "heartbeat rejected 413; task 114 owns it; queued, last run failed" | **Root cause is one line**: `fleet-admin/src/server.js:40` whitelists only `/harness/rounds` for the 10 MB parser; `/harness/heartbeat` and `/harness/events` inherit `json1mb` (`:38`). And the reporter makes it worse: `runs` = every run ever (`server.py:664` → `list_run_items()`, no cap), each run serialised twice (`reporter.py:250`), `_MAX_PAYLOAD_BYTES` declared but never used (`reporter.py:44`), 413 swallowed as non-retryable (`reporter.py:408-414`). **Task 114 is in `done/` with run `476970f4` from 09-11 — a 26th stranding, unrequeued** | Two-sided fix, both one-liners plus a cap; 114 must be requeued now. This is the highest value/hour item in the whole plan |

Also verified: the CT110 `Launcher` really launches (`launcher.py:298-322`), has **no shadow/observe mode**, **no retry/requeue** (`mark_failed` is terminal, `queue.py:450`), occupancy = live-run path string only (`launcher.py:236-241`, no dirty-tree check, in-process lock only); `_VALID_STATUS` still lacks `blocked` (134 unlanded; the worktree added `spec_pending` instead); MCP `dispatch` checks one bearer, no per-caller scoping (`mcp_tools.py:134-162`) so `harness_resolve_gate` is open to any bearer holder; the gateway's `intended-key-matrix.yaml:19` puts **`hydra` + `hydrafleet` in the EXECUTOR base access group**, i.e. an executor role can resolve gates and stop runs today. Task 168's text has no shadow phase though task 55's spec mandates one (55 L21 vs 168 L83/L100). Ledger TICK headers stopped 09-11; strandings total 25 (26 with 114). Ship Plane v125 now carries a 57-row board (D1 fixed).

#### M2. Which repo's expert owns what (the whole plan, by repo)

**LongHorizon-Harness (the launcher becomes the orchestrator)** — 3 tasks, 1 amend
- **172 launcher-terminal-and-retry** (gap 1.8 + 1.15): promote `launched→done|failed` from the run report (exists, `launcher.py:209-216`) and add `requeue(entry, cause)` creating a successor entry with `retry_of`, bounded by `max_retries` from `[queue]` config; `record_skip` allowed on failed entries. E2E: forced 429 on round 1 → entry `failed` with cause, successor `pending`; never `done` while non-terminal. Base `origin/main`.
- **173 launcher-shadow-and-occupancy** (cutover Step 2 + the 09-14 collision finding): `[queue] observe = true` short-circuits before `_launch` (`launcher.py:298`) and emits `queue.shadow_launch`/`queue.shadow_skip` with the full decision; occupancy also treats a workspace with `git status --porcelain` non-empty or an upstream-less branch with unmerged commits as occupied; a file lease under `runs_root/queue/.lease` so two API processes cannot both pass eligibility. Collides with task 48/PR #8 — rebase on it.
- **174 harness-tool-scoping-and-ceilings** (gaps 4–6 at the harness): `requested_by` on entries; per-tool allowlist keyed by caller (`chat`, `hydra`, `overseer`) in `mcp_tools.dispatch`; per-caller entries/hour (10, 429) and `max_rounds` clamp (≤50, 422) per §3.5; `harness_resolve_gate` refused for `chat`. Add `blocked` to `_VALID_STATUS` here if 134 has not, with the one transition table (only `pending→launched` is guarded today).
- **134** (Postgres store): relaunch now that `GH_TOKEN` is live; must land `blocked` + transitions; needs the CT103 role (Paxton). **168** (cutover): amend to carry 55's shadow phase and promotion evidence (7 days, one CT110 reboot, one network blip, zero double-launch) instead of a single switch.

**ptait09-easybutt0n-ai / fleet-admin (the report view)** — 2 tasks, 1 requeue
- **114** requeue immediately; rescope to the two-sided fix: `server.js:40` → `req.path.startsWith('/harness/')` (or mount `harnessJson` on heartbeat + events), and reporter-side: heartbeat sends only non-terminal runs plus counts, drops the duplicated `summary`, enforces `_MAX_PAYLOAD_BYTES`, and treats 413 as a retry-after-shrink, not a drop. Read-back: `GET /api/fleet/nodes` non-empty; `fleet_ever_succeeded=true` on `/api/meta`.
- **175 fleet-admin-deploy-path**: fleet-admin has no compose service, no workflow, no Caddyfile in the repo; the CT202-behind-Caddy topology exists only on the host. Capture it in code (compose service + Caddy snippet + a deploy step mirroring hydra-ci's rsync/compose pattern), so 148c/148e and 114 have a lane. The handoff's own "Hydra deploy path" row was really this repo's gap.
- **148b/148e/148c** (trios endpoint, push patch, page): unchanged; 148b already notes "live push BLOCKED ON 114".

**cognizioware-hydra (control plane)** — 2 tasks, 1 decision
- **176 hydra-enqueue-and-interlock**: add `enqueue_task` to `FLEET_TOOLS` → LHH `POST /api/queue` carrying `dedup_key`, `requested_by`, `priority`; extend the `fleet-routes.js:94` guard to pending gates read from `fleet.harness_gates`, and reject `force` for router/DB restart actions (gap 4.1). Base `origin/main`. Optionally a `maintenance_window` flag the overseer sets during a drain.
- **166** amend: it is not "create a deploy workflow"; it is "hydra-ci deploy job is the only path; write the runbook; decide PR #5 (rebase onto main or close as superseded); close `feat/harness-fleet` with a note that its content is in main; triage #23/#24/#25 (all MERGEABLE)."
- **Decision (Paxton):** PR #5 device-token provisioning — rebase or drop.

**cognizioware-mcp-tools (gateway = the only enforcement point today)** — 1 task, 1 amend
- **177 gateway-caller-scoping**: the `lhharness` alias exposes the four harness tools to `fleet-runners`; `intended-key-matrix.yaml:19` grants `hydra`+`hydrafleet` to EXECUTOR keys. Split: chat agent key group gets `lhharness` enqueue/list/status only (no `harness_resolve_gate`), executors lose `hydrafleet`. Assert it in suite 29/20. This is what makes §3.2 enforceable before 174 lands.
- **167** (lh-orchestrator skill): unchanged; it must cite `dedup_key`, the chat contract §3, and the shadow-mode flag by name once 173 exists.

**BMAD_Cognizioware / Fleet Chat** — **140** unchanged (spec committed `d84b7a7`; resume at next phase). It is the operator UI and follows the cutover.

**Overseer / queue tree** — **168** (cutover, amended above), **163** (queue/overseer folder), and the tick sweep for strandings (LOOP-PROMPT RULE 2 already has it; 114 slipped through — the sweep must also cover entries whose run id predates the rule).

#### M2a. Paxton's use case — "log into chat, load the lh-harness skill, get status of everything, resolve a gate" — and its three logic gaps
Feasible with no new MCP server: `lhharness` (4 tools) + `hydrafleet` (28, incl. `list_fleet_runs`, `get_run_snapshot`, `get_run_activity`, `resolve_run_gate`) + Hydra's `overseer-briefing` prompt already exist. Task 167 becomes an **operator playbook** (intent → gateway tool name, state vocabulary, read-back rule, approvals live in the snapshot `approvals[]`, filter non-terminal). Gaps that must be closed in the plan, not the skill:
1. **Two queues until 168**: chat enqueue lands in CT110's store, which the PC launcher never reads. Status and gates work now; enqueue is real only after cutover.
2. **Identity**: Fleet Chat (Open WebUI → mcpo) holds one gateway key for all users, so "my key can resolve, the bot's cannot" is unenforceable inside chat. The migration doc §3.2 and the handoff's gap 5 both forbid the chat agent from gate resolution, and a human in Fleet Chat is indistinguishable from the agent. **Decision (Paxton, 2026-09-14): per-user keys in Fleet Chat FIRST — see M2b.** Until each Fleet Chat user carries their own gateway key, gate resolution is not offered from chat at all. 177 issues the two key groups: `chat-agent` = enqueue/list/status; `operator` = full set.
3. **Attribution**: `harness_resolve_gate` records no caller; Hydra's `resolve_run_gate` requires a `rationale` and logs it. The skill routes gate resolution through Hydra, never the raw harness tool; 174 later adds `requested_by`/actor at the harness.

#### M2b. Paxton's decisions (2026-09-14, interview)
1. **Gate identity: per-user keys in Fleet Chat first.** Not personal-key clients only, not the shared key. Gate resolution from chat is allowed only once each Fleet Chat user carries their own gateway identity.
2. **Mechanism: one LiteLLM virtual key per Fleet Chat user**, in two access groups: `operator` (full `lhharness` + `hydrafleet`, gate resolution via Hydra with rationale) and `chat-agent` (enqueue/list/status only). Open WebUI passes the user's own key to the tool server; no trusted identity header. This lands in **177** (gateway groups + key minting via the litellm-admin path) and **140** (Fleet Chat wiring per user); until both are live, gate resolution stays out of chat entirely.
3. **Ordering: all fleet-plane tasks append after every powerplatform task**, in M3 order (114, 175, 177, 172 first; then 173, 174, 176). 9999x prefixes; pp keeps every slot ahead.
4. **Shadow window: no fixed length; promote when the six evidence items hold** (agreement with zero unexplained divergence, zero phantom launches, zero missed launches, liveness green across one CT110 reboot and one network blip, zero double-launch, time-to-launch ≤ PC baseline). The reboot and blip are exercised deliberately, not waited for. 168's text carries this instead of "7 days".
5. **Hydra PR #5: close as superseded**; 166 closes the PR and `feat/harness-fleet` with a note that the control-plane content is already in main. Device-token provisioning is dropped for now.
6. **Per-user key persistence in the Web UI (Paxton, added at plan review):** the Fleet Chat UI must let a user enter their gateway key once and **persist it for that user** (Open WebUI per-user settings/valves, stored server-side against the login, never in the shared tool-server config), with a **default** for users who have not entered one (the `chat-agent` group key, so status and enqueue work out of the box), and a one-click **accept/use** step so the same key is what the harness tools receive for that user. Ownership: **140** builds the UI field + persistence + default; **177** mints and rotates the keys and defines the two groups; the operator-playbook in **167** documents "enter your key once; gate resolution appears only when your key is in the operator group". Verification by read-back: log in as two users, enter two keys, restart the UI, confirm each user's tools call the gateway with their own key (gateway spend logs show two key ids), and that a user with no key gets the default and no gate tool.
7. **Golden scenario coverage (Paxton):** the per-user key flow is a golden QA run on the Fleet Chat operator surface under **task 113** (golden runs for every operator surface; gated on 111's BMAD flow specs): log in → enter key → persisted across restart → status of everything → resolve a gate via Hydra with rationale → the resolution shows the user's identity. 113's task text gets this scenario added by name; it is not a new task.

#### M3. Sequence (corrected critical path)
1. Now, in parallel, no dependencies: **114** (requeue + two-sided fix), **175**, **177**, **166** amend, **172**.
2. **173** after 172 (same files). **134** relaunch now; **174** after 134 (status set).
3. Shadow window: **173** deployed with `observe=true` in a zero-active window (a CT110 restart); 7 days of agreement vs the PC launcher, read from the fleet window (needs 114) — this is the only calendar-long step.
4. **168** cutover with 55's promotion evidence; bearer rotation; PC launcher archived (104).
5. After cutover: **176** interlock matters, **167**/**140** point the overseer and Fleet Chat at the API.
Estimate stands at ~2–3 weeks, but with A gone and the Hydra half already built, hands-on windows drop to three (173 deploy, 134/168 cutover, 176 deploy) and harness runs to ~7.

#### M4. What this changes in the existing queue (no duplicates)
- Requeue 114 (stranded); amend 166, 168; new 172, 173, 174 (LHH), 175 (fleet-admin), 176 (hydra), 177 (mcp-tools). Verify none exists under another number first: grep task texts for "shadow", "requeue", "enqueue_task", "startsWith('/harness/')", "intended-key-matrix".
- Correct the handoff file `C:/tmp/HANDOFF-hydra-fleet-plane-replaces-overseer-2026-09-14.md` rows per M1 so the next reader does not re-derive them, and note the stale-clone caveat on hydra `claude-code.yml`.

## Recommended fixes (what the implementation phase would do)

Order of value: (1) author + publish the `lh-orchestrator` skill (H1) and queue `harness-queue-cutover` (A) — this is the lost hard requirement and it retires every PC-launcher defect at once; (2) interim `running/` state on the PC launcher only if cutover is more than a week out (D7, 14 strandings); (3) the one live hazard fixes now: `740-97` trio→kimi, 154's note (branch gone, per-path carry-over), 146 gets the rotation-overwrite bug (G); (4) queue-file hygiene below, safe any time; (5) artifact regeneration from `gh pr list` + queue dirs + ledger state table, with per-section stamps; (6) ledger corrections (E1-E4, C1-C3) and this plan handed to `longhorizon-harness-99` by SendMessage; (7) Langfuse (F). No zero-pad renames and no PC-launcher refactors beyond (2)-(3): that code is being retired.

Queue-side (safe, file moves/edits with `.bak` first, launcher tolerates):
- B2 move 157 to done; B9 clean orphans; B4 strip stale run_id; B7 fix `name`; A3 trio→kimi.
- A1/B6 zero-pad rename requires a launcher pause (`DRAIN` flag) since the glob re-sorts live.
Launcher-side (A2 try/except) — but 155 is the task that moves `launch_queue.py` into a repo; patching in place means patching a file with no history. Recommend: do 155 first (or at minimum copy the script into LongHorizon-Harness `tools/` before editing), then restart the launcher and confirm process start > mtime.
Overseer-side: feed C1–C3 and A4 back to `longhorizon-harness-99` as a ledger row / SendMessage; do not edit its LOOP-PROMPT without Paxton.
Langfuse: queue `pp-langfuse-observability` (section F); apply harness sampling via `set-perkey-langfuse.sh` only after confirming the key's current metadata by read-back.

## Verification
- Skill: `prompts/get skills-skill-lh-orchestrator` through the gateway returns the body; `GET /claude-code/marketplace.json` lists it (today `plugins: []`).
- Cutover: `GET /api/queue` on CT110 shows 24 pending + 15 blocked with priorities in file order; `DRAIN` set, `ACTIVE=0` read from `/api/runs`, PC launcher process absent, old bearer rejected with 401.
- Stranding: after the sweep, every `done/` entry's `run_id` resolves to a terminal `/api/runs` status; the e2e 429 case leaves the entry `failed`, never `done`.
- Queue hygiene: `ls C:\tmp\queue\blocked` = 14 task JSONs, 0 `.bak`; `done` gains 157; `740-97` reads `"trio": "kimi"`; 154's note names `refs/pull/107/head`, not the deleted branch.
- Artifacts: the republished Ship Plane carries the ledger state table with all 39 open task ids, PR counts equal `gh pr list` output at publish time, and each section has its own stamp.
- Ledger: correction rows for E1-E4 present; band line equals `ls | wc` for the three directories.
- Langfuse: `lh-harness` key metadata read back shows the sampling change; org event rate drops on the next daily read; powerplatform prod emits one trace per user turn with scores attached.
