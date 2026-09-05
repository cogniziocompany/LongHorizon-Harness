# LONG-HORIZON TASK: Cognizioware EasyButton Greenfield Factory — build a production-grade Power Platform agent factory

## What this is

A long-horizon task in the LongHorizon-Harness sense: give the manager (Claude Code / Codex / OpenCode) an outcome once, and keep it working across the desktop and the terminal for dozens of hours, checkpointing verified progress at every round. That task is to build the Cognizioware **EasyButton Greenfield Factory** — a code-first, Postgres-backed agent factory that turns a public website checkout (Stripe $39/month subscription) into a freshly created Dataverse environment with a built solution, verified by an evidence trail and a human gate.

This task is intentionally designed to run for a long time with bounded steps and durable checkpointing (the LongHorizon manager + executor + auditor loop against a real computer). It references the existing LongHorizon-Harness on-disk task format (`@task.md`, `runs/<run-id>/`, `checkpoint` artifacts) while staying inside its safety rails.

## What must be true at the end

- The checkout → environment creation → greenfield factory run flow is **end-to-end real, not simulated**: a real Stripe checkout event produces a real Power Platform environment creation request, a real factory run, a real solution deployment, and real evidence rows. It starts with test data and is proven in dev before it ever touches prod.
- The factory lane is Postgres-backed (runs, phases, reports, evidence, recommendations, checkpoints) with a read model that supports replay, plus a durable human-gate checkpoint/resume pattern across host restarts.
- The agent-facing tool surface is controlled by gateway access groups keyed by role, with per-role MCP allowlists and RAG access for spec/architecture research.
- The QA lane has Playwright browser automation plus visual diffing; the RSI eval loop reports regressions into Dataverse Cases with the full eval history available.
- The whole thing is deployable by a CI pipeline and guarded by a per-phase evidence fingerprint (frozen_sha) that invalidates stale approvals on source change.

## The current state (what this session left behind)

| Item | State |
|---|---|
| Prod billing lane (`mcp-cognizioware.easybutt0n.ai`) | Healthy on sha-44c4ccc; partner endpoints 401 without key; Partner__ApiKey ↔ BILLING_API_KEY matched on all lanes |
| LitellM gateway (CT202) | Healthy; QA lane tool present; n8n workflow `LOa4QOnaJxerfKCn` produces 3/3 PASS on A1 (form_visible, form_filled, checkout_shown) in ~65s |
| Partner unit tests + Stripe lifecycle | PartnerBillingControllerTests + SignupControllerTests pass (15/15); Stripe test-clock lifecycle verified in dev lane |
| `ebtestMcp*` cleanup | Fully verified on dev (`user_organizations`, `subscriptions`, `billing_records` all 0) |
| Design docs | System design + prod CICD handoff in `design/plans/` and `design/handoffs/` |
| CT202 config | The two unpushed commits for MCP access_groups fix are now pushed to `origin/codex/litellm-expert-handoff` |

## What the factory must build (the actual product)

A **Power Platform agent factory** that turns public website checkout into a fresh CE environment with a built solution, verified by evidence, with a human gate, and a durable checkpoint that survives host restarts. The factory is not Copilot Studio or Copilot SDK; it uses an OpenAI-compatible/open-source-friendly agent SDK (OpenAI Agents SDK) against the company's own LiteLLM gateway (qwen3.6/glm-5.2:cloud) with MCP access groups that scope each agent's tools.

### Two lanes

1. **Greenfield lane** (new): from a blank Dataverse environment, build a new solution from scratch with a phase machine (requirements → architecture → planning → initialization → implementation → validation → handoff → complete). Each phase produces evidence and a frozen fingerprint; gates are mandatory and stale fingerprints invalidate downstream approvals.
2. **Brownfield lane** (existing): the current n8n-driven six-agent pipeline for updates/modifications to existing environments stays live.

### How it runs

- **Trigger**: POST `/api/factory/run` on the prod backend, with the runner's goal, customer Stripe customer id, profile, target org, source org (for parity benchmarking), and authority envelope.
- **State**: all in Postgres on CT100 — `factory_runs`, `factory_run_phases` (with human-gate checkpoints in `checkpoint_payload` JSONB), `factory_run_phase_reports`, `factory_run_traceability`, `factory_run_evidence`, `factory_run_recommendations`.
- **Workers**: spawned as child processes with cwd isolation into per-run worktrees under `/opt/powerplatform-factory-runs/<runId>/`; each worker writes a terminal report to workspace and gets validated against the schema + fingerprint + evidence references.
- **Human gates**: durable — the gating process exits after writing a checkpoint payload; a separate resume process picks it up.
- **Replay**: re-read the evidence timeline from Postgres (phase checkpoints + evidence rows) at selectable speed.

