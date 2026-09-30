# Handoff: spec staging for the queue (2026-09-10)

**Audience:** the overseer session (`longhorizon-harness-99`) and whoever deploys CT110.
**Branch:** `feat/spec-staging` in `LongHorizon-Harness`.

## Why

Manager prompts were measured at 90,000–96,500 tokens. Only ~4.1k of that is fixed
instruction text (commit `92806f2`, PR #12). The rest is (a) the raw `task_file` prose plus
operator `note`, and (b) prior-round history that `build_role_manager_prompt` spent
`role_history_chars` (100,000) on **twice**: once for auditor reports, once for harness
feedback. A queued task had no stage between "queued" and "launched" that turned prose into
a bounded, reviewable spec.

## What shipped

| piece | where | one line |
|---|---|---|
| Spec template | `templates/task-spec.md` | BMAD-derived (Intent / Boundaries / I/O matrix / Code Map / Tasks & Acceptance / Verification / Open Questions). Frontmatter `status: draft` or `ready-for-dev` is the launch gate. No size rule. |
| Staging script (live queue) | `C:\tmp\stage_specs.py` | For every `C:\tmp\queue\*.json` without `spec_file`: writes a draft spec into `<source-root>/<workspace>/.lh-harness/specs/<name>.spec.md` (fallback `C:\tmp\queue\specs\`), appends the original task verbatim for distillation, writes `spec_file`, `spec_status` and size fields into the JSON. Idempotent. Re-measures every spec each pass and flags out-of-range. |
| Live launcher gate | `C:\tmp\launch_queue.py` | `_task_text(j)`: with `spec_file`, launches only when the spec is `ready-for-dev` and sends the **spec body** as the task; otherwise prints `SPEC_PENDING` and holds. Missing files no longer crash the loop. **pid 53332 still runs the old code until restarted.** |
| Queue B (service) | `src/lh_harness/queue.py`, `launcher.py`, `webapi/server.py`, `mcp_tools.py` | New status `spec_pending`; `spec_*` fields; `mark_spec_ready`; routes `GET/POST /api/queue/{id}/spec`, `GET /api/queue/spec_stats`, `POST /api/queue/spec_stats/record`; MCP `harness_get_spec`, `harness_mark_spec_ready`. |
| Expected-size range | `src/lh_harness/spec_stats.py` | Median ± 3 × 1.4826 × MAD over the last 50 finished specs, established at 5 samples. `[queue.spec_stats]` config. Both queues feed one file via the record route. |
| Web UI | `frontend/web/src/api.ts`, `App.tsx`, `style.css` | Queue panel in the sidebar: rows with status badge and size cell, range in the header, out-of-range notice in the preview, "Mark ready" button. Built into `src/lh_harness/_frontend/web/dist/`. |
| Token guard | `src/lh_harness/role_prompts.py`, `types.py`, `cli.py`, `manager.py` | Auditor reports and harness feedback now **share** `role_history_chars` instead of each getting it. `task_text_warn_chars` (8,000; `--task-text-warn-chars`, `LH_HARNESS_TASK_TEXT_WARN_CHARS`) emits one `RuntimeWarning` per run when the task text is oversized. |
| Docs | `docs/queue.md` | "Spec staging" section. |

## How the overseer tick uses it

1. `python C:\tmp\stage_specs.py` creates draft specs for anything new, re-measures the rest,
   prints a table and any `SPEC_OUT_OF_RANGE` lines.
2. Fill each draft spec in place (the template's comment block says how). Delete the appended
   "Source task" section once distilled. Flip `status: ready-for-dev`.
3. The next `stage_specs.py` pass records the finished size (POSTs to
   `/api/queue/spec_stats/record` once the new server is deployed; local
   `C:\tmp\queue\spec_stats.json` fallback until then).
4. The launcher (after restart) sends the spec body, not the prose.

Restart order once the branch is merged and CT110 is on it: deploy server, run `stage_specs.py`,
restart `launch_queue.py`. Restarting the launcher **before** running `stage_specs.py` changes
nothing (no `spec_file` fields yet). Restarting it **after** gates every task on its spec, which is
the intent but should be done deliberately with the queue visible.

## Verification done

- `pytest` in a Linux container (the store's atomic writes need POSIX flags; Windows cannot run
  them): 585 passed, 2 failed. Both failures (`test_deepseek_adapter_runs_end_to_end_with_fake_dsh`,
  `test_launcher_key_health_allows_kimi_when_threshold_met`) fail identically on the clean tree.
- `stage_specs.py` dry run and real run against a temp copy of `748-117-pr-backlog-triage.json`;
  `_task_text` exercised on draft, ready, missing, and no-frontmatter specs.
- `npm run build` in `frontend/web`: zero type errors; core suite 61 pass.
- Not verified: the UI against a live backend; the live server at `192.168.21.168:8799` returns
  405 for the new routes until deployed.

## Decisions for the overseer (options ranked; pick one per item, dedupe first)

### A. Bridge `C:\tmp\queue` into the QueueStore so the UI shows the live fleet

Until this lands the queue panel shows queue B only; queue A stays visible through
`overseer_status.py` and the LEDGER. Dedupe phrase: **"file queue to QueueStore"**.

| # | option | change | pro | con |
|---|---|---|---|---|
| 1 (recommended) | **Mirror**: `launch_queue.py` upserts one `q-*` entry per A file each cycle (deterministic `queue_id` from the filename, stored back in the A JSON), status from directory plus `spec_status`, and calls `mark_launched(run_id)` after launch | `launch_queue.py` about 40 lines; `queue.py` gains `upsert()` with caller-supplied ids; `launcher.py` skips `source: "file-queue"` entries | zero change to launch decisions; UI sees the fleet on the first cycle; watchdog and LEDGER untouched | two copies of state; A entries are read-only in the UI (priority and delete return 409) |
| 2 | **Read-through**: `QueueStore.list()` also scans `C:\tmp\queue`, `done`, `blocked` and synthesises entries | `queue.py` plus `[queue.file_queue_dir]` | smallest diff; single source of truth | every `/api/queue` call re-reads about 30 files; `done/` means launched-not-succeeded so status needs a supervisor lookup; launcher must ignore `fq-*` ids |
| 3 | **Migrate**: port probe, rotation, ramp and degraded logic into `launcher.py`, retire `launch_queue.py` | `launcher.py` about 300 lines, all `C:\tmp\*.py` siblings, LEDGER tooling | one queue, one launcher | every rule in the launcher was paid for by a live incident; porting during an active queue will regress at least one. Follow-on after option 1 is stable. |

**Decision:** (blank)

### B. Vendored eval harnesses carry the same history double-spend

Three copies each spend `max_history_chars` twice, exactly as `role_prompts.py` did:
`eval/OSWorldv2-harness/cua-harness/src/cua_harness/role_prompts.py:206-209`,
`eval/WeaveBench-harness/cua-harness/src/cua_harness/role_prompts.py:173-176`,
`eval/TB-harness/Harness/src/role_prompts.py:166-170`. Their env var is
`CUA_HARNESS_ROLE_HISTORY_CHARS`; OSWorld and WeaveBench default it to `0` and the WeaveBench run
script exports `0`; TB defaults to the config value. Whether `0` means unlimited or none differs
per copy and must be read before changing anything. Dedupe phrase: **"halve shared history budget
in manager prompt"**. This is the same story as the `role_prompts.py` fix above with three more
sites, not a new task.

| # | option | change | pro | con |
|---|---|---|---|---|
| 1 (recommended) | **Fix in place**, same patch as the main harness | 3 copies of `role_prompts.py` (4 lines each) plus the warning in 3 agent files | eval numbers become comparable with the main harness; smallest diff | vendored copies drift further from upstream (already true) |
| 2 | **Import `lh_harness.role_prompts`** from the eval harnesses | delete 3 copies, add shims, pin `PYTHONPATH` | one implementation forever | the copies have diverged (verifier vs auditor naming, different round types); a refactor that needs eval reruns to prove parity |
| 3 | **Document only** | handoff plus 3 READMEs | zero risk to in-flight evals | the 90k problem stays in the eval path and misleads anyone reading eval token counts |

**Decision:** (blank)
