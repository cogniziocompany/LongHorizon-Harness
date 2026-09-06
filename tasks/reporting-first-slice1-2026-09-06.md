# TASK — cognizioware-hydra: Reporting-First Intelligence slice 1 (RP-00 durable read model + RP-02a read-only LiteLLM/harness reporting screen)

**Asked by Paxton 2026-09-06 09:05 PT:** "run a lh-harness task on applying this to our current code base and infra … and manage it", pointing at
the reporting-first package saved (SHA-256 verified by its author) in `cognizioware-powerplatform/design/plans`:
`cognizioware-reporting-first-council.md`, `cognizioware-litellm-reporting-data-catalog.md`, `cognizioware-reporting-first-blueprint.md`,
`cognizioware-harness-current-code-review.md`. (The package's "Architecture and Frontend Specification" and the three model reviews were not
copied there; the four files above are the authoritative set.) The package's own closing question — "implement the first read-only LiteLLM
reporting screen in Hydra next?" — is what this run does.
**Overseer role:** manager — worktree, run, review, PR → hydra main, deploy to the orchestrator on corsairai300, live validation against CT202 LiteLLM.
**Repo / branch:** cogniziocompany/cognizioware-hydra, `feat/reporting-first-slice-1` from origin/main @ 57da83a (includes the ship-plan spec, PR #8).
**Harness workspace (WSL-created, LOCKED):** `/mnt/c/Users/PaxtonTait/source/cognizioware-hydra-reporting` — the four docs seeded untracked in
`design/inputs/reporting-first/` (committed by the run in slice 1).
**Task text:** `C:\tmp\reporting-slice1-task.txt` (verbatim).

## Infra facts verified before launch (2026-09-06 09:00–09:30 PT)
- LiteLLM on CT202 is 1.93.0 with Postgres; `GET /spend/logs` needs a date window (unbounded call times out >25 s); windowed call answers in 0.2 s
  (per-day summary; `summarize=false` for rows); `GET /metrics` → 404 (Prometheus callback not enabled); admin key required, NOT given to the run.
- Hydra stack has Redis only (ioredis unused at runtime), no Postgres → slice adds an optional `pg` read model (`REPORTING_DATABASE_URL`), target
  the CT103 agent-db Postgres at deploy; fixture/in-memory mode otherwise with `store: disconnected` on the Data Health bar.
- Langfuse and Prometheus fields are UNAVAILABLE with reasons in this slice; harness facts come from Hydra's harness-client (live) or fixtures.
- Identity/tenant crosswalk (RP-09) does not exist → the slice is internal-only behind Hydra operator auth; no customer payloads, no economics beyond cost provenance.

## Slices
1. Land the package + RP-00 schema/migrations (source_registry, llm_attempt, harness_run_fact, source_event dedupe), idempotent upserts, tests.
2. Read-only adapters: LiteLLM date-windowed spend logs → attempts (usage_state, price_mapped, cost_provenance), harness snapshots → run facts by (node, run, epoch), source-health registry; `scripts/reporting-ingest.mjs --once`.
3. Read-only API under Hydra auth: /reporting/health, /overview, /models, /runs(+drilldown), /recommendations (empty shape, nullable youtrack_issue_id).
4. Hydra "Reporting" tab: Data Health bar, AI Operations overview, Model & route table, Harness runs, read-only Recommendations card; fetch errors render disconnected/stale, never healthy.
5. Verification + `design/reporting-first/SLICE-1-HANDBACK.md` with the catalog's acceptance checklist and the UNVERIFIED list; backlog rows RP-00/RP-02a.

## Model trio
manager `kimi-k2.7-code:pool` · executor `kimi-k2.7-code:pool` · auditor `kimi-k3:pool` (14 rounds).

## After completion (overseer)
1. Review: no write routes, no unbounded spend-log calls, price_mapped=false never zero, no secrets; tests + smoke evidence.
2. PR → main, merge; deploy the orchestrator on corsairai300 (tarball; restore the live .env; batch with any LiteLLM window) with
   REPORTING_DATABASE_URL (CT103 agent-db) + REPORTING_LITELLM_URL/KEY (a dedicated LiteLLM admin-scoped key minted for reporting); run the ingest once;
   validate the live spend-log row schema against the fixture and record deltas in the handback.
3. Next slices: RP-09 identity gate, Langfuse evidence, RP-11 economics, review workflow; YouTrack promotion only under separate authorization.

**Harness run:** `20260906T155348Z_630e3309` (WSL, `:pool` trio, 14 rounds).
