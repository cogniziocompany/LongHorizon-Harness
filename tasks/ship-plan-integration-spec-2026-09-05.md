# TASK — cognizioware-hydra: Revenue-pipeline integration spec (Ship Plan reporting in Hydra ↔ YouTrack via mcp-cognizioware, customer admin + CRM sync, timesheets, RSI loop)

**Asked by Paxton 2026-09-05 23:05–23:25 PT** (verbatim intent, consolidated): create an lh-harness task for cognizioware-hydra; the overseer's Ship
Plan reporting design (artifact 66bed19d) using mcp-cognizioware's YouTrack integration so AI agents and users can comment, do ticket CRUD and
record time sheets, tracked in the right places in YouTrack; related integration with Dynamics; all admin parts of the Power Platform customers go
through the mcp-cognizioware API (or directly) for new contacts and accounts, synced with the company CRM sales department (accounts, contacts,
opportunities, leads); tie the timesheet YouTrack integration to the other end of the business flow; "this all should support all the docs in
here [cognizioware-powerplatform/design/plans] as our revenue pipeline; all repos are in support of this mission-critical company task"; "our RSI
loop planned for a future phase is to push and scale to achieve those variable revenue metrics"; "add this main business outlay".
**Overseer role:** manager — worktree, run, review, PR → main (hydra), then launch the P0 build tasks the spec produces (each its own managed run).
**Repo / branch:** cogniziocompany/cognizioware-hydra, `docs/ship-plan-integration-spec` from origin/main @ 1d83c00.
**Harness workspace (WSL-created, LOCKED):** `/mnt/c/Users/PaxtonTait/source/cognizioware-hydra-shipplan` — inputs seeded untracked:
`design/inputs/youtrack-integration-gap-analysis.md` (from mcp-cognizioware) and `design/inputs/ship-plan.html` (the live artifact; not to be committed).
**Harness run:** `20260906T061807Z_c9ab282f` (WSL, `:pool` trio, 14 rounds). **Task text:** `C:\tmp\shipplan-spec-task.txt` (verbatim).

## Inputs the run reads (all read-only)
- YouTrack gap analysis (BillingService = backbone; Phase 1 being built in parallel by run fc60d514 — design against its surface).
- cognizioware-powerplatform `design/plans/*`: guest-launch handoff/plan/backlog (now merged to develop with F01/F03-F04/P01/decision register via #59),
  powerplatform-greenfield-factory-system-design.md, file-to-eval-requirements-plan.md, cognizioware-2027-commercialization-and-market-model.xlsx
  (the variable revenue metrics / business outlay — quoted by sheet/cell), Cognizioware-Orchestration-Comparison-Handoff.docx.
- The Ship Plan artifact structure; LongHorizon-Harness task records; Hydra orchestrator src + web UI; mcp-cognizioware lead/contact controllers,
  D365CeService (to be replaced, never reused), StripeService, admin SPA.

## Deliverables (design/ship-plan-integration/)
1. `00-revenue-pipeline-overview.md` — business outlay + end-to-end flow lead → CRM → provisioning/billing → delivery → YouTrack → invoicing → metrics → RSI loop (future, guardrails, not built); repo responsibility matrix; invariants.
2. `10-ship-plan-reporting-hydra.md` — Hydra Ship Plan module (data model bound to YouTrack issues, REST + `/fleet-mcp` tools wrapping mcp-cognizioware, web page mirroring the artifact, conventions, permissions, event flow, failure modes).
3. `20-customer-admin-and-crm-sync.md` — customer admin API through mcp-cognizioware, identity mapping, bidirectional Dynamics 365 Sales sync rules, `ID365SalesService` replacing D365CeService, Lindy lead intake disposition, timesheet tie-in.
4. `30-backlog.md` + decision register (OPEN rows for Paxton) + `40-harness-tasks/` (ready-to-launch task texts for the P0 build items).

## Model trio
manager `kimi-k2.7-code:pool` · executor `kimi-k2.7-code:pool` · auditor `kimi-k3:pool`.

## After completion (overseer)
1. Review: docs-only, citations, no secrets, xlsx metrics cited or UNREAD.
2. PR → main (hydra), merge; hand Paxton the decision register; launch the P0 build tasks in dependency order (after YouTrack Phase 1 lands).

## Completed (2026-09-06 00:55 PT)
Run c9ab282f done in 6 rounds: 4 commits, 12 files +1053 (00 overview, 10 Hydra Ship Plan design 307 lines, 20 customer admin + CRM sync 421 lines,
30 backlog + decision register R01–R11 OPEN, 40 harness task texts RP-01..RP-08 ≈ 2.5–3 KB each). Gap: the workbook was UNREAD in WSL (no openpyxl) —
the overseer extracted the Start Here decision metrics, 2027 Inputs and Monthly Targets with openpyxl on Windows and appended them (86f3501).
Merged to hydra main as PR #8. Next: Paxton answers R01–R11; then RP-01 (YouTrack Phase 2 Hydra events bridge) launches after YouTrack Phase 1
(fc60d514) lands; RP-02/03 (Hydra Ship Plan model+UI, fleet-mcp tools) can start on the docs; RP-04..08 gate on R06–R11.
Headline figures for Paxton: 2027 exit Microsoft subscription ARR target $286,560; 2027 revenue excl. assurance $221,890; guest price $39/seat/month;
peak delivery 217 h/month vs 62.5 h reserved; core program contribution −$47,030 (per the workbook, planning inputs not bookings).