### The tool matrix (gateway access groups keyed by role)

The LiteLLM gateway is the tool gate; each key carries `object_permission.mcp_access_groups` that the gateway enforces. Per-role access:

| Role | Access groups |
|---|---|
| spec-writer | `dataverse-read`, `xrm-metadata`, `rag-lookup` |
| architect | `dataverse-write`, `dataverse-read`, `xrm-forms`, `xrm-views`, `xrm-sitemap`, `xrm-appmodule`, `xrm-webresources`, `xrm-metadata`, `rag-lookup` |
| builder | `dataverse-write`, `xrm-forms`, `xrm-views`, `xrm-sitemap`, `xrm-appmodule`, `xrm-webresources` |
| reviewer | `dataverse-read`, `xrm-metadata`, `factory-read` |
| qa-tester | `dataverse-read`, `qa-playwright`, `telemetry`, `n8n-ops` (brownfield only) |
| security-scanner | `dataverse-read` |
| orchestrator | `factory-state`, `factory-read`, `factory-preflight`, `dev-tools` (host ops only) |
| rag-researcher | `rag-lookup` |
| handoff-writer | `factory-read` |

Plus the environment-creation MCP surface (`pp-env-*`) on CT202 behind `dev-tools`, used by the checkout flow to spin up new customer envs.

### RAG layer

Microsoft Learn Dynamics/Dataverse docs + our Cognizioware design/handoff docs are vectorized into a pgvector store on CT103 and exposed via a new `rag-docs-mcp` server on the LiteLLM gateway. `spec-writer` and `architect` get `rag-lookup` access. The embedding model is local Ollama (`nomic-embed-text` 768-dim) on the LAN.

### TRMS-style "recommendations" surface

The factory records known-gap items as first-class advisory rows (`factory_run_recommendations`) with category (mid-sprint-rebuild, partial-coverage, future-concern, blocked-dependency, technical-debt, known-flake), severity, and evidence paths. These surface in the admin UI as a "not fully complete" panel, which is how TRMS handled things like the Case entity rebuild on OOB `incident` mid-sprint or the Suspension→Case Sync flow re-wire.

## LongHorizon integration notes

This task is intentionally shaped for LongHorizon-Harness:

- **Manager/Executor/Auditor boundaries** map directly onto the factory's orchestrator/worker/validator split (Manager = orchestrator's phase machine + state store, Executor = each worker's bounded step, Auditor = the validator + evidence checks).
- **The task is explicitly long-horizon** (dozens of hours across multiple sprints and hosts).
- **Durable checkpoints** are a first-class primitive of the factory (human-gate `checkpoint_payload` row + workspace mirror + resume route).
- **The LongHorizon harness runs against the live fleet** (ptait09/desk03 runners), so the same agent can also perform the deployment and QA work.

## What still needs doing

1. **M1** — Factory state helper + schema validation: Postgres-backed state rows + TS port of factory_state.py / validate_run.py, unit tests for init/gate/fingerprint/block/resume.
2. **M2** — Orchestrator + worker runner skeleton: phase machine + worker spawn loop + terminal-report validator + state transitions.
3. **M3** — First real greenfield run: a new toy CE app (publisher + solution + 2 tables + 1 view + 1 form) in a fresh eval org; verify evidence rows, traceability, fingerprint, handoff.
4. **M4** — Recommendations + admin UI: `factory_run_recommendations` + route + admin panel, demo a TRMS-style mid-sprint rebuild row.
5. **M5** — Brownfield coexistence: confirm n8n path and billing path unchanged; RSI evals still run nightly (the RSI eval gate is currently a soft-fail when no eval candidates exist, not a hard gate).
6. **M6** — MCP surfaces + RAG: `factory-state`, `factory-read`, `factory-preflight`, `rag-docs-mcp` on CT202 with access groups; populate RAG store.
7. **M7** — Checkpoint/resume durability: durable checkpoint writes + resume route; a run that stops at a human gate is resumable across host restarts.
8. **M8** — Environment creation tools: `pp-env-*` MCP tools on CT202, wired into the checkout flow.

## References

