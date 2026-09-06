# TASK — CI/e2e offload to the PVE servers + cognizioware-qa as a callable service (contracts, API, reusable workflow, client, UI buttons, Hydra integration)

**Asked by Paxton 2026-09-06 09:10–09:25 PT** (angry, and right): the dev PC ptait09 was hosting the three GitHub self-hosted runners (pp evals,
QA gates, mcp-cognizioware), the QA backend, the WSL harness runs and the overseer — "this PC is almost unusable"; "auto approve anything to get it
off this PC and into the actual compute servers correctly"; "all e2e should not be on a single dev box — dev boxes only for active local dev testing
and exception cases; regular CI/CD offloaded to our PVE servers (e.g. cognizioware-ptait07)"; "cognizioware-qa as a sub-package deployable or
accessible from UIs of the other repos … payload to trigger the QA run with payload prepared is a must-have"; "/deep-research industry practices on
how to have it easily callable from other repos' apps and instance runs on the servers"; "Hydra needs to integrate the QA, the web UX and pass over
details to test, if known".
**Overseer role:** manager AND infra executor. Done immediately (09:12–09:20 PT): deregistered runners `ptait09`, `ptait09-qa`, `ptait09-mcp` from
GitHub (local services still exist but idle; disabling them needs an elevated shell on the PC); cancelled the pp runs that were on the PC (promotion
run 16's prod eval gate — prod is deployed and its CE gate passed); killed the WSL reporting harness run 630e3309 (to be relaunched on CT110);
diagnosed ptait01 load 334 = LiteLLM zombie `timeout` children (1,574 in CT204 uat, restart dropped load to 51 — follow-up: root-cause the leak);
started CT210 `ci-runners` on ptait07 (192.168.21.170, Debian 12, 8c/16GB/80GB, nesting, Docker, Node 22, pwsh, user `runner`).
**Harness node:** CT110 on corsairai300 (`http://192.168.21.168:8799`, LH_HARNESS_WEB_TOKEN from CT110's service env) — per Paxton's rule that
runs execute on the company infra, not the dev PC.
**Repo / branch:** cogniziocompany/cognizioware-qa, `feat/qa-service-and-ci-offload` from origin/master, workspace `/home/harness/work/cognizioware-qa` on CT110.
**Task text:** `C:\tmp\ci-offload-qa-service-task.txt` (verbatim).

## Deliverables (per slice)
1. Policy + topology doc (dev boxes = local dev only; CT210 primary + CT203 secondary Linux runners; labels per repo; docker-in-LXC; QA backend on CT210).
2. Workflow patches for powerplatform + mcp-cognizioware (no Windows shells/paths; server labels; Playwright with deps; `runs-on` exception input), runner install script, QA backend compose, CT210 runbook.
3. QA-as-a-service: `qa-run-request.v1` / `qa-run-result.v1` contracts; `POST /api/qa-runs` + status + result callback; reusable `qa-run.yml` (workflow_call); `@cognizioware-qa/client` package; MCP tool spec.
4. "Run QA" UI: QA frontend form, embeddable `qa-run-button`, Hydra integration design (Ship Plan step + Reporting slice pass repo/sha/env/base_url/env_id; web-ux Playwright spec becomes a QA target).
5. Callability survey (reusable workflow + App token vs internal REST/MCP service vs Testkube-style platform vs Checks API vs ephemeral LXD/Proxmox runners) + HANDBACK with migration checklist.

## Overseer follow-through (after the run)
- Register the three runners on CT210 (one-hour tokens), move the QA backend + Postgres data from ptait09, apply the workflow patches (admin merge),
  re-run the pp prod eval gate from the server, then disable the PC's runner services (needs Paxton's elevated shell).
- Relaunch the reporting slice-1 run (630e3309's task) on CT110 once its repo clone exists there.

## Completed (2026-09-06 10:50 PT)
Run ea04634b (CT110) done in 12 rounds: 7 commits, 53 files +5307 (policy + topology; runner install + QA backend compose + 6 workflow patches with
task texts; qa-run-request/result v1 contracts, `POST /api/qa-runs` + status/result callback, reusable `qa-run.yml`, `@cognizioware-qa/client`
+ web button, MCP tool spec; Run-QA UI + Hydra integration design; callability survey; HANDBACK). Merged as cognizioware-qa #5.
Infra done by the overseer meanwhile: CT210 `ci-runners` on ptait07 (Debian 12, Docker 29.8, Node 22.23, pwsh 7.5) with runners `ct210-pp`
(self-hosted,Linux,X64,lan-deploy) and `ct210-qa` (self-hosted,Linux,X64,qa-host) online; QA backend + Postgres (9 environments, 130 runs
migrated) on CT210 :8400→4000; deploy key `id_ed25519_proxmox` installed for the runner user; qa-gate.yml probes /api/health and installs
Playwright --with-deps (cognizioware-qa #4); local QA/Hydra containers on ptait09 stopped and removed; mcp-cognizioware needs no self-hosted runner
(all ubuntu-latest). Pending: apply the powerplatform workflow patches (eval-gate, promote, deploy-all) after promotion run 16's prod gate finishes
on CT210; disable the PC's three runner services (needs an elevated shell — Paxton).
- 16:45 PT: first real deploy on the Linux runner failed (rollout steps were PowerShell under bash) → pp #74/#75 convert ci.yml + promote.yml deploy
  steps to bash; WRONG on first pass: #75 left `if ($LASTEXITCODE …)` + `$env:GITHUB_OUTPUT` inside the bash steps → still `syntax error near
  unexpected token '{'` (runs 34066670957, 34066962672); pp #76 finishes the conversion. Lesson: after a shell conversion grep the whole step for
  pwsh syntax ($LASTEXITCODE, $env:, .Trim(), Write-Host) — not just the failing line. The dev PC now runs nothing for CI.
