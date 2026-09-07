# Hydra as the Overseer Control Plane for LongHorizon-Harness

## Product evaluation catalog, clean rebuilds, promotion, and cleanup (2026-09-07)

### Confirmed user decisions and working interpretation

- TRMS stays evaluation #1 for powerplatform.easybutt0n.ai. Add evaluation #2, #3, and an extensible #N catalog from existing completed examples.
- Full-build evaluations exercise the product flow from uploaded files through BMAD/specification, approvals, sprint build/rework, final delivery, independent QA, and advisory evaluation scoring.
- A valid score STRICTLY GREATER THAN 90% automatically passes the evaluation promotion gate. Exactly 90% does not auto-pass. Continue through lower environments toward production using the existing product promotion pipeline.
- Anything less than a verified 100% remains intact for review: session/workspace and its cloud solution/components. This includes an 91-99.99% result that passed promotion. No automatic delete, archive, expiry, or teardown for those runs.
- A verified 100% result is eligible for automatic cleanup after evidence is safely retained and any downstream use of that run's artifacts has finished.
- A shared web/admin feed must show auto-deleted runs and runs waiting for Paxton's approval. Failed/incomplete runs remain in this same feed with their artifacts available.
- Lowest implementation effort: extend the Power Platform Admin Evals panel and reuse QA Mission Control evidence/detail pages; expose links or an embedded read view in Hydra. Do not create a separate dashboard or scoring service.
- Working interpretation after the optional aggregation question received no answer: apply >90% to each enabled full-app evaluation in the release manifest. All selected case gates must pass before promoting the candidate; also display the combined score, but do not let a high average conceal a case at 90% or below. This is an explicit implementation assumption, not a claim of a separately confirmed aggregation choice.
- The deployment being evaluated here is the Power Platform PRODUCT candidate promoted through dev/UAT/prod. Recreated Ticketing/TRMS apps are disposable eval outputs in the tier's designated Dataverse eval targets. Promoting the product does not publish those test apps into a customer's production org.

Latest user correction: the auto-promotion threshold is over 90%, replacing the earlier over-80% discussion. This policy supersedes earlier proposed evaluation thresholds and manual-only evaluation promotion language in this plan. Existing build/design inputs and specification-change approval semantics still apply. No deployments, deletions, or scheduled evaluation jobs were executed by this plan revision.

### Catalog: evaluation cases, not accumulated leftover apps

The app selector screenshot identifies useful example candidates (Ticketing, Tool Lending Library, PetLicensing, School Science Fair/Science Fair variants, TRMS App). It does not establish solution GUIDs, component ownership, session mapping, or which app names are duplicate runs. Resolve those from product/run records and Dataverse metadata before using any existing app as a reference.

| Case | Stable proposed key | Reuse grounded in existing work | Full-build evaluation scope |
|---|---|---|---|
| #1 TRMS | pp-full-trms | Existing TRMS delivery contract, functional/technical specs, final-solution walkthrough and screenshot references | Existing two-axis specification conformance and final-reference fidelity, complete product delivery, documentation and foresight analysis. |
| #2 Ticketing | pp-full-ticketing | e2e/evals/catalog.mjs multi-mini-app and the Ticketing example shown in the screenshot | Upload a human-readable requirements pack describing Ticket, auto-number Ticket Number, Priority choice, Description, Due Date, Vendor lookup, and Ticketing model-driven app. Verify create/edit/reopen behavior and presentation against the frozen case assertions, then verify final specs/output/docs/advisory report. |
| #3 Tool Lending Library | pp-full-tool-lending | ARBITRARY_POOL in packages/backend/src/routes/rsi.routes.ts; design/rsi/golden-run-01-trms-eval.md arbitrary-app result; screenshot's Library/Staff variants | Members, tools, borrowing/returns, due dates, late fees and maintenance according to a frozen uploaded requirements pack. Resolve which prior app variant supplies the reference; do not infer shared ownership from display names. |
| #4 Pet Licensing | pp-full-pet-licensing | Existing arbitrary-app request and PetLicensing example | Registration, annual renewal, lost-pet reports, citations and reunification as specified in the uploaded case pack, including finished documentation and independent outcome evidence. |
| #5 Science Fair | pp-full-science-fair | Existing arbitrary-app request and Science Fair app variants | Project submission, judging/rubric scores, table scheduling and awards per the case pack. Reconcile prior variants into one versioned definition rather than treating each leftover app as a new case. |
| #6 ... #N | pp-full-<case-key> | Other prior examples with recoverable requirements and reference evidence | Add a data-driven case manifest and assertions using the same driver, score policy, ownership model, and cleanup lifecycle. No fixed maximum or combinatorial explosion is required. |

Implement the catalog in cognizioware-powerplatform. Reuse existing example requirements; materialize uploadable input packs from those sources and record source provenance. A draft synthesized from a historical prompt must be labeled as a new fixture version, not as an original uploaded customer document.

Each case manifest records case ID/version, input files and hashes, required artifacts, atomic assertions, expected browser journeys, reference screenshots where available, applicable scoring dimensions, scope/exclusions, and allowed target constraints. Freeze it before a run; generated output cannot remove expected assertions. Keep input fixtures and reference oracles independent from generated results.

The current micro/multistep cases remain useful lower-cost diagnostics. The existing Ticketing micro/multistep webhook case is not the new upload-to-delivery evaluation #2.

### Review of the current implementation

Source review on PTAIT09: cognizioware-powerplatform origin/develop 046c73fb0f02dd1cc18d0797ca00469cbfc89c67 and cognizioware-qa origin/master 1a8784d7b328f60d90d05639078559e85ef474f7, fetched without switching or editing the working branches. These findings are source-grounded; deployment versions and live state still need verification during implementation.

