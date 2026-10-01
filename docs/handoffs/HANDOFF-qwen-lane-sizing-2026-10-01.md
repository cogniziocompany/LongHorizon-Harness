# Handoff: how big a task can the local qwen lane finish? (2026-10-01)

**For:** an outside expert in local-model capacity and agent task design.
**From:** Overseer1, the CT110 queue overseer, at Paxton's request on 2026-10-01: "Create a handoff for another expert that will help us size qwen requirements."
**Ask in one line:** turn our anecdotes into **measured limits and a sizing rubric**. We want to know, before a task is queued, whether the local qwen trio can finish it. If it can't, the rubric should say how to split the task or that it should go to the cloud (`kimi`).

## 1. The system

**Harness.** LongHorizon-Harness runs on CT110 (`http://192.168.21.168:8799`).
- Every queued task runs as a *trio*: manager, executor, then auditor, repeating by rounds until it finishes.
- Per-role time limits come from `src/lh_harness/config.py` `[run.timeouts]`: manager 300 s, executor (cli and gui) **1800 s**, auditor 300 s.
- If the auditor's report is malformed, a format-repair pass runs under the auditor budget.
- After 3 consecutive failed rounds, the run stops at an operator gate ("Repeated failures require operator input").

**The `qwen` trio is fully local.** Every role uses the gateway alias `qwen3.8`, which is `ornith-1.5:9b-256k`. That's a 9B hybrid SSM model (family qwen35) with tools, thinking and vision.

**Lanes.** Two RTX 3090s (24 GB each) on Proxmox ptait01 (`192.168.21.110`), one Ollama container per card:
- `:11441` on gpu1
- `:11438` on gpu0, which also serves the Ollama Cloud relay and embeddings

**Concurrency:**
- Each lane serves **one request at a time** (`n_seq_max = 1`; `OLLAMA_NUM_PARALLEL` has no effect).
- Total local concurrency is therefore **2**, shared by every qwen harness run, the prod router (CT202) and the UAT router (CT204).

**Context:**
- The harness budget is 128k tokens.
- The lanes are proven at 262,144 tokens (needle test, 186k-token prompt, 2026-09-10).
- Never send `num_ctx`; it reloads the lane.
- Cold reload takes 0.5–8.6 s. Allow 30 s for a cold first token.

**Cloud alternative: the `kimi` trio** (`kimi-k3`, synthetic.new).
- It finishes the same tasks reliably.
- But it was rate-limited (429) for most of 2026-09-29 to 2026-10-01, which is why work keeps being pushed onto qwen.

**Current sizing rule** (OVERSEER.md "Sizing work for the local qwen lane", 2026-09-30). These are guesses, not measurements:
- One deliverable per sub-task: one file, or one file plus its test, of about 150 changed lines or less.
- Exact code in the spec: either the full new file or exact old → new edits.
- A named test command with its expected result.
- Chain sub-tasks; don't batch them.
- Design and research tasks stay on kimi.
- After 3 failed rounds, split further or write it up for Paxton.

## 2. What we observed (2026-09-29 to 2026-10-01)

**Outcome counts.** These are queue entries in the CT110 API window, read on 2026-10-01 ~21:45Z:
- qwen: **5 `done`, 45 `failed`** (1 still running)
- kimi: 68 `done`, 178 `failed` (most kimi failures were provider 429s, not task failures)

Caveat: some qwen entries counted as `failed` were stopped deliberately after their work landed. For example, r1e only verified an existing PR and then hit its round limit. Treat these counts as a direction, not a precise rate.

**Every qwen success with a large spec had an exact-code APPLY script:**

