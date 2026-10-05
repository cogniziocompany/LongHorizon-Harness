---
spec_status: ready-for-dev
---

# RSI observability reports: telemetry, Langfuse, evals and self-learning in Power BI and the fleet Reports tab (2026-10-05)

**Ask (Paxton, 2026-10-05):** "start mining for the telemetry data. Also the Langfuse data. What's in Langfuse cloud? We're going to start tracking that. And then also the eval data. We need to start tracking our recursive self-learning through surfacing all that data into this Power BI and building reports for that. Work with the overseer and the rest of the Claude Bridge."

**Overseer role:** manager. Executor runs build each phase; deploys go through the Deploy MCP Tools lane as usual.

## Session & agent identity
| field | value |
|---|---|
| Planning session | `47a358d5-8c57-46ad-9226-63e23362b33b` (Claude Code, PTAIT09, short ref `[47a358]`) |
| Agent name | none issued |
| Related | Power BI spec `tasks/powerbi-report-server-2026-10-03.md` (PR #98); task 169 `tasks/langfuse-quota-and-pp-observability-task.txt` |

## What already exists (do not rebuild)
| Piece | Where | State |
|---|---|---|
| Reporting DB | `reporting` on CT103 pg16 (`192.168.21.154:5432`), schemas `stage` (sync-owned) and `rpt` (report views) | live |
| Roles | `reporting_sync` (owner), `pbi_reader` (SELECT on `rpt.*` only). Passwords: `REPORTING_SYNC_PASSWORD`, `REPORTING_PBI_READER_PASSWORD` in CT202 `mcp-tools.env` | live |
| Sync service | `reporting-sync` compose service on CT202, `infrastructure/docker/reporting-sync/` in mcp-tools (PR #398). Every 15 min: harness runs + queue (CT110 API), LiteLLM spend per day/model/key | live, healthy |
| Power BI Report Server | PBIRS01, VM 213 on ptait07, `http://192.168.21.167/Reports`, folder `/Fleet`, shared ODBC data source `/Fleet/reporting-pg16` (pbi_reader). 5 paginated reports. Evaluation editions (180 days). Interactive `.pbix` works | live |
| Fleet Reports tab | `https://fleet.easybutt0n.ai/reports`, fleet-admin `src/reports.js` + `web/src/pages/ReportsPage.tsx` (PR #414), reads `rpt.*` | merged, deploying |
| Report publisher | RDL generation + REST/SOAP publish script (to be committed in Phase 0) | exists in the planning session scratchpad |

## Rules for every phase
- New sources land in `stage.*` through **reporting-sync** (one module per source), and reports read only `rpt.*` views. Never point Power BI or fleet-admin at a source system directly.
- Every new `rpt.*` view gets `GRANT SELECT ... TO pbi_reader` and a row count in `rpt.sync_status`.
- Secrets by name only, in CT202 `mcp-tools.env` (or a dedicated `*.env` for write secrets). Never in the repo, the task text, a PR body or a log.
- **Langfuse quota (task 169):** reading the public API does not ingest events, but pace it. Incremental cursor by `timestamp`, at most one page sweep per sync, and a daily aggregate rather than raw observations.
- Do not add new Langfuse **ingestion** as part of this task.
- Tests for every module: fixtures, no network. Plus a check that every query against the reporting DB touches `rpt.*`/`stage.*` only.

## Phase 0 — commit the publisher, fix what is fake
1. Commit the RDL publisher (paginated reports + SOAP shared data source) into `infrastructure/docker/reporting-sync/reports/`, with the five existing reports as data so re-running is idempotent.
2. Paxton said a report "has fake data". Before building more:
   - Open each PBIRS report and the fleet Reports tab and compare their numbers with the sources: CT110 `/api/runs?fields=summary` and the LiteLLM spend total.
   - Record any mismatch and fix it.
   - Note that the DevSpecOps Report-only board is a different page (devspecops1, live Entra data).

**STOP GATE 0:** every number on every existing report matches its source within one sync interval, and the evidence is written in the PR.

## Phase 1 — harness telemetry (the RSI core)
Sources on CT110: `episode_stats.py` events, the experience API (`webapi/experience_routes.py`, MSCE L1/L2/L3), run outcome and report fields (`run_outcome.py`), auditor verdicts per round, provider errors (`provider_errors.py`), workspace guard and contention records.

New stage tables, at least:
- `stage.harness_rounds`: run, round, role, model, duration, outcome, auditor verdict.
- `stage.harness_episodes`: run, round, role, turns, tokens if present, tool errors.
- `stage.experience_items`: level, created, reused count.

Views, at least:
- `rpt.rsi_run_quality`: first-pass completion rate, rounds-to-complete, auditor rejection rate, failure rate by reason, all per week and per repo.
- `rpt.rsi_learning_curve`: the same metrics per task family over time. A task family is a queue name stem or spec file.
- `rpt.rsi_experience_reuse`: experience items created vs reused, and the success rate of runs that reused experience vs runs that did not.

**STOP GATE 1:** for 5 sampled runs, the round and episode counts in `rpt.*` equal what the CT110 run pages show.

## Phase 2 — Langfuse Cloud (org cognizio.company, US region, Hobby plan)
**CORRECTION 2026-10-05 (Paxton's screenshots):** an earlier revision of this section said nothing was being ingested. That was wrong. Langfuse keys are **per project**, and the org has several projects:

| Project | State | Notes |
|---|---|---|
| `lh-harness` (id `cmtf8t8bi03r0ad0debhnthc0`) | **Live.** About 2,000 observations a day: `litellm:lh-harness` generations plus `guardrail` spans | Fed by the per-key logging on the LiteLLM `lh-harness` key. This is the harness's data. |
| West Hive Capital project (hive-portal) | **Live.** About 320 observations: `hive-orchestrator`, `llm-step-0`, `eval:tool-calling` (tags `case:…`, `model:…`) | Fed by the app's own SDK. On 2026-10-03/04 every orchestrator call failed with an Ollama weekly-usage-limit `RateLimitError`. |
| `cognizioware-vlab` | Empty: 0 traces. One dataset, `trms-qa-agent-evals` (16 cases, 0 runs) | The only project a reporting key exists for so far. |

Why the earlier check was wrong: the Langfuse keys on the LiteLLM key are stored encrypted (`litellm_enc::…`), and the 401 came from testing with the ciphertext.

**Keys.**
- On CT202 (`mcp-tools.env`): `LANGFUSE_REPORTING_PUBLIC_KEY` / `LANGFUSE_REPORTING_SECRET_KEY` / `LANGFUSE_REPORTING_HOST`, for `cognizioware-vlab`.
- **Needed from Paxton:** one read key pair per live project, for `lh-harness` and for the West Hive project (Project settings → API Keys). Name them `LANGFUSE_REPORTING_<PROJECT>_PUBLIC_KEY` / `_SECRET_KEY`.
- reporting-sync must take a list of projects, not one.

**Quota.** At about 2,000 observations a day, `lh-harness` alone uses the 50,000-event Hobby allowance in about 25 days. Task 169's sampling is still required. Get the rate from Paxton through OPEN-ASKS.

**Langfuse v4 deadline, 2026-11-16:** see `tasks/devspecops1-upstream-audit-2026-10-05.md` (work item 1). Phase 2's reporting queries must use v4-compatible API endpoints from the start.

1. **Inventory first.** Before designing tables, write the counts of traces, observations, scores, sessions, datasets and prompts, the date span, and the top trace names and tags into the PR. Today's known tagging is `lh-run/<run_id>`, `round_N` and `<role>`, from the Claude Code adapter.
2. Stage tables:
   - `stage.langfuse_daily`: day, model, run tag, role, traces, observations, tokens in/out, cost, latency p50/p95, errors.
   - `stage.langfuse_scores`: daily aggregates.
3. Views:
   - `rpt.llm_trace_daily`.
   - `rpt.rsi_cost_per_outcome`: Langfuse cost joined to harness runs on `lh-run/<run_id>`, giving cost per completed run and per failed run.

**STOP GATE 2:** the Langfuse daily cost for 3 sample days agrees with LiteLLM spend for the `lh-harness` key within 5%, or the difference is explained in the PR.

## Phase 3 — evals
Sources:
- **mcp-tools E2E gate:** `results.json` artifacts from Deploy MCP Tools runs via the GitHub API, plus quarantine decisions.
- **Harness eval gates:** `tasks/eval-gate-*`, `product-eval-catalog-task.txt`.
- **Benchmark results:** under `eval/` (OSWorld-V2, WeaveBench), if any runs exist.
- **powerplatform evals:** see `pp-eval-flakiness-task.txt`.

Views:
- `rpt.eval_suite_daily`: suite, pass, fail, skip, quarantined.
- `rpt.eval_flaky_suites`.
- `rpt.rsi_eval_trend`: eval pass rate over time, next to harness run quality.

**STOP GATE 3:** the pass/fail counts of the last 5 deploy E2E gates match the GitHub run annotations.

## Phase 4 — telemetry beyond the harness
- **Logs:** Seq (`seq.easybutt0n.ai`) and the OTel collector on ptait01. Error and warning counts per service per day (`rpt.service_errors_daily`).
- **Fleet:** fleet-admin `fleet.*` device heartbeats and events (`rpt.fleet_health_daily`).
- **Gateway and services:** MCP gateway tool-call volume and errors from LiteLLM, if logged.

## Phase 5 — reports
- **Power BI Report Server**, folder `/Fleet/RSI`, built by the publisher:
  - **RSI Overview:** learning curve, first-pass rate, rounds-to-complete, cost per completed run, eval trend.
  - **Run Quality:** by repo and week.
  - **Experience Reuse.**
  - **Langfuse Traces and Cost.**
  - **Eval Health:** suites, flaky list.
  - **Service Errors.**
- **Interactive `.pbix` (optional):** an RSI dashboard built in Power BI Desktop RS on PBIRS01, if the paginated set is not enough. Save the `.pbix` in the repo.
- **Fleet Reports tab:** add sections for RSI Overview and Eval Health, read from the same `rpt.*` views.

**STOP GATE 5:** every report renders with live data (CSV export over the ReportServer URL returns rows). The fleet tab shows the new sections. Paxton signs off on one screenshot per report.

## Phase 6 — cognizioware-qa as the auditor of the QA loop (Paxton, 2026-10-05)
**Ask:** "cognizioware-qa is considered the auditor in our QA loop. All the data points needed to achieve our goals should be recorded from these, and we should get in a pattern of using cognizioware-qa to validate before external-team display or approval of your, or a team of sessions', work."

What exists: the `cognizioware-qa` repo (QA runtime + Mission Control). It has `qa_runs` / `qa_run_steps` tables with pass/fail scores and a 0-5 rating per step, a Playwright runner, and acceptance suites (`oidc-`, `billing-`, `fleet-`, `litellm-`, `overseer-acceptance`). mcp-tools also has the `qa-run-mcp` gateway service.

1. **Plan first (its own run, design only).** Write `design/qa-auditor-loop.md` in cognizioware-qa covering:
   - which work types must pass a cognizioware-qa run before they are shown to Paxton or an external team (harness run PRs, deploys, session handoffs);
   - which suite covers each type, and where coverage is missing;
   - the verdict contract: pass / fail / not-covered, with an evidence link;
   - how a harness run or a Claude session requests a QA run (`qa-run-mcp`) and where the verdict is written back (PR comment, queue entry, OPEN-ASKS row).
2. **Record every data point.** A reporting-sync module for `qa_runs` / `qa_run_steps` (stage tables), and these views:
   - `rpt.qa_runs_daily`;
   - `rpt.qa_verdict_by_work_item`;
   - `rpt.rsi_qa_first_pass`: the share of work items that pass QA on the first attempt, over time. This is the quality signal an independent auditor gives the RSI loop.
3. **The gate.** A work item is "ready for approval" only with a passing cognizioware-qa verdict attached, or an explicit `not-covered` with the gap filed. The enforcement point and exceptions come from the design in step 1. Do not block deploys with it until Paxton approves the design.
4. **Reports.** Add "QA Auditor" to `/Fleet/RSI` in Power BI and to the fleet Reports tab.

**STOP GATE 6:** Paxton approves the design. Three real work items show a recorded QA verdict in `rpt.qa_verdict_by_work_item`, and the report shows them.

## Bridge and overseer
- The overseer queues phases 0 to 5 as separate runs, in order. Each run's PR names its STOP GATE evidence.
- Phase 2 needs read keys for the two live Langfuse projects and a sampling rate from Paxton.
- Phase 6 starts with the design-only run.
- Questions for Paxton go through OPEN-ASKS, not chat.
