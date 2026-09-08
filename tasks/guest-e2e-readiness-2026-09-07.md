# TASK — Guest launch: full end-to-end test readiness (Stripe TEST card → customer → environments → wizard → design → approve → build), audit + plan

**Asked by Paxton 2026-09-07 02:55 PT:** "when can we do the full powerplatform Stripe test customer with test CC (it is already the customer id we
have), use the meter per night — let's get this done; deep-research with a new lh-harness to audit our current stage … if e-sign isn't done use
the lightweight pdf.js/thumbnail process; save an approval log with the summary (read to the user from chat, or auto-approve) as an entry in each
new solution's Cognizioware env variables; every solution of ours has it; a second solution in the same env reuses the structure, no duplicates."

## Run A (research + plan, docs only)
Workspace CT110 `cognizioware-powerplatform-evalfix` (pp checkout, branch `docs/guest-e2e-readiness-audit`), read access to the billing, hydra-rsi and
overseer task checkouts. Task text `C:\tmp\guest-e2e-readiness-task.txt`; queued as `02-guest-e2e-readiness` (right after the LiteLLM upgrade).
Deliverables: E2E-READINESS-AUDIT (per step: exists / in flight / missing, file:line), E2E-GAP-SLICES (ordered build slices incl. the lightweight
e-sign stamp and the per-solution approval-log env variable with get-or-create reuse), E2E-TIMELINE (the honest "when": dev → uat → prod after
seven test-mode nightly closes), e2e/guest-e2e checklist.json for a future QA-gate `guest` target.

## What already exists (overseer's view, to be verified by the run)
- Billing slice 1 on dev + uat lanes: ledger, tariff (BYOK ×1.0 / managed ×1.20), single Stripe writer, observation-only meter
  `cognizioware_guest_tokens`, hosted Checkout + webhook inbox, nightly settlement (03:15 LA), kill switches OFF; Stripe TEST objects created
  (product, $39/mo base price, $0 metered price, meter) on both lanes' test accounts. RP-14 receiver merged (switches off).
- Guest slice 1 in prod (F03/F04 fail-closed, admin shell, environment_grants + proposed_role_changes tables).
- Env → Session wizard (v2 design) building on CT110 (run b41549c8, paused on quota): publisher/solution/env-variable get-or-create, docs store per
  solution, processing lockout, CGZ codes, approval gate with ladder + reset, extract screen, Brownfield workflow.
- Not started: customer account creation from Checkout → environment provisioning/grant automation; lightweight e-sign stamp; approval-log env
  variable + chat read-back/auto-approve; a `guest` QA-gate target; the meter is observation-only (no charging) by design until seven clean closes.

## After Run A
Build slices become harness tasks in dependency order (billing → pp), each through its lane; Paxton's own items (Stripe dashboard, environment
admin, sign-in proofs) listed in the timeline doc; the first full dev run is scheduled from the timeline's date.

