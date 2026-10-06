---
spec_status: ready-for-dev
---

# Reporting layers v2: evals, OpenTelemetry (Seq), Langfuse, delivery, and the reports they feed (2026-10-05)

**Ask (Paxton, 2026-10-05):** "Start designing more new reports and new layers. We have the eval data and other new sources. Also all the OpenTelemetry data going to Seq."

Planning session `[47a358]`. Builds on `tasks/rsi-observability-reports-2026-10-05.md` (RSI phases 0–6) and `tasks/powerbi-report-server-2026-10-03.md`. Where they overlap, this spec owns the **layer model** and the new sources; the RSI spec keeps the harness, QA and Langfuse phases it already defines.

## 1. Layer model

Today there are two layers: `stage.*` (typed copies) and `rpt.*` (report views). As more sources arrive, every report would re-derive the same joins. Add a raw layer, a conformed layer and a semantic model.

| Layer | Schema | Holds | Written by | Read by |
|---|---|---|---|---|
| L0 Raw | `raw` | Append-only source payloads. Every row has `source`, `source_key`, `ingested_at`, `cursor` and a `jsonb` body. Retention 90 days. | reporting-sync | reporting-sync only |
| L1 Stage | `stage` | Typed, deduplicated rows per source, keyed on the source id. Already exists. | reporting-sync | core builds |
| L2 Core | `core` | Conformed dimensions and facts shared by every report (below) | reporting-sync, after stage | marts |
| L3 Marts | `rpt` | One view (or materialized view) per report section. `pbi_reader` sees only this schema. | reporting-sync | Power BI, fleet Reports tab |
| L4 Semantic | Power BI shared model | One `.pbix` dataset over `rpt.*` with relationships and measures; reports are built on it | report publisher | report authors |

Rules:
- A report never reads `stage` or `raw`.
- A source change touches only L0/L1 and its core mapping.
- Materialize a mart only when its view takes more than 2 s; refresh it in the same sync transaction.

### Conformed dimensions (`core.dim_*`)
| Dimension | Key | Sourced from |
|---|---|---|
| `dim_date` | date | generated |
| `dim_service` | `service_name` | OTel `service.name` (`OTEL_SERVICE_NAME` per compose service), compose service names, harness nodes |
| `dim_repo` | `owner/repo` | GitHub, harness workspaces, queue entries |
| `dim_environment` | `tier`: dev / uat / prod / lab | powerplatform `instances.json` tiers, Langfuse projects (`*-uat`, prod), LiteLLM key aliases |
| `dim_model` | normalised model id | LiteLLM `model_group`, Langfuse `model`, pp `eval_runs` |
| `dim_api_key` | alias or key prefix | LiteLLM keys |
| `dim_run` | harness `run_id` | CT110 |
| `dim_task_family` | queue name stem or spec file | CT110 queue |
| `dim_eval_case` | `eval_case_id` + version | powerplatform `eval_runs`, mcp-tools E2E suites, Langfuse datasets |

### Core facts (`core.fact_*`), grain first
| Fact | Grain | Sources |
|---|---|---|
| `fact_llm_calls_daily` | day × model × key × environment × service | LiteLLM spend logs; Langfuse observations (all four projects) |
| `fact_run` / `fact_round` | one harness run / round | CT110 (RSI Phase 1). Includes the **derived outcome**: accepted completion vs operator cancel vs failure vs incomplete. |
| `fact_eval_run` | one eval run | powerplatform `eval_runs` (prod 3,356 rows; dev/uat tiers too); mcp-tools E2E gate per suite |
| `fact_eval_check` | one check inside an eval run | `eval_runs.checks` (jsonb) unnested |
| `fact_eval_candidate` | one candidate | powerplatform `eval_candidates` (270) → `boundary_traces` |
| `fact_boundary_trace_daily` | day × tool × agent × actor × env | powerplatform `boundary_traces` (71,880). Volume, bytes, truncated, quarantined and quarantine reasons. No payload bodies. |
| `fact_log_hourly` | hour × service × level × environment | Seq (OTel logs) |
| `fact_error_fingerprint_daily` | day × service × fingerprint | Seq. Message template + exception type → fingerprint, first seen / last seen. |
| `fact_span_hourly` | hour × service × operation | Seq (OTel spans, if present): count, error count, p50/p95/p99 duration |
| `fact_qa_verdict` | one QA verdict on one work item | cognizioware-qa `qa_runs` / `qa_run_steps` (RSI Phase 6) |
| `fact_delivery` | one PR / one deploy run | GitHub: PR opened→merged, workflow runs (deploy, E2E gate), conclusions, quarantine decisions |
| `fact_quota_daily` | day × provider × account | Ollama weekly limits (from LiteLLM `RateLimitError`s), Langfuse events vs allowance, Synthetic credit |
| `fact_upstream_finding` | one devspecops1 finding | devspecops1 store (`tasks/devspecops1-upstream-audit-2026-10-05.md`) |