| Existing capability / gap | Source | Required application |
|---|---|---|
| Session attachments are uploaded, text-extracted, persisted, and folded into Start Design input. Extraction/dispatch caps and quarantining can omit content. | Power Platform session.routes.ts, attachment.service.ts, workspace.routes.ts | Exercise the real upload UI. Assert file hash, extraction success and actual baseline inclusion; fail coverage for missing/truncated required content rather than merely checking HTTP 201. |
| The RSI full-app runner inserts a session directly and calls the design webhook. It uses latest-workflow-execution polling and a pipeline QA verdict. | Power Platform routes/rsi.routes.ts | Add the UI-driven full-build runner. Bind each observed execution to the exact session/run; a concurrent run's completion must never count for this evaluation. |
| The existing Ticketing case uses shared evalsuite / eval_* fixtures and direct developer/QA webhook prompts. | Power Platform e2e/evals/catalog.mjs | Preserve as a micro/multistep test; add an isolated upload-to-delivery case with its own fixture manifest and session-owned solution. |
| DELETE /api/sessions/:id calls archiveSession; it only sets status=archived. solution_state begins as an unmanaged placeholder, and target_solution_name is not a complete ownership ledger. | Power Platform session.routes.ts, session.service.ts | Introduce an explicit eval cleanup action tied to solution GUIDs and created component IDs. Keep ordinary customer session archiving behavior intact. |
| QA has POST /api/qa-runs, persisted service_qa_runs, reusable workflow/client, result callbacks and Mission Control detail/comparison screens. | QA qa-runs.routes.ts, qa-runs.service.ts, qa-run-trigger.service.ts, contracts/, frontend/src/qa/ | Reuse this service. Extend/version request/result contracts for full-product cases, product session/workspace/solution IDs, fixture manifests, detailed scores and cleanup correlation. |
| The strict v1 QA request has ce/web-ux targets and trms/greenfield profiles, but no full product-upload lifecycle or eval-case identity. | QA qa-run-request.v1.json | Add a compatible versioned contract and update schema, caller, dispatch workflow, runner and result path together; unknown extra fields are currently rejected. |
| The greenfield discovery helper examines the factory's Postgres migrations and explicitly cannot infer a generated app's full Dataverse requirements. | QA runner/flows/factory-discovery.ts | Derive Ticketing/Library/etc. assertions from each approved product case baseline. Keep factory-infrastructure discovery as a distinct diagnostic. |
| Basic QA scores, weighted scores, infra failures and skipped checks already exist. Service callback validation currently checks little more than verdict and numeric score; some report paths compute percentages over only scored checks. | QA qa.service.ts, qa-runs.routes.ts, scripts/gate-report.mjs, gate-status.mjs | Validate the full result schema and correlate candidate/run/manifest. Derive full-case scores against the frozen expected denominator. A displayed 100 over a subset does not authorize deletion. |
| Factory recommendations already have storage and APIs; admin has EvalsPanel. | Power Platform factory-recommendations.ts, factory.routes.ts, AdminPage.tsx | Link existing advisory rows to the eval session/run and expose scored dimensions plus recommendations alongside promotion and cleanup status. |
| Promotion already combines primary in-repo and secondary QA scores, defaults to 50/50 and threshold 90 with >= comparison. Upstream primary/secondary failures can stop execution before the weighted gate. | Power Platform promote.yml, eval-gate.yml, scripts/eval-gate.mjs | Implement the user's strict >90 policy end-to-end. Publish valid partial scores even when assertions fail; keep score collection separate from pass decision. Retain infra-error classification and never fabricate a score for missing execution. |

### One full-build attempt, from upload to evidence

1. Register the attempt with evalCaseId/version, product candidate SHA/image digest, product tier/base URL, tenant, target Dataverse environment ID/URL, and input manifest hashes. Reserve a clean disposable target/namespace before the browser starts.
2. Sign in to the tier's Power Platform product, select its allowed eval environment, create a NEW session/workspace, and upload the case input files through the product UI. Verify actual extraction and inclusion in the design baseline.
3. Run the existing BMAD/design and approval flow; save brief, PRD, technical design, stories, sprint plan, acceptance assertions and the approved baseline. Preserve interactive questions and sprint-boundary specification changes.
4. Build through the PRODUCT's normal orchestration and tools. Keep exact session/execution IDs and solution/component ownership receipts. Bounded rework within an attempt is allowed and recorded.
5. Reach a real terminal delivery state. Collect the built solution export/output, generated specs, delivery documents, walkthrough/demo recordings, gap report and recommendations. A timeout or missing artifact remains visible and cannot be labeled complete.
6. Cognizioware QA independently checks the generated app via API and browser, including screenshots and business behavior. Grade requirement coverage, technical/functional conformance, presentation/reference fidelity when applicable, documentation and the requested advisory/foresight evaluation.
7. Persist the complete run/evidence package outside the disposable session and cloud solution. Store individual failed/missing assertion IDs, numerator/denominator, score dimensions, timings/cost, and regression links.
8. Compute promotion and cleanup as SEPARATE decisions from the persisted result. Show them immediately in the relevant tier's admin feed.
9. Where cleanup is eligible/approved, remove only the attempt-owned cloud artifacts, verify removal, then purge the disposable session/workspace data while retaining the permanent eval record and evidence. Record a cleanup receipt and verify the next fresh attempt cannot reuse leftovers.

Full-build mode starts clean on every NEW attempt. It does not need a newly purchased/recreated Dataverse environment every time: a verified clean isolated namespace in a dedicated eval org can suffice. Exact-name parity cases require an exclusive clean target when unique run prefixes would change what is being scored. A prior failed attempt kept for review cannot be overwritten; use another isolated target or queue.

Keep explicit brownfield/rework evaluations as a separate mode with a pinned starting solution. Do not relabel cumulative convergence against leftover state as a fresh-build pass.

### Environment -> session/workspace -> solution ownership

One disposable evaluation session/workspace owns one primary Dataverse solution, plus an explicit list of any ancillary assets it creates. A solution may contain multiple app modules; an app card is not the ownership boundary.

Persist: tenantId, productTier, productSessionId, productWorkspaceId, evalCaseId/version, evalRunId, targetEnvironmentId/OrganizationId, solutionId/uniqueName/type, publisher/prefix, created component IDs/types, created data/flow/external artifact IDs, pre-existing dependency IDs, artifact/evidence manifest hashes, and cleanup state.

Validate the target identity at every build, QA, and cleanup boundary. The product tier (dev/UAT/prod) and the Dataverse target organization are distinct fields. Production-tier evaluation uses its designated disposable eval org, not a customer's live production org.

For unmanaged solutions, deleting the solution only removes the container and leaves the customizations. Cleanup must remove exclusively run-owned apps, site maps, relationships, tables/columns, flows and other components in dependency order, then the container, and verify absence. For managed test imports, use supported uninstall and dependency checks; retain export/evidence before removal. Never delete the Default Solution, shared/OOB components, or other sessions' assets based on a name/prefix guess. Unresolved ownership/dependencies make cleanup blocked and visible, even at 100%.

References: https://learn.microsoft.com/en-us/power-platform/alm/solution-concepts-alm and https://learn.microsoft.com/en-us/power-apps/maker/model-driven-apps/delete-model-driven-app

### Independent promotion and cleanup policy

| Final validated case score | Promotion decision | Session and cloud artifacts |
|---|---|---|
| Exactly 100%, all expected assertions accounted for | Auto-pass | Auto-clean after evidence retention and downstream artifact use; keep permanent eval feed/report. |
| >90% and <100% | Auto-pass with visible gaps; candidate advances when the release's selected eval gates pass | Retain intact; awaiting Paxton's cleanup/review decision. |
| 0-90% inclusive | Hold promotion for review | Retain intact; awaiting review. |
| Unknown, incomplete result, missing evidence, or infrastructure-blocked evaluation | No valid auto-pass decision | Retain intact; show reason and retry/review action. |

