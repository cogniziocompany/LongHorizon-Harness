# TASKS — Cognizioware Guest Launch: first authorized build slices (run on CT110, not the dev PC)

**Trigger (Paxton 2026-09-06 09:40 PT):** resolved the decision register in-place (Status column: D01–D03 TOP PRIORITY GO, D08 HIGH, D11 medium,
D06/D07/D09/D12/D13/D14 priority, D04/D05/D10 low; every proposed default accepted) and said "ready for another lh-harness task to start on these
different items, create as many as you need, not on this machine". Register resolutions committed to develop as pp #70.
**Harness node:** CT110 (corsairai300), `http://192.168.21.168:8799`. **Overseer role:** manager — review, PR, dev-lane deploy, QA gate, promotion.

## Run A — guest slice 1 (app: F03/F04 fail-closed fixes + Claims365-pattern admin shell)
- Repo: cognizioware-powerplatform, branch `feat/guest-slice-1` from origin/develop @ f7bb19c, workspace `/home/harness/work/cognizioware-powerplatform` (cloned on CT110 today).
- Spec: `design/plans/guest-launch/FIRST-SLICE-PROPOSAL.md` (12 defects 1.1–1.11, shell 2.1–2.5, engineering order §4), F03-F04 defect list, backlog F03/F04/A01/A02, plan invariants.
- Run: `20260906T164532Z_38ef0e49` (16 rounds). Task text `C:\tmp\guest-slice1-task.txt`.
- Rules: fail closed; no Stripe/billing/pricing/meter/scheduled-task changes; no secrets; no new deps; runtime proofs (Dataverse/MSAL/n8n) are NOT available on CT110 → tests + fakes, then the dev lane + QA gate prove it.

## Run B — billing slice 1 (mcp-cognizioware: single Stripe writer, meter isolation, comparison path, hosted Checkout, nightly close)
- Repo: mcp-cognizioware, branch `feat/guest-billing-slice-1` from origin/develop @ d26ccc5 (contains YouTrack Phase 1), workspace `/home/harness/work/mcp-cognizioware`.
- Spec: guest-launch plan (invariants 1–12, ledger contract, reconciliation + settlement state machine), backlog P02/P03/U01/U03/B01–B07, register D06/D07/D08/D09/D13, README commercial decisions (BYOK zero surcharge; managed ×1.20).
- Run: `20260906T164700Z_eb012adc` (16 rounds). Task text `C:\tmp\guest-billing-slice1-task.txt`.
- Rules: Stripe TEST MODE enforced in code; meter observation-only with isolation tests; kill switches default off; no live charge path; no YouTrack changes; no secrets.

## Model trio (both)
manager `kimi-k2.7-code:pool` · executor `kimi-k2.7-code:pool` · auditor `kimi-k3:pool`.

## After completion (overseer)
- A: review defect→commit table + tests; PR → develop; dev lane (CT100 .163) applies migrations at boot; QA gate `ce`/greenfield + web-ux target; then promotion (runs now execute on CT210, not the PC).
- B: review invariants evidence; PR → develop; dev billing lane (CT100 :20252) with Stripe TEST keys + the objects listed in the handback; billing QA gate; uat via release fast-forward; prod only after the plan's seven successful test-mode closes.
- Slice 2 candidates after both land: P03 provisioning (Power Pages PAYG per D03), U02 LiteLLM-first capture, B06 dunning, A03/A04 admin telemetry.

## Run A completed (2026-09-06 15:55 PT)
Guest slice 1 delivered across two kimi runs (38ef0e49 slices 1–3 before the Ollama quota outage; 84bec9d9 slices 4–6 after): 6 commits, 41 files
+3382/−271, 5 backend + 4 frontend test files, SLICE-1-HANDBACK.md with the defect→commit table (11/12 fixed; #9 System Customizer requirement →
P03), go/no-go evidence and the NOT-proven-at-runtime list (Dataverse, MSAL, Stripe, n8n, billing adapter, LiteLLM). Merged to develop as pp #73;
dev lane deploying (migrations environment_grants + proposed_role_changes apply at app start). Next: QA gate ce/greenfield + web-ux target on the dev
lane, then promotion (pipeline now runs on CT210). The qwen3.8 attempts in between (86cd5320, b14906b4, 4ca2c788, 4a64dc70) produced no commits and
are documented in docs/handoffs/local-executor-and-cloud-capacity-handoff-2026-09-06.md.

