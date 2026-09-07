# Expert handoff program: Hydra control plane + product evaluation catalog — 2026-09-07

**Source:** docs/handoffs/based-on-this-tasks-template-overseer-hi-fancy-fountain.md (committed 3743866). Paxton: "one task encompassing our entire infra and the revenue repo powerplatform; apply everything from the other expert." The handoff itself assigns the work to the owner queues, so it is queued as five harness tasks, each carrying the handoff verbatim:
- 14a pp product eval catalog (cases #1-#N, full-build driver, strict >90 promotion, ownership/retention/cleanup, EvalsPanel feed, acceptance scenarios) - 12 rounds, pp-evalfix workspace.
- 14b qa full-case contract v2 + scoring + evidence + Mission Control reuse - 8 rounds.
- 14c hydra control plane: reconcile items 2-6 then build gaps (external CT110 node + links [absorbs 13b], fleet runs + queue panel, doctrine invariants, MCP tools + prompts, UX parity/activity feed, placement/migrate/offload) - 10 rounds.
- 14d harness infra hardening + docker node image + disposable infra acceptance (after task 12) - 8 rounds.
- 14e hivemind memory MCP + ingest + chat outlet filter + gateway registration - 8 rounds.
**Namespace (Paxton, final 17:55 PT):** hard-coded Cognizioware publisher (`cgz`, reused from the guest-access publisher-ensure step) + a race-safe NUMERIC attempt suffix per (org, case) reserved in the ownership ledger and verified against Dataverse before create: `cgz_<CaseKey>_a<NN>` for the solution, `cgz_<name>_a<NN>` for created components; numbers never reused; GUID/component ids stay the cleanup keys. Earlier GUID-slug variants withdrawn.
**Sequencing:** 12 -> 14d; 14c after 12's contract; 14a/14b/14e independent. Deploys: harness release at idle, Hydra on corsairai300, lanes for pp/qa/mcp-tools.