Use full precision and expected assertion counts for decisions. 90.00 is not greater than 90; 99.995 displayed as 100 is not a complete 100% result. A genuine partial test result may have a valid numeric score and pass above 90; an absent/invalid test result is not converted into one. Keep all gaps visible after an auto-pass. Recommendations that are optional future ideas remain advisory; required assertions cannot be relabeled advisory to inflate the score.

A lower-tier case at <100 stays intact even if a later-tier run reaches 100 or the product deploys to prod. Evaluate cleanup per attempt, never from a release average. Under-100 review actions are: inspect evidence/open retained app, retain, approve cleanup, or rerun as a new isolated attempt. Approval of cleanup does not rewrite the score or grant promotion.

Promote the SAME candidate digest through dev -> UAT -> prod, re-running selected product evaluations at each tier against that tier's eval targets. Expose dev results before UAT, UAT results before production, and post-deploy production verification separately. A failure detected after production is marked post-deployment review-required, not retroactively "blocked before deploy."

For product eval promotion, align the existing 90% setting with strict >90 comparison, remove all-green short-circuits and raw QA-status-as-gate coupling with the >90 evaluator. Keep raw QA verdicts and defect classifications for diagnosis. Existing unrelated CI gates continue to apply. Inspect repository environment protections during implementation; configure the authorized automated product promotion path through supported settings, never bypass protections or credentials. This document does not assert that those settings have already changed.

### Minimal admin UX and durable retention

Primary implementation: extend cognizioware-powerplatform/packages/frontend/src/admin/AdminPage.tsx EvalsPanel with one feed grouped/filterable by tier, case and candidate, and a session/workspace link. Reuse QA RunDetail/CompareRuns for evidence. Add deep links in Hydra's run activity so the same decision is discoverable from the infrastructure UI.

Each row shows case/version, attempt, dev/UAT/prod, product session/workspace, solution/app links, exact score plus coverage, failed assertions/advisory findings, comparison to the previous equivalent attempt, promotion state, cleanup state, timestamps and actor. Show Auto-deleted, Awaiting review, Retained after auto-pass, Cleanup running, Cleanup blocked/failed, and Manually deleted explicitly.

The permanent eval feed, input/reference versions, scored assertions, generated documentation, screenshots/recordings, exported output, recommendations and cleanup receipts must survive deletion of the disposable session. Do not cascade-delete them through sessions or QA environment deletion. Existing temporary GitHub artifact URLs are insufficient as the sole long-term evidence store; persist required artifacts through the existing artifact store before cleanup.

Use a durable cleanup job with idempotency and per-resource receipts. Save evidence first, stop writers/lock the exact owned target, delete/verify cloud resources, then purge session data and temporary blobs. If cloud deletion fails, leave session and ownership information accessible and mark cleanup blocked. A successful HTTP request alone does not mark Auto-deleted. Approvals are tied to the exact run and ownership manifest.

### Implementation slices and reviewable exit criteria

A. Product-owned case catalog and uploaded fixtures: define #1-#5, extensible manifest schema, assertion/register and source versions; materialize Ticketing first from the existing example. Keep fixture readiness distinct from a successful executed eval.
B. Full-product browser driver: start at upload, carry the baseline into build, correlate exact session/execution IDs, collect final documents and QA/advisory outputs. Run on CT210/company CI compute, with LH-Harness/Hydra supervising implementation and infrastructure operations.
C. QA contract and scoring integration: reuse QA service/client/workflow with versioned full-case inputs; derive checks from the approved app case manifest; validate complete results and persist evidence. Bind scores to candidate, tier, environment, session and case version.
D. Promotion and early visibility: implement strict >90 case policy in the shared decision logic, replace conflicting promotion short-circuits, surface dev/UAT findings before production, and expose raw QA plus combined summaries.
E. Ownership and retention: durable solution/component inventory, permanent eval records independent of sessions, recoverable cleanup job, verified 100%-only automatic cleanup and manual review for all lower scores.
F. Admin feed: extend the existing Power Platform Evals panel, reuse QA details/comparisons, link from Hydra. No duplicated state store or dashboard.
G. Prove two independent clean Ticketing attempts and then the remaining cases. Acceptance scenarios: 90 holds; 91/95 pass but remain intact; exact 100 cleans after saved evidence; rounded-100 does not clean; missing/skipped required assertions do not masquerade as full coverage; failed runs remain in the feed; manual approval deletes only the selected run's owned assets; cleanup failure retains inspectable session/manifest; concurrent runs cannot cross-score or cross-delete; retry cannot inherit hidden app/schema/data leftovers.

The target is repeatable, attributable product evaluation #1 through #N with early lower-tier feedback. Work required to implement these slices belongs in the existing product/QA/infrastructure owner queues; this revision defines the work and accepted flow and does not claim runnable new evaluations or live cleanup/promotion changes.

---


## Scope correction - Power Platform product and TRMS evaluation #1 (2026-09-07)

**User-confirmed scope:** TRMS is evaluation #1 for **https://powerplatform.easybutt0n.ai**, owned by **C:\Users\PaxtonTait\source\cognizioware-powerplatform**. Hydra, LongHorizon-Harness, the model/tool gateway, memory, and the QA service are shared infrastructure supporting that product and other repositories.

This section governs the product/evaluation boundary and current follow-through. The original work packages below remain implementation context; their present-tense deployment claims must be reconciled against current code and deployed versions before execution.

### Ownership and what is being evaluated

| Layer | Owner / location | Responsibility and evidence |
|---|---|---|
| Product under evaluation | powerplatform.easybutt0n.ai; cognizioware-powerplatform | Customer request, BMAD design/import, approval, sprint build/rework, conversational status, solution delivery, documentation, and product evaluation history. |
| Evaluation #1 | TRMS fixtures and delivery contract in the Power Platform evaluation program | Measure how closely environments produced THROUGH THE PRODUCT satisfy the functional and technical specifications, and separately how closely they match the final TRMS solution reference. TRMS-specific identifiers, fixtures, assertions, and scoring belong here. |
| Shared execution infrastructure | LongHorizon-Harness + Hydra | Bounded execution, independent audit, durable progress, fleet placement, workspace ownership, recovery, queueing, and activity visibility. Infrastructure checks establish that these mechanisms work. |
| Independent QA service | cognizioware-qa; server QA lane documented on CT210 | Browser/API checks and Playwright/Puppeteer screenshot validation against the product's pinned evaluation manifest; return evidence tied to the actual target environment and candidate. |
| Shared gateway and memory | cognizioware-mcp-tools / LiteLLM / Hivemind | Role-scoped model/tool access, trace correlation, and recalled decisions with provenance. Product acceptance remains grounded in the product's authoritative contract and evidence. |