## 2. New sources and how to read them

| Source | Access path (read-only) | Status |
|---|---|---|
| **Seq (OpenTelemetry logs and traces)**: `https://seq.easybutt0n.ai`. Every mcp-tools service exports OTLP there (`OTEL_EXPORTER_OTLP_ENDPOINT=https://seq.easybutt0n.ai/ingest/otlp`, `OTEL_SERVICE_NAME` per service); powerplatform logs via `pino-seq`. | Seq HTTP API with a **read-only API key**. Aggregate in Seq with its query API (`select count(*) … group by service.name, @Level, time(1h)`), never by pulling raw events. Incremental by time window. | **BLOCKED: needs a Seq API key with Read permission** (Seq → Settings → API Keys). Anonymous reads return 401; the only Seq key in the estate is the ingest key. Store as `SEQ_REPORTING_API_KEY` in CT202 `mcp-tools.env`. |
| **powerplatform eval and boundary data**: Postgres in the prod stack on pve151 (`cognizioware-powerplatform-postgres-1`); dev/uat tiers on ptait07 CT pp-dev-uat | A dedicated **read-only role** `reporting_reader` with SELECT on `eval_runs`, `eval_candidates`, `boundary_traces` (columns without `payload`), `n8n_runs`. Network: allow only CT202's address. | Needs an operator step on pve151 (role + pg_hba + port). Never read `payload`. |
| **Langfuse**: four projects. `lh-harness` (12.9k traces / 322k observations), `lh-harness-uat` (empty), `westhivecapital-hivemind` (1,393 traces, 1,128 `tool_call_checks` scores), `powerplatform-easybutt0n-ai` (prod, new), `powerplatform-uat-easybutt0n-ai`, `cognizioware-vlab` (one dataset) | Public API with each project's key, v4-compatible endpoints, daily aggregates. Keys: PTAIT09 `.secrets\languse-cloud-key-*.txt` → CT202 `mcp-tools.env` as `LANGFUSE_REPORTING_<PROJECT>_PUBLIC_KEY/_SECRET_KEY`. | Keys available |
| **GitHub delivery data**: all org repos | GitHub API with a read-only token (Actions + PRs). Workflow runs and jobs, PR timelines, E2E gate annotations, `results.json` artifacts. | Needs a fine-grained read-only token as `GITHUB_REPORTING_TOKEN` |
| **cognizioware-qa** | Its Postgres (`cognizioware_qa`, tables `qa_runs`, `qa_run_steps`) wherever it is deployed. Phase 6 of the RSI spec finds that location. | Per the RSI spec |

## 3. New reports (Power BI `/Fleet/*` and fleet Reports tab sections)