## Run B completed (2026-09-06 16:40 PT)
Billing slice 1 (eb012adc, kimi, resumed after the quota window): 7 commits, 56 files +16217/−114; dotnet build 0 errors; tests 259/0 (9 DataIntegrity
need Postgres → NOT RUN). Invariants 1–12 evidence table ✔ (test mode enforced, deterministic ids, observation-only meter, BYOK zero, markup once,
non-authoritative comparison, kill switches default off, 03:15 LA close, hosted Checkout + webhook inbox, dedupe, repair queue, lost-response recovery,
discrepancy holds). Merged to develop as mcp-cognizioware #71 → dev billing lane. Overseer created the Stripe TEST objects (guest product, $39/mo base
price, observation-only $0 metered price, billing meter `cognizioware_guest_tokens`) with the dev lane's sk_test key and wrote the Stripe__Guest*/Billing__*
keys into /opt/mcp-cognizioware-dev/.env (kill switches off, live mode disallowed). Container restart with the new env after the lane deploy.

## Correction (2026-09-06 16:55 PT)
The dev lane did NOT receive guest slice 1 when #73 merged: the CI deploy job failed on both develop runs after #75 (34066670957, 34066962672) with
`syntax error near unexpected token '{'` — #75 left the PowerShell `if ($LASTEXITCODE …)` lines and a `$env:GITHUB_OUTPUT` write inside the bash steps.
The .163 lane was still on sha-f7bb19c, so the green CE QA gate at 16:34 (score 100, 4/4 suites, greenfield) tested the OLD build and proves nothing
about slice 1. Fix: pp #76 (drops the pwsh remnants in ci.yml + promote.yml). Sequence now: develop CI → rollout to .163 → verify migrations 029/030 →
re-run the CE QA gate → pinned-sha promotion.

## Dev lane really deployed (2026-09-06 16:43 PT)
pp #76 merged → develop CI 34067523857 build/image/deploy all green from ct210-pp; .163 lane now runs app:sha-476811f (healthy), `_migrations` shows
environment_grants + proposed_role_changes tables present; app log "Migrations complete". CE QA gate re-dispatched for sha 476811f (qa run 34067710993).
Billing lane: mcp-cognizioware had NO self-hosted runner left after the PC deregistration (its deploy job also needs `self-hosted,lan-deploy`) — registered
`ct210-billing` on CT210 (service actions.runner.cogniziocompany-mcp-cognizioware.ct210-billing), verified runner→CT100 ssh with the proxmox key, and
converted its rollout step to bash (mcp-cognizioware #72). Develop CI 34067654276 is deploying sha b9b06b7 (slice 1 + #72); the rollout's
`compose up -d --no-deps app` re-reads .env, so the Stripe__Guest*/Billing__* keys take effect on this deploy.

## Billing dev lane live (2026-09-06 16:52 PT)
mcp-cognizioware develop CI 34067654276 green end-to-end from ct210-billing; billing-dev-lane-app-1 now sha-b9b06b7 (healthy), 10 Stripe__Guest*/Billing__*
keys present in the container, `/api/v1/guest/subscription` → 401 unauthenticated (route wired, auth enforced). Next: billing QA gate on the dev lane;
uat via release fast-forward after the pp promotion settles. pp promotion for 476811f dispatched 16:50 PT (prod auto-approval watcher running).
- 16:49 PT: billing QA gate (target=billing, env=dev, base_url http://192.168.21.153:20252, caller sha b9b06b7) on ct210-qa: score 100, 8/8 admin
  acceptance tests, status `cognizioware-qa/billing-dev` success (qa run 34067904053). Next for billing: mirror the Stripe__Guest*/Billing__*/YouTrack__*
  keys into the uat lane env, then release fast-forward → uat lane; prod stays closed until seven successful test-mode nightly closes (plan invariant).
- 20:10 PT: promotion 34067791009 (pinned 476811f): dev AND uat fully green (deploy, CE QA gate, primary eval gate, eval score gate). Prod deploy
  approved by the overseer at 20:08 PT per Paxton's standing rule (the watcher's own approve call failed on Windows process substitution — fixed in
  promo-guest.sh; approved by hand with a JSON file). Prod deploying now; prod CE gate + prod eval gate follow (expect 15/16 again until the
  webhook-crud fix lands — that gate result is informational for this run, the deploy itself is done once the rollout step passes).
- 21:10 PT: promotion 34067791009 COMPLETED SUCCESS — dev, uat AND prod: deploy + CE QA gate + primary eval gate + eval score gate all green.
  Guest slice 1 (pp #73), the web nav-archive (#67) and the runner/deploy fixes (#71–#76) are in PROD at sha 476811f. Prod eval score gate passed
  (the multi-webhook-crud case that scored 15/16 in run 16 did not block this run — see the webhook task file; the fix run stays queued as
  hardening). Slice 2 candidates (P03 provisioning, U02 LiteLLM-first capture, B06 dunning, A03/A04 admin telemetry) can be planned.