The development harness may build or improve cognizioware-powerplatform as one of its repository workloads. Separately, the product's own orchestration can adopt Manage-Execute-Audit principles through its existing code-first orchestration plan. Running an LH-Harness worker that directly builds TRMS outside the product does not establish a passing product eval. Any direct-build diagnostic must be labeled separately from evaluation #1.

Retain the existing product orchestration migration ownership and engine contracts. This plan does not select a wholesale runtime replacement or move product acceptance authority into Hydra.

### Apply the paper at both boundaries

1. **Shared infrastructure:** preserve independently verified checkpoints across workers and hosts; enforce exclusive workspace ownership and capacity; keep executor claims separate from auditor findings.
2. **Product orchestration:** carry the approved BMAD baseline and requirement IDs through bounded build/rework steps, independently validate target-environment changes, and update completion state only from accepted evidence.
3. **Product evaluation #1:** exercise the customer-facing Power Platform workflow with TRMS as the reference case. Score the resulting solution and delivered documentation; retain run IDs that connect the product session to infrastructure and QA evidence.

Single-action versus batched-action behavior is an execution-setting comparison. Batch operations may be used within a bounded step, with verification before dependent steps. It must not change the product's requirement denominator or redefine a partial delivery as complete.

### TRMS evaluation #1 contract

- **Mandatory input:** approved BMAD output, generated or imported through the supported product path, must reach the build step. No build dispatch without the approved baseline and required manifests.
- **Two separate comparisons:** (a) normative functional/technical specification conformance, and (b) fidelity to the final-solution reference, including behavior and visual evidence. Historical omissions or incorrect completion claims in the reference never reduce the normative denominator.
- **Stable run identity:** bind baseline/spec hashes, candidate SHA, environment identity/fingerprint, graph/profile version, QA run IDs, and documentation artifacts to the same evaluation run. Retain product and infrastructure run IDs separately and correlate them.
- **Independent validation:** use cognizioware-qa browser/API checks and Playwright/Puppeteer screenshot evidence. Schema counts alone do not establish workflow, visual, or full-solution conformance.
- **Conversation and changes:** users can ask questions throughout execution. Product specification changes are queued for sprint completion, then reviewed, versioned, and used for the next build/refactor cycle. Infrastructure pacing at a harness round boundary cannot silently alter an active product sprint's approved scope.
- **Delivery:** written specifications, walkthrough/demo documentation, chaptered recordings, known gaps, and the foresight-gap-analysis outcome are part of the completion contract. Distinguish useful future recommendations from implemented and verified behavior.
- **Acceptance:** use the current user-confirmed evaluation promotion and cleanup policy above (>90% auto-promotion; verified 100% only for auto-cleanup). Preserve approved source-precedence decisions and the existing design/specification-change gates. Earlier proposed evaluation thresholds are superseded; deployment settings still require implementation through supported controls.
- **Generalization:** TRMS remains evaluation #1 within a reusable product evaluation system. Retain arbitrary-app, micro, and multistep evaluations to detect TRMS-specific overfitting.

### Current source grounding

Inspected on PTAIT09 on 2026-09-07; source inspection does not attest current deployment:

- Power Platform checkout: aac0d46041f18569d48728cdbe71afcbd4234678, branch ci/env-promotion. Existing untracked work was left intact.
- Product-owned evaluation references in that checkout: design/rsi/golden-run-01-trms-eval.md; design/handoffs/trms-qa-eval-handoff-2026-07-15.md; e2e/playwright/trms-eval.spec.js; e2e/evals/README.md and catalog.mjs.
- Newer design contracts were read from the already-local origin/develop ref: design/orchestrator/code-first-orchestrator-handoff.md; design/orchestrator/baseline-manifest.json; design/handoffs/n8n-code-orchestration-20260905/trms-delivery-contract.json. The Phase A manifest records resolved decisions; the older design contract still contains proposal/unresolved fields. Reconcile through the product's existing precedence rules, not the older fields alone.
- LongHorizon-Harness checkout 108b861 and Hydra fleet worktree 721de8f show existing infrastructure implementation, so the original "greenfield" work packages below are not a current completion inventory.
- LongHorizon-Harness/tasks/fleet-entry-point-and-harness-ux-2026-09-07.md records the queue moving into the harness service (POST/GET /api/queue, lhharness gateway alias), with chat and Hydra using the shared entry point. It also records that the overseer cutover had not happened.
- LongHorizon-Harness/tasks/ci-offload-and-qa-service-2026-09-06.md records server execution/QA offload. Normal CI/QA and harness workloads stay on company servers; PTAIT09 is for active development and explicit exceptions.

### Revised follow-through and acceptance tracks

1. Reconcile implemented, merged, deployed, and actually exercised capabilities separately. Follow the existing queue/UX work rather than creating a second scheduler or queue.
2. Harden the shared infrastructure: align lifecycle vocabulary; treat unavailable run inventory as unknown; atomically reserve workspaces; obey capacity even on fallback; confirm worker exit before migration; transfer and verify checkpoint/artifact state on the destination. Keep these changes owned by the relevant infrastructure repo sessions.
3. Validate those mechanisms using small disposable infrastructure tasks and failure/recovery scenarios. Infrastructure acceptance must be independently demonstrable without a TRMS build.
4. In cognizioware-powerplatform, continue the existing product orchestration plan with approved BMAD inputs, evidence-bound state, sprint change boundaries, QA integration, and final documentation. Cross-repo support is coordinated by the overseer; the product owner retains product contract decisions.
5. Run TRMS evaluation #1 through the supported Power Platform workflow on its approved eval tier/target. Record both specification conformance and final-reference fidelity, documentation completeness, false completion claims, recovery, intervention, time, and cost. Infrastructure failures remain visible in the overall product attempt outcome, with cause attribution.
6. Compare baseline and changed product orchestration under matched task/spec, target starting state, model/tool permissions, and budgets; vary model mixtures separately. Use arbitrary-app and smaller product evals as regression checks. Paper benchmark percentages are external evidence, not predicted TRMS scores.

**Acceptance separation:** an infrastructure pass means its execution/control contracts hold; a product evaluation pass means the delivered solution satisfies the product's approved evaluation contract. Neither result automatically grants the other. Deployment and product acceptance continue through their existing authorized gates.

---


## Original implementation context (historical)

`tasks/TEMPLATE-overseer-hierarchy.md` encodes a three-tier supervision doctrine (human → overseer session → repo-session agents → harness runs) that today is executed **entirely by hand**: an overseer agent finds session UUIDs in `~/.claude/projects/*.jsonl`, shells `claude --resume -p`, and polls `GET /api/runs/{id}/snapshot` against one of two hand-built harness deployments (WSL2 on ptait09, CT110 systemd). There is no programmatic multi-run orchestrator anywhere in `LongHorizon-Harness` — no fleet script, no poller, no placement logic. Every rule in the playbook (one run per working tree, pacing instructions, step budgets, successor runs, load-shedding) is doctrine an agent must remember rather than an API it can call.