| Task | Spec | Shape | Result |
|---|---|---|---|
| dr-r1a device-events schema | 8.7 KB, APPLY script | 1 SQL file plus test | PR [cognizioware-mcp-tools#233](https://github.com/cogniziocompany/cognizioware-mcp-tools/pull/233) merged |
| dr-r1b `POST /device/events` | 13.2 KB, APPLY script | `server.js` plus 1 test file, +171 lines | PR [cognizioware-mcp-tools#240](https://github.com/cogniziocompany/cognizioware-mcp-tools/pull/240) merged, 7 tests |
| dr-r1e Caddy route (verify only) | 4.7 KB, APPLY script | confirmed an existing 2-line diff | correct verification; entry counted failed (round limit) |
| dr-r4e, dr-r4a, dr-r6 | 1.6 to 4.4 KB, no APPLY | small, single-area changes | done |

**Typical qwen failures** were free-form or multi-file specs where the model had to design or explore:

| Task | Spec | Shape | Failure |
|---|---|---|---|
| fc-H1a latest-event endpoint | 2.5 KB, no code | 3 source files plus tests, design | executor **timed out at 1800 s in 3 consecutive rounds**, no output. The same spec on kimi produced [LongHorizon-Harness#82](https://github.com/cogniziocompany/LongHorizon-Harness/pull/82) (+479 lines, 3 files) within about an hour. |
| org-graphify A/4 graph builder | 10.7 KB, partial code | new service: builder plus UAT graph server | round 1 done, then **3 × 1800 s executor timeouts**, nothing pushed. Run `20261001T192820Z_7193bd4a`. |
| fc-H4a maintenance suspend/resume | 3.3 KB, no code | 2 source files plus tests | failed on qwen; on kimi it produced [LongHorizon-Harness#81](https://github.com/cogniziocompany/LongHorizon-Harness/pull/81) (+505 lines) |
| fc-F0 liveness formatting | 2.4 KB, no code | several web files | looped 3 rounds, "only calibration audited, no code yet". After a split into 7 one-file APPLY specs (fc-F0a to F0g), F0a verified 11/11 tests locally. |
| 296 local-lane config (run `20260930T024423Z_a42191e9`) | 3.1 KB | multi-file | **"invalid route"** loops and **120 s format-repair timeouts**: the model could not keep the harness control-header format while doing the work |
| 297 E2E gate fixes; fwux A1/M1/J1; 300 requester identity; 293/294 retired models | 2 to 9 KB, free-form | multi-file | failed; most were redone by splitting or on kimi |

**Side effect: lane contention.** On 2026-10-01 the mcp-tools deploy [run 36914087650](https://github.com/cogniziocompany/cognizioware-mcp-tools/actions/runs/36914087650) failed its UAT QA gate on latency alone:
- `qwen3.8-nothink` chat took 60.4 s
- `qwen3.8` `/v1/messages` took 85.5 s

The budget was 60 s, and at that moment both lanes were busy with harness qwen runs. The fix in progress is to give local models their own QA budget ([cognizioware-qa#39](https://github.com/cogniziocompany/cognizioware-qa/pull/39)). Long qwen episodes also hold the CT110 deploy's zero-active window, so deploys wait for them.

## 3. What we need from you

1. **Measured capacity of one lane** for this model at our settings:
   - prefill and decode tokens per second
   - prefill time at 16k, 64k and 128k context
   - how much tool-calling and file reading fits in one **1800 s** executor episode

   That gives us a token and turn budget per round instead of a line count.
2. **Root cause of the two failure modes:**
   - **(a) executor timeouts with no output.** Is it exploration (reading too many files), thinking-token overrun, slow prefill on a growing context, or tool-loop churn?
   - **(b) "invalid route" and format-repair loops.** Does the 9B model lose the control-header format when the task is complex, and would a simpler or stricter format, or a grammar or structured-output constraint, fix it?
3. **A sizing rubric** we can apply mechanically before queueing. Inputs: spec size, number of files, whether there's an exact APPLY script, whether a named test exists, expected changed lines, and how much context the task needs. Output: "qwen-fit", "split like this", or "kimi only". Validate it against the runs above. Run logs are in `/home/harness/work/.lh-harness/runs/<run_id>/` on CT110, and each run has a deep link at `http://192.168.21.168:8799/runs/<run_id>`.
4. **A spec template** that maximizes qwen success. Our current best is the one-file APPLY-script format, e.g. the fc-F0a and dr-r1b specs in Overseer1's `plans/` folder. Tell us what to add or remove.
5. **Harness settings for the local trio:** whether the executor timeout, the round count, the format-repair budget, thinking on or off (`qwen3.8` vs `qwen3.8-nothink`) or the context budget should differ for qwen. Also whether one lane should be reserved for routers or QA while two harness runs are active.
6. **Optional:** the design of a pre-enqueue "qwen-fit" linter, a small check on the spec text that routes or warns before a task is queued.

## 4. Constraints

- Don't change the lanes under running work. Don't send `num_ctx`, don't restart containers, and don't swap models on a lane without Paxton.
- Measure with unique prompts. To prove a request reached a GPU, count `POST     "/api/chat"` lines (quoted, multiple spaces) in `docker logs` on both lanes before and after; a 200 from the router can be a cache hit.
- No secrets in reports: use env var names only.
- Deliverables go as a PR to `LongHorizon-Harness/docs/` (rubric plus template), plus a short summary for Paxton.

## 5. Sources

- OVERSEER.md (Overseer1): "Sizing work for the local qwen lane" and "Local lab hardware: the two local lanes"
- cognizioware-mcp-tools `design docs/handoff-docs/`: `ornith-db-owned-local-lane.md`, `proving-ollama-lane-routing.md`, `qwen38-64k-harness-context-handoff.md`
- `src/lh_harness/config.py` `[run.timeouts]`; `src/lh_harness/role_prompts.py` (the auditor format-repair prompt)
- CT110 queue API `GET /api/queue` (entry `task`, `trio`, `status`, `run_id`) and the run directories above
