# TASK — Cognizioware Guest Launch: Step 1 (land the handoff package, F01 baseline, F03/F04 defect enumeration, P01 spike, decision register)

**Asked by Paxton 2026-09-05 22:15 PT:** "add this another lh-harness task you will manage: design/plans cognizioware-guest-launch-{handoff,plan,backlog}.md — start with the handoff; verified all three file hashes match the originals."
**Overseer role:** manager — worktree, run, review; then Paxton decides the first code slice (the handoff says implementation is NOT authorized by the package).
**Repo:** cogniziocompany/cognizioware-powerplatform, branch `docs/guest-launch-baseline` from origin/develop @ edd45a1.
**Harness workspace (WSL-created, LOCKED):** `/mnt/c/Users/PaxtonTait/source/cognizioware-powerplatform-guest` — the three plan files were copied in untracked (they exist nowhere in git; the user's checkout has them untracked on ci/env-promotion).
**Task text:** `C:\tmp\guest-launch-task.txt` (verbatim in the run).

## Scope (docs only, read-only on source)
1. Commit the package with sha256 of each file in the commit body.
2. F01 baseline record: branches/HEADs/dirty counts/ahead-behind/package versions for the app tree, the nested v2-n8n-orchestrator tree, the ci/env-promotion checkout and mcp-cognizioware; "material differences from the inspected baselines" with file:line on current develop.
3. F03/F04 authorization + redaction defect enumeration with file:line, exploit sketch, fail-closed fix, proving test, backlog id.
4. P01 portal/licensing spike (Power Pages + hosted Checkout vs existing SPA; UNVERIFIED markers), DECISION-REGISTER.md, FIRST-SLICE-PROPOSAL.md sized in days and requiring Paxton's go.

Never touch other checkouts (handoff: "Do not reset, clean, force-checkout, pull over or discard local changes"); no secrets in docs; static inspection is not a runtime test.

## Model trio
manager `kimi-k2.7-code:pool` · executor `kimi-k2.7-code:pool` · auditor `kimi-k3:pool` (API keys `roles` + `max_rounds` 14).

## After completion (overseer)
1. Review: docs-only diff; citations present; no secrets; no implementation claims.
2. Push branch, PR → develop (docs-only, dev-lane no-op), merge.
3. Hand Paxton the DECISION-REGISTER rows and the FIRST-SLICE-PROPOSAL; the first code slice (authorization/redaction + admin shell) becomes its own harness task only after his go.

## Completed (2026-09-05 23:20 PT)
Run 925b0511 done: 6 commits, 8 files +1928 (package landed with sha256; F01 baseline 317 lines; F03/F04 12 defects with file:line, fixes, tests; P01 spike
with 9 UNVERIFIED markers; DECISION-REGISTER 14 rows D01–D14; FIRST-SLICE-PROPOSAL 6–9 eng days + 2–3 review days). Gate resolved "stop". Merged to develop as
pp #59 (docs-only; dev lane no-op). Decision register D01–D14 is on Paxton before the first code slice is launched.
