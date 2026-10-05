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

## Phase 2 — Langfuse Cloud (project cognizioware-vlab, US region)
**Needs from Paxton:** a Langfuse API key pair for the project, saved on PTAIT09 as `C:\Users\PaxtonTait\.secrets\langfuse-cognizioware-vlab-keys.txt` and set on CT202 as `LANGFUSE_REPORTING_PUBLIC_KEY` / `LANGFUSE_REPORTING_SECRET_KEY`.
- The per-key logging credentials on the LiteLLM `lh-harness` key are not usable: verified 2026-10-05, they return 401.
- `langfuse-mcp` has no stored key.

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

## Bridge and overseer
- The overseer queues phases 0 to 5 as separate runs, in order. Each run's PR names its STOP GATE evidence.
- Phase 2 is blocked until the Langfuse key pair exists. Run Phases 1 and 3 in the meantime.
- Questions for Paxton go through OPEN-ASKS, not chat.