- Greenfield software factory skill: [https://github.com/kenhuangus/greenfield-software-factory](https://github.com/kenhuangus/greenfield-software-factory)
- LongHorizon-Harness: [https://github.com/AMAP-ML/LongHorizon-Harness](https://github.com/AMAP-ML/LongHorizon-Harness)
- Claims 365 Control Center module (the reference UX)
- TRMS repo (the reference build discipline + flakiness shape)
## What the factory must build (the actual product)

A **Power Platform agent factory** that turns public website checkout into a fresh CE environment with a built solution, verified by evidence, with a human gate, and a durable checkpoint that survives host restarts. The factory is not Copilot Studio or Copilot SDK; it uses an OpenAI-compatible/open-source-friendly agent SDK (OpenAI Agents SDK) against the company's own LiteLLM gateway (qwen3.6/glm-5.2:cloud) with MCP access groups that scope each agent's tools.

### Two lanes

1. **Greenfield lane** (new): from a blank Dataverse environment, build a new solution from scratch with a phase machine (requirements → architecture → planning → initialization → implementation → validation → handoff → complete). Each phase produces evidence and a frozen fingerprint; gates are mandatory and stale fingerprints invalidate downstream approvals.
2. **Brownfield lane** (existing): the current n8n-driven six-agent pipeline for updates/modifications to existing environments stays live.

### How it runs

- **Trigger**: POST `/api/factory/run` on the prod backend, with the runner's goal, customer Stripe customer id, profile, target org, source org (for parity benchmarking), and authority envelope.
- **State**: all in Postgres on CT100 — `factory_runs`, `factory_run_phases` (with human-gate checkpoints in `checkpoint_payload` JSONB), `factory_run_phase_reports`, `factory_run_traceability`, `factory_run_evidence`, `factory_run_recommendations`.
- **Workers**: spawned as child processes with cwd isolation into per-run worktrees under `/opt/powerplatform-factory-runs/<runId>/`; each worker writes a terminal report to workspace and gets validated against the schema + fingerprint + evidence references.
- **Human gates**: durable — the gating process exits after writing a checkpoint payload; a separate resume process picks it up.
- **Replay**: re-read the evidence timeline from Postgres (phase checkpoints + evidence rows) at selectable speed.

### The tool matrix (gateway access groups keyed by role)

The LiteLLM gateway is the tool gate; each key carries `object_permission.mcp_access_groups` that the gateway enforces. Per-role access:

| Role | Access groups |
|---|---|
| spec-writer | `dataverse-read`, `xrm-metadata`, `rag-lookup` |
| architect | `dataverse-write`, `dataverse-read`, `xrm-forms`, `xrm-views`, `xrm-sitemap`, `xrm-appmodule`, `xrm-webresources`, `xrm-metadata`, `rag-lookup` |
| builder | `dataverse-write`, `xrm-forms`, `xrm-views`, `xrm-sitemap`, `xrm-appmodule`, `xrm-webresources` |
| reviewer | `dataverse-read`, `xrm-metadata`, `factory-read` |
| qa-tester | `dataverse-read`, `qa-playwright`, `telemetry`, `n8n-ops` (brownfield only) |
| security-scanner | `dataverse-read` |
| orchestrator | `factory-state`, `factory-read`, `factory-preflight`, `dev-tools` (host ops only) |
| rag-researcher | `rag-lookup` |
| handoff-writer | `factory-read` |

Plus the environment-creation MCP surface (`pp-env-*`) on CT202 behind `dev-tools`, used by the checkout flow to spin up new customer envs.

### RAG layer

Microsoft Learn Dynamics/Dataverse docs + our Cognizioware design/handoff docs are vectorized into a pgvector store on CT103 and exposed via a new `rag-docs-mcp` server on the LiteLLM gateway. `spec-writer` and `architect` get `rag-lookup` access. The embedding model is local Ollama (`nomic-embed-text` 768-dim) on the LAN.

### TRMS-style "recommendations" surface

The factory records known-gap items as first-class advisory rows (`factory_run_recommendations`) with category (mid-sprint-rebuild, partial-coverage, future-concern, blocked-dependency, technical-debt, known-flake), severity, and evidence paths. These surface in the admin UI as a "not fully complete" panel, which is how TRMS handled things like the Case entity rebuild on OOB `incident` mid-sprint or the Suspension→Case Sync flow re-wire.

## LongHorizon integration notes

This task is intentionally shaped for LongHorizon-Harness:

- **Manager/Executor/Auditor boundaries** map directly onto the factory's orchestrator/worker/validator split (Manager = orchestrator's phase machine + state store, Executor = each worker's bounded step, Auditor = the validator + evidence checks).
- **The task is explicitly long-horizon** (dozens of hours across multiple sprints and hosts).
- **Durable checkpoints** are a first-class primitive of the factory (human-gate `checkpoint_payload` row + workspace mirror + resume route).
- **The LongHorizon harness runs against the live fleet** (ptait09/desk03 runners), so the same agent can also perform the deployment and QA work.

## What still needs doing

1. **M1** — Factory state helper + schema validation: Postgres-backed state rows + TS port of factory_state.py / validate_run.py, unit tests for init/gate/fingerprint/block/resume.
2. **M2** — Orchestrator + worker runner skeleton: phase machine + worker spawn loop + terminal-report validator + state transitions.
3. **M3** — First real greenfield run: a new toy CE app (publisher + solution + 2 tables + 1 view + 1 form) in a fresh eval org; verify evidence rows, traceability, fingerprint, handoff.
4. **M4** — Recommendations + admin UI: `factory_run_recommendations` + route + admin panel, demo a TRMS-style mid-sprint rebuild row.
5. **M5** — Brownfield coexistence: confirm n8n path and billing path unchanged; RSI evals still run nightly (the RSI eval gate is currently a soft-fail when no eval candidates exist, not a hard gate).
6. **M6** — MCP surfaces + RAG: `factory-state`, `factory-read`, `factory-preflight`, `rag-docs-mcp` on CT202 with access groups; populate RAG store.
7. **M7** — Checkpoint/resume durability: durable checkpoint writes + resume route; a run that stops at a human gate is resumable across host restarts.
8. **M8** — Environment creation tools: `pp-env-*` MCP tools on CT202, wired into the checkout flow.

## References

- Greenfield software factory skill: [https://github.com/kenhuangus/greenfield-software-factory](https://github.com/kenhuangus/greenfield-software-factory)
- LongHorizon-Harness: [https://github.com/AMAP-ML/LongHorizon-Harness](https://github.com/AMAP-ML/LongHorizon-Harness)
- Claims 365 Control Center module (the reference UX)
- TRMS repo (the reference build discipline + flakiness shape)


---

## LiteLLM gateway capabilities this task uses

The reference LiteLLM gateway admin UI shows the relevant sections we'll use: Virtual Keys, Playground, Models + Endpoints, Agentic → MCP Servers, Agentic → Guardrails, Policies, Tools → Search Tools + Vector Stores + Tool Policies, plus Observability (Usage, Logs, Guardrails Monitor) and Access Control (Teams, Internal Users, Organizations, Access Groups, Budgets).

This task uses them as follows:

1. **Playground (chat model + tools)** — the LongHorizon Manager/Executor/Auditor roles and our own factory worker steps can use the same LiteLLM `/v1/chat/completions` endpoint with tool calling. The model is our local `qwen3.6` for vision-heavy work and `glm-5.2:cloud` for longer reasoning; both are served by the gateway.
2. **Tools → Vector Stores** — the RAG layer should create a vector store in the gateway UI with the Microsoft Learn Dynamics/Dataverse docs chunked corpus (plus our own design/handoff docs), keyed by section path. We can then use the gateway's retrieval tool surface instead of building a separate `rag-docs-mcp` if we want to reuse the gateway's own retrieval plumbing.
3. **Tools → Search Tools / Tool Policies** — the agentic tool calls into the gateway can be constrained by tool policies per role, which lines up with the per-role MCP access groups we already defined (`dataverse-write` vs `dataverse-read` vs `xrm-*` etc.).
4. **Guardrails** — the guardrail policy surface exists and we should use it to catch common agent failure modes (e.g. hallucinated tool names, out-of-scope writes, missing evidence artifacts) before they land in a run's evidence rows.
5. **Observability → Usage / Logs / Guardrails Monitor** — the gateway's own observability surfaces already show per-key usage and logs. Where possible we should wire our factory runs to tag spans with `run_id` + `phase` so the gateway's usage logs become an additional trace surface alongside Langfuse.

This is additive to the design doc — it doesn't replace the factory's Postgres state or the existing Langfuse tracing.


## Promotion pipeline (2026-09-05)
Run 7dc4b478 (died on Ollama quota at r16) → `20260905T101725Z_bd00534f` (`:pool` trio) verified completion at r3. Branch `ci/env-promotion`
(6b41a4a promote.yml + eval-gate.yml dual-eval, scripts/promotion/README.md, actionlint; aac0d46 kb-hook fix) stacks on the never-pushed
factory lineage, so both were pushed and opened as stacked PRs: **#39** `feat/factory-environments` → develop, **#40** `ci/env-promotion` → #39.
First real promote run needs the `production` environment approval. Login-MRU: **#38** (owner 2e684a91) — also removed tracked `kb-article/.env`.
