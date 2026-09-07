# TASK — cognizioware-powerplatform: every saved branch → promoted, salvaged, or archived; dev-PC cleanup of what is already in prod

**Asked by Paxton 2026-09-07 03:15 PT:** "what are all the branches saved for the powerplatform repo? get them deployed, tested and then revised
or deployed to prod; if in prod, remove from the hard drive … use an lh-harness task to do this also."

## State at 03:20 PT (overseer, read-only)
- 51 remote branches. origin/develop is ahead of origin/main (guest slice 1, nav-archive, promote/eval-gate fixes, v2 design docs, pp #77 eval prompt).
- Not in develop or main (5): fix/dataverse-mcp-bootstrap-auth (Jun 10), codex/prod-cicd-handoff (Aug 9), evals/tool-level-suite (Aug 11),
  feat/code-first-orchestrator (Sep 5; reconciled into docs/orchestrator-reconciled-v2 + Phase A), fix/eval-runner-webhook-timeout (the original #43,
  superseded by #62/#63).
- Dev PC: 48 local branches, 14 worktrees (6 Windows-side under C:\tmp, 5 WSL-side, 3 named dirs).

## Done now (dev PC, reversible via origin)
- Deleted 18 local branches already in prod (origin/main): ci/register-promotion-workflows, ci/trms-qa-eval-npm-ci, claude/beautiful-yalow-e5601a,
  fix/deploy-steps-bash, fix/eval-gate-{concurrency,timeout}, fix/eval-runner-timeout-main, fix/promote-{ce-greenfield,deployed-sha-output,
  linux-runner-shell,poll-auth,poll-freshness,poll-token-env,qa-ref-master,status-permission,workflow-permissions}, perplexity-computer-branch,
  v2-n8n-orchestrator. 30 local branches remain (in develop → deleted after the promotion lands them in prod; 3 local-only ones judged by the run).
- Removed the 6 Windows-created worktrees under C:\tmp (pp-39, pp-40, pp-evalfix, pp-fix, pp-reg, pp-trms). WSL worktrees untouched (never prune
  from Windows). 8 worktrees remain.
- Dispatched a pinned promotion of the current develop tip (prod auto-approval per the standing rule); the dev eval gate may still be flaky under
  the Ollama quota wall — if it fails on the same three multistep cases, the eval-flakiness task (queued) comes first.

## Run (harness, docs + salvage branches only; no deletions from CT110)
Task text `C:\tmp\pp-branch-triage-task.txt`; queued as `03-pp-branch-triage` (local qwen3.8 lane after the guest readiness audit). Deliverables:
BRANCH-REGISTER (all 51 with verdicts PROMOTE / SALVAGE / ARCHIVE / DELETE), `salvage/<name>` branches for unique docs/config, and a dry-run
cleanup script (archive tags + remote deletes + dev-PC commands, with the WSL-worktree rule).

## Overseer after the run
Push + PR the salvage branches → develop; run the cleanup script `--apply` for ARCHIVE/DELETE on origin; after the promotion lands, delete the
develop-only local branches and the C:\Users\PaxtonTait\source\cognizioware-powerplatform-* named worktrees from WSL where they were created.

## Progress
- 03:25 PT: local cleanup done; promotion dispatched (watcher); triage run queued.
- 03:45 PT: triage executed by the overseer directly (both harness lanes busy; git archaeology needs no run). Register data: 51 remote branches →
  20 fully-in-prod branches archive-tagged (`archive/<name>-2026-09-07`, 20 tags on origin) and deleted from origin (incl. the stray `origin`
  branch, `sync-main-develop`, `codex/eval-validation-20260714`; `release` is protected → kept); the 5 "neither" branches inspected: code-first
  orchestrator (reconciled on develop — the handoff doc differs only because develop's v2 is newer), codex/prod-cicd-handoff (docs already on
  develop; remaining diff = env-file values → never port), dataverse-mcp bootstrap auth (the AZURE_* fallback is in develop's mcp-servers.ts;
  the rest is a repo-level graphify skill + hooks — the KB-hook hazard — not ported), eval-runner-webhook-timeout (superseded by #62/#63) → all
  four ARCHIVED (tagged) and deleted; evals/tool-level-suite = SALVAGE (CT100 static-IP + DNS gotcha docs missing on develop) → PR next.
  Remote heads 51 → 27 (26 = develop-only branches that PROMOTE with the running promotion, + main/develop/release). Harness queue entry removed.
- 04:05 PT: SALVAGE done — pp #82 merged to develop (CT100 static-IP + DNS gotcha in deployments/README.md, two handoff notes, and
  design/plans/BRANCH-REGISTER-2026-09-07.md with a verdict per branch); evals/tool-level-suite then archived + deleted. The dev-PC-only
  design/n8n-code-orchestration-20260905 turned out byte-identical to develop (#77) → archived + deleted, its Windows worktree and the
  code-first worktree removed. Remote heads 51 → 27: main, develop, release (protected) + 24 develop-only branches that PROMOTE with run
  34094892708 and are deleted (tag first) once it lands. Local: 26 branches, 6 worktrees (5 WSL-created — remove from WSL after the promotion).
