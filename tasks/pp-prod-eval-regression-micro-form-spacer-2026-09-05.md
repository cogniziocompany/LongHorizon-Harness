# TASK — powerplatform prod eval regression: `micro-form-spacer@prod` (and `multi-webhook-crud@prod` stuck running)

**Owner session:** Claude Code `2e684a91-d0c1-47c2-8be4-a1837658ab5b` (cwd `C:\Users\PaxtonTait\source\cognizioware-powerplatform`).
**Overseer role:** filed by the overseer during the 2026-09-05 ship batch; the overseer holds the two blocked PRs (#41, #42) until the
main-branch eval gate is green again. Paxton's call (2026-09-05): wait for the fix, no admin-merge.

## What is red
`.github/workflows/eval-gate.yml` on `main` runs the live eval catalog against prod (`https://powerplatform.easybutt0n.ai`). Since 17:11 PT
2026-09-05 every run reports 14/16 green with:
- `eval:micro-form-spacer@prod` — `outcome=fail regression` (three consecutive runs: 33975003060 at 17:11, rerun 17:27, rerun 17:44)
- `eval:multi-webhook-crud@prod` — `outcome=running regression` (never completes)
The scheduled run at 15:07 PT (33970761281) was green, so the regression window is 15:07–17:11 PT. Nothing was deployed to pp prod in that
window; the mcp-tools lane restarted the LiteLLM router on CT204 (UAT) at 17:2x and 17:4x but the prod router on CT202 was not touched until
#63 deployed at ~17:50, after the first two failures.

## Case definitions
- `e2e/evals/catalog.mjs:147` — id `micro-form-spacer`, tool `xrm-dataverse_form_update_formxml`, prompt "Add a spacer to the Asset main form."
- `e2e/evals/README.md:29` describes it.
- `multi-webhook-crud` — same catalog; find why the harness marks it `running` (timeout? webhook target unreachable?).

## Deliverables
1. Reproduce both cases against prod from the LAN runner or locally (`e2e/evals`), capture the actual tool call + Dataverse response.
2. Root cause: is it the Dataverse org (form locked, solution layer, permission), the `xrm-dataverse` MCP on CT202 (LiteLLM MCP gateway), or the
   eval harness itself (stale baseline / timeout)? Check the CT202 `cognizioware-xrm-mcp` container logs around 17:11 and 17:44 PT.
3. Fix at the right layer (repo code/eval only; no prod Dataverse mutations beyond what the eval itself performs; no CT202 changes — hand those
   to the overseer), or, if the eval is wrong, correct the case and say why.
4. Prove: `eval-gate.yml` green on `main` (dispatch or scheduled), 16/16.
5. Then tell the overseer so #41 (scheduled trms-qa-eval npm ci fix) and #42 (register promotion workflows on main) can merge.

## Launch (if you use a harness run)
Standard trio per `tasks/TEMPLATE-overseer-hierarchy.md` (`kimi-k2.7-code:pool` manager/executor, `kimi-k3:pool` auditor; API keys
`roles` + `max_rounds`). Worktree from `origin/develop` created from WSL and locked; put this file's absolute path in the task text.