Meanwhile `cognizioware-hydra` already is a working fleet control plane: an orchestrator with a device registry, outbound-dialing device agents on Windows/Linux, REST + a stateless MCP server at `POST /mcp` registered into the LiteLLM gateway as alias `hydra`, per-device health heartbeats with `throttled`/`gating` flags, and per-OS service installers. What it lacks is any notion of a *durable task* — sessions are ephemeral PTYs, and "run a job" means `send_input` + poll `read_screen`.

**Goal:** put the two together. Hydra becomes the overseer-facing control plane: it installs our lh-harness build on a device as a Docker node, exposes harness runs as first-class fleet objects across many machines / repos / OSes, and gives an overseer one MCP surface for both run-level supervision (snapshots, instructions, gates) and infra-level actions (place, migrate, offload). CT110 stays the reference instance and serves as both the default placement target and the overflow fallback.

**Decisions taken (confirmed with the user):**
- Control plane lives **in Hydra**; harness stays a payload Hydra installs and drives.
- Harness reachable **both** ways: Hydra-proxied by default (harness bound to loopback inside its container), direct base-URL as a debug escape hatch.
- Infra scope v1 = **migrate + offload + terminal/session kill & restart + harness-container restart** — every action short of device power. No device reboot/shutdown, no host-service restarts, no deploy actions.
- Installer v1 = **Docker image only**; primary and fallback placement both point at the CT110 instance.

---

## Architecture

```
Overseer (Claude session)
   │  MCP alias `hydra` (LiteLLM gateway) │ or REST + Bearer HYDRA_API_KEY
   ▼
Hydra orchestrator  ── harness-registry ── placement/offload scheduler
   │                      │
   │ WS /agent            │ direct HTTPS (external nodes)
   ▼                      ▼
device-agent (ptait09, desk03, htpc01)      CT110  harness.lan.easybutt0n.ai
   └── docker: lh-harness-node container    (external node, kind=external)
         └── lh-harness web :8799 (loopback) + workspaces + repos
```

A **harness node** is the new unit of placement. Two kinds:
- `kind: "managed"` — a `lh-harness-node` container Hydra installed on a registered device; reached by proxying HTTP through the device agent's WS.
- `kind: "external"` — a pre-existing harness (CT110) reached by direct HTTPS + bearer. Registered from config, never installed or migrated by Hydra.

---

## Work item 1 — `lh-harness-node` Docker image (repo: `LongHorizon-Harness`)

New top-level `docker/` directory. The repo currently ships **no** Dockerfile, compose, or install script — this is greenfield.

