# TASK — n8n → code (LangGraph JS) orchestration: Phase A, verify and pin

**Status:** DRAFT — BLOCKED ON PAXTON'S DECISIONS (section "Decisions required before launch"). Do not launch until every decision row has a value.
**Owner session:** Claude Code `62e850d6-2a00-4933-a033-b41c3b3e4376` (cwd `C:\Users\PaxtonTait\source\cognizioware-powerplatform`, device PTAIT09).
**Overseer role:** manager — schedules the run once decisions are filled in, reviews, gives the owner the go to push/PR.
**Predecessor:** Step 0 = PR #50 `docs/orchestrator-reconciled-v2` → develop (https://github.com/cogniziocompany/cognizioware-powerplatform/pull/50). Phase A starts from `origin/develop` after #50 merges.
**Authoritative spec (read FIRST):** `design/orchestrator/code-first-orchestrator-handoff.md` (v2) §12 Phase A, plus `design/orchestrator/orchestration-migration-audit-2026-09-05.md` (G2, G3, G7, G10, R7) and `design/handoffs/n8n-code-orchestration-20260905/trms-delivery-contract.json`.
**Plan file:** `C:\Users\PaxtonTait\.claude\plans\we-want-to-swithc-wild-crab.md` → "### A — Verify and pin".
**Estimate:** 2–3 engineer-days. Docs + test scaffolding + lockfile only. No runtime, key, n8n, or infrastructure changes.

## Scope (exactly the plan's Phase A)

1. **Live n8n inventory** (read-only) via `ssh -F deployments/ssh/config` to the CT103 host (`192.168.21.154`, `n8n.easybutt0n.ai`): n8n version + image digest, active workflow list with IDs, exported JSON for this repo's families only (design-build-sprint, bmad-design, sprint-build, eval-micro-run, support/*), waiting executions count per workflow, credential *names* (never values). Write `design/orchestrator/baseline-2026-09-06.md` with SHA-256 of each export. Record the ~50 non-powerplatform workflows only as a count and owner list (out of scope, must not be touched).
2. **Gateway checks** against the LiteLLM router (`https://litellm.easybutt0n.ai`, fallback `https://litellm-gateway-api.cognizioware.com`): `/v1/models` catalog snapshot; health of `kimi-k2.7-code:cloud` and `general-chat`; LiteLLM version; per-key access-group visibility test in the pattern of `e2e/suites/17-mcp-server-filtering` for the planned groups (`dataverse-read`, `dataverse-write`, `xrm-metadata`, `xrm-appmodule`, `xrm-sitemap`, `qa-playwright`, `rag-lookup`); **residency test**: mint a temporary key restricted to a local model and prove whether the router's global `fallbacks` can still route it to `glm-5.3-flash:cloud`, then test `fallbacks: []` / `disable_fallbacks` on the key. Delete the temporary key. Record results in the baseline doc (G7).
3. **TRMS baseline manifest**: write `design/orchestrator/baseline-manifest.json` from `trms-delivery-contract.json#inspectedSources` (re-hash the files, fail if any hash drifted) plus the decisions below (precedence, dispositions, missing slots, interview choices).
4. **Package pins**: add to `packages/backend/package.json` (or the new `packages/orchestrator/package.json` stub) the exact versions `@langchain/langgraph 1.4.14`, `@langchain/langgraph-checkpoint-postgres 1.0.5`, `@langchain/core 1.2.9`, `@langchain/openai` (current 1.x, pin exact), `@langchain/mcp-adapters 1.1.4`, `@langfuse/langchain 5.11.0`; bump `@langfuse/otel` and `@langfuse/tracing` to the same 5.11.x; `npm install` must resolve with `zod ^3.24` on Node 22.15 and Express 5; `npm run build` in `packages/backend` stays green. No runtime import of the new packages yet.
5. **Exit criteria**: baseline doc + manifest committed; ACL and fallback results recorded with the exact key config used; lockfile resolves; `node tests/ci/workflow-guards.mjs` passes; `git diff --stat` touches only `design/**`, `packages/**/package.json`, `package-lock.json`.

