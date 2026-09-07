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