- `docker/Dockerfile` — `python:3.12-slim` base; install `lh-harness` from the repo (`pip install -e .`) so repo edits are the source of truth like the WSL2 deployment; install Node + `@anthropic-ai/claude-code` for the `claude_code` adapter; `git`, `openssh-client`. Non-root user `harness` (uid 1001), `WORKDIR /home/harness`. **Linux only** — `LocalEnvironment.exec` uses `os.killpg`/`SIGHUP` and is unrunnable on native Windows (`cognizioware-how-to.md:36-37`); on Windows devices the container is the whole point.
- `docker/entrypoint.sh` — materialize `$WORKSPACE_ROOT/.lh-harness/config.toml` from env if absent (reuse `CONFIG_TEMPLATE` in [config.py:60-142](src/lh_harness/config.py#L60-L142) rather than hand-writing TOML — call `lh-harness init`), then `exec lh-harness web --host 0.0.0.0 --port 8799 --auth-token "$LH_HARNESS_WEB_TOKEN" --runs-root … --workspace-root … --no-open`. The server refuses a non-loopback bind without a token ([server.py:1217-1221](src/lh_harness/webapi/server.py#L1217-L1221)) — the token is mandatory and generated at install time.
- `docker/compose.node.yml` — one service, named volumes for `/home/harness/work` (workspaces/repos) and `/home/harness/runs` (runs-root), `.ssh` mount for the GitHub deploy key, healthcheck `GET /api/meta` with the bearer.
- **Config surface passed through verbatim** — this is the "exact user-facing config capabilities" requirement. Env → config.toml, no new invented keys:
  - `[run]`: `agent`, `model`, `reasoning_effort`, `runs_root`, `workspace`, `base_url`, `prompt_language`, `max_rounds`, `guard_exclude_paths`, `claude_mcp_config`, `codex_mcp_config`, `mcp_add_dirs` ([config.py:28-48](src/lh_harness/config.py#L28-L48))
  - `[run.roles.<role>]` for all 8 role names ([config.py:17-26](src/lh_harness/config.py#L17-L26))
  - `[run.timeouts]` — **only** `manager`/`gui_executor`/`cli_executor`/`auditor`; a misplaced key crashes the worker
  - env: `LH_HARNESS_WEB_TOKEN`, `LH_HARNESS_WEB_DEFAULT_AGENT/MODEL/MANAGER_MODEL/AUDITOR_MODEL` ([server.py:40-47](src/lh_harness/webapi/server.py#L40-L47)), `LH_HARNESS_DASHBOARD_FAILURE_LIMIT`, `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` (LiteLLM routing; drives model discovery in [model_catalog.py:213-270](src/lh_harness/model_catalog.py#L213-L270))
- `docker/README.md` — the two supported placements and the CT110 equivalence note.

Seed the image config from the known-good CT110 values (`.lh-harness/config.toml` in `cognizioware-mcp-tools`: manager 300 / gui_executor 5400 / cli_executor 5400 / auditor 600, guard excludes `node_modules`, `graphify-out`, `logs`) and the trio in [tasks/TEMPLATE-litellm-qwen-local.md](tasks/TEMPLATE-litellm-qwen-local.md).

## Work item 2 — harness registry + node lifecycle (repo: `cognizioware-hydra`)

- `orchestrator/src/harness-registry.js` (new) — node records `{nodeId, kind, deviceId?, baseUrl?, token, status, workspaces[], capacity{maxConcurrentRuns}, lastSeen}`. Persist in Redis alongside device tokens; follow the `addDeviceToken`/`hasDeviceToken` pattern in [redis-store.js:117](orchestrator/src/redis-store.js#L117) and [auth.js:58-62](orchestrator/src/auth.js#L58-L62). Seed `ct110` as an `external` node from env (`HARNESS_NODES=ct110:https://harness.lan.easybutt0n.ai`, token from `mcp-tools.env`-style env, never committed).
- `orchestrator/src/harness-manager.js` (new) — install/status/uninstall for `managed` nodes. Install = send a new `harness_install` command over the existing device WS carrying image ref + generated web token + config env; the agent runs `docker compose up -d`. Reuse `DeviceManager`'s request/response correlation (`device-manager.js:118-149`) rather than a second transport.
- `device-agent/src/harness.js` (new) — handles `harness_install` / `harness_status` / `harness_uninstall` / `harness_http`. `harness_http` is the **proxy**: forwards `{method, path, headers, body}` to `http://127.0.0.1:<mapped port>/api/…` and returns the response, so the harness API is never exposed on the network. Register in the message switch in [device-agent/src/index.js](device-agent/src/index.js). Bodies are chunked or size-capped — the harness caps at 1 MiB in, but artifacts/snapshots come back large.
- **Windows devices**: install requires Docker Desktop / WSL2 backend. Agent reports `dockerAvailable` in its `register` caps (`device-agent/src/index.js:44-53`); a device without it is `install_unsupported` and placement skips it.

## Work item 3 — fleet run API (repo: `cognizioware-hydra`)

New routes in [orchestrator/src/index.js](orchestrator/src/index.js), alongside `/devices` and `/instances`, all under `requireApiKey`:

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/harness/nodes` | list / register node (`managed` triggers install, `external` just records) |
| GET/DELETE | `/harness/nodes/:nodeId` | node detail (health, run count, capacity) / deregister |
| GET | `/harness/nodes/:nodeId/workspaces` | repos/working trees available on that node |
| GET | `/fleet/runs` | **aggregated across all nodes** — the overseer's single pane: `{nodeId, runId, repo, status, round, activeRole, gates[], lastEventAt}` |
| POST | `/fleet/runs` | create a run with **placement**: `{repo, task, model, roles, maxRounds, nodeId?}`; scheduler picks the node when `nodeId` is absent |
| GET | `/fleet/runs/:nodeId/:runId/snapshot` | proxied `GET /api/runs/{id}/snapshot` |
| POST | `/fleet/runs/:nodeId/:runId/instructions` | proxied pacing injection (playbook Step 1) |
| POST | `/fleet/runs/:nodeId/:runId/gates/:aid/resolve` | proxied approval resolve (Step 5) |
| POST | `/fleet/runs/:nodeId/:runId/{stop,resume}` | proxied lifecycle (Step 3 config cycle) |
| POST | `/fleet/runs/:nodeId/:runId/migrate` | **infra**: successor run on another node (Step 6) |
| POST | `/fleet/offload` | **infra**: rebalance queued/pending work off a saturated node |
| POST | `/harness/nodes/:nodeId/restart` | **infra**: restart the `lh-harness-node` container (managed nodes; `external` → 501). Refuses while non-terminal runs exist unless `force: true` + `rationale`; workers/runs survive via `POST /resume {"mode":"continue"}` after the container is back (audited progress persists on the runs volume) |
| POST | `/fleet/terminals/:deviceId/:sessionId/kill` | **infra**: destroy a stuck PTY session (wraps existing `DELETE /devices/:id/sessions/:sid`), recording `rationale` |
| POST | `/fleet/terminals/:deviceId/:sessionId/restart` | **infra**: kill + recreate a session with the same shell/cols/rows (composes existing `destroySession` + `createSession` in [device-manager.js:118-149](orchestrator/src/device-manager.js#L118-L149)) |
| GET | `/fleet/capacity` | per-node health + run counts + `throttled`/`gating` from device heartbeats |

Proxy layer `orchestrator/src/harness-client.js` (new) — one function that, given a nodeId, routes either through `harness_http` over WS (`managed`) or `fetch` with the node bearer (`external`). **Both access paths satisfied by one abstraction**; the direct escape hatch is `GET /harness/nodes/:id` returning `baseUrl` when the node has one, for out-of-band debugging.

**Doctrine encoded as API invariants** (this is the payoff — the template's rules become enforced, not remembered):
- *One run per working tree*: `POST /fleet/runs` rejects with 409 when the target `(nodeId, workspace)` already has a non-terminal run, unless `allowSharedTree: true` is explicitly passed. Terminal-status set comes from the harness's own `TERMINAL_STATUSES` (`supervisor/lifecycle.py`); `waiting_approval` is **non-terminal**.
- *Top-level `model` is required* — `POST /fleet/runs` validates it before forwarding, because omitting it makes the workspace config leak and the worker die (`cognizioware-how-to.md:75-77`).
- *Never resume a cancelled run blindly* — `/resume` on a `cancelled` run requires a `cancelReasonAck` field.
- Every mutating fleet call takes a `rationale` string, appended to a per-run intervention log (the template's "one line in memory immediately" rule).

## Work item 4 — placement, migration, offload (repo: `cognizioware-hydra`)

`orchestrator/src/fleet-scheduler.js` (new). Deliberately small and rule-based; no queue engine in v1.

- **Placement**: filter nodes to those hosting `repo` and under `maxConcurrentRuns`, excluding devices reporting `gating` (from `device-agent/src/health.js:38-40`). Order: explicit `nodeId` → node already holding that repo's checkout → **CT110 (primary default)** → other managed nodes → **CT110 (fallback)** when everything else is saturated or unhealthy. Records the reason on the run.
- **Migrate** (`POST /fleet/runs/:node/:run/migrate`) — implements the template's Step 6 successor pattern as code:
  1. Snapshot predecessor; extract audited state (commit ids, round count, surviving uncommitted edits).
  2. `POST /stop` on the predecessor **before** the successor's first executor round — two live workers in one tree collide. Stop is the reliable fallback when a gate-resolve errors.
  3. Create the successor on the target node with task text prefixed `CONTINUATION RUN (successor to <id> — do not restart from scratch)` + the inherited-state block + all original constraints, optionally swapping the executor seat (e.g. `qwen3.8` → `kimi-k2.7-code:cloud`).
  4. Link `successorOf`/`succeededBy` in the registry so `/fleet/runs` shows the chain and monitors can re-point.
- **Offload** (`POST /fleet/offload {fromNode, count?}`) — for runs not yet started, re-place; for running work, migrate via the path above. Capacity routing follows the template: local-GPU executors for the runs that justify it, cloud executors (`kimi-k2.7-code:cloud`) for backfill/overflow so runs don't starve each other.
- **In scope (non-power infra actions)**: terminal/session kill + restart, harness-container restart, worker stop/resume cycling — everything the playbook needs short of touching device power. The device agent gains a `harness_restart` command (`docker compose restart` on the node container) alongside `harness_install`/`harness_status`.
- **Explicitly out of scope for v1**: device reboot/shutdown, host-level service restarts (hydra-agent itself, runners), deploy windows. The API returns 501 `infra_action_not_enabled` for those paths so the surface is discoverable and the boundary is explicit — and no tier can reboot the host that carries the overseer/orchestrator, per the template.

## Work item 5 — MCP tools (repo: `cognizioware-hydra`)

Extend [orchestrator/src/mcp.js](orchestrator/src/mcp.js) — same stateless `StreamableHTTPServerTransport` at `POST /mcp`, same `HYDRA_MCP_TOKEN || HYDRA_API_KEY` bearer ([mcp.js:6-13](orchestrator/src/mcp.js#L6-L13)). Add to the existing 7 terminal tools:

`list_harness_nodes`, `install_harness_node`, `restart_harness_node`, `list_fleet_runs`, `create_fleet_run`, `get_run_snapshot`, `send_run_instructions`, `resolve_run_gate`, `stop_run`, `resume_run`, `migrate_run`, `offload_node`, `kill_terminal`, `restart_terminal`, `get_fleet_capacity`.

Plus MCP **prompts** (joining `run-remote-command` / `check-fleet-health` / `drive-interactive-cli` at [mcp.js:159-190](orchestrator/src/mcp.js#L159-L190)) that ship the doctrine to the overseer at call time:
- `overseer-briefing` — the tier responsibility matrix and escalation ladder.
- `run-management-playbook` — Steps 0-7, told as "try the cheapest remedy first, declare your escalation point before you reach it".
- `assign-repo-session` — the fill-in-the-⟨⟩ assignment directive from the template.

Tool descriptions must carry the hard-won gotchas inline (snapshot polling cadence, `waiting_approval` non-terminal, "accepted ≠ applied — confirm consumption in the next round's plan text").

## Work item 6 — web UX parity: every API/MCP action visible per session (repo: `cognizioware-hydra`)

Everything the fleet API/MCP does must be observable in the Hydra web UI ([orchestrator/public/index.html](orchestrator/public/index.html)) — no invisible control paths.

- **Per-session/per-run activity feed**: every mutating call (create/stop/resume/migrate/offload, instruction, gate resolve, terminal kill/restart, node install/restart) is appended to the intervention log from Work item 3 with `{ts, actor (API key label vs MCP client), action, target, rationale, result}`, and stored in Redis per target. New authenticated endpoints `GET /fleet/runs/:nodeId/:runId/activity` and `GET /devices/:deviceId/sessions/:sessionId/activity` serve it.
- **UI additions** to the existing harness page:
  - A **Fleet** tab: nodes with health/capacity, aggregated runs table (`/fleet/runs`), per-run drawer showing snapshot summary, gates awaiting resolution, and the activity feed — MCP-initiated actions render identically to REST/UI-initiated ones.
  - On each **terminal session card** (existing device-session view): an activity strip showing input sent via API/MCP (`send_input`, plugin actions), kills/restarts with rationale, and who did it. The existing per-second frame stream already shows the *effect*; this adds the *cause*.
- **Transport**: authenticated polling of the activity endpoints in v1 (the `/live` WS is unauthenticated at handshake — see Risks — so fleet/activity events are NOT broadcast there). The UI polls at 2-5s, same pattern the page already uses for screens.
- MCP calls are tagged with the client identity from the bearer used, so the UI can distinguish overseer-MCP actions from human-UI actions.

## Work item 7 — hivemind memory layer (chat.easybutt0n.ai + harness sessions → pgvector RAG)

Confirmed feasible; everything needed already exists in the infra. Grounding facts:
- Open WebUI (`/opt/fleet-chat` on corsairai300) stores sessions in SQLite `webui.db` (`chat`, `folder`, `user` tables); **no** pipelines/functions/webhooks configured today. mcpo bridges the LiteLLM gateway with `x-mcp-servers` scoping (full 769-tool aggregate breaks mcpo's OpenAPI generation).
- The only proven pgvector instance is **CT103 `agent-db`** (`pgvector/pgvector:pg16`, 192.168.21.154), already holding `hivemind.contact_memories` with `embedding vector(768)` (migration `westhivecapital-hivemind/hive-mind/migrations/013-contact-memory.sql`).
- The stack-standard embedder is **`nomic-embed-text-v2-moe:latest` (768-dim)** via LiteLLM `POST /v1/embeddings` — `hive-mind/scout/contact_memory.py:33-62` is the copy-this writer shape (LiteLLM base, embed, upsert with content_hash dedupe, `<=>` cosine recall).
- Global LiteLLM callbacks are **prohibited** (multi-tenant Langfuse leak, 2026-06-27 incident); the sanctioned pattern is per-key `metadata.logging[]` (already live on the `lh-harness` key with `x-litellm-tags: lh-run/<id>,round_N,<role>`).
- The gateway's `postgres_mcp_*` servers are read-only — a memory **writer** must be a new MCP server.

### 7a — chat.easybutt0n.ai gets Hydra fleet access
Add `hydrafleet` (and existing `hydra`) to the mcpo backend's `x-mcp-servers` header in `/opt/fleet-chat/mcpo.config.json`, using a virtual key scoped `object_permission.mcp_access_groups: ["fleet-runners", ...]` — scope by **group, never server ID** (IDs regenerate; ID-scoped keys go deny-all). Chat users then drive Hydra exactly as an API user can.

### 7b — memory store
New schema `hivemind_sessions` in CT103 `agent-db` (mirror migration-013 shape):
`session_memories(id, source CHECK('openwebui','lh-harness','overseer'), source_ref, session_id, user_name, device, repo, branch, folder, tier CHECK('overseer','repo-session','run') NULL, occurred_at, text, content_hash, embedding vector(768), metadata JSONB, UNIQUE(source, session_id, content_hash))` + an idempotent upsert function. No ivfflat index until the corpus is real (per hivemind precedent).

### 7c — capture pipelines (proper pipelines, not Langfuse-derived)
- **Chat**: first Open WebUI **Filter Function on the outlet hook** posts each completed turn (chat id, user, folder, model, messages) to the memory MCP. Fallback/backfill: a poller reading `webui.db` from the `fleet-chat_openwebui_data` volume. Repo/branch/device don't exist in Open WebUI — derive device from which runner/Ollama served the turn, and repo/branch from tool-call arguments when present; else NULL.
- **Harness runs (all hierarchy tiers)**: an ingest worker per harness node walks `runs-root/<run-id>/lh_harness/.../events.jsonl` + round artifacts and the Hydra fleet activity log (Work item 6), chunking per round: task text, manager plan, auditor verdict, gate resolutions with rationale. Tier metadata comes from the fleet API: runs carry `nodeId/repo/workspace`; overseer/repo-session attribution comes from the activity log's `actor` field. This captures the whole Paxton→overseer→repo-session→run tree as linked memories (`metadata.successorOf` preserved for migration chains).
- **Langfuse's role**: observability + supplementary ETL source only. Keep per-key logging on the chat and lh-harness keys so traces stay queryable, but do NOT build the memory pipeline on it: it is Langfuse *cloud* (external SaaS, per-key opt-in only), traces are turn-level LLM I/O without repo/branch/device/tier semantics unless separately tagged, and it has no vector recall. High-level stop, not the pipeline — exactly as suspected.

### 7d — memory MCP server (`memory` alias, undashed)
New `infrastructure/docker/memory-mcp/` in `cognizioware-mcp-tools` following the ssh-mcp pattern (Node 20 + TS, StreamableHTTP stateless, `/health`, bearer). Tools: `remember_session` (embed via LiteLLM + upsert), `recall` (cosine top-k with source/repo/user/tier filters), `get_session`, `list_recent`. Register in `litellm-config.yaml` `mcp_servers` with `access_groups: ["hive-mind","repo-tools"]` and alias `memory` (verify no dash-prefix collision). Add it to the mcpo `x-mcp-servers` list so chat can recall its own hivemind.

## Work item 8 — gateway registration + skill

- `litellm-config.yaml` — `hydra_mcp` is already registered at lines 243-249 with `access_groups: ["repo-tools","dev-tools"]`; the new tools inherit it. **But**: these tools create runs and move work across machines, closer in blast radius to `run_command` than to `read_screen`. Follow the precedent set for the fleet runners at lines 268-290 and add a second narrow entry `hydra_fleet_mcp` with `access_groups: ["fleet-runners"]`, so only fleet-scoped keys get the mutating surface. Alias must stay undashed (`hydrafleet`) — LiteLLM splits `tools/call` names on the first dash.
- Update `~/.claude/skills/remote-pc/SKILL.md` Hydra section (lines 167-194) with the fleet-run surface, and `LongHorizon-Harness/cognizioware-how-to.md` with a "Docker node" deployment alongside the WSL2 and CT110 sections.
- Rewrite `tasks/TEMPLATE-overseer-hierarchy.md`'s addressing section: `claude --resume <UUID> -p` stays for talking to *sessions*, but run monitoring/creation moves to the Hydra fleet API. Keep the playbook prose — it is what the MCP prompts serve.

---

## Original infrastructure implementation sequence (historical)

1. Item 1 (image) — independently testable, unblocks everything.
2. Item 2 (registry + agent proxy) with **CT110 registered as `external` first** — proves the proxy abstraction and the fleet API against a live harness before any install code runs.
3. Item 3 (fleet API) against CT110 only.
4. Item 2's `managed` install path on one device (htpc01, Linux, simplest Docker story).
5. Item 4 (scheduler/migrate/offload).
6. Item 5 (MCP) + Item 6 (web UX parity) together — the activity log lands with the first mutating endpoints in item 3, the UI reads it here.
7. Item 7 (hivemind memory): 7b schema → 7d memory MCP → 7c harness ingest → 7c chat filter → 7a chat Hydra access. The memory MCP is useful standalone before any capture pipeline lands.
8. Item 8 (gateway registration + docs). Note: registering new MCP servers restarts the LiteLLM gateway, which drops mcpo/Open WebUI — batch into a deploy window; likewise recreating the Hydra orchestrator drops chat.easybutt0n.ai (the cloudflared tunnel shares its netns).

## Verification

- **Image**: `docker compose -f docker/compose.node.yml up`; `curl -H "Authorization: Bearer $TOKEN" localhost:8799/api/meta` returns capabilities + the LiteLLM-discovered model list. Confirms `ANTHROPIC_BASE_URL` discovery works from inside the container.
- **External node**: register CT110, `GET /fleet/runs` lists its live runs; `GET /fleet/runs/ct110/<id>/snapshot` matches a direct call to `harness.lan.easybutt0n.ai`. Byte-compare the two.
- **Proxy path**: install on htpc01 via `POST /harness/nodes`; verify the harness port is **not** reachable from another host (`curl 192.168.21.148:8799` must fail) while `GET /fleet/runs/htpc01/…` succeeds.
- **End-to-end run**: `POST /fleet/runs` with no `nodeId` on a small repo → lands on CT110 per the primary rule → poll to completion → inject a pacing instruction mid-run and confirm consumption appears in the next round's plan text.
- **Invariants**: second run on the same `(node, workspace)` returns 409; a create without top-level `model` is rejected before dispatch.
- **Migrate**: start a run on htpc01, `POST …/migrate` to CT110 with an executor swap; assert predecessor reaches a terminal status *before* the successor's first executor round, and that the successor task text carries the inherited-state block.
- **Terminal kill/restart**: create a device session, wedge it (`cat` with no input), `POST /fleet/terminals/…/kill` → session gone from `GET /devices/:id`; `…/restart` → new sessionId, same shell/dims, fresh frames streaming within ~2s. Confirm the intervention log recorded the rationale.
- **Harness-container restart**: with an active run, `POST /harness/nodes/htpc01/restart` without `force` → 409 listing the live runs; stop the run, restart → container healthy, `GET /api/meta` (proxied) responds, then `resume {"mode":"continue"}` on the stopped run picks up its audited progress. Verify no test path can reach device power: reboot/shutdown endpoints return 501 on every node kind.
- **Hivemind**: `remember_session` a synthetic chat turn and a real harness round via the memory MCP; `recall` with a repo filter returns it with cosine score; verify the row in CT103 `agent-db` has a 768-dim embedding and dedupe holds on re-ingest (same content_hash → no duplicate). Then from chat.easybutt0n.ai: call a Hydra fleet tool through mcpo, and `recall` a prior session.
- **UX parity**: perform each action once via MCP and once via REST (send_input, kill, restart, create run, resolve gate) and confirm every one appears in the session/run activity feed in the web UI with actor + rationale; no mutating path may leave the feed empty.
- **MCP**: `node design/mcp-eval.js` extended with the new tools; then through the gateway — `POST /mcp/` with `x-mcp-servers: hydra_fleet_mcp` and a fleet-scoped key lists the new tools, while a `dev-tools`-only key does not.
- **Regression**: `npm test` in `orchestrator/` (node --test, ioredis-mock + testcontainers) and `pytest` in `LongHorizon-Harness` — 25 test modules including `tests/webapi/` and `tests/supervisor/` must stay green.

## Risks

- **Secrets**: `.mcp.json`, `infrastructure/mcp-tools.env`, and `remote-pc/SKILL.md` already carry live bearer keys in plaintext/git. Node tokens generated at install must go to Redis and env only — not into any committed file. Worth a separate cleanup pass; flagging, not fixing here.
- **Proxy body size**: harness artifacts (8 MiB cap) and trajectories exceed comfortable WS frame sizes. Cap proxied responses and expose artifacts via a streaming path or a size-limited 413 rather than silently truncating.
- **Docker-in-WSL2 on Windows devices** is the least-proven leg. htpc01 first; treat Windows managed nodes as a follow-on if the caps probe proves unreliable.
- **`/live` WS is unauthenticated at handshake** ([auth.js:33-56](orchestrator/src/auth.js#L33-L56) exempts upgrades). Fleet-run events must not be broadcast there until that is fixed — new events go to authenticated REST polling only in v1.