## Decisions required before launch (Paxton)

| # | Decision | Options / evidence | Value |
|---|---|---|---|
| D1 | DRD precedence | `Task_2_2_DRD_v1_3_Updates.md` filename says v1.3 but body says v1.2. Which governs, and is the Updates file normative or proposal-only? | |
| D2 | Walkthrough reference version | Screenshot handoff targets `TRMS_Solution_Walkthrough_v1_1.docx`; `v1_2.docx` also exists. Which is the frozen reference? | |
| D3 | ACS email precedence | Does the ACS outbound-email change (N-4) supersede the older FRD/DRD email requirements? Any other approved later changes that override Task 2 docs? | |
| D4 | Disposition US-06 (legacy activity import, Blocked) | in scope / reference gap / excluded with reason | |
| D5 | Disposition US-22 (EnvisionConnect bulk import, Blocked) | in scope / reference gap / excluded with reason | |
| D6 | Disposition hearing T-3 reminder (unbuilt) | in scope / reference gap / excluded | |
| D7 | Disposition campaign-sending prerequisite (US-09, gated on IT ticket) and missing QA enforcement fixtures | in scope / reference gap / excluded | |
| D8 | Historical completion claims flagged inaccurate in the internal QA walkthrough (e.g. Case auto-creation) | treat as reference gap only (recommended) / other | |
| D9 | Missing screenshot slots `3.3-F`, `5.4-A`, `5.4-B` | capture new references during Phase G1 / drop the slots / supply images | |
| D10 | Interview Q1: acceptance rule | all required checks pass (proposed default) / weighted non-critical threshold (state %) | |
| D11 | Interview Q2: delivery format | written docs + chaptered recordings (proposed default) / written docs + runnable demo scripts only | |
| D12 | Interview Q3: auto-approval scope after first manual acceptance | accept-and-continue unless a change is pending (proposed default) / accept-then-pause | |
| D13 | Pilot model binding | keep `kimi-k2.7-code:cloud` (live n8n today) / switch to `general-chat` alias / other | |
| D14 | Residency posture for the pilot tenant | cloud allowed / local-only (then the G7 fallback test result decides key config) | |
| D15 | Temporary-key minting for the residency test | allowed on the shared router with immediate deletion (recommended) / not allowed, test read-only from existing keys | |

Rules the decisions cannot change (from the contract): first sprint, initial BMAD, spec revisions and final acceptance are always manual; no auto-approval policy is enabled in Phase A; reference gaps never reduce the normative denominator.

## Constraints

- Read-only against n8n, Dataverse, Langfuse and the gateway except the one temporary key in item 2 (only if D15 = allowed). No workflow edits, no activations, no imports.
- Secrets never written to the baseline doc, manifest, or task output; credential names only. The n8n API JWTs and webhook secret in `dev.env`/`uat.env`/`prod.env` are known-leaked (audit G14) and must not be copied anywhere new.
- Use `ssh -F deployments/ssh/config` aliases; do not add host keys or new SSH config.
- Explicit-path commits; no push (owner pushes on overseer go).

## Model trio (proposed)
manager `kimi-k2.7-code:pool` · executor `kimi-k2.7-code:pool` · auditor `kimi-k3:pool`.

## After completion
1. Overseer: review baseline doc, manifest (decisions D1–D15 reflected), fallback test result, lockfile diff.
2. Owner 62e850d6 (on go): push `feat/orchestrator-phase-a-verify-pin`, PR → develop titled `chore(orchestrator): phase A — live baseline, gateway ACL/fallback results, TRMS baseline manifest, package pins`.
3. Then schedule Phase B (spike + eval lane) — gated on: lockfile resolved, fallback result known, D10–D12 recorded.