## Progress
- 03:00 PT: task text + queue entry written (kimi trio, 6 rounds); launches when two Ollama keys answer.
- 03:05 PT: queue entry switched to the qwen3.8 trio so the audit starts when the Remote Control run frees the local lane (the kimi pool is at 1 healthy key).
- 03:12 PT: D-B widened per Paxton: a per-solution AUDIT LOG of the whole guide-rail deployment process ("Guide-Rail Ledger" working name; chat read-back "Ledger recap", auto-approve "Green-light rules", lightweight e-sign "Wax seal" — the run proposes final names), stored in cgz_<Name>_GuideRailLedger env variables with rollover + index, reused per environment.
- 2026-09-07 12:05 PT: run 15fa88b8 (all-qwen3.8) blocked at round 4 with zero deliverables: qwen executor timeouts, then the ptait01 outage killed it mid-way, resumed, still nothing produced. Stopped; re-queued as 02b on the kimi pool (8 rounds). Branch docs/guest-e2e-readiness-audit sits clean on develop b9865ed.
- 12:50 PT: kimi run 25706f79 delivered all four docs in ~25 min (2595cba, cbe6a6c, 1bfc175, c8f60fc; 1101 lines; no secrets). Headline: **first full dev run possible 2026-09-11** after slices 1, 4, 3, 2; auditable dev E2E (Wax seal + Solution Passport + nightly trigger) 2026-09-15; UAT 2026-09-18; earliest prod 2026-09-26 (seven test-mode closes). Ten operator prerequisites for Paxton (Stripe test customer bound to a billing row, Stripe__Guest* + backend env on dev, non-prod Dataverse env + roles, environment_grants seed, n8n design-build-sprint on dev, MCP proxy children, Billing__NightlyCloseEnabled=false, approve Wax seal / Solution Passport naming). PR #83 to develop. Next: slices 1-4 as harness tasks.
- 12:55 PT: PR #83 merged to develop (b50202e). Build tasks queued from the gap list: 07-guest-slices-1-4 (guest-onboard + env grants API/UI; kimi, 10 rounds, pp-evalfix workspace) then 08-guest-slices-2-3 (usage reporting call site + SPA Checkout; waits for 1-4 on develop). Slices 5-8 (Wax seal, Solution Passport, nightly trigger, Playwright guest E2E) follow after the first click-through. Task texts C:\tmp\guest-slices-*-task.txt.
- 14:35 PT: run 33e5da6d (slices 1+4) asked how to create the billing user_organizations row (no upsert endpoint in the billing service). Decision: never write the billing DB from pp; contract `POST /api/v1/customers/ensure` (idempotent by email+tenant) - pp side coded now against a fake, billing side queued as 07b-billing-ensure-customer (mcp-cognizioware, kimi). Runs f28bec57 (10-qa-gate) and 93fdd703 (20-billing-prod-lane) died on the prod outage (502 / connection refused) and were resumed.
- 16:00 PT: run 33e5da6d (slices 1+4) complete: b436862 (guest-onboard + ensureCustomer contract shim, prod refusal), 32f550b (migration 031), 9cc1c81 (grant API/UI + session grant check); 13 files +1250/-32; 46 slice tests green; 3 pre-existing failures (admin-recommendations, migrations need Docker) reproduce on develop; frontend vitest not installed in the workspace (lane covers it). Contract doc design/plans/guest-launch/BILLING-CONTRACT-ensure-customer.md is the input for 07b. PR to develop next; 08 (slices 2+3) unblocks once merged.
- 16:05 PT: PR #84 (slices 1+4) merged to develop (046c73f); CI 34165084379 running. 08-guest-slices-2-3 is now eligible (base check sees the merge). Reminder: the dev deploy of these routes happens through the develop promotion lane, which is still red on the eval NO_RESPONSE (task 06b).
- 16:35 PT: develop deploy re-run green after the pp-dev-uat disk cleanup; dev health 200 at http://192.168.21.163:3000. Slices 1+4 are live on dev.
- 19:55 PT: dev lane env (/opt/powerplatform-dev/.env on ptait07 CT100, backup .env.bak-20260907-guest) gained STRIPE_TEST_CUSTOMER_ID and GUEST_TEST_PRINCIPAL_EMAIL (BILLING_API_URL/KEY were already there). The running dev app did not pick them up yet: compose references an image tag not present locally, so a recreate is not possible by hand; the next develop rollout (PR #87 or the next merge) recreates the app with the new env. QA repo secrets for the admin gate set (ADMIN_CENTER_AI_*, LITELLM_MASTER_KEY_UAT/PROD, variable ADMIN_GATE_NOTIFY_TO).
- 20:05 PT: verified on the dev billing lane container: Billing__NightlyCloseEnabled=false (GuestExecutionEnabled / GuestChargingEnabled present, switches off). Paxton confirmed ai-dev03 is in Entra group Ai Agents-Dev. Still his: a non-prod Dataverse env with System Customizer for ai-dev01.

- 2026-09-07 23:55 PT: PR #89 (slices 0/2/3) merged as cbd0e88; dev lane green (second LAN runner ct210-pp2 in place). Seeding run on CT100 in the app container with BACKEND_URL=http://127.0.0.1:3000: environment d30303b8 (org b400f72c, org9130dfc5.crm.dynamics.com) seeded, grant b21a938d for ai-dev01@cognizio.company created, **verifyAuth PASS** (customerId=internal). Dev is ready for the human click-through once the billing ensure-customer endpoint (05h0) lands on the billing dev lane; the SPA checkout proxy and usage reporting are live on dev now.

## Release gate decision (Paxton 2026-09-08 00:20 PT)
The "seven successful test-mode closes before production" rule is procedural (plans only); the only code guard is ProdStartupGuard (Stripe live mode needs Stripe__AllowLiveMode). For the pre-production guest launch on Stripe TEST mode the wait is DROPPED: prod promotion of the guest code is not held for closes. Queue `05h2b-billing-release-gate-setting` (mcp-cognizioware, kimi, 6 rounds, text `C:	mpilling-release-gate-task.txt`) makes it a configurable admin setting (Release.RequiredTestModeCloses default 7, Enforced toggle, DB-persisted with audit rows, admin card at billing.easybutt0n.ai/admin), enforced only when flipping to live Stripe. Ship Plane: guest prod no longer waits on closes.

- 2026-09-08 01:40 PT: billing ensure-customer merged (#76), dev lane green. Promotion blocked: promote-billing.yml was not on main (fixed via #77, legacy push deploy disabled, prod branch created) and the dev lane never posts the cognizioware-qa/billing-dev status (gate dispatched by hand; lane fix folded into 05h3). Promotion re-runs automatically once the gate is green.