| Report | Answers | Built from |
|---|---|---|
| **RSI Scorecard** (one page, the headline) | Are we getting better? First-pass accepted completion, QA first-pass, eval pass rate, cost per accepted outcome, change failure rate. 4-week trend each. | fact_run, fact_qa_verdict, fact_eval_run, fact_llm_calls_daily, fact_delivery |
| **Eval Health** | Which evals fail, where, and is it getting better? Pass rate by `eval_name` / `kind` / `tier` / `product_tier`; check-level failure heatmap from `checks`; duration p50/p95 (`timings_ms`); flaky cases (alternating outcomes on the same `candidate_sha`); regression by candidate sha. | fact_eval_run, fact_eval_check, dim_eval_case |
| **Eval-to-Fix Loop** | Do boundary failures become fixes? Funnel: boundary trace → eval candidate (`reason`, `status`) → eval run → outcome; time in each step. | fact_boundary_trace_daily, fact_eval_candidate, fact_eval_run |
| **Agent Tool Boundary** | What are agents doing at the tool boundary? Volume by tool / agent / actor, quarantine rate and reasons, payload size, truncation. | fact_boundary_trace_daily |
| **Service Reliability (OTel)** | Which services are unhealthy now? Errors and warnings per service per hour, top error fingerprints, new fingerprints (first seen in 24 h), span latency p95 per operation. | fact_log_hourly, fact_error_fingerprint_daily, fact_span_hourly |
| **LLM Observability** | Cost, latency and quality per project / model / role. Cost per session and per harness run; tool-call quality (`tool_call_checks`); provider rate-limit failures. | fact_llm_calls_daily (+ Langfuse scores) |
| **Delivery (DORA)** | How fast and how safely do we ship? PR lead time, deploy frequency per repo/env, change failure rate, E2E gate block reasons, quarantined suites and their age. | fact_delivery, fact_eval_run (E2E) |
| **Capacity and Quotas** | When do we hit a wall? Burn rate vs limit per provider/account (Ollama weekly, Langfuse events, Synthetic credit), days to exhaustion. | fact_quota_daily, fact_llm_calls_daily |
| **Upstream Deadlines** | What breaks next and when? devspecops1 findings sorted by days remaining (first: Langfuse v4, 2026-11-16). | fact_upstream_finding |
| **QA Auditor** | Defined in the RSI spec, Phase 6 | fact_qa_verdict |

The existing five `/Fleet` reports keep working; they move onto `core` in Phase A without changing their output.

## 4. Build phases (each its own harness run, in order where noted)

| Phase | Scope | Depends on | STOP GATE |
|---|---|---|---|
| **A. Layers** | Create `raw` and `core`. Build `dim_date`, `dim_service`, `dim_repo`, `dim_environment`, `dim_model`, `dim_api_key`, `fact_llm_calls_daily`, and repoint the existing `rpt.*` views onto `core` with identical output. | RSI P0 (outcome audit) merged | Every existing report renders the same numbers before and after (CSV diff in the PR). |
| **B. Evals + boundary** | powerplatform source module: `fact_eval_run`, `fact_eval_check`, `fact_eval_candidate`, `fact_boundary_trace_daily`, `dim_eval_case`. Eval Health, Eval-to-Fix Loop and Agent Tool Boundary reports. | A; operator creates `reporting_reader` on pve151 | `fact_eval_run` row count and outcome split equal the prod table's for the same window. |
| **C. Seq / OTel** | Seq source module (aggregate queries only): `fact_log_hourly`, `fact_error_fingerprint_daily`, `fact_span_hourly`. Service Reliability report. | A; **Seq read key** | Hourly error counts for 3 services match Seq's own dashboard for the same hours. |
| **D. Langfuse multi-project** | One module, list of projects. Joins harness runs on session/`lh-run` tags. LLM Observability report. | A; RSI Phase 2 keys | Daily cost for `lh-harness` agrees with LiteLLM spend for that key within 5%. |
| **E. Delivery** | GitHub module: `fact_delivery`, E2E gate results. Delivery (DORA) report. | A; `GITHUB_REPORTING_TOKEN` | Deploy counts for 2 repos match the Actions UI for the same week. |
| **F. Scorecard + quotas + deadlines** | `fact_quota_daily`, `fact_upstream_finding`; RSI Scorecard, Capacity and Quotas, Upstream Deadlines. Power BI shared model (`.pbix`) over `rpt.*`. | B–E as available | Scorecard renders from live data; each tile links to its detail report. |

**Every phase:** tests with fixtures (no network), the read-only and no-payload rules checked by tests, secrets by name only, PR evidence for the STOP GATE, a cognizioware-qa verdict on the PR (RSI Phase 6) once that gate exists.

## 5. Needs from Paxton
1. **Seq read API key** (Seq → Settings → API Keys → Read). Save as `C:\Users\PaxtonTait\.secrets\seq-reporting-read-key.txt`.
2. **GitHub fine-grained token**: org repos, read-only Actions, Contents (metadata), Pull requests. Save as `.secrets\github-reporting-read-token.txt`.
3. **OK to create a read-only Postgres role on powerplatform prod** (pve151), reachable only from CT202. This changes a prod database's access rules, so it needs an explicit yes.
